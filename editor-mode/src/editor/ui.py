"""Dashboard pages of Editor Mode.

Server-rendered over the same functions as the JSON API, so tenancy, roles
and checks are the same: every page reads inside the caller's tenant (row
level security applies) and every form posts to a route that requires the
role the action needs. Forms answer with a redirect and a one-shot notice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from importlib import metadata
from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, bindparam, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from . import connect, evergreen, menus, opportunity as oppo, queue, ratelimit, readers
from . import activity as act, accounts, auth, autopilot, billing, credits, delivery, drafts, evening, i18n, jobs, manage, models as m, news_base, profile as prof, proposals as props, images, publishing, quotas, repo, tenant_data, translate as tr, trends, trials, usage, vault, searchconsole, weekly
from .i18n import N_, t
from .markdown import to_html

log = logging.getLogger(__name__)

COOKIE = "editor_token"
SITE_COOKIE = "editor_site"
# "site": the Desk shows only the site chosen in the menu; anything else
# (the default): every site.
SCOPE_COOKIE = "editor_scope"
NOTICE_COOKIE = "editor_notice"
# The trend of the moment shows a topic only when its relevance found at least
# two words of one of the site's subjects (one word alone is 0.33 at most).
MOMENT_RELEVANCE = 0.5

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
    "explainer": N_("spiegazione"), "recipe": N_("ricetta"), "menu": N_("menù"), "comparison": N_("confronto"), "data": N_("dati"),
}
KINDS = {"rss": "RSS", "hackernews": "HN", "github": "GitHub", "huggingface": "Hugging Face", "arxiv": "arXiv"}
TARGET_KINDS = {"wordpress": "WordPress", "github": N_("GitHub (repo del sito)"), "webhook": "Webhook",
                "telegram_channel": N_("Canale Telegram")}
QUOTA_LABELS = {
    "sites": N_("Siti"),
    "sources": N_("Fonti attive"),
    "drafts": N_("Articoli del mese"),
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


# Fewer articles read than this and the profile is a guess: the start page asks for the context first.
THIN_POSTS = 5


def auto_last(settings: dict) -> dict | None:
    """The automatic newsroom's last hour, with its time as a datetime."""
    last = dict(settings.get("auto_last") or {})
    if not last:
        return None
    try:
        last["at"] = datetime.fromisoformat(last["at"])
    except (KeyError, TypeError, ValueError):
        last["at"] = None
    return last


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
    (r"the destination (.+) takes articles in (\w+), this one is in (\w+)",
     N_(r"La destinazione \1 riceve articoli in \2, questo è in \3: scegli la destinazione della sua lingua.")),
    (r"repository (\S+) not found, or the token cannot see it",
     N_(r"GitHub non trova il repository \1 con questo token: nel token scegli «Only select repositories» con \1 e dai Contents in lettura e scrittura.")),
    (r"nothing new for more than (\d+) days", N_(r"Nessuna notizia nuova da più di \1 giorni: ritorna in valutazione tra due settimane.")),
    (r"(\d+) failed fetches in a row", N_(r"\1 letture fallite di fila: la riprovo da sola ogni giorno.")),
    (r"off topic \(relevance ([\d.]+) on the last (\d+) items\)",
     N_(r"Fuori tema: pertinenza \1 sulle ultime \2 notizie. Ritorna in valutazione tra due settimane.")),
    (r"not enough credits: (\d+) needed, (\d+) left",
     N_(r"Crediti insufficienti: ne servono \1 e ne restano \2. Puoi aggiungerne in Il mio piano.")),
    (r"drafts limit reached \((\d+) of (\d+), plan .*\)",
     N_(r"Articoli del piano finiti per questo mese (\1 di \2).")),
    (r"the trial is over: proposals only until a plan is chosen",
     N_("La prova è finita: solo proposte finché non scegli un piano.")),
    (r"replaced by GitHub Trending",
     N_("Sostituita da GitHub Trending: conta quali repository gli sviluppatori premiano oggi, non quali contengono una parola.")),
]


def problem(text) -> str:
    """The crawler's and profile's notes, in the dashboard's language."""
    text = re.sub(r"^(?:\w+Error: )+", "", str(text).strip())
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


