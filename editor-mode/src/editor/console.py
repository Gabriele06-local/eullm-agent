"""The operator's side of the service: a console page and a JSON API to
create newsrooms, give them a plan and limits, suspend or delete them,
invite their owners, and see what each one uses.

Two ways in, both checked here:
- a person signed in whose account is an operator (the console, and the
  API from the same browser);
- an operator API key (`Authorization: Bearer edo_...`), for the billing
  system: when a payment goes through it calls POST /api/operator/tenants
  with the customer's email, and the owner receives the invite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from . import accounts, billing, commerce, newsrooms, repo, trials, usage
from . import models as m
from .auth import ROLES
from .i18n import t

COOKIE = "editor_token"
SITE_COOKIE = "editor_site"
NOTICE_COOKIE = "editor_notice"


class LimitsIn(BaseModel):
    """A missing field leaves the limit as it is; null removes it."""
    max_sites: int | None = Field(None, ge=0)
    max_active_sources: int | None = Field(None, ge=0)
    max_items_per_day: int | None = Field(None, ge=0)
    max_llm_cost_month: float | None = Field(None, ge=0)
    max_drafts_month: int | None = Field(None, ge=0)
    max_languages: int | None = Field(None, ge=1)


class _Settings(BaseModel):
    plan: str | None = Field(None, max_length=60)
    timezone: str | None = Field(None, max_length=60)
    briefing_hour: int | None = Field(None, ge=0, le=23)
    language: str | None = Field(None, pattern="^[a-z]{2}$")
    limits: LimitsIn | None = None

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v):
        if v is not None:
            try:
                ZoneInfo(v)
            except Exception as e:
                raise ValueError("unknown time zone") from e
        return v

    def spec(self) -> dict:
        out = self.model_dump(exclude_unset=True, exclude={"limits"})
        if self.limits is not None:
            out["limits"] = self.limits.model_dump(exclude_unset=True)
        return out


class NewsroomIn(_Settings):
    name: str = Field(..., min_length=1, max_length=100)
    owner_email: str = Field(..., max_length=254)
    owner_name: str | None = Field(None, max_length=100)
    tenant_id: str | None = Field(None, max_length=40)
    # The billing system's reference (a subscription, a customer): asking
    # again with the same reference returns the same newsroom.
    external_ref: str | None = Field(None, max_length=200)


class NewsroomPatch(_Settings):
    name: str | None = Field(None, min_length=1, max_length=100)
    active: bool | None = None


class InviteIn(BaseModel):
    email: str = Field(..., max_length=254)
    role: str = Field("owner", pattern="^(owner|editor|viewer)$")


def _num(value: str, money: bool = False):
    """A limit typed in the console: empty means no limit."""
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    n = float(value) if money else int(value)
    if n < 0:
        raise ValueError
    return n


def register(app, ctx) -> None:
    """ctx: db, admin, pages, core, mail (or None), public_url."""
    db, admin, pages = ctx.db, ctx.admin, ctx.pages
    pages.env.filters.setdefault("tok", usage.tokens)

    def refusals() -> list[dict]:
        with accounts._read(admin) as conn:
            return trials.refusals(conn)

    def link(request: Request, token: str) -> str:
        base = (ctx.public_url or str(request.base_url)).rstrip("/")
        return f"{base}/invite/{token}"

    def same_site(request: Request) -> None:
        if request.method != "GET" and request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
            raise HTTPException(403, "cross-site request refused")

    def operator(request: Request) -> str:
        """Who acts as operator: 'api:<key name>' or the person's email."""
        request.state.reseller_id = None
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            key = accounts.operator_key(admin, header.removeprefix("Bearer ").strip())
            if key is None:
                raise HTTPException(401, "missing or invalid operator key")
            request.state.reseller_id = key.reseller_id
            return f"api:{key.name}"
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        if s is None:
            raise HTTPException(401, "sign in as an operator")
        if not s.operator:
            raise HTTPException(403, "operator account required")
        same_site(request)
        return s.email

    def send(request: Request, token: str, kind: str, email: str, newsroom: str | None, by: str) -> dict:
        """Email the link when SMTP is configured; the link is returned
        either way, for the caller to deliver it itself."""
        url = link(request, token) if kind != "ready" else f"{(ctx.public_url or str(request.base_url)).rstrip('/')}/login"
        sent = False
        if ctx.mail is not None:
            subject, body = accounts.link_text(kind, url, newsroom, by)
            try:
                ctx.mail(email, subject, body)
                sent = True
            except Exception:
                sent = False
        return {"email": email, "url": url, "sent": sent}

    def own(request: Request, tenant_id: str) -> None:
        """A reseller's key reaches only its own customers' newsrooms."""
        rid = getattr(request.state, "reseller_id", None)
        if rid is not None and billing.reseller_of_tenant(admin, tenant_id) != rid:
            raise HTTPException(404, "newsroom not found")

    def no_reseller(request: Request) -> None:
        if getattr(request.state, "reseller_id", None) is not None:
            raise HTTPException(403, "not available to a reseller key")

    def failed(e: Exception):
        if isinstance(e, LookupError):
            raise HTTPException(404, "newsroom not found")
        raise HTTPException(400, str(e))

    # --- JSON -------------------------------------------------------------------

    @app.get("/api/operator/tenants")
    def api_list(request: Request, external_ref: str | None = None, by: str = Depends(operator)):
        rows = newsrooms.listing(admin, db, external_ref=external_ref)
        rid = request.state.reseller_id
        if rid is not None:
            mine = billing.tenants_of_reseller(admin, rid)
            rows = [r for r in rows if r["tenant_id"] in mine]
        return {"tenants": rows}

    @app.post("/api/operator/tenants", status_code=201)
    def api_create(request: Request, body: NewsroomIn, by: str = Depends(operator)):
        no_reseller(request)  # a reseller signs its customers up (/api/operator/signup)
        try:
            res = newsrooms.create(admin, body.spec() | body.model_dump(
                include={"name", "owner_email", "owner_name", "tenant_id", "external_ref"}), by, ctx.core)
        except (newsrooms.NewsroomError, accounts.AccountError) as e:
            failed(e)
        out = {"tenant": newsrooms.listing(admin, db, res["tenant_id"])[0], "created": res["created"],
               "owner": {"email": res["owner_email"], "invite": None}, "warning": res["warning"]}
        if res["invite"]:
            out["owner"]["invite"] = send(request, res["invite"], "invite", res["owner_email"], body.name, "EuLLM Editor")
        # Already known (the same external_ref): 200 instead of 201.
        return out if res["created"] else JSONResponse(jsonable(out), status_code=200)

    @app.get("/api/operator/tenants/{tenant_id}")
    def api_get(request: Request, tenant_id: str, by: str = Depends(operator)):
        own(request, tenant_id)
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            raise HTTPException(404, "newsroom not found")
        return rows[0] | {"people": [dict(r._mapping) for r in accounts.members(admin, tenant_id)]}

    @app.patch("/api/operator/tenants/{tenant_id}")
    def api_update(request: Request, tenant_id: str, body: NewsroomPatch, by: str = Depends(operator)):
        own(request, tenant_id)
        if request.state.reseller_id is not None and body.model_dump(exclude_unset=True).keys() - {"name", "active"}:
            raise HTTPException(403, "a reseller changes the plan through /subscription")
        try:
            res = newsrooms.update_newsroom(admin, tenant_id, body.spec(), ctx.core)
        except (newsrooms.NewsroomError, LookupError) as e:
            failed(e)
        return {"tenant": newsrooms.listing(admin, db, tenant_id)[0], "warning": res["warning"]}

    @app.post("/api/operator/tenants/{tenant_id}/invites", status_code=201)
    def api_invite(request: Request, tenant_id: str, body: InviteIn, by: str = Depends(operator)):
        own(request, tenant_id)
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            raise HTTPException(404, "newsroom not found")
        try:
            token = accounts.create_invite(admin, body.email, by, tenant_id=tenant_id, role=body.role)
        except accounts.AccountError as e:
            failed(e)
        return send(request, token, "invite", accounts.normalise_email(body.email), rows[0]["name"], "EuLLM Editor")

    @app.delete("/api/operator/tenants/{tenant_id}")
    def api_delete(request: Request, tenant_id: str, confirm: str = "", by: str = Depends(operator)):
        """Every row of the newsroom goes; `confirm` repeats its id."""
        own(request, tenant_id)
        if confirm != tenant_id:
            raise HTTPException(400, "repeat the newsroom id in ?confirm= to delete it")
        try:
            return {"deleted": newsrooms.delete(admin, tenant_id)}
        except LookupError as e:
            failed(e)

    # --- console ---------------------------------------------------------------------

    def console_user(request: Request):
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        if s is None or not s.operator:
            return None
        return s

    def page(request: Request, s, status_code: int = 200, **data):
        notice = request.cookies.get(NOTICE_COOKIE)
        from urllib.parse import unquote
        resp = pages.TemplateResponse(request, "console.html.j2", {
            "me": s, "rooms": newsrooms.listing(admin, db), "operators": accounts.operators(admin),
            "keys": accounts.operator_tokens(admin), "pending": accounts.pending_invites(admin, None),
            "roles": ROLES, "notice": unquote(notice) if notice else None, "mine": accounts.memberships(admin, s.user_id),
            "customers": billing.customers(admin), "resellers": billing.resellers(admin), "plans": billing.plans(admin),
            "subs": billing.subscriptions(admin), "reseller_names": {r["id"]: r["name"] for r in billing.resellers(admin)},
            "statuses": billing.STATUS_LABELS, "money": billing.money, "online": ctx.gateway is not None,
            "refusals": refusals(), "trial_kinds": trials.LABELS, "new": None, **data}, status_code=status_code)
        if notice:
            resp.delete_cookie(NOTICE_COOKIE)
        return resp

    def back(notice: str) -> RedirectResponse:
        from urllib.parse import quote
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(NOTICE_COOKIE, quote(notice), max_age=60, httponly=True, samesite="strict")
        return resp

    def need_console(request: Request):
        s = console_user(request)
        if s is None:
            raise HTTPException(403, "operator account required")
        same_site(request)
        return s

    def usage_tenants() -> tuple[list[str], dict]:
        with accounts._read(admin) as conn:
            rows = conn.execute(select(m.tenants.c.tenant_id, m.tenants.c.name)).all()
        names = {r.tenant_id: r.name for r in rows}
        names[repo.ARCHIVE] = t("Archivio condiviso")
        return sorted(names), names

    @app.get("/admin/usage", response_class=HTMLResponse)
    def usage_page(request: Request, days: int = 30):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        days = max(1, min(days, 366))
        tenants, names = usage_tenants()
        notice = request.cookies.get(NOTICE_COOKIE)
        from urllib.parse import unquote
        resp = pages.TemplateResponse(request, "usage.html.j2", {
            "me": s, "r": usage.report(admin, db, tenants, days), "names": names, "purposes": usage.PURPOSES,
            "notice": unquote(notice) if notice else None})
        if notice:
            resp.delete_cookie(NOTICE_COOKIE)
        return resp

    @app.get("/admin/usage.csv")
    def usage_csv(request: Request, days: int = 30):
        if console_user(request) is None:
            raise HTTPException(403, "operator account required")
        days = max(1, min(days, 366))
        tenants, _ = usage_tenants()
        day = datetime.now(UTC).date().isoformat()
        return Response(usage.csv_rows(db, tenants, days), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="model-usage-{days}d-{day}.csv"'})

    def back_usage(notice: str) -> RedirectResponse:
        from urllib.parse import quote
        resp = RedirectResponse("/admin/usage#prezzi", status_code=303)
        resp.set_cookie(NOTICE_COOKIE, quote(notice), max_age=60, httponly=True, samesite="strict")
        return resp

    @app.post("/admin/usage/prices")
    def usage_price(model: str = Form(...), input_per_mtok: str = Form(...), output_per_mtok: str = Form(...),
                    note: str = Form(""), s=Depends(need_console)):
        try:
            usage.set_price(admin, model, float(input_per_mtok.replace(",", ".")),
                            float(output_per_mtok.replace(",", ".")), note)
        except ValueError:
            return back_usage(t("Prezzo non salvato: servono due numeri, zero o più (per esempio 0,40)."))
        return back_usage(t("Prezzo di riferimento di {model} salvato.", model=model.strip()))

    @app.post("/admin/usage/prices/delete")
    def usage_price_delete(model: str = Form(...), s=Depends(need_console)):
        usage.delete_price(admin, model)
        return back_usage(t("Prezzo di riferimento di {model} tolto.", model=model))

    @app.get("/operator")
    def old_console():
        return RedirectResponse("/admin", status_code=301)

    @app.get("/admin", response_class=HTMLResponse)
    def console(request: Request):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, s)

    @app.post("/admin/newsrooms", response_class=HTMLResponse)
    def ui_create(request: Request, name: str = Form(...), owner_email: str = Form(...), plan: str = Form(""),
                  max_sites: str = Form(""), max_active_sources: str = Form(""), max_items_per_day: str = Form(""),
                  max_llm_cost_month: str = Form(""), max_drafts_month: str = Form(""), max_languages: str = Form(""),
                  s=Depends(need_console)):
        try:
            limits = {"max_sites": _num(max_sites), "max_active_sources": _num(max_active_sources),
                      "max_items_per_day": _num(max_items_per_day),
                      "max_llm_cost_month": _num(max_llm_cost_month, money=True),
                      "max_drafts_month": _num(max_drafts_month), "max_languages": _num(max_languages)}
        except ValueError:
            return page(request, s, 400, error=t("I limiti sono numeri di zero o più; vuoto vuol dire senza limite."))
        spec = {"name": name, "owner_email": owner_email, "limits": limits} | ({"plan": plan.strip()[:60]} if plan.strip() else {})
        try:
            res = newsrooms.create(admin, spec, s.email, ctx.core)
        except (newsrooms.NewsroomError, accounts.AccountError) as e:
            return page(request, s, 400, error=t("Redazione non creata: {error}.", error=e))
        new = {"what": t("Redazione {name} creata (id {tenant})", name=name.strip(), tenant=res["tenant_id"]),
               "warning": res["warning"]}
        if res["invite"]:
            new["link"] = send(request, res["invite"], "invite", res["owner_email"], name.strip(), s.email)
        else:
            new["note"] = t("{email} ha già un account: la trova fra le sue redazioni.", email=res["owner_email"])
        return page(request, s, new=new)

    @app.post("/admin/newsrooms/{tenant_id}")
    def ui_update(tenant_id: str, name: str = Form(""), plan: str = Form(""), max_sites: str = Form(""),
                  max_active_sources: str = Form(""), max_items_per_day: str = Form(""),
                  max_llm_cost_month: str = Form(""), max_drafts_month: str = Form(""), max_languages: str = Form(""),
                  s=Depends(need_console)):
        try:
            limits = {"max_sites": _num(max_sites), "max_active_sources": _num(max_active_sources),
                      "max_items_per_day": _num(max_items_per_day),
                      "max_llm_cost_month": _num(max_llm_cost_month, money=True),
                      "max_drafts_month": _num(max_drafts_month), "max_languages": _num(max_languages)}
            res = newsrooms.update_newsroom(admin, tenant_id, {"limits": limits} | (
                {"plan": plan.strip()[:60]} if plan.strip() else {}) | (
                {"name": name.strip()} if name.strip() else {}), ctx.core)
        except ValueError:
            return back(t("I limiti sono numeri di zero o più; vuoto vuol dire senza limite."))
        except LookupError:
            return back(t("Redazione non trovata."))
        return back(t("Redazione {tenant} aggiornata.", tenant=tenant_id)
                    + (" " + t("Attenzione: {warning}", warning=res["warning"]) if res["warning"] else ""))

    @app.post("/admin/newsrooms/{tenant_id}/active")
    def ui_active(tenant_id: str, active: str = Form(...), s=Depends(need_console)):
        on = active == "1"
        try:
            newsrooms.update_newsroom(admin, tenant_id, {"active": on})
        except LookupError:
            return back(t("Redazione non trovata."))
        return back(t("Redazione {tenant} riattivata.", tenant=tenant_id) if on else
                    t("Redazione {tenant} sospesa: le persone non entrano e il lavoro programmato si ferma, i dati restano.",
                      tenant=tenant_id))

    @app.post("/admin/newsrooms/{tenant_id}/invite", response_class=HTMLResponse)
    def ui_invite(request: Request, tenant_id: str, email: str = Form(...), role: str = Form("owner"),
                  s=Depends(need_console)):
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            return back(t("Redazione non trovata."))
        try:
            token = accounts.create_invite(admin, email, s.email, tenant_id=tenant_id, role=role)
        except accounts.AccountError as e:
            return page(request, s, 400, error=t("Invito non creato: {error}.", error=e))
        return page(request, s, new={"what": t("Invito a {newsroom} per {email}", newsroom=rows[0]["name"],
                                                       email=email.strip().lower()),
                                     "link": send(request, token, "invite", email.strip().lower(), rows[0]["name"], s.email)})

    @app.post("/admin/newsrooms/{tenant_id}/delete")
    def ui_delete(tenant_id: str, confirm: str = Form(""), s=Depends(need_console)):
        if confirm.strip() != tenant_id:
            return back(t("Per cancellare scrivi l'id della redazione ({tenant}): niente è stato cancellato.", tenant=tenant_id))
        try:
            newsrooms.delete(admin, tenant_id)
        except LookupError:
            return back(t("Redazione non trovata."))
        return back(t("Redazione {tenant} cancellata con tutti i suoi dati.", tenant=tenant_id))

    @app.get("/admin/enter/{tenant_id}")
    def ui_enter(request: Request, tenant_id: str):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if not newsrooms.listing(admin, db, tenant_id):
            return back(t("Redazione non trovata."))
        token = accounts.start_session(admin, s.user_id, tenant_id)
        accounts.end_session(admin, request.cookies.get(COOKIE))
        resp = RedirectResponse("/", status_code=303)
        set_session(request, resp, token)
        resp.delete_cookie(SITE_COOKIE)
        return resp

    @app.post("/admin/operators", response_class=HTMLResponse)
    def ui_add_operator(request: Request, email: str = Form(...), s=Depends(need_console)):
        try:
            token = accounts.create_invite(admin, email, s.email, operator=True)
        except accounts.AccountError as e:
            return page(request, s, 400, error=t("Invito non creato: {error}.", error=e))
        return page(request, s, new={"what": t("Invito come operatore per {email}", email=email.strip().lower()),
                                     "link": send(request, token, "invite", email.strip().lower(), None, s.email)})

    @app.post("/admin/operators/{uid}/remove")
    def ui_remove_operator(uid: int, s=Depends(need_console)):
        try:
            email = accounts.remove_operator(admin, uid, s.user_id)
        except LookupError:
            return back(t("Operatore non trovato."))
        except accounts.AccountError as e:
            return back(t("Operatore non tolto: {e}.", e=e))
        return back(t("{email} non è più operatore: le sue redazioni restano.", email=email))

    @app.post("/admin/invites/{sha}/revoke")
    def ui_revoke_operator_invite(sha: str, s=Depends(need_console)):
        return back(t("Invito annullato.") if accounts.revoke_invite(admin, None, sha)
                    else t("Invito già usato o scaduto."))

    @app.post("/admin/reset", response_class=HTMLResponse)
    def ui_reset(request: Request, email: str = Form(...), s=Depends(need_console)):
        try:
            e = accounts.normalise_email(email)
        except accounts.AccountError as err:
            return page(request, s, 400, error=f"{err}.")
        with accounts._read(admin) as conn:
            if accounts.find_user(conn, e) is None:
                return page(request, s, 400, error=t("Nessun account per {email}.", email=e))
        token = accounts.create_invite(admin, e, s.email, kind="reset")
        return page(request, s, new={"what": t("Link per reimpostare la password di {email}", email=e),
                                     "link": send(request, token, "reset", e, None, s.email)})

    @app.post("/admin/keys", response_class=HTMLResponse)
    def ui_new_key(request: Request, name: str = Form(...), s=Depends(need_console)):
        try:
            key = accounts.create_operator_token(admin, name)
        except accounts.AccountError as e:
            return page(request, s, 400, error=f"{e}.")
        return page(request, s, new={"what": t("Chiave API «{name}» creata", name=name.strip()), "key": key})

    @app.post("/admin/keys/{key_id}/revoke")
    def ui_revoke_key(key_id: int, s=Depends(need_console)):
        return back(t("Chiave revocata.") if accounts.revoke_operator_token(admin, key_id) else t("Chiave già revocata."))

    commerce.register(app, ctx, operator=operator, own=own, no_reseller=no_reseller, send=send,
                      need_console=need_console, page=page, back=back, failed=failed)


def set_session(request: Request, resp, token: str) -> None:
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                    max_age=accounts.SESSION_DAYS * 86400)


def jsonable(value):
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(value)
