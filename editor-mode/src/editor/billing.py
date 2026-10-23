"""Customers, resellers, plans and subscriptions.

The public site sends a new customer through the operator API (signup):
the customer's billing data, a newsroom on the trial plan, its owner
invited by email. When the trial ends without a payment the newsroom is
put on hold: its scheduled work stops, its data stay, and its owner can
still sign in to choose a plan and pay (/account). A payment (Mollie, a
bank transfer recorded by the operator, or a reseller's word) starts or
extends the paid period; when that period ends without a new payment the
newsroom goes on hold again after GRACE_DAYS.

A reseller sells newsrooms to its own customers with an operator key
bound to it: it sees and changes only those, and bills them itself
(provider "reseller": no trial expiry unless it sets a period).

Everything here goes through the owner connection (`admin`).
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, insert, select, update

from . import accounts, i18n, newsrooms
from . import models as m
from .i18n import N_, t

log = logging.getLogger(__name__)

TRIAL_PLAN = "trial"
GRACE_DAYS = 3  # after a paid period ends, before the hold
REMIND_DAYS = 2  # before the trial ends
INTERVALS = ("month", "year")
PROVIDERS = ("manual", "mollie", "reseller")
STATUS_LABELS = {
    "trialing": N_("In prova"),
    "active": N_("Attivo"),
    "past_due": N_("Pagamento non riuscito"),
    "canceled": N_("Disdetto"),
    "expired": N_("Scaduto"),
}
CUSTOMER_FIELDS = ("kind", "name", "email", "phone", "vat_number", "tax_code", "address", "postal_code", "city",
                   "province", "country", "sdi_code", "pec")
PLAN_LIMITS = ("max_sites", "max_active_sources", "max_items_per_day", "max_drafts_month", "max_llm_cost_month")


class BillingError(ValueError):
    pass


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def add_interval(when: datetime, interval: str) -> datetime:
    """One month (or year) later; the 31st becomes the month's last day."""
    months = 12 if interval == "year" else 1
    y, mo = divmod(when.month - 1 + months, 12)
    year, month = when.year + y, mo + 1
    return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))


def money(cents: int | None, currency: str = "EUR") -> str:
    if cents is None:
        return "–"
    whole, rest = divmod(int(cents), 100)
    symbol = "€" if currency == "EUR" else currency
    return f"{whole:,}".replace(",", ".") + (f",{rest:02d}" if rest else "") + f" {symbol}"


# --- plans ---------------------------------------------------------------------------

def plans(admin, public_only: bool = False) -> list[dict]:
    p = m.plans
    q = select(p).order_by(p.c.sort, p.c.code)
    if public_only:
        q = q.where(p.c.public)
    with accounts._read(admin) as conn:
        return [dict(r._mapping) for r in conn.execute(q).all()]


def get_plan(conn, code: str):
    plan = conn.execute(select(m.plans).where(m.plans.c.code == code)).first()
    if plan is None:
        raise BillingError(t("piano sconosciuto: {code}", code=code))
    return plan


def price(plan, interval: str) -> int | None:
    return plan.price_year_cents if interval == "year" else plan.price_month_cents


def save_plan(admin, code: str, fields: dict) -> None:
    """Change a plan of the catalogue (name, prices, limits, trial days,
    shown on the public site or not). Newsrooms already on it keep their
    limits until their plan is applied again (the next payment)."""
    allowed = {"name", "description", "price_month_cents", "price_year_cents", "trial_days", "max_sites",
               "max_languages", "max_active_sources", "max_items_per_day", "max_drafts_month", "max_llm_cost_month",
               "public", "sort"}
    values = {k: v for k, v in fields.items() if k in allowed}
    for k, v in values.items():
        if k not in ("name", "description", "public") and v is not None and (isinstance(v, bool) or v < 0):
            raise BillingError(t("{field}: un numero di zero o più", field=k))
    if "name" in values and not str(values["name"]).strip():
        raise BillingError(t("il piano ha bisogno di un nome"))
    with admin.engine.begin() as conn:
        if not conn.execute(update(m.plans).where(m.plans.c.code == code).values(**values)).rowcount:
            raise BillingError(t("piano sconosciuto: {code}", code=code))


