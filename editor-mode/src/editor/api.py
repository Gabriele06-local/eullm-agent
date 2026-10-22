"""Dashboard and JSON API of Editor Mode (FastAPI).

Every route resolves the caller's token to a tenant and works inside that
tenant's scope, so row level security applies to every query. Roles:
viewer reads, editor decides proposals and gives feedback, owner also adds
sites, approves profiles and manages sources. The HTML pages are a thin
server-rendered layer over the same functions; a richer frontend can use
the JSON routes.
"""

from __future__ import annotations

import re
from pathlib import Path
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import IntegrityError

from . import accounts, auth, console, drafts, i18n, jobs, models as m, profile as prof, proposals as props, publishing, queue, repo, ui, vault
from .briefing import TEMPLATES as _BRIEF_TEMPLATES

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC = {name: (kind, (_STATIC_DIR / name).read_bytes())
           for name, kind in (("logo.webp", "image/webp"), ("favicon.png", "image/png"))}

DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
COOKIE = "editor_token"


class SiteIn(BaseModel):
    domain: str = Field(..., max_length=253)


class DecisionIn(BaseModel):
    accept: bool
    note: str | None = Field(None, max_length=1000)


class FeedbackIn(BaseModel):
    target: str = Field(..., pattern="^(proposal|topic|source|draft)$")
    target_id: int
    verdict: str = Field(..., pattern="^(up|down|already_covered|off_topic)$")
    note: str | None = Field(None, max_length=1000)


class TargetIn(BaseModel):
    site_id: int
    kind: str = Field(..., pattern="^(wordpress|webhook|telegram_channel)$")
    name: str = Field(..., max_length=100)
    config: dict = {}
    # The secret's short name. Its value is either given here (stored
    # encrypted, never returned) or set by the operator in the environment
    # variable EDITOR_SECRET_<TENANT>__<NAME>.
    secret_env: str | None = Field(None, pattern="^[A-Z0-9]+(_[A-Z0-9]+)*$", max_length=40)
    secret: str | None = Field(None, min_length=1, max_length=4096)


class SecretIn(BaseModel):
    value: str = Field(..., min_length=1, max_length=4096)


class PublicationIn(BaseModel):
    target_id: int
    mode: str = Field("draft", pattern="^(draft|publish)$")


class SourceStatusIn(BaseModel):
    status: str = Field(..., pattern="^(active|suspended|rejected)$")
    reason: str | None = Field(None, max_length=300)


def normalise_domain(value: str) -> str:
    d = value.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
    if not DOMAIN.match(d):
        raise HTTPException(400, "not a valid domain name")
    return d


