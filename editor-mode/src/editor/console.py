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

import logging
from datetime import UTC, datetime
from importlib import metadata
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from . import accounts, backups, billing, commerce, credits, installs, newsrooms, repo, trials, updates, usage
from . import models as m
from .auth import ROLES
from .i18n import t

log = logging.getLogger(__name__)


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when

COOKIE = "editor_token"
INSTALLED = metadata.version("eullm-editor-mode")
SITE_COOKIE = "editor_site"
NOTICE_COOKIE = "editor_notice"
NOTICE_AT_COOKIE = "editor_notice_at"  # the console section the notice belongs to
NOTICE_KIND_COOKIE = "editor_notice_kind"  # "error" when the notice says something went wrong


class LimitsIn(BaseModel):
    """A missing field leaves the limit as it is; null removes it."""
    max_sites: int | None = Field(None, ge=0)
    max_active_sources: int | None = Field(None, ge=0)
    max_llm_cost_month: float | None = Field(None, ge=0)
    max_drafts_month: int | None = Field(None, ge=0)
    max_languages: int | None = Field(None, ge=1)
    credits_month: int | None = Field(None, ge=0)
    max_users: int | None = Field(None, ge=1)


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
    """A limit or an amount typed in the console: empty means no limit.

    The decimal separator is the comma, as the fields' hints say ("19,90"),
    so dots group thousands: "1.000" is a thousand, not one. A dot is kept as
    the decimal point when it cannot be a group (the last part is not three
    digits), which is how the forms themselves pre-fill prices ("19.90").
    """
    value = (value or "").strip().replace(" ", "")
    if not value:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    else:
        parts = value.split(".")
        if (len(parts) > 1 and parts[0] not in ("", "0")
                and all(len(x) == 3 for x in parts[1:])):
            value = "".join(parts)
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

    def credit_prices() -> dict:
        with accounts._read(admin) as conn:
            return credits.prices(conn)

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
        url = link(request, token) if kind not in ("ready", "ready_pay") else f"{(ctx.public_url or str(request.base_url)).rstrip('/')}/login"
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
            out["owner"]["invite"] = send(request, res["invite"], "invite", res["owner_email"], body.name, "Redazia")
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
        return send(request, token, "invite", accounts.normalise_email(body.email), rows[0]["name"], "Redazia")

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

    def room_work(room: str) -> dict:
        """A newsroom's sites with their state and its recent failed work,
        for its card: what the operator checks before answering a customer."""
        from datetime import timedelta

        from . import activity, autopilot, jobs

        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        out = {"sites": [], "failed": []}
        try:
            with db.tenant(room) as ts:
                profiles = {}
                for r in ts.execute(select(m.editorial_profiles.c.site_id, m.editorial_profiles.c.status,
                                           m.editorial_profiles.c.body)
                                    .order_by(m.editorial_profiles.c.version)).all():
                    profiles.setdefault(r.site_id, {})[r.status] = r.body
                for site in ts.execute(select(m.sites).order_by(m.sites.c.id)).all():
                    mine = profiles.get(site.id, {})
                    cfg = autopilot.settings_of(mine.get("approved"))
                    day = autopilot.today(ts, site.id, start)
                    out["sites"].append({
                        "id": site.id, "name": site.name or site.domain, "domain": site.domain,
                        "profile": "approved" if "approved" in mine else "draft" if "draft" in mine else "none",
                        "auto": bool(mine.get("approved")) and cfg["enabled"], "per_day": cfg["per_day"],
                        "published": day["published"], "discarded": day["discarded"], "held": day["held"]})
                week = datetime.now(UTC) - timedelta(days=7)
                out["failed"] = [dict(j, what=t(activity.KINDS[j["kind"]]) if j["kind"] in activity.KINDS else j["kind"])
                                 for j in jobs.recent(ts, 40) if j["status"] == "failed"
                                 and _utc(j["finished_at"] or j["started_at"] or week) >= week][:5]
        except Exception:
            log.exception("work of %s not read", room)
        return out

    def page(request: Request, s, status_code: int = 200, view: str = "redazioni", room: str | None = None,
             **data):
        """One page of the console: `view` is the section of the menu, `room`
        the newsroom shown in full (view "redazione")."""
        notice = request.cookies.get(NOTICE_COOKIE)
        notice_at = request.cookies.get(NOTICE_AT_COOKIE) if notice else None
        notice_error = bool(notice) and request.cookies.get(NOTICE_KIND_COOKIE) == "error"
        from urllib.parse import unquote
        if room:  # a refund cancelled in Mollie's dashboard shows as cancelled here
            try:
                billing.sync_refunds(admin, ctx.gateway, room)
            except Exception:
                log.exception("refunds of %s not synced", room)
        if view == "aggiornamenti" and (where := updates.folder(ctx.updates_dir)) is not None:
            updates.refresh(where, INSTALLED, updates.this_install())
        plans = billing.plans(admin)
        subs = billing.subscriptions(admin)
        rooms = newsrooms.listing(admin, db, room) if room else newsrooms.listing(admin, db)
        extra = {}
        if view in ("redazioni", "pagamenti", "cliente"):
            pays = billing.all_payments(admin)
            extra["month"] = billing.month_numbers(pays, subs)
            extra["all_pays"] = pays
        if view == "redazioni":
            extra["numbers"] = room_numbers(rooms, subs)
        if view == "cliente":
            cid = data.get("customer_id")
            extra["the_customer"] = next((c for c in billing.customers(admin) if c["id"] == cid), None)
            mine = {n["tenant_id"] for n in (extra["the_customer"] or {}).get("newsrooms", [])}
            extra["all_pays"] = [x for x in extra["all_pays"] if x["tenant_id"] in mine]
        if view == "redazione" and room:
            extra["invited"] = accounts.pending_invites(admin, room)
        if view == "nuova" and not data.get("form") and (pick := request.query_params.get("customer", "")).isdigit():
            data["form"] = {"customer_mode": "existing", "customer_id": pick}  # from a customer's page
        resp = pages.TemplateResponse(request, "console.html.j2", {
            "view": view, "me": s, "rooms": rooms, "operators": accounts.operators(admin),
            "keys": accounts.operator_tokens(admin), "pending": accounts.pending_invites(admin, None),
            "roles": ROLES, "notice": unquote(notice) if notice else None, "notice_at": notice_at, "mine": accounts.memberships(admin, s.user_id),
            "customers": billing.customers(admin), "resellers": billing.resellers(admin), "plans": plans,
            "plan_names": {p["code"]: p["name"] for p in plans},
            "subs": subs, "payments": billing.payments_by_tenant(admin),
            "single_room": room, "people": accounts.members(admin, room) if room else [],
            "work": room_work(room) if room and view == "redazione" else None,
            "room_customer": billing.customer_of(admin, room) if room else None,
            "credits": credits.balances(admin), "credit_prices": credit_prices(), "packs": credits.packs(admin),
            "addons": billing.addons(admin),
            "reseller_names": {r["id"]: r["name"] for r in billing.resellers(admin)},
            "statuses": billing.STATUS_LABELS, "money": billing.money, "online": ctx.gateway is not None,
            "refusals": refusals(), "trial_kinds": trials.LABELS,
            "updates": updates.state(updates.folder(ctx.updates_dir), INSTALLED), "installs": installs.summary(admin),
            "backups": backups.state(updates.folder(ctx.updates_dir)) if view == "backup" else None,
            "notice_error": notice_error, "new": None, "form": {}, "reopen": None, "q": "", **extra, **data},
            status_code=status_code)
        if notice:
            resp.delete_cookie(NOTICE_COOKIE)
            resp.delete_cookie(NOTICE_AT_COOKIE)
            resp.delete_cookie(NOTICE_KIND_COOKIE)
        return resp

    def room_numbers(rooms: list[dict], subs: dict) -> dict:
        """The top of the newsrooms page: what the plans bring in a month,
        trials (and those ending within three days), newsrooms waiting for
        a payment, the model's cost of the month."""
        trial = [x for x in subs.values() if x["status"] == "trialing"]
        cost = sum(float(((r.get("usage") or {}).get("llm_cost") or {}).get("used") or 0) for r in rooms)
        mrr = sum(x.get("monthly_cents") or 0 for x in subs.values())
        return {"mrr": mrr, "paying": sum(1 for x in subs.values() if x["status"] == "active"),
                "trial": len(trial), "soon": sum(1 for x in trial if (x.get("days_left") or 0) <= 3),
                "unpaid": sum(1 for r in rooms if r.get("hold") == "billing"
                              or (subs.get(r["tenant_id"]) or {}).get("status") in ("expired", "past_due")),
                "cost": cost, "margin": mrr / 100 - cost}

    def back(notice: str, at: str = "", error: bool = False) -> RedirectResponse:
        """Back to the page of the console `at` belongs to (a newsroom
        r-<id>, a customer c-<id>, a reseller v-<id>, a plan p-<code>, or a
        section), the notice shown at the top. `error`: something went
        wrong, and the window it came from (`at`, when it is one) opens
        again with the reason."""
        from urllib.parse import quote
        resp = RedirectResponse(admin_page(at), status_code=303)
        resp.set_cookie(NOTICE_COOKIE, quote(notice), max_age=60, httponly=True, samesite="strict")
        if at:
            resp.set_cookie(NOTICE_AT_COOKIE, at, max_age=60, httponly=True, samesite="strict")
        if error:
            resp.set_cookie(NOTICE_KIND_COOKIE, "error", max_age=60, httponly=True, samesite="strict")
        return resp

    def admin_page(at: str) -> str:
        from urllib.parse import quote
        if at.startswith("r-"):
            return f"/admin/newsrooms/{quote(at[2:].split('--')[0])}"
        if at.startswith("c-") and at[2:].split("--")[0].isdigit():
            return f"/admin/customers/{at[2:].split('--')[0]}"
        for prefix, path in (("v-", "resellers"), ("p-", "plans"), ("k-", "plans")):
            if at.startswith(prefix):
                return f"/admin/{path}"
        return {"clienti": "/admin/customers", "nuovo-cliente": "/admin/customers/new", "rivenditori": "/admin/resellers",
                "piani": "/admin/plans", "nuovo-piano": "/admin/plans", "crediti": "/admin/plans",
                "nuovo-pacchetto": "/admin/plans", "nuovo-rivenditore": "/admin/resellers",
                "aggiornamenti": "/admin/updates", "backup": "/admin/backups", "operatori": "/admin/operators",
                "nuovo-amministratore": "/admin/operators", "api": "/admin/keys", "nuova-chiave": "/admin/keys",
                "pagamenti": "/admin/payments", "nuova": "/admin/new"}.get(at, "/admin")

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
            "routable": usage.ROUTABLE, "routes": usage.route_list(admin),
            "subs": billing.subscriptions(admin), "plans": {p["code"]: p for p in billing.plans(admin)},
            "notice": unquote(notice) if notice else None, "view": "consumi",
            "updates": updates.state(updates.folder(ctx.updates_dir), INSTALLED),
            "mine": accounts.memberships(admin, s.user_id)})
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

    def back_usage(notice: str, at: str = "prezzi") -> RedirectResponse:
        from urllib.parse import quote
        resp = RedirectResponse(f"/admin/usage#{at}", status_code=303)
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

    @app.post("/admin/usage/prices/all")
    async def usage_prices_all(request: Request, s=Depends(need_console)):
        """The whole table of reference prices with one Save: a row with
        both prices empty has no price, a ticked Togli removes it."""
        form = await request.form()
        remove = {str(v) for v in form.getlist("remove")}
        bad = []
        for model, i, o, note in zip(form.getlist("model"), form.getlist("input_per_mtok"),
                                     form.getlist("output_per_mtok"), form.getlist("note"), strict=False):
            model, i, o = str(model).strip(), str(i).strip(), str(o).strip()
            if not model:
                continue
            if model in remove or (not i and not o):
                usage.delete_price(admin, model)
                continue
            try:
                usage.set_price(admin, model, float(i.replace(",", ".")), float(o.replace(",", ".")), str(note))
            except ValueError:
                bad.append(model)
        if bad:
            return back_usage(t("Prezzi salvati, tranne {models}: servono due numeri, zero o più (per esempio 0,40).",
                                models=", ".join(bad)))
        return back_usage(t("Prezzi di riferimento salvati."))

    @app.post("/admin/usage/models")
    async def usage_models(request: Request, s=Depends(need_console)):
        form = await request.form()
        for purpose in usage.ROUTABLE:
            if purpose in form:
                usage.set_route(admin, purpose, str(form[purpose]))
        return back_usage(t("Modelli dei passi salvati: valgono dalla prossima chiamata, entro un minuto."), "modelli")

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

    def section(view: str):
        def show(request: Request):
            s = console_user(request)
            if s is None:
                return RedirectResponse("/login", status_code=303)
            return page(request, s, view=view)
        return show

    # Each entry of the menu is a page of its own: the list of newsrooms
    # stays short however many there are, the rest opens when needed.
    for path, view in (("new", "nuova"), ("customers", "clienti"), ("resellers", "rivenditori"),
                       ("plans", "piani"), ("operators", "operatori"), ("keys", "api"),
                       ("updates", "aggiornamenti"), ("backups", "backup")):
        app.get(f"/admin/{path}", response_class=HTMLResponse)(section(view))

    @app.get("/admin/customers/new", response_class=HTMLResponse)
    def console_new_customer(request: Request):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, s, view="nuovo-cliente")

    @app.get("/admin/customers/{cid}", response_class=HTMLResponse)
    def console_customer(request: Request, cid: int):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if billing.customer(admin, cid) is None:
            return back(t("Cliente non trovato."), "clienti", error=True)
        return page(request, s, view="cliente", customer_id=cid)

    @app.get("/admin/payments", response_class=HTMLResponse)
    def console_payments(request: Request):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, s, view="pagamenti")

    @app.get("/admin/payments.csv")
    def console_payments_csv(request: Request):
        """Every payment for the accountant: semicolons and decimal commas,
        as a spreadsheet in Italian opens them, with a BOM for the accents."""
        import csv
        import io
        if console_user(request) is None:
            raise HTTPException(403, "operator account required")
        kinds = {"first": t("primo pagamento"), "recurring": t("rinnovo"), "manual": t("bonifico"),
                 "refund": t("rimborso"), "credits": t("crediti")}
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow([t("Data"), t("Redazione"), t("Id redazione"), t("Cliente"), t("P.IVA"), t("Codice fiscale"),
                    t("Tipo"), t("Importo"), t("Valuta"), t("Stato"), t("Metodo"), t("Id pagamento"), t("Nota")])
        for x in billing.all_payments(admin, 100000):
            when = x["paid_at"] or x["created_at"]
            cents = -x["amount_cents"] if x["kind"] == "refund" else x["amount_cents"]
            w.writerow([when.strftime("%Y-%m-%d %H:%M") if when else "", x["newsroom"] or "", x["tenant_id"] or "",
                        x["customer_name"] or "", x["customer_vat"] or "", x["customer_tax_code"] or "",
                        kinds.get(x["kind"], x["kind"]), f"{cents / 100:.2f}".replace(".", ","), x["currency"],
                        x["status"], "Mollie" if x["provider"] == "mollie" else t("bonifico") if x["provider"] == "manual"
                        else x["provider"], x["provider_payment_id"] or "", x["note"] or ""])
        day = datetime.now(UTC).date().isoformat()
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="pagamenti-{day}.csv"'})

    def search(q: str) -> dict:
        """Newsrooms, customers and people whose name, id, email, VAT number,
        tax code or city hold the words searched."""
        q = " ".join(q.split()).lower()
        if len(q) < 2:
            return {"rooms": [], "customers": [], "people": []}
        tn, c, u, ms = m.tenants, m.customers, m.users, m.memberships

        def has(*cols):
            return or_(*(func.lower(col).contains(q, autoescape=True) for col in cols))
        with accounts._read(admin) as conn:
            rooms = conn.execute(select(tn.c.tenant_id, tn.c.name, tn.c.active, tn.c.hold, tn.c.customer_id)
                                 .where(tn.c.tenant_id != repo.ARCHIVE, has(tn.c.name, tn.c.tenant_id, tn.c.external_ref))
                                 .order_by(tn.c.name).limit(50)).all()
            customers = conn.execute(select(c).where(has(c.c.name, c.c.email, c.c.vat_number, c.c.tax_code, c.c.pec,
                                                         c.c.city, c.c.phone, c.c.external_ref))
                                     .order_by(c.c.name).limit(50)).all()
            people = conn.execute(select(u.c.id, u.c.email, u.c.name, u.c.operator).where(has(u.c.email, u.c.name))
                                  .order_by(u.c.email).limit(50)).all()
            where = {}
            for r in conn.execute(select(ms.c.user_id, ms.c.role, tn.c.tenant_id, tn.c.name)
                                  .join(tn, tn.c.tenant_id == ms.c.tenant_id)
                                  .where(ms.c.user_id.in_([p.id for p in people] or [0]))).all():
                where.setdefault(r.user_id, []).append(dict(r._mapping))
        return {"rooms": [dict(r._mapping) for r in rooms], "customers": [dict(r._mapping) for r in customers],
                "people": [dict(p._mapping, rooms=where.get(p.id, [])) for p in people]}

    @app.get("/admin/search", response_class=HTMLResponse)
    def console_search(request: Request, q: str = ""):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        found = search(q)
        hits = [*found["rooms"], *found["customers"]]
        if len(hits) == 1 and not found["people"]:  # one answer: straight to it
            one = hits[0]
            return RedirectResponse(f"/admin/newsrooms/{one['tenant_id']}" if "tenant_id" in one
                                    else f"/admin/customers/{one['id']}", status_code=303)
        return page(request, s, view="cerca", q=" ".join(q.split()), found=found)

    @app.get("/admin/newsrooms/{tenant_id}", response_class=HTMLResponse)
    def console_room(request: Request, tenant_id: str):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if not newsrooms.listing(admin, db, tenant_id):
            return back(t("Redazione non trovata."), error=True)
        return page(request, s, view="redazione", room=tenant_id)

    @app.get("/api/public/latest")
    def public_latest(v: str = "", id: str = "", fresh: int = 0):
        """The latest release for an installation that checks for updates;
        the check is counted (installs.py)."""
        rel = installs.latest(fresh=bool(fresh))
        if rel is None:
            raise HTTPException(503, "latest release not known")
        if v or id:
            installs.record(admin, id, v)
        return JSONResponse(rel, headers={"Cache-Control": "no-store"})

    @app.post("/admin/updates/check")
    def ui_updates_check(s=Depends(need_console)):
        where = updates.folder(ctx.updates_dir)
        if where is None:
            return back(t("Aggiornamenti dal pannello non attivi su questo server."), "aggiornamenti", error=True)
        found = updates.check(where, force=True, installed=INSTALLED, iid=updates.this_install())
        if found.get("error"):
            return back(t("GitHub non ha risposto: {error}", error=found["error"]), "aggiornamenti", error=True)
        v = found.get("version") or ""
        if updates.newer(v, INSTALLED):
            return back(t("È disponibile la versione {version}.", version=v), "aggiornamenti")
        return back(t("Nessun aggiornamento: la {version} è l'ultima.", version=INSTALLED), "aggiornamenti")

    @app.get("/admin/updates/progress")
    def ui_updates_progress(s=Depends(need_console)):
        """Where the update is, for the console's bar (read every 2 seconds)."""
        st = updates.state(updates.folder(ctx.updates_dir), INSTALLED)
        status = st["status"] or {}
        return JSONResponse({"busy": st["busy"], "pending": bool(st["pending"]), "state": status.get("state"),
                             "step": status.get("step"), "percent": updates.percent(st),
                             "version": status.get("version"), "installed": INSTALLED},
                            headers={"Cache-Control": "no-store"})

    @app.post("/admin/backups/settings")
    def ui_backups_settings(enabled: str = Form(""), frequency: str = Form("daily"), time: str = Form("03:30"),
                            weekday: str = Form("7"), dir: str = Form(""), keep: str = Form("14"),
                            owner: str = Form(""), name: str = Form("redazia"), remote: str = Form(""),
                            target: str = Form(""), port: str = Form("22"), s=Depends(need_console)):
        where = updates.folder(ctx.updates_dir)
        if where is None:
            return back(t("Backup dal pannello non attivi su questo server."), "backup", error=True)
        hour, _, minute = (time or "03:30").partition(":")
        try:
            backups.save(where, {"enabled": enabled, "frequency": frequency, "hour": hour, "minute": minute or "0",
                                 "weekday": weekday, "dir": dir, "keep": keep, "owner": owner, "name": name,
                                 "remote": remote, "target": target, "port": port}, s.email)
        except backups.Invalid as e:
            return back(t(str(e)), "backup", error=True)
        return back(t("Impostazioni dei backup salvate: il server le legge in pochi secondi."), "backup")

    @app.post("/admin/backups/now")
    def ui_backups_now(s=Depends(need_console)):
        where = updates.folder(ctx.updates_dir)
        if where is None:
            return back(t("Backup dal pannello non attivi su questo server."), "backup", error=True)
        if backups.state(where)["pending"]:
            return back(t("Un backup è già in corso."), "backup")
        backups.ask_now(where, s.email)
        return back(t("Backup chiesto: il server lo fa subito, l'elenco qui sotto si aggiorna da solo."), "backup")

    @app.post("/admin/backups/delete")
    async def ui_backups_delete(request: Request, s=Depends(need_console)):
        where = updates.folder(ctx.updates_dir)
        if where is None:
            return back(t("Backup dal pannello non attivi su questo server."), "backup", error=True)
        files = [str(x) for x in (await request.form()).getlist("file")]
        if not files:
            return back(t("Spunta almeno un archivio da eliminare."), "backup", error=True)
        try:
            names = backups.ask_delete(where, files, s.email)
        except backups.Invalid as e:
            return back(t(str(e)), "backup", error=True)
        if len(names) == 1:
            return back(t("Archivio {name} eliminato dal server.", name=names[0]), "backup")
        return back(t("{n} archivi eliminati dal server.", n=len(names)), "backup")

    @app.get("/admin/backups/state")
    def ui_backups_state(s=Depends(need_console)):
        st = backups.state(updates.folder(ctx.updates_dir))
        return JSONResponse({"pending": st["pending"], "at": (st["status"].get("result") or {}).get("at"),
                             "count": len(st["archives"])}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/updates/apply")
    def ui_updates_apply(version: str = Form(...), s=Depends(need_console)):
        where = updates.folder(ctx.updates_dir)
        if where is None:
            return back(t("Aggiornamenti dal pannello non attivi su questo server."), "aggiornamenti", error=True)
        known = updates.state(where, INSTALLED)
        # Only the release GitHub announced, and only forward.
        # A retry after a release whose images carried another version, republished since.
        again = known["mismatch"] and version == known["mismatch"]
        if version != (known["available"] or {}).get("version") or not (known["newer"] or again):
            return back(t("Versione non disponibile."), "aggiornamenti", error=True)
        if known["pending"] or (known["status"] or {}).get("state") == "running":
            return back(t("Un aggiornamento è già in corso."), "aggiornamenti")
        updates.request(where, version, s.email)
        return back(t("Aggiornamento alla {version} chiesto: il server fa il backup, scarica la versione e "
                      "riavvia; la barra mostra a che punto è. Il pannello resta irraggiungibile per qualche "
                      "secondo.", version=version),
                    "aggiornamenti")

    @app.post("/admin/newsrooms", response_class=HTMLResponse)
    def ui_create(request: Request, name: str = Form(...), owner_email: str = Form(""), plan: str = Form(""),
                  max_sites: str = Form(""), max_active_sources: str = Form(""),
                  max_llm_cost_month: str = Form(""), max_drafts_month: str = Form(""), max_languages: str = Form(""),
                  credits_month: str = Form(""), customer: str = Form(""), customer_mode: str = Form(""),
                  customer_id: str = Form(""), c_kind: str = Form("company"), c_name: str = Form(""),
                  c_email: str = Form(""), c_phone: str = Form(""), c_vat_number: str = Form(""),
                  c_tax_code: str = Form(""), c_address: str = Form(""), c_postal_code: str = Form(""),
                  c_city: str = Form(""), c_province: str = Form(""), c_country: str = Form("IT"),
                  c_sdi_code: str = Form(""), c_pec: str = Form(""), c_reseller: str = Form(""),
                  s=Depends(need_console)):
        """One step for a sale: the customer (a new one with its billing data,
        an existing one, or none for an internal newsroom), the newsroom with
        its director, the plan. Nothing is created until all of it is right;
        a mistake shows the form again with what was written."""
        spec = {"kind": c_kind, "name": c_name, "email": c_email, "phone": c_phone, "vat_number": c_vat_number,
                "tax_code": c_tax_code, "address": c_address, "postal_code": c_postal_code, "city": c_city,
                "province": c_province, "country": c_country or "IT", "sdi_code": c_sdi_code, "pec": c_pec}
        if not customer_id.strip() and customer.strip().isdigit():  # the form before 0.28.42
            customer_id = customer.strip()
        mode = customer_mode.strip() or ("existing" if customer_id.strip().isdigit()
                                         else "new" if c_name.strip() or c_email.strip() else "none")
        form = {"name": name, "owner_email": owner_email, "plan": plan, "max_sites": max_sites,
                "max_active_sources": max_active_sources,
                "max_llm_cost_month": max_llm_cost_month, "max_drafts_month": max_drafts_month,
                "max_languages": max_languages, "credits_month": credits_month, "customer_mode": mode,
                "customer_id": customer_id.strip(), "c_reseller": c_reseller.strip(),
                **{f"c_{k}": v for k, v in spec.items()}}

        def again(error: str, status: int = 400, **more):
            return page(request, s, status, view="nuova", error=error, form={**form, **more.pop("form", {})}, **more)
        try:
            limits = {"max_sites": _num(max_sites), "max_active_sources": _num(max_active_sources),
                      "max_llm_cost_month": _num(max_llm_cost_month, money=True),
                      "max_drafts_month": _num(max_drafts_month), "max_languages": _num(max_languages),
                      "credits_month": _num(credits_month)}
        except ValueError:
            return again(t("I limiti sono numeri di zero o più; vuoto vuol dire senza limite."))
        code = plan.strip().lower()
        chosen = next((p for p in billing.plans(admin) if p["code"] == code), None) if code else None
        if code and chosen is None:
            return again(t("piano sconosciuto: {code}", code=code))
        cid, email_of = None, ""
        if mode == "existing":
            found = billing.customer(admin, int(customer_id)) if customer_id.strip().isdigit() else None
            if found is None:
                return again(t("Scegli il cliente dall'elenco, oppure inseriscine uno nuovo."))
            cid, email_of = found["id"], found["email"]
        elif mode == "new":
            try:
                data = billing.clean_customer(spec)
                with accounts._read(admin) as conn:
                    twin = billing.twin_customer(conn, data)
            except (billing.BillingError, accounts.AccountError) as e:
                return again(t("Dati del cliente da correggere: {error}.", error=e))
            if twin is not None:
                # Already a customer: the form comes back with it chosen, nothing lost.
                row, message = twin
                return again(t(message, name=row.name) + " " + t("L'ho scelto in «Cliente esistente»: controlla e crea la redazione."),
                             form={"customer_mode": "existing", "customer_id": str(row.id)})
            email_of = data["email"]
        if not owner_email.strip():
            owner_email = email_of  # the director is the customer, unless said otherwise
        if not owner_email.strip():
            return again(t("Scrivi l'email del direttore, oppure scegli un cliente che ne ha una."))
        try:
            res = newsrooms.create(admin, {"name": name, "owner_email": owner_email, "limits": limits}, s.email, ctx.core)
        except (newsrooms.NewsroomError, accounts.AccountError) as e:
            return again(t("Redazione non creata: {error}.", error=e))
        tid = res["tenant_id"]
        new = {"what": t("Redazione {name} creata (id {tenant})", name=name.strip(), tenant=tid), "warning": res["warning"]}
        if res["invite"]:
            new["link"] = send(request, res["invite"], "invite", res["owner_email"], name.strip(), s.email)
        else:
            new["note"] = t("{email} ha già un account: la trova fra le sue redazioni.", email=res["owner_email"])
        if mode == "new":
            try:
                cid = billing.create_customer(admin, spec, int(c_reseller) if c_reseller.strip().isdigit() else None)
            except billing.BillingError as e:
                new["warning"] = t("Cliente non creato: {error}. Crealo dalla scheda della redazione.", error=e)
        if cid is not None:
            try:
                billing.link_customer(admin, tid, cid)
            except (billing.BillingError, LookupError) as e:
                new["warning"] = t("Cliente non collegato: {error}.", error=e)
        if chosen is not None:
            # The plan's limits and subscription; the numbers written by hand win over the plan's.
            try:
                billing.start(admin, tid, chosen["code"], cid, core=ctx.core)
                if own := {k: v for k, v in limits.items() if v is not None}:
                    newsrooms.update_newsroom(admin, tid, {"limits": own}, ctx.core)
            except (ValueError, LookupError) as e:
                new["warning"] = t("Piano non applicato: {error}.", error=e)
        return page(request, s, view="redazione", room=tid, new=new)

    @app.post("/admin/newsrooms/{tenant_id}")
    def ui_update(tenant_id: str, name: str = Form(""), plan: str = Form(""), max_sites: str = Form(""),
                  max_active_sources: str = Form(""),
                  max_llm_cost_month: str = Form(""), max_drafts_month: str = Form(""), max_languages: str = Form(""),
                  credits_month: str = Form(""), max_users: str = Form(""), s=Depends(need_console)):
        try:
            limits = {"max_sites": _num(max_sites), "max_active_sources": _num(max_active_sources),
                      "max_llm_cost_month": _num(max_llm_cost_month, money=True),
                      "max_drafts_month": _num(max_drafts_month), "max_languages": _num(max_languages),
                      "credits_month": _num(credits_month), "max_users": _num(max_users)}
            res = newsrooms.update_newsroom(admin, tenant_id, {"limits": limits} | (
                {"plan": plan.strip()[:60]} if plan.strip() else {}) | (
                {"name": name.strip()} if name.strip() else {}), ctx.core)
        except ValueError:
            return back(t("I limiti sono numeri di zero o più; vuoto vuol dire senza limite."), f"r-{tenant_id}--edit", error=True)
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        return back(t("Redazione {tenant} aggiornata.", tenant=tenant_id)
                    + (" " + t("Attenzione: {warning}", warning=res["warning"]) if res["warning"] else ""),
                    f"r-{tenant_id}--edit")

    @app.post("/admin/newsrooms/{tenant_id}/active")
    def ui_active(tenant_id: str, active: str = Form(...), s=Depends(need_console)):
        on = active == "1"
        try:
            newsrooms.update_newsroom(admin, tenant_id, {"active": on})
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        return back(t("Redazione {tenant} riattivata.", tenant=tenant_id) if on else
                    t("Redazione {tenant} sospesa: le persone non entrano e il lavoro programmato si ferma, i dati restano.",
                      tenant=tenant_id), f"r-{tenant_id}")

    @app.post("/admin/newsrooms/{tenant_id}/invite", response_class=HTMLResponse)
    def ui_invite(request: Request, tenant_id: str, email: str = Form(...), role: str = Form("owner"),
                  s=Depends(need_console)):
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            return back(t("Redazione non trovata."), error=True)
        try:
            token = accounts.create_invite(admin, email, s.email, tenant_id=tenant_id, role=role)
        except accounts.AccountError as e:
            return page(request, s, 400, view="redazione", room=tenant_id, error=t("Invito non creato: {error}.", error=e))
        return page(request, s, view="redazione", room=tenant_id,
                    new={"what": t("Invito a {newsroom} per {email}", newsroom=rows[0]["name"],
                                                       email=email.strip().lower()),
                                     "link": send(request, token, "invite", email.strip().lower(), rows[0]["name"], s.email)})

    @app.post("/admin/newsrooms/{tenant_id}/people/{uid}/role")
    def ui_person_role(tenant_id: str, uid: int, role: str = Form(...), s=Depends(need_console)):
        try:
            accounts.change_role(admin, tenant_id, uid, role)
        except accounts.AccountError as e:
            return back(t("Ruolo non cambiato: {e}.", e=e), f"r-{tenant_id}", error=True)
        return back(t("Ruolo aggiornato."), f"r-{tenant_id}")

    @app.post("/admin/newsrooms/{tenant_id}/people/{uid}/remove")
    def ui_person_remove(tenant_id: str, uid: int, s=Depends(need_console)):
        try:
            accounts.remove_member(admin, tenant_id, uid)
        except accounts.AccountError as e:
            return back(t("Persona non rimossa: {e}.", e=e), f"r-{tenant_id}", error=True)
        return back(t("Persona rimossa dalla redazione: non entra più."), f"r-{tenant_id}")

    def resend(request: Request, s, tenant_id: str, email: str, role: str):
        """A new invite for someone who has not come in yet: the links sent
        before, perhaps expired, stop working."""
        rows = newsrooms.listing(admin, db, tenant_id)
        if not rows:
            return back(t("Redazione non trovata."), error=True)
        old = [i.token_sha256 for i in accounts.pending_invites(admin, tenant_id) if i.email == email]
        token = accounts.create_invite(admin, email, s.email, tenant_id=tenant_id, role=role)
        for sha in old:
            accounts.revoke_invite(admin, tenant_id, sha)
        return page(request, s, view="redazione", room=tenant_id,
                    new={"what": t("Nuovo invito a {newsroom} per {email}", newsroom=rows[0]["name"], email=email),
                         "link": send(request, token, "invite", email, rows[0]["name"], s.email)})

    @app.post("/admin/newsrooms/{tenant_id}/people/{uid}/resend", response_class=HTMLResponse)
    def ui_person_resend(request: Request, tenant_id: str, uid: int, s=Depends(need_console)):
        who = next((x for x in accounts.members(admin, tenant_id) if x.id == uid), None)
        if who is None:
            return back(t("Persona non trovata in questa redazione."), f"r-{tenant_id}", error=True)
        if who.active:
            return back(t("{email} è già entrato: per una nuova password usa Amministratori › Chi non riesce a entrare.",
                          email=who.email), f"r-{tenant_id}", error=True)
        return resend(request, s, tenant_id, who.email, who.role)

    @app.post("/admin/newsrooms/{tenant_id}/invites/{sha}/resend", response_class=HTMLResponse)
    def ui_invite_resend(request: Request, tenant_id: str, sha: str, s=Depends(need_console)):
        i = next((x for x in accounts.pending_invites(admin, tenant_id) if x.token_sha256 == sha), None)
        if i is None:
            return back(t("Invito già usato o scaduto."), f"r-{tenant_id}", error=True)
        return resend(request, s, tenant_id, i.email, i.role or "editor")

    @app.post("/admin/newsrooms/{tenant_id}/invites/{sha}/revoke")
    def ui_invite_revoke(tenant_id: str, sha: str, s=Depends(need_console)):
        n = accounts.revoke_invite(admin, tenant_id, sha)
        return back(t("Invito annullato.") if n else t("Invito già usato o scaduto."), f"r-{tenant_id}", error=not n)

    @app.post("/admin/newsrooms/{tenant_id}/delete")
    def ui_delete(tenant_id: str, confirm: str = Form(""), s=Depends(need_console)):
        if confirm.strip() != tenant_id:
            return back(t("Per cancellare scrivi l'id della redazione ({tenant}): niente è stato cancellato.", tenant=tenant_id),
                        f"r-{tenant_id}--delete", error=True)
        try:
            newsrooms.delete(admin, tenant_id)
        except LookupError:
            return back(t("Redazione non trovata."), error=True)
        return back(t("Redazione {tenant} cancellata con tutti i suoi dati.", tenant=tenant_id))

    @app.get("/admin/enter/{tenant_id}")
    def ui_enter(request: Request, tenant_id: str):
        s = console_user(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if not newsrooms.listing(admin, db, tenant_id):
            return back(t("Redazione non trovata."), error=True)
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
            return page(request, s, 400, view="operatori", error=t("Invito non creato: {error}.", error=e))
        return page(request, s, view="operatori", new={"what": t("Invito come amministratore per {email}", email=email.strip().lower()),
                                     "link": send(request, token, "invite", email.strip().lower(), None, s.email)})

    @app.post("/admin/operators/{uid}/remove")
    def ui_remove_operator(uid: int, s=Depends(need_console)):
        try:
            email = accounts.remove_operator(admin, uid, s.user_id)
        except LookupError:
            return back(t("Amministratore non trovato."), "operatori", error=True)
        except accounts.AccountError as e:
            return back(t("Amministratore non tolto: {e}.", e=e), "operatori", error=True)
        return back(t("{email} non è più amministratore: le sue redazioni restano.", email=email), "operatori")

    @app.post("/admin/invites/{sha}/revoke")
    def ui_revoke_operator_invite(sha: str, s=Depends(need_console)):
        return back(t("Invito annullato.") if accounts.revoke_invite(admin, None, sha)
                    else t("Invito già usato o scaduto."), "operatori")

    @app.post("/admin/reset", response_class=HTMLResponse)
    def ui_reset(request: Request, email: str = Form(...), s=Depends(need_console)):
        try:
            e = accounts.normalise_email(email)
        except accounts.AccountError as err:
            return page(request, s, 400, view="operatori", error=f"{err}.")
        with accounts._read(admin) as conn:
            if accounts.find_user(conn, e) is None:
                return page(request, s, 400, view="operatori", error=t("Nessun account per {email}.", email=e))
        token = accounts.create_invite(admin, e, s.email, kind="reset")
        return page(request, s, view="operatori", new={"what": t("Link per reimpostare la password di {email}", email=e),
                                     "link": send(request, token, "reset", e, None, s.email)})

    @app.post("/admin/keys", response_class=HTMLResponse)
    def ui_new_key(request: Request, name: str = Form(...), reseller_id: str = Form(""), s=Depends(need_console)):
        try:
            seller = int(reseller_id) if reseller_id.strip().isdigit() else None
            if reseller_id.strip() and (seller is None or billing.reseller(admin, seller) is None):
                raise accounts.AccountError(t("rivenditore non trovato"))
            key = accounts.create_operator_token(admin, name, reseller_id=seller)
        except accounts.AccountError as e:
            return page(request, s, 400, view="api", error=f"{e}.")
        return page(request, s, view="api", new={"what": t("Chiave API «{name}» creata", name=name.strip()), "key": key})

    @app.post("/admin/keys/{key_id}/revoke")
    def ui_revoke_key(key_id: int, s=Depends(need_console)):
        return back(t("Chiave revocata.") if accounts.revoke_operator_token(admin, key_id) else t("Chiave già revocata."),
                    "api")

    commerce.register(app, ctx, operator=operator, own=own, no_reseller=no_reseller, send=send,
                      need_console=need_console, page=page, back=back, failed=failed)


def set_session(request: Request, resp, token: str) -> None:
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                    max_age=accounts.SESSION_DAYS * 86400)


def jsonable(value):
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(value)