def create_plan(admin, code: str, name: str) -> str:
    """A new plan in the catalogue, hidden from the public site until its
    prices and limits are set and it is made public."""
    code = (code or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", code):
        raise BillingError(t("il codice è fatto di lettere minuscole, numeri e trattini (es. pro-plus)"))
    name = (name or "").strip()[:100]
    if not name:
        raise BillingError(t("il piano ha bisogno di un nome"))
    with admin.engine.begin() as conn:
        if conn.execute(select(m.plans.c.code).where(m.plans.c.code == code)).first() is not None:
            raise BillingError(t("il piano {code} esiste già", code=code))
        last = conn.execute(select(func.max(m.plans.c.sort))).scalar() or 0
        conn.execute(insert(m.plans).values(code=code, name=name, description="", currency="EUR", trial_days=0,
                                            public=False, sort=last + 10))
    return code


def delete_plan(admin, code: str) -> str:
    """Remove a plan nobody is on. The trial plan stays: new sign-ups start on it."""
    if code == TRIAL_PLAN:
        raise BillingError(t("il piano di prova serve a chi si iscrive: non si elimina"))
    with admin.engine.begin() as conn:
        plan = conn.execute(select(m.plans).where(m.plans.c.code == code)).first()
        if plan is None:
            raise LookupError(code)
        used = conn.execute(select(func.count()).select_from(m.subscriptions).where(
            (m.subscriptions.c.plan_code == code) | (m.subscriptions.c.desired_plan == code))).scalar()
        used += conn.execute(select(func.count()).select_from(m.tenants).where(m.tenants.c.plan == code)).scalar()
        if used:
            raise BillingError(t("{n} redazioni usano {name}: spostale su un altro piano, oppure toglilo dal sito "
                                 "pubblico", n=used, name=plan.name))
        conn.execute(delete(m.plans).where(m.plans.c.code == code))
    return plan.name


def apply_plan(admin, tenant_id: str, plan, core=None) -> str | None:
    """The plan's limits on the newsroom (and its model budget in the Core)."""
    return newsrooms.update_newsroom(admin, tenant_id, {
        "plan": plan.code, "limits": {k: getattr(plan, k) for k in PLAN_LIMITS}}, core)["warning"]


# --- customers -----------------------------------------------------------------------

def clean_customer(spec: dict, partial: bool = False) -> dict:
    """The billing data, checked: what an Italian invoice needs (VAT number
    or tax code, address, SDI code or PEC) and the same for other countries."""
    out = {}
    for k in CUSTOMER_FIELDS:
        if k in spec and spec[k] is not None:
            out[k] = str(spec[k]).strip()[:200]
    if not partial:
        for k in ("name", "email"):
            if not out.get(k):
                raise BillingError(t("manca il campo {field}", field=k))
    if "kind" in out and out["kind"] not in ("company", "person"):
        raise BillingError(t("kind è company o person"))
    if "name" in out and not out["name"]:
        raise BillingError(t("serve un nome o una ragione sociale"))
    if "email" in out:
        out["email"] = accounts.normalise_email(out["email"])
    if out.get("pec"):
        try:
            out["pec"] = accounts.normalise_email(out["pec"])
        except accounts.AccountError as e:
            raise BillingError(t("PEC non valida")) from e
    if "country" in out:
        out["country"] = out["country"].upper() or "IT"
        if not re.fullmatch(r"[A-Z]{2}", out["country"]):
            raise BillingError(t("il paese è un codice di due lettere (IT, DE, FR...)"))
    country = out.get("country", "IT")
    if out.get("vat_number"):
        vat = re.sub(r"[\s.-]", "", out["vat_number"]).upper()
        if vat.startswith(country) and country != "IT" or vat.startswith("IT"):
            vat = vat[2:]
        if country == "IT" and not re.fullmatch(r"\d{11}", vat):
            raise BillingError(t("la partita IVA italiana ha 11 cifre"))
        out["vat_number"] = vat
    if out.get("tax_code"):
        out["tax_code"] = re.sub(r"\s", "", out["tax_code"]).upper()
        if country == "IT" and not re.fullmatch(r"[A-Z0-9]{11}|[A-Z0-9]{16}", out["tax_code"]):
            raise BillingError(t("il codice fiscale ha 16 caratteri (11 cifre per un'azienda)"))
    if out.get("sdi_code"):
        out["sdi_code"] = out["sdi_code"].upper()
        if not re.fullmatch(r"[A-Z0-9]{6,7}", out["sdi_code"]):
            raise BillingError(t("il codice destinatario SDI ha 7 caratteri (6 per la pubblica amministrazione)"))
    if out.get("province"):
        out["province"] = out["province"].upper()
    return out


def _customer_row(conn, customer_id: int, reseller_id: int | None = None):
    c = m.customers
    q = select(c).where(c.c.id == customer_id)
    if reseller_id is not None:
        q = q.where(c.c.reseller_id == reseller_id)
    return conn.execute(q).first()


def upsert_customer(conn, spec: dict, reseller_id: int | None = None) -> tuple[int, bool]:
    """The customer with this external_ref (for this reseller), updated, or
    a new one. Returns (id, created)."""
    data = clean_customer(spec)
    ref = (spec.get("external_ref") or "").strip()[:200] or None
    c = m.customers
    if ref is not None:
        q = select(c.c.id).where(c.c.external_ref == ref)
        q = q.where(c.c.reseller_id == reseller_id) if reseller_id is not None else q.where(c.c.reseller_id.is_(None))
        old = conn.execute(q).scalar()
        if old is not None:
            conn.execute(update(c).where(c.c.id == old).values(**data, updated_at=datetime.now(UTC)))
            return old, False
    new = conn.execute(insert(c).values(**data, external_ref=ref, reseller_id=reseller_id)
                       .returning(c.c.id)).scalar_one()
    return new, True


def update_customer(admin, customer_id: int, spec: dict, reseller_id: int | None = None) -> dict:
    data = clean_customer(spec, partial=True)
    with admin.engine.begin() as conn:
        if _customer_row(conn, customer_id, reseller_id) is None:
            raise LookupError(customer_id)
        if data:
            conn.execute(update(m.customers).where(m.customers.c.id == customer_id)
                         .values(**data, updated_at=datetime.now(UTC)))
    return customer(admin, customer_id)


def delete_customer(admin, customer_id: int) -> str:
    """Remove a customer that has no newsroom any more (its data are then
    no longer needed for invoices to come)."""
    with admin.engine.begin() as conn:
        row = _customer_row(conn, customer_id)
        if row is None:
            raise LookupError(customer_id)
        rooms = conn.execute(select(func.count()).select_from(m.tenants).where(
            m.tenants.c.customer_id == customer_id)).scalar()
        if rooms:
            raise BillingError(t("{name} ha ancora {n} redazioni: eliminale prima, o collegale a un altro cliente",
                                 name=row.name, n=rooms))
        conn.execute(delete(m.customers).where(m.customers.c.id == customer_id))
    return row.name


def customer(admin, customer_id: int, reseller_id: int | None = None) -> dict | None:
    with accounts._read(admin) as conn:
        row = _customer_row(conn, customer_id, reseller_id)
        if row is None:
            return None
        rooms = conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.customer_id == customer_id)
                             .order_by(m.tenants.c.tenant_id)).scalars().all()
    return {**dict(row._mapping), "tenants": list(rooms)}


