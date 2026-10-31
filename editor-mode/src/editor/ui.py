"""Dashboard pages of Editor Mode.

Server-rendered over the same functions as the JSON API, so tenancy, roles
and checks are the same: every page reads inside the caller's tenant (row
level security applies) and every form posts to a route that requires the
role the action needs. Forms answer with a redirect and a one-shot notice.
"""

from __future__ import annotations

import json
import re
from importlib import metadata
from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, bindparam, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from . import opportunity as oppo, queue
from . import activity as act, accounts, auth, autopilot, billing, delivery, drafts, i18n, jobs, manage, models as m, news_base, profile as prof, proposals as props, images, publishing, quotas, repo, tenant_data, translate as tr, trends, trials, usage, vault
from .i18n import N_, t
from .markdown import to_html

COOKIE = "editor_token"
SITE_COOKIE = "editor_site"
# "site": the Desk shows only the site chosen in the menu; anything else
# (the default): every site.
SCOPE_COOKIE = "editor_scope"
NOTICE_COOKIE = "editor_notice"

ORIGINS = {
    "manual": N_("Aggiunta a mano"),
    "site_outbound": N_("Link dal sito"),
    "site_feed": N_("Feed del sito"),
    "autodiscovery": N_("Feed scoperto"),
    "api_query": N_("Ricerca dal profilo"),
    "model_suggestion": N_("Suggerita dal modello"),
}
FORMATS = {
    "analysis": N_("analisi"), "news": N_("notizia"), "opinion": N_("opinione"), "guide": N_("guida"),
    "how-to": N_("guida"), "tutorial": N_("guida"), "interview": N_("intervista"), "review": N_("recensione"),
    "explainer": N_("spiegazione"),
}
KINDS = {"rss": "RSS", "hackernews": "HN", "github": "GitHub", "huggingface": "Hugging Face", "arxiv": "arXiv"}
TARGET_KINDS = {"wordpress": "WordPress", "github": N_("GitHub (repo del sito)"), "webhook": "Webhook",
                "telegram_channel": N_("Canale Telegram")}
QUOTA_LABELS = {
    "sites": N_("Siti"),
    "sources": N_("Fonti attive"),
    "items": N_("Notizie oggi"),
    "drafts": N_("Bozze nel mese"),
    "llm_cost": N_("Costo modello nel mese"),
}
CHECKS = [
    ("no valid source", N_("Ogni frase ha almeno una fonte")),
    ("numbers not found", N_("Ogni numero compare nelle fonti citate")),
    ("copies a passage", N_("Nessun passaggio copiato parola per parola")),
]

SEO_CHECKS = {
    "keyphrase": N_("C'è una frase chiave, quella che i lettori cercano"),
    "keyphrase_title": N_("La frase chiave è nel titolo"),
    "keyphrase_lead": N_("La frase chiave è nelle prime righe"),
    "title_length": N_("Titolo per Google entro 60 caratteri"),
    "description_length": N_("Descrizione per Google tra 70 e 160 caratteri"),
    "answer_first": N_("Le prime righe rispondono subito alla domanda"),
    "headings": N_("Almeno due sottotitoli"),
    "faq": N_("Almeno due domande frequenti con risposta"),
    "internal_links": N_("Link ad altri articoli del sito"),
    "sources": N_("Almeno due fonti citate"),
    "length": N_("Lunghezza vicina a quella degli articoli del sito"),
}


MONTHS = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
DAYS = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


class LoginRequired(Exception):
    pass