def export_response(db, tenant_id: str, as_zip: bool = True) -> Response:
    day = datetime.now(UTC).date().isoformat()
    if as_zip:
        return Response(tenant_data.export_zip(db, tenant_id), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="redazia-{tenant_id}-{day}.zip"'})
    return Response(tenant_data.export_json(db, tenant_id), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{tenant_id}-{day}.json"'})


def draft_state(status: str, decided_by: str | None) -> str:
    """A draft's state as shown: a refused draft is "discarded" when the
    automatic newsroom refused it, "rejected" when a person did."""
    from .autopilot import AUTO

    return "discarded" if status == "rejected" and decided_by == AUTO else status


def target_secret_name(kind: str, name: str, site_id: int) -> str:
    """A name for a destination's credential when the form leaves it empty."""
    prefix = {"wordpress": "WP", "github": "GH", "webhook": "HOOK", "telegram_channel": "TG"}.get(kind, "PUB")
    words = re.sub(r"[^A-Z0-9]+", "_", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper())
    return "_".join(x for x in (prefix, words.strip("_")[:24].strip("_"), str(site_id)) if x)


def language_name(code) -> str:
    """A language by its own name ("it", "it-IT" → "Italiano"); anything
    that is not a code is shown as it is."""
    text = str(code or "").strip()
    short = re.split(r"[-_]", text, maxsplit=1)[0].lower()
    if len(text) <= 5 and short in tr.LANGUAGES:
        return tr.LANGUAGES[short]
    return text


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

    def vat_note(customer) -> str:
        """What the VAT adds to a price shown before it, for this customer."""
        if billing.vat_rate(customer):
            return t("Al pagamento si aggiunge l'IVA al {rate}%.", rate=billing.VAT_PERCENT)
        return t("Per un'azienda con partita IVA in un altro paese dell'UE non si aggiunge l'IVA: la versa il cliente (inversione contabile).")
    env.globals["vat_note"] = vat_note

    def with_vat_s(cents, currency: str = "EUR", customer=None) -> str:
        """A price before VAT as the customer pays it, VAT included (the law
        wants it for consumers, Codice del Consumo art. 49): empty when no
        VAT is added (reverse charge) or there is no price."""
        if not cents or not billing.vat_rate(customer):
            return ""
        return t("{price} IVA inclusa", price=billing.money(billing.with_vat(int(cents), customer)[0], currency))
    env.globals["with_vat"] = with_vat_s
    env.filters["problem"] = problem
    env.filters["s100"] = score100
    env.filters["plural"] = plural
    env.filters["langname"] = language_name
    env.filters["flag"] = drafts.flag_text
    env.globals["draft_state"] = draft_state

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

    def scoped(request: Request, sites, site) -> int | None:
        """The site the menu is on, when one is chosen instead of "Tutti i
        siti": the Attività box then shows only its work (and the collection)."""
        return site.id if site is not None and len(sites) > 1 and not whole(request) else None

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
        # "Primi passi" in the menu until the site in view can publish.
        starting = site is None or not s.execute(select(m.publish_targets.c.id).where(
            m.publish_targets.c.site_id == site.id).limit(1)).first() or not s.execute(
            select(m.editorial_profiles.c.id).where(m.editorial_profiles.c.site_id == site.id,
                                                    m.editorial_profiles.c.status == "approved").limit(1)).first()
        rooms = [x for x in accounts.memberships(admin, c.user_id) if x.active] if c.user_id else []
        resp = pages.TemplateResponse(request, name, {
            "caller": c, "sites": sites, "site": site, "active": active, "newsrooms": rooms,
            "all_sites": whole(request), "starting": starting,
            "plan_state": billing.subscription(admin, c.tenant_id) if c.can("owner") else None,
            "plan_name": plan_name(s),
            "feedback_new": readers.unread(s, site.id if site else None),
            # «Pubblica le autorizzate» follows the director on every page until it is pressed.
            "held": publishing.held_count(s) if c.can("owner") else 0,
            # The newsroom sees its plan's units; the model's euros only the operators.
            "credit_state": None if c.operator else credits.state(s),
            "activity": data.pop("activity", None) or act.recent(s, tz, problem, site=scoped(request, sites, site)),
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

    # Wrong passwords counted per address (ratelimit.py); a test may pass its own.
    limiter = getattr(ctx, "limiter", None) or ratelimit.Limiter()

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
        ip = request.client.host if request.client else None
        if limiter.blocked(ip, "" if token.strip() else email):
            return login_form(request, t("Troppi tentativi sbagliati da questa connessione: riprova fra qualche minuto."),
                              429, email)
        if token.strip():
            c = auth.resolve(db, token.strip())
            if c is None or token.strip().startswith("eds_"):
                limiter.failed(ip)
                return login_form(request, t("Token non valido"), 401)
            return signed_in(request, token.strip())
        try:
            u = accounts.authenticate(admin, email, password)
        except accounts.AccountError as e:
            return login_form(request, f"{str(e)[:1].upper()}{str(e)[1:]}.", 429, email)
        if u is None:
            limiter.failed(ip, email)
            return login_form(request, t("Email o password sbagliate"), 401, email)
        limiter.succeeded(ip, email)
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
        ip = request.client.host if request.client else None
        if mail is not None and not limiter.blocked(ip):
            limiter.failed(ip)  # every request counts: one address cannot ask for many accounts
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
                # The page answers the same either way; the log says why no email left.
                log.exception("password reset email not sent")
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
            WITH followed AS (  -- the sources the site follows, read once (an OR in the join read them per item)
                SELECT f.id AS source_id FROM editor.sources f
                 WHERE f.status = 'active' AND f.shared IS NOT TRUE AND (f.site_id = :site OR f.site_id IS NULL)
                UNION
                SELECT f.archive_id FROM editor.sources f
                 WHERE f.status = 'active' AND f.shared IS NOT TRUE AND (f.site_id = :site OR f.site_id IS NULL)
                   AND f.archive_id IS NOT NULL)
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
                          WHERE ti.topic_id = t.id AND si.source_id IN (SELECT source_id FROM followed))
            ORDER BY opportunity DESC NULLS LAST, hype DESC NULLS LAST LIMIT :lim""").bindparams(
            bindparam("since", type_=m.topics.c.updated_at.type)),
            {"site": site_id, "lim": limit, "since": datetime.now(UTC) - timedelta(days=7)}).all()

    def on_subject(s, site_id: int, topic_ids: list[int]) -> set[int]:
        """The topics whose latest Opportunity found them about one of the
        site's subjects, not just near it."""
        o = m.opportunity_scores.c
        latest: dict[int, dict] = {}
        for r in s.execute(select(o.topic_id, o.components).where(
                o.site_id == site_id, o.topic_id.in_(topic_ids or [0])).order_by(o.computed_at.desc())).all():
            latest.setdefault(r.topic_id, r.components or {})
        return {tid for tid, comp in latest.items() if (comp.get("relevance") or 0) >= MOMENT_RELEVANCE}

    def proposal_rows(s, site_id: int | None, statuses: tuple[str, ...], limit: int = 30):
        p = m.proposals
        hype = (select(m.trend_scores.c.score).where(m.trend_scores.c.topic_id == p.c.topic_id)
                .order_by(m.trend_scores.c.computed_at.desc()).limit(1).scalar_subquery())
        cites = (select(func.count()).select_from(m.proposal_citations)
                 .where(m.proposal_citations.c.proposal_id == p.c.id).scalar_subquery())
        draft = (select(func.max(m.drafts.c.id)).where(m.drafts.c.proposal_id == p.c.id,
                                                       m.drafts.c.translation_of.is_(None)).scalar_subquery())
        # Once the draft went out the plan says so, with the link (Francesco 2026-11-11:
        # published recipes still read "Bozza pronta").
        pub = m.publications
        out = (select(pub.c.external_url).where(pub.c.draft_id == draft, pub.c.status == "published",
                                                pub.c.withdrawn_at.is_(None))
               .order_by(pub.c.published_at.desc()).limit(1).scalar_subquery())
        live = (select(func.count()).select_from(pub).where(pub.c.draft_id == draft, pub.c.status == "published",
                                                            pub.c.withdrawn_at.is_(None)).scalar_subquery())
        q = (select(p, m.sites.c.domain, hype.label("hype"), cites.label("citations"), draft.label("draft_id"),
                    live.label("published"), out.label("published_url"))
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
            if evergreen.is_evergreen(basis):
                out[r.id] = themed_details(r, basis, cited, model)
                continue
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
        for r in rows:  # every outlet a proposal draws on, not only the newest news
            if r.id in out:
                out[r.id]["outlets"] = list(dict.fromkeys(n.source for n in news.get(r.id, [])))[:3]
        return out

    def themed_details(r, basis: dict, cited: list, model: bool) -> dict:
        """A proposal on a theme (evergreen.py): why the site needs it
        instead of why today, and no hype."""
        theme, reason = basis.get("theme") or r.title, basis.get("reason")
        if reason == "search":
            why = t("Su Google la ricerca «{query}» ha portato al tuo sito {n} impressioni in 28 giorni, ma nessun tuo "
                    "articolo risponde a quella ricerca.", query=basis.get("query") or theme, n=basis.get("impressions") or 0)
        elif reason == "season":
            why = t("«{theme}» è un tema che i tuoi lettori cercano in questo periodo dell'anno.", theme=theme)
        elif not basis.get("posts"):
            why = t("Il tuo sito non ha articoli su «{theme}», uno degli argomenti del profilo.", theme=theme)
        elif basis.get("last"):
            why = t("Su «{theme}» il tuo sito ha {n} articoli, l'ultimo del {date}: è ora di tornarci.", theme=theme,
                    n=basis["posts"], date=basis["last"])
        else:
            why = t("Su «{theme}» il tuo sito ha solo {n} articoli, meno degli altri argomenti del profilo.",
                    theme=theme, n=basis["posts"])
        opp = round(r.opportunity or 0)
        return {
            "title": r.title, "themed": True, "single": False,
            "article": r.angle if model and r.angle else t("Una guida sul tema, scritta dalle fonti qui sotto."),
            "format": t(FORMATS[r.format]) if r.format in FORMATS else r.format,
            "news": cited, "why": why, "today": r.why_now if model else "", "update": None, "related": [],
            "opportunity": t("Punteggio {n}/100: quanto serve al sito un articolo su questo tema. Conta il bisogno "
                             "del sito, non quanto se ne parla oggi.", n=opp),
            "hype": t("Contenuto senza scadenza: l'hype del giorno non conta."), "opp": opp, "hype_n": None,
        }

    def start_of_day(s) -> datetime:
        tz = zone(s)
        return datetime.combine(datetime.now(tz).date(), time(), tz)

    # --- Oggi -----------------------------------------------------------------------

    @app.get("/ui/activity", response_class=HTMLResponse)
    def activity_box(request: Request, mode: str = "recent", c: auth.Caller = Depends(viewer)):
        """The Attività box alone, fetched again while work runs: as on every
        page (recent), on the Desk (desk) or on the Attività page (page)."""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            data = act.recent(s, zone(s), problem, full=mode in ("desk", "page"), compact=mode != "page",
                              site=scoped(request, sites, site))
        return pages.TemplateResponse(request, "_activity.html.j2", {"activity": data},
                                      headers={"Cache-Control": "no-store"})

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, c: auth.Caller = Depends(viewer)):
        """Every job of the last day, with all it produced."""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            return render(request, "activity.html.j2", c, s, "today", _sites=(sites, site),
                          activity=act.recent(s, zone(s), problem, full=True, compact=False,
                                              site=scoped(request, sites, site)))

    def limit_boxes(s, usage, auto_cards, credit) -> list[dict]:
        """The limits of the plan and how far each is: green far from it,
        amber close to it, red when reached. Unlimited ones are not shown."""
        def box(label, used, limit, note=""):
            if not limit:
                return None
            pct = round(100 * used / limit)
            return {"label": label, "used": used, "limit": limit, "pct": min(100, pct), "note": note,
                    "level": "red" if used >= limit else "amber" if pct >= 80 else "green"}
        out = []
        for a in auto_cards:
            cfg, kept = a["cfg"], a["kept"]
            out.append(box(t("Articoli oggi · {domain}", domain=a["site"].domain), kept, cfg["per_day"],
                           t("riprende domani") if kept >= cfg["per_day"] else ""))
        if usage:
            u, pl = usage.used, usage.plan
            out += [box(t("Articoli del mese"), u.get("drafts_month", 0), pl.get("max_drafts_month")),
                    box(t("Fonti attive"), u.get("active_sources", 0), pl.get("max_active_sources")),
                    box(t("Siti"), u.get("sites", 0), pl.get("max_sites")),
                    ]
        if credit and credit.get("enabled"):
            out.append(box(t("Crediti del mese"), credit["month_used"], credit["month"]))
        return [x for x in out if x]

    def the_day(s, usage, auto_cards, sid) -> dict:
        """What came out today and how long the month's articles last at
        this pace: the first thing a director looks for in the morning."""
        import calendar
        import math

        since = start_of_day(s)
        pub, pt, dr = m.publications.c, m.publish_targets.c, m.drafts.c
        # Each article once, whatever the destinations it went to; its translations apart.
        q = (select(dr.translation_of.is_(None).label("original"), func.count(func.distinct(pub.draft_id)))
             .select_from(m.publications.join(m.publish_targets, pt.id == pub.target_id)
                          .join(m.drafts, m.drafts.c.id == pub.draft_id))
             .where(pub.status == "published", pub.published_at >= since).group_by(dr.translation_of.is_(None)))
        if sid is not None:
            q = q.where(pt.site_id == sid)
        counted = {bool(k): n for k, n in s.execute(q).all()}
        out = {"published": counted.get(True, 0), "translated": counted.get(False, 0),
               "discarded": sum(a["day"]["discarded"] for a in auto_cards),
               "held": sum(a["day"]["held"] for a in auto_cards), "pace": None}
        if usage and usage.plan.get("max_drafts_month"):
            now = datetime.now(zone(s))
            limit, used = int(usage.plan["max_drafts_month"]), int(usage.used.get("drafts_month") or 0)
            days = calendar.monthrange(now.year, now.month)[1]
            per_day = used / now.day
            end = math.ceil(limit / per_day) if per_day else None
            out["pace"] = {"used": used, "limit": limit, "left": max(0, limit - used),
                           "month": round(per_day * days), "runs_out": end if end is not None and end <= days else None}
        return out

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
            proposed = proposal_rows(s, sid, ("proposed",), limit=60)
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
            # Sites whose automatic newsroom picks the proposals by itself.
            auto_bodies = {r.site_id: r.body for r in s.execute(select(m.editorial_profiles.c.site_id, m.editorial_profiles.c.body)
                                                                .where(m.editorial_profiles.c.status == "approved")).all()
                           if autopilot.settings_of(r.body)["enabled"]}
            auto_sites = set(auto_bodies)
            # Every proposal waiting, not only the ones listed.
            # Those of a site on automatic are not: the newsroom picks from them, or lets them lapse.
            proposed_total = s.execute(select(func.count()).select_from(m.proposals).where(
                m.proposals.c.status == "proposed", *of(m.proposals.c.site_id))).scalar() or 0
            auto_pending = s.execute(select(func.count()).select_from(m.proposals).where(
                m.proposals.c.status == "proposed", m.proposals.c.site_id.in_(auto_bodies or [0]),
                *of(m.proposals.c.site_id))).scalar() or 0
            proposed_total -= auto_pending
            # The numbers of the collection: the plan's for the newsroom, or the site's.
            collected = {"sources": usage.used.get("active_sources", 0) if usage else 0,
                         "today": usage.used.get("items_today", 0) if usage else 0}
            if one:
                collected = {
                    "sources": s.execute(select(func.count()).select_from(m.sources).where(
                        m.sources.c.site_id == sid, m.sources.c.status == "active")).scalar() or 0,
                    "today": s.execute(select(func.count()).select_from(m.source_items).where(
                        repo.followed_by(sid), m.source_items.c.fetched_at >= start_of_day(s))).scalar() or 0}
            # Sites whose automatic newsroom picks the proposals by itself.
            proposed = [p for p in proposed if p.site_id not in auto_sites][:12]
            explained = proposal_details(s, proposed)
            # The automatic newsrooms' day: what was published, what it is doing, why it stopped.
            auto_cards = []
            for x in ([one] if one else sites):
                if x.id not in auto_bodies:
                    continue
                body = auto_bodies[x.id]
                day = autopilot.today(s, x.id, start_of_day(s))
                urls = dict(s.execute(select(m.publications.c.draft_id, m.publications.c.external_url).where(
                    m.publications.c.draft_id.in_([i["id"] for i in day["items"]] or [0]),
                    m.publications.c.status == "published", m.publications.c.withdrawn_at.is_(None))).all())
                cfg = autopilot.settings_of(body)
                auto_cards.append({"site": x, "cfg": cfg, "day": day, "urls": urls,
                                   "kept": autopilot.made_since(s, x.id, start_of_day(s),
                                                                fix=cfg["on_problem"] == "fix")[0],
                                   "last": auto_last((body.get("settings") or {})),
                                   "left_out": (auto_last((body.get("settings") or {})) or {}).get("left_out", 0)
                                   if (auto_last((body.get("settings") or {})) or {}).get("done") else 0})
            # A source the maintenance suspended is news, not a decision.
            decisions = len(profile_drafts) + proposed_total + len(pending_pubs)
            desk = desk_rows(s, [one] if one else sites, lines, pending)
            # The cockpit: the busiest and the most expensive sites, the latest news.
            busiest = sorted((x for x in desk if x["news"] or x["proposals_week"] or x["drafts_week"]),
                             key=lambda x: (-(x["news"] + 5 * x["proposals_week"] + 10 * x["drafts_week"]), x["domain"]))[:5]
            costly = sorted((x for x in desk if x["calls"]), key=lambda x: (-x["cost"], -x["tokens"]))[:5]
            # Of the proposals of the last 30 days, how many came out as
            # articles (Francesco 2026-11-15: a useful number, not the model's cost).
            pr, dr, pb = m.proposals.c, m.drafts.c, m.publications.c
            since30 = datetime.now(UTC) - timedelta(days=30)
            made30 = s.execute(select(func.count()).select_from(m.proposals).where(
                pr.created_at >= since30, *of(pr.site_id))).scalar() or 0
            out30 = s.execute(select(func.count(func.distinct(dr.proposal_id)))
                              .select_from(m.drafts.join(m.publications, pb.draft_id == dr.id))
                              .join(m.proposals, pr.id == dr.proposal_id)
                              .where(pr.created_at >= since30, dr.translation_of.is_(None),
                                     pb.status == "published", pb.withdrawn_at.is_(None),
                                     *of(pr.site_id))).scalar() or 0
            si = m.source_items
            news_day = s.execute(select(func.count()).select_from(si).where(
                repo.followed_by(sid), si.c.fetched_at >= datetime.now(UTC) - timedelta(hours=24))).scalar() or 0
            latest = s.execute(
                select(si.c.title, si.c.url, si.c.published_at, si.c.fetched_at, m.sources.c.name.label("source"))
                .join(m.sources, m.sources.c.id == si.c.source_id)
                .where(repo.followed_by(sid), si.c.duplicate_of.is_(None),
                       si.c.fetched_at >= datetime.now(UTC) - timedelta(days=3))
                .order_by(si.c.fetched_at.desc(), si.c.id.desc()).limit(6)).all()
            approved_bodies = dict(s.execute(select(m.editorial_profiles.c.site_id, m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.status == "approved")).all())
            return render(request, "today.html.j2", c, s, "today", _sites=(sites, site), desk=desk, one=one,
                          busiest=busiest, costly=costly, made30=made30, out30=out30,
                          news_day=news_day, latest=latest,
                          activity=act.recent(s, zone(s), problem, full=True, site=sid),
                          profile_drafts=profile_drafts, lines=lines, site_lines=site_lines,
                          proposed=proposed, explained=explained, pending_pubs=pending_pubs, auto_sites=auto_sites, auto_cards=auto_cards,
                          stopped=stopped, drafts_ready=sum(draft_counts.values()),
                          needs_review=draft_counts.get("needs_review", 0), new_today=new_today,
                          usage=usage, last_fetch=last_fetch, core_trouble=core_trouble, briefing=briefing, decisions=decisions,
                          proposed_total=proposed_total, collected=collected,
                          limits=limit_boxes(s, usage, auto_cards, credits.state(s)),
                          day=the_day(s, usage, auto_cards, sid),
                          request_sites=[x for x in ([one] if one else sites) if x.id in approved_bodies],
                          # A site of recipes may also ask for a whole menu (menus.py, Francesco 2026-11-11).
                          menu_sites=[x.id for x in ([one] if one else sites)
                                      if autopilot.recipe_site(approved_bodies.get(x.id))])

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
        has_target = count(m.publish_targets, of(m.publish_targets.c.site_id)) > 0
        has_profile = count(m.editorial_profiles, of(m.editorial_profiles.c.site_id)) > 0
        steps = [
            {"title": t("Aggiungi il sito"), "past": t("Sito aggiunto"), "done": bool(sites), "href": None,
             "busy": "analysis" in working and not sites,
             "wait": t("Scansione del sito in corso: legge fino a 25 articoli, di solito pochi minuti."),
             "todo": t("Scrivi il dominio qui sotto: la redazione legge il sito e ne ricava temi, pubblico e tono.")},
            {"title": t("Collega il sito"), "past": t("Sito collegato"), "href": "/connect", "busy": False, "wait": "",
             "done": has_target, "later": has_profile or "analysis" in working,
             "todo": t("Dimmi con cosa è fatto (WordPress, GitHub, altro): provo il collegamento, leggo le categorie e poi analizzo il sito.")},
            {"title": t("Approva il profilo"), "past": t("Profilo approvato"), "done": bool(lines.get(sid) if sid else lines), "href": "/site", "busy": bool({"analysis", "brief"} & working),
             "wait": t("Analisi in corso: il profilo da approvare arriva appena finisce."),
             "todo": t("Controlla temi e articoli d'esempio, togli i temi che non vuoi e approva.")},
            {"title": t("Attiva le fonti"), "past": t("Fonti attivate"), "done": count(m.sources, m.sources.c.status == "active", of_site) > 0, "href": "/sources",
             "busy": "discovery" in working, "wait": t("Ricerca delle fonti in corso dopo l'approvazione del profilo."),
             "todo": t("Guarda origine, ultime notizie e punteggio delle candidate e attiva quelle che vuoi.")},
            {"title": t("Raccogli le notizie"), "past": t("Notizie raccolte"), "done": bool(s.execute(select(m.source_items.c.id).where(site_items).limit(1)).first()), "href": "/trends", "busy": True,
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
        ]
        if sid is not None:
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == sid, m.editorial_profiles.c.status == "approved")).scalar()
            if autopilot.settings_of(body)["enabled"]:
                # The automatic newsroom chooses and writes: nothing to decide by hand.
                for x in steps[5:7]:
                    x["busy"], x["wait"] = True, t("La redazione automatica è accesa: sceglie le proposte, scrive e "
                                                  "pubblica da sola ogni ora. Non serve il tuo sì; lo stato è in "
                                                  "Sito e profilo › Redazione automatica.")
                    x["href"] = "/site?tab=auto"
        if all(x["done"] for x in steps):
            return None
        # A site read without a destination can wait for it: the next step is the one in view
        # until nothing else is left, then the connection is.
        pending = [i for i, x in enumerate(steps) if not x["done"]]
        current = next((i for i in pending if not steps[i].get("later")), pending[0])
        for i, x in enumerate(steps):
            x["current"] = i == current
        return steps

    @app.get("/start", response_class=HTMLResponse)
    def start_view(request: Request, c: auth.Caller = Depends(viewer)):
        """The guided path: from a domain to the first published article,
        one step at a time, with what each step needs right on the page."""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            p = m.editorial_profiles
            lines = dict(s.execute(select(p.c.site_id, p.c.version).where(p.c.status == "approved")).all())
            steps = first_steps(s, sites, lines, site.id if site else None) if site else first_steps(s, sites, lines)
            current = next((i for i, x in enumerate(steps) if x["current"]), None) if steps else None
            data = {}
            if steps:
                data["key"] = key = ["site", "connect", "profile", "sources", "news", "proposal", "draft"][current]
            if site is not None and steps:
                if key == "profile":
                    draft = s.execute(select(p).where(p.c.site_id == site.id, p.c.status == "draft")
                                      .order_by(p.c.version.desc()).limit(1)).first()
                    if draft is not None:
                        b = draft.body
                        data["profile"] = {
                            "version": draft.version,
                            "sector": (b.get("sector") or {}).get("value") if isinstance(b.get("sector"), dict) else b.get("sector"),
                            "audience": (b.get("audience") or {}).get("value") if isinstance(b.get("audience"), dict) else b.get("audience"),
                            "subtopics": [x.get("name") for x in b.get("subtopics", [])][:10],
                            "gaps": b.get("gaps", [])[:4]}
                        # Too little to read (a new site): the profile is a guess from the home page and
                        # its sections, so the context comes first and approval second.
                        read = (b.get("analysis") or {}).get("posts_read") or 0
                        by_brief = (b.get("sector") or {}).get("by") == "brief" if isinstance(b.get("sector"), dict) else False
                        if read < THIN_POSTS and not by_brief:
                            cfg = autopilot.settings_of(b)
                            data["thin"] = {"read": read, "brief": cfg["brief"], "sections": cfg["sections"],
                                            "language": (getattr(site, "language", None) or "it")[:2]}
                elif key == "sources":
                    src = m.sources.c
                    data["candidates"] = s.execute(
                        select(src.id, src.name, src.kind, src.score, src.url).where(
                            src.site_id == site.id, src.status == "candidate")
                        .order_by(src.score.desc().nulls_last()).limit(8)).all()
                elif key == "proposal":
                    rows = proposal_rows(s, site.id, ("proposed",), limit=3)
                    data["proposed"], data["explained"] = rows, proposal_details(s, rows)
                elif key == "draft":
                    dr = m.drafts.c
                    data["drafts"] = s.execute(select(dr.id, dr.title, dr.status).where(
                        dr.site_id == site.id, dr.status.in_(("draft", "needs_review")), dr.translation_of.is_(None))
                        .order_by(dr.id.desc()).limit(3)).all()
            auto = bool(s.execute(select(m.tenants.c.autopilot)).scalar())
            return render(request, "start.html.j2", c, s, "start", _sites=(sites, site), steps=steps,
                          current=current, autopilot=auto, **data)

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
            "sources_style": (lambda st: "none" if st != "list" and autopilot.recipe_site(body) else st)(
                ((body or {}).get("settings") or {}).get("sources_style") or "notes"),
            "content_mode": evergreen.mode_of(body), "evergreen_share": evergreen.share_of(body),
            "evening": evening.settings_of(body),
            "evening_allowed": evening.allowed(s),
            "evening_section": evening.section_of(evening.settings_of(body), (body or {}).get("language")),
            "auto": autopilot.settings_of(body or s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site.id, m.editorial_profiles.c.status == "draft")
                .order_by(m.editorial_profiles.c.version.desc()).limit(1)).scalar()),
            "auto_today": autopilot.today(s, site.id, start_of_day(s)),
            "auto_last": auto_last((body or {}).get("settings") or {}),
            "auto_allowed": quotas.autopilot_allowed(s),
            "auto_targets": s.execute(select(pt.id, pt.name, pt.kind).where(
                pt.site_id == site.id, pt.enabled.is_not(False)).order_by(pt.id)).all(),
            "drafts": drafts, "publications": pubs,
            "targets": count(m.publish_targets, pt.site_id == site.id, pt.enabled.is_not(False)),
            "cost": cost[0], "calls": cost[1],
            "discards": props.discards(s, site.id, month_ago),
            "auto_month": auto_month(s, site.id),
            "home_country": autopilot.HOME.get(tr.main_language(s, site.id) or "", ""),
        }

    def auto_month(s, site_id: int) -> dict | None:
        """The automatic newsroom's articles against the plan's drafts of
        the month, which every site and translation of the newsroom shares."""
        u = quotas.usage(s)
        limit = u.plan.get("max_drafts_month") if u else None
        if limit is None:
            return None
        per_day = {r.site_id: autopilot.settings_of(r.body) for r in s.execute(
            select(m.editorial_profiles.c.site_id, m.editorial_profiles.c.body)
            .where(m.editorial_profiles.c.status == "approved")).all()}
        here = per_day.get(site_id)
        others = sum(cfg["per_day"] for sid, cfg in per_day.items() if sid != site_id and cfg["enabled"])
        mine = here["per_day"] if here and here["enabled"] else 0
        left = max(0, int(limit - (u.used.get("drafts_month") or 0)))
        total = mine + others
        return {"limit": int(limit), "left": left, "mine": mine, "others": others, "month": total * 30,
                "days": left // total if total else None, "credits": u.plan.get("credits_month") is not None}

    def site_page(request, c, s, sites, site, editing: bool = False, status_code: int = 200, error: str | None = None,
                  tab: str = "profile", confirm_delete: bool = False):
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
        prep = profile_prep(s, site) if site and shown is None else None
        return render(request, "site.html.j2", c, s, "site", status_code=status_code, thin_posts=THIN_POSTS, _sites=(sites, site),
                      profiles=profiles, approved=approved, draft=draft, shown=shown, analysis=analysis, prep=prep,
                      tab=tab, confirm_delete=confirm_delete, rows=rows, evidence=ev, editing=editing, error=error,
                      numbers=site_numbers(s, site) if site else None,
                      comparison=prof.compare(approved.body, draft.body) if approved and draft else [],
                      jobs=[j for j in jobs.recent(s, 10) if j["status"] != "done"][:5],
                      domain_free=manage.can_change_domain(s, site.id) if site else False,
                      langs=tr.LANGUAGES, own_language=tr.main_language(s, site.id) if site else None,
                      site_langs=tr.site_languages(s, site.id) if site else [],
                      max_languages=tr.allowed(s, site.id if site else None), plan_languages=tr.allowed(s),
                      lang_offer=language_offer(c, s, site),
                      feed_token=delivery.current_token(s, site.id) if site else None,
                      feed_base=(getattr(ctx, "public_url", "") or str(request.base_url)).rstrip("/"),
                      gsc=search_panel(s, site) if site else None)

    def language_offer(c: auth.Caller, s, site) -> dict | None:
        """A language more for the site (Francesco 2026-11-10): what it costs
        and whether it can be bought online now; for the owner of a plan
        that limits the languages."""
        if site is None or not c.can("owner"):
            return None
        if tr.allowed(s) is None:
            return None
        try:
            offer = billing.language_offer(admin, c.tenant_id)
        except billing.BillingError:
            return None
        offer["extra"] = billing.extra_languages(admin, c.tenant_id).get(site.id, 0)
        offer["tenant_id"] = c.tenant_id
        cur = offer["currency"]
        customer = billing.customer_of(admin, c.tenant_id)
        offer["vat"] = billing.vat_rate(customer)
        now_gross = billing.with_vat(offer["now"], customer)[0] if offer["now"] is not None else None
        offer |= {"month_s": billing.money(offer["month"], cur), "year_s": billing.money(offer["year"], cur),
                  "month_vat_s": billing.money(billing.with_vat(offer["month"] or 0, customer)[0], cur),
                  "year_vat_s": billing.money(billing.with_vat(offer["year"] or 0, customer)[0], cur),
                  "now_s": billing.money(now_gross, cur) if now_gross is not None else None}
        if ctx.gateway is None and offer["ok"]:
            offer |= {"ok": False, "why": t("pagamenti online non configurati: contatta chi ti ha venduto il servizio")}
        return offer

    def profile_prep(s, site) -> dict:
        """Where the first profile of a site is: the newest analysis or brief
        job for it (queued, running with its step, failed with why) or none."""
        j = m.jobs.c
        r = s.execute(select(j.id, j.kind, j.status, j.error, j.progress, j.started_at, j.finished_at)
                      .where(or_(and_(j.kind == "analysis", j.subject == site.domain),
                                 and_(j.kind == "brief", j.subject == f"site {site.id}")))
                      .order_by(j.id.desc()).limit(1)).first()
        if r is None:
            return {"state": "none"}
        step = None
        if r.progress and r.progress.get("text"):
            try:
                step = t(r.progress["text"], **(r.progress.get("values") or {}))
            except (KeyError, IndexError, ValueError):
                step = r.progress["text"]
        return {"state": r.status, "kind": r.kind, "error": r.error, "step": step,
                "at": r.finished_at or r.started_at}

    def search_panel(s, site) -> dict:
        """Google Search Console on the site's page: the property, its
        state, the last 28 days and the account to add in Search Console."""
        key = searchconsole.load_key()
        out = {"configured": key is not None, "email": key.client_email if key else "",
               "property": site.gsc_property, "status": site.gsc_status,
               "problem": searchconsole.problem(site.gsc_status), "checked": site.gsc_checked_at,
               "suggest": f"sc-domain:{site.domain}", "data": None}
        if site.gsc_property:
            end = datetime.now(zone(s)).date() - timedelta(days=1)
            start = end - timedelta(days=27)
            tot = searchconsole.totals(s, site.id, start, end)
            if tot["impressions"]:
                out["data"] = {"totals": tot, "pages": searchconsole.top(s, site.id, "page", start, end, 5),
                               "queries": searchconsole.top(s, site.id, "query", start, end, 8)}
        return out

    @app.get("/site", response_class=HTMLResponse)
    def site_view(request: Request, edit: int = 0, tab: str = "profile", delete: int = 0,
                  c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            return site_page(request, c, s, sites, site, editing=bool(edit) and c.can("owner"), tab=tab,
                             confirm_delete=bool(delete) and c.can("owner"))

    @app.post("/ui/sites")
    def ui_add_site(request: Request, domain: str = Form(...), back: str = Form("/"),
                    c: auth.Caller = Depends(ctx.need("owner"))):
        from .api import normalise_domain
        d = normalise_domain(domain)
        try:
            sid = ctx.add_site(c.tenant_id, d)
        except trials.TrialUsed:
            return done("/settings#h-sites", t("{domain} ha già avuto la prova gratuita in un'altra redazione: per lavorarci scegli un piano in Abbonamento.", domain=d))
        except quotas.QuotaExceeded:
            return done("/settings#h-sites", t("Il piano non ha posto per un altro sito: scegline uno più grande in Abbonamento."))
        # The site is in; what it is made with comes before the analysis, so the first reading
        # already knows its categories and the way articles will get there.
        resp = done("/connect", t("{domain} aggiunto: dimmi con cosa è fatto e lo collego, poi lo analizzo.", domain=d))
        choose_site(resp, sid)
        return resp

    # --- Collega il sito ------------------------------------------------------------

    def connect_message(e: connect.ConnectError) -> str:
        i = e.info
        return {
            "address": t("L'indirizzo non è valido: scrivilo per esteso, per esempio https://esempio.it."),
            "https": t("Serve un indirizzo https: WordPress accetta le password applicative solo su una connessione sicura."),
            "private": t("Questo indirizzo non è raggiungibile da internet: scrivi quello pubblico del sito."),
            "timeout": t("Il sito non ha risposto in tempo: riprova tra poco."),
            "unreachable": t("Non riesco a raggiungere il sito: controlla l'indirizzo e che sia online."),
            "redirect": t("Il sito rimanda a un altro indirizzo: scrivi quello definitivo (di solito con https e www come lo vedi nel browser)."),
            "credentials_missing": t("Mancano le credenziali: compila tutti i campi."),
            "not_wordpress": t("A questo indirizzo non trovo l'API di WordPress (risposta {status}). Controlla l'indirizzo, oppure che un plugin di sicurezza non la blocchi.", status=i.get("status", "")),
            "auth": t("WordPress ha rifiutato utente o password. Serve la password applicativa (non quella con cui entri) e lo username dell'utente per cui l'hai creata, non il nome dato alla password. Se sono giusti, il server non passa l'accesso a WordPress: nel .htaccess del sito serve SetEnvIf Authorization \"(.*)\" HTTP_AUTHORIZATION=$1."),
            "rest": t("WordPress ha risposto con un errore ({status}): riprova, o controlla i plugin di sicurezza.", status=i.get("status", "")),
            "role": t("Questo utente non può scrivere articoli (ruolo {roles}): serve Autore, Editor o Amministratore.", roles=", ".join(i.get("roles") or []) or "?"),
            "repo_name": t("Scrivi il repository come proprietario/nome, per esempio primoco/miosito, oppure incolla il suo indirizzo GitHub."),
            "token_bad": t("GitHub ha rifiutato il token: è sbagliato, scaduto o revocato. Creane uno nuovo seguendo i passi qui sopra."),
            "token_forbidden": t("GitHub non lascia usare questo token su {repo}: controlla i permessi e che il repository sia tra quelli scelti.", repo=i.get("repo", "")),
            "repo_missing": t("Non trovo {repo}: il nome è sbagliato, oppure il token non è stato abilitato su questo repository.", repo=i.get("repo", "")),
            "no_write": t("Il token vede {repo} ma non può scriverci: nei permessi serve Contents in lettura e scrittura.", repo=i.get("repo", "")),
            "branch_missing": t("Il ramo {branch} non esiste in questo repository.", branch=i.get("branch", "")),
            "github": t("GitHub ha risposto con un errore: {detail}", detail=i.get("detail", "")),
        }.get(e.code, t("Collegamento non riuscito ({code}).", code=e.code))

    def connect_page(request, c, s, sites, site, *, kind="", host="", form=None, error=None, result=None, status=200):
        return render(request, "connect.html.j2", c, s, "start", status_code=status, _sites=(sites, site),
                      kind=kind, host=host, form=form or {}, error=error, result=result,
                      has_profile=bool(s.execute(select(m.editorial_profiles.c.id).where(
                          m.editorial_profiles.c.site_id == site.id).limit(1)).first()),
                      linked=s.execute(select(m.publish_targets.c.id, m.publish_targets.c.name, m.publish_targets.c.kind)
                                       .where(m.publish_targets.c.site_id == site.id)).all())

    @app.get("/connect", response_class=HTMLResponse)
    def connect_view(request: Request, kind: str = "", host: str = "", c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            if site is None:
                return RedirectResponse("/start", status_code=303)
            return connect_page(request, c, s, sites, site, kind=kind if kind in ("wordpress", "github", "webhook") else "",
                                host=host if host in connect.HOSTS else "")

    def start_analysis_once(tenant_id: str, site_id: int, domain: str) -> bool:
        """The first reading of the site, unless it has had one: connecting again changes nothing there."""
        with db.tenant(tenant_id) as s:
            if s.execute(select(m.editorial_profiles.c.id).where(m.editorial_profiles.c.site_id == site_id).limit(1)).first():
                return False
        ctx.run_analysis(tenant_id, domain)
        return True

    @app.post("/ui/connect/{site_id}/later")
    def ui_connect_later(site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise HTTPException(404, "site not found")
        start_analysis_once(c.tenant_id, site_id, domain)
        return done("/start", t("Analisi di {domain} partita: la segui passo per passo nel riquadro Attività. Il collegamento lo fai quando vuoi, da Primi passi.", domain=domain))

    @app.post("/ui/connect/{site_id}/check")
    def ui_connect_check(request: Request, site_id: int, kind: str = Form(...), address: str = Form(""),
                         user: str = Form(""), secret: str = Form(""), host: str = Form(""), branch: str = Form(""),
                         path: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        if kind not in ("wordpress", "github", "webhook"):
            raise HTTPException(400, "unknown kind")
        host = host if host in connect.HOSTS else ""
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            row = s.execute(select(m.sites).where(m.sites.c.id == site_id)).first()
            if row is None:
                raise HTTPException(404, "site not found")
            site = row
        form = {"address": address.strip(), "user": user.strip(), "branch": branch.strip(), "path": path.strip()}

        def again(e: connect.ConnectError | str):
            with db.tenant(c.tenant_id) as s:
                return connect_page(request, c, s, sites, site, kind=kind, host=host, form=form,
                                    error=connect_message(e) if isinstance(e, connect.ConnectError) else e)

        try:
            if kind == "wordpress":
                found = connect.wordpress(address or f"https://{site.domain}", user, secret, resolver=connect.resolver)
                config = {"url": found["base"], "user": found["user"], "categories": found["categories"],
                          "can_upload": found["can_upload"], "can_publish": found["can_publish"]}
                connect.save_target(db, c.tenant_id, site_id, "wordpress", found["name"] or site.domain, config,
                                    secret.strip(), "WP", site.domain)
            elif kind == "github":
                found = connect.github(address, secret, branch=branch, path=path, resolver=connect.resolver)
                chosen = host or found["host"] or "other"
                config = {"repo": found["repo"], "host": chosen,
                          # The preview deploy is what tells a bad build before it reaches the site;
                          # GitHub Pages and unknown hosts have none to wait for.
                          "verify": chosen in ("vercel", "cloudflare", "netlify")}
                if branch.strip():
                    config["branch"] = branch.strip()
                if found["articles_path"]:
                    config["path"] = found["articles_path"]
                if found.get("recipes_path"):  # a recipe site: its recipes, its courses
                    config["recipe_path"] = found["recipes_path"]
                    config["categories"] = found.get("categories") or []
                connect.save_target(db, c.tenant_id, site_id, "github", found["repo"], config, secret.strip(),
                                    "GH", site.domain)
                if found.get("recipes_path"):
                    with db.tenant(c.tenant_id) as s:
                        prof.mark_recipe_site(s, site_id, [x["name"] for x in found.get("categories") or []])
                found["host"] = chosen
            else:
                url = address.strip()
                try:
                    publishing.add_target(db, c.tenant_id, site_id, "webhook", site.domain, {"url": url},
                                          connect.secret_name("WEBHOOK", site.domain) if secret.strip() else None)
                    if secret.strip():
                        vault.put(db, c.tenant_id, connect.secret_name("WEBHOOK", site.domain), secret.strip())
                except publishing.PublishError as e:
                    return again(t("Indirizzo non valido: {e}.", e=e))
                found = {"url": url}
        except connect.ConnectError as e:
            return again(e)
        except (vault.VaultError, publishing.PublishError) as e:
            return again(t("Collegamento non salvato: {e}.", e=e))
        # A WordPress with no articles yet: its analysis would read nothing, the profile starts
        # from what the owner writes (Francesco 2026-11-05, wp.aitoday.it).
        empty = kind == "wordpress" and found.get("posts") == 0
        started = False if empty else start_analysis_once(c.tenant_id, site_id, site.domain)
        with db.tenant(c.tenant_id) as s:
            return connect_page(request, c, s, sites, site, kind=kind, host=host,
                                result={"kind": kind, "found": found, "started": started, "empty": empty})

    @app.post("/ui/sites/{site_id}/review")
    def ui_review(site_id: int, back: str = "/site", c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise HTTPException(404, "site not found")
        ctx.run_analysis(c.tenant_id, domain)
        return done(safe_path(back, "/site"),
                    t("Analisi di {domain} partita: la segui passo per passo nel riquadro Attività.", domain=domain))

    @app.post("/ui/sites/{site_id}/brief")
    def ui_brief(site_id: int, brief: str = Form(""), sections: str = Form(""), language: str = Form("it"),
                 back: str = Form("/site"), c: auth.Caller = Depends(ctx.need("owner"))):
        """The profile from the owner's words, for a site with too little to
        read: the other settings of the automatic newsroom stay as they are."""
        to = safe_path(back, "/site")
        if not brief.strip():
            return done(to, t("Scrivi prima il contesto: il profilo si ricava da lì."))
        p = m.editorial_profiles.c
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
            body = s.execute(select(p.body).where(p.site_id == site_id, p.status.in_(("draft", "approved")))
                             .order_by(p.version.desc()).limit(1)).scalar()
            values = autopilot.settings_of(body) | {"brief": brief.strip()}
            if sections.strip():
                values["sections"] = [y.strip() for y in re.split(r"[\n,;]", sections) if y.strip()]
            try:
                values = autopilot.check_settings(values)
            except ValueError as e:
                return done(to, t("Impostazioni non salvate: {e}.", e=e))
            autopilot.save(s, site_id, values)
        queue.enqueue(db, c.tenant_id, "brief", f"site {site_id}",
                      {"site_id": site_id, "language": language if language in autopilot.LANGS else "it", "auto": values})
        menu_from_sections(c, site_id, values["sections"])
        return done(to, t("Il modello scrive il profilo dal contesto: lo segui nel riquadro Attività, poi lo approvi qui."))

    @app.post("/ui/sites/{site_id}/approve/{version}")
    def ui_approve(site_id: int, version: int, back: str = Form("/site"), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if not prof.approve(s, site_id, version, c.name):
                raise HTTPException(404, "version not found")
        ctx.run_discovery(c.tenant_id, site_id)
        return done(safe_path(back, "/site"), t("Versione {version} approvata: è la linea del sito da ora. Cerco le fonti adatte.", version=version))

    @app.post("/ui/sites/{site_id}/per-day")
    def ui_per_day(site_id: int, n: int = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if not prof.set_proposals_per_day(s, site_id, n):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site?tab=auto", t("Da ora {n} proposte al giorno per questo sito.", n=max(0, min(50, n))))

    @app.post("/ui/sites/{site_id}/cover")
    def ui_cover(site_id: int, style: str = Form(...), text: str = Form(""),
                 c: auth.Caller = Depends(ctx.need("owner"))):
        if style not in ("impact", "editorial"):
            raise HTTPException(400, "unknown style")
        with db.tenant(c.tenant_id) as s:
            if not prof.set_cover(s, site_id, style, bool(text)):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site?tab=auto", t("Stile delle copertine salvato: vale dalla prossima immagine."))

    @app.post("/ui/sites/{site_id}/auto")
    def ui_auto(site_id: int, brief: str = Form(""), sections: str = Form(""), directives: str = Form(""),
                avoid: str = Form(""), per_day: int = Form(6), min_opportunity: float = Form(40),
                min_sources: int = Form(2), start: int = Form(7), end: int = Form(23), target_id: str = Form(""),
                enabled: str = Form(""), credit: str = Form(""), language: str = Form("it"),
                on_problem: str = Form("fix"), morning_share: int = Form(50), borrow: int = Form(2),
                extra_credits: int = Form(0), extra_min_opportunity: float = Form(65),
                country_share: int = Form(34), home_country: str = Form(""), section_share: int = Form(25),
                action: str = Form("save"), c: auth.Caller = Depends(ctx.need("owner"))):
        def lines(x: str) -> list[str]:
            return [y.strip() for y in re.split(r"[\n,;]", x) if y.strip()]
        values = {"enabled": bool(enabled), "brief": brief.strip(), "sections": lines(sections),
                  "directives": directives.strip(), "avoid": lines(avoid), "per_day": per_day,
                  "min_opportunity": min_opportunity, "min_sources": min_sources, "hours": [start, end],
                  "target_id": int(target_id) if target_id.isdigit() else None, "credit": bool(credit),
                  "on_problem": on_problem if on_problem in autopilot.ON_PROBLEM else "fix",
                  "morning_share": morning_share, "borrow": borrow, "extra_credits": extra_credits,
                  "extra_min_opportunity": extra_min_opportunity, "country_share": country_share,
                  "home_country": home_country, "section_share": section_share}
        with db.tenant(c.tenant_id) as s:  # set in "Come scrive il sito", not here
            current = autopilot.settings_of(s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar())
            values["analysis"], before = current["analysis"], current["sections"]
            values |= {k: current[k] for k in ("breaking", "breaking_outlets", "breaking_hours")}
        try:
            values = autopilot.check_settings(values)
        except ValueError as e:
            return done("/site?tab=auto", t("Impostazioni non salvate: {e}.", e=e))
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
            saved = autopilot.save(s, site_id, values)
        if action == "profile":
            if not values["brief"]:
                return done("/site?tab=auto", t("Scrivi prima il contesto: il profilo si ricava da lì."))
            queue.enqueue(db, c.tenant_id, "brief", f"site {site_id}",
                          {"site_id": site_id, "language": language if language in autopilot.LANGS else "it",
                           "auto": values})
            menu_from_sections(c, site_id, values["sections"])
            return done("/site", t("Il modello scrive il profilo dal contesto: lo segui nel riquadro Attività, "
                                   "poi lo approvi qui sotto."))
        if action == "menu" or (values["sections"] and values["sections"] != before):
            # The rubrics as the WordPress site's categories and menu: when they change, or when asked.
            if menu_from_sections(c, site_id, values["sections"]):
                if action == "menu":
                    return done("/site?tab=auto", t("Le rubriche vanno nel menu del sito WordPress: lo segui nel "
                                                  "riquadro Attività."))
            elif action == "menu":
                return done("/site?tab=auto", t("Per il menu servono le rubriche e un sito WordPress collegato."))
        if not saved:
            return done("/site?tab=auto", t("Il sito non ha ancora un profilo: premi \"Crea il profilo dal contesto\"."))
        if values["enabled"]:
            return done("/site?tab=auto", t("Redazione automatica accesa: dal prossimo giro orario sceglie, scrive e "
                                          "pubblica fino a {n} articoli al giorno, dalle {start} alle {end}.",
                                          n=values["per_day"], start=values["hours"][0], end=values["hours"][1]))
        return done("/site?tab=auto", t("Impostazioni salvate. La redazione automatica è spenta."))

    @app.post("/ui/sites/{site_id}/auto/stop")
    def ui_auto_stop(site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
            if body is not None:
                autopilot.save(s, site_id, {**autopilot.settings_of(body), "enabled": False})
        return done("/site?tab=auto", t("Redazione automatica fermata: non parte nessun articolo nuovo. Quelli già in "
                                      "scrittura restano bozze da rileggere."))

    @app.post("/ui/sites/{site_id}/analysis")
    def ui_analysis(site_id: int, on: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
            if body is None or not autopilot.save(s, site_id, {**autopilot.settings_of(body), "analysis": on == "1"}):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site?tab=auto", t("Analisi negli articoli: accesa, vale dal prossimo articolo.") if on == "1"
                    else t("Analisi negli articoli: spenta, gli articoli raccontano solo i fatti."))

    @app.post("/ui/sites/{site_id}/breaking")
    def ui_breaking(site_id: int, on: str = Form(""), outlets: int = Form(3), hours: int = Form(3),
                    c: auth.Caller = Depends(ctx.need("owner"))):
        """Breaking news: a story many outlets tell within a few hours goes
        out even past the day's articles, one fewer tomorrow."""
        with db.tenant(c.tenant_id) as s:
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
            if body is None:
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
            try:
                autopilot.save(s, site_id, {**autopilot.settings_of(body), "breaking": on == "1",
                                            "breaking_outlets": outlets, "breaking_hours": hours})
            except ValueError as e:
                return done("/site?tab=auto", t("Impostazioni non salvate: {e}.", e=e))
        if on != "1":
            return done("/site?tab=auto", t("Breaking news spente: oltre gli articoli del giorno non esce niente."))
        return done("/site?tab=auto", t("Breaking news accese: una notizia che almeno {n} testate danno entro {h} ore esce "
                               "anche a giornata finita o fuori orario, e il giorno dopo ne esce una in meno.",
                               n=outlets, h=hours))

    @app.post("/ui/sites/{site_id}/evening")
    def ui_evening(site_id: int, enabled: str = Form(""), hour: int = Form(21), min_articles: int = Form(6),
                   section: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            values = evening.check_settings({"enabled": enabled == "1", "hour": hour, "min_articles": min_articles,
                                             "section": section})
        except ValueError as e:
            return done("/site?tab=auto", t("Impostazioni non salvate: {e}.", e=e))
        with db.tenant(c.tenant_id) as s:
            if not evening.save(s, site_id, values):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        if values["enabled"]:
            return done("/site?tab=auto", t("Il Punto della Sera è acceso: ogni sera alle {hour} legge gli articoli del "
                                          "giorno e ne scrive l'analisi, se ne sono usciti almeno {n}.",
                                          hour=values["hour"], n=values["min_articles"]))
        return done("/site?tab=auto", t("Il Punto della Sera è spento."))

    @app.post("/ui/sites/{site_id}/content-mode")
    def ui_content_mode(site_id: int, mode: str = Form(...), share: int = Form(evergreen.SHARE),
                        c: auth.Caller = Depends(ctx.need("owner"))):
        if mode not in evergreen.MODES:
            raise HTTPException(400, "unknown mode")
        with db.tenant(c.tenant_id) as s:
            if not prof.set_content_mode(s, site_id, mode, share):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site?tab=auto", t("Tipo di contenuti salvato: vale dalle prossime proposte."))

    @app.post("/ui/sites/{site_id}/sources-style")
    def ui_sources_style(site_id: int, style: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        if style not in ("notes", "list", "none"):
            raise HTTPException(400, "unknown style")
        with db.tenant(c.tenant_id) as s:
            if not prof.set_sources_style(s, site_id, style):
                return done("/site", t("Approva prima il profilo: le proposte partono dalla linea approvata."))
        return done("/site?tab=auto", t("Fonti negli articoli salvate: valgono dalla prossima pubblicazione."))

    @app.post("/ui/sites/{site_id}/search-console")
    def ui_search_console(site_id: int, prop: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        client = searchconsole.client_from_env()
        try:
            status = searchconsole.set_property(db, c.tenant_id, site_id, prop, client)
        except LookupError:
            raise HTTPException(404, "site not found") from None
        if status is None:
            return done("/site?tab=publish", t("Search Console scollegata: i suoi dati sono stati cancellati."))
        if client is None:
            return done("/site?tab=publish", t("Proprietà salvata. Il server non ha ancora la chiave di Google (EDITOR_GSC_KEY): i dati arrivano quando c'è."))
        if status != "ok":
            return done("/site?tab=publish", t(searchconsole.problem(status) or status))
        searchconsole.sync(db, c.tenant_id, client, site_id=site_id)
        return done("/site?tab=publish", t("Search Console collegata: clic e ricerche degli ultimi giorni sono arrivati, poi si aggiornano ogni notte."))

    @app.post("/ui/sites/{site_id}/search-console/sync")
    def ui_search_console_sync(site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        client = searchconsole.client_from_env()
        if client is None:
            return done("/site?tab=publish", t("Il server non ha ancora la chiave di Google (EDITOR_GSC_KEY)."))
        got = searchconsole.sync(db, c.tenant_id, client, site_id=site_id).get(site_id)
        if isinstance(got, str):
            return done("/site?tab=publish", t(searchconsole.problem(got) or got))
        return done("/site?tab=publish", t("Dati di Search Console aggiornati: {n} righe.", n=got or 0))

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
            return done("/site?tab=site", t("La v{version} è la linea in uso: approvane un'altra prima di eliminarla.", version=version))
        if why == "used":
            return done("/site?tab=site", t("La v{version} ha già prodotto proposte e punteggi: resta nello storico.", version=version))
        return done("/site?tab=site", t("Versione v{version} eliminata.", version=version))

    @app.post("/ui/sites/{site_id}/profile")
    def ui_edit_profile(site_id: int, sector: str = Form(""), audience: str = Form(""), tone: str = Form(""),
                        c: auth.Caller = Depends(ctx.need("owner"))):
        """Sector, audience and tone from a form: the owner never writes the
        profile by hand (Francesco 2026-11-19)."""
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
            version = prof.set_basics(s, c.tenant_id, site_id, c.name, sector, audience, tone)
        if version is None:
            return done("/site", t("Nessuna modifica: il profilo resta com'era."))
        return done("/site", t("Profilo aggiornato (v{version}).", version=version))

    @app.post("/ui/sites/{site_id}/choose")
    async def ui_choose_profile(request: Request, site_id: int, c: auth.Caller = Depends(ctx.need("owner"))):
        """The comparison of a proposed version, row by row: a ticked row
        takes the proposal, an unticked one keeps the approved value."""
        form = await request.form()
        try:
            draft_v, approved_v = int(form.get("draft", "")), int(form.get("approved", ""))
        except ValueError:
            raise HTTPException(400, "versions missing") from None
        take = {str(x) for x in form.getlist("take")}
        with db.tenant(c.tenant_id) as s:
            try:
                version, how = prof.approve_choice(s, c.tenant_id, site_id, draft_v, approved_v, take, c.name)
            except LookupError:
                return done("/site", t("Nel frattempo il profilo è cambiato: controlla di nuovo il confronto e scegli."))
            except ValueError:
                return done("/site", t("Queste scelte non formano un profilo valido: nulla è cambiato, la linea resta la v{version}.",
                                       version=approved_v))
        if how == "kept":
            return done("/site", t("Hai tenuto tutto della v{version}: la proposta è scartata.", version=version))
        ctx.run_discovery(c.tenant_id, site_id)
        if how == "approved":
            return done("/site", t("Versione {version} approvata: è la linea del sito da ora. Cerco le fonti adatte.", version=version))
        return done("/site", t("Approvata la v{version}, con le tue scelte fra la v{old} e la v{new}: è la linea del sito da ora. "
                               "Cerco le fonti adatte.", version=version, old=approved_v, new=draft_v))

    @app.get("/guide", response_class=HTMLResponse)
    def guide(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            u = quotas.usage(s)
            return render(request, "guide.html.j2", c, s, "guide", plan=u.plan if u else {}, prices=credits.prices(s))

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

    @app.post("/ui/sites/{site_id}/sources/add")
    def ui_source_add(site_id: int, urls: str = Form(...), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
                raise HTTPException(404, "site not found")
        wanted = [x for x in re.split(r"[\s,]+", urls) if x][:20]
        if not wanted:
            return done("/sources", t("Scrivi almeno un indirizzo."))
        ctx.run_discovery(c.tenant_id, site_id, wanted, c.name)
        return done("/sources?status=active", t("Lettura delle fonti partita: le segui nel riquadro Attività. Quelle che hanno un feed entrano attive."))

    # --- Trend e piano --------------------------------------------------------------

    @app.get("/trends", response_class=HTMLResponse)
    def trends_view(request: Request, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            topics = topic_scores(s, site.id if site else None, 60) if site else []
            dots = [t for t in topics if t.hype is not None and t.opportunity is not None]
            # Every proposal waiting for a yes (the Desk counts them all), and
            # the latest of those being written or written.
            plan = (list(proposal_rows(s, site.id, ("proposed",), 500))
                    + list(proposal_rows(s, site.id, ("accepted", "drafted"), 20))) if site else []
            waiting = sum(1 for p in plan if p.status == "proposed")
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
                # Only what is about the site's subjects: a story of the day that
                # scores well on Hype alone (a privacy fine on a cooking site,
                # Francesco 2026-11-15) is not this site's trend.
                close = on_subject(s, site.id, [x["topic_id"] for x in snap.topics if fit.get(x["topic_id"])])
                for x in snap.topics:
                    if (fit.get(x["topic_id"]) or 0) >= props.MIN_OPPORTUNITY and x["topic_id"] in close \
                            and len(moment) < 3:
                        moment.append(dict(x, opportunity=fit[x["topic_id"]]))
                offered = proposal_rows(s, site.id, ("proposed",), 500)
                for x in moment:
                    x["titles"] = [p for p in offered if p.topic_id == x["topic_id"]]
                shown = {p.id for x in moment for p in x["titles"]}
                plan = [p for p in plan if p.id not in shown]
            # What waits for a decision first, then what is being written, then the rest.
            order = {"proposed": 0, "accepted": 1, "drafted": 2}
            plan = sorted(plan, key=lambda p: order.get(p.status, 3))
            explained = proposal_details(s, plan)
            hour = s.execute(select(m.tenants.c.briefing_hour)).scalar()
            body = prof.approved_profile(s, site.id)
            return render(request, "trends.html.j2", c, s, "trends", _sites=(sites, site), topics=topics,
                          auto_site=bool(body is not None and autopilot.settings_of(body.body)["enabled"]),
                          dots=dots, plan=plan, explained=explained, briefing_hour=hour, can_draft=ctx.core is not None,
                          writing=draft_jobs(s, [p.id for p in plan if p.status == "accepted" and not p.draft_id]),
                          snap=snap, day=day, moment=moment, waiting=waiting if site else 0,
                          local=lambda at: at.astimezone(zone(s)))

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
    def ui_collect(back: str = Form("/"), c: auth.Caller = Depends(ctx.need("editor"))):
        """Read the sources now, then topics, Hype, the trend of the moment and Opportunity."""
        ctx.run_collect(c.tenant_id)
        return done(safe_path(back, "/"), t("Raccolta partita: la segui passo per passo nel riquadro Attività."))

    @app.post("/ui/proposals/{proposal_id}")
    def ui_decide(proposal_id: int, accept: str = Form(...), back: str = Form("/trends"), reason: str = Form(""),
                  c: auth.Caller = Depends(ctx.need("editor"))):
        if not props.decide(db, c.tenant_id, proposal_id, accept == "1", c.name, reason=reason or None):
            return done(safe_path(back, "/trends"), t("Questa proposta era già stata decisa."))
        if accept == "1" and ctx.core is not None:
            ctx.run_draft(c.tenant_id, proposal_id)
            with db.tenant(c.tenant_id) as s:
                title = s.execute(select(m.proposals.c.title).where(m.proposals.c.id == proposal_id)).scalar() or ""
            return done(safe_path(back, "/trends"), t("Proposta accettata: «{title}» è in scrittura, la segui nel riquadro "
                                                      "Attività.", title=title))
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
                                        d.c.language, d.c.translation_of, d.c.decided_by,
                                        claims.label("claims")).where(d.c.site_id == site.id)
                                 .order_by(d.c.created_at.desc()).limit(300)).all()
                # Discarded by the automatic newsroom, refused by a person: two different things.
                # Out on the site: "Pubblicata", not "Approvata" (Francesco 2026-11-11).
                pub = m.publications
                live = set(s.execute(select(pub.c.draft_id).where(
                    pub.c.draft_id.in_([r.id for r in rows] or [0]), pub.c.status == "published",
                    pub.c.withdrawn_at.is_(None))).scalars())
                rows = [{**r._mapping, "state": "published" if r.id in live else draft_state(r.status, r.decided_by)}
                        for r in rows]
                # Each translation right under its original.
                kids: dict[int, list] = {}
                shown = {r["id"] for r in rows}
                for r in rows:
                    if r["translation_of"] in shown:
                        kids.setdefault(r["translation_of"], []).append(r)
                rows = [x for r in rows if r["translation_of"] not in shown for x in [r, *kids.get(r["id"], [])]]
            # Why the automatic newsroom discarded or held an article: its own note on it.
            why = dict(s.execute(select(m.feedback.c.target_id, m.feedback.c.note).where(
                m.feedback.c.target == "draft", m.feedback.c.given_by == autopilot.AUTO,
                m.feedback.c.target_id.in_([r["id"] for r in rows] or [0]), m.feedback.c.note.is_not(None))).all())
            return render(request, "drafts.html.j2", c, s, "drafts", _sites=(sites, site), rows=rows, langs=tr.LANGUAGES,
                          why=why)

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
            targets = [x for x in s.execute(select(m.publish_targets).where(
                m.publish_targets.c.site_id == d.site_id, m.publish_targets.c.enabled.is_not(False))).all()
                if publishing.fits_language(x, d.language)]  # Italian text never goes to the English site
            pubs = s.execute(select(m.publications.c.id, m.publications.c.status, m.publications.c.mode, m.publications.c.target_id,
                                    m.publications.c.decision_note,
                                    m.publications.c.external_url, m.publications.c.withdrawn_at,
                                    m.publications.c.updated_at, m.publications.c.published_at, m.publications.c.error,
                                    m.publish_targets.c.name, m.publish_targets.c.kind)
                             .join(m.publish_targets, m.publish_targets.c.id == m.publications.c.target_id)
                             .where(m.publications.c.draft_id == draft_id).order_by(m.publications.c.id)).all()
            sites, site = current_site(s, request)
            # The original and its translations, and the site's languages still missing.
            root = d.translation_of or d.id
            family = s.execute(select(m.drafts.c.id, m.drafts.c.language, m.drafts.c.status, m.drafts.c.title,
                                      m.drafts.c.translation_of, m.drafts.c.decided_by)
                               .where(or_(m.drafts.c.id == root, m.drafts.c.translation_of == root))
                               .order_by(m.drafts.c.id)).all()
            original = next((x for x in family if x.id == root), None)
            j = m.jobs.c
            translating = {a.get("language") for a in s.execute(select(j.args).where(
                j.kind == "translate", j.status.in_(("queued", "running")))).scalars() if a and a.get("draft_id") == root}
            img_busy = any(a and a.get("draft_id") == root for a in s.execute(select(j.args).where(
                j.kind == "image", j.status.in_(("queued", "running")))).scalars())
            image = images.of(s, d.id)
            # A cover made after the article went online: it goes there with «Aggiorna sul sito».
            stale_cover = {p.id for p in pubs if p.status == "published" and p.mode == "publish" and not p.withdrawn_at
                           and p.kind != "telegram_channel" and publishing.cover_changed(image, p)}
            cover = {"cost": credits.cover_cost(s, root), "free": credits.free_covers_left(s, root),
                     "credits": credits.state(s)}
            # A version written again: what of the old one is online, to replace.
            replaces = (d.seo or {}).get("replaces")
            online_old = s.execute(select(m.publications.c.id, m.publish_targets.c.name)
                                   .join(m.publish_targets, m.publish_targets.c.id == m.publications.c.target_id)
                                   .where(m.publications.c.draft_id == replaces, m.publications.c.status == "published",
                                          m.publications.c.mode == "publish")).all() if replaces else []
            changing = {a.get("publication_id") for a in s.execute(select(j.args).where(
                j.kind == "change", j.status.in_(("queued", "running")))).scalars() if a}
            redoing = any(a and a.get("replaces") == d.id for a in s.execute(select(j.args).where(
                j.kind == "draft", j.status.in_(("queued", "running")))).scalars())
            have = {x.language for x in family}
            wanted = [x for x in tr.site_languages(s, d.site_id) if x not in have]
            # Why the automatic newsroom discarded or stopped it, and the version it wrote instead.
            fb = m.feedback.c
            auto_note = s.execute(select(fb.note).where(
                fb.target == "draft", fb.target_id == draft_id, fb.given_by == autopilot.AUTO, fb.note.is_not(None))
                .order_by(fb.id.desc()).limit(1)).scalar() if d.status != "approved" else None
            newer = s.execute(select(m.drafts.c.id, m.drafts.c.version, m.drafts.c.status).where(
                m.drafts.c.proposal_id == d.proposal_id, m.drafts.c.version > d.version,
                m.drafts.c.translation_of.is_(None)).order_by(m.drafts.c.version)).first() \
                if auto_note and d.proposal_id and not d.translation_of else None
            # A menu: its new recipes, each a draft of its own, and whether they are online.
            course_ids = [x["draft_id"] for x in (d.seo or {}).get("menu") or [] if x.get("draft_id")]
            courses = {r.id: {"status": r.status, "decided_by": r.decided_by, "online": menus.online(s, r.id)}
                       for r in s.execute(select(m.drafts.c.id, m.drafts.c.status, m.drafts.c.decided_by)
                                          .where(m.drafts.c.id.in_(course_ids or [0]))).all()}
            site_sections = autopilot.settings_of(s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar())["sections"]
            return render(request, "draft.html.j2", c, s, "drafts", _sites=(sites, site), d=d, sections=sections,
                          site_sections=site_sections, courses=courses,
                          original=original, translations=[x for x in family if x.translation_of],
                          wanted=wanted, translating=translating, langs=tr.LANGUAGES, can_translate=ctx.core is not None,
                          image=image, img_busy=img_busy, images_on=getattr(ctx, "imager", None) is not None,
                          cover=cover, stale_cover=stale_cover, auto_note=auto_note, newer=newer,
                          body_html=to_html(delivery.body_md(d.body_md)),
                          sources=[{"n": refs[i], **items[i]._mapping} for i in order if i in items],
                          flags=flags, checks=checks, targets=targets, pubs=pubs, kinds=TARGET_KINDS,
                          sd=d.seo or {}, editing=bool(edit) and c.can("editor"), claims=claims,
                          body_text=drafts.as_text(claims), online_old=online_old, replaces=replaces,
                          changing=changing, redoing=redoing, flagged_ordinals=flagged, seo_checks=[{"label": t(SEO_CHECKS[x["key"]]), "ok": x["ok"]}
                                                      for x in (d.seo or {}).get("checks", []) if x.get("key") in SEO_CHECKS])

    @app.post("/ui/drafts/{draft_id}/decision")
    def ui_draft_decision(draft_id: int, accept: str = Form(...), publish_to: int = Form(0),
                          c: auth.Caller = Depends(ctx.need("editor"))):
        if not drafts.decide(db, c.tenant_id, draft_id, accept == "1", c.name):
            return done(f"/drafts/{draft_id}", t("Questa bozza era già stata decisa."))
        if accept != "1":
            return done(f"/drafts/{draft_id}", t("Bozza rifiutata."))
        todo = ctx.run_translations(c.tenant_id, draft_id)
        mailed = getattr(ctx, "run_delivery", None) and ctx.run_delivery(c.tenant_id, draft_id)
        extra = (" " + t("La mandiamo anche per email ai destinatari della redazione, con il pacchetto da pubblicare.")
                 if mailed else "")
        if publish_to and c.can("owner"):  # «Approva e pubblica»: a director's yes covers the publication too
            return publish_now(c, draft_id, publish_to, "publish", extra)
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
        return done("/site?tab=publish", msg)

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
        # The type read from the file, not the one stored: only an image leaves this address.
        return Response(bytes(img.content), media_type=images.mime_of(bytes(img.content)),
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.post("/ui/drafts/{draft_id}/image")
    def ui_image(draft_id: int, c: auth.Caller = Depends(ctx.need("editor"))):
        if getattr(ctx, "imager", None) is None:
            return done(f"/drafts/{draft_id}", t("Immagini non configurate: serve EDITOR_IMAGES_API_KEY sul server."))
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts.c.translation_of).where(m.drafts.c.id == draft_id)).first()
            if d is not None:
                cost = credits.cover_cost(s, d.translation_of or draft_id)
                left = credits.state(s).get("left", 0)
                if cost and cost > left:
                    return done(f"/drafts/{draft_id}", t("Crediti insufficienti: una nuova copertina costa {n} crediti e "
                                                         "ne restano {left}. Puoi aggiungerne in Il mio piano.",
                                                         n=cost, left=left))
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
                limit = tr.allowed(s, site_id)
            return done("/site?tab=publish#h-languages", t("Questo sito può avere {n} lingue, compresa la sua: togline "
                                                           "qualcuna, aggiungi una lingua extra o passa a un piano più "
                                                           "grande.", n=limit))
        if not chosen:
            return done("/site?tab=publish", t("Gli articoli restano solo nella lingua del sito."))
        return done("/site?tab=publish", t("Ogni articolo approvato sarà tradotto in: {languages}.",
                                           languages=", ".join(tr.LANGUAGES[x] for x in chosen)))

    def menu_from_sections(c: auth.Caller, site_id: int, sections: list[str]) -> bool:
        """The sections as categories and menu entries on the site's
        WordPress, right when they are confirmed (Francesco 2026-11-05:
        "perché aspettare"). False when the site has no WordPress."""
        with db.tenant(c.tenant_id) as s:
            wp = s.execute(select(m.publish_targets.c.id).where(
                m.publish_targets.c.site_id == site_id, m.publish_targets.c.kind == "wordpress")).first()
        if wp is None or not sections:
            return False
        queue.enqueue(db, c.tenant_id, "wpmenu", f"site {site_id}", {"site_id": site_id, "sections": sections})
        return True

    def file_under(c: auth.Caller, draft_id: int, section: str) -> None:
        """The site's section the article goes under (its category on
        WordPress), when one of the site's sections is chosen."""
        if not section.strip():
            return
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts.c.seo, m.drafts.c.site_id).where(m.drafts.c.id == draft_id)).first()
            if d is None:
                return
            chosen = drafts.site_section(s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar(),
                section)
            if chosen and chosen != (d.seo or {}).get("section"):
                s.execute(update(m.drafts).where(m.drafts.c.id == draft_id).values(seo={**(d.seo or {}), "section": chosen}))

    @app.post("/ui/drafts/{draft_id}/publish")
    def ui_publish(draft_id: int, target_id: int = Form(...), mode: str = Form("draft"), section: str = Form(""),
                   c: auth.Caller = Depends(ctx.need("editor"))):
        if mode not in ("draft", "publish"):
            raise HTTPException(400, "unknown mode")
        file_under(c, draft_id, section)
        if c.can("owner"):  # a director does not ask himself: it goes out now
            return publish_now(c, draft_id, target_id, mode)
        if waiting := menu_first(c, draft_id, target_id, mode, approve=False):
            return waiting
        try:
            publishing.request(db, c.tenant_id, draft_id, target_id, mode, c.name)
        except publishing.PublishError as e:
            return done(f"/drafts/{draft_id}", t("Richiesta non inviata: {e}", e=problem(e)))
        except IntegrityError:
            return done(f"/drafts/{draft_id}", t("Questa pubblicazione è già stata chiesta."))
        return done("/publications", t("Pubblicazione chiesta: aspetta l'approvazione di un direttore."))

    def menu_first(c: auth.Caller, draft_id: int, target_id: int, mode: str, approve: bool, extra: str = ""):
        """A menu whose new recipes are not online: they go first, the menu
        follows by itself (menus.publish_first). None for any other draft."""
        with db.tenant(c.tenant_id) as s:
            todo = menus.missing(s, draft_id)
        if not todo:
            return None
        try:
            sent = menus.publish_first(db, c.tenant_id, draft_id, target_id, mode, c.name, approve,
                                       lambda pid: ctx.run_publication(c.tenant_id, pid))
        except publishing.PublishError as e:
            return done(f"/drafts/{draft_id}", t("Pubblicazione non partita: {e}", e=problem(e)) + extra)
        msg = (t("Prima vanno online le {n} ricette nuove del menù: il menù parte da solo subito dopo.", n=len(todo))
               if approve else t("Chieste le {n} ricette nuove del menù: il menù parte da solo quando sono online.",
                                 n=sent))
        return done(f"/drafts/{draft_id}", msg + extra)

    def publish_now(c: auth.Caller, draft_id: int, target_id: int, mode: str, extra: str = ""):
        """A director's publication: asked and approved in one step, sent by the worker.
        To a GitHub site it waits for «Pubblica le autorizzate», so the site
        builds once for all of them (Francesco 2026-11-14)."""
        if waiting := menu_first(c, draft_id, target_id, mode, approve=True, extra=extra):
            return waiting
        try:
            pub_id = publishing.request(db, c.tenant_id, draft_id, target_id, mode, c.name)
        except publishing.PublishError as e:
            return done(f"/drafts/{draft_id}", t("Pubblicazione non partita: {e}", e=problem(e)) + extra)
        except IntegrityError:
            return done(f"/drafts/{draft_id}", t("Questa pubblicazione è già stata chiesta.") + extra)
        publishing.decide(db, c.tenant_id, pub_id, True, c.name)
        with db.tenant(c.tenant_id) as s:
            kind = s.execute(select(m.publish_targets.c.kind).where(m.publish_targets.c.id == target_id)).scalar()
        if kind == "github":
            publishing.hold(db, c.tenant_id, pub_id)
            return done(f"/drafts/{draft_id}", t("Autorizzata: va online con le altre quando premi «Pubblica le "
                                                 "autorizzate», in un solo aggiornamento del sito.") + extra)
        ctx.run_publication(c.tenant_id, pub_id)
        return done(f"/drafts/{draft_id}", t("Pubblicazione partita: la segui nel riquadro Attività.") + extra)

    @app.post("/ui/publications/release")
    def ui_release(back: str = Form("/"), c: auth.Caller = Depends(ctx.need("owner"))):
        """«Pubblica le autorizzate»: everything authorized goes out, one commit per repository."""
        n = publishing.release(db, c.tenant_id, lambda first, others, site_id: queue.enqueue(
            db, c.tenant_id, "publish", f"publication {first}",
            {"publication_id": first, "site_id": site_id, **({"together": others} if others else {})}))
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        if not n:
            return done(back, t("Non c'è niente da pubblicare."))
        return done(back, t("Pubblicazione partita per {n} articoli: la segui nel riquadro Attività.", n=n))

    @app.post("/ui/publications/{pub_id}/update")
    def ui_pub_update(pub_id: int, draft_id: int = Form(0), section: str = Form(""),
                      c: auth.Caller = Depends(ctx.need("owner"))):
        """The director puts the current text (or a version written again)
        online in place of the published one."""
        with db.tenant(c.tenant_id) as s:
            pub = s.execute(select(m.publications.c.draft_id, m.publications.c.status)
                            .where(m.publications.c.id == pub_id)).first()
            back = f"/drafts/{draft_id or (pub.draft_id if pub else '')}"
            if pub is None or pub.status != "published":
                return done(back, t("Questo articolo non è pubblicato."))
            d = s.execute(select(m.drafts.c.status).where(m.drafts.c.id == (draft_id or pub.draft_id))).first()
            if d is None or d.status != "approved":
                return done(back, t("Approva prima la bozza: online va solo quello che un direttore ha approvato."))
        file_under(c, draft_id or pub.draft_id, section)
        queue.enqueue(db, c.tenant_id, "change", f"update {pub_id}",
                      {"publication_id": pub_id, "op": "update", "draft_id": draft_id or None, "by": c.name})
        return done(back, t("Aggiornamento in corso: lo segui nel riquadro Attività."))

    @app.post("/ui/publications/{pub_id}/withdraw")
    def ui_pub_withdraw(pub_id: int, confirm: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            pub = s.execute(select(m.publications.c.draft_id, m.publications.c.status)
                            .where(m.publications.c.id == pub_id)).first()
        if pub is None or pub.status != "published":
            return done("/publications", t("Questo articolo non è pubblicato."))
        if confirm != "1":
            return done(f"/drafts/{pub.draft_id}", t("Spunta la conferma per ritirare l'articolo."))
        queue.enqueue(db, c.tenant_id, "change", f"withdraw {pub_id}",
                      {"publication_id": pub_id, "op": "withdraw", "by": c.name})
        return done(f"/drafts/{pub.draft_id}", t("Ritiro in corso: lo segui nel riquadro Attività."))

    @app.post("/ui/requests")
    def ui_request_article(site_id: int = Form(...), text: str = Form(""), links: str = Form(""),
                           kind: str = Form("article"), c: auth.Caller = Depends(ctx.need("editor"))):
        """An article asked for in the newsroom's own words, with optional
        links: written from the links and the archive's news on the subject,
        or not written when there is nothing to cite."""
        from . import requested

        text = " ".join(text.split())[:requested.MAX_TEXT]
        if len(text) < 15:
            return done("/#h-request", t("Scrivi cosa vuoi nell'articolo: l'argomento, il taglio, a chi è rivolto."))
        if ctx.core is None:
            return done("/#h-request", t("Nessun Core configurato: per scrivere serve un modello."))
        with db.tenant(c.tenant_id) as s:
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
            if body is None:
                return done("/#h-request", t("Prima approva il profilo del sito: l'articolo si scrive nella sua lingua e nel suo tono."))
        kind = "menu" if kind == "menu" and autopilot.recipe_site(body) else "article"
        found = requested.links_of(links)
        key = hashlib.sha256(f"{kind}|{text}|{' '.join(found)}".encode()).hexdigest()[:12]
        queue.enqueue(db, c.tenant_id, "request", f"request {site_id} {key}",
                      {"site_id": site_id, "text": text, "links": found, "by": c.name, "kind": kind})
        if kind == "menu":
            return done("/", t("Richiesta del menù presa: la redazione sceglie le portate, riusa le ricette del sito "
                               "che vanno bene e scrive le nuove. Il menù e le sue ricette nuove arrivano in Bozze "
                               "tra qualche minuto."))
        return done("/", t("Richiesta presa: la redazione legge i link, cerca le notizie sull'argomento e scrive la "
                           "bozza. La trovi in Bozze tra qualche minuto; se non trova fonti te lo dice in Attività."))

    @app.post("/ui/drafts/{draft_id}/redo")
    def ui_redo_draft(draft_id: int, note: str = Form(""), c: auth.Caller = Depends(ctx.need("editor"))):
        """Write the article again from its proposal, with what to change."""
        note = " ".join(note.split())[:1000]
        if len(note) < 5:
            return done(f"/drafts/{draft_id}", t("Scrivi cosa va cambiato: la nuova versione parte da lì."))
        if ctx.core is None:
            return done(f"/drafts/{draft_id}", t("Nessun Core configurato: per riscrivere serve un modello."))
        with db.tenant(c.tenant_id) as s:
            d = s.execute(select(m.drafts.c.proposal_id, m.drafts.c.site_id, m.drafts.c.translation_of)
                          .where(m.drafts.c.id == draft_id)).first()
        if d is None:
            raise HTTPException(404, "draft not found")
        if d.translation_of:
            return done(f"/drafts/{draft_id}", t("Una traduzione non si rifà: rifai l'originale e traducilo di nuovo."))
        queue.enqueue(db, c.tenant_id, "draft", f"redo {draft_id}",
                      {"proposal_id": d.proposal_id, "site_id": d.site_id, "notes": note, "replaces": draft_id})
        return done(f"/drafts/{draft_id}", t("Nuova versione in scrittura: la trovi in Bozze quando è pronta. "
                                             "La rileggi, la approvi e poi la metti online al posto di questa."))

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

    # --- feedback from readers (readers.py), in trial -------------------------

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_view(request: Request, show: str = "open", c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            sites, site = current_site(s, request)
            r = m.reader_messages
            rows = []
            if site:
                q = (select(r).where(r.c.site_id == site.id, r.c.status != "pending")
                     .order_by(r.c.confirmed_at.desc().nulls_last(), r.c.id.desc()).limit(200))
                if show != "all":
                    q = q.where(r.c.status.in_(("new", "read")))
                rows = s.execute(q).all()
            base = (ctx.public_url or str(request.base_url)).rstrip("/")
            return render(request, "feedback.html.j2", c, s, "feedback", _sites=(sites, site), rows=rows, show=show,
                          snippet=f'<script src="{base}/static/feedback.js" defer></script>',
                          can_mail=ctx.mail is not None, turnstile=bool(all(readers.turnstile_keys())),
                          kinds={"error": N_("Errore segnalato"), "comment": N_("Commento"), "other": N_("Altro")})

    @app.post("/ui/sites/{site_id}/feedback")
    def ui_feedback_switch(site_id: int, on: str = Form("0"), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            if s.execute(update(m.sites).where(m.sites.c.id == site_id).values(feedback=on == "1")).rowcount != 1:
                raise HTTPException(404, "site not found")
        return done("/feedback", t("Feedback dei lettori attivo: aggiungi la riga di codice al sito.") if on == "1"
                    else t("Feedback dei lettori spento: il modulo sparisce dal sito."))

    @app.post("/ui/feedback/{message_id}")
    def ui_feedback_status(message_id: int, status: str = Form(...), c: auth.Caller = Depends(ctx.need("editor"))):
        r = m.reader_messages
        with db.tenant(c.tenant_id) as s:
            if status == "delete":
                s.execute(delete(r).where(r.c.id == message_id, r.c.status != "pending"))
            elif status in ("new", "read", "closed"):
                s.execute(update(r).where(r.c.id == message_id, r.c.status != "pending").values(status=status))
            else:
                raise HTTPException(400, "unknown status")
        return done(f"/feedback#m-{message_id}" if status != "delete" else "/feedback")

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
            env_name = target_secret_name(kind, name, site_id)
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
        w = m.weekly_reports.c
        reports = s.execute(select(w.id, w.week_start, w.sent_at, w.sent_to, w.send_error)
                            .order_by(w.week_start.desc()).limit(8)).all()
        return render(request, "settings.html.j2", c, s, "settings", status_code=status_code, usage=usage,
                      reports=reports,
                      recipients=recipients, tokens=tokens, new_token=new_token, new_link=new_link,
                      error=error, people=accounts.members(admin, c.tenant_id) if owner else [],
                      invited=accounts.pending_invites(admin, c.tenant_id) if owner else [],
                      seats=accounts.seats(admin, c.tenant_id) if owner else (0, None))

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
                        "payments": billing.payments_of(admin, c.tenant_id, 24),
                        "reseller": billing.reseller_of_tenant(admin, c.tenant_id) is not None}
            cr = credits.state(s)
            moves = credits.ledger(s, 15) if cr["enabled"] else []
            return render(request, "plan.html.j2", c, s, "plan", usage=usage, cr=cr, moves=moves,
                          credit_prices=credits.prices(s), free_redraws=credits.FREE_REDRAWS,
                          packs=credits.packs(admin, active_only=True, s=s) if cr["enabled"] else [],
                          meters=meters_of(usage, money=c.operator),
                          room=room, me_email=email,
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

    def weekly_settings():
        st = getattr(ctx, "settings", None)
        if st is None:
            from .runtime import settings as from_env
            st = from_env()
        return st

    def last_week_start(s):
        return datetime.now(zone(s)).date() - timedelta(days=7)

    @app.get("/reports/preview", response_class=HTMLResponse)
    def weekly_preview(c: auth.Caller = Depends(viewer)):
        """The report of the last seven days, as it would arrive now."""
        with db.tenant(c.tenant_id) as s:
            start = last_week_start(s)
        _, _, (_, _, page) = weekly.build(db, c.tenant_id, start, weekly_settings(), store=False)
        return HTMLResponse(page)

    @app.get("/reports/{rid}", response_class=HTMLResponse)
    def weekly_report(rid: int, c: auth.Caller = Depends(viewer)):
        with db.tenant(c.tenant_id) as s:
            page = s.execute(select(m.weekly_reports.c.body_html).where(m.weekly_reports.c.id == rid)).scalar()
        if page is None:
            raise HTTPException(404, "report not found")
        return HTMLResponse(page)

    @app.post("/ui/weekly/send-me")
    def weekly_send_me(c: auth.Caller = Depends(ctx.need("owner"))):
        st = weekly_settings()
        if not st.smtp_host:
            return done("/settings#h-weekly", t("Il server non ha la posta configurata: guarda l'anteprima."))
        with accounts._read(admin) as conn:
            email = conn.execute(select(m.users.c.email).where(m.users.c.id == c.user_id)).scalar() if c.user_id else None
        if not email:
            return done("/settings#h-weekly", t("Questo accesso non ha un'email: guarda l'anteprima."))
        with db.tenant(c.tenant_id) as s:
            start = last_week_start(s)
        _, _, (subject, body, page) = weekly.build(db, c.tenant_id, start, st, store=False)
        try:
            weekly.send_mail(st, [email], subject, body, page)
        except Exception as e:
            return done("/settings#h-weekly", t("Email non partita: {e}.", e=f"{type(e).__name__}"))
        return done("/settings#h-weekly", t("Rapporto degli ultimi sette giorni mandato a {email}.", email=email))

    @app.post("/ui/recipients/{rid}/delete")
    def ui_del_recipient(rid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            s.execute(m.recipients.delete().where(m.recipients.c.id == rid))
        return done("/settings", t("Destinatario rimosso."))

    @app.get("/ui/export")
    def ui_export(format: str = "zip", c: auth.Caller = Depends(ctx.need("owner"))):
        return export_response(db, c.tenant_id, as_zip=format != "json")

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
        used, included = accounts.seats(admin, c.tenant_id)
        if included is not None and used >= included:
            # The plan's accesses are taken: someone must leave, or a bigger plan.
            with db.tenant(c.tenant_id) as s:
                return settings_page(request, c, s, status_code=400, error=t(
                    "Invito non creato: il piano comprende {n} accessi e sono tutti usati. Togli una persona o un "
                    "invito, oppure passa a un piano più grande in Il mio piano.", n=included))
        try:
            token = accounts.create_invite(admin, email, c.name, tenant_id=c.tenant_id, role=role)
        except accounts.AccountError as e:
            with db.tenant(c.tenant_id) as s:
                return settings_page(request, c, s, error=t("Invito non creato: {e}.", e=e), status_code=400)
        return invite_sent(request, c, token, accounts.normalise_email(email))

    def invite_again(request: Request, c: auth.Caller, email: str, role: str):
        """A new link for someone who has not come in yet: the links sent
        before, perhaps expired, stop working."""
        old = [i.token_sha256 for i in accounts.pending_invites(admin, c.tenant_id) if i.email == email]
        token = accounts.create_invite(admin, email, c.name, tenant_id=c.tenant_id, role=role)
        for sha in old:
            accounts.revoke_invite(admin, c.tenant_id, sha)
        return invite_sent(request, c, token, email)

    @app.post("/ui/people/{uid}/resend", response_class=HTMLResponse)
    def ui_person_resend(request: Request, uid: int, c: auth.Caller = Depends(ctx.need("owner"))):
        who = next((x for x in accounts.members(admin, c.tenant_id) if x.id == uid), None)
        if who is None:
            return done("/settings", t("Persona non trovata in questa redazione."))
        if who.active:
            return done("/settings", t("{email} è già entrato: se non ricorda la password, la cambia da «Password dimenticata?» "
                                       "nella pagina di accesso.", email=who.email))
        return invite_again(request, c, who.email, who.role)

    @app.post("/ui/invites/{sha}/resend", response_class=HTMLResponse)
    def ui_invite_resend(request: Request, sha: str, c: auth.Caller = Depends(ctx.need("owner"))):
        i = next((x for x in accounts.pending_invites(admin, c.tenant_id) if x.token_sha256 == sha), None)
        if i is None:
            return done("/settings", t("Invito già usato o scaduto."))
        return invite_again(request, c, i.email, i.role or "editor")

    def invite_sent(request: Request, c: auth.Caller, token: str, to: str):
        """The settings page with the new link: sent by email when the
        server can, otherwise shown to hand over."""
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
            return done("/site?tab=site", t("{domain} non è un dominio valido.", domain=domain.strip()[:100]))
        if new != old:
            if not free:
                return done("/site?tab=site", t("Il profilo di {domain} è già approvato: per cambiare dominio elimina il sito "
                                       "e aggiungi quello giusto.", domain=old))
            with db.tenant(c.tenant_id) as s:
                if s.execute(select(m.sites.c.id).where(m.sites.c.domain == new)).scalar() is not None:
                    return done("/site?tab=site", t("{domain} c'è già tra i siti della redazione.", domain=new))
            if ctx.admin is not None:
                try:
                    trials.check_domain(ctx.admin, c.tenant_id, new)
                except trials.TrialUsed:
                    d = new
                    return done("/site?tab=site", t("{domain} ha già avuto la prova gratuita in un'altra redazione: per lavorarci scegli un piano in Abbonamento.", domain=d))
            try:
                manage.delete_site(db, c.tenant_id, site_id)
            except manage.ManageError as e:
                return done("/site?tab=site", str(e))
            ctx.run_analysis(c.tenant_id, new)
            with db.tenant(c.tenant_id) as s:
                sid = s.execute(select(m.sites.c.id).where(m.sites.c.domain == new)).scalar()
            resp = done("/site?tab=site", t("Dominio corretto da {old} a {new}: l'analisi riparte da capo, la segui in "
                                   "Attività.", old=old, new=new))
            if sid is not None:
                resp.set_cookie(SITE_COOKIE, str(sid), httponly=True, samesite="strict", max_age=365 * 86400)
            return resp
        try:
            manage.rename_site(db, c.tenant_id, site_id, name or old)
        except manage.ManageError as e:
            return done("/site?tab=site", str(e))
        return done("/site?tab=site", t("Sito aggiornato."))

    @app.post("/ui/sites/{site_id}/delete")
    def ui_delete_site(site_id: int, confirm: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        with db.tenant(c.tenant_id) as s:
            domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise HTTPException(404, "site not found")
        if confirm.strip().lower() != domain:
            return done("/site?tab=site&delete=1", t("Per eliminare scrivi il dominio esatto ({domain}): niente è stato "
                                            "eliminato.", domain=domain))
        try:
            manage.delete_site(db, c.tenant_id, site_id)
        except manage.ManageError as e:
            return done("/site?tab=site&delete=1", str(e))
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
                       weekly_report: str = Form(""), c: auth.Caller = Depends(ctx.need("owner"))):
        try:
            manage.preferences(admin, c.tenant_id, timezone, briefing_hour, language, weekly=weekly_report == "1")
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
            if "body" in form:  # the free editor
                recipe = ({k: str(form.get(f"recipe_{k}", "")) for k in (
                    "ingredients", "steps", "servings", "prep_minutes", "cook_minutes", "difficulty", "category", "tags")}
                    if "recipe_ingredients" in form else None)  # the card of the site's own recipe
                res = drafts.edit_text(db, c.tenant_id, draft_id, str(form.get("title", "")),
                                       str(form.get("subtitle", "")), str(form.get("body", "")), search, recipe)
            else:
                res = drafts.edit(db, c.tenant_id, draft_id, str(form.get("title", "")),
                                  str(form.get("subtitle", "")), texts, search)
        except LookupError:
            raise HTTPException(404, "draft not found") from None
        except ValueError as e:
            ref = re.search(r"\[(\d+)\]", str(e))
            msg = (t("il titolo va da 5 a 160 caratteri.") if "title" in str(e)
                   else t("la ricetta ha bisogno di almeno 2 ingredienti e dei passaggi.") if "recipe" in str(e)
                   else t("la fonte [{n}] non è nell'elenco.", n=ref.group(1)) if ref
                   else t("il testo è vuoto.") if "empty" in str(e)
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