def customers(admin, reseller_id: int | None = None, external_ref: str | None = None) -> list[dict]:
    """Every customer (of a reseller) with its newsrooms and subscriptions."""
    c, t, s = m.customers, m.tenants, m.subscriptions
    q = select(c).order_by(c.c.name)
    if reseller_id is not None:
        q = q.where(c.c.reseller_id == reseller_id)
    if external_ref is not None:
        q = q.where(c.c.external_ref == external_ref)
    with accounts._read(admin) as conn:
        rows = conn.execute(q).all()
        rooms = conn.execute(select(t.c.tenant_id, t.c.name, t.c.active, t.c.hold, t.c.customer_id, s.c.plan_code,
                                    s.c.status, s.c.trial_ends_at, s.c.current_period_end, s.c.provider,
                                    s.c.billing_interval)
                             .outerjoin(s, s.c.tenant_id == t.c.tenant_id)
                             .where(t.c.customer_id.is_not(None)).order_by(t.c.name)).all()
    by = {}
    for r in rooms:
        by.setdefault(r.customer_id, []).append(dict(r._mapping))
    return [{**dict(r._mapping), "newsrooms": by.get(r.id, [])} for r in rows]


def customer_of(admin, tenant_id: str) -> dict | None:
    with accounts._read(admin) as conn:
        cid = conn.execute(select(m.tenants.c.customer_id).where(m.tenants.c.tenant_id == tenant_id)).scalar()
    return customer(admin, cid) if cid is not None else None


def set_tenant_customer(admin, tenant_id: str, spec: dict) -> int:
    """Billing data of a newsroom, from its owner: the customer it has,
    updated, or a new one."""
    with admin.engine.begin() as conn:
        t = conn.execute(select(m.tenants.c.customer_id).where(m.tenants.c.tenant_id == tenant_id)).first()
        if t is None:
            raise LookupError(tenant_id)
        if t.customer_id is not None:
            data = clean_customer(spec)
            conn.execute(update(m.customers).where(m.customers.c.id == t.customer_id)
                         .values(**data, updated_at=datetime.now(UTC)))
            return t.customer_id
        cid, _ = upsert_customer(conn, spec)
        conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(customer_id=cid))
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id).values(customer_id=cid))
        return cid


