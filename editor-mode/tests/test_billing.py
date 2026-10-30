"""Selling the service: plans, signup from the public site, the trial and
its end, payments (bank transfer and gateway), resellers."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from editor import accounts, billing, gateway as gw, quotas
from editor.api import create_app
from editor.db import Database


@pytest.fixture
def admin(db):
    a = Database.from_url(db.admin_url)
    yield a
    a.engine.dispose()


class FakeGateway:
    """Mollie's part: payments it can be asked about, renewals it makes."""
    name = "mollie"

    def __init__(self):
        import uuid
        self.payments, self.subscriptions, self.cancelled, self.n = {}, {}, [], 0
        self.run = uuid.uuid4().hex[:6]  # tests share the PostgreSQL database

    def create_customer(self, name, email, metadata):
        return "cst_1"

    def first_payment(self, customer_id, amount_cents, currency, description, redirect_url, webhook_url, metadata):
        self.n += 1
        pid = f"tr_{self.run}{self.n}"
        self.payments[pid] = {"id": pid, "status": "open", "amount_cents": amount_cents, "currency": currency,
                              "metadata": metadata, "mandate_id": "mdt_1", "subscription_id": None}
        self.last = {"redirect": redirect_url, "webhook": webhook_url}
        return pid, f"https://pay.example/{pid}"

    def payment(self, payment_id):
        return self.payments.get(payment_id)

    def create_subscription(self, customer_id, amount_cents, currency, interval, start, description, webhook_url,
                            mandate_id, metadata):
        sid = f"sub_{self.run}{len(self.subscriptions) + 1}"
        self.subscriptions[sid] = {"amount": amount_cents, "interval": interval, "start": start, "mandate": mandate_id}
        return sid

    def cancel_subscription(self, customer_id, subscription_id):
        self.cancelled.append(subscription_id)

    def renewal(self, subscription_id, status="paid"):
        self.n += 1
        pid = f"tr_{self.run}{self.n}"
        self.payments[pid] = {"id": pid, "status": status, "amount_cents": 4900, "currency": "EUR", "metadata": {},
                              "mandate_id": "mdt_1", "subscription_id": subscription_id}
        return pid


def client(db, **kw):
    sent, told = [], []  # to the customers; to the console's people

    def mail(to, subject, body):
        (told if subject.startswith("[Redazia]") else sent).append((to, subject, body))
    app = create_app(db, mail=mail, public_url="https://editor.example.com", **kw)
    c = TestClient(app)
    c.told = told
    return c, sent


def key(db, reseller_id=None, name="sito"):
    return {"Authorization": f"Bearer {accounts.create_operator_token(Database.from_url(db.admin_url), name, reseller_id)}"}


CUSTOMER = {"kind": "company", "name": "Rossi Media srl", "email": "Amministrazione@Rossi.it", "vat_number": "IT 01234567890",
            "tax_code": "01234567890", "address": "Via Roma 1", "postal_code": "00100", "city": "Roma",
            "province": "rm", "country": "it", "sdi_code": "abc1234"}


def fresh_ids() -> dict:
    """A VAT number and tax code of their own: the free trial is given once
    per company, and on PostgreSQL the tests share a database."""
    import random
    n = f"{random.randrange(10**10, 10**11)}"
    return {"vat_number": n, "tax_code": n}


def signup(c, headers, ref="web-1", email=None, **extra):
    """On PostgreSQL the tests share a database: each one signs up its own people."""
    customer = extra.pop("customer", None) or CUSTOMER | fresh_ids()
    customer = customer | ({"email": email} if email else {})
    body = {"customer": customer, "newsroom": "Rossi News", "external_ref": ref} | extra
    return c.post("/api/operator/signup", json=body, headers=headers)