def create_app(db, core=None, admin=None, mail=None, public_url: str = "", gateway=None) -> FastAPI:
    """admin: the owner connection, for accounts and the operator (by
    default the database's own `admin_url`, else `db`); mail: (to,
    subject, text) -> None, for invites (None: links are shown instead);
    public_url: the address people open, for the links in invites;
    gateway: online payments (gateway.Mollie), None for bank transfers."""
    app = FastAPI(title="EuLLM Agent · Editor Mode", docs_url=None, redoc_url=None)
    if admin is None:
        from .db import Database
        admin = getattr(db, "_admin", None)
        if admin is None:
            url = getattr(db, "admin_url", None)
            admin = db._admin = Database.from_url(url) if url else db

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str):
        # Only the files shipped with the package, never a path.
        if name not in _STATIC:
            raise HTTPException(404)
        return Response(_STATIC[name][1], media_type=_STATIC[name][0],
                        headers={"Cache-Control": "public, max-age=86400"})
    pages = Jinja2Templates(env=_BRIEF_TEMPLATES)
    i18n.install(pages.env)

    @app.middleware("http")
    async def language(request: Request, call_next):
        # Every page, notice and error of this request in the chosen language.
        token = i18n.use(i18n.pick(request.cookies.get(i18n.COOKIE), request.headers.get("accept-language")))
        try:
            return await call_next(request)
        finally:
            i18n._current.reset(token)

    @app.get("/lang", include_in_schema=False)
    def set_language(l: str, back: str = "/"):
        resp = RedirectResponse(ui.safe_path(back), status_code=303)
        if l in i18n.LANGS:
            resp.set_cookie(i18n.COOKIE, l, httponly=True, samesite="lax", max_age=365 * 86400)
        return resp

    def caller(request: Request) -> auth.Caller:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.cookies.get(COOKIE)
        c = auth.resolve(db, token, admin)
        if c is None:
            raise HTTPException(401, "missing or invalid token")
        if request.method == "POST" and not header and request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
            raise HTTPException(403, "cross-site request refused")
        return c

    def need(role: str):
        def dep(c: auth.Caller = Depends(caller)) -> auth.Caller:
            if not c.can(role):
                raise HTTPException(403, f"{role} role required")
            return c
        return dep

    def site_or_404(s, site_id: int):
        row = s.execute(select(m.sites).where(m.sites.c.id == site_id)).first()
        if row is None:
            raise HTTPException(404, "site not found")
        return row

    # Background work is queued, never run in this process: the worker runs
    # it (work.py), and a restart of the dashboard loses nothing.
    def run_analysis(tenant: str, domain: str):
        queue.enqueue(db, tenant, "analysis", domain)

    def run_discovery(tenant: str, site_id: int):
        with db.tenant(tenant) as s:  # named by its domain on the pages that list the work
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        queue.enqueue(db, tenant, "discovery", domain or f"site {site_id}", {"site_id": site_id})

    def run_draft(tenant: str, proposal_id: int):
        queue.enqueue(db, tenant, "draft", f"proposal {proposal_id}", {"proposal_id": proposal_id})

    def run_proposals(tenant: str, site_id: int, since: datetime):
        queue.enqueue(db, tenant, "proposals", f"site {site_id}", {"site_id": site_id, "since": since.isoformat()})

    def run_publication(tenant: str, pub_id: int):
        queue.enqueue(db, tenant, "publish", f"publication {pub_id}", {"publication_id": pub_id})

    # --- JSON -------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs(c: auth.Caller = Depends(caller)):
        """Latest background work and how it ended."""
        with db.tenant(c.tenant_id) as s:
            return {"jobs": jobs.recent(s, 50)}

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/sites")
    def list_sites(c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            out = []
            for site in s.execute(select(m.sites).order_by(m.sites.c.domain)).all():
                p = m.editorial_profiles
                versions = s.execute(select(p.c.version, p.c.status, p.c.origin).where(p.c.site_id == site.id)
                                     .order_by(p.c.version)).all()
                last = s.execute(select(m.site_analyses.c.status, m.site_analyses.c.problems, m.site_analyses.c.started_at)
                                 .where(m.site_analyses.c.site_id == site.id).order_by(m.site_analyses.c.id.desc()).limit(1)).first()
                out.append({"id": site.id, "domain": site.domain, "name": site.name, "language": site.language,
                            "profiles": [dict(v._mapping) for v in versions],
                            "last_analysis": dict(last._mapping) if last else None})
        return {"sites": out}

    @app.post("/api/sites", status_code=202)
    def add_site(body: SiteIn, c: auth.Caller = Depends(need("owner"))):
        domain = normalise_domain(body.domain)
        run_analysis(c.tenant_id, domain)
        return {"domain": domain, "status": "analysis started"}

    @app.post("/api/sites/{site_id}/review", status_code=202)
    def review_site(site_id: int, c: auth.Caller = Depends(need("owner"))):
        with db.tenant(c.tenant_id) as s:
            site = site_or_404(s, site_id)
        run_analysis(c.tenant_id, site.domain)
        return {"domain": site.domain, "status": "review started"}

    @app.get("/api/sites/{site_id}/profiles")
    def profiles(site_id: int, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            site_or_404(s, site_id)
            p = m.editorial_profiles
            rows = s.execute(select(p).where(p.c.site_id == site_id).order_by(p.c.version.desc())).all()
        return {"profiles": [{k: v for k, v in r._mapping.items() if k != "tenant_id"} for r in rows]}

    @app.post("/api/sites/{site_id}/profiles/{version}/approve")
    def approve(site_id: int, version: int, c: auth.Caller = Depends(need("owner"))):
        with db.tenant(c.tenant_id) as s:
            site_or_404(s, site_id)
            if not prof.approve(s, site_id, version, c.name):
                raise HTTPException(404, "version not found")
        # Sources follow the approved line only, so look for them now.
        run_discovery(c.tenant_id, site_id)
        return {"approved": version}

    @app.put("/api/sites/{site_id}/profile")
    def edit_profile(site_id: int, body: dict, c: auth.Caller = Depends(need("owner"))):
        try:
            body = prof.validate_edit(body)
        except ValueError as e:
            raise HTTPException(400, f"invalid profile: {e}") from None
        with db.tenant(c.tenant_id) as s:
            site = site_or_404(s, site_id)
            body["domain"] = site.domain
            pid = prof.edit(s, c.tenant_id, site_id, body)
            version = s.execute(select(m.editorial_profiles.c.version).where(m.editorial_profiles.c.id == pid)).scalar()
        return {"draft_version": version}

    @app.get("/api/sites/{site_id}/sources")
    def list_sources(site_id: int, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            site_or_404(s, site_id)
            rows = s.execute(select(m.sources).where(m.sources.c.site_id == site_id).order_by(m.sources.c.score.desc().nulls_last())).all()
        keep = ("id", "kind", "name", "url", "status", "origin", "score", "evaluation", "evidence", "status_reason", "last_item_at")
        return {"sources": [{k: r._mapping[k] for k in keep} for r in rows]}

    @app.post("/api/sources/{source_id}/status")
    def source_status(source_id: int, body: SourceStatusIn, c: auth.Caller = Depends(need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sources.c.id).where(m.sources.c.id == source_id, repo.own())).scalar() is None:
                raise HTTPException(404, "source not found")
            repo.set_source_status(s, source_id, body.status, body.reason or f"set by {c.name}", by=c.name)
        return {"status": body.status}

    @app.get("/api/proposals")
    def list_proposals(status: str = "proposed", c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            rows = s.execute(select(m.proposals, m.sites.c.domain).join(m.sites, m.sites.c.id == m.proposals.c.site_id)
                             .where(m.proposals.c.status == status).order_by(m.proposals.c.created_at.desc()).limit(200)).all()
            out = []
            for r in rows:
                cites = s.execute(select(m.source_items.c.id, m.source_items.c.title, m.source_items.c.url)
                                  .join(m.proposal_citations, m.proposal_citations.c.item_id == m.source_items.c.id)
                                  .where(m.proposal_citations.c.proposal_id == r.id)).all()
                d = {k: v for k, v in r._mapping.items() if k != "tenant_id"}
                d["citations"] = [dict(x._mapping) for x in cites]
                out.append(d)
        return {"proposals": out}

    @app.post("/api/proposals/{proposal_id}/decision")
    def decision(proposal_id: int, body: DecisionIn, c: auth.Caller = Depends(need("editor"))):
        if not props.decide(db, c.tenant_id, proposal_id, body.accept, c.name, body.note):
            raise HTTPException(409, "proposal not found or already decided")
        return {"status": "accepted" if body.accept else "rejected"}

    @app.post("/api/proposals/{proposal_id}/draft", status_code=202)
    def request_draft(proposal_id: int, c: auth.Caller = Depends(need("editor"))):
        if core is None:
            raise HTTPException(503, "no Core configured: drafts need a model")
        with db.tenant(c.tenant_id) as s:
            st = s.execute(select(m.proposals.c.status).where(m.proposals.c.id == proposal_id)).scalar()
        if st not in ("accepted", "drafted"):
            raise HTTPException(409, "accept the proposal first")
        run_draft(c.tenant_id, proposal_id)
        return {"status": "writing"}

    @app.get("/api/drafts")
    def list_drafts(status: str | None = None, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            q = select(m.drafts.c.id, m.drafts.c.proposal_id, m.drafts.c.version, m.drafts.c.title, m.drafts.c.status,
                       m.drafts.c.created_at).order_by(m.drafts.c.created_at.desc()).limit(200)
            if status:
                q = q.where(m.drafts.c.status == status)
            return {"drafts": [dict(r._mapping) for r in s.execute(q).all()]}

    @app.get("/api/drafts/{draft_id}")
    def get_draft(draft_id: int, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
            if d is None:
                raise HTTPException(404, "draft not found")
            claims = drafts.stored_claims(s, draft_id)
        out = {k: v for k, v in d._mapping.items() if k != "tenant_id"}
        out["claims"] = [{k: c[k] for k in ("ordinal", "section", "text", "sources")} for c in claims if c["sources"]]
        return out

    @app.post("/api/drafts/{draft_id}/decision")
    def draft_decision(draft_id: int, body: DecisionIn, c: auth.Caller = Depends(need("editor"))):
        if not drafts.decide(db, c.tenant_id, draft_id, body.accept, c.name):
            raise HTTPException(409, "draft not found or already decided")
        return {"status": "approved" if body.accept else "rejected"}

    @app.get("/api/export")
    def get_export(c: auth.Caller = Depends(need("owner"))):
        """Every row of the tenant, credentials left out."""
        return ui.export_response(db, c.tenant_id)

    @app.get("/api/usage")
    def get_usage(c: auth.Caller = Depends(need("owner"))):
        from . import quotas
        with db.tenant(c.tenant_id) as s:
            u = quotas.usage(s)
        if u is None:
            raise HTTPException(404, "tenant not found")
        return {"plan": u.plan.get("plan"), "usage": {
            what: {"used": u.used.get(used), "limit": u.plan.get(limit)} for what, (limit, used) in quotas.LIMITS.items()}}

    @app.get("/api/targets")
    def list_targets(c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            rows = s.execute(select(m.publish_targets).order_by(m.publish_targets.c.id)).all()
        return {"targets": [{k: v for k, v in r._mapping.items() if k != "tenant_id"} for r in rows]}

    @app.post("/api/targets", status_code=201)
    def add_target(body: TargetIn, c: auth.Caller = Depends(need("owner"))):
        with db.tenant(c.tenant_id) as s:
            site_or_404(s, body.site_id)
        if body.secret is not None and body.secret_env is None:
            raise HTTPException(400, "a secret needs a name (secret_env)")
        try:
            if body.secret is not None:
                vault.put(db, c.tenant_id, body.secret_env, body.secret)
            tid = publishing.add_target(db, c.tenant_id, body.site_id, body.kind, body.name, body.config, body.secret_env)
        except (publishing.PublishError, vault.VaultError) as e:
            raise HTTPException(400, str(e)) from None
        return {"id": tid}

    @app.get("/api/secrets")
    def list_secrets(c: auth.Caller = Depends(need("owner"))):
        """Names of the stored secrets; values are never returned."""
        with db.tenant(c.tenant_id) as s:
            return {"secrets": sorted(vault.stored(s))}

    @app.put("/api/secrets/{name}")
    def put_secret(name: str, body: SecretIn, c: auth.Caller = Depends(need("owner"))):
        try:
            vault.put(db, c.tenant_id, name, body.value)
        except vault.VaultError as e:
            raise HTTPException(400, str(e)) from None
        return {"stored": name}

    @app.delete("/api/secrets/{name}")
    def delete_secret(name: str, c: auth.Caller = Depends(need("owner"))):
        if not vault.remove(db, c.tenant_id, name):
            raise HTTPException(404, "no such secret")
        return {"deleted": name}

    @app.post("/api/drafts/{draft_id}/publications", status_code=201)
    def request_publication(draft_id: int, body: PublicationIn, c: auth.Caller = Depends(need("editor"))):
        try:
            pid = publishing.request(db, c.tenant_id, draft_id, body.target_id, body.mode, c.name)
        except publishing.PublishError as e:
            raise HTTPException(409, str(e)) from None
        except IntegrityError:
            raise HTTPException(409, "already requested") from None
        return {"id": pid, "status": "pending_approval"}

    @app.get("/api/publications")
    def list_publications(status: str | None = None, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            q = select(m.publications.c.id, m.publications.c.draft_id, m.publications.c.target_id, m.publications.c.mode,
                       m.publications.c.status, m.publications.c.requested_by, m.publications.c.decided_by,
                       m.publications.c.external_url, m.publications.c.error).order_by(m.publications.c.id.desc())
            if status:
                q = q.where(m.publications.c.status == status)
            return {"publications": [dict(r._mapping) for r in s.execute(q).all()]}

    @app.post("/api/publications/{pub_id}/decision")
    def decide_publication(pub_id: int, body: DecisionIn, c: auth.Caller = Depends(need("owner"))):
        if not publishing.decide(db, c.tenant_id, pub_id, body.accept, c.name, body.note):
            raise HTTPException(409, "publication not found or already decided")
        if body.accept:
            run_publication(c.tenant_id, pub_id)
        return {"status": "approved" if body.accept else "rejected"}

    @app.post("/api/feedback", status_code=201)
    def feedback(body: FeedbackIn, c: auth.Caller = Depends(need("editor"))):
        with db.tenant(c.tenant_id) as s:
            props.record_feedback(s, c.tenant_id, body.target, body.target_id, body.verdict, c.name, body.note)
        return {"recorded": True}

    @app.get("/api/topics")
    def topics(site_id: int | None = None, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            q = text("""
                SELECT t.id, t.label, t.keywords, t.labelled_by,
                       (SELECT score FROM editor.trend_scores ts WHERE ts.topic_id = t.id ORDER BY computed_at DESC LIMIT 1) AS hype,
                       (SELECT score FROM editor.opportunity_scores o WHERE o.topic_id = t.id
                          AND (CAST(:site AS bigint) IS NULL OR o.site_id = :site) ORDER BY computed_at DESC LIMIT 1) AS opportunity,
                       (SELECT count(*) FROM editor.topic_items ti WHERE ti.topic_id = t.id) AS items
                FROM editor.topics t WHERE t.updated_at > :since
                  -- Topics of the archive with news from a source the newsroom (or the site) follows.
                  AND EXISTS (SELECT 1 FROM editor.topic_items ti JOIN editor.source_items si ON si.id = ti.item_id
                              JOIN editor.sources f ON f.id = si.source_id OR f.archive_id = si.source_id
                              WHERE ti.topic_id = t.id AND f.status = 'active' AND f.shared IS NOT TRUE
                                AND (CAST(:site AS bigint) IS NULL OR f.site_id = :site OR f.site_id IS NULL))
                ORDER BY hype DESC NULLS LAST LIMIT 100""").bindparams(
                bindparam("since", type_=m.topics.c.updated_at.type)).columns(keywords=m.topics.c.keywords.type)
            rows = s.execute(q, {"site": site_id, "since": datetime.now(UTC) - timedelta(days=7)}).all()
        return {"topics": [dict(r._mapping) for r in rows]}

    @app.get("/api/briefings/{day}")
    def get_briefing(day: date, c: auth.Caller = Depends(caller)):
        with db.tenant(c.tenant_id) as s:
            b = s.execute(select(m.briefings).where(m.briefings.c.briefing_date == day)).first()
        if b is None:
            raise HTTPException(404, "no briefing for that day")
        return {"date": day.isoformat(), "markdown": b.body_md, "sent_email_at": b.sent_email_at,
                "sent_telegram_at": b.sent_telegram_at}

    # --- HTML -------------------------------------------------------------

    ui.register(app, SimpleNamespace(db=db, admin=admin, pages=pages, need=need, core=core, mail=mail,
                                     public_url=public_url,
                                     run_analysis=run_analysis, run_discovery=run_discovery, run_draft=run_draft,
                                     run_proposals=run_proposals,
                                     run_publication=run_publication))
    console.register(app, SimpleNamespace(db=db, admin=admin, pages=pages, core=core, mail=mail,
                                          public_url=public_url, gateway=gateway))

    @app.exception_handler(HTTPException)
    def errors(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or request.headers.get("authorization"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        return pages.TemplateResponse(request, "error.html.j2", {"status": exc.status_code, "detail": exc.detail},
                                      status_code=exc.status_code)

    return app


def default_app() -> FastAPI:
    from . import runtime

    from . import gateway

    st = runtime.settings()
    return create_app(runtime.database(), runtime.core(), runtime.admin_database(),
                      accounts.smtp_mailer(st), st.public_url, gateway.from_settings(st))