def reseller_of_tenant(admin, tenant_id: str) -> int | None:
    with accounts._read(admin) as conn:
        return conn.execute(select(m.customers.c.reseller_id).join(m.tenants, m.tenants.c.customer_id == m.customers.c.id)
                            .where(m.tenants.c.tenant_id == tenant_id)).scalar()


def tenants_of_reseller(admin, reseller_id: int) -> set[str]:
    with accounts._read(admin) as conn:
        return set(conn.execute(select(m.tenants.c.tenant_id).join(m.customers, m.tenants.c.customer_id == m.customers.c.id)
                                .where(m.customers.c.reseller_id == reseller_id)).scalars())


# --- resellers -----------------------------------------------------------------------

def create_reseller(admin, name: str, email: str, discount_pct: float = 0, notes: str = "") -> int:
    name = (name or "").strip()[:100]
    if not name:
        raise BillingError(t("il rivenditore ha bisogno di un nome"))
    if not 0 <= discount_pct <= 100:
        raise BillingError(t("lo sconto è una percentuale da 0 a 100"))
    with admin.engine.begin() as conn:
        return conn.execute(insert(m.resellers).values(name=name, email=accounts.normalise_email(email),
                                                       discount_pct=discount_pct, notes=(notes or "")[:1000],
                                                       active=True).returning(m.resellers.c.id)).scalar_one()


def update_reseller(admin, reseller_id: int, fields: dict) -> None:
    values = {k: v for k, v in fields.items() if k in ("name", "email", "discount_pct", "notes", "active") and v is not None}
    if "email" in values:
        values["email"] = accounts.normalise_email(values["email"])
    if "discount_pct" in values and not 0 <= values["discount_pct"] <= 100:
        raise BillingError(t("lo sconto è una percentuale da 0 a 100"))
    with admin.engine.begin() as conn:
        if not conn.execute(update(m.resellers).where(m.resellers.c.id == reseller_id).values(**values)).rowcount:
            raise LookupError(reseller_id)
        if values.get("active") is False:  # its keys stop at once
            conn.execute(update(m.operator_tokens).where(m.operator_tokens.c.reseller_id == reseller_id,
                                                         m.operator_tokens.c.revoked_at.is_(None))
                         .values(revoked_at=datetime.now(UTC)))


def delete_reseller(admin, reseller_id: int) -> str:
    """Remove a reseller without customers, with its API keys."""
    with admin.engine.begin() as conn:
        r = conn.execute(select(m.resellers.c.name).where(m.resellers.c.id == reseller_id)).first()
        if r is None:
            raise LookupError(reseller_id)
        n = conn.execute(select(func.count()).select_from(m.customers).where(
            m.customers.c.reseller_id == reseller_id)).scalar()
        if n:
            raise BillingError(t("{name} ha {n} clienti: disattivalo invece di eliminarlo", name=r.name, n=n))
        conn.execute(delete(m.operator_tokens).where(m.operator_tokens.c.reseller_id == reseller_id))
        conn.execute(delete(m.resellers).where(m.resellers.c.id == reseller_id))
    return r.name


def resellers(admin) -> list[dict]:
    r, c = m.resellers, m.customers
    with accounts._read(admin) as conn:
        rows = conn.execute(select(r).order_by(r.c.name)).all()
        counts = dict(conn.execute(select(c.c.reseller_id, func.count()).where(c.c.reseller_id.is_not(None))
                                   .group_by(c.c.reseller_id)).all())
    return [{**dict(x._mapping), "customers": counts.get(x.id, 0)} for x in rows]


def reseller(admin, reseller_id: int):
    with accounts._read(admin) as conn:
        return conn.execute(select(m.resellers).where(m.resellers.c.id == reseller_id)).first()


# --- subscriptions -------------------------------------------------------------------

def subscription(admin, tenant_id: str) -> dict | None:
    s, p = m.subscriptions, m.plans
    with accounts._read(admin) as conn:
        row = conn.execute(select(s, p.c.name.label("plan_name"), p.c.price_month_cents, p.c.price_year_cents,
                                  p.c.currency).join(p, p.c.code == s.c.plan_code)
                           .where(s.c.tenant_id == tenant_id)).first()
    return describe(row) if row is not None else None


