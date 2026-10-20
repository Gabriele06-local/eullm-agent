"""Dashboard pages of Editor Mode.

Server-rendered over the same functions as the JSON API, so tenancy, roles
and checks are the same: every page reads inside the caller's tenant (row
level security applies) and every form posts to a route that requires the
role the action needs. Forms answer with a redirect and a one-shot notice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.exc import IntegrityError

from . import accounts, auth, drafts, jobs, models as m, profile as prof, proposals as props, publishing, quotas, tenant_data, vault

COOKIE = "editor_token"
SITE_COOKIE = "editor_site"
NOTICE_COOKIE = "editor_notice"

ORIGINS = {
    "manual": "Aggiunta a mano",
    "site_outbound": "Link dal sito",
    "site_feed": "Feed del sito",
    "autodiscovery": "Feed scoperto",
    "api_query": "Ricerca dal profilo",
    "model_suggestion": "Suggerita dal modello",
}
KINDS = {"rss": "RSS", "hackernews": "HN", "github": "GitHub", "huggingface": "Hugging Face", "arxiv": "arXiv"}
TARGET_KINDS = {"wordpress": "WordPress", "webhook": "Webhook", "telegram_channel": "Canale Telegram"}
QUOTA_LABELS = {
    "sites": "Siti",
    "sources": "Fonti attive",
    "items": "Notizie oggi",
    "drafts": "Bozze nel mese",
    "llm_cost": "Costo modello nel mese",
}
CHECKS = [
    ("no valid source", "Ogni frase ha almeno una fonte"),
    ("numbers not found", "Ogni numero compare nelle fonti citate"),
    ("copies a passage", "Nessun passaggio copiato parola per parola"),
]


MONTHS = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
DAYS = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


class LoginRequired(Exception):
    pass


def safe_path(value: str | None, default: str = "/") -> str:
    """A local path to go back to; anything else becomes the default."""
    if (value and value.startswith("/") and not value.startswith("//") and "\\" not in value
            and "\n" not in value and "\r" not in value):
        return value
    return default


def done(path: str, notice: str | None = None) -> RedirectResponse:
    resp = RedirectResponse(path, status_code=303)
    if notice:
        resp.set_cookie(NOTICE_COOKIE, quote(notice), max_age=60, httponly=True, samesite="strict")
    return resp


def number(value, digits: int = 2) -> str:
    if value is None:
        return "–"
    return f"{value:.{digits}f}".replace(".", ",")


def plural(n, one: str, many: str) -> str:
    n = n or 0
    return f"{n} {one if n == 1 else many}"


def score100(value) -> str:
    return "–" if value is None else str(round(value))


def export_response(db, tenant_id: str) -> Response:
    day = datetime.now(UTC).date().isoformat()
    return Response(tenant_data.export_json(db, tenant_id), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{tenant_id}-{day}.json"'})


def register(app, ctx) -> None:
    """Add the dashboard routes. `ctx` carries db, pages, need, and the
    functions that queue background work (run_analysis, run_discovery,
    run_draft, run_publication) and the core."""
    db, pages = ctx.db, ctx.pages
    admin = getattr(ctx, "admin", None) or db
    env = pages.env
    env.filters["num"] = number
    env.filters["s100"] = score100
    env.filters["plural"] = plural

    def viewer(request: Request) -> auth.Caller:
        c = auth.resolve(db, request.cookies.get(COOKIE), admin)
        if c is None:
            raise LoginRequired()
        return c

    @app.exception_handler(LoginRequired)
    def to_login(request: Request, exc: LoginRequired):
        # An operator signed in outside any newsroom works from the console.
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        return RedirectResponse("/admin" if s is not None and s.operator else "/login", status_code=303)

    def zone(s) -> ZoneInfo:
        tz = s.execute(select(m.tenants.c.timezone)).scalar()
        try:
            return ZoneInfo(tz or "Europe/Rome")
        except Exception:
            return ZoneInfo("Europe/Rome")

    def current_site(s, request: Request):
        sites = s.execute(select(m.sites).order_by(m.sites.c.domain)).all()
        wanted = request.cookies.get(SITE_COOKIE)
        chosen = next((x for x in sites if str(x.id) == wanted), sites[0] if sites else None)
        return sites, chosen

    def render(request: Request, name: str, c: auth.Caller, s, active: str, status_code: int = 200, **data):
        sites, site = data.pop("_sites", None) or current_site(s, request)
        tz = zone(s)

        def when(value, part: str = "full") -> str:
            if value is None:
                return "–"
            if isinstance(value, datetime):
                value = value.astimezone(tz)
                if part == "time":
                    return value.strftime("%H:%M")
            day = f"{value.day} {MONTHS[value.month - 1]}"
            return f"{day} {value:%H:%M}" if part == "full" and isinstance(value, datetime) else day

        notice = request.cookies.get(NOTICE_COOKIE)
        rooms = [x for x in accounts.memberships(admin, c.user_id) if x.active] if c.user_id else []
        resp = pages.TemplateResponse(request, name, {
            "caller": c, "sites": sites, "site": site, "active": active, "newsrooms": rooms,
            "notice": unquote(notice) if notice else None, "now": datetime.now(tz), "when": when, "days": DAYS, "months": MONTHS, **data,
        }, status_code=status_code)
        if notice:
            resp.delete_cookie(NOTICE_COOKIE)
        return resp

    # --- session --------------------------------------------------------------

    def login_form(request: Request, error: str | None = None, status_code: int = 200, email: str = ""):
        return pages.TemplateResponse(request, "login.html.j2", {"error": error, "email": email,
                                                                 "can_reset": ctx_mail() is not None,
                                                                 "setup": accounts.needs_setup(admin)},
                                      status_code=status_code)

    def ctx_mail():
        return getattr(ctx, "mail", None)

    def signed_in(request: Request, token: str, to: str = "/") -> RedirectResponse:
        accounts.end_session(admin, request.cookies.get(COOKIE))
        resp = RedirectResponse(to, status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        max_age=30 * 86400)
        resp.delete_cookie(SITE_COOKIE)
        return resp

    def session_for(request: Request, s: accounts.Session) -> RedirectResponse:
        """Sign in to the newsroom the session names, or the first one the
        person belongs to; an operator goes to the administration (/admin),
        from where /editor or "Entra" opens a newsroom."""
        tenant = s.tenant_id
        if tenant is None and not s.operator:
            tenant = next((x.tenant_id for x in accounts.memberships(admin, s.user_id) if x.active), None)
        if tenant is None and not s.operator:
            return login_form(request, "Il tuo account non è in nessuna redazione attiva: chiedi un invito.", 403)
        token = accounts.start_session(admin, s.user_id, tenant)
        return signed_in(request, token, "/" if tenant else "/admin")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return login_form(request)

    @app.get("/editor")
    def editor_home(request: Request):
        """The newsroom side: the person's newsroom (an operator's first one)."""
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        if s is None:
            return RedirectResponse("/" if auth.resolve(db, request.cookies.get(COOKIE)) else "/login", status_code=303)
        if s.tenant_id is not None:
            return RedirectResponse("/", status_code=303)
        tenant = next((x.tenant_id for x in accounts.memberships(admin, s.user_id) if x.active), None)
        if tenant is None:
            return RedirectResponse("/admin", status_code=303)
        return signed_in(request, accounts.start_session(admin, s.user_id, tenant))

    def setup_form(request: Request, token: str, error: str | None = None, status_code: int = 200):
        return pages.TemplateResponse(request, "setup.html.j2", {
            "token": token, "valid": accounts.setup_valid(admin, token), "error": error,
            "min": accounts.MIN_PASSWORD}, status_code=status_code)

    @app.get("/setup/{token}", response_class=HTMLResponse)
    def setup_page(request: Request, token: str):
        return setup_form(request, token)

    @app.post("/setup/{token}")
    def setup(request: Request, token: str, email: str = Form(...), name: str = Form(""),
              password: str = Form(...), again: str = Form(...)):
        if password != again:
            return setup_form(request, token, "Le due password non coincidono.", 400)
        try:
            s = accounts.complete_setup(admin, token, email, name, password)
        except accounts.AccountError as e:
            return setup_form(request, token, f"{str(e)[:1].upper()}{str(e)[1:]}.", 400)
        return signed_in(request, accounts.start_session(admin, s.user_id, None), "/admin")

    @app.post("/login")
    def login(request: Request, email: str = Form(""), password: str = Form(""), token: str = Form("")):
        if token.strip():
            c = auth.resolve(db, token.strip())
            if c is None or token.strip().startswith("eds_"):
                return login_form(request, "Token non valido", 401)
            return signed_in(request, token.strip())
        try:
            u = accounts.authenticate(admin, email, password)
        except accounts.AccountError as e:
            return login_form(request, f"{str(e)[:1].upper()}{str(e)[1:]}.", 429, email)
        if u is None:
            return login_form(request, "Email o password sbagliate", 401, email)
        return session_for(request, accounts.Session(u.id, u.email, u.name, bool(u.operator), None, None))

    @app.post("/logout")
    def logout(request: Request):
        accounts.end_session(admin, request.cookies.get(COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        resp.delete_cookie(SITE_COOKIE)
        return resp

    @app.get("/newsroom")
    def switch_newsroom(request: Request, t: str):
        """Move to another newsroom of the same person."""
        s = accounts.resolve_session(admin, request.cookies.get(COOKIE))
        if s is None:
            return RedirectResponse("/login", status_code=303)
        if not s.operator and t not in {x.tenant_id for x in accounts.memberships(admin, s.user_id) if x.active}:
            raise HTTPException(404, "newsroom not found")
        return signed_in(request, accounts.start_session(admin, s.user_id, t))

    def invite_form(request: Request, token: str, info, error: str | None = None, status_code: int = 200):
        return pages.TemplateResponse(request, "invite.html.j2", {"info": info, "token": token, "error": error,
                                                                  "min": accounts.MIN_PASSWORD},
                                      status_code=status_code)

    @app.get("/invite/{token}", response_class=HTMLResponse)
    def invite_page(request: Request, token: str):
        return invite_form(request, token, accounts.invite(admin, token), status_code=200)

    @app.post("/invite/{token}")
    def invite_accept(request: Request, token: str, password: str = Form(...), again: str = Form(""),
                      name: str = Form("")):
        info = accounts.invite(admin, token)
        if info is None:
            return invite_form(request, token, None, status_code=410)
        if not (info["kind"] == "invite" and info["has_password"]) and password != again:
            return invite_form(request, token, info, "Le due password non coincidono.", 400)
        try:
            s = accounts.accept(admin, token, password, name)
        except accounts.AccountError as e:
            return invite_form(request, token, info, f"{str(e)[:1].upper()}{str(e)[1:]}.", 400)
        return session_for(request, s)

    @app.get("/forgot", response_class=HTMLResponse)
    def forgot_page(request: Request):
        return pages.TemplateResponse(request, "forgot.html.j2", {"sent": False, "can_reset": ctx_mail() is not None})

    @app.post("/forgot", response_class=HTMLResponse)
    def forgot(request: Request, email: str = Form(...)):
        """The same answer whether the account exists or not."""
        mail = ctx_mail()
        if mail is not None:
            try:
                e = accounts.normalise_email(email)
                with accounts._read(admin) as conn:
                    u = accounts.find_user(conn, e)
                if u is not None and u.disabled_at is None:
                    token = accounts.create_invite(admin, e, "password dimenticata", kind="reset")
                    base = (getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/")
                    mail(e, *accounts.link_text("reset", f"{base}/invite/{token}", None, ""))
            except Exception:
                pass
        return pages.TemplateResponse(request, "forgot.html.j2", {"sent": True, "can_reset": mail is not None})

    @app.get("/switch")
    def switch(request: Request, site: int, back: str | None = None, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site)).scalar() is None:
                raise HTTPException(404, "site not found")
        resp = RedirectResponse(safe_path(back), status_code=303)
        resp.set_cookie(SITE_COOKIE, str(site), httponly=True, samesite="strict", max_age=365 * 86400)
        return resp

    # --- queries shared by pages -------------------------------------------------

    def topic_scores(s, site_id: int | None, limit: int = 100):
        return s.execute(text("""
            SELECT t.id, t.label,
                   (SELECT score FROM editor.trend_scores ts WHERE ts.topic_id = t.id ORDER BY computed_at DESC LIMIT 1) AS hype,
                   (SELECT score FROM editor.opportunity_scores o WHERE o.topic_id = t.id AND o.site_id = :site
                     ORDER BY computed_at DESC LIMIT 1) AS opportunity,
                   (SELECT count(*) FROM editor.topic_items ti WHERE ti.topic_id = t.id) AS items
            FROM editor.topics t WHERE t.updated_at > :since
            ORDER BY opportunity DESC NULLS LAST, hype DESC NULLS LAST LIMIT :lim""").bindparams(
            bindparam("since", type_=m.topics.c.updated_at.type)),
            {"site": site_id, "lim": limit, "since": datetime.now(UTC) - timedelta(days=7)}).all()

    def proposal_rows(s, site_id: int | None, statuses: tuple[str, ...], limit: int = 30):
        p = m.proposals
        hype = (select(m.trend_scores.c.score).where(m.trend_scores.c.topic_id == p.c.topic_id)
                .order_by(m.trend_scores.c.computed_at.desc()).limit(1).scalar_subquery())
        cites = (select(func.count()).select_from(m.proposal_citations)
                 .where(m.proposal_citations.c.proposal_id == p.c.id).scalar_subquery())
        draft = (select(func.max(m.drafts.c.id)).where(m.drafts.c.proposal_id == p.c.id).scalar_subquery())
        q = (select(p, m.sites.c.domain, hype.label("hype"), cites.label("citations"), draft.label("draft_id"))
             .join(m.sites, m.sites.c.id == p.c.site_id).where(p.c.status.in_(statuses))
             .order_by(p.c.created_at.desc()).limit(limit))
        if site_id is not None:
            q = q.where(p.c.site_id == site_id)
        return s.execute(q).all()

    def start_of_day(s) -> datetime:
        tz = zone(s)
        return datetime.combine(datetime.now(tz).date(), time(), tz)

    # --- Oggi -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            sid = site.id if site else None
            p = m.editorial_profiles
            profile_drafts = s.execute(
                select(p.c.site_id, p.c.version, p.c.origin, p.c.changes, m.sites.c.domain)
                .join(m.sites, m.sites.c.id == p.c.site_id).where(p.c.status == "draft")
                .order_by(p.c.created_at.desc())).all()
            proposed = proposal_rows(s, sid, ("proposed",), limit=6)
            pending_pubs = s.execute(
                select(m.publications.c.id, m.publications.c.mode, m.publications.c.requested_by,
                       m.drafts.c.id.label("draft_id"), m.drafts.c.title, m.publish_targets.c.name.label("target"),
                       m.publish_targets.c.kind)
                .join(m.drafts, m.drafts.c.id == m.publications.c.draft_id)
                .join(m.publish_targets, m.publish_targets.c.id == m.publications.c.target_id)
                .where(m.publications.c.status == "pending_approval").order_by(m.publications.c.id)).all()
            stopped = s.execute(
                select(m.sources.c.id, m.sources.c.name, m.sources.c.status_reason)
                .where(m.sources.c.status == "suspended",
                       m.sources.c.status_changed_at > datetime.now(UTC) - timedelta(days=7))).all()
            draft_counts = dict(s.execute(
                select(m.drafts.c.status, func.count()).where(m.drafts.c.status.in_(("draft", "needs_review")))
                .group_by(m.drafts.c.status)).all())
            new_today = s.execute(select(func.count()).select_from(m.proposals).where(
                m.proposals.c.status == "proposed", m.proposals.c.created_at >= start_of_day(s))).scalar()
            usage = quotas.usage(s)
            tops = [t for t in topic_scores(s, sid, 20) if t.opportunity is not None][:4] if sid else []
            last_fetch = s.execute(select(func.max(m.sources.c.last_fetched_at))).scalar()
            briefing = s.execute(select(m.briefings).where(
                m.briefings.c.briefing_date == datetime.now(zone(s)).date())).first()
            decisions = len(profile_drafts) + len(proposed) + len(pending_pubs) + len(stopped)
            return render(request, "today.html.j2", c, s, "today", _sites=(sites, site),
                          profile_drafts=profile_drafts, proposed=proposed, pending_pubs=pending_pubs,
                          stopped=stopped, drafts_ready=sum(draft_counts.values()),
                          needs_review=draft_counts.get("needs_review", 0), new_today=new_today,
                          usage=usage, tops=tops, last_fetch=last_fetch, briefing=briefing, decisions=decisions)

    # --- Sito e profilo -------------------------------------------------------------

    def site_page(request, c, s, sites, site, editing: bool = False, status_code: int = 200, error: str | None = None):
        p = m.editorial_profiles
        profiles = s.execute(select(p).where(p.c.site_id == site.id).order_by(p.c.version.desc())).all() if site else []
        approved = next((x for x in profiles if x.status == "approved"), None)
        draft = next((x for x in profiles if x.status == "draft"), None)
        analysis = s.execute(select(m.site_analyses).where(m.site_analyses.c.site_id == site.id)
                             .order_by(m.site_analyses.c.id.desc()).limit(1)).first() if site else None
        shown = draft or approved
        rows = []
        if shown is not None:
            before = {x["name"]: x.get("share") for x in (approved.body.get("subtopics", []) if approved and draft else [])}
            names = [x["name"] for x in shown.body.get("subtopics", [])]
            names += [n for n in before if n not in names]
            now_by = {x["name"]: x for x in shown.body.get("subtopics", [])}
            for n in names:
                new = now_by.get(n, {}).get("share")
                old = before.get(n)
                rows.append({"name": n, "old": old, "new": new or 0.0,
                             "delta": None if old is None or new is None else round((new - old) * 100),
                             "fresh": bool(draft and approved and old is None)})
        evidence = []
        if shown is not None:
            for key in ("sector", "audience"):
                evidence += (shown.body.get(key) or {}).get("evidence", [])
            for x in shown.body.get("subtopics", []):
                evidence += [(e, x["name"]) if isinstance(e, str) else e for e in x.get("evidence", [])]
            evidence += (shown.body.get("style") or {}).get("evidence", [])
        seen, ev = set(), []
        for e in evidence:
            url, label = e if isinstance(e, tuple) else (e, None)
            if url not in seen:
                seen.add(url)
                ev.append({"url": url, "label": label})
        return render(request, "site.html.j2", c, s, "site", status_code=status_code, _sites=(sites, site),
                      profiles=profiles, approved=approved, draft=draft, shown=shown, analysis=analysis,
                      rows=rows, evidence=ev, editing=editing, error=error,
                      jobs=[j for j in jobs.recent(s, 10) if j["status"] != "done"][:5],
                      body_json=json.dumps(shown.body, ensure_ascii=False, indent=2) if shown is not None else "")

    @app.get("/site", response_class=HTMLResponse)
    def site_view(request: Request, edit: int = 0, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            return site_page(request, c, s, sites, site, editing=bool(edit) and c.can("owner"))

    @app.post("/ui/sites")
    def ui_add_site(request: Request, domain: str = Form(...),
                    c: auth.Caller = Depends(ctx.need("owner"))):
        from .api import normalise_domain
        d = normalise_domain(domain)
        ctx.run_analysis(c.tenant_id, d)
        return done("/site", f"Analisi di {d} in coda: il sito compare qui quando il lavoro finisce, poi il profilo arriva in bozza da approvare.")

    @app.post("/ui/sites/{site_id}/review")
    def ui_review(site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise HTTPException(404, "site not found")
        ctx.run_analysis(c.tenant_id, domain)
        return done("/site", f"Rianalisi di {domain} avviata.")

    @app.post("/ui/sites/{site_id}/approve/{version}")
    def ui_approve(site_id: int, version: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if not prof.approve(s, site_id, version, c.name):
                raise HTTPException(404, "version not found")
        ctx.run_discovery(c.tenant_id, site_id)
        return done("/site", f"Versione {version} approvata: è la linea del sito da ora. Cerco le fonti adatte.")

    @app.post("/ui/sites/{site_id}/discard/{version}")
    def ui_discard(site_id: int, version: int, c: auth.Caller = Depends(ctx.need("owner"))):
        p = m.editorial_profiles
        with db.tenant(c.tenant_id) as s:
            n = s.execute(update(p).where(p.c.site_id == site_id, p.c.version == version, p.c.status == "draft")
                          .values(status="retired")).rowcount
        if not n:
            raise HTTPException(409, "not a draft version")
        return done("/site", f"Proposta v{version} scartata: la linea resta quella approvata.")

    @app.post("/ui/sites/{site_id}/profile")
    def ui_edit_profile(request: Request, site_id: int, body: str = Form(...),
                        c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            site = next((x for x in sites if x.id == site_id), None)
            if site is None:
                raise HTTPException(404, "site not found")
            try:
                value = prof.validate_edit(json.loads(body))
            except ValueError as e:
                return site_page(request, c, s, sites, site, editing=True, status_code=400,
                                 error=f"Profilo non valido: {e}")
            value["domain"] = site.domain
            pid = prof.edit(s, c.tenant_id, site_id, value)
            version = s.execute(select(m.editorial_profiles.c.version).where(m.editorial_profiles.c.id == pid)).scalar()
        return done("/site", f"Modifiche salvate come bozza v{version}: approvala per renderla attiva.")

    # --- Fonti --------------------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    def sources_view(request: Request, status: str = "active", c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            rows, counts = [], {}
            if site:
                src = m.sources
                counts = dict(s.execute(select(src.c.status, func.count()).where(src.c.site_id == site.id)
                                        .group_by(src.c.status)).all())
                q = select(src).where(src.c.site_id == site.id).order_by(src.c.score.desc().nulls_last())
                if status != "all":
                    q = q.where(src.c.status == status)
                rows = s.execute(q.limit(500)).all()
            return render(request, "sources.html.j2", c, s, "sources", _sites=(sites, site), rows=rows,
                          counts=counts, total=sum(counts.values()), status=status, origins=ORIGINS, kinds=KINDS,
                          usage=quotas.usage(s))

    @app.post("/ui/sources/{source_id}/status")
    def ui_source_status(source_id: int, status: str = Form(...), back: str = Form("/sources"),
                         c: auth.Caller = Depends(ctx.need("owner"))):
        from . import repo
        if status not in ("active", "suspended", "rejected"):
            raise HTTPException(400, "unknown status")
        with db.tenant(c.tenant_id) as s:
            name = s.execute(select(m.sources.c.name).where(m.sources.c.id == source_id)).scalar()
            if name is None:
                raise HTTPException(404, "source not found")
            if status == "active":
                quotas.check(s, "sources")
            repo.set_source_status(s, source_id, status, f"set by {c.name}", by=c.name)
        label = {"active": "riattivata", "suspended": "sospesa", "rejected": "scartata"}[status]
        return done(safe_path(back, "/sources"), f"{name} {label}.")

    @app.post("/ui/sites/{site_id}/discover")
    def ui_discover(site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
        ctx.run_discovery(c.tenant_id, site_id)
        return done("/sources?status=candidate", "Ricerca di nuove fonti avviata: le trovate entrano come candidate.")

    # --- Trend e piano --------------------------------------------------------------

    @app.get("/trends", response_class=HTMLResponse)
    def trends_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            topics = topic_scores(s, site.id if site else None, 60) if site else []
            dots = [t for t in topics if t.hype is not None and t.opportunity is not None][:14]
            plan = proposal_rows(s, site.id, ("proposed", "accepted", "drafted"), 20) if site else []
            # Written drafts stay in the plan for a week, then live under Bozze.
            week = datetime.now(UTC) - timedelta(days=7)
            plan = [p for p in plan if p.status != "drafted" or p.created_at > week]
            hour = s.execute(select(m.tenants.c.briefing_hour)).scalar()
            return render(request, "trends.html.j2", c, s, "trends", _sites=(sites, site), topics=topics,
                          dots=dots, plan=plan, briefing_hour=hour, can_draft=ctx.core is not None)

    @app.post("/ui/proposals/{proposal_id}")
    def ui_decide(proposal_id: int, accept: str = Form(...), back: str = Form("/trends"),
                  c: auth.Caller = Depends(ctx.need("editor"))):
        if not props.decide(db, c.tenant_id, proposal_id, accept == "1", c.name):
            return done(safe_path(back, "/trends"), "Questa proposta era già stata decisa.")
        if accept == "1" and ctx.core is not None:
            ctx.run_draft(c.tenant_id, proposal_id)
            return done(safe_path(back, "/trends"), "Proposta accettata: la bozza è in scrittura.")
        return done(safe_path(back, "/trends"), "Proposta accettata." if accept == "1" else "Proposta scartata.")

    @app.post("/ui/proposals/{proposal_id}/draft")
    def ui_draft(proposal_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        if ctx.core is None:
            return done("/trends", "Nessun Core configurato: per scrivere le bozze serve un modello.")
        with db.tenant(c.tenant_id) as s:
            st = s.execute(select(m.proposals.c.status).where(m.proposals.c.id == proposal_id)).scalar()
        if st not in ("accepted", "drafted"):
            return done("/trends", "Accetta prima la proposta.")
        ctx.run_draft(c.tenant_id, proposal_id)
        return done("/drafts", "Bozza in scrittura: compare qui quando è pronta.")

    # --- Bozze ----------------------------------------------------------------------

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            rows = []
            if site:
                d = m.drafts
                claims = (select(func.count()).select_from(m.draft_claims)
                          .where(m.draft_claims.c.draft_id == d.c.id).scalar_subquery())
                rows = s.execute(select(d.c.id, d.c.title, d.c.status, d.c.version, d.c.created_at, d.c.flags,
                                        claims.label("claims")).where(d.c.site_id == site.id)
                                 .order_by(d.c.created_at.desc()).limit(100)).all()
            return render(request, "drafts.html.j2", c, s, "drafts", _sites=(sites, site), rows=rows)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_view(request: Request, draft_id: int, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
            if d is None:
                raise HTTPException(404, "draft not found")
            claims = drafts.stored_claims(s, draft_id)
            order: list[int] = []
            for cl in claims:
                order += [i for i in cl["sources"] if i not in order]
            items = {r.id: r for r in s.execute(
                select(m.source_items.c.id, m.source_items.c.title, m.source_items.c.url,
                       m.source_items.c.published_at, m.sources.c.name.label("source"))
                .join(m.sources, m.sources.c.id == m.source_items.c.source_id)
                .where(m.source_items.c.id.in_(order or [0]))).all()}
            refs = {item_id: n + 1 for n, item_id in enumerate(order)}
            flagged = {f.get("claim") for f in d.flags or []}
            sections, current = [], None
            for cl in claims:
                if current is None or cl["section"] != current["title"]:
                    current = {"title": cl["section"], "claims": []}
                    sections.append(current)
                current["claims"].append({"ordinal": cl["ordinal"], "text": cl["text"],
                                          "refs": [refs[i] for i in cl["sources"]],
                                          "flagged": cl["ordinal"] in flagged})
            flags = d.flags or []
            problems = " ".join(p for f in flags for p in f.get("problems", []))
            checks = [{"label": label, "ok": key not in problems} for key, label in CHECKS]
            targets = s.execute(select(m.publish_targets).where(m.publish_targets.c.site_id == d.site_id,
                                                                m.publish_targets.c.enabled.is_not(False))).all()
            pubs = s.execute(select(m.publications.c.status, m.publications.c.mode, m.publications.c.external_url,
                                    m.publish_targets.c.name)
                             .join(m.publish_targets, m.publish_targets.c.id == m.publications.c.target_id)
                             .where(m.publications.c.draft_id == draft_id).order_by(m.publications.c.id)).all()
            sites, site = current_site(s, request)
            return render(request, "draft.html.j2", c, s, "drafts", _sites=(sites, site), d=d, sections=sections,
                          sources=[{"n": refs[i], **items[i]._mapping} for i in order if i in items],
                          flags=flags, checks=checks, targets=targets, pubs=pubs, kinds=TARGET_KINDS)

    @app.post("/ui/drafts/{draft_id}/decision")
    def ui_draft_decision(draft_id: int, accept: str = Form(...), c: auth.Caller = Depends(ctx.need("editor"))):
        if not drafts.decide(db, c.tenant_id, draft_id, accept == "1", c.name):
            return done(f"/drafts/{draft_id}", "Questa bozza era già stata decisa.")
        return done(f"/drafts/{draft_id}", "Bozza approvata: ora puoi chiederne la pubblicazione."
                    if accept == "1" else "Bozza rifiutata.")

    @app.post("/ui/drafts/{draft_id}/publish")
    def ui_publish(draft_id: int, target_id: int = Form(...), mode: str = Form("draft"),
                   c: auth.Caller = Depends(ctx.need("editor"))):
        if mode not in ("draft", "publish"):
            raise HTTPException(400, "unknown mode")
        try:
            publishing.request(db, c.tenant_id, draft_id, target_id, mode, c.name)
        except publishing.PublishError as e:
            return done(f"/drafts/{draft_id}", f"Richiesta non inviata: {e}.")
        except IntegrityError:
            return done(f"/drafts/{draft_id}", "Questa pubblicazione è già stata chiesta.")
        return done("/publications", "Pubblicazione chiesta: aspetta l'approvazione di un owner.")

    # --- Pubblicazioni --------------------------------------------------------------

    @app.get("/publications", response_class=HTMLResponse)
    def publications_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            pub, t = m.publications, m.publish_targets
            q = (select(pub, m.drafts.c.title, t.c.name.label("target"), t.c.kind)
                 .join(m.drafts, m.drafts.c.id == pub.c.draft_id).join(t, t.c.id == pub.c.target_id)
                 .order_by(pub.c.id.desc()).limit(100))
            if site:
                q = q.where(m.drafts.c.site_id == site.id)
            rows = s.execute(q).all()
            targets = s.execute(select(t).where(t.c.site_id == site.id).order_by(t.c.id)).all() if site else []
            return render(request, "publications.html.j2", c, s, "publications", _sites=(sites, site),
                          pending=[r for r in rows if r.status == "pending_approval"],
                          history=[{**r._mapping, "uncertain": r.status == "sending" and r.decided_at is not None
                                    and r.decided_at < datetime.now(UTC) - timedelta(minutes=10)}
                                   for r in rows if r.status != "pending_approval"],
                          targets=targets, kinds=TARGET_KINDS, stored=vault.stored(s),
                          secret_var=lambda name: _secret_var(c.tenant_id, name))

    @app.post("/ui/publications/{pub_id}/decision")
    def ui_pub_decision(pub_id: int, accept: str = Form(...),
                        c: auth.Caller = Depends(ctx.need("owner"))):
        ok = accept == "1"
        if not publishing.decide(db, c.tenant_id, pub_id, ok, c.name):
            return done("/publications", "Questa pubblicazione era già stata decisa.")
        if ok:
            ctx.run_publication(c.tenant_id, pub_id)
        return done("/publications", "Approvata: l'invio parte a breve." if ok else "Pubblicazione rifiutata.")

    @app.post("/ui/publications/{pub_id}/uncertain")
    def ui_pub_uncertain(pub_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        if not publishing.mark_uncertain_failed(db, c.tenant_id, pub_id, c.name):
            return done("/publications", "Questa pubblicazione non è più in invio.")
        return done("/publications", "Segnata come non riuscita: puoi chiederla di nuovo.")

    def _secret_var(tenant_id, name):
        try:
            return publishing.secret_variable(tenant_id, name)
        except publishing.PublishError:
            return f"{name} (nome non valido: ricrea la destinazione)"

    @app.post("/ui/targets")
    def ui_add_target(site_id: int = Form(...), kind: str = Form(...), name: str = Form(...),
                      address: str = Form(""), user: str = Form(""), secret_env: str = Form(""),
                      secret: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        if kind not in TARGET_KINDS:
            raise HTTPException(400, "unknown kind")
        config = {"chat_id": address.strip()} if kind == "telegram_channel" else {"url": address.strip()}
        if kind == "wordpress":
            config["user"] = user.strip()
        env_name = secret_env.strip() or None
        if env_name is not None and (len(env_name) > 40 or not publishing.SECRET_NAME.match(env_name)):
            return done("/publications", "Il nome del segreto va scritto in MAIUSCOLO, es. WP_SITO.")
        if secret and env_name is None:
            return done("/publications", "Dai un nome alla credenziale, es. WP_SITO.")
        try:
            with db.tenant(c.tenant_id) as s:
                if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                    raise HTTPException(404, "site not found")
            if secret:
                vault.put(db, c.tenant_id, env_name, secret)
            publishing.add_target(db, c.tenant_id, site_id, kind, name.strip()[:100], config, env_name)
        except (publishing.PublishError, vault.VaultError) as e:
            return done("/publications", f"Destinazione non aggiunta: {e}.")
        added = f"Destinazione {name.strip()[:100]} aggiunta."
        if env_name and secret:
            added += " La credenziale è salvata cifrata e non verrà più mostrata."
        elif env_name:
            added += f" Sul server la credenziale va nella variabile {publishing.secret_variable(c.tenant_id, env_name)}."
        return done("/publications", added)

    @app.post("/ui/secrets/{name}")
    def ui_put_secret(name: str, value: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            vault.put(db, c.tenant_id, name, value)
        except vault.VaultError as e:
            return done("/publications", f"Credenziale non salvata: {e}.")
        return done("/publications", f"Credenziale {name} aggiornata: è salvata cifrata e non verrà più mostrata.")

    # --- Impostazioni ---------------------------------------------------------------

    def settings_page(request, c, s, new_token: str | None = None, new_link: dict | None = None,
                      error: str | None = None, status_code: int = 200):
        usage = quotas.usage(s)
        meters = []
        if usage is not None:
            for what, (limit_col, used_col) in quotas.LIMITS.items():
                used, limit = usage.used.get(used_col) or 0, usage.plan.get(limit_col)
                meters.append({"label": QUOTA_LABELS[what], "used": used, "limit": limit, "money": what == "llm_cost",
                               "pct": min(100, round(100 * used / limit)) if limit else 0})
        recipients = s.execute(select(m.recipients).order_by(m.recipients.c.id)).all()
        t = m.access_tokens
        tokens = s.execute(select(t.c.id, t.c.name, t.c.role, t.c.created_at, t.c.revoked_at)
                           .order_by(t.c.revoked_at.is_not(None), t.c.created_at)).all() if c.can("owner") else []
        owner = c.can("owner")
        return render(request, "settings.html.j2", c, s, "settings", status_code=status_code, usage=usage,
                      meters=meters, recipients=recipients, tokens=tokens, new_token=new_token, new_link=new_link,
                      error=error, people=accounts.members(admin, c.tenant_id) if owner else [],
                      invited=accounts.pending_invites(admin, c.tenant_id) if owner else [])

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            return settings_page(request, c, s)

    @app.post("/ui/recipients")
    def ui_add_recipient(channel: str = Form(...), address: str = Form(...),
                         c: auth.Caller = Depends(ctx.need("owner"))):
        address = address.strip()
        if channel not in ("email", "telegram") or not address or len(address) > 200:
            return done("/settings", "Destinatario non valido.")
        if any(ch in address for ch in "\r\n\t,;<>"):
            return done("/settings", "Destinatario non valido.")
        if channel == "email" and ("@" not in address or " " in address):
            return done("/settings", "Indirizzo email non valido.")
        with db.tenant(c.tenant_id) as s:
            s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES (:t, :c, :a) "
                           "ON CONFLICT DO NOTHING"), {"t": c.tenant_id, "c": channel, "a": address})
        return done("/settings", "Destinatario aggiunto: riceve il prossimo briefing.")

    @app.post("/ui/recipients/{rid}/delete")
    def ui_del_recipient(rid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            s.execute(m.recipients.delete().where(m.recipients.c.id == rid))
        return done("/settings", "Destinatario rimosso.")

    @app.get("/ui/export")
    def ui_export(c: auth.Caller = Depends(ctx.need("owner"))):
        return export_response(db, c.tenant_id)

    @app.post("/ui/tokens", response_class=HTMLResponse)
    def ui_new_token(request: Request, name: str = Form(...), role: str = Form(...),
                     c: auth.Caller = Depends(ctx.need("owner"))):
        name = name.strip()[:100]
        if role not in auth.ROLES or not name:
            return done("/settings", "Nome o ruolo non valido.")
        token = auth.create_token(db, c.tenant_id, name, role)
        with db.tenant(c.tenant_id) as s:
            return settings_page(request, c, s, new_token=token)

    @app.post("/ui/tokens/{tid}/revoke")
    def ui_revoke(tid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            t = m.access_tokens
            n = s.execute(update(t).where(t.c.id == tid, t.c.revoked_at.is_(None))
                          .values(revoked_at=datetime.now(UTC))).rowcount
        return done("/settings", "Accesso revocato." if n else "Accesso già revocato.")

    # --- Persone della redazione ----------------------------------------------------

    @app.post("/ui/people/invite", response_class=HTMLResponse)
    def ui_invite(request: Request, email: str = Form(...), role: str = Form("editor"),
                  c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            token = accounts.create_invite(admin, email, c.name, tenant_id=c.tenant_id, role=role)
        except accounts.AccountError as e:
            with db.tenant(c.tenant_id) as s:
                return settings_page(request, c, s, error=f"Invito non creato: {e}.", status_code=400)
        to = accounts.normalise_email(email)
        base = (getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/")
        url, sent = f"{base}/invite/{token}", False
        with db.tenant(c.tenant_id) as s:
            room = s.execute(select(m.tenants.c.name)).scalar()
            mail = ctx_mail()
            if mail is not None:
                try:
                    mail(to, *accounts.link_text("invite", url, room, c.name))
                    sent = True
                except Exception:
                    sent = False
            return settings_page(request, c, s, new_link={"email": to, "url": url, "sent": sent})

    @app.post("/ui/people/{uid}/role")
    def ui_role(uid: int, role: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            accounts.change_role(admin, c.tenant_id, uid, role)
        except accounts.AccountError as e:
            return done("/settings", f"Ruolo non cambiato: {e}.")
        return done("/settings", "Ruolo aggiornato.")

    @app.post("/ui/people/{uid}/remove")
    def ui_remove(uid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            accounts.remove_member(admin, c.tenant_id, uid)
        except accounts.AccountError as e:
            return done("/settings", f"Persona non rimossa: {e}.")
        return done("/settings", "Persona rimossa dalla redazione: non entra più.")

    @app.post("/ui/invites/{sha}/revoke")
    def ui_revoke_invite(sha: str, c: auth.Caller = Depends(ctx.need("owner"))):
        n = accounts.revoke_invite(admin, c.tenant_id, sha)
        return done("/settings", "Invito annullato." if n else "Invito già usato o scaduto.")
