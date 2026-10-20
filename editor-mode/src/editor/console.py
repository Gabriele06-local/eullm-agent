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

from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from . import accounts, newsrooms
from .auth import ROLES

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

    def link(request: Request, token: str) -> str:
        base = (ctx.public_url or str(request.base_url)).rstrip("/")
        return f"{base}/invite/{token}"

    def same_site(request: Request) -> None:
        if request.method != "GET" and request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
            raise HTTPException(403, "cross-site request refused")

    def operator(request: Request) -> str:
        """Who acts as operator: 'api:<key name>' or the person's email."""
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            name = accounts.resolve_operator_token(admin, header.removeprefix("Bearer ").strip())
            if name is None:
                raise HTTPException(401, "missing or invalid operator key")
            return f"api:{name}"
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
        url = link(request, token)
        sent = False
        if ctx.mail is not None:
            subject, body = accounts.link_text(kind, url, newsroom, by)
            try:
                ctx.mail(email, subject, body)
                sent = True
            except Exception:
                sent = False
        return {"email": email, "url": url, "sent": sent}

    def failed(e: Exception):
        if isinstance(e, LookupError):
            raise HTTPException(404, "newsroom not found")
        raise HTTPException(400, str(e))

    # --- JSON -------------------------------------------------------------------

    @app.get("/api/operator/tenants")
    def api_list(external_ref: str | None = None, by: str = Depends(operator)):
        return {"tenants": newsrooms.listing(admin, db, external_ref=external_ref)}

    @app.post("/api/operator/tenants", status_code=201)
    def api_create(request: Request, body: NewsroomIn, by: str = Depends(operator)):
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
    def api_get(tenant_id: str, by: str = Depends(operator)):
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            raise HTTPException(404, "newsroom not found")
        return rows[0] | {"people": [dict(r._mapping) for r in accounts.members(admin, tenant_id)]}

    @app.patch("/api/operator/tenants/{tenant_id}")
    def api_update(tenant_id: str, body: NewsroomPatch, by: str = Depends(operator)):
        try:
            res = newsrooms.update_newsroom(admin, tenant_id, body.spec(), ctx.core)
        except (newsrooms.NewsroomError, LookupError) as e:
            failed(e)
        return {"tenant": newsrooms.listing(admin, db, tenant_id)[0], "warning": res["warning"]}

    @app.post("/api/operator/tenants/{tenant_id}/invites", status_code=201)
    def api_invite(request: Request, tenant_id: str, body: InviteIn, by: str = Depends(operator)):
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            raise HTTPException(404, "newsroom not found")
        try:
            token = accounts.create_invite(admin, body.email, by, tenant_id=tenant_id, role=body.role)
        except accounts.AccountError as e:
            failed(e)
        return send(request, token, "invite", accounts.normalise_email(body.email), rows[0]["name"], "EuLLM Editor")

    @app.delete("/api/operator/tenants/{tenant_id}")
    def api_delete(tenant_id: str, confirm: str = "", by: str = Depends(operator)):
        """Every row of the newsroom goes; `confirm` repeats its id."""
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
            "new": None, **data}, status_code=status_code)
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
                  max_llm_cost_month: str = Form(""), max_drafts_month: str = Form(""), s=Depends(need_console)):
        try:
            limits = {"max_sites": _num(max_sites), "max_active_sources": _num(max_active_sources),
                      "max_items_per_day": _num(max_items_per_day),
                      "max_llm_cost_month": _num(max_llm_cost_month, money=True),
                      "max_drafts_month": _num(max_drafts_month)}
        except ValueError:
            return page(request, s, 400, error="I limiti sono numeri di zero o più; vuoto vuol dire senza limite.")
        spec = {"name": name, "owner_email": owner_email, "limits": limits} | ({"plan": plan.strip()[:60]} if plan.strip() else {})
        try:
            res = newsrooms.create(admin, spec, s.email, ctx.core)
        except (newsrooms.NewsroomError, accounts.AccountError) as e:
            return page(request, s, 400, error=f"Redazione non creata: {e}.")
        new = {"what": f"Redazione {name.strip()} creata (id {res['tenant_id']})", "warning": res["warning"]}
        if res["invite"]:
            new["link"] = send(request, res["invite"], "invite", res["owner_email"], name.strip(), s.email)
        else:
            new["note"] = f"{res['owner_email']} ha già un account: la trova fra le sue redazioni."
        return page(request, s, new=new)

    @app.post("/admin/newsrooms/{tenant_id}")
    def ui_update(tenant_id: str, plan: str = Form(""), max_sites: str = Form(""), max_active_sources: str = Form(""),
                  max_items_per_day: str = Form(""), max_llm_cost_month: str = Form(""),
                  max_drafts_month: str = Form(""), s=Depends(need_console)):
        try:
            limits = {"max_sites": _num(max_sites), "max_active_sources": _num(max_active_sources),
                      "max_items_per_day": _num(max_items_per_day),
                      "max_llm_cost_month": _num(max_llm_cost_month, money=True),
                      "max_drafts_month": _num(max_drafts_month)}
            res = newsrooms.update_newsroom(admin, tenant_id, {"limits": limits} | (
                {"plan": plan.strip()[:60]} if plan.strip() else {}), ctx.core)
        except ValueError:
            return back("I limiti sono numeri di zero o più; vuoto vuol dire senza limite.")
        except LookupError:
            return back("Redazione non trovata.")
        return back(f"Piano di {tenant_id} aggiornato." + (f" Attenzione: {res['warning']}" if res["warning"] else ""))

    @app.post("/admin/newsrooms/{tenant_id}/active")
    def ui_active(tenant_id: str, active: str = Form(...), s=Depends(need_console)):
        on = active == "1"
        try:
            newsrooms.update_newsroom(admin, tenant_id, {"active": on})
        except LookupError:
            return back("Redazione non trovata.")
        return back(f"Redazione {tenant_id} riattivata." if on else
                    f"Redazione {tenant_id} sospesa: le persone non entrano e il lavoro programmato si ferma, i dati restano.")

    @app.post("/admin/newsrooms/{tenant_id}/invite", response_class=HTMLResponse)
    def ui_invite(request: Request, tenant_id: str, email: str = Form(...), role: str = Form("owner"),
                  s=Depends(need_console)):
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            return back("Redazione non trovata.")
        try:
            token = accounts.create_invite(admin, email, s.email, tenant_id=tenant_id, role=role)
        except accounts.AccountError as e:
            return page(request, s, 400, error=f"Invito non creato: {e}.")
        return page(request, s, new={"what": f"Invito a {rows[0]['name']} per {email.strip().lower()}",
                                     "link": send(request, token, "invite", email.strip().lower(), rows[0]["name"], s.email)})

    @app.post("/admin/newsrooms/{tenant_id}/delete")
    def ui_delete(tenant_id: str, confirm: str = Form(""), s=Depends(need_console)):
        if confirm.strip() != tenant_id:
            return back(f"Per cancellare scrivi l'id della redazione ({tenant_id}): niente è stato cancellato.")
        try:
            newsrooms.delete(admin, tenant_id)
        except LookupError:
            return back("Redazione non trovata.")
        return back(f"Redazione {tenant_id} cancellata con tutti i suoi dati.")

    @app.get("/admin/enter/{tenant_id}")
    def ui_enter(request: Request, tenant_id: str):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if not newsrooms.listing(admin, db, tenant_id):
            return back("Redazione non trovata.")
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
            return page(request, s, 400, error=f"Invito non creato: {e}.")
        return page(request, s, new={"what": f"Invito come operatore per {email.strip().lower()}",
                                     "link": send(request, token, "invite", email.strip().lower(), None, s.email)})

    @app.post("/admin/reset", response_class=HTMLResponse)
    def ui_reset(request: Request, email: str = Form(...), s=Depends(need_console)):
        try:
            e = accounts.normalise_email(email)
        except accounts.AccountError as err:
            return page(request, s, 400, error=f"{err}.")
        with accounts._read(admin) as conn:
            if accounts.find_user(conn, e) is None:
                return page(request, s, 400, error=f"Nessun account per {e}.")
        token = accounts.create_invite(admin, e, s.email, kind="reset")
        return page(request, s, new={"what": f"Link per reimpostare la password di {e}",
                                     "link": send(request, token, "reset", e, None, s.email)})

    @app.post("/admin/keys", response_class=HTMLResponse)
    def ui_new_key(request: Request, name: str = Form(...), s=Depends(need_console)):
        try:
            key = accounts.create_operator_token(admin, name)
        except accounts.AccountError as e:
            return page(request, s, 400, error=f"{e}.")
        return page(request, s, new={"what": f"Chiave API «{name.strip()}» creata", "key": key})

    @app.post("/admin/keys/{key_id}/revoke")
    def ui_revoke_key(key_id: int, s=Depends(need_console)):
        return back("Chiave revocata." if accounts.revoke_operator_token(admin, key_id) else "Chiave già revocata.")


def set_session(request: Request, resp, token: str) -> None:
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                    max_age=accounts.SESSION_DAYS * 86400)


def jsonable(value):
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(value)