def subscriptions(admin) -> dict[str, dict]:
    """Every newsroom's subscription, by tenant id (the console)."""
    s, p = m.subscriptions, m.plans
    with accounts._read(admin) as conn:
        rows = conn.execute(select(s, p.c.name.label("plan_name"), p.c.price_month_cents, p.c.price_year_cents,
                                   p.c.currency).join(p, p.c.code == s.c.plan_code)).all()
    return {r.tenant_id: describe(r) for r in rows}


def describe(row, now: datetime | None = None) -> dict:
    """A subscription with what a page says about it."""
    out = dict(row._mapping)
    now = _now(now)
    out["label"] = t(STATUS_LABELS[out["status"]]) if out["status"] in STATUS_LABELS else out["status"]
    out["days_left"] = None
    end = out["trial_ends_at"] if out["status"] == "trialing" else out["current_period_end"]
    if end is not None:
        out["days_left"] = max(0, (end - now).days + (1 if (end - now).seconds else 0))
    return out


def on_hold(admin, tenant_id: str) -> bool:
    with accounts._read(admin) as conn:
        return conn.execute(select(m.tenants.c.hold).where(m.tenants.c.tenant_id == tenant_id)).scalar() == "billing"


def _hold(conn, tenant_id: str) -> None:
    conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(active=False, hold="billing"))


def _release(conn, tenant_id: str) -> None:
    conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id, m.tenants.c.hold == "billing")
                 .values(active=True, hold=None))


def _leave_the_gateway(admin, tenant_id: str, sub: dict, values: dict, gateway) -> None:
    """The subscription stops being paid at the gateway: its automatic
    renewals are cancelled and its id forgotten. Without this the gateway
    would charge again at the next renewal and the notice would extend the
    period a second time: the customer would pay twice for one period."""
    if sub.get("provider") != "mollie" or not sub.get("provider_subscription_id"):
        return
    if gateway is None:
        raise BillingError(t("il pagamento online non è configurato: i rinnovi su Mollie restano attivi"))
    customer = customer_of(admin, tenant_id)
    if customer is not None:
        try:
            gateway.cancel_subscription(customer["provider_customer_id"], sub["provider_subscription_id"])
        except Exception as e:
            log.exception("the automatic renewals of %s were not cancelled", tenant_id)
            raise BillingError(t("i rinnovi automatici su Mollie non sono stati annullati, riprova")) from e
    values["provider_subscription_id"] = None


def start(admin, tenant_id: str, plan_code: str, customer_id: int | None = None, provider: str = "manual",
          interval: str = "month", paid_until: datetime | None = None, core=None, now: datetime | None = None,
          gateway=None) -> dict:
    """The newsroom's subscription: a plan with a trial starts its trial,
    any other plan is active (until `paid_until`, or open-ended when not
    given: a reseller or the operator decides). Replaces an existing one."""
    now = _now(now)
    if interval not in INTERVALS:
        raise BillingError(t("l'intervallo è month o year"))
    if provider not in PROVIDERS:
        raise BillingError(t("provider: uno di {names}", names=", ".join(PROVIDERS)))
    old = subscription(admin, tenant_id)
    with admin.engine.begin() as conn:
        plan = get_plan(conn, plan_code)
        if conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.tenant_id == tenant_id)).first() is None:
            raise LookupError(tenant_id)
        trial = plan.trial_days > 0
        values = {"plan_code": plan.code, "billing_interval": interval, "provider": provider,
                  "status": "trialing" if trial else "active",
                  "trial_ends_at": now + timedelta(days=plan.trial_days) if trial else None,
                  "current_period_end": None if trial else paid_until, "reminded_at": None, "updated_at": now}
        if not trial:  # the plan wished for during the trial is now the plan
            values |= {"desired_plan": None, "desired_interval": None}
        if customer_id is not None:
            values["customer_id"] = customer_id
        s = m.subscriptions
        if conn.execute(select(s.c.id).where(s.c.tenant_id == tenant_id)).first() is None:
            conn.execute(insert(s).values(tenant_id=tenant_id, created_at=now, **values))
        else:
            if old is not None and old.get("provider") != provider:
                _leave_the_gateway(admin, tenant_id, old, values, gateway)
            conn.execute(update(s).where(s.c.tenant_id == tenant_id).values(**values))
        if customer_id is not None:
            conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(customer_id=customer_id))
        if paid_until is None or paid_until > now or trial:
            _release(conn, tenant_id)
    apply_plan(admin, tenant_id, plan, core)
    return subscription(admin, tenant_id)


