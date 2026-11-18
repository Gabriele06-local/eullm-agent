"""Selling the service: the routes around billing.py.

- For the public site (operator key): the public plans, the signup of a
  new customer (billing data + newsroom on trial + owner invited), the
  customer records, the plan of a newsroom, the payment page.
- For resellers: the same, with a key bound to the reseller, limited to
  its own customers.
- For the gateway: the payment notices (/api/billing/mollie/webhook).
- For the owner of a newsroom: /account, with the subscription, the
  billing data, the choice of a plan and the payment; it stays open when
  the newsroom is on hold, so that a trial that ended can still be paid.
- For the operator: plans, resellers and subscriptions in /admin.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Literal
from urllib.parse import quote, unquote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import accounts, billing, credits, i18n, newsrooms
from . import models as m
from .gateway import GatewayError
from .i18n import t

log = logging.getLogger(__name__)

# The subject of what the console's people are told, easy to filter.
OPERATOR_TAG = "[Redazia]"

COOKIE = "editor_token"
NOTICE_COOKIE = "editor_notice"


class CustomerIn(BaseModel):
    kind: Literal["company", "person"] = "company"
    name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., max_length=254)
    phone: str = Field("", max_length=40)
    vat_number: str = Field("", max_length=40)
    tax_code: str = Field("", max_length=40)
    address: str = Field("", max_length=200)
    postal_code: str = Field("", max_length=20)
    city: str = Field("", max_length=100)
    province: str = Field("", max_length=10)
    country: str = Field("IT", max_length=2)
    sdi_code: str = Field("", max_length=7)
    pec: str = Field("", max_length=254)
    external_ref: str | None = Field(None, max_length=200)


class CustomerPatch(BaseModel):
    kind: Literal["company", "person"] | None = None
    name: str | None = Field(None, min_length=1, max_length=200)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=40)
    vat_number: str | None = Field(None, max_length=40)
    tax_code: str | None = Field(None, max_length=40)
    address: str | None = Field(None, max_length=200)
    postal_code: str | None = Field(None, max_length=20)
    city: str | None = Field(None, max_length=100)
    province: str | None = Field(None, max_length=10)
    country: str | None = Field(None, max_length=2)
    sdi_code: str | None = Field(None, max_length=7)
    pec: str | None = Field(None, max_length=254)


class SignupIn(BaseModel):
    customer: CustomerIn
    newsroom: str | None = Field(None, max_length=100)
    owner_email: str | None = Field(None, max_length=254)
    owner_name: str | None = Field(None, max_length=100)
    plan: str | None = Field(None, max_length=40)
    interval: Literal["month", "year"] = "month"
    # The public site's id of this signup: the same one again changes nothing.
    external_ref: str | None = Field(None, max_length=200)
    # The visitor's language: the errors of this request and the owner's
    # invite are written in it (else the Accept-Language header).
    language: Literal["en", "it", "de", "fr", "es"] | None = None


class SubscriptionIn(BaseModel):
    plan: str = Field(..., max_length=40)
    interval: Literal["month", "year"] = "month"
    # Paid (or granted) until this day; none: open-ended.
    paid_until: datetime | None = None
    # A payment received outside the gateway (bank transfer), in cents.
    amount_cents: int | None = Field(None, ge=0)
    note: str = Field("", max_length=300)


class CheckoutIn(BaseModel):
    plan: str = Field(..., max_length=40)
    interval: Literal["month", "year"] = "month"
    redirect_url: str = Field(..., max_length=500)


class ResellerIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=254)
    discount_pct: float = Field(0, ge=0, le=100)
    notes: str = Field("", max_length=1000)


def _cents(value: str) -> int | None:
    """A price typed in the console (49, 49,90 or 1.490,00); empty: none."""
    v = (value or "").strip().replace("€", "").replace(" ", "")
    if not v:
        return None
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    n = float(v)
    if not math.isfinite(n) or not math.isfinite(n * 100):
        raise ValueError
    cents = round(n * 100)
    if cents < 0:
        raise ValueError
    return cents


def _int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    n = int(value)
    if n < 0:
        raise ValueError
    return n


def register(app, ctx, operator, own, no_reseller, send, need_console, page, back, failed) -> None:
    db, admin, pages = ctx.db, ctx.admin, ctx.pages
    gateway = ctx.gateway

    def base(request: Request) -> str:
        return (ctx.public_url or str(request.base_url)).rstrip("/")

    def webhook_url(request: Request) -> str:
        return f"{base(request)}/api/billing/{gateway.name}/webhook" if gateway else ""

    def invite_out(request, res, newsroom_name):
        if res["invite"]:
            return send(request, res["invite"], "invite", res["owner_email"], newsroom_name, "Redazia")
        if res["created"] and res.get("existing_user"):
            # The owner signs in as always: an email says the newsroom is there.
            # Bought on the site ("Acquista"): signing in leads to the payment.
            kind = "ready_pay" if res.get("desired_plan") and res.get("status") in ("trialing", "expired") else "ready"
            out = send(request, "", kind, res["owner_email"], newsroom_name, "Redazia")
            return {**out, "existing": True}
        return None

    def mail_to(to: str, subject: str, body: str) -> bool:
        if ctx.mail is None or not to:
            return False
        try:
            ctx.mail(to, subject, body)
            return True
        except Exception:
            log.warning("email to %s not sent: %s", to, subject)
            return False

    def tell_operators(request: Request, subject: str, lines: list[str], at: str = "clienti",
                       lang: str | None = None) -> None:
        """What happens to the business (signups, payments) reaches the
        people of the console by email, with a button to that newsroom."""
        body = "\n".join(lines) + "\n\n" + t("Apri nella console: {url}", lang, url=f"{base(request)}/admin#{at}") + "\n"
        for op in accounts.operators(admin):
            mail_to(op.email, f"{OPERATOR_TAG} {subject}", body)

    def reseller_id(request: Request) -> int | None:
        return getattr(request.state, "reseller_id", None)

    # --- public ------------------------------------------------------------------------

    @app.get("/api/public/plans")
    def public_plans():
        """The plans the public site shows, with prices and limits."""
        out = [{k: p[k] for k in ("code", "name", "description", "price_month_cents", "price_year_cents", "currency",
                                  "trial_days", "max_sites", "max_languages", "max_active_sources",
                                  "max_drafts_month", "autopilot", "credits_month", "evening", "max_users")}
               for p in billing.plans(admin, public_only=True)]
        trial = next((p for p in billing.plans(admin) if p["code"] == billing.TRIAL_PLAN), None)
        with accounts._read(admin) as conn:
            prices = credits.prices(conn)
        packs = [{k: x[k] for k in ("code", "name", "credits", "price_cents", "currency")}
                 for x in credits.packs(admin, active_only=True)]
        addons = [{k: x[k] for k in ("code", "name", "price_month_cents", "price_year_cents", "currency")}
                  for x in billing.addons(admin)]
        return JSONResponse({"plans": out, "trial_days": trial["trial_days"] if trial else 0,
                             "credits": {"prices": prices, "free_redraws": credits.FREE_REDRAWS, "packs": packs},
                             "addons": addons,
                             # Prices are before VAT; a consumer must see them with it (0.34.4).
                             "vat_percent": billing.VAT_PERCENT},
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"})

    # --- operator and resellers (API) --------------------------------------------------

    @app.post("/api/operator/signup", status_code=201)
    def api_signup(request: Request, body: SignupIn, by: str = Depends(operator)):
        token = i18n.use(body.language) if body.language else None
        try:
            spec = body.model_dump(exclude={"language"})
            spec["customer"] = {k: v for k, v in spec["customer"].items() if v is not None}
            try:
                res = billing.signup(admin, spec, by, reseller_id(request), ctx.core)
            except (billing.BillingError, newsrooms.NewsroomError, accounts.AccountError) as e:
                raise HTTPException(400, str(e)) from e
            room = newsrooms.listing(admin, db, res["tenant_id"])[0]
            out = {"customer": billing.customer(admin, res["customer_id"]), "tenant": room,
                   "subscription": billing.subscription(admin, res["tenant_id"]), "created": res["created"],
                   "owner": {"email": res["owner_email"], "invite": invite_out(request, res, room["name"])},
                   "trial_used": res.get("trial_used", []), "warning": res["warning"]}
            if res["created"]:
                cust = body.customer
                plan = res.get("desired_plan") or res.get("plan") or ""
                tell_operators(request, t("Nuova redazione: {name} ({plan})", name=room["name"], plan=plan), [
                    t("Cliente: {name} <{email}>", name=cust.name, email=cust.email),
                    t("Redazione: {name} ({id})", name=room["name"], id=res["tenant_id"]),
                    t("Piano acquistato, in attesa di pagamento: {plan}", plan=res["desired_plan"]) if res.get("desired_plan")
                    else t("Piano: {plan} ({status})", plan=res.get("plan") or "", status=res.get("status") or ""),
                    *([t("Prova negata: dati già usati in una prova precedente.")] if res.get("trial_used") else []),
                    *([t("Da un rivenditore.")] if reseller_id(request) is not None else []),
                ], f"r-{res['tenant_id']}")
            return out if res["created"] else JSONResponse(_json(out), status_code=200)
        finally:
            if token is not None:
                i18n._current.reset(token)

    @app.get("/api/operator/customers")
    def api_customers(request: Request, external_ref: str | None = None, by: str = Depends(operator)):
        return {"customers": billing.customers(admin, reseller_id(request), external_ref)}

    @app.get("/api/operator/customers/{customer_id}")
    def api_customer(request: Request, customer_id: int, by: str = Depends(operator)):
        c = billing.customer(admin, customer_id, reseller_id(request))
        if c is None:
            raise HTTPException(404, "customer not found")
        return c

    @app.patch("/api/operator/customers/{customer_id}")
    def api_customer_update(request: Request, customer_id: int, body: CustomerPatch, by: str = Depends(operator)):
        try:
            return billing.update_customer(admin, customer_id, body.model_dump(exclude_unset=True), reseller_id(request))
        except LookupError as e:
            raise HTTPException(404, "customer not found") from e
        except (billing.BillingError, accounts.AccountError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/operator/tenants/{tenant_id}/subscription")
    def api_subscription(request: Request, tenant_id: str, by: str = Depends(operator)):
        own(request, tenant_id)
        sub = billing.subscription(admin, tenant_id)
        if sub is None:
            raise HTTPException(404, "no subscription")
        return sub

    @app.put("/api/operator/tenants/{tenant_id}/subscription")
    def api_set_subscription(request: Request, tenant_id: str, body: SubscriptionIn, by: str = Depends(operator)):
        """The plan of a newsroom, set by the operator (a bank transfer) or
        by its reseller. With amount_cents the payment is recorded too."""
        own(request, tenant_id)
        provider = "reseller" if reseller_id(request) is not None else "manual"
        try:
            if body.amount_cents is not None and provider == "manual":
                if billing.subscription(admin, tenant_id) is None:
                    billing.start(admin, tenant_id, body.plan, provider=provider, interval=body.interval, core=ctx.core)
                return billing.record_payment(admin, tenant_id, body.amount_cents, body.paid_until, by, body.note,
                                              body.plan, body.interval, ctx.core, gateway=gateway)
            return billing.start(admin, tenant_id, body.plan, billing_customer(tenant_id), provider, body.interval,
                                 body.paid_until, ctx.core, gateway=gateway)
        except LookupError as e:
            raise HTTPException(404, "newsroom not found") from e
        except billing.BillingError as e:
            raise HTTPException(400, str(e)) from e

    def billing_customer(tenant_id: str) -> int | None:
        c = billing.customer_of(admin, tenant_id)
        return c["id"] if c else None

    @app.post("/api/operator/tenants/{tenant_id}/cancel")
    def api_cancel(request: Request, tenant_id: str, by: str = Depends(operator)):
        own(request, tenant_id)
        try:
            return billing.cancel(admin, tenant_id, gateway)
        except LookupError as e:
            raise HTTPException(404, "no subscription") from e

    @app.post("/api/operator/tenants/{tenant_id}/checkout")
    def api_checkout(request: Request, tenant_id: str, body: CheckoutIn, by: str = Depends(operator)):
        """The address of the payment page, for the public site to send the
        customer to; the gateway brings them back to redirect_url."""
        no_reseller(request)
        if not body.redirect_url.startswith(("https://", "http://")):
            raise HTTPException(400, "redirect_url is an http(s) address")
        try:
            return {"url": billing.checkout(admin, gateway, tenant_id, body.plan, body.interval, body.redirect_url,
                                            webhook_url(request))}
        except (billing.BillingError, GatewayError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/operator/resellers")
    def api_resellers(request: Request, by: str = Depends(operator)):
        no_reseller(request)
        return {"resellers": billing.resellers(admin)}

    @app.post("/api/operator/resellers", status_code=201)
    def api_reseller_add(request: Request, body: ResellerIn, by: str = Depends(operator)):
        no_reseller(request)
        try:
            rid = billing.create_reseller(admin, body.name, body.email, body.discount_pct, body.notes)
        except (billing.BillingError, accounts.AccountError) as e:
            raise HTTPException(400, str(e)) from e
        return {"id": rid, "key": accounts.create_operator_token(admin, f"rivenditore {body.name}", rid)}

    # --- gateway -------------------------------------------------------------------------

    @app.post("/api/billing/mollie/webhook")
    async def mollie_webhook(request: Request):
        """Mollie posts only the payment id: it is read back from Mollie.
        Unknown ids get 200 as well, so that nothing is revealed."""
        if gateway is None or gateway.name != "mollie":
            return Response(status_code=200)
        form = await request.form()
        pay_id = str(form.get("id") or "")
        try:
            done = billing.gateway_event(admin, gateway, pay_id, webhook_url(request), ctx.core)
        except Exception:
            # Mollie retries a non-200 answer: the payment is read again then.
            log.exception("payment notice %s not handled", pay_id)
            return Response(status_code=500)
        try:
            payment_news(request, pay_id, done)
        except Exception:
            log.exception("emails about payment %s not sent", pay_id)
        return Response(status_code=200)

    def payment_news(request: Request, pay_id: str, done: str) -> None:
        """A payment that changed things is told to the console and, when
        it starts or renews a plan, to the customer."""
        if done not in ("activated", "renewed", "past_due", "failed", "expired", "canceled"):
            return
        pay = billing.payment_by_provider_id(admin, pay_id)
        if pay is None:
            return
        tid = pay["tenant_id"]
        room = (newsrooms.listing(admin, db, tid) or [{"name": tid}])[0]
        sub = billing.subscription(admin, tid) or {}
        cust = billing.customer_of(admin, tid) or {}
        amount = billing.money(pay["amount_cents"], pay["currency"])
        # No reader on the other side of a webhook: the console's language
        # (EDITOR_LANGUAGE) for the administrators, the newsroom's for the customer.
        ops = i18n.pick(None, None)
        lang = room.get("language") if room.get("language") in i18n.LANGS else ops
        what = {"activated": t("Pagamento ricevuto", ops), "renewed": t("Rinnovo pagato", ops),
                "past_due": t("Rinnovo non riuscito", ops)}.get(done, t("Pagamento non riuscito", ops))
        tell_operators(request, f"{what}: {room['name']} · {amount}", [
            t("Cliente: {name} <{email}>", ops, name=cust.get("name") or "", email=cust.get("email") or ""),
            t("Redazione: {name} ({id})", ops, name=room["name"], id=tid),
            t("Piano: {plan} ({status})", ops, plan=sub.get("plan_name") or sub.get("plan_code") or "",
              status=sub.get("label") or ""),
            t("Pagamento Mollie: {id}", ops, id=pay_id),
        ], f"r-{tid}--pay", ops)
        if done in ("activated", "renewed") and cust.get("email"):
            until = sub.get("current_period_end")
            mail_to(cust["email"], t("Pagamento ricevuto: {name} è attiva", lang, name=room["name"]),
                    t("Abbiamo ricevuto il pagamento di {amount} per il piano {plan} della redazione {name}.", lang,
                      amount=amount, plan=sub.get("plan_name") or "", name=room["name"])
                    + ("\n" + t("Il piano è attivo fino al {date}; il rinnovo è automatico e lo puoi disdire dalla "
                                 "pagina dell'abbonamento.", lang, date=until.strftime("%d/%m/%Y")) if until else "")
                    + "\n\n" + t("Vai al tuo abbonamento", lang) + f": {base(request)}/account\n")

    # --- the owner's account page ------------------------------------------------------

    def person(request: Request):
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        if s is None:
            raise HTTPException(401, "sign in")
        if request.method != "GET" and request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
            raise HTTPException(403, "cross-site request refused")
        return s

    def owned(s, tenant_id: str):
        room = next((x for x in accounts.memberships(admin, s.user_id)
                     if x.tenant_id == tenant_id and x.role == "owner"), None)
        if room is None and not s.operator:
            raise HTTPException(404, "newsroom not found")
        return room

    def to_account(notice: str | None = None, tenant_id: str | None = None, back: str = "") -> RedirectResponse:
        """Back to /account, or to the newsroom's Piano page when the form came from there."""
        to = ("/plan" if back == "plan" else "/site?tab=publish#h-languages" if back == "site"
              else f"/account#t-{tenant_id}" if tenant_id else "/account")
        resp = RedirectResponse(to, status_code=303)
        if notice:
            resp.set_cookie(NOTICE_COOKIE, quote(notice), max_age=60, httponly=True, samesite="strict")
        return resp

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request):
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        if s is None:
            return RedirectResponse("/login", status_code=303)
        rooms = []
        for x in accounts.memberships(admin, s.user_id):
            if x.role != "owner":
                continue
            rooms.append({"tenant_id": x.tenant_id, "name": x.name, "active": x.active,
                          "subscription": billing.subscription(admin, x.tenant_id),
                          "customer": billing.customer_of(admin, x.tenant_id),
                          "payments": billing.payments_of(admin, x.tenant_id, 24),
                          "reseller": billing.reseller_of_tenant(admin, x.tenant_id) is not None})
        notice = request.cookies.get(NOTICE_COOKIE)
        resp = pages.TemplateResponse(request, "account.html.j2", {
            "me": s, "rooms": rooms, "plans": billing.plans(admin, public_only=True), "money": billing.money,
            "online": gateway is not None, "notice": unquote(notice) if notice else None,
            "back": s.tenant_id is not None})
        if notice:
            resp.delete_cookie(NOTICE_COOKIE)
        return resp

    @app.post("/account/{tenant_id}/customer")
    def account_customer(tenant_id: str, request: Request, kind: str = Form("company"), name: str = Form(""),
                         email: str = Form(""), phone: str = Form(""), vat_number: str = Form(""),
                         tax_code: str = Form(""), address: str = Form(""), postal_code: str = Form(""),
                         city: str = Form(""), province: str = Form(""), country: str = Form("IT"),
                         sdi_code: str = Form(""), pec: str = Form(""), back: str = Form(""), s=Depends(person)):
        owned(s, tenant_id)
        spec = {"kind": kind, "name": name, "email": email, "phone": phone, "vat_number": vat_number,
                "tax_code": tax_code, "address": address, "postal_code": postal_code, "city": city,
                "province": province, "country": country, "sdi_code": sdi_code, "pec": pec}
        try:
            billing.set_tenant_customer(admin, tenant_id, spec)
            billing._renewal_follows(admin, ctx.gateway, tenant_id, force=False)  # the VAT can change with them
        except (billing.BillingError, accounts.AccountError) as e:
            return to_account(t("Dati non salvati: {error}.", error=e), tenant_id, back)
        except LookupError:
            return to_account(t("Redazione non trovata."), tenant_id, back)
        return to_account(t("Dati di fatturazione salvati."), tenant_id, back)

    @app.post("/account/{tenant_id}/checkout")
    def account_checkout(tenant_id: str, request: Request, plan: str = Form(...), interval: str = Form("month"),
                         back: str = Form(""), s=Depends(person)):
        owned(s, tenant_id)
        paid = f"{base(request)}/account/{tenant_id}/paid" + ("?back=plan" if back == "plan" else "")
        try:
            url = billing.checkout(admin, gateway, tenant_id, plan, interval, paid, webhook_url(request))
        except GatewayError as e:
            if "No suitable payment methods" in str(e):
                # The gateway account has no method for recurring payments on:
                # the operator's setting, not the customer's doing.
                log.warning("checkout of %s: %s (enable credit cards in the Mollie website profile)", tenant_id, e)
                return to_account(t("Pagamento non avviato: il pagamento online non è ancora pronto. Riprova più tardi o "
                                    "scrivi all'assistenza."), tenant_id, back)
            return to_account(t("Pagamento non avviato: {error}.", error=e), tenant_id, back)
        except billing.BillingError as e:
            return to_account(t("Pagamento non avviato: {error}.", error=e), tenant_id, back)
        except Exception:
            log.exception("checkout of %s failed", tenant_id)
            return to_account(t("Pagamento non avviato: errore interno, riprova tra poco."), tenant_id, back)
        return RedirectResponse(url, status_code=303)

    @app.post("/account/{tenant_id}/credits")
    def account_credits(tenant_id: str, request: Request, pack: str = Form(...), back: str = Form(""),
                        s=Depends(person)):
        owned(s, tenant_id)
        paid = f"{base(request)}/account/{tenant_id}/credits-paid" + ("?back=plan" if back == "plan" else "")
        try:
            url = billing.buy_credits(admin, gateway, tenant_id, pack, paid, webhook_url(request))
        except (GatewayError, billing.BillingError, credits.CreditsError) as e:
            return to_account(t("Pagamento non avviato: {error}.", error=e), tenant_id, back)
        except Exception:
            log.exception("credits checkout of %s failed", tenant_id)
            return to_account(t("Pagamento non avviato: errore interno, riprova tra poco."), tenant_id, back)
        return RedirectResponse(url, status_code=303)

    def site_of(tenant_id: str, site_id: int):
        with db.tenant(tenant_id) as ts:
            row = ts.execute(select(m.sites.c.id, m.sites.c.name, m.sites.c.domain).where(m.sites.c.id == site_id)).first()
        if row is None:
            raise HTTPException(404, "site not found")
        return row

    @app.post("/account/{tenant_id}/language")
    def account_language(tenant_id: str, request: Request, site_id: int = Form(...), back: str = Form("site"),
                         s=Depends(person)):
        """A language more for a site (Francesco 2026-11-10): what is left of
        the paid period now, then with the plan's renewals."""
        owned(s, tenant_id)
        site = site_of(tenant_id, site_id)
        paid = f"{base(request)}/account/{tenant_id}/language-paid" + ("?back=site" if back == "site" else "")
        try:
            url = billing.buy_language(admin, gateway, tenant_id, site.id, site.domain or site.name, paid,
                                       webhook_url(request))
        except (GatewayError, billing.BillingError) as e:
            return to_account(t("Pagamento non avviato: {error}.", error=e), tenant_id, back)
        except Exception:
            log.exception("language checkout of %s failed", tenant_id)
            return to_account(t("Pagamento non avviato: errore interno, riprova tra poco."), tenant_id, back)
        return RedirectResponse(url, status_code=303)

    @app.get("/account/{tenant_id}/language-paid")
    def account_language_paid(tenant_id: str, request: Request, back: str = ""):
        return to_account(t("Grazie. La lingua si aggiunge appena il circuito conferma il pagamento, di solito entro un "
                            "minuto: poi la scegli qui sotto."), tenant_id, back)

    @app.post("/account/{tenant_id}/language/remove")
    def account_language_remove(tenant_id: str, request: Request, site_id: int = Form(...), back: str = Form("site"),
                                s=Depends(person)):
        """One extra language less for the site, from the next renewal; the
        site must first have no more languages chosen than it keeps."""
        owned(s, tenant_id)
        site = site_of(tenant_id, site_id)
        from . import translate as tr

        with db.tenant(tenant_id) as ts:
            limit = tr.allowed(ts, site.id)
            used = 1 + len(tr.site_languages(ts, site.id))
        if limit is not None and used > limit - 1:
            return to_account(t("Togli prima una lingua dal sito: dopo ne resterebbero {n}, compresa la sua.",
                                n=limit - 1), tenant_id, back)
        try:
            n = billing.remove_language(admin, gateway, tenant_id, site.id)
        except billing.BillingError as e:
            return to_account(t("Lingua non tolta: {error}.", error=e), tenant_id, back)
        return to_account(t("Lingua extra tolta: dal prossimo rinnovo non si paga più. Lingue extra del sito: {n}.", n=n),
                          tenant_id, back)

    @app.get("/account/{tenant_id}/credits-paid")
    def account_credits_paid(tenant_id: str, request: Request, back: str = ""):
        return to_account(t("Grazie. I crediti arrivano appena il circuito conferma il pagamento, di solito entro un "
                            "minuto."), tenant_id, back)

    @app.get("/account/{tenant_id}/paid")
    def account_paid(tenant_id: str, request: Request, back: str = ""):
        return to_account(t("Grazie. Il pagamento è in verifica: la redazione si attiva appena il circuito lo conferma, "
                            "di solito entro un minuto."), tenant_id, back)

    @app.post("/account/{tenant_id}/cancel")
    def account_cancel(tenant_id: str, request: Request, back: str = Form(""), s=Depends(person)):
        owned(s, tenant_id)
        try:
            sub = billing.cancel(admin, tenant_id, gateway)
        except LookupError:
            return to_account(t("Nessun abbonamento da disdire."), tenant_id, back)
        end = sub["current_period_end"]
        return to_account(t("Abbonamento disdetto: niente più rinnovi.") +
                          (" " + t("La redazione resta attiva fino al {date}.", date=f"{end:%d/%m/%Y}") if end else ""),
                          tenant_id, back)

    # --- the operator's console ----------------------------------------------------------

    @app.post("/admin/plans/{code}")
    def ui_plan(code: str, name: str = Form(...), description: str = Form(""), price_month: str = Form(""),
                price_year: str = Form(""), trial_days: str = Form("0"), max_sites: str = Form(""),
                max_languages: str = Form(""), max_active_sources: str = Form(""),
                max_drafts_month: str = Form(""), credits_month: str = Form(""), max_users: str = Form(""),
                public: str = Form(""),
                autopilot: str = Form("1"), evening: str = Form("0"), s=Depends(need_console)):
        try:
            fields = {"name": name.strip(), "description": description.strip(), "price_month_cents": _cents(price_month),
                      "price_year_cents": _cents(price_year), "trial_days": _int(trial_days) or 0,
                      "max_sites": _int(max_sites), "max_languages": _int(max_languages),
                      "max_active_sources": _int(max_active_sources),
                      "max_drafts_month": _int(max_drafts_month), "credits_month": _int(credits_month),
                      "max_users": _int(max_users), "public": public == "1",
                      "autopilot": autopilot == "1", "evening": evening == "1"}
            billing.save_plan(admin, code, fields)
        except ValueError as e:
            return back(t("Piano non salvato: {error}.",
                          error=e if isinstance(e, billing.BillingError) else t("numeri di zero o più")), f"p-{code}", error=True)
        return back(t("Piano {name} salvato. Le redazioni già su questo piano prendono i nuovi limiti al prossimo pagamento.",
                      name=name.strip()), f"p-{code}")

    @app.post("/admin/plans")
    def ui_new_plan(code: str = Form(""), name: str = Form(...), s=Depends(need_console)):
        try:
            code = billing.create_plan(admin, code, name)
        except billing.BillingError as e:
            return back(t("Piano non creato: {error}.", error=e), "nuovo-piano", error=True)
        return back(t("Piano {name} creato, non ancora sul sito: imposta prezzi e limiti e scegli \"Sul sito: Sì\".",
                      name=name.strip()), f"p-{code}")

    @app.post("/admin/plans/{code}/delete")
    def ui_delete_plan(code: str, s=Depends(need_console)):
        try:
            name = billing.delete_plan(admin, code)
        except LookupError:
            return back(t("Piano non trovato."), "piani", error=True)
        except billing.BillingError as e:
            return back(t("Piano non eliminato: {error}.", error=e), "piani", error=True)
        return back(t("Piano {name} eliminato.", name=name), "piani")

    # --- credits: price list, packs, credits of a newsroom ----------------------------

    @app.post("/admin/addons/{code}")
    def ui_addon(code: str, price_month: str = Form(""), price_year: str = Form(""), s=Depends(need_console)):
        try:
            billing.save_addon(admin, code, _cents(price_month), _cents(price_year))
        except ValueError as e:
            return back(t("Prezzi non salvati: {error}.",
                          error=e if isinstance(e, billing.BillingError) else t("numeri di zero o più")), "componenti",
                        error=True)
        return back(t("Prezzi delle lingue extra salvati: valgono per i nuovi acquisti e i rinnovi che cambiano."),
                    "componenti")

    @app.post("/admin/credits/prices")
    def ui_credit_prices(article: str = Form(""), translation: str = Form(""), image: str = Form(""),
                         s=Depends(need_console)):
        try:
            values = {k: _int(v) for k, v in (("article", article), ("translation", translation), ("image", image))
                      if v.strip()}
            credits.set_prices(admin, values)
        except ValueError as e:
            return back(t("Listino non salvato: {error}.",
                          error=e if isinstance(e, credits.CreditsError) else t("numeri di zero o più")), "crediti", error=True)
        return back(t("Listino dei crediti salvato."), "crediti")

    @app.post("/admin/credits/packs")
    def ui_new_pack(code: str = Form(...), name: str = Form(...), credits_n: str = Form(..., alias="credits"),
                    price: str = Form(...), s=Depends(need_console)):
        try:
            credits.create_pack(admin, code, name, _int(credits_n) or 0, _cents(price) or 0)
        except ValueError as e:
            return back(t("Pacchetto non creato: {error}.",
                          error=e if isinstance(e, credits.CreditsError) else t("numeri di zero o più")), "crediti", error=True)
        return back(t("Pacchetto {name} creato.", name=name.strip()), "crediti")

    @app.post("/admin/credits/packs/{code}")
    def ui_pack(code: str, name: str = Form(...), credits_n: str = Form(..., alias="credits"), price: str = Form(...),
                active: str = Form("1"), sort: str = Form("0"), s=Depends(need_console)):
        try:
            credits.save_pack(admin, code, name, _int(credits_n) or 0, _cents(price) or 0, active == "1",
                              _int(sort) or 0)
        except ValueError as e:
            return back(t("Pacchetto non salvato: {error}.",
                          error=e if isinstance(e, credits.CreditsError) else t("numeri di zero o più")), "crediti", error=True)
        return back(t("Pacchetto {name} salvato.", name=name.strip()), "crediti")

    @app.post("/admin/credits/packs/{code}/delete")
    def ui_delete_pack(code: str, s=Depends(need_console)):
        try:
            name = credits.delete_pack(admin, code)
        except LookupError:
            return back(t("Pacchetto non trovato."), "crediti", error=True)
        return back(t("Pacchetto {name} eliminato: i crediti già comprati restano.", name=name), "crediti")

    @app.post("/admin/newsrooms/{tenant_id}/customer")
    def ui_room_customer(tenant_id: str, kind: str = Form("company"), name: str = Form(""), email: str = Form(""),
                         phone: str = Form(""), vat_number: str = Form(""), tax_code: str = Form(""),
                         address: str = Form(""), postal_code: str = Form(""), city: str = Form(""),
                         province: str = Form(""), country: str = Form("IT"), sdi_code: str = Form(""),
                         pec: str = Form(""), s=Depends(need_console)):
        at = f"r-{tenant_id}--customer"
        spec = {"kind": kind, "name": name, "email": email, "phone": phone, "vat_number": vat_number,
                "tax_code": tax_code, "address": address, "postal_code": postal_code, "city": city,
                "province": province, "country": country, "sdi_code": sdi_code, "pec": pec}
        try:
            billing.set_tenant_customer(admin, tenant_id, spec)
            billing._renewal_follows(admin, ctx.gateway, tenant_id, force=False)  # the VAT can change with them
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        except (billing.BillingError, accounts.AccountError) as e:
            return back(t("Dati non salvati: {error}.", error=e), at, error=True)
        return back(t("Dati di fatturazione salvati."), at)

    @app.post("/admin/newsrooms/{tenant_id}/customer/link")
    def ui_room_link(tenant_id: str, customer_id: str = Form(""), s=Depends(need_console)):
        at = f"r-{tenant_id}--customer"
        try:
            billing.link_customer(admin, tenant_id, int(customer_id) if customer_id.strip() else None)
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        except (billing.BillingError, ValueError) as e:
            return back(t("Dati non salvati: {error}.", error=e), at, error=True)
        return back(t("Cliente collegato.") if customer_id.strip() else t("Cliente scollegato."), at)

    @app.post("/admin/newsrooms/{tenant_id}/credits")
    def ui_grant(tenant_id: str, credits_n: str = Form(..., alias="credits"), amount: str = Form(""),
                 note: str = Form(""), s=Depends(need_console)):
        at = f"r-{tenant_id}--credits"
        try:
            n = int((credits_n or "").strip().replace(".", ""))
            cents = _cents(amount) or 0
            billing.record_credits(admin, tenant_id, n, cents, s.email, note.strip()[:200])
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        except (credits.CreditsError, billing.BillingError) as e:
            return back(t("Crediti non registrati: {error}.", error=e), at, error=True)
        except ValueError:
            return back(t("Crediti non registrati: {error}.", error=t("i crediti sono un numero intero diverso da zero")), at, error=True)
        if n > 0:
            msg = t("{n} crediti aggiunti a {tenant}.", n=n, tenant=tenant_id)
        else:
            msg = t("{n} crediti tolti a {tenant}.", n=-n, tenant=tenant_id)
        return back(msg + (" " + t("Pagamento di {amount} registrato.", amount=billing.money(cents)) if cents else ""), at)

    @app.post("/admin/customers", response_class=HTMLResponse)
    def ui_new_customer(request: Request, kind: str = Form("company"), name: str = Form(""), email: str = Form(""),
                        phone: str = Form(""), vat_number: str = Form(""), tax_code: str = Form(""),
                        address: str = Form(""), postal_code: str = Form(""), city: str = Form(""),
                        province: str = Form(""), country: str = Form("IT"), sdi_code: str = Form(""),
                        pec: str = Form(""), reseller: str = Form(""), then: str = Form(""), room: str = Form(""),
                        s=Depends(need_console)):
        """A customer entered by hand: from Clienti, or from the page of a
        newsroom that has none (`room`), which it is then tied to. A mistake
        shows the form again with what was written."""
        spec = {"kind": kind, "name": name, "email": email, "phone": phone, "vat_number": vat_number,
                "tax_code": tax_code, "address": address, "postal_code": postal_code, "city": city,
                "province": province, "country": country or "IT", "sdi_code": sdi_code, "pec": pec}
        form = {**spec, "reseller": reseller.strip()}
        room = room.strip()
        if room and not newsrooms.listing(admin, db, room):
            return back(t("Redazione non trovata."), error=True)
        try:
            cid = billing.create_customer(admin, spec, int(reseller) if reseller.strip().isdigit() else None)
        except billing.DuplicateCustomer as e:
            if room:  # the window comes back with that customer chosen
                return page(request, s, 400, view="redazione", room=room, reopen=f"r-{room}--customer",
                            error=str(e) + " " + t("L'ho scelto in «Cliente esistente»: premi Collega."),
                            form={**form, "customer_mode": "existing", "customer_id": str(e.customer_id)})
            return page(request, s, 400, view="nuovo-cliente", form=form, error=str(e), twin=e.customer_id)
        except (billing.BillingError, accounts.AccountError) as e:
            error = t("Cliente non creato: {error}.", error=e)
            if room:
                return page(request, s, 400, view="redazione", room=room, reopen=f"r-{room}--customer", error=error,
                            form={**form, "customer_mode": "new"})
            return page(request, s, 400, view="nuovo-cliente", form=form, error=error)
        if room:
            billing.link_customer(admin, room, cid)
            return back(t("Cliente {name} creato e collegato alla redazione.", name=name.strip()), f"r-{room}")
        if then == "room":  # straight on to its newsroom
            return RedirectResponse(f"/admin/new?customer={cid}", status_code=303)
        return back(t("Cliente {name} creato. Ora puoi creargli la redazione.", name=name.strip()), f"c-{cid}")

    @app.post("/admin/customers/{cid}")
    def ui_customer(cid: int, kind: str = Form("company"), name: str = Form(""), email: str = Form(""),
                    phone: str = Form(""), vat_number: str = Form(""), tax_code: str = Form(""), address: str = Form(""),
                    postal_code: str = Form(""), city: str = Form(""), province: str = Form(""),
                    country: str = Form("IT"), sdi_code: str = Form(""), pec: str = Form(""), s=Depends(need_console)):
        spec = {"kind": kind, "name": name, "email": email, "phone": phone, "vat_number": vat_number,
                "tax_code": tax_code, "address": address, "postal_code": postal_code, "city": city,
                "province": province, "country": country, "sdi_code": sdi_code, "pec": pec}
        try:
            billing.clean_customer(spec)
            billing.update_customer(admin, cid, spec)
        except LookupError:
            return back(t("Cliente non trovato."), "clienti", error=True)
        except (billing.BillingError, accounts.AccountError) as e:
            return back(t("Cliente non aggiornato: {error}.", error=e), f"c-{cid}--edit", error=True)
        return back(t("Cliente {name} aggiornato.", name=name.strip()), f"c-{cid}")

    @app.post("/admin/customers/{cid}/delete")
    def ui_delete_customer(cid: int, s=Depends(need_console)):
        try:
            name = billing.delete_customer(admin, cid)
        except LookupError:
            return back(t("Cliente non trovato."), "clienti", error=True)
        except billing.BillingError as e:
            return back(t("Cliente non eliminato: {error}.", error=e), f"c-{cid}--delete", error=True)
        return back(t("Cliente {name} eliminato.", name=name), "clienti")

    @app.post("/admin/resellers/{rid}")
    def ui_edit_reseller(rid: int, name: str = Form(...), email: str = Form(...), discount: str = Form("0"),
                         s=Depends(need_console)):
        try:
            if not name.strip():
                raise billing.BillingError(t("il rivenditore ha bisogno di un nome"))
            try:
                pct = float((discount or "0").replace(",", "."))
            except ValueError:
                raise billing.BillingError(t("lo sconto è una percentuale da 0 a 100")) from None
            billing.update_reseller(admin, rid, {"name": name.strip()[:100], "email": email, "discount_pct": pct})
        except LookupError:
            return back(t("Rivenditore non trovato."), "rivenditori", error=True)
        except (ValueError, accounts.AccountError) as e:
            return back(t("Rivenditore non aggiornato: {error}.", error=e), f"v-{rid}", error=True)
        return back(t("Rivenditore {name} aggiornato.", name=name.strip()), f"v-{rid}")

    @app.post("/admin/resellers/{rid}/delete")
    def ui_delete_reseller(rid: int, s=Depends(need_console)):
        try:
            name = billing.delete_reseller(admin, rid)
        except LookupError:
            return back(t("Rivenditore non trovato."), "rivenditori", error=True)
        except billing.BillingError as e:
            return back(t("Rivenditore non eliminato: {error}.", error=e), f"v-{rid}", error=True)
        return back(t("Rivenditore {name} eliminato con le sue chiavi.", name=name), "rivenditori")

    @app.post("/admin/resellers", response_class=HTMLResponse)
    def ui_reseller(request: Request, name: str = Form(...), email: str = Form(...), discount: str = Form("0"),
                    s=Depends(need_console)):
        try:
            pct = float((discount or "0").replace(",", "."))
            rid = billing.create_reseller(admin, name, email, pct)
        except (ValueError, accounts.AccountError) as e:
            return page(request, s, 400, view="rivenditori", error=t("Rivenditore non creato: {error}.", error=e))
        key = accounts.create_operator_token(admin, f"rivenditore {name.strip()}", rid)
        return page(request, s, view="rivenditori", new={"what": t("Rivenditore {name} creato con la sua chiave API", name=name.strip()), "key": key})

    @app.post("/admin/resellers/{rid}/key", response_class=HTMLResponse)
    def ui_reseller_key(request: Request, rid: int, s=Depends(need_console)):
        r = billing.reseller(admin, rid)
        if r is None or not r.active:
            return back(t("Rivenditore non trovato o disattivato."), "rivenditori", error=True)
        key = accounts.create_operator_token(admin, f"rivenditore {r.name}", rid)
        return page(request, s, view="rivenditori", new={"what": t("Nuova chiave API per {name}", name=r.name), "key": key})

    @app.post("/admin/resellers/{rid}/active")
    def ui_reseller_active(rid: int, active: str = Form(...), s=Depends(need_console)):
        try:
            billing.update_reseller(admin, rid, {"active": active == "1"})
        except LookupError:
            return back(t("Rivenditore non trovato."), "rivenditori", error=True)
        return back(t("Rivenditore riattivato.") if active == "1" else
                    t("Rivenditore disattivato: le sue chiavi non valgono più, i suoi clienti restano."), f"v-{rid}")

    @app.post("/admin/subscriptions/{tenant_id}")
    def ui_subscription(tenant_id: str, plan: str = Form(...), interval: str = Form("month"),
                        paid_until: str = Form(""), amount: str = Form(""), dialog: str = Form(""),
                        s=Depends(need_console)):
        at = f"r-{tenant_id}--{dialog if dialog in ('plan', 'record', 'trial') else 'sub'}"
        if dialog == "record" and not amount.strip():
            return back(t("Pagamento non registrato: scrivi l'importo ricevuto."), at, error=True)
        try:
            until = billing.paid_until_date(paid_until)
            cents = _cents(amount)
            if cents is not None:
                if billing.subscription(admin, tenant_id) is None:
                    billing.start(admin, tenant_id, plan, interval=interval, core=ctx.core)
                billing.record_payment(admin, tenant_id, cents, until, s.email, "registrato dalla console", plan,
                                       interval, ctx.core, gateway=gateway)
            else:
                old = billing.subscription(admin, tenant_id) or {}
                provider = "reseller" if old.get("provider") == "reseller" else "manual"
                billing.start(admin, tenant_id, plan, billing_customer(tenant_id), provider, interval, until, ctx.core,
                              gateway=gateway)
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        except ValueError as e:
            return back(t("Abbonamento non cambiato: {error}.", error=e), at, error=True)
        name = room_name(tenant_id)
        if dialog == "record":
            return back(t("Pagamento di {amount} registrato: {tenant} è attiva.", amount=billing.money(cents),
                          tenant=name), f"r-{tenant_id}")
        if dialog == "trial":
            return back(t("Prova ridata a {tenant}.", tenant=name), f"r-{tenant_id}")
        return back(t("Abbonamento di {tenant} aggiornato.", tenant=name), f"r-{tenant_id}")

    def room_name(tenant_id: str) -> str:
        return (newsrooms.listing(admin, db, tenant_id) or [{"name": tenant_id}])[0]["name"]

    @app.post("/admin/newsrooms/{tenant_id}/cancel")
    def ui_cancel(tenant_id: str, s=Depends(need_console)):
        """No more renewals, no refund: the newsroom works until the end of
        the period already paid (a trial until its end), then stops."""
        try:
            sub = billing.cancel(admin, tenant_id, gateway)
        except LookupError:
            return back(t("Questa redazione non ha un abbonamento."), f"r-{tenant_id}", error=True)
        except Exception as e:  # the gateway did not answer: nothing changed
            log.exception("renewals of %s not stopped", tenant_id)
            return back(t("Rinnovi non fermati: {error}.", error=e), f"r-{tenant_id}--cancel", error=True)
        end, name = sub.get("current_period_end"), room_name(tenant_id)
        return back(t("Rinnovi fermati: {tenant} lavora fino al {date}, poi si ferma.", tenant=name,
                      date=end.strftime("%d/%m/%Y")) if end else t("Rinnovi fermati per {tenant}.", tenant=name),
                    f"r-{tenant_id}")

    @app.post("/admin/payments/{pid}/refund")
    def ui_refund(request: Request, pid: int, amount: str = Form(""), then: str = Form("none"),
                  confirm: str = Form(""), s=Depends(need_console)):
        at = f"r-{billing.payment_tenant(admin, pid)}--refund-{pid}"
        if confirm != "1":
            return back(t("Rimborso non fatto: spunta la conferma."), at, error=True)
        try:
            done = billing.refund(admin, gateway, pid, _cents(amount), s.email, then)
        except LookupError:
            return back(t("Pagamento non trovato."), error=True)
        except ValueError as e:
            return back(t("Rimborso non fatto: {error}.", error=e), at, error=True)
        room = (newsrooms.listing(admin, db, done["tenant_id"]) or [{"name": done["tenant_id"]}])[0]
        amount_text = billing.money(done["amount_cents"], done["currency"])
        after = {"none": t("l'abbonamento resta com'è"),
                 "renewals": t("i rinnovi sono fermi, la redazione lavora fino alla fine del periodo"),
                 "now": t("la redazione è ferma da adesso")}[then]
        tell_operators(request, t("Rimborso: {name} · {amount}", name=room["name"], amount=amount_text), [
            t("Redazione: {name} ({id})", name=room["name"], id=done["tenant_id"]),
            t("Rimborso di {payment} fatto da {who}: {after}.", payment=done["payment_id"], who=s.email, after=after),
            t("Rimborso Mollie: {id} ({status})", id=done["refund_id"], status=done["status"]),
        ], at)
        return back(t("Rimborsati {amount} a {name}: {after}. Sulla carta del cliente arrivano in qualche giorno.",
                      amount=amount_text, name=room["name"], after=after), at)

def _json(value):
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(value)