def safe_path(value: str | None, default: str = "/") -> str:
    """A local path to go back to; anything else becomes the default."""
    # No control character either: browsers drop a tab, so "/\t/evil.com" would become "//evil.com".
    if (value and value.startswith("/") and not value.startswith("//") and "\\" not in value
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        return value
    return default


try:
    VERSION = metadata.version("eullm-editor-mode")
except metadata.PackageNotFoundError:
    VERSION = None


def done(path: str, notice: str | None = None) -> RedirectResponse:
    resp = RedirectResponse(path, status_code=303)
    if notice:
        resp.set_cookie(NOTICE_COOKIE, quote(notice), max_age=60, httponly=True, samesite="strict")
    return resp


PROBLEMS = [
    (r"no RSS or Atom feed found", N_("Il sito non ha un feed RSS o Atom: gli articoli sono stati trovati da sitemap e pagine.")),
    (r"no usable sitemap", N_("Nessuna sitemap utilizzabile: gli articoli sono stati trovati seguendo i link.")),
    (r"page budget reached", N_("Raggiunto il limite di pagine da leggere per un'analisi.")),
    (r"no publication dates found", N_("Non ho trovato le date di pubblicazione: il ritmo del sito non si può misurare.")),
    (r"language not determined", N_("Lingua non determinata.")),
    (r"home page unreachable", N_("La home page non risponde.")),
    (r"robots\.txt unreachable.*", N_("Il sito non risponde: nemmeno robots.txt si può leggere. Controlla che il dominio sia scritto giusto e che il sito sia online.")),
    (r"(\d+) articles read \(at least (\d+) wanted\)", N_(r"Letti \1 articoli (ne servono almeno \2 per un'analisi completa).")),
    (r"only (\d+) articles readable and no categories.*", N_(r"Solo \1 articoli leggibili e nessuna categoria: troppo poco per descrivere il sito.")),
    (r"model analysis failed \((.*)\): .*", N_(r"Il modello non ha completato l'analisi (\1): settore, pubblico e stile restano da determinare.")),
    (r"model analysis not run.*", N_("Analisi del modello non eseguita: settore, pubblico e stile restano da determinare.")),
    (r"not enough material.*", N_("Materiale insufficiente: settore, pubblico e stile restano da determinare.")),
    (r"nothing new for more than (\d+) days", N_(r"Nessuna notizia nuova da più di \1 giorni: ritorna in valutazione tra due settimane.")),
    (r"(\d+) failed fetches in a row", N_(r"\1 letture fallite di fila: la riprovo da sola ogni giorno.")),
    (r"off topic \(relevance ([\d.]+) on the last (\d+) items\)",
     N_(r"Fuori tema: pertinenza \1 sulle ultime \2 notizie. Ritorna in valutazione tra due settimane.")),
    (r"replaced by GitHub Trending",
     N_("Sostituita da GitHub Trending: conta quali repository gli sviluppatori premiano oggi, non quali contengono una parola.")),
]


def problem(text) -> str:
    """The crawler's and profile's notes, in the dashboard's language."""
    text = str(text)
    for pattern, it in PROBLEMS:
        if re.fullmatch(pattern, text, flags=re.S):
            return re.sub(pattern, t(it), text, flags=re.S)
    return text


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
    env.filters["tok"] = usage.tokens
    env.globals["version"] = VERSION
    env.filters["problem"] = problem
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
        # A person whose newsroom is on hold for billing chooses a plan in /account.
        to = "/login" if s is None else "/admin" if s.operator else "/account"
        return RedirectResponse(to, status_code=303)

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

    def whole(request: Request) -> bool:
        """Is the Desk showing every site ("Tutti i siti" in the menu)?"""
        return request.cookies.get(SCOPE_COOKIE) != "site"

    def choose_site(resp, site_id: int) -> None:
        resp.set_cookie(SITE_COOKIE, str(site_id), httponly=True, samesite="strict", max_age=365 * 86400)
        resp.set_cookie(SCOPE_COOKIE, "site", httponly=True, samesite="strict", max_age=365 * 86400)

    def forget_site(resp) -> None:
        resp.delete_cookie(SITE_COOKIE)
        resp.delete_cookie(SCOPE_COOKIE)

    def plan_name(s) -> str | None:
        """The name of the newsroom's plan, for the menu (also without a subscription)."""
        code = s.execute(select(m.tenants.c.plan)).scalar()
        if not code:
            return None
        with accounts._read(admin) as conn:
            return conn.execute(select(m.plans.c.name).where(m.plans.c.code == code)).scalar() or code

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
            day = f"{value.day} {i18n.MONTHS[i18n.current()][value.month - 1]}"
            return f"{day} {value:%H:%M}" if part == "full" and isinstance(value, datetime) else day

        notice = request.cookies.get(NOTICE_COOKIE)
        rooms = [x for x in accounts.memberships(admin, c.user_id) if x.active] if c.user_id else []
        resp = pages.TemplateResponse(request, name, {
            "caller": c, "sites": sites, "site": site, "active": active, "newsrooms": rooms,
            "all_sites": whole(request),
            "plan_state": billing.subscription(admin, c.tenant_id) if c.can("owner") else None,
            "plan_name": plan_name(s),
            "activity": data.pop("activity", None) or act.recent(s, tz, problem),
            "notice": unquote(notice) if notice else None, "now": datetime.now(tz), "when": when, "days": i18n.DAYS[i18n.current()], "months": i18n.MONTHS[i18n.current()], **data,
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
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                        max_age=30 * 86400)
        forget_site(resp)
        return resp

    def session_for(request: Request, s: accounts.Session) -> RedirectResponse:
        """Sign in to the newsroom the session names, or the first one the
        person belongs to; an operator goes to the administration (/admin),
        from where /editor or "Entra" opens a newsroom."""
        tenant = s.tenant_id
        if tenant is None and not s.operator and (bought := billing.bought_unpaid(admin, s.user_id)):
            # Bought on the site and not paid yet: the payment comes first.
            return signed_in(request, accounts.start_session(admin, s.user_id, None if billing.on_hold(admin, bought) else bought), "/account")
        if tenant is None and not s.operator:
            rooms = accounts.memberships(admin, s.user_id)
            tenant = next((x.tenant_id for x in rooms if x.active), None)
            if tenant is None and any(x.role == "owner" and billing.on_hold(admin, x.tenant_id) for x in rooms):
                # Its trial or paid period ended: the owner can still choose a plan.
                return signed_in(request, accounts.start_session(admin, s.user_id, None), "/account")
        if tenant is None and not s.operator:
            return login_form(request, t("Il tuo account non è in nessuna redazione attiva: chiedi un invito."), 403)
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
            return setup_form(request, token, t("Le due password non coincidono."), 400)
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
                return login_form(request, t("Token non valido"), 401)
            return signed_in(request, token.strip())
        try:
            u = accounts.authenticate(admin, email, password)
        except accounts.AccountError as e:
            return login_form(request, f"{str(e)[:1].upper()}{str(e)[1:]}.", 429, email)
        if u is None:
            return login_form(request, t("Email o password sbagliate"), 401, email)
        return session_for(request, accounts.Session(u.id, u.email, u.name, bool(u.operator), None, None))

    @app.post("/logout")
    def logout(request: Request):
        accounts.end_session(admin, request.cookies.get(COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        forget_site(resp)
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
            return invite_form(request, token, info, t("Le due password non coincidono."), 400)
        try:
            s = accounts.accept(admin, token, password, name)
        except accounts.AccountError as e:
            return invite_form(request, token, info, f"{str(e)[:1].upper()}{str(e)[1:]}.", 400)
        if not s.operator and (bought := billing.bought_unpaid(admin, s.user_id)):
            # Bought on the site: the payment comes first, the newsroom is already there.
            return signed_in(request, accounts.start_session(admin, s.user_id, None if billing.on_hold(admin, bought) else bought), "/account")
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
                # One link every few minutes per address: the form cannot flood a mailbox.
                if u is not None and u.disabled_at is None and not accounts.reset_sent_recently(admin, e):
                    token = accounts.create_invite(admin, e, "password dimenticata", kind="reset")
                    base = (getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/")
                    mail(e, *accounts.link_text("reset", f"{base}/invite/{token}", None, ""))
            except Exception:
                pass
        return pages.TemplateResponse(request, "forgot.html.j2", {"sent": True, "can_reset": mail is not None})

    @app.get("/switch")
    def switch(request: Request, site: str, back: str | None = None, c: auth.Caller = Depends(viewer)):
        """The site menu: one site (every page shows it, the Desk too) or
        "all" (the Desk shows every site; the pages of one site keep the
        last one chosen)."""
        if site == "all":
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(SCOPE_COOKIE, "all", httponly=True, samesite="strict", max_age=365 * 86400)
            return resp
        with db.tenant(c.tenant_id) as s:
            if not site.isdigit() or s.execute(select(m.sites.c.id).where(m.sites.c.id == int(site))).scalar() is None:
                raise HTTPException(404, "site not found")
        resp = RedirectResponse(safe_path(back), status_code=303)
        choose_site(resp, int(site))
        return resp

    # --- queries shared by pages -------------------------------------------------

    def topic_scores(s, site_id: int | None, limit: int = 100):
        return s.execute(text("""
            SELECT t.id, t.label,
                   (SELECT score FROM editor.trend_scores ts WHERE ts.topic_id = t.id ORDER BY computed_at DESC LIMIT 1) AS hype,
                   (SELECT score FROM editor.opportunity_scores o WHERE o.topic_id = t.id AND o.site_id = :site
                     ORDER BY computed_at DESC LIMIT 1) AS opportunity,
                   (SELECT count(*) FROM editor.topic_items ti WHERE ti.topic_id = t.id) AS items,
                   (SELECT count(DISTINCT si.source_id) FROM editor.topic_items ti
                     JOIN editor.source_items si ON si.id = ti.item_id WHERE ti.topic_id = t.id) AS sources,
                   (SELECT max(src.score) FROM editor.topic_items ti JOIN editor.source_items si ON si.id = ti.item_id
                     JOIN editor.sources src ON src.id = si.source_id WHERE ti.topic_id = t.id) AS best_source,
                   t.summary,
                   (SELECT si.title FROM editor.topic_items ti JOIN editor.source_items si ON si.id = ti.item_id
                     WHERE ti.topic_id = t.id ORDER BY si.published_at DESC NULLS LAST, si.id DESC LIMIT 1) AS headline,
                   (SELECT si.url FROM editor.topic_items ti JOIN editor.source_items si ON si.id = ti.item_id
                     WHERE ti.topic_id = t.id ORDER BY si.published_at DESC NULLS LAST, si.id DESC LIMIT 1) AS headline_url
            FROM editor.topics t WHERE t.updated_at > :since
              -- Topics of the archive with news from a source the site follows.
              AND EXISTS (SELECT 1 FROM editor.topic_items ti JOIN editor.source_items si ON si.id = ti.item_id
                          JOIN editor.sources f ON f.id = si.source_id OR f.archive_id = si.source_id
                          WHERE ti.topic_id = t.id AND f.status = 'active' AND f.shared IS NOT TRUE
                            AND (f.site_id = :site OR f.site_id IS NULL))
            ORDER BY opportunity DESC NULLS LAST, hype DESC NULLS LAST LIMIT :lim""").bindparams(
            bindparam("since", type_=m.topics.c.updated_at.type)),
            {"site": site_id, "lim": limit, "since": datetime.now(UTC) - timedelta(days=7)}).all()

    def proposal_rows(s, site_id: int | None, statuses: tuple[str, ...], limit: int = 30):
        p = m.proposals
        hype = (select(m.trend_scores.c.score).where(m.trend_scores.c.topic_id == p.c.topic_id)
                .order_by(m.trend_scores.c.computed_at.desc()).limit(1).scalar_subquery())
        cites = (select(func.count()).select_from(m.proposal_citations)
                 .where(m.proposal_citations.c.proposal_id == p.c.id).scalar_subquery())
        draft = (select(func.max(m.drafts.c.id)).where(m.drafts.c.proposal_id == p.c.id,
                                                       m.drafts.c.translation_of.is_(None)).scalar_subquery())
        q = (select(p, m.sites.c.domain, hype.label("hype"), cites.label("citations"), draft.label("draft_id"))
             .join(m.sites, m.sites.c.id == p.c.site_id).where(p.c.status.in_(statuses))
             .order_by(p.c.created_at.desc()).limit(limit))
        if site_id is not None:
            q = q.where(p.c.site_id == site_id)
        return s.execute(q).all()

    def proposal_details(s, rows) -> dict[int, dict]:
        """What each proposal stands on, in plain words: the article it
        suggests, the news it comes from, why it suits the site and what its
        two scores mean."""
        ids = [r.id for r in rows]
        if not ids:
            return {}
        si, pc, o = m.source_items, m.proposal_citations, m.opportunity_scores
        news: dict[int, list] = {}
        for n in s.execute(
                select(pc.c.proposal_id, si.c.title, si.c.url, si.c.published_at, m.sources.c.name.label("source"))
                .join(si, si.c.id == pc.c.item_id).join(m.sources, m.sources.c.id == si.c.source_id)
                .where(pc.c.proposal_id.in_(ids)).order_by(si.c.published_at.desc().nulls_last())).all():
            news.setdefault(n.proposal_id, []).append(n)
        out = {}
        for r in rows:
            comp = s.execute(select(o.c.components).where(o.c.topic_id == r.topic_id, o.c.site_id == r.site_id)
                             .order_by(o.c.computed_at.desc()).limit(1)).scalar() or {}
            sources = s.execute(select(func.count(func.distinct(si.c.source_id)))
                                .join(m.topic_items, m.topic_items.c.item_id == si.c.id)
                                .where(m.topic_items.c.topic_id == r.topic_id)).scalar() or 0
            model = r.generated_by == "model"
            cited = news.get(r.id, [])
            if model and r.angle:
                article = r.angle
            elif len(cited) == 1:
                article = t("Un articolo che riprende la notizia qui sotto e la spiega ai lettori del tuo sito.")
            else:
                article = t("Un articolo che mette insieme le notizie qui sotto e le spiega ai lettori del tuo sito.")
            basis = r.basis or {}
            single = basis.get("kind") == "news"
            matched = basis.get("matched") if single else comp.get("matched_subtopic")
            why = []
            if matched:
                why.append(t("Rientra in «{topic}», uno degli argomenti del tuo profilo editoriale.", topic=matched))
            elif single and basis.get("picked_by") == "model":
                why.append(t("Il modello l'ha scelta tra le notizie delle ultime 72 ore come utile ai lettori del sito."))
            # Why today: how fresh the news is and how many sources tell it.
            today = []
            newest = max((n.published_at for n in cited if n.published_at), default=None)
            if newest is not None:
                hours = max(0, int((datetime.now(UTC) - newest).total_seconds() // 3600))
                ago = (t("meno di un'ora fa") if hours < 1 else t("{n} ore fa", n=hours) if hours < 48
                       else t("{n} giorni fa", n=hours // 24))
                today.append(t("Ne parlano {k} fonti diverse; la notizia più recente è uscita {ago}.", k=sources, ago=ago)
                             if sources > 1 else t("La notizia è uscita {ago}.", ago=ago))
            if model and r.why_now:
                today.append(r.why_now)
            # The site's own articles it could complete, or one it should rather update.
            posts = oppo.related_posts(s, r.site_id, [r.title] + [n.title for n in cited])
            update = posts[0] if posts and posts[0]["score"] >= oppo.UPDATE_SIMILARITY else None
            opp = round(r.opportunity or 0)
            hype = None if r.hype is None else round(r.hype)
            if hype is None:
                hype_text = t("Hype non ancora misurato: il tema è appena comparso.")
            elif sources <= 1:
                hype_text = t("Hype {n}/100: per ora ne parla una sola fonte, quindi non è ancora un tema di cui "
                              "parla tutto il settore. Non è un difetto: la proposta nasce perché la notizia è "
                              "vicina al tuo sito e la fonte è affidabile.", n=hype)
            elif hype < 30:
                hype_text = t("Hype {n}/100: se ne parla poco nel settore ({k} fonti).", n=hype, k=sources)
            elif hype < 60:
                hype_text = t("Hype {n}/100: se ne parla nel settore ({k} fonti).", n=hype, k=sources)
            else:
                hype_text = t("Hype {n}/100: è un tema caldo, ne parlano molte fonti ({k}).", n=hype, k=sources)
            out[r.id] = {
                # Without the model the title is the topic's keywords: the news headline says more.
                "title": r.title if model or not cited else cited[0].title,
                "article": article,
                "format": t(FORMATS[r.format]) if r.format in FORMATS else r.format,
                "news": cited,
                "why": " ".join(why),
                "today": " ".join(today),
                "update": update,
                "related": [x for x in posts if x is not update],
                "opportunity": t("Punteggio {n}/100 tra le notizie delle ultime 72 ore: vicinanza ai tuoi argomenti, "
                                 "fonte seguita dal sito e sua affidabilità, freschezza, quante fonti la riportano, "
                                 "novità rispetto a ciò che hai già scritto.", n=opp) if single else
                t("Opportunità {n}/100: quanto conviene a questo sito scriverne adesso, in base "
                  "alla vicinanza ai tuoi argomenti, alla novità e all'affidabilità delle fonti. "
                  "Da {min} in su l'agente la propone.", n=opp, min=round(props.MIN_OPPORTUNITY)),
                "single": single,
                "hype": hype_text,
                "opp": opp, "hype_n": hype,
            }
        return out

    def start_of_day(s) -> datetime:
        tz = zone(s)
        return datetime.combine(datetime.now(tz).date(), time(), tz)

    # --- Oggi -----------------------------------------------------------------------

    @app.get("/ui/activity", response_class=HTMLResponse)
    def activity_box(request: Request, mode: str = "recent", c: auth.Caller = Depends(viewer)):
        """The Attività box alone, fetched again while work runs: as on every
        page (recent), on the Desk (desk) or on the Attività page (page)."""
        with db.tenant(c.tenant_id) as s:
            data = act.recent(s, zone(s), problem, full=mode in ("desk", "page"), compact=mode != "page")
        return pages.TemplateResponse(request, "_activity.html.j2", {"activity": data},
                                      headers={"Cache-Control": "no-store"})

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, c: auth.Caller = Depends(viewer)):
        """Every job of the last day, with all it produced."""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            return render(request, "activity.html.j2", c, s, "today", _sites=(sites, site),
                          activity=act.recent(s, zone(s), problem, full=True, compact=False))

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            # "Tutti i siti" in the menu (the default): the whole newsroom;
            # one site chosen: only its decisions, work and numbers.
            one = None if whole(request) or len(sites) < 2 else site
            sid = one.id if one else None

            def of(col):
                return [col == sid] if one else []
            p = m.editorial_profiles
            profile_drafts = s.execute(
                select(p.c.site_id, p.c.version, p.c.origin, p.c.changes, m.sites.c.domain)
                .join(m.sites, m.sites.c.id == p.c.site_id).where(p.c.status == "draft", *of(p.c.site_id))
                .order_by(p.c.created_at.desc())).all()
            lines = dict(s.execute(select(p.c.site_id, p.c.version).where(p.c.status == "approved")).all())
            pending = {}
            for r in profile_drafts:
                pending.setdefault(r.site_id, r.version)
            site_lines = [{"id": x.id, "domain": x.domain, "approved": lines.get(x.id), "pending": pending.get(x.id)}
                          for x in sites]
            # The Desk: decisions, work and numbers of every site, or of the one chosen.
            proposed = proposal_rows(s, sid, ("proposed",), limit=12)
            explained = proposal_details(s, proposed)
            pending_pubs = s.execute(
                select(m.publications.c.id, m.publications.c.mode, m.publications.c.requested_by,
                       m.drafts.c.id.label("draft_id"), m.drafts.c.title, m.publish_targets.c.name.label("target"),
                       m.publish_targets.c.kind)
                .join(m.drafts, m.drafts.c.id == m.publications.c.draft_id)
                .join(m.publish_targets, m.publish_targets.c.id == m.publications.c.target_id)
                .where(m.publications.c.status == "pending_approval", *of(m.drafts.c.site_id))
                .order_by(m.publications.c.id)).all()
            # Suspended in the last week: by the maintenance for this newsroom,
            # or in the archive for everyone (a feed that stopped answering).
            a = m.sources.alias("a")
            week_ago = datetime.now(UTC) - timedelta(days=7)
            stopped = s.execute(
                select(m.sources.c.id, func.coalesce(a.c.status_reason, m.sources.c.status_reason).label("status_reason"),
                       m.sources.c.name, m.sources.c.site_id, m.sites.c.domain)
                .join(m.sites, m.sites.c.id == m.sources.c.site_id)
                .outerjoin(a, a.c.id == m.sources.c.archive_id)
                .where(*of(m.sources.c.site_id), or_(and_(m.sources.c.status == "suspended", m.sources.c.status_changed_at > week_ago),
                           and_(m.sources.c.status == "active", a.c.status == "suspended",
                                a.c.status_changed_at > week_ago)))).all()
            draft_counts = dict(s.execute(
                select(m.drafts.c.status, func.count()).where(m.drafts.c.status.in_(("draft", "needs_review")),
                                                              *of(m.drafts.c.site_id))
                .group_by(m.drafts.c.status)).all())
            new_today = s.execute(select(func.count()).select_from(m.proposals).where(
                m.proposals.c.status == "proposed", m.proposals.c.created_at >= start_of_day(s),
                *of(m.proposals.c.site_id))).scalar()
            usage = quotas.usage(s)
            # The archive sources this newsroom follows: when they were last read,
            # and fetches the Core could not make (down, or out of daily fetches).
            followed = list(repo.followed_archive(s))
            if one:  # the sources this site reads: its own, or the archive ones they follow
                src = m.sources.c
                followed = [r.archive_id or r.id for r in s.execute(select(src.id, src.archive_id).where(
                    src.site_id == sid, src.status == "active")).all()]
            last_fetch = s.execute(select(func.max(m.sources.c.last_fetched_at)).where(
                m.sources.c.id.in_(followed))).scalar()
            troubles = list(s.execute(select(m.sources.c.last_error).where(
                m.sources.c.id.in_(followed), m.sources.c.status == "active", m.sources.c.last_error.like("CoreTrouble%"),
                m.sources.c.last_fetched_at > datetime.now(UTC) - timedelta(hours=2))).scalars())
            core_trouble = {"n": len(troubles), "limit": any("limit" in e for e in troubles)} if troubles else None
            briefing = s.execute(select(m.briefings).where(
                m.briefings.c.briefing_date == datetime.now(zone(s)).date())).first()
            # Every proposal waiting, not only the ones listed.
            proposed_total = s.execute(select(func.count()).select_from(m.proposals).where(
                m.proposals.c.status == "proposed", *of(m.proposals.c.site_id))).scalar() or 0
            # The numbers of the collection: the plan's for the newsroom, or the site's.
            collected = {"sources": usage.used.get("active_sources", 0) if usage else 0,
                         "today": usage.used.get("items_today", 0) if usage else 0}
            if one:
                collected = {
                    "sources": s.execute(select(func.count()).select_from(m.sources).where(
                        m.sources.c.site_id == sid, m.sources.c.status == "active")).scalar() or 0,
                    "today": s.execute(select(func.count()).select_from(m.source_items).where(
                        repo.followed_by(sid), m.source_items.c.fetched_at >= start_of_day(s))).scalar() or 0}
            # A source the maintenance suspended is news, not a decision.
            decisions = len(profile_drafts) + proposed_total + len(pending_pubs)
            desk = desk_rows(s, [one] if one else sites, lines, pending)
            # The cockpit: the busiest and the most expensive sites, the latest news.
            busiest = sorted((x for x in desk if x["news"] or x["proposals_week"] or x["drafts_week"]),
                             key=lambda x: (-(x["news"] + 5 * x["proposals_week"] + 10 * x["drafts_week"]), x["domain"]))[:5]
            costly = sorted((x for x in desk if x["calls"]), key=lambda x: (-x["cost"], -x["tokens"]))[:5]
            u = m.llm_usage.c
            month = s.execute(select(func.coalesce(func.sum(u.cost), 0.0), func.count()).where(
                u.at >= datetime.now(UTC) - timedelta(days=30), u.error.is_(None), *of(u.site_id))).one()
            si = m.source_items
            news_day = s.execute(select(func.count()).select_from(si).where(
                repo.followed_by(sid), si.c.fetched_at >= datetime.now(UTC) - timedelta(hours=24))).scalar() or 0
            latest = s.execute(
                select(si.c.title, si.c.url, si.c.published_at, si.c.fetched_at, m.sources.c.name.label("source"))
                .join(m.sources, m.sources.c.id == si.c.source_id)
                .where(repo.followed_by(sid), si.c.duplicate_of.is_(None))
                .order_by(si.c.fetched_at.desc(), si.c.id.desc()).limit(6)).all()
            return render(request, "today.html.j2", c, s, "today", _sites=(sites, site), desk=desk, one=one,
                          busiest=busiest, costly=costly, cost_month=month[0], calls_month=month[1],
                          news_day=news_day, latest=latest,
                          activity=act.recent(s, zone(s), problem, full=True, site=sid),
                          profile_drafts=profile_drafts, lines=lines, site_lines=site_lines,
                          proposed=proposed, explained=explained, pending_pubs=pending_pubs,
                          stopped=stopped, drafts_ready=sum(draft_counts.values()),
                          needs_review=draft_counts.get("needs_review", 0), new_today=new_today,
                          usage=usage, last_fetch=last_fetch, core_trouble=core_trouble, briefing=briefing, decisions=decisions,
                          proposed_total=proposed_total, collected=collected)

    def draft_jobs(s, proposal_ids: list[int]) -> dict[int, dict]:
        """The latest draft job of each accepted proposal still without a
        draft: queued, running or failed, with its error."""
        if not proposal_ids:
            return {}
        j = m.jobs.c
        out = {}
        for r in s.execute(select(j.subject, j.status, j.error).where(
                j.kind == "draft", j.subject.in_([f"proposal {i}" for i in proposal_ids])).order_by(j.id)).all():
            out[int(r.subject.split()[-1])] = {"status": r.status, "error": problem(r.error) if r.error else None}
        return out

    def desk_rows(s, sites, lines, pending) -> list[dict]:
        """One row per site on the Desk: where it stands, its numbers, the
        work running on it and the next step."""
        day_ago = datetime.now(UTC) - timedelta(hours=24)
        week_ago = datetime.now(UTC) - timedelta(days=7)
        j = m.jobs.c
        active = s.execute(select(j.kind, j.subject, j.args).where(j.status.in_(("queued", "running")))).all()
        u = m.llm_usage.c
        # What each site cost in the last 30 days, at the prices of core.yaml.
        spent = {r.site_id: r for r in s.execute(
            select(u.site_id, func.coalesce(func.sum(u.cost), 0.0).label("cost"), func.count().label("calls"),
                   func.coalesce(func.sum(u.input_tokens), 0).label("tin"),
                   func.coalesce(func.sum(u.output_tokens), 0).label("tout"))
            .where(u.at >= datetime.now(UTC) - timedelta(days=30), u.error.is_(None)).group_by(u.site_id)).all()}
        rows = []
        for x in sites:
            def count(table, *where):
                return s.execute(select(func.count()).select_from(table).where(*where)).scalar() or 0
            working = {r.kind for r in active if r.subject == x.domain or (r.args or {}).get("site_id") == x.id
                       or r.kind in ("collect", "tick", "draft")}
            steps = first_steps(s, sites, lines, x.id, working)
            nxt = next((st for st in steps if st["current"]), None) if steps else None
            rows.append({
                "id": x.id, "domain": x.domain, "approved": lines.get(x.id), "pending": pending.get(x.id),
                "sources": count(m.sources, m.sources.c.site_id == x.id, m.sources.c.status == "active"),
                "candidates": count(m.sources, m.sources.c.site_id == x.id, m.sources.c.status == "candidate"),
                "news": count(m.source_items, repo.followed_by(x.id), m.source_items.c.fetched_at >= day_ago),
                "proposals": count(m.proposals, m.proposals.c.site_id == x.id, m.proposals.c.status == "proposed"),
                "drafts": count(m.drafts, m.drafts.c.site_id == x.id, m.drafts.c.status.in_(("draft", "needs_review"))),
                "proposals_week": count(m.proposals, m.proposals.c.site_id == x.id, m.proposals.c.created_at >= week_ago),
                "drafts_week": count(m.drafts, m.drafts.c.site_id == x.id, m.drafts.c.created_at >= week_ago),
                "cost": spent[x.id].cost if x.id in spent else 0.0, "calls": spent[x.id].calls if x.id in spent else 0,
                "tokens": (spent[x.id].tin + spent[x.id].tout) if x.id in spent else 0,
                "next": nxt, "working": bool(working - {"collect", "tick", "draft"}),
            })
        return rows

    def first_steps(s, sites, lines, sid=None, working=None) -> list[dict] | None:
        """The way from a domain to the first article, step by step, with
        what the newsroom is waiting for; None once every step is done.
        Sources and news are those of the site in view, like the Fonti page."""
        def count(table, *where):
            return s.execute(select(func.count()).select_from(table).where(*where)).scalar() or 0
        of_site = m.sources.c.site_id == sid
        site_items = repo.followed_by(sid)
        if working is None:
            working = set(s.execute(select(m.jobs.c.kind).where(m.jobs.c.status.in_(("queued", "running")))).scalars())
        of = (lambda col: col == sid) if sid is not None else (lambda col: True)
        steps = [
            {"title": t("Aggiungi il sito"), "past": t("Sito aggiunto"), "done": bool(sites), "href": None,
             "busy": "analysis" in working and not sites,
             "wait": t("Scansione del sito in corso: legge fino a 25 articoli, di solito pochi minuti."),
             "todo": t("Scrivi il dominio qui sotto: la redazione legge il sito e ne ricava temi, pubblico e tono.")},
            {"title": t("Approva il profilo"), "past": t("Profilo approvato"), "done": bool(lines.get(sid) if sid else lines), "href": "/site", "busy": "analysis" in working,
             "wait": t("Analisi in corso: il profilo da approvare arriva appena finisce."),
             "todo": t("Controlla temi e articoli d'esempio, togli i temi che non vuoi e approva.")},
            {"title": t("Attiva le fonti"), "past": t("Fonti attivate"), "done": count(m.sources, m.sources.c.status == "active", of_site) > 0, "href": "/sources",
             "busy": "discovery" in working, "wait": t("Ricerca delle fonti in corso dopo l'approvazione del profilo."),
             "todo": t("Guarda origine, ultime notizie e punteggio delle candidate e attiva quelle che vuoi.")},
            {"title": t("Raccogli le notizie"), "past": t("Notizie raccolte"), "done": count(m.source_items, site_items) > 0, "href": "/trends", "busy": True,
             "wait": t("Parte da sola ogni ora dalle fonti attive, oppure subito con \"Raccogli le notizie\"."),
             "todo": ""},
            {"title": t("Accetta una proposta"), "past": t("Proposta accettata"), "done": count(m.proposals, of(m.proposals.c.site_id), m.proposals.c.status.in_(("accepted", "drafted"))) > 0,
             "href": "/trends", "busy": not count(m.proposals, of(m.proposals.c.site_id), m.proposals.c.status == "proposed"),
             "wait": (t("Hai scartato le proposte arrivate finora. ") if count(m.proposals, of(m.proposals.c.site_id), m.proposals.c.status == "rejected") else "")
             + t("Le prossime arrivano domattina all'ora del briefing, oppure subito con \"Proponi adesso\" in "
                 "Trend e piano: prima i temi raccontati da più fonti, poi le notizie singole più adatte al sito."),
             "todo": t("Accetta le proposte che ti interessano e scarta le altre.")},
            {"title": t("Rileggi la prima bozza"), "past": t("Prima bozza approvata"), "done": count(m.drafts, of(m.drafts.c.site_id), m.drafts.c.status == "approved") > 0,
             "href": "/drafts", "busy": "draft" in working, "wait": t("La bozza è in scrittura."),
             "todo": t("Quando accetti una proposta la bozza si scrive da sola: rileggila, guarda fonti e controlli automatici e approvala.")},
            {"title": t("Collega il sito per pubblicare"), "past": t("Sito collegato"), "done": count(m.publish_targets, of(m.publish_targets.c.site_id)) > 0, "href": "/publications",
             "busy": False, "wait": "", "todo": t("Aggiungi WordPress, GitHub, un webhook o un canale Telegram: nulla esce senza il tuo sì.")},
        ]
        if all(x["done"] for x in steps):
            return None
        current = next(i for i, x in enumerate(steps) if not x["done"])
        for i, x in enumerate(steps):
            x["current"] = i == current
        return steps

    # --- Sito e profilo -------------------------------------------------------------

    def site_numbers(s, site) -> dict:
        """Everything a site has, in numbers, from profile to publications:
        the top of its page, each number opening the page with the list."""
        def count(table, *where):
            return s.execute(select(func.count()).select_from(table).where(*where)).scalar() or 0
        now = datetime.now(UTC)
        day_ago, week_ago, month_ago = now - timedelta(hours=24), now - timedelta(days=7), now - timedelta(days=30)
        src, it, pr, dr = m.sources.c, m.source_items.c, m.proposals.c, m.drafts.c
        p, pt = m.publications.c, m.publish_targets.c
        of = src.site_id == site.id
        statuses = dict(s.execute(select(src.status, func.count()).where(of).group_by(src.status)).all())
        planned = dict(s.execute(select(pr.status, func.count()).where(pr.site_id == site.id).group_by(pr.status)).all())
        drafts = dict(s.execute(select(dr.status, func.count()).where(dr.site_id == site.id).group_by(dr.status)).all())
        pubs = dict(s.execute(select(p.status, func.count()).select_from(
            m.publications.join(m.publish_targets, pt.id == p.target_id))
            .where(pt.site_id == site.id).group_by(p.status)).all())
        u = m.llm_usage.c
        cost = s.execute(select(func.coalesce(func.sum(u.cost), 0.0), func.count()).where(
            u.site_id == site.id, u.at >= month_ago, u.error.is_(None))).one()
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site.id, m.editorial_profiles.c.status == "approved")).scalar()
        return {
            "versions": count(m.editorial_profiles, m.editorial_profiles.c.site_id == site.id),
            "sources": statuses, "news_day": count(m.source_items, repo.followed_by(site.id), it.fetched_at >= day_ago),
            "news_week": count(m.source_items, repo.followed_by(site.id), it.fetched_at >= week_ago),
            "proposals": planned, "proposals_today": count(m.proposals, pr.site_id == site.id, pr.created_at >= start_of_day(s)),
            "per_day": ((body or {}).get("settings") or {}).get("proposals_per_day", props.PER_DAY) if body else None,
            "cover_style": ((body or {}).get("settings") or {}).get("cover_style") or "impact",
            "cover_text": bool(((body or {}).get("settings") or {}).get("cover_text")),
            "sources_style": ((body or {}).get("settings") or {}).get("sources_style") or "notes",
            "auto": autopilot.settings_of(body or s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site.id, m.editorial_profiles.c.status == "draft")
                .order_by(m.editorial_profiles.c.version.desc()).limit(1)).scalar()),
            "auto_today": autopilot.today(s, site.id, start_of_day(s)),
            "auto_allowed": quotas.autopilot_allowed(s),
            "auto_targets": s.execute(select(pt.id, pt.name, pt.kind).where(
                pt.site_id == site.id, pt.enabled.is_not(False)).order_by(pt.id)).all(),
            "drafts": drafts, "publications": pubs,
            "targets": count(m.publish_targets, pt.site_id == site.id, pt.enabled.is_not(False)),
            "cost": cost[0], "calls": cost[1],
            "discards": props.discards(s, site.id, month_ago),
        }

    def site_page(request, c, s, sites, site, editing: bool = False, status_code: int = 200, error: str | None = None):
        p = m.editorial_profiles
        profiles = s.execute(select(p).where(p.c.site_id == site.id).order_by(p.c.version.desc())).all() if site else []
        approved = next((x for x in profiles if x.status == "approved"), None)
        draft = next((x for x in profiles if x.status == "draft"), None)
        analysis = s.execute(select(m.site_analyses).where(m.site_analyses.c.site_id == site.id)
                             .order_by(m.site_analyses.c.id.desc()).limit(1)).first() if site else None
        shown = draft or approved
        # Titles of the articles read, to show which ones stand for each topic.
        titles = {x.get("url"): x.get("title") for x in ((analysis.snapshot or {}).get("posts", []) if analysis else [])}
        rows = []
        if shown is not None:
            before = {x["name"]: x.get("share") for x in (approved.body.get("subtopics", []) if approved and draft else [])}
            names = [x["name"] for x in shown.body.get("subtopics", [])]
            names += [n for n in before if n not in names]
            now_by = {x["name"]: x for x in shown.body.get("subtopics", [])}
            for n in names:
                new = now_by.get(n, {}).get("share")
                old = before.get(n)
                examples = [{"url": u, "title": titles.get(u) or u} for u in now_by.get(n, {}).get("evidence", [])[:2]
                            if isinstance(u, str)]
                rows.append({"name": n, "old": old, "new": new or 0.0, "examples": examples,
                             "keywords": now_by.get(n, {}).get("keywords", [])[:6],
                             "delta": None if old is None or new is None else round((new - old) * 100),
                             "fresh": bool(draft and approved and old is None), "current": n in now_by})
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
                      numbers=site_numbers(s, site) if site else None,
                      comparison=prof.compare(approved.body, draft.body) if approved and draft else [],
                      jobs=[j for j in jobs.recent(s, 10) if j["status"] != "done"][:5],
                      domain_free=manage.can_change_domain(s, site.id) if site else False,
                      langs=tr.LANGUAGES, own_language=tr.main_language(s, site.id) if site else None,
                      site_langs=tr.site_languages(s, site.id) if site else [], max_languages=tr.allowed(s),
                      feed_token=delivery.current_token(s, site.id) if site else None,
                      feed_base=(getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/"),
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
        try:
            ctx.run_analysis(c.tenant_id, d)
        except trials.TrialUsed:
            return done("/settings#h-sites", t("{domain} ha già avuto la prova gratuita in un'altra redazione: per lavorarci scegli un piano in Abbonamento.", domain=d))
        except quotas.QuotaExceeded:
            return done("/settings#h-sites", t("Il piano non ha posto per un altro sito: scegline uno più grande in Abbonamento."))
        with db.tenant(c.tenant_id) as s:
            sid = s.execute(select(m.sites.c.id).where(m.sites.c.domain == d)).scalar()
        resp = done("/", t("Analisi di {domain} partita: la segui passo per passo nel riquadro Attività.", domain=d))
        if sid is not None:
            choose_site(resp, sid)
        return resp

    @app.post("/ui/sites/{site_id}/review")
    def ui_review(site_id: int, back: str = "/site", c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise HTTPException(404, "site not found")
        ctx.run_analysis(c.tenant_id, domain)
        return done(safe_path(back, "/site"),
                    t("Analisi di {domain} partita: la segui passo per passo nel riquadro Attività.", domain=domain))

    @app.post("/ui/sites/{site_id}/approve/{version}")
    def ui_approve(site_id: int, version: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if not prof.approve(s, site_id, version, c.name):
                raise HTTPException(404, "version not found")
        ctx.run_discovery(c.tenant_id, site_id)
        return done("/site", t("Versione {version} approvata: è la linea del sito da ora. Cerco le fonti adatte.", version=version))

    @app.post("/ui/sites/{site_id}/per-day")
    def ui_per_day(site_id: int, n: int = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if not prof.set_proposals_per_day(s, site_id, n):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site", t("Da ora {n} proposte al giorno per questo sito.", n=max(0, min(50, n))))

    @app.post("/ui/sites/{site_id}/cover")
    def ui_cover(site_id: int, style: str = Form(...), text: str = Form(""),
                 c: auth.Caller = Depends(ctx.need("owner"))):
        if style not in ("impact", "editorial"):
            raise HTTPException(400, "unknown style")
        with db.tenant(c.tenant_id) as s:
            if not prof.set_cover(s, site_id, style, bool(text)):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site", t("Stile delle copertine salvato: vale dalla prossima immagine."))

    @app.post("/ui/sites/{site_id}/auto")
    def ui_auto(site_id: int, brief: str = Form(""), sections: str = Form(""), directives: str = Form(""),
                avoid: str = Form(""), per_day: int = Form(6), min_opportunity: float = Form(40),
                min_sources: int = Form(2), start: int = Form(7), end: int = Form(23), target_id: str = Form(""),
                enabled: str = Form(""), credit: str = Form(""), language: str = Form("it"),
                action: str = Form("save"), c: auth.Caller = Depends(ctx.need("owner"))):
        def lines(x: str) -> list[str]:
            return [y.strip() for y in re.split(r"[\n,;]", x) if y.strip()]
        values = {"enabled": bool(enabled), "brief": brief.strip(), "sections": lines(sections),
                  "directives": directives.strip(), "avoid": lines(avoid), "per_day": per_day,
                  "min_opportunity": min_opportunity, "min_sources": min_sources, "hours": [start, end],
                  "target_id": int(target_id) if target_id.isdigit() else None, "credit": bool(credit)}
        try:
            values = autopilot.check_settings(values)
        except ValueError as e:
            return done("/site#h-auto", t("Impostazioni non salvate: {e}.", e=e))
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
            saved = autopilot.save(s, site_id, values)
        if action == "profile":
            if not values["brief"]:
                return done("/site#h-auto", t("Scrivi prima il contesto: il profilo si ricava da lì."))
            queue.enqueue(db, c.tenant_id, "brief", f"site {site_id}",
                          {"site_id": site_id, "language": language if language in autopilot.LANGS else "it",
                           "auto": values})
            return done("/site", t("Il modello scrive il profilo dal contesto: lo segui nel riquadro Attività, "
                                   "poi lo approvi qui sotto."))
        if not saved:
            return done("/site#h-auto", t("Il sito non ha ancora un profilo: premi \"Crea il profilo dal contesto\"."))
        if values["enabled"]:
            return done("/site#h-auto", t("Redazione automatica accesa: dal prossimo giro orario sceglie, scrive e "
                                          "pubblica fino a {n} articoli al giorno, dalle {start} alle {end}.",
                                          n=values["per_day"], start=values["hours"][0], end=values["hours"][1]))
        return done("/site#h-auto", t("Impostazioni salvate. La redazione automatica è spenta."))

    @app.post("/ui/sites/{site_id}/auto/stop")
    def ui_auto_stop(site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
            if body is not None:
                autopilot.save(s, site_id, {**autopilot.settings_of(body), "enabled": False})
        return done("/site#h-auto", t("Redazione automatica fermata: non parte nessun articolo nuovo. Quelli già in "
                                      "scrittura restano bozze da rileggere."))

    @app.post("/ui/sites/{site_id}/sources-style")
    def ui_sources_style(site_id: int, style: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        if style not in ("notes", "list"):
            raise HTTPException(400, "unknown style")
        with db.tenant(c.tenant_id) as s:
            if not prof.set_sources_style(s, site_id, style):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site", t("Fonti negli articoli salvate: valgono dalla prossima pubblicazione."))

    @app.post("/ui/sites/{site_id}/discard/{version}")
    def ui_discard(site_id: int, version: int, c: auth.Caller = Depends(ctx.need("owner"))):
        p = m.editorial_profiles
        with db.tenant(c.tenant_id) as s:
            n = s.execute(update(p).where(p.c.site_id == site_id, p.c.version == version, p.c.status == "draft")
                          .values(status="retired")).rowcount
        if not n:
            raise HTTPException(409, "not a draft version")
        return done("/site", t("Proposta v{version} scartata: la linea resta quella approvata.", version=version))

    @app.post("/ui/sites/{site_id}/topics/remove")
    def ui_remove_topic(site_id: int, name: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            version = prof.remove_topic(s, c.tenant_id, site_id, name, c.name)
        if version is None:
            raise HTTPException(404, "topic not found")
        return done("/site", t("Tema «{name}» tolto (v{version}): non viene più seguito, anche nelle prossime analisi.", name=name, version=version))

    @app.post("/ui/sites/{site_id}/topics/restore")
    def ui_restore_topic(site_id: int, name: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            version = prof.restore_topic(s, c.tenant_id, site_id, name, c.name)
        if version is None:
            raise HTTPException(404, "topic not found")
        return done("/site", t("Tema «{name}» rimesso (v{version}).", name=name, version=version))

    @app.post("/ui/sites/{site_id}/versions/{version}/delete")
    def ui_delete_version(site_id: int, version: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            why = prof.delete_version(s, site_id, version)
        if why == "not found":
            raise HTTPException(404, "version not found")
        if why == "approved":
            return done("/site", t("La v{version} è la linea in uso: approvane un'altra prima di eliminarla.", version=version))
        if why == "used":
            return done("/site", t("La v{version} ha già prodotto proposte e punteggi: resta nello storico.", version=version))
        return done("/site", t("Versione v{version} eliminata.", version=version))

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
                                 error=t("Profilo non valido: {e}", e=e))
            value["domain"] = site.domain
            pid = prof.edit(s, c.tenant_id, site_id, value)
            version = s.execute(select(m.editorial_profiles.c.version).where(m.editorial_profiles.c.id == pid)).scalar()
        return done("/site", t("Modifiche salvate come bozza v{version}: approvala per renderla attiva.", version=version))

    @app.get("/guide", response_class=HTMLResponse)
    def guide(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            return render(request, "guide.html.j2", c, s, "guide")

    # --- Fonti --------------------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    def sources_view(request: Request, status: str = "active", c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            rows, counts = [], {}
            if site:
                src = m.sources
                a = m.sources.alias("a")
                counts = dict(s.execute(select(src.c.status, func.count()).where(src.c.site_id == site.id)
                                        .group_by(src.c.status)).all())
                # Each source with what the shared archive knows of it: when it
                # last brought news, its last read error, a suspension for everyone.
                q = (select(src, a.c.last_item_at.label("shared_last_item_at"), a.c.last_error.label("shared_error"),
                            a.c.status.label("shared_status"), a.c.status_reason.label("shared_reason"))
                     .outerjoin(a, a.c.id == src.c.archive_id)
                     .where(src.c.site_id == site.id).order_by(src.c.score.desc().nulls_last()))
                if status != "all":
                    q = q.where(src.c.status == status)
                rows = s.execute(q.limit(500)).all()
            # When this site has none, say which other sites do, so a link
            # from the home page never lands on an empty list without a way on.
            elsewhere = []
            if site and not rows:
                src = m.sources
                q = (select(m.sites.c.id, m.sites.c.domain, func.count()).join(src, src.c.site_id == m.sites.c.id)
                     .where(m.sites.c.id != site.id).group_by(m.sites.c.id, m.sites.c.domain).order_by(m.sites.c.domain))
                if status != "all":
                    q = q.where(src.c.status == status)
                elsewhere = [{"id": i, "domain": d, "count": n} for i, d, n in s.execute(q)]
            # How much each source brought in the last week: one that brings
            # nothing is easy to spot.
            # A source's news are in the archive (and, collected before it, its own).
            reads: dict[int, int] = {}
            for r in rows:
                reads[r.id] = r.id
                if r.archive_id:
                    reads[r.archive_id] = r.id
            week: dict[int, int] = {}
            if rows:
                it = m.source_items
                for sid, n in s.execute(select(it.c.source_id, func.count()).where(
                        it.c.source_id.in_(list(reads)), it.c.fetched_at >= datetime.now(UTC) - timedelta(days=7))
                        .group_by(it.c.source_id)):
                    week[reads[sid]] = week.get(reads[sid], 0) + n
            # The latest news of each source shown: what it actually brings.
            latest: dict[int, list] = {}
            if rows:
                it = m.source_items
                for r in s.execute(select(it.c.source_id, it.c.title, it.c.url)
                                   .where(it.c.source_id.in_(list(reads)))
                                   .order_by(it.c.published_at.desc().nulls_last()).limit(3000)):
                    if len(latest.setdefault(reads[r.source_id], [])) < 3:
                        latest[reads[r.source_id]].append({"title": r.title, "url": r.url})
            return render(request, "sources.html.j2", c, s, "sources", _sites=(sites, site), rows=rows,
                          counts=counts, total=sum(counts.values()), status=status, origins=ORIGINS, kinds=KINDS,
                          usage=quotas.usage(s), latest=latest, elsewhere=elsewhere, week=week)

    @app.post("/ui/sources/{source_id}/status")
    def ui_source_status(source_id: int, status: str = Form(...), back: str = Form("/sources"),
                         c: auth.Caller = Depends(ctx.need("owner"))):
        from . import repo
        if status not in ("active", "suspended", "rejected"):
            raise HTTPException(400, "unknown status")
        with db.tenant(c.tenant_id) as s:
            name = s.execute(select(m.sources.c.name).where(m.sources.c.id == source_id, repo.own())).scalar()
            if name is None:
                raise HTTPException(404, "source not found")
            if status == "active":
                quotas.check(s, "sources")
            repo.set_source_status(s, source_id, status, f"set by {c.name}", by=c.name)
        notice = {"active": t("{name} riattivata.", name=name), "suspended": t("{name} sospesa.", name=name),
                  "rejected": t("{name} scartata.", name=name)}[status]
        return done(safe_path(back, "/sources"), notice)

    @app.post("/ui/sites/{site_id}/discover")
    def ui_discover(site_id: int, back: str = "/sources?status=candidate", c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
        ctx.run_discovery(c.tenant_id, site_id)
        return done(safe_path(back, "/sources?status=candidate"),
                    t("Ricerca delle fonti partita: la segui nel riquadro Attività. Le trovate entrano come candidate."))

    # --- Trend e piano --------------------------------------------------------------

    @app.get("/trends", response_class=HTMLResponse)
    def trends_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            topics = topic_scores(s, site.id if site else None, 60) if site else []
            dots = [t for t in topics if t.hype is not None and t.opportunity is not None]
            plan = proposal_rows(s, site.id, ("proposed", "accepted", "drafted"), 20) if site else []
            # Written drafts stay in the plan for a week, then live under Bozze.
            week = datetime.now(UTC) - timedelta(days=7)
            plan = [p for p in plan if p.status != "drafted" or p.created_at > week]
            # The trend of the moment: the archive's latest snapshot, the
            # topics in it that suit this site, and titles to choose from.
            archive = repo.archive_of(db, c.tenant_id)
            snap = trends.latest(s, archive)
            day = trends.today(s, archive, start_of_day(s))
            moment = []
            if site and snap:
                fit = {x.id: x.opportunity for x in topics}
                for x in snap.topics:
                    if (fit.get(x["topic_id"]) or 0) >= props.MIN_OPPORTUNITY and len(moment) < 3:
                        moment.append(dict(x, opportunity=fit[x["topic_id"]]))
                offered = proposal_rows(s, site.id, ("proposed",), 60)
                for x in moment:
                    x["titles"] = [p for p in offered if p.topic_id == x["topic_id"]]
                shown = {p.id for x in moment for p in x["titles"]}
                plan = [p for p in plan if p.id not in shown]
            # What waits for a decision first, then what is being written, then the rest.
            order = {"proposed": 0, "accepted": 1, "drafted": 2}
            plan = sorted(plan, key=lambda p: order.get(p.status, 3))
            explained = proposal_details(s, plan)
            hour = s.execute(select(m.tenants.c.briefing_hour)).scalar()
            return render(request, "trends.html.j2", c, s, "trends", _sites=(sites, site), topics=topics,
                          dots=dots, plan=plan, explained=explained, briefing_hour=hour, can_draft=ctx.core is not None,
                          writing=draft_jobs(s, [p.id for p in plan if p.status == "accepted" and not p.draft_id]),
                          snap=snap, day=day, moment=moment, local=lambda at: at.astimezone(zone(s)))

    @app.get("/index", response_class=HTMLResponse)
    def news_index(request: Request, at: int | None = None, section: str = "", c: auth.Caller = Depends(viewer)):
        """The news index: the ranking of the moment, every three hours from
        07:00, by section, each topic with its summary and news."""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            archive = repo.archive_of(db, c.tenant_id)
            day = trends.today(s, archive, start_of_day(s))
            snap = next((d for d in day if d.id == at), None) if at else None
            snap = snap or trends.latest(s, archive)
            topics = list(snap.topics) if snap else []
            counts = {k: sum(1 for x in topics if (x.get("section") or "other") == k)
                      for k in (*news_base.SECTIONS, "other")}
            if section in counts:
                topics = [x for x in topics if (x.get("section") or "other") == section]
            return render(request, "index.html.j2", c, s, "index", _sites=(sites, site), snap=snap, day=day,
                          topics=topics, counts=counts, section=section if section in counts else "",
                          local=lambda v: v.astimezone(zone(s)))

    @app.post("/ui/trends/{topic_id}/titles")
    def ui_titles(request: Request, topic_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        """Titles to choose from for one trend of the moment."""
        with db.tenant(c.tenant_id) as s:
            _, site = current_site(s, request)
            if site is None:
                return done("/trends", t("Aggiungi prima un sito."))
            if s.execute(select(m.topics.c.id).where(m.topics.c.id == topic_id)).scalar() is None:
                raise HTTPException(404, "topic not found")
        ctx.run_titles(c.tenant_id, site.id, topic_id)
        return done("/trends#moment", t("Sto preparando i titoli: compaiono qui tra qualche minuto."))

    @app.post("/ui/proposals/now")
    def ui_propose_now(request: Request, c: auth.Caller = Depends(ctx.need("editor"))):
        """The day's proposals now, without waiting for the briefing."""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            wanted = request.query_params.get("site")
            if wanted:  # from the Desk, one row per site
                site = next((x for x in sites if str(x.id) == wanted), None)
            if site is None:
                return done("/trends", t("Aggiungi prima un sito."))
            if not s.execute(select(m.editorial_profiles.c.id).where(m.editorial_profiles.c.site_id == site.id,
                                                                       m.editorial_profiles.c.status == "approved")).first():
                return done("/trends", t("Approva prima il profilo del sito."))
            since = start_of_day(s)
        # Asked for by a person: never held back by the day's number.
        ctx.run_proposals(c.tenant_id, site.id, since, manual=True)
        return done(safe_path(request.query_params.get("back"), "/trends"),
                    t("Proposte per {domain} in preparazione: le segui nel riquadro Attività.", domain=site.domain))

    @app.post("/ui/collect")
    def ui_collect(c: auth.Caller = Depends(ctx.need("editor"))):
        """Read the sources now, then topics, Hype, the trend of the moment and Opportunity."""
        ctx.run_collect(c.tenant_id)
        return done("/", t("Raccolta partita: la segui passo per passo nel riquadro Attività."))

    @app.post("/ui/proposals/{proposal_id}")
    def ui_decide(proposal_id: int, accept: str = Form(...), back: str = Form("/trends"), reason: str = Form(""),
                  c: auth.Caller = Depends(ctx.need("editor"))):
        if not props.decide(db, c.tenant_id, proposal_id, accept == "1", c.name, reason=reason or None):
            return done(safe_path(back, "/trends"), t("Questa proposta era già stata decisa."))
        if accept == "1" and ctx.core is not None:
            ctx.run_draft(c.tenant_id, proposal_id)
            return done(safe_path(back, "/trends"), t("Proposta accettata: la bozza è in scrittura, la segui nel riquadro Attività."))
        if accept != "1" and reason in props.REASONS:
            return done(safe_path(back, "/trends"), {
                "already_covered": t("Proposta scartata: i temi già trattati sul sito scendono nelle prossime proposte."),
                "off_topic": t("Proposta scartata: i temi come questo scendono nelle prossime proposte."),
                "too_generic": t("Proposta scartata: le prossime proposte saranno più specifiche."),
                "unreliable_source": t("Proposta scartata: le fonti che citava valgono meno per questo sito."),
            }[reason])
        return done(safe_path(back, "/trends"), t("Proposta accettata.") if accept == "1" else t("Proposta scartata."))

    @app.post("/ui/proposals/{proposal_id}/draft")
    def ui_draft(proposal_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        if ctx.core is None:
            return done("/trends", t("Nessun Core configurato: per scrivere le bozze serve un modello."))
        with db.tenant(c.tenant_id) as s:
            st = s.execute(select(m.proposals.c.status).where(m.proposals.c.id == proposal_id)).scalar()
            only = quotas.proposals_only(s)
        if only:
            return done("/plan", t("La prova è finita: per scrivere le bozze scegli un piano."))
        if st not in ("accepted", "drafted"):
            return done("/trends", t("Accetta prima la proposta."))
        ctx.run_draft(c.tenant_id, proposal_id)
        return done("/drafts", t("Bozza in scrittura: compare qui quando è pronta."))

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
                                        d.c.language, d.c.translation_of,
                                        claims.label("claims")).where(d.c.site_id == site.id)
                                 .order_by(d.c.created_at.desc()).limit(100)).all()
            return render(request, "drafts.html.j2", c, s, "drafts", _sites=(sites, site), rows=rows, langs=tr.LANGUAGES)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_view(request: Request, draft_id: int, edit: int = 0, c: auth.Caller = Depends(viewer)):
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
            checks = [{"label": t(label), "ok": key not in problems} for key, label in CHECKS]
            targets = s.execute(select(m.publish_targets).where(m.publish_targets.c.site_id == d.site_id,
                                                                m.publish_targets.c.enabled.is_not(False))).all()
            pubs = s.execute(select(m.publications.c.status, m.publications.c.mode, m.publications.c.external_url,
                                    m.publish_targets.c.name)
                             .join(m.publish_targets, m.publish_targets.c.id == m.publications.c.target_id)
                             .where(m.publications.c.draft_id == draft_id).order_by(m.publications.c.id)).all()
            sites, site = current_site(s, request)
            # The original and its translations, and the site's languages still missing.
            root = d.translation_of or d.id
            family = s.execute(select(m.drafts.c.id, m.drafts.c.language, m.drafts.c.status, m.drafts.c.title,
                                      m.drafts.c.translation_of)
                               .where(or_(m.drafts.c.id == root, m.drafts.c.translation_of == root))
                               .order_by(m.drafts.c.id)).all()
            original = next((x for x in family if x.id == root), None)
            j = m.jobs.c
            translating = {a.get("language") for a in s.execute(select(j.args).where(
                j.kind == "translate", j.status.in_(("queued", "running")))).scalars() if a and a.get("draft_id") == root}
            img_busy = any(a and a.get("draft_id") == root for a in s.execute(select(j.args).where(
                j.kind == "image", j.status.in_(("queued", "running")))).scalars())
            image = images.of(s, d.id)
            have = {x.language for x in family}
            wanted = [x for x in tr.site_languages(s, d.site_id) if x not in have]
            return render(request, "draft.html.j2", c, s, "drafts", _sites=(sites, site), d=d, sections=sections,
                          original=original, translations=[x for x in family if x.translation_of],
                          wanted=wanted, translating=translating, langs=tr.LANGUAGES, can_translate=ctx.core is not None,
                          image=image, img_busy=img_busy, images_on=getattr(ctx, "imager", None) is not None,
                          body_html=to_html(delivery.body_md(d.body_md)),
                          sources=[{"n": refs[i], **items[i]._mapping} for i in order if i in items],
                          flags=flags, checks=checks, targets=targets, pubs=pubs, kinds=TARGET_KINDS,
                          sd=d.seo or {}, editing=bool(edit) and c.can("editor"), claims=claims, flagged_ordinals=flagged, seo_checks=[{"label": t(SEO_CHECKS[x["key"]]), "ok": x["ok"]}
                                                      for x in (d.seo or {}).get("checks", []) if x.get("key") in SEO_CHECKS])

    @app.post("/ui/drafts/{draft_id}/decision")
    def ui_draft_decision(draft_id: int, accept: str = Form(...), c: auth.Caller = Depends(ctx.need("editor"))):
        if not drafts.decide(db, c.tenant_id, draft_id, accept == "1", c.name):
            return done(f"/drafts/{draft_id}", t("Questa bozza era già stata decisa."))
        if accept != "1":
            return done(f"/drafts/{draft_id}", t("Bozza rifiutata."))
        todo = ctx.run_translations(c.tenant_id, draft_id)
        mailed = getattr(ctx, "run_delivery", None) and ctx.run_delivery(c.tenant_id, draft_id)
        extra = (" " + t("La mandiamo anche per email ai destinatari della redazione, con il pacchetto da pubblicare.")
                 if mailed else "")
        if todo:
            return done(f"/drafts/{draft_id}", t("Bozza approvata. Traduzioni in scrittura ({languages}): le segui nel "
                                                 "riquadro Attività, ognuna conta come un articolo.",
                                                 languages=", ".join(tr.LANGUAGES[x] for x in todo)) + extra)
        return done(f"/drafts/{draft_id}", t("Bozza approvata: ora puoi chiederne la pubblicazione.") + extra)

    @app.get("/drafts/{draft_id}/package.zip")
    def ui_package(request: Request, draft_id: int, c: auth.Caller = Depends(ctx.need("viewer"))):
        base = (getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/")
        with db.tenant(c.tenant_id) as s:
            a = delivery.article(s, draft_id, base)
        if a is None:
            raise HTTPException(404, "draft not found")
        name, data = delivery.package(a)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/ui/sites/{site_id}/feed")
    def ui_feed(site_id: int, renew: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            delivery.feed_token(db, c.tenant_id, site_id, renew == "1")
        except LookupError:
            raise HTTPException(404, "site not found") from None
        msg = (t("Indirizzi del feed cambiati: quelli vecchi non funzionano più, aggiornali in Zapier, Make o n8n.")
               if renew == "1" else t("Feed creato: copia l'indirizzo nello strumento che pubblica per te."))
        return done("/site#h-delivery", msg)

    @app.get("/feeds/{name}")
    def feed(request: Request, name: str, lang: str | None = None):
        """The approved articles of a site, for the automation tools: the token
        in the address is the key, no login."""
        token, _, fmt = name.rpartition(".")
        if fmt not in ("json", "rss"):
            raise HTTPException(404, "feed not found")
        found = delivery.by_token(db, token)
        if found is None:
            raise HTTPException(404, "feed not found")
        base = (getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/")
        with db.tenant(found.tenant_id) as s:
            site = s.execute(select(m.sites.c.domain).where(m.sites.c.id == found.site_id)).scalar()
        arts = delivery.feed_articles(db, found.tenant_id, found.site_id, base, (lang or "").strip().lower()[:2] or None)
        url = f"{base}/feeds/{name}"
        headers = {"Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex"}
        if fmt == "json":
            return Response(json.dumps(delivery.json_feed(arts, site, url), ensure_ascii=False),
                            media_type="application/feed+json", headers=headers)
        return Response(delivery.rss(arts, site, url), media_type="application/rss+xml", headers=headers)

    @app.get("/media/{token}")
    def media(token: str):
        """A cover image, for the dashboard and for the sites that publish it:
        the token is the key, no login."""
        img = images.by_token(db, token.rsplit(".", 1)[0])
        if img is None:
            raise HTTPException(404, "image not found")
        return Response(bytes(img.content), media_type=img.mime,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.post("/ui/drafts/{draft_id}/image")
    def ui_image(draft_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        if getattr(ctx, "imager", None) is None:
            return done(f"/drafts/{draft_id}", t("Immagini non configurate: serve EDITOR_IMAGES_API_KEY sul server."))
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts.c.translation_of).where(m.drafts.c.id == draft_id)).first()
        if d is None:
            raise HTTPException(404, "draft not found")
        ctx.run_image(c.tenant_id, d.translation_of or draft_id)
        return done(f"/drafts/{draft_id}", t("Immagine in preparazione: la segui nel riquadro Attività. "
                                             "Vale anche per le traduzioni."))

    @app.post("/ui/drafts/{draft_id}/image/delete")
    def ui_image_delete(draft_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        with db.tenant(c.tenant_id) as s:
            root = s.execute(select(m.drafts.c.translation_of).where(m.drafts.c.id == draft_id)).scalar() or draft_id
            ids = [root] + list(s.execute(select(m.drafts.c.id).where(m.drafts.c.translation_of == root)).scalars())
        for i in ids:
            images.remove(db, c.tenant_id, i)
        return done(f"/drafts/{draft_id}", t("Immagine tolta: l'articolo esce senza copertina."))

    @app.post("/ui/drafts/{draft_id}/translate")
    def ui_translate(draft_id: int, language: str = Form(...), c: auth.Caller = Depends(ctx.need("editor"))):
        if ctx.core is None:
            return done(f"/drafts/{draft_id}", t("Nessun Core configurato: per tradurre serve un modello."))
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts.c.site_id, m.drafts.c.translation_of).where(m.drafts.c.id == draft_id)).first()
            if d is None:
                raise HTTPException(404, "draft not found")
            ok = language in tr.site_languages(s, d.site_id)
        if not ok:
            return done(f"/drafts/{draft_id}", t("Aggiungi prima la lingua al sito, in Sito e profilo."))
        ctx.run_translation(c.tenant_id, d.translation_of or draft_id, language)
        return done(f"/drafts/{draft_id}", t("Traduzione in {language} in scrittura: la segui nel riquadro Attività.",
                                             language=tr.LANGUAGES[language]))

    @app.post("/ui/sites/{site_id}/languages")
    async def ui_languages(request: Request, site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        form = await request.form()
        try:
            chosen = tr.set_languages(db, c.tenant_id, site_id, [str(x) for x in form.getlist("languages")])
        except LookupError:
            raise HTTPException(404, "site not found") from None
        except tr.LanguageError:
            with db.tenant(c.tenant_id) as s:
                limit = tr.allowed(s)
            return done("/site#h-languages", t("Il piano permette {n} lingue per sito, compresa la sua: togline qualcuna "
                                               "o passa a un piano più grande.", n=limit))
        if not chosen:
            return done("/site#h-languages", t("Gli articoli restano solo nella lingua del sito."))
        return done("/site#h-languages", t("Ogni articolo approvato sarà tradotto in: {languages}.",
                                           languages=", ".join(tr.LANGUAGES[x] for x in chosen)))

    @app.post("/ui/drafts/{draft_id}/publish")
    def ui_publish(draft_id: int, target_id: int = Form(...), mode: str = Form("draft"),
                   c: auth.Caller = Depends(ctx.need("editor"))):
        if mode not in ("draft", "publish"):
            raise HTTPException(400, "unknown mode")
        try:
            publishing.request(db, c.tenant_id, draft_id, target_id, mode, c.name)
        except publishing.PublishError as e:
            return done(f"/drafts/{draft_id}", t("Richiesta non inviata: {e}.", e=e))
        except IntegrityError:
            return done(f"/drafts/{draft_id}", t("Questa pubblicazione è già stata chiesta."))
        return done("/publications", t("Pubblicazione chiesta: aspetta l'approvazione di un direttore."))

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
                          targets=targets, kinds=TARGET_KINDS, stored=vault.stored(s), langs=tr.LANGUAGES,
                          secret_var=lambda name: _secret_var(c.tenant_id, name))

    @app.post("/ui/publications/{pub_id}/decision")
    def ui_pub_decision(pub_id: int, accept: str = Form(...),
                        c: auth.Caller = Depends(ctx.need("owner"))):
        ok = accept == "1"
        if not publishing.decide(db, c.tenant_id, pub_id, ok, c.name):
            return done("/publications", t("Questa pubblicazione era già stata decisa."))
        if ok:
            ctx.run_publication(c.tenant_id, pub_id)
        return done("/publications", t("Approvata: l'invio parte a breve.") if ok else t("Pubblicazione rifiutata."))

    @app.post("/ui/publications/{pub_id}/uncertain")
    def ui_pub_uncertain(pub_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        if not publishing.mark_uncertain_failed(db, c.tenant_id, pub_id, c.name):
            return done("/publications", t("Questa pubblicazione non è più in invio."))
        return done("/publications", t("Segnata come non riuscita: puoi chiederla di nuovo."))

    def _secret_var(tenant_id, name):
        try:
            return publishing.secret_variable(tenant_id, name)
        except publishing.PublishError:
            return t("{name} (nome non valido: ricrea la destinazione)", name=name)

    @app.post("/ui/targets")
    def ui_add_target(site_id: int = Form(...), kind: str = Form(...), name: str = Form(...),
                      address: str = Form(""), user: str = Form(""), secret_env: str = Form(""),
                      secret: str = Form(""), indexnow_key: str = Form(""), jsonld: str = Form(""),
                      branch: str = Form(""), path: str = Form(""), image_path: str = Form(""),
                      article_url: str = Form(""), verify: str = Form(""), language: str = Form(""),
                      c: auth.Caller = Depends(ctx.need("owner"))):
        if kind not in TARGET_KINDS:
            raise HTTPException(400, "unknown kind")
        if kind == "telegram_channel":
            config = {"chat_id": address.strip()}
        elif kind == "github":
            config = {"repo": address.strip(), "verify": bool(verify)}
            for key, value in (("branch", branch), ("path", path), ("image_path", image_path), ("url", article_url)):
                if value.strip():
                    config[key] = value.strip()
        else:
            config = {"url": address.strip()}
        if kind == "wordpress":
            config["user"] = user.strip()
            if jsonld:
                config["jsonld"] = True
        if language in tr.LANGUAGES:
            config["language"] = language
        key = indexnow_key.strip()
        if key:
            if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
                return done("/publications", t("La chiave IndexNow va da 8 a 128 caratteri tra lettere, numeri e trattini."))
            config["indexnow_key"] = key
        env_name = secret_env.strip() or None
        if env_name is not None and (len(env_name) > 40 or not publishing.SECRET_NAME.match(env_name)):
            return done("/publications", t("Il nome del segreto va scritto in MAIUSCOLO, es. WP_SITO."))
        if secret and env_name is None:
            return done("/publications", t("Dai un nome alla credenziale, es. WP_SITO."))
        try:
            with db.tenant(c.tenant_id) as s:
                if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                    raise HTTPException(404, "site not found")
            if secret:
                vault.put(db, c.tenant_id, env_name, secret)
            publishing.add_target(db, c.tenant_id, site_id, kind, name.strip()[:100], config, env_name)
        except (publishing.PublishError, vault.VaultError) as e:
            return done("/publications", t("Destinazione non aggiunta: {e}.", e=e))
        added = t("Destinazione {name} aggiunta.", name=name.strip()[:100])
        if env_name and secret:
            added += t(" La credenziale è salvata cifrata e non verrà più mostrata.")
        elif env_name:
            added += t(" Sul server la credenziale va nella variabile {var}.", var=publishing.secret_variable(c.tenant_id, env_name))
        return done("/publications", added)

    @app.post("/ui/secrets/{name}")
    def ui_put_secret(name: str, value: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            vault.put(db, c.tenant_id, name, value)
        except vault.VaultError as e:
            return done("/publications", t("Credenziale non salvata: {e}.", e=e))
        return done("/publications", t("Credenziale {name} aggiornata: è salvata cifrata e non verrà più mostrata.", name=name))

    # --- Impostazioni ---------------------------------------------------------------

    def settings_page(request, c, s, new_token: str | None = None, new_link: dict | None = None,
                      error: str | None = None, status_code: int = 200):
        usage = quotas.usage(s)
        recipients = s.execute(select(m.recipients).order_by(m.recipients.c.id)).all()
        t = m.access_tokens
        tokens = s.execute(select(t.c.id, t.c.name, t.c.role, t.c.created_at, t.c.revoked_at)
                           .order_by(t.c.revoked_at.is_not(None), t.c.created_at)).all() if c.can("owner") else []
        owner = c.can("owner")
        return render(request, "settings.html.j2", c, s, "settings", status_code=status_code, usage=usage,
                      recipients=recipients, tokens=tokens, new_token=new_token, new_link=new_link,
                      error=error, people=accounts.members(admin, c.tenant_id) if owner else [],
                      invited=accounts.pending_invites(admin, c.tenant_id) if owner else [])

    def meters_of(usage, money: bool) -> list[dict]:
        out = []
        if usage is not None:
            for what, (limit_col, used_col) in quotas.LIMITS.items():
                if what == "llm_cost" and not money:
                    continue
                used, limit = usage.used.get(used_col) or 0, usage.plan.get(limit_col)
                out.append({"label": QUOTA_LABELS[what], "used": used, "limit": limit, "money": what == "llm_cost",
                            "pct": min(100, round(100 * used / limit)) if limit else 0})
        return out

    @app.get("/plan", response_class=HTMLResponse)
    def plan_view(request: Request, c: auth.Caller = Depends(viewer)):
        """The newsroom's plan in its own menu: what it is, how much of it is
        used, and (for the owner) choosing it, paying it, the billing data."""
        with db.tenant(c.tenant_id) as s:
            usage = quotas.usage(s)
            room, email = None, ""
            if c.can("owner"):
                with accounts._read(admin) as conn:
                    email = conn.execute(select(m.users.c.email).where(m.users.c.id == c.user_id)).scalar() if c.user_id else None
                room = {"tenant_id": c.tenant_id, "active": True,
                        "subscription": billing.subscription(admin, c.tenant_id),
                        "customer": billing.customer_of(admin, c.tenant_id),
                        "reseller": billing.reseller_of_tenant(admin, c.tenant_id) is not None}
            return render(request, "plan.html.j2", c, s, "plan", usage=usage,
                          meters=meters_of(usage, money=c.operator), room=room, me_email=email,
                          plans=billing.plans(admin, public_only=True), money=billing.money,
                          online=ctx.gateway is not None)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            return settings_page(request, c, s)

    @app.post("/ui/recipients")
    def ui_add_recipient(channel: str = Form(...), address: str = Form(...),
                         c: auth.Caller = Depends(ctx.need("owner"))):
        address = address.strip()
        if channel not in ("email", "telegram") or not address or len(address) > 200:
            return done("/settings", t("Destinatario non valido."))
        if any(ch in address for ch in "\r\n\t,;<>"):
            return done("/settings", t("Destinatario non valido."))
        if channel == "email" and ("@" not in address or " " in address):
            return done("/settings", t("Indirizzo email non valido."))
        with db.tenant(c.tenant_id) as s:
            s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES (:t, :c, :a) "
                           "ON CONFLICT DO NOTHING"), {"t": c.tenant_id, "c": channel, "a": address})
        return done("/settings", t("Destinatario aggiunto: riceve il prossimo briefing."))

    @app.post("/ui/recipients/{rid}/delete")
    def ui_del_recipient(rid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            s.execute(m.recipients.delete().where(m.recipients.c.id == rid))
        return done("/settings", t("Destinatario rimosso."))

    @app.get("/ui/export")
    def ui_export(c: auth.Caller = Depends(ctx.need("owner"))):
        return export_response(db, c.tenant_id)

    @app.post("/ui/tokens", response_class=HTMLResponse)
    def ui_new_token(request: Request, name: str = Form(...), role: str = Form(...),
                     c: auth.Caller = Depends(ctx.need("owner"))):
        name = name.strip()[:100]
        if role not in auth.ROLES or not name:
            return done("/settings", t("Nome o ruolo non valido."))
        token = auth.create_token(db, c.tenant_id, name, role)
        with db.tenant(c.tenant_id) as s:
            return settings_page(request, c, s, new_token=token)

    @app.post("/ui/tokens/{tid}/revoke")
    def ui_revoke(tid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            tk = m.access_tokens
            n = s.execute(update(tk).where(tk.c.id == tid, tk.c.revoked_at.is_(None))
                          .values(revoked_at=datetime.now(UTC))).rowcount
        return done("/settings", t("Accesso revocato.") if n else t("Accesso già revocato."))

    # --- Persone della redazione ----------------------------------------------------

    @app.post("/ui/people/invite", response_class=HTMLResponse)
    def ui_invite(request: Request, email: str = Form(...), role: str = Form("editor"),
                  c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            token = accounts.create_invite(admin, email, c.name, tenant_id=c.tenant_id, role=role)
        except accounts.AccountError as e:
            with db.tenant(c.tenant_id) as s:
                return settings_page(request, c, s, error=t("Invito non creato: {e}.", e=e), status_code=400)
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
            return done("/settings", t("Ruolo non cambiato: {e}.", e=e))
        return done("/settings", t("Ruolo aggiornato."))

    @app.post("/ui/people/{uid}/remove")
    def ui_remove(uid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            accounts.remove_member(admin, c.tenant_id, uid)
        except accounts.AccountError as e:
            return done("/settings", t("Persona non rimossa: {e}.", e=e))
        return done("/settings", t("Persona rimossa dalla redazione: non entra più."))

    @app.post("/ui/invites/{sha}/revoke")
    def ui_revoke_invite(sha: str, c: auth.Caller = Depends(ctx.need("owner"))):
        n = accounts.revoke_invite(admin, c.tenant_id, sha)
        return done("/settings", t("Invito annullato.") if n else t("Invito già usato o scaduto."))

    # --- Change and delete what was created -------------------------------------------

    @app.post("/ui/sites/{site_id}/edit")
    def ui_edit_site(request: Request, site_id: int, name: str = Form(""), domain: str = Form(""),
                     c: auth.Caller = Depends(ctx.need("owner"))):
        from .api import normalise_domain
        with db.tenant(c.tenant_id) as s:
            old = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
            if old is None:
                raise HTTPException(404, "site not found")
            free = manage.can_change_domain(s, site_id)
        try:
            new = normalise_domain(domain) if domain.strip() else old
        except HTTPException:
            return done("/site", t("{domain} non è un dominio valido.", domain=domain.strip()[:100]))
        if new != old:
            if not free:
                return done("/site", t("Il profilo di {domain} è già approvato: per cambiare dominio elimina il sito "
                                       "e aggiungi quello giusto.", domain=old))
            with db.tenant(c.tenant_id) as s:
                if s.execute(select(m.sites.c.id).where(m.sites.c.domain == new)).scalar() is not None:
                    return done("/site", t("{domain} c'è già tra i siti della redazione.", domain=new))
            if ctx.admin is not None:
                try:
                    trials.check_domain(ctx.admin, c.tenant_id, new)
                except trials.TrialUsed:
                    d = new
                    return done("/site", t("{domain} ha già avuto la prova gratuita in un'altra redazione: per lavorarci scegli un piano in Abbonamento.", domain=d))
            try:
                manage.delete_site(db, c.tenant_id, site_id)
            except manage.ManageError as e:
                return done("/site", str(e))
            ctx.run_analysis(c.tenant_id, new)
            with db.tenant(c.tenant_id) as s:
                sid = s.execute(select(m.sites.c.id).where(m.sites.c.domain == new)).scalar()
            resp = done("/site", t("Dominio corretto da {old} a {new}: l'analisi riparte da capo, la segui in "
                                   "Attività.", old=old, new=new))
            if sid is not None:
                resp.set_cookie(SITE_COOKIE, str(sid), httponly=True, samesite="strict", max_age=365 * 86400)
            return resp
        try:
            manage.rename_site(db, c.tenant_id, site_id, name or old)
        except manage.ManageError as e:
            return done("/site", str(e))
        return done("/site", t("Sito aggiornato."))

    @app.post("/ui/sites/{site_id}/delete")
    def ui_delete_site(site_id: int, confirm: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise HTTPException(404, "site not found")
        if confirm.strip().lower() != domain:
            return done("/site#h-danger", t("Per eliminare scrivi il dominio esatto ({domain}): niente è stato "
                                            "eliminato.", domain=domain))
        try:
            manage.delete_site(db, c.tenant_id, site_id)
        except manage.ManageError as e:
            return done("/site#h-danger", str(e))
        resp = done("/", t("{domain} eliminato con profilo, fonti seguite, proposte, bozze e destinazioni.",
                           domain=domain))
        forget_site(resp)
        return resp

    @app.post("/ui/targets/{target_id}")
    def ui_edit_target(target_id: int, name: str = Form(...), address: str = Form(""), user: str = Form(""),
                       indexnow_key: str = Form(""), jsonld: str = Form(""), enabled: str = Form(""),
                       branch: str = Form(""), path: str = Form(""), image_path: str = Form(""),
                       article_url: str = Form(""), verify: str = Form(""), language: str = Form(""),
                       c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            manage.update_target(db, c.tenant_id, target_id, name=name, address=address, user=user,
                                 indexnow_key=indexnow_key, jsonld=bool(jsonld), enabled=bool(enabled),
                                 branch=branch, path=path, image_path=image_path,
                                 article_url=article_url, verify=bool(verify), language=language)
        except LookupError:
            raise HTTPException(404, "target not found") from None
        except manage.ManageError as e:
            return done("/publications#h-targets", t("Destinazione non modificata: {e}", e=e))
        return done("/publications#h-targets", t("Destinazione {name} aggiornata.", name=name.strip()[:100]))

    @app.post("/ui/targets/{target_id}/delete")
    def ui_delete_target(target_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            name = manage.delete_target(db, c.tenant_id, target_id)
        except LookupError:
            raise HTTPException(404, "target not found") from None
        except manage.ManageError as e:
            return done("/publications#h-targets", str(e))
        return done("/publications#h-targets", t("Destinazione {name} eliminata: gli articoli già inviati restano "
                                                 "sul sito.", name=name))

    @app.post("/ui/recipients/{rid}")
    def ui_edit_recipient(rid: int, channel: str = Form(...), address: str = Form(...), enabled: str = Form(""),
                          c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            manage.update_recipient(db, c.tenant_id, rid, channel, address, bool(enabled))
        except LookupError:
            raise HTTPException(404, "recipient not found") from None
        except manage.ManageError as e:
            return done("/settings#h-rec", str(e))
        return done("/settings#h-rec", t("Destinatario aggiornato."))

    @app.post("/ui/preferences")
    def ui_preferences(timezone: str = Form(...), briefing_hour: str = Form(...), language: str = Form(...),
                       c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            manage.preferences(admin, c.tenant_id, timezone, briefing_hour, language)
        except manage.ManageError as e:
            return done("/settings#h-prefs", str(e))
        return done("/settings#h-prefs", t("Preferenze salvate: il prossimo briefing segue i nuovi orari."))

    @app.post("/ui/drafts/{draft_id}/edit")
    async def ui_edit_draft(request: Request, draft_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        form = await request.form()
        texts = {}
        for key, value in form.items():
            if key.startswith("claim-") and key[6:].isdigit():
                texts[int(key[6:])] = str(value)
        search = {k: str(form.get(f"seo_{k}", "")) for k in ("keyphrase", "title", "description", "slug")}
        try:
            res = drafts.edit(db, c.tenant_id, draft_id, str(form.get("title", "")), str(form.get("subtitle", "")),
                              texts, search)
        except LookupError:
            raise HTTPException(404, "draft not found") from None
        except ValueError as e:
            msg = (t("il titolo va da 5 a 160 caratteri.") if "title" in str(e)
                   else t("una frase non può restare vuota."))
            return done(f"/drafts/{draft_id}?edit=1", t("Modifiche non salvate: {e}", e=msg))
        notice = t("Modifiche salvate.")
        if res["flags"]:
            notice += " " + t("Alcune frasi non passano i controlli: sono evidenziate nel testo.")
        if res["was_approved"]:
            notice += " " + t("La bozza era approvata: va riapprovata prima di pubblicarla.")
        return done(f"/drafts/{draft_id}", notice)

    @app.post("/ui/drafts/{draft_id}/delete")
    def ui_delete_draft(draft_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        try:
            title = manage.delete_draft(db, c.tenant_id, draft_id)
        except LookupError:
            raise HTTPException(404, "draft not found") from None
        except manage.ManageError as e:
            return done(f"/drafts/{draft_id}", str(e))
        return done("/drafts", t("Bozza «{title}» eliminata: la proposta torna accettata e puoi riscriverla.",
                                 title=title))
