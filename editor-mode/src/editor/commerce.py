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
from datetime import datetime
from typing import Literal
from urllib.parse import quote, unquote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import accounts, billing, newsrooms
from .gateway import GatewayError

log = logging.getLogger(__name__)

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
    n = round(float(v) * 100)
    if n < 0:
        raise ValueError
    return n


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
        if not res["invite"]:
            return None
        return send(request, res["invite"], "invite", res["owner_email"], newsroom_name, "EuLLM Editor")

    def reseller_id(request: Request) -> int | None:
        return getattr(request.state, "reseller_id", None)

    # --- public ------------------------------------------------------------------------

    @app.get("/api/public/plans")
    def public_plans():
        """The plans the public site shows, with prices and limits."""
        out = [{k: p[k] for k in ("code", "name", "description", "price_month_cents", "price_year_cents", "currency",
                                  "trial_days", "max_sites", "max_languages", "max_active_sources",
                                  "max_drafts_month")} for p in billing.plans(admin, public_only=True)]
        trial = next((p for p in billing.plans(admin) if p["code"] == billing.TRIAL_PLAN), None)
        return JSONResponse({"plans": out, "trial_days": trial["trial_days"] if trial else 0},
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"})

    # --- operator and resellers (API) --------------------------------------------------

    @app.post("/api/operator/signup", status_code=201)
    def api_signup(request: Request, body: SignupIn, by: str = Depends(operator)):
        spec = body.model_dump()
        spec["customer"] = {k: v for k, v in spec["customer"].items() if v is not None}
        try:
            res = billing.signup(admin, spec, by, reseller_id(request), ctx.core)
        except (billing.BillingError, newsrooms.NewsroomError, accounts.AccountError) as e:
            raise HTTPException(400, str(e)) from e
        room = newsrooms.listing(admin, db, res["tenant_id"])[0]
        out = {"customer": billing.customer(admin, res["customer_id"]), "tenant": room,
               "subscription": billing.subscription(admin, res["tenant_id"]), "created": res["created"],
               "owner": {"email": res["owner_email"], "invite": invite_out(request, res, room["name"])},
               "warning": res["warning"]}
        return out if res["created"] else JSONResponse(_json(out), status_code=200)

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
                                              body.plan, body.interval, ctx.core)
            return billing.start(admin, tenant_id, body.plan, billing_customer(tenant_id), provider, body.interval,
                                 body.paid_until, ctx.core)
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
            billing.gateway_event(admin, gateway, pay_id, webhook_url(request), ctx.core)
        except Exception:
            # Mollie retries a non-200 answer: the payment is read again then.
            log.exception("payment notice %s not handled", pay_id)
            return Response(status_code=500)
        return Response(status_code=200)

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

    def to_account(notice: str | None = None, tenant_id: str | None = None) -> RedirectResponse:
        resp = RedirectResponse(f"/account#t-{tenant_id}" if tenant_id else "/account", status_code=303)
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
                         sdi_code: str = Form(""), pec: str = Form(""), s=Depends(person)):
        owned(s, tenant_id)
        spec = {"kind": kind, "name": name, "email": email, "phone": phone, "vat_number": vat_number,
                "tax_code": tax_code, "address": address, "postal_code": postal_code, "city": city,
                "province": province, "country": country, "sdi_code": sdi_code, "pec": pec}
        try:
            billing.set_tenant_customer(admin, tenant_id, spec)
        except (billing.BillingError, accounts.AccountError) as e:
            return to_account(f"Dati non salvati: {e}.", tenant_id)
        return to_account("Dati di fatturazione salvati.", tenant_id)

    @app.post("/account/{tenant_id}/checkout")
    def account_checkout(tenant_id: str, request: Request, plan: str = Form(...), interval: str = Form("month"),
                         s=Depends(person)):
        owned(s, tenant_id)
        try:
            url = billing.checkout(admin, gateway, tenant_id, plan, interval,
                                   f"{base(request)}/account/{tenant_id}/paid", webhook_url(request))
        except (billing.BillingError, GatewayError) as e:
            return to_account(f"Pagamento non avviato: {e}.", tenant_id)
        except Exception:
            log.exception("checkout of %s failed", tenant_id)
            return to_account("Pagamento non avviato: errore interno, riprova tra poco.", tenant_id)
        return RedirectResponse(url, status_code=303)

    @app.get("/account/{tenant_id}/paid")
    def account_paid(tenant_id: str, request: Request):
        return to_account("Grazie. Il pagamento è in verifica: la redazione si attiva appena il circuito lo conferma, "
                          "di solito entro un minuto.", tenant_id)

    @app.post("/account/{tenant_id}/cancel")
    def account_cancel(tenant_id: str, request: Request, s=Depends(person)):
        owned(s, tenant_id)
        try:
            sub = billing.cancel(admin, tenant_id, gateway)
        except LookupError:
            return to_account("Nessun abbonamento da disdire.", tenant_id)
        end = sub["current_period_end"]
        return to_account("Abbonamento disdetto: niente più rinnovi." +
                          (f" La redazione resta attiva fino al {end:%d/%m/%Y}." if end else ""), tenant_id)

    # --- the operator's console ----------------------------------------------------------

    @app.post("/admin/plans/{code}")
    def ui_plan(code: str, name: str = Form(...), description: str = Form(""), price_month: str = Form(""),
                price_year: str = Form(""), trial_days: str = Form("0"), max_sites: str = Form(""),
                max_languages: str = Form(""), max_active_sources: str = Form(""), max_items_per_day: str = Form(""),
                max_drafts_month: str = Form(""), public: str = Form(""), s=Depends(need_console)):
        try:
            fields = {"name": name.strip(), "description": description.strip(), "price_month_cents": _cents(price_month),
                      "price_year_cents": _cents(price_year), "trial_days": _int(trial_days) or 0,
                      "max_sites": _int(max_sites), "max_languages": _int(max_languages),
                      "max_active_sources": _int(max_active_sources), "max_items_per_day": _int(max_items_per_day),
                      "max_drafts_month": _int(max_drafts_month), "public": public == "1"}
            billing.save_plan(admin, code, fields)
        except ValueError as e:
            return back(f"Piano non salvato: {e if isinstance(e, billing.BillingError) else 'numeri di zero o più'}.")
        return back(f"Piano {name.strip()} salvato. Le redazioni già su questo piano prendono i nuovi limiti al prossimo pagamento.")

    @app.post("/admin/resellers", response_class=HTMLResponse)
    def ui_reseller(request: Request, name: str = Form(...), email: str = Form(...), discount: str = Form("0"),
                    s=Depends(need_console)):
        try:
            pct = float((discount or "0").replace(",", "."))
            rid = billing.create_reseller(admin, name, email, pct)
        except (ValueError, accounts.AccountError) as e:
            return page(request, s, 400, error=f"Rivenditore non creato: {e}.")
        key = accounts.create_operator_token(admin, f"rivenditore {name.strip()}", rid)
        return page(request, s, new={"what": f"Rivenditore {name.strip()} creato con la sua chiave API", "key": key})

    @app.post("/admin/resellers/{rid}/key", response_class=HTMLResponse)
    def ui_reseller_key(request: Request, rid: int, s=Depends(need_console)):
        r = billing.reseller(admin, rid)
        if r is None or not r.active:
            return back("Rivenditore non trovato o disattivato.")
        key = accounts.create_operator_token(admin, f"rivenditore {r.name}", rid)
        return page(request, s, new={"what": f"Nuova chiave API per {r.name}", "key": key})

    @app.post("/admin/resellers/{rid}/active")
    def ui_reseller_active(rid: int, active: str = Form(...), s=Depends(need_console)):
        try:
            billing.update_reseller(admin, rid, {"active": active == "1"})
        except LookupError:
            return back("Rivenditore non trovato.")
        return back("Rivenditore riattivato." if active == "1" else
                    "Rivenditore disattivato: le sue chiavi non valgono più, i suoi clienti restano.")

    @app.post("/admin/subscriptions/{tenant_id}")
    def ui_subscription(tenant_id: str, plan: str = Form(...), interval: str = Form("month"),
                        paid_until: str = Form(""), amount: str = Form(""), s=Depends(need_console)):
        try:
            until = billing.paid_until_date(paid_until)
            cents = _cents(amount)
            if cents is not None:
                if billing.subscription(admin, tenant_id) is None:
                    billing.start(admin, tenant_id, plan, interval=interval, core=ctx.core)
                billing.record_payment(admin, tenant_id, cents, until, s.email, "registrato dalla console", plan,
                                       interval, ctx.core)
            else:
                old = billing.subscription(admin, tenant_id) or {}
                provider = "reseller" if old.get("provider") == "reseller" else "manual"
                billing.start(admin, tenant_id, plan, billing_customer(tenant_id), provider, interval, until, ctx.core)
        except LookupError:
            return back("Redazione non trovata.")
        except ValueError as e:
            return back(f"Abbonamento non cambiato: {e}.")
        return back(f"Abbonamento di {tenant_id} aggiornato.")


def _json(value):
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(value)