def record_payment(admin, tenant_id: str, amount_cents: int, until: datetime | None, by: str, note: str = "",
                   plan_code: str | None = None, interval: str | None = None, core=None,
                   now: datetime | None = None, gateway=None) -> dict:
    """A payment the operator received (a bank transfer): the plan becomes
    active until `until` (by default one interval after the current end)."""
    now = _now(now)
    sub = subscription(admin, tenant_id)
    if sub is None:
        raise LookupError(tenant_id)
    interval = interval or sub["billing_interval"]
    values = {"plan_code": sub["plan_code"], "billing_interval": interval, "status": "active",
              "current_period_end": None, "trial_ends_at": None, "updated_at": now,
              "desired_plan": None, "desired_interval": None}
    with admin.engine.begin() as conn:
        plan = get_plan(conn, plan_code or (sub["plan_code"] if sub["plan_code"] != TRIAL_PLAN else "starter"))
        base = max(sub["current_period_end"] or now, now)
        until = until or add_interval(base, interval)
        values["plan_code"] = plan.code
        values["current_period_end"] = until
        if sub["provider"] == "mollie":
            values["provider"] = "manual"
            _leave_the_gateway(admin, tenant_id, sub, values, gateway)
        conn.execute(insert(m.payments).values(tenant_id=tenant_id, subscription_id=sub["id"], provider="manual",
                                               kind="manual", amount_cents=amount_cents, status="paid",
                                               note=f"{by}: {note}"[:500], paid_at=now))
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id).values(**values))
        _release(conn, tenant_id)
    apply_plan(admin, tenant_id, plan, core)
    return subscription(admin, tenant_id)


def cancel(admin, tenant_id: str, gateway=None, now: datetime | None = None) -> dict:
    """No more renewals: the newsroom keeps working until the end of the
    period already paid, then goes on hold."""
    now = _now(now)
    sub = subscription(admin, tenant_id)
    if sub is None:
        raise LookupError(tenant_id)
    if sub["provider"] == "mollie" and sub["provider_subscription_id"] and gateway is not None:
        c = customer_of(admin, tenant_id)
        gateway.cancel_subscription(c["provider_customer_id"], sub["provider_subscription_id"])
    with admin.engine.begin() as conn:
        values = {"status": "canceled", "updated_at": now}
        if sub["status"] == "trialing":  # nothing paid: it ends with the trial
            values["current_period_end"] = sub["trial_ends_at"]
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id).values(**values))
    return subscription(admin, tenant_id)


def owners(admin, tenant_id: str) -> list[str]:
    return [p.email for p in accounts.members(admin, tenant_id) if p.role == "owner"]


def lifecycle(admin, now: datetime | None = None, mail=None, public_url: str = "") -> dict:
    """Run every hour by the worker: reminders before a trial ends, the hold
    when a trial or a paid period ends. Returns what it did."""
    now = _now(now)
    s, tn = m.subscriptions, m.tenants
    report = {"reminded": [], "held": []}
    with accounts._read(admin) as conn:
        rows = conn.execute(select(s.c.tenant_id, s.c.status, s.c.trial_ends_at, s.c.current_period_end,
                                   s.c.reminded_at, tn.c.name, tn.c.hold, tn.c.language)
                            .join(tn, tn.c.tenant_id == s.c.tenant_id)
                            .where(s.c.status != "expired")).all()
    link = f"{public_url.rstrip('/')}/account" if public_url else "/account"
    for r in rows:
        ends = r.trial_ends_at if r.status == "trialing" else r.current_period_end
        if ends is None:
            continue
        if r.status == "trialing" and ends - timedelta(days=REMIND_DAYS) <= now < ends and r.reminded_at is None:
            if mail is not None:
                # No request here: the newsroom's own language.
                lang = r.language if r.language in i18n.LANGS else i18n.DEFAULT
                for to in owners(admin, r.tenant_id):
                    try:
                        mail(to, t("La prova di {name} finisce il {date}", lang, name=r.name, date=f"{ends:%d/%m}"),
                             t("La prova gratuita della redazione {name} finisce il {date}.", lang, name=r.name,
                               date=f"{ends:%d/%m/%Y}") + "\n\n"
                             + t("Per continuare senza interruzioni scegli un piano:", lang) + f"\n\n{link}\n\n"
                             + t("Se non scegli un piano la redazione si ferma, ma i tuoi dati restano e "
                                 "puoi riattivarla quando vuoi.", lang) + "\n")
                    except Exception:
                        log.exception("trial reminder to %s failed", to)
            with admin.engine.begin() as conn:
                conn.execute(update(s).where(s.c.tenant_id == r.tenant_id).values(reminded_at=now))
            report["reminded"].append(r.tenant_id)
        grace = timedelta(0) if r.status == "trialing" else timedelta(days=GRACE_DAYS)
        if ends + grace <= now:
            with admin.engine.begin() as conn:
                conn.execute(update(s).where(s.c.tenant_id == r.tenant_id).values(status="expired", updated_at=now))
                _hold(conn, r.tenant_id)
            report["held"].append(r.tenant_id)
            log.info("newsroom %s on hold: %s ended", r.tenant_id, "trial" if r.status == "trialing" else "paid period")
    return report