def unique(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_billing_data_are_checked():
    data = billing.clean_customer(CUSTOMER)
    assert data["vat_number"] == "01234567890" and data["email"] == "amministrazione@rossi.it"
    assert data["country"] == "IT" and data["province"] == "RM" and data["sdi_code"] == "ABC1234"
    for wrong, what in (({"vat_number": "123"}, "11 cifre"), ({"sdi_code": "12"}, "SDI"), ({"country": "Italia"}, "due lettere"),
                        ({"tax_code": "XYZ"}, "codice fiscale"), ({"pec": "non una mail"}, "PEC")):
        with pytest.raises(billing.BillingError, match=what):
            billing.clean_customer(CUSTOMER | wrong)
    # Abroad the VAT number keeps its own form.
    assert billing.clean_customer(CUSTOMER | {"country": "DE", "vat_number": "DE123456789", "tax_code": ""})["vat_number"] == "123456789"
    assert billing.add_interval(datetime(2026, 1, 31, tzinfo=UTC), "month") == datetime(2026, 2, 28, tzinfo=UTC)
    assert billing.add_interval(datetime(2026, 2, 28, tzinfo=UTC), "year").year == 2027
    assert billing.money(14900) == "149 €" and billing.money(4990) == "49,90 €"


def test_public_plans_and_signup_with_a_trial(db, admin):
    c, sent = client(db)
    plans = c.get("/api/public/plans")
    assert plans.status_code == 200 and plans.headers["access-control-allow-origin"] == "*"
    assert [p["code"] for p in plans.json()["plans"]] == ["solo", "starter", "pro", "business", "agency"] and plans.json()["trial_days"] == 7
    assert c.post("/api/operator/signup", json={"customer": CUSTOMER}).status_code == 401
    headers = key(db)
    ref, email, ids = unique("web"), f"{unique('amm')}@rossi.it", fresh_ids()
    r = signup(c, headers, ref, email, customer=CUSTOMER | ids)
    assert r.status_code == 201, r.text
    out = r.json()
    tid = out["tenant"]["tenant_id"]
    assert out["customer"]["vat_number"] == ids["vat_number"] and out["customer"]["tenants"] == [tid]
    assert out["subscription"]["status"] == "trialing" and out["subscription"]["plan_code"] == "trial"
    assert out["subscription"]["days_left"] == 7
    # The trial plan's limits are on the newsroom.
    assert out["tenant"]["plan"] == "trial" and out["tenant"]["usage"]["drafts"]["limit"] == 5
    assert out["tenant"]["usage"]["sites"]["limit"] == 1
    assert out["owner"]["email"] == email and out["owner"]["invite"]["sent"]
    assert sent[0][0] == email
    # The public site sends the same signup again: nothing new.
    again = signup(c, headers, ref, email)
    assert again.status_code == 200 and again.json()["tenant"]["tenant_id"] == tid and len(sent) == 1
    assert c.get(f"/api/operator/customers?external_ref={ref}", headers=headers).json()["customers"][0]["newsrooms"][0]["status"] == "trialing"
    bad = signup(c, headers, ref=unique("web"), customer=CUSTOMER | {"vat_number": "12"})
    assert bad.status_code == 400 and "11 cifre" in bad.text


def test_the_trial_ends_the_owner_pays_by_bank_transfer(db, admin):
    c, sent = client(db)
    headers = key(db)
    email = f"{unique('amm')}@rossi.it"
    tid = signup(c, headers, unique("web"), email).json()["tenant"]["tenant_id"]
    invite = sent[0][2].split("\n")[-2]
    c.post(f"/invite/{invite.rsplit('/', 1)[1]}", data={"password": "password del capo", "again": "password del capo"})
    page = c.get("/")
    assert "Prova gratuita: 7 giorni rimasti" in page.text and "Prossimo passo" in page.text
    now = datetime.now(UTC)
    # Two days before the end: one reminder, once.
    report = billing.lifecycle(admin, now + timedelta(days=5, hours=1), mail=lambda *a: sent.append(a),
                               public_url="https://editor.example.com")
    assert tid in report["reminded"] and tid not in report["held"]
    assert sent[-1][0] == email and "https://editor.example.com/account" in sent[-1][2]
    assert tid not in billing.lifecycle(admin, now + timedelta(days=5, hours=2))["reminded"]
    # The trial ends: proposals only, the newsroom still open, no drafts and no publishing.
    report = billing.lifecycle(admin, now + timedelta(days=7, minutes=1), mail=lambda *a: sent.append(a),
                               public_url="https://editor.example.com")
    assert tid in report["proposals_only"] and tid not in report["held"]
    assert billing.subscription(admin, tid)["status"] == "expired" and not billing.on_hold(admin, tid)
    assert sent[-1][0] == email and "solo le proposte" in sent[-1][2]
    assert "ricevi solo le proposte" in c.get("/").text
    with db.tenant(tid) as s:
        assert quotas.proposals_only(s) and not quotas.autopilot_allowed(s)
        with pytest.raises(quotas.QuotaExceeded):
            quotas.check(s, "drafts")
    assert tid not in billing.lifecycle(admin, now + timedelta(days=20))["held"]
    # A month of proposals only without a plan: the newsroom stops, its owner can still reach the account page.
    assert tid in billing.lifecycle(admin, now + timedelta(days=7 + billing.PROPOSALS_ONLY_DAYS, minutes=2))["held"]
    assert billing.on_hold(admin, tid)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    r = c.post("/login", data={"email": email, "password": "password del capo"},
               follow_redirects=False)
    assert r.headers["location"] == "/account"
    page = c.get("/account")
    assert page.status_code == 200 and "La redazione è ferma" in page.text and "Starter" in page.text
    assert "pagamento online non è ancora attivo" in page.text
    # The operator records the bank transfer: a month of Starter, the newsroom back at work.
    r = c.put(f"/api/operator/tenants/{tid}/subscription", json={"plan": "starter", "amount_cents": 4900,
                                                                 "note": "bonifico 21/10"}, headers=headers)
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["status"] == "active" and sub["plan_code"] == "starter" and 27 <= sub["days_left"] <= 32
    assert not billing.on_hold(admin, tid)
    with db.tenant(tid) as s:
        assert not quotas.proposals_only(s) and quotas.autopilot_allowed(s)
    assert c.get(f"/api/operator/tenants/{tid}", headers=headers).json()["max_drafts_month"] == 30
    # A month and the grace days later without a payment: proposals only again.
    assert tid in billing.lifecycle(admin, now + timedelta(days=32 + billing.GRACE_DAYS))["proposals_only"]


def test_online_payment_and_renewals_through_the_gateway(db, admin):
    fake = FakeGateway()
    c, sent = client(db, gateway=fake)
    op = f"{unique('op')}@example.com"
    accounts.create_user(Database.from_url(db.admin_url), op, "Operatore", "password lunga", operator=True)
    owner = f"{unique('amm')}@rossi.it"
    tid = signup(c, key(db), unique("web"), owner).json()["tenant"]["tenant_id"]
    # The console's people hear of every new newsroom.
    assert any(to == op and "Nuova redazione" in subject for to, subject, _ in c.told)
    invite = sent[0][2].split("\n")[-2]
    c.post(f"/invite/{invite.rsplit('/', 1)[1]}", data={"password": "password del capo", "again": "password del capo"})
    page = c.get("/account")
    assert "Vai al pagamento" in page.text
    r = c.post(f"/account/{tid}/checkout", data={"plan": "pro", "interval": "year"}, follow_redirects=False)
    first = f"tr_{fake.run}1"
    assert r.status_code == 303 and r.headers["location"] == f"https://pay.example/{first}"
    assert fake.payments[first]["amount_cents"] == 149000
    assert fake.last == {"redirect": f"https://editor.example.com/account/{tid}/paid",
                         "webhook": "https://editor.example.com/api/billing/mollie/webhook"}
    # Mollie's notice before the payment is done changes nothing; unknown ids get 200 too.
    assert c.post("/api/billing/mollie/webhook", data={"id": first}).status_code == 200
    assert c.post("/api/billing/mollie/webhook", data={"id": "tr_999"}).status_code == 200
    assert billing.subscription(admin, tid)["status"] == "trialing"
    fake.payments[first]["status"] = "paid"
    assert c.post("/api/billing/mollie/webhook", data={"id": first}).status_code == 200
    sub = billing.subscription(admin, tid)
    assert sub["status"] == "active" and sub["plan_code"] == "pro" and sub["provider"] == "mollie"
    # The payment is told to the customer and to the console.
    assert any("Pagamento ricevuto" in subject and "1.490" in body and "/account" in body for _, subject, body in sent)
    assert any(to == op and "Pagamento ricevuto" in subject for to, subject, _ in c.told)
    msub = f"sub_{fake.run}1"
    assert sub["billing_interval"] == "year" and sub["provider_subscription_id"] == msub
    assert fake.subscriptions[msub]["interval"] == "year" and fake.subscriptions[msub]["mandate"] == "mdt_1"
    assert fake.subscriptions[msub]["start"] == sub["current_period_end"].date()
    # The same notice twice: one subscription.
    c.post("/api/billing/mollie/webhook", data={"id": first})
    assert len(fake.subscriptions) == 1
    end = sub["current_period_end"]
    # A renewal Mollie makes on its own extends the period; a failed one marks it.
    c.post("/api/billing/mollie/webhook", data={"id": fake.renewal(msub)})
    assert billing.subscription(admin, tid)["current_period_end"] == billing.add_interval(end, "year")
    c.post("/api/billing/mollie/webhook", data={"id": fake.renewal(msub, "failed")})
    assert billing.subscription(admin, tid)["status"] == "past_due"
    assert any(to == op and "Rinnovo non riuscito" in subject for to, subject, _ in c.told)
    assert "L&#39;ultimo pagamento non è riuscito" in c.get("/").text or "ultimo pagamento non è riuscito" in c.get("/").text
    # Cancelling stops the renewals at the gateway.
    c.post(f"/account/{tid}/cancel")
    assert fake.cancelled == [msub] and billing.subscription(admin, tid)["status"] == "canceled"


def test_the_plan_is_in_the_newsroom_menu(db, admin):
    """The owner finds the plan, its usage and the payment inside the
    newsroom (Piano), and comes back there from the forms and the gateway."""
    fake = FakeGateway()
    c, sent = client(db, gateway=fake)
    op = f"{unique('op')}@example.com"
    accounts.create_user(Database.from_url(db.admin_url), op, "Operatore", "password lunga", operator=True)
    owner = f"{unique('amm')}@rossi.it"
    tid = signup(c, key(db), unique("web"), owner).json()["tenant"]["tenant_id"]
    # The console's people hear of every new newsroom.
    assert any(to == op and "Nuova redazione" in subject for to, subject, _ in c.told)
    invite = sent[0][2].split("\n")[-2]
    c.post(f"/invite/{invite.rsplit('/', 1)[1]}", data={"password": "password del capo", "again": "password del capo"})
    assert 'href="/plan"' in c.get("/").text
    page = c.get("/plan")
    assert page.status_code == 200 and "Starter" in page.text and "7 giorni rimasti" in page.text and "Il mio piano" in page.text
    assert "0 / 5" in page.text  # the trial limits articles (and sources, sites)
    assert "Quanto hai usato" in page.text and "Vai al pagamento" in page.text
    assert 'name="back" value="plan"' in page.text
    assert "Costo modello" not in page.text  # the customer sees units of the plan, not the model's cost
    r = c.post(f"/account/{tid}/customer", data={"name": "Rossi", "email": "r@example.com", "kind": "person",
                                                "tax_code": "VRDMRA80A01H501U", "back": "plan"},
               follow_redirects=False)
    assert r.headers["location"] == "/plan"
    r = c.post(f"/account/{tid}/checkout", data={"plan": "starter", "interval": "month", "back": "plan"},
               follow_redirects=False)
    assert r.headers["location"].startswith("https://pay.example/")
    assert fake.last["redirect"] == f"https://editor.example.com/account/{tid}/paid?back=plan"
    assert c.get(f"/account/{tid}/paid?back=plan", follow_redirects=False).headers["location"] == "/plan"


def test_a_manual_payment_stops_the_automatic_renewals(db, admin):
    """A bank transfer paid by the operator ends the renewals of the gateway:
    otherwise the gateway would charge again and the period would grow twice."""
    fake = FakeGateway()
    c, sent = client(db, gateway=fake)
    op = f"{unique('op')}@example.com"
    accounts.create_user(Database.from_url(db.admin_url), op, "Operatore", "password lunga", operator=True)
    owner = f"{unique('amm')}@rossi.it"
    tid = signup(c, key(db), unique("web"), owner).json()["tenant"]["tenant_id"]
    # The console's people hear of every new newsroom.
    assert any(to == op and "Nuova redazione" in subject for to, subject, _ in c.told)
    invite = sent[0][2].split("\n")[-2]
    c.post(f"/invite/{invite.rsplit('/', 1)[1]}", data={"password": "password del capo", "again": "password del capo"})
    c.post(f"/account/{tid}/customer", data={"name": "Rossi", "email": "r@example.com", "kind": "person",
                                            "tax_code": "VRDMRA80A01H501U"})
    c.post(f"/account/{tid}/checkout", data={"plan": "pro", "interval": "year"}, follow_redirects=False)
    first = f"tr_{fake.run}1"
    fake.payments[first]["status"] = "paid"
    c.post("/api/billing/mollie/webhook", data={"id": first})
    msub = billing.subscription(admin, tid)["provider_subscription_id"]
    assert msub
    # The operator records the bank transfer: the automatic renewal stops.
    r = c.put(f"/api/operator/tenants/{tid}/subscription",
              json={"plan": "starter", "amount_cents": 4900, "note": "bonifico"}, headers=key(db))
    assert r.status_code == 200, r.text
    assert fake.cancelled == [msub], "the automatic renewal was left alive"
    sub = billing.subscription(admin, tid)
    assert sub["provider"] == "manual" and sub["provider_subscription_id"] is None
    # A notice of the cancelled subscription no longer extends the period.
    end = sub["current_period_end"]
    c.post("/api/billing/mollie/webhook", data={"id": fake.renewal(msub)})
    assert billing.subscription(admin, tid)["current_period_end"] == end


def test_billing_data_from_the_account_page(db, admin):
    fake = FakeGateway()
    c, sent = client(db, gateway=fake)
    room = c.post("/api/operator/tenants", json={"name": "Senza dati", "owner_email": f"{unique('o')}@example.com"},
                  headers=key(db)).json()["tenant"]["tenant_id"]
    billing.start(admin, room, "trial")
    invite = sent[0][2].split("\n")[-2]
    c.post(f"/invite/{invite.rsplit('/', 1)[1]}", data={"password": "password del capo", "again": "password del capo"})
    r = c.post(f"/account/{room}/checkout", data={"plan": "starter"}, follow_redirects=False)
    assert "dati di fatturazione" in r.headers["set-cookie"] or "dati%20di%20fatturazione" in r.headers["set-cookie"]
    c.post(f"/account/{room}/customer", data={"name": "Verdi", "email": "v@example.com", "kind": "person",
                                              "tax_code": "VRDMRA80A01H501U"})
    assert billing.customer_of(admin, room)["tax_code"] == "VRDMRA80A01H501U"
    r = c.post(f"/account/{room}/checkout", data={"plan": "starter"}, follow_redirects=False)
    assert r.headers["location"].startswith("https://pay.example/"), r.headers.get("set-cookie")


@pytest.mark.postgres
def test_a_reseller_sees_only_its_customers(db, admin):
    c, _ = client(db)
    mine = key(db)
    other = signup(c, mine, unique("diretto"), f"{unique('d')}@rossi.it").json()["tenant"]["tenant_id"]
    r = c.post("/api/operator/resellers", json={"name": "Agenzia Blu", "email": "info@blu.it", "discount_pct": 20},
               headers=mine)
    assert r.status_code == 201
    blu = {"Authorization": f"Bearer {r.json()['key']}"}
    ref = unique("cliente")
    out = signup(c, blu, ref, f"{unique('c')}@rossi.it", plan="pro")
    assert out.status_code == 201, out.text
    theirs = out.json()["tenant"]["tenant_id"]
    sub = out.json()["subscription"]
    # A plan chosen by the reseller is active, billed by it, open-ended.
    assert sub["status"] == "active" and sub["provider"] == "reseller" and sub["current_period_end"] is None
    assert [t["tenant_id"] for t in c.get("/api/operator/tenants", headers=blu).json()["tenants"]] == [theirs]
    assert len(c.get("/api/operator/customers", headers=blu).json()["customers"]) == 1
    assert c.get(f"/api/operator/tenants/{other}", headers=blu).status_code == 404
    assert c.put(f"/api/operator/tenants/{other}/subscription", json={"plan": "business"}, headers=blu).status_code == 404
    assert c.patch(f"/api/operator/tenants/{theirs}", json={"limits": {"max_sites": 99}}, headers=blu).status_code == 403
    assert c.patch(f"/api/operator/tenants/{theirs}", json={"active": False}, headers=blu).status_code == 200
    assert c.post("/api/operator/tenants", json={"name": "x", "owner_email": "x@example.com"}, headers=blu).status_code == 403
    assert c.get("/api/operator/resellers", headers=blu).status_code == 403
    r = c.put(f"/api/operator/tenants/{theirs}/subscription", json={"plan": "business"}, headers=blu)
    assert r.status_code == 200 and r.json()["plan_code"] == "business" and r.json()["provider"] == "reseller"
    # The same external ref from two resellers: two customers.
    assert signup(c, mine, ref, f"{unique('c')}@rossi.it").status_code == 201
    # A reseller turned off: its keys stop at once.
    rid = next(x["id"] for x in billing.resellers(admin) if x["email"] == "info@blu.it" and x["active"])
    billing.update_reseller(admin, rid, {"active": False})
    assert c.get("/api/operator/tenants", headers=blu).status_code == 401
    assert theirs not in billing.lifecycle(admin, datetime.now(UTC) + timedelta(days=400))["held"]


def test_mollie_client_speaks_the_v2_api():
    with respx.mock(base_url=gw.MOLLIE_API) as mock:
        mock.post("/customers").respond(201, json={"id": "cst_9"})
        pay = mock.post("/payments").respond(201, json={"id": "tr_9", "_links": {"checkout": {"href": "https://m/pay"}}})
        mock.get("/payments/tr_9").respond(200, json={"id": "tr_9", "status": "paid", "amount": {"currency": "EUR", "value": "149.00"},
                                                      "metadata": {"plan": "pro"}, "mandateId": "mdt_9", "customerId": "cst_9"})
        sub = mock.post("/customers/cst_9/subscriptions").respond(201, json={"id": "sub_9"})
        cancel = mock.delete("/customers/cst_9/subscriptions/sub_9").respond(204)
        m = gw.Mollie("test_abc", httpx.Client())
        assert m.create_customer("Rossi", "r@example.com", {}) == "cst_9"
        assert m.first_payment("cst_9", 4990, "EUR", "Starter", "https://r", "https://w", {"plan": "starter"}) == ("tr_9", "https://m/pay")
        body = pay.calls.last.request.read().decode()
        assert '"sequenceType":"first"' in body.replace(" ", "") and '"value":"49.90"' in body.replace(" ", "")
        assert m.payment("tr_9") | {} == {"id": "tr_9", "status": "paid", "amount_cents": 14900, "currency": "EUR",
                                         "metadata": {"plan": "pro"}, "mandate_id": "mdt_9", "subscription_id": None,
                                         "customer_id": "cst_9"}
        assert m.payment("cst_nope") is None
        from datetime import date
        assert m.create_subscription("cst_9", 14900, "EUR", "year", date(2027, 10, 21), "Pro", "https://w", "mdt_9", {}) == "sub_9"
        body = sub.calls.last.request.read().decode().replace(" ", "")
        assert '"interval":"12months"' in body and '"startDate":"2027-10-21"' in body and '"mandateId":"mdt_9"' in body
        m.cancel_subscription("cst_9", "sub_9")
        assert cancel.called
    with pytest.raises(gw.GatewayError):
        gw.Mollie("sk_wrong")


def test_the_console_shows_customers_resellers_and_plans(db, admin):
    c, sent = client(db)
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    ids = fresh_ids()
    tid = signup(c, key(db), unique("web"), f"{unique('amm')}@rossi.it", customer=CUSTOMER | ids).json()["tenant"]["tenant_id"]
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin")
    assert page.status_code == 200
    for words in ("Clienti", "Rossi Media srl", f"P.IVA {ids['vat_number']}", "Seconde prove negate", "SDI ABC1234", "Rivenditori", "Piani",
                  "Prova · In prova", "Abbonamento"):
        assert words in page.text, words
    r = c.post("/admin/plans/starter", data={"name": "Starter", "price_month": "59,00", "price_year": "590",
                                              "trial_days": "0", "max_sites": "1", "max_drafts_month": "40", "public": "1"},
               follow_redirects=False)
    assert r.status_code == 303
    starter = next(p for p in c.get("/api/public/plans").json()["plans"] if p["code"] == "starter")
    assert starter["price_month_cents"] == 5900 and starter["price_year_cents"] == 59000 and starter["max_drafts_month"] == 40
    # A bank transfer recorded from the console.
    c.post(f"/admin/subscriptions/{tid}", data={"plan": "starter", "interval": "month", "paid_until": "2030-01-31",
                                                "amount": "59"})
    sub = billing.subscription(admin, tid)
    assert sub["status"] == "active" and sub["current_period_end"].date().isoformat() == "2030-01-31"
    r = c.post("/admin/resellers", data={"name": "Agenzia Verde", "email": "verde@example.com", "discount": "15"})
    assert "edo_" in r.text and "Agenzia Verde" in r.text
    billing.save_plan(admin, "starter", {"price_month_cents": 4900, "price_year_cents": 49000, "max_drafts_month": 30})


def test_a_paid_plan_chosen_on_the_site_waits_for_its_payment(db, admin):
    c, sent = client(db)
    r = signup(c, key(db), unique("web"), f"{unique('amm')}@rossi.it", plan="pro", interval="year", language="de")
    assert r.status_code == 201, r.text
    out = r.json()
    sub = out["subscription"]
    # The plan bought, stopped until it is paid: no trial.
    assert sub["status"] == "expired" and sub["plan_code"] == "pro"
    assert billing.on_hold(admin, out["tenant"]["tenant_id"])
    assert sub["desired_plan"] == "pro" and sub["desired_interval"] == "year"
    assert out["tenant"]["language"] == "de"
    # Paid by bank transfer: the wish is now the plan.
    tid = out["tenant"]["tenant_id"]
    r = c.put(f"/api/operator/tenants/{tid}/subscription", json={"plan": "pro", "amount_cents": 149000, "interval": "year"},
              headers=key(db))
    assert r.status_code == 200, r.text
    assert r.json()["plan_code"] == "pro" and r.json()["status"] == "active" and r.json()["desired_plan"] is None


def test_the_billing_data_of_a_newsroom_that_does_not_exist_are_refused(db, admin):
    c, _ = client(db)
    accounts.create_user(Database.from_url(db.admin_url), "op@example.com", "Operatore",
                         "password lunga", operator=True)
    assert c.post("/login", data={"email": "op@example.com", "password": "password lunga"}).status_code == 200
    r = c.post("/account/editor-non-esiste/customer",
               data={"kind": "company", "name": "Rossi Media srl", "email": "amministrazione@rossi.it"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/account")
    assert "Redazione non trovata" in c.get("/account").text


@pytest.mark.postgres
def test_a_second_newsroom_for_someone_with_an_account_is_announced(db, admin):
    c, sent = client(db)
    headers = key(db)
    email = f"{unique('amm')}@rossi.it"
    signup(c, headers, unique("web"), email)
    token = sent[0][2].split("\n")[-2].rsplit("/", 1)[1]
    c.post(f"/invite/{token}", data={"password": "password del capo", "again": "password del capo"})
    out = signup(c, headers, unique("web"), email, newsroom="Rossi Sport").json()
    # No password to choose: the email says where to sign in.
    assert out["owner"]["invite"]["existing"] and out["owner"]["invite"]["sent"]
    assert out["owner"]["invite"]["url"] == "https://editor.example.com/login"
    assert sent[-1][0] == email and "Rossi Sport" in sent[-1][1] and "https://editor.example.com/login" in sent[-1][2]


def test_the_free_trial_is_given_once():
    from editor import trials
    assert trials.norm_email(" Mario.Rossi+redazia@GoogleMail.com") == "mariorossi@gmail.com"
    assert trials.norm_email("m.rossi+x@rossi.it") == "m.rossi@rossi.it"
    assert trials.norm_phone("+39 333 123 4567") == trials.norm_phone("3331234567") == "331234567"
    assert trials.norm_domain("https://WWW.Rossi.it/blog") == "rossi.it"
    assert trials.masked("email", "mariorossi@gmail.com") == "ma…@gmail.com"


@pytest.mark.postgres
def test_a_second_trial_waits_for_a_plan(db, admin):
    from editor import trials
    c, sent = client(db)
    headers = key(db)
    ids = fresh_ids()
    first = signup(c, headers, unique("web"), f"{unique('amm')}@rossi.it", customer=CUSTOMER | ids).json()
    assert first["subscription"]["status"] == "trialing" and first["trial_used"] == []
    # Same company, a new email: the newsroom is made, on hold until a plan is paid.
    again = signup(c, headers, unique("web"), f"{unique('altro')}@rossi.it", customer=CUSTOMER | ids,
                   newsroom="Rossi Bis").json()
    tid = again["tenant"]["tenant_id"]
    assert again["subscription"]["status"] == "expired" and {x["kind"] for x in again["trial_used"]} == {"vat", "tax_code"}
    assert again["owner"]["invite"]["sent"]  # the owner can still sign in and pay
    with admin.engine.connect() as conn:
        refused = trials.refusals(conn)
    assert refused[0]["newsroom"] == tid and refused[0]["reasons"][0]["newsroom"] == first["tenant"]["tenant_id"]
    # A site that had its trial in the first newsroom cannot have it again.
    domain = f"{unique('rossi')}.it"
    trials.check_domain(admin, first["tenant"]["tenant_id"], domain)
    third = signup(c, headers, unique("web"), f"{unique('terzo')}@rossi.it", newsroom="Rossi Ter").json()
    with pytest.raises(trials.TrialUsed):
        trials.check_domain(admin, third["tenant"]["tenant_id"], f"https://www.{domain}/")
    trials.check_domain(admin, third["tenant"]["tenant_id"], f"{unique('nuovo')}.it")
    # The operator sees why.
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin").text
    assert "Seconde prove negate" in page and f"…{ids['vat_number'][-4:]}" in page


def test_solo_has_no_automatic_newsroom_and_a_buyer_goes_to_payment(db, admin):
    c, sent = client(db)
    email = f"{unique('amm')}@rossi.it"
    out = signup(c, key(db), unique("web"), email, plan="solo").json()
    tid = out["tenant"]["tenant_id"]
    # Bought on the site: setting the password leads straight to the payment.
    invite = sent[0][2].split("\n")[-2]
    r = c.post(f"/invite/{invite.rsplit('/', 1)[1]}", data={"password": "password del capo", "again": "password del capo"},
               follow_redirects=False)
    assert r.headers["location"] == "/account"
    page = c.get("/account").text
    assert "Hai scelto il piano Solo</strong>" in page and "Da pagare" in page
    # Paid: Solo's limits, and no automatic newsroom.
    r = c.put(f"/api/operator/tenants/{tid}/subscription", json={"plan": "solo", "amount_cents": 1990}, headers=key(db))
    assert r.status_code == 200, r.text
    with db.tenant(tid) as s:
        assert not quotas.autopilot_allowed(s) and not quotas.proposals_only(s)
        assert quotas.usage(s).plan["max_drafts_month"] == 10
    if db.sqlite:
        return  # one newsroom only
    # The same person buys a second newsroom: no new password, the email
    # says to complete the payment and signing in leads to it.
    c2, sent2 = client(db)
    r2 = signup(c2, key(db), unique("web"), email, plan="solo")
    assert sent2, r2.text
    assert "completa il pagamento" in sent2[-1][1] and sent2[-1][2].rstrip().endswith("/login")
    r = c2.post("/login", data={"email": email, "password": "password del capo"}, follow_redirects=False)
    assert r.headers["location"] == "/account"