def signup(admin, spec: dict, by: str, reseller_id: int | None = None, core=None, now: datetime | None = None) -> dict:
    """A new customer from the public site (or a reseller): billing data,
    a newsroom on `plan` (the trial by default) and its owner invited.
    spec: customer{...}, newsroom (name), owner_email (default: the
    customer's email), owner_name, plan, interval, external_ref.
    With an external_ref already known nothing new is made."""
    cust = dict(spec.get("customer") or {})
    ref = (spec.get("external_ref") or cust.get("external_ref") or "").strip()[:200] or None
    if ref:
        cust["external_ref"] = ref
    plan_code = spec.get("plan") or TRIAL_PLAN
    interval = spec.get("interval") or "month"
    # From the public site a paid plan starts only with its payment: the
    # newsroom starts on trial and the choice waits in /account. A reseller
    # bills its customers itself, so its plan starts at once.
    desired = None
    if reseller_id is None and plan_code != TRIAL_PLAN:
        desired, plan_code = plan_code, TRIAL_PLAN
    name = (spec.get("newsroom") or cust.get("name") or "").strip()
    with admin.engine.begin() as conn:
        get_plan(conn, plan_code)
        if desired:
            get_plan(conn, desired)
        cid, created = upsert_customer(conn, cust, reseller_id)
    room_ref = f"{'r' + str(reseller_id) if reseller_id is not None else 'site'}:{ref}" if ref else None
    lang = spec.get("language") or i18n.current()
    res = newsrooms.create(admin, {"name": name, "owner_email": spec.get("owner_email") or cust["email"],
                                   "owner_name": spec.get("owner_name"), "external_ref": room_ref,
                                   "language": lang}, by, core)
    tid = res["tenant_id"]
    if res["created"] or subscription(admin, tid) is None:
        provider = "reseller" if reseller_id is not None else "manual"
        start(admin, tid, plan_code, cid, provider, interval, core=core, now=now)
    if desired:
        with admin.engine.begin() as conn:
            conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tid, m.subscriptions.c.status == "trialing")
                         .values(desired_plan=desired, desired_interval=interval))
    sub = subscription(admin, tid)
    return {"customer_id": cid, "customer_created": created, "tenant_id": tid, "created": res["created"],
            "owner_email": res["owner_email"], "invite": res["invite"], "warning": res["warning"],
            "plan": sub["plan_code"] if sub else None, "status": sub["status"] if sub else None,
            "desired_plan": sub.get("desired_plan") if sub else None}


# --- payments through a gateway ------------------------------------------------------

def checkout(admin, gateway, tenant_id: str, plan_code: str, interval: str, redirect_url: str, webhook_url: str,
             now: datetime | None = None) -> str:
    """Start paying a plan: the address of the gateway's payment page. The
    first payment also authorises the renewals (a mandate)."""
    if gateway is None:
        raise BillingError(t("pagamenti online non configurati: contatta chi ti ha venduto il servizio"))
    if interval not in INTERVALS:
        raise BillingError(t("l'intervallo è month o year"))
    sub = subscription(admin, tenant_id)
    if sub is not None and sub["provider"] == "reseller":
        raise BillingError(t("questa redazione è fatturata dal tuo rivenditore"))
    c = customer_of(admin, tenant_id)
    if c is None:
        raise BillingError(t("prima servono i dati di fatturazione"))
    with accounts._read(admin) as conn:
        plan = get_plan(conn, plan_code)
    amount = price(plan, interval)
    if not amount:
        raise BillingError(t("questo piano non si paga online"))
    cust_id = c["provider_customer_id"]
    if not cust_id:
        cust_id = gateway.create_customer(c["name"], c["email"], {"customer_id": c["id"]})
        with admin.engine.begin() as conn:
            conn.execute(update(m.customers).where(m.customers.c.id == c["id"]).values(provider_customer_id=cust_id))
    meta = {"tenant_id": tenant_id, "plan": plan.code, "interval": interval}
    period = t("anno") if interval == "year" else t("mese")
    pay_id, url = gateway.first_payment(cust_id, amount, plan.currency, f"EuLLM Editor {plan.name} ({period}) · {tenant_id}",
                                        redirect_url, webhook_url, meta)
    with admin.engine.begin() as conn:
        conn.execute(insert(m.payments).values(tenant_id=tenant_id, subscription_id=sub["id"] if sub else None,
                                               provider=gateway.name, provider_payment_id=pay_id, kind="first",
                                               amount_cents=amount, currency=plan.currency, status="open",
                                               note=f"{plan.code}/{interval}", created_at=_now(now)))
    return url


def gateway_event(admin, gateway, payment_id: str, webhook_url: str, core=None, now: datetime | None = None) -> str:
    """The gateway says a payment changed: read it from the gateway (the
    notice itself carries only its id) and act. Returns what was done."""
    now = _now(now)
    pay = gateway.payment(payment_id)
    if pay is None:
        return "unknown"
    p, s = m.payments, m.subscriptions
    with accounts._read(admin) as conn:
        ours = conn.execute(select(p).where(p.c.provider_payment_id == payment_id)).first()
        sub_row = None
        if ours is None and pay.get("subscription_id"):
            sub_row = conn.execute(select(s).where(s.c.provider_subscription_id == pay["subscription_id"])).first()
    if ours is None and sub_row is None:
        return "unknown"
    if ours is None:  # a renewal Mollie made on its own
        with admin.engine.begin() as conn:
            conn.execute(insert(p).values(tenant_id=sub_row.tenant_id, subscription_id=sub_row.id, provider=gateway.name,
                                          provider_payment_id=payment_id, kind="recurring",
                                          amount_cents=pay["amount_cents"], currency=pay["currency"], status="open",
                                          created_at=now))
            ours = conn.execute(select(p).where(p.c.provider_payment_id == payment_id)).first()
    status = pay["status"]
    if ours.status == status or ours.status == "paid":
        return "unchanged"
    with admin.engine.begin() as conn:
        conn.execute(update(p).where(p.c.id == ours.id).values(status=status, paid_at=now if status == "paid" else None))
    tenant_id = ours.tenant_id
    sub = subscription(admin, tenant_id)
    if status == "paid" and ours.kind == "first":
        meta = pay.get("metadata") or {}
        plan_code, interval = meta.get("plan") or "starter", meta.get("interval") or "month"
        until = add_interval(now, interval)
        c = customer_of(admin, tenant_id)
        if sub and sub["provider"] == "mollie" and sub["provider_subscription_id"]:  # a change of plan
            try:
                gateway.cancel_subscription(c["provider_customer_id"], sub["provider_subscription_id"])
            except Exception:
                log.exception("old subscription of %s not cancelled", tenant_id)
        with accounts._read(admin) as conn:
            plan = get_plan(conn, plan_code)
        sub_id = gateway.create_subscription(c["provider_customer_id"], price(plan, interval), plan.currency, interval,
                                             until.date(), f"EuLLM Editor {plan.name} · {tenant_id}", webhook_url,
                                             pay.get("mandate_id"), {"tenant_id": tenant_id})
        with admin.engine.begin() as conn:
            conn.execute(update(s).where(s.c.tenant_id == tenant_id).values(
                plan_code=plan.code, billing_interval=interval, status="active", provider="mollie",
                provider_subscription_id=sub_id, trial_ends_at=None, current_period_end=until, updated_at=now))
            _release(conn, tenant_id)
        apply_plan(admin, tenant_id, plan, core)
        return "activated"
    if status == "paid":
        with admin.engine.begin() as conn:
            base = max(sub["current_period_end"] or now, now)
            conn.execute(update(s).where(s.c.tenant_id == tenant_id).values(
                status="active", current_period_end=add_interval(base, sub["billing_interval"]), updated_at=now))
            _release(conn, tenant_id)
        return "renewed"
    if status in ("failed", "expired", "canceled") and ours.kind == "recurring":
        with admin.engine.begin() as conn:
            conn.execute(update(s).where(s.c.tenant_id == tenant_id, s.c.status == "active")
                         .values(status="past_due", updated_at=now))
        return "past_due"
    return status


def paid_until_date(value: str | None) -> datetime | None:
    """A date typed in the console (YYYY-MM-DD): the end of that day, UTC."""
    if not value:
        return None
    d = date.fromisoformat(value.strip())
    return datetime(d.year, d.month, d.day, 23, 59, tzinfo=UTC)
