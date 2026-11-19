"""Publishing behind approval.

A publication is requested for an approved draft and a target, waits in
`pending_approval` until a person decides, and only then is executed. The
target's address must resolve to public addresses (a tenant cannot point
Editor Mode at an internal service) and credentials come from environment
variables, never from the database. A tenant only names its secret (``WP``);
the variable actually read is ``EDITOR_SECRET_<TENANT>__WP``, which the
operator sets on the server, so a tenant can never reach the server's own
variables or another tenant's. Every outcome is recorded.

Adapters: WordPress REST API (as draft or published post), a GitHub
repository (the article as Markdown with front matter, and the cover image,
committed for the static sites deployed from it — Vercel, GitHub Pages;
a draft mode publication opens a pull request instead of committing), a
signed webhook (for automation tools that post to LinkedIn, X,
newsletters...), and a Telegram channel.
"""

from __future__ import annotations

import base64
import hashlib
import html
import hmac
import logging
import ipaddress
import json
import os
import re
import socket
from datetime import UTC, datetime
from urllib.parse import quote, urljoin, urlsplit

import httpcore
import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from . import drafts, images, models as m
from . import quotas, seo
from .markdown import to_html

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


GITHUB_AGAIN = (30, 120)  # seconds before each new try of a publication GitHub failed on its side


def _github_hiccup(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, PublishError) and bool(re.search(r"^GitHub, .*: (429|500|502|503|504)\b", str(e)))


def _public_addresses(host: str, port: int, resolver=None) -> list[str]:
    """Every address ``host`` resolves to, refusing if any is not public."""
    resolver = resolver or socket.getaddrinfo
    try:
        infos = resolver(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise PublishError(f"cannot resolve {host}") from e
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PublishError(f"{host} resolves to a non-public address ({ip})")
        addresses.append(str(ip))
    if not addresses:
        raise PublishError(f"cannot resolve {host}")
    return addresses


def check_public_url(url: str, resolver=None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PublishError("targets must use https")
    if parts.username or parts.password:
        raise PublishError("credentials in URLs are not allowed")
    _public_addresses(parts.hostname or "", parts.port or 443, resolver)


class _PinnedBackend(httpcore.SyncBackend):
    """Connects only to the public address checked at connection time.

    `check_public_url` alone leaves a gap: httpx would resolve the name again,
    and a name that changes between the two lookups (DNS rebinding) reaches
    an internal address. Here the name is resolved once, checked, and the
    socket goes to that very address; TLS still verifies the certificate
    against the original host name.
    """

    def __init__(self, resolver=None):
        self.resolver = resolver

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = _public_addresses(host, port, self.resolver)[0]
        return super().connect_tcp(address, port, timeout, local_address, socket_options)


class _PinnedTransport(httpx.HTTPTransport):
    def __init__(self, resolver=None):
        super().__init__()
        self._pool = httpcore.ConnectionPool(ssl_context=httpx.create_ssl_context(),
                                             network_backend=_PinnedBackend(resolver))



def wp_url(base: str, route: str, query: str = "", plain: bool = False) -> str:
    """A WordPress REST address. With `plain`, the form that works without
    pretty permalinks (?rest_route=), where /wp-json/ answers 404."""
    route = route.lstrip("/")
    if plain:
        return f"{base}/?rest_route=/{route}" + (f"&{query}" if query else "")
    return f"{base}/wp-json/{route}" + (f"?{query}" if query else "")


def not_rest(r: httpx.Response) -> bool:
    """A 404 from the web server, not from the REST API: /wp-json/ is not
    routed (plain permalinks, or a server that ignores .htaccess)."""
    return r.status_code == 404 and "json" not in r.headers.get("content-type", "")

def pinned_client(resolver=None) -> httpx.Client:
    """The client publications go out with: no proxy from the environment
    (it would resolve the name itself), no redirects, pinned addresses."""
    return httpx.Client(transport=_PinnedTransport(resolver), timeout=30, follow_redirects=False, trust_env=False)


_REDIRECT_STATUS = {301, 302, 303, 307, 308}


def _ensure_no_redirect(response: httpx.Response, base: str, resolver=None) -> None:
    """Refuse redirect responses instead of following them.

    The publisher client is created with ``follow_redirects=False`` (like the
    Core, which re-checks every hop), so a 3xx here means the target tried to
    send us elsewhere. Fail closed: validate the Location when present so a
    redirect to a private address is reported as such, and refuse the rest.
    """
    if response.status_code not in _REDIRECT_STATUS:
        return
    location = response.headers.get("location", "")
    if location:
        target = urljoin(base, location)
        try:
            check_public_url(target, resolver)
        except PublishError as e:
            raise PublishError(f"redirect refused ({response.status_code} to {location}): {e}") from e
    raise PublishError(f"redirects are not followed ({response.status_code} to {location or '?'})")


SECRET_PREFIX = "EDITOR_SECRET_"
TENANT_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SECRET_NAME = re.compile(r"^[A-Z0-9]+(_[A-Z0-9]+)*$")

GITHUB_API = "https://api.github.com"
GITHUB_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


def github_config(config: dict) -> dict:
    """Validate a github target's config: ``repo`` as ``owner/name`` (the
    only required key), optional ``branch`` (else the repository's default),
    ``path`` for the articles (default ``content/blog``), ``image_path`` for
    the covers (else learned from the posts, or ``public/images``), ``url``
    the address of an article with ``{slug}`` (else learned), ``verify``
    false to publish without waiting for the site's checks, ``wait_minutes``
    for them (default 15) and ``api`` for a GitHub Enterprise server.
    ``recipe_path`` and ``recipe_url``: where a recipe goes when the site
    keeps its recipes apart (else found by itself, sitegit.recipe_folder)."""
    config = dict(config or {})
    repo = str(config.get("repo", "")).strip()
    if not GITHUB_REPO.fullmatch(repo) or ".." in repo:
        raise PublishError("a GitHub target needs the repository as owner/name, e.g. primoco/miosito")
    config["repo"] = repo
    for key in ("branch", "path", "image_path", "recipe_path"):
        value = str(config.get(key, "")).strip().strip("/")
        if ".." in value:
            raise PublishError(f"{key} cannot be ..: it stays inside the repository")
        if value:
            config[key] = value
        else:
            config.pop(key, None)
    if config.get("api"):
        config["api"] = str(config["api"]).strip().rstrip("/")
        check_public_url(config["api"])
    else:
        config.pop("api", None)
    for key in ("url", "recipe_url"):
        url = str(config.get(key) or "").strip()
        if url:
            if not url.startswith("https://") or "{slug}" not in url or any(c.isspace() for c in url):
                raise PublishError("the articles' address is an https address with {slug} in it, e.g. https://site.it/blog/{slug}")
            config[key] = url
        else:
            config.pop(key, None)
    if config.get("verify") is False or str(config.get("verify")).lower() in ("0", "false", "no"):
        config["verify"] = False
    else:
        config.pop("verify", None)
    if config.get("wait_minutes") not in (None, ""):
        config["wait_minutes"] = max(1, min(60, int(config["wait_minutes"])))
    else:
        config.pop("wait_minutes", None)
    return config


def site_path(repo_path: str) -> str:
    """The address on the site of a file in the repository: the folder the
    generator serves as the site's root (public/ for Astro, Next, Vite;
    static/ for Hugo, SvelteKit) is not part of it."""
    for root in ("public/", "static/"):
        if repo_path.startswith(root):
            return "/" + repo_path[len(root):]
    return "/" + repo_path


def secret_variable(tenant_id: str, name: str) -> str:
    """The environment variable holding a tenant's secret ``name``.

    The tenant part has no double underscore (ids are lowercase words joined
    by single dashes) and the name cannot start with one, so the first ``__``
    separates them and two tenants can never map to the same variable.
    """
    if not TENANT_ID.match(tenant_id):
        raise PublishError(f"tenant id {tenant_id!r} cannot own secrets: use lowercase letters, digits and single dashes")
    if not SECRET_NAME.match(name or "") or len(name) > 40:
        raise PublishError("the secret name must be UPPERCASE letters and digits, words joined by single underscores")
    return f"{SECRET_PREFIX}{tenant_id.upper().replace('-', '_')}__{name}"


def resolve_secret(db, target) -> str:
    """A target's credential: stored encrypted in the database (entered from
    the dashboard) or, failing that, the environment variable."""
    from . import vault

    if not target.secret_env:
        raise PublishError(f"target {target.name} has no secret")
    try:
        value = vault.get(db, target.tenant_id, target.secret_env)
    except vault.VaultError as e:
        raise PublishError(str(e)) from None
    if value:
        return value
    var = secret_variable(target.tenant_id, target.secret_env)
    value = os.environ.get(var)
    if not value:
        raise PublishError(f"secret {target.secret_env} is not stored and environment variable {var} is not set")
    return value


def _need(secret: str | None, target) -> str:
    if not secret:
        raise PublishError(f"target {target.name} has no secret")
    return secret


def add_target(db, tenant_id: str, site_id: int, kind: str, name: str, config: dict, secret_env: str | None) -> int:
    if kind in ("wordpress", "webhook"):
        check_public_url(config.get("url", ""))
    if kind == "github":
        config = github_config(config)
    if kind == "telegram_channel" and not str(config.get("chat_id", "")).strip():
        raise PublishError("a Telegram channel needs chat_id")
    if any(k in config for k in ("password", "token", "secret", "api_key")):
        raise PublishError("secrets are stored apart (secret_env and the vault), not in config")
    if kind == "wordpress" and not str(config.get("user", "")).strip():
        raise PublishError("a WordPress target needs the user of its application password")
    if secret_env is not None:
        secret_variable(tenant_id, secret_env)
    with db.tenant(tenant_id) as s:
        return s.execute(insert(m.publish_targets).values(
            tenant_id=tenant_id, site_id=site_id, kind=kind, name=name, config=config, secret_env=secret_env,
        ).returning(m.publish_targets.c.id)).scalar_one()


def search_fields(s, d) -> dict:
    """The draft's search fields and its schema.org data, for the CMS."""
    sd = d.seo or {}
    site = s.execute(select(m.sites.c.name, m.sites.c.domain).where(m.sites.c.id == d.site_id)).first()
    dc, dcs, si = m.draft_claims, m.draft_claim_sources, m.source_items
    claims = s.execute(select(dc.c.section, dc.c.text).where(dc.c.draft_id == d.id).order_by(dc.c.ordinal)).all()
    urls = list(dict.fromkeys(s.execute(select(si.c.url).join(dcs, dcs.c.item_id == si.c.id)
                                        .join(dc, dc.c.id == dcs.c.claim_id).where(dc.c.draft_id == d.id)).scalars()))
    faq = [(q, " ".join(c.text for c in claims if c.section == q)) for q in sd.get("faq", [])]
    description = sd.get("description") or d.subtitle or ""
    section = sd.get("section")
    if not section and d.translation_of:  # a translation is in its original's section
        section = (s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == d.translation_of)).scalar() or {}).get("section")
    original = None
    if d.translation_of:  # the original's address, so the site links the two languages
        pub = s.execute(select(m.publications.c.payload).where(m.publications.c.draft_id == d.translation_of)
                        .order_by(m.publications.c.id.desc()).limit(1)).scalar() or {}
        oseo = s.execute(select(m.drafts.c.seo, m.drafts.c.title).where(m.drafts.c.id == d.translation_of)).first()
        original = pub.get("slug") or (oseo and ((oseo.seo or {}).get("slug") or seo.slugify(oseo.title)))
    recipe = seo.recipe_card(sd.get("recipe"), claims)
    return {
        "translation_of": original, "recipe": recipe,
        "site_domain": site.domain if site else None, "section": section,
        "slug": sd.get("slug") or seo.slugify(d.title), "seo_title": sd.get("title") or d.title,
        "meta_description": description, "keyphrase": sd.get("keyphrase") or "",
        "jsonld": seo.jsonld(title=d.title, description=description, language=d.language, url=None,
                             keyphrase=sd.get("keyphrase") or "", published=datetime.now(UTC).isoformat(),
                             site_name=site.name if site else None,
                             site_url=f"https://{site.domain}/" if site else None, citations=urls,
                             faq=[x for x in faq if x[1]], recipe=recipe),
    }


def without_title(md: str) -> str:
    """The article without its first heading (the title), for a CMS that
    shows the title from its own field; the subtitle stays as the lead."""
    head, sep, rest = md.lstrip().partition("\n")
    return rest.lstrip("\n") if head.startswith("# ") else md


def fits_language(target, language: str | None) -> bool:
    """A destination set for one language takes only articles in it; one
    set for none takes any."""
    want = ((target.config or {}).get("language") or "").strip()[:2].lower()
    return not want or want == (language or "")[:2].lower()


def cover_changed(image, pub) -> bool:
    """Whether the cover was made after the article last went to its site
    (published or updated): then an update has to send it too."""
    if image is None:
        return False
    sent, made = pub.updated_at or pub.published_at, getattr(image, "created_at", None)
    return sent is None or made is None or made > sent


def build_payload(s, d) -> dict:
    """What a site receives for a draft: text, HTML and search fields."""
    body = d.body_md
    from .autopilot import recipe_site

    profile = s.execute(select(m.editorial_profiles.c.body).where(
        m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar() or {}
    style = (profile.get("settings") or {}).get("sources_style")
    if style == "none" or (style != "list" and recipe_site(profile)):
        body = drafts.without_sources(body)
    elif style == "list":
        body = drafts.discreet_sources(body)
    if (d.seo or {}).get("menu"):  # a menu links its new recipes by their address on the site
        from . import menus

        try:
            body = menus.resolve(s, body)
        except ValueError as e:
            raise PublishError(str(e)) from None
    return {"title": d.title, "subtitle": d.subtitle, "markdown": body, "html": to_html(body),
            "language": d.language, **search_fields(s, d)}


def request(db, tenant_id: str, draft_id: int, target_id: int, mode: str, by: str) -> int:
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        t = s.execute(select(m.publish_targets).where(m.publish_targets.c.id == target_id)).first()
        if d is None or t is None:
            raise PublishError("draft or target not found")
        if d.status != "approved":
            raise PublishError("only approved drafts can be published")
        if quotas.proposals_only(s):
            raise PublishError("the trial is over: choose a plan to publish")
        if t.site_id != d.site_id or not t.enabled:
            raise PublishError("target not available for this site")
        if not fits_language(t, d.language):
            raise PublishError(f"the destination {t.name} takes articles in {t.config.get('language')}, "
                               f"this one is in {d.language}")
        payload = build_payload(s, d)
        return s.execute(insert(m.publications).values(
            tenant_id=tenant_id, draft_id=draft_id, target_id=target_id, mode=mode, payload=payload, requested_by=by,
        ).returning(m.publications.c.id)).scalar_one()


def decide(db, tenant_id: str, pub_id: int, approve: bool, by: str, note: str | None = None) -> bool:
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.publications.c.status).where(m.publications.c.id == pub_id)).first()
        if row is None or row.status != "pending_approval":
            return False
        s.execute(update(m.publications).where(m.publications.c.id == pub_id).values(
            status="approved" if approve else "rejected", decided_by=by, decided_at=datetime.now(UTC), decision_note=note))
    return True


# A publication a director authorized that waits for «Pubblica le autorizzate», to go out with the
# others in one update of the site (Francesco 2026-11-14: five recipes one after the other made ten
# Vercel builds). It is approved, with this note, and nothing has queued it yet.
HELD = "held"


def hold(db, tenant_id: str, pub_id: int) -> None:
    with db.tenant(tenant_id) as s:
        s.execute(update(m.publications).where(m.publications.c.id == pub_id,
                                               m.publications.c.status == "approved").values(decision_note=HELD))


def held_count(s) -> int:
    p = m.publications.c
    return s.execute(select(func.count()).select_from(m.publications).where(
        p.status == "approved", p.decision_note == HELD)).scalar() or 0


def release(db, tenant_id: str, send) -> int:
    """Send every authorized publication: those for the same GitHub
    repository and branch in one commit, `send(first, others, site_id)` once
    per group, any other on its own. Returns how many went."""
    p, tg = m.publications.c, m.publish_targets.c
    with db.tenant(tenant_id) as s:
        ids = list(s.execute(update(m.publications).where(p.status == "approved", p.decision_note == HELD)
                             .values(decision_note=None).returning(p.id)).scalars())
        rows = s.execute(select(p.id, p.mode, m.publish_targets, m.drafts.c.site_id.label("draft_site"))
                         .join(m.publish_targets, tg.id == p.target_id).join(m.drafts, m.drafts.c.id == p.draft_id)
                         .where(p.id.in_(ids or [0])).order_by(p.id)).all() if ids else []
    groups: dict = {}
    for r in rows:
        c = r.config or {}
        key = (r.kind, c.get("repo"), c.get("branch"), c.get("api"), r.secret_env, r.mode) if r.kind == "github" \
            else ("alone", r[0])
        groups.setdefault(key, []).append(r)
    for group in groups.values():
        send(group[0][0], [x[0] for x in group[1:]], group[0].draft_site)
    return len(rows)


def mark_uncertain_failed(db, tenant_id: str, pub_id: int, by: str) -> bool:
    """A person checked the CMS after a send whose outcome is unknown and
    found nothing: the publication counts as failed and can be requested
    again."""
    with db.tenant(tenant_id) as s:
        n = s.execute(update(m.publications).where(
            m.publications.c.id == pub_id, m.publications.c.status == "sending",
        ).values(status="failed", error=f"outcome unknown; checked on the CMS by {by}")).rowcount
    return bool(n)


# The theme's places for the main menu, the likeliest first; else the first it has.
MENU_PLACES = ("primary", "main", "menu-1", "primary-menu", "main-menu", "header", "header-menu", "top")
MENU_NAME = "Redazia"


class _Staged:
    """A repository as it will be once the files staged for the same commit
    are in: what is staged is read from there, the rest from GitHub."""

    def __init__(self, gh, staged: dict):
        self._gh, self._staged = gh, staged

    def file(self, path: str, ref: str):
        return self._staged[path] if path in self._staged else self._gh.file(path, ref)

    def __getattr__(self, name):
        return getattr(self._gh, name)


class Publisher:
    def __init__(self, http: httpx.Client | None = None, resolver=None, telegram_api="https://api.telegram.org",
                 public_url: str = "", sleep=None):
        self.http = http or pinned_client(resolver)
        self.public_url = public_url.rstrip("/")  # where the sites fetch the cover image (/media/<token>)
        self.resolver = resolver
        self.telegram_api = telegram_api
        self.sleep = sleep or __import__("time").sleep  # waiting for a site's checks

    def run(self, db, tenant_id: str, pub_id: int, together=()) -> dict:
        """Execute an approved publication. Refuses anything not approved.

        The publication is marked ``sending`` before the request leaves: two
        workers cannot both send it, and a process that dies mid-way leaves
        it ``sending`` for a person to check on the CMS (see
        `mark_uncertain_failed`) instead of sending it again.

        `together`: other approved publications, the article's translations,
        that go in the same commit when they are for the same GitHub
        repository and branch; any other is sent on its own after it."""
        if together:
            return self._run_together(db, tenant_id, pub_id, list(together))
        with db.tenant(tenant_id) as s:
            pub = s.execute(update(m.publications).where(
                m.publications.c.id == pub_id, m.publications.c.status == "approved",
                m.publications.c.decided_by.is_not(None),
            ).values(status="sending", attempts=m.publications.c.attempts + 1).returning(m.publications)).first()
            if pub is None:
                cur = s.execute(select(m.publications).where(m.publications.c.id == pub_id)).first()
                if cur is None:
                    raise PublishError("publication not found")
                if cur.status == "published":
                    return {"status": "published", "url": cur.external_url}
                if cur.status == "sending":
                    return {"status": "sending"}
                raise PublishError(f"publication is {cur.status}: it needs a person's approval first")
            target = s.execute(select(m.publish_targets).where(m.publish_targets.c.id == pub.target_id)).first()
            cms_url = s.execute(select(m.publications.c.external_url).where(
                m.publications.c.draft_id == pub.draft_id, m.publications.c.status == "published",
                m.publications.c.external_url.is_not(None)).limit(1)).scalar()
            image = images.content(s, pub.draft_id)
        secret = None
        try:
            secret = resolve_secret(db, target) if target.secret_env else None
            for pause in (*GITHUB_AGAIN, None):
                try:
                    ext_id, ext_url = getattr(self, f"_{target.kind}")(target, pub, cms_url, secret, image)
                    break
                except (PublishError, httpx.HTTPError) as e:
                    # GitHub failing on its side: the whole publication, which GitHub's flow can repeat
                    # (the branch and the pull request are found again), is tried once more after a pause.
                    if target.kind != "github" or pause is None or not _github_hiccup(e):
                        raise
                    log.warning("GitHub hiccup publishing %s (%s): trying again in %s s", pub_id, e, pause)
                    self.sleep(pause)
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"
            if secret:  # a token can sit in the request URL (Telegram)
                msg = msg.replace(secret, "***")
            with db.tenant(tenant_id) as s:
                s.execute(update(m.publications).where(m.publications.c.id == pub_id).values(
                    status="failed", error=msg[:500]))
            return {"status": "failed", "error": msg}
        with db.tenant(tenant_id) as s:
            s.execute(update(m.publications).where(m.publications.c.id == pub_id).values(
                status="published", published_at=datetime.now(UTC), external_id=ext_id, external_url=ext_url, error=None))
            if target.kind in seo.SITE_TARGETS and pub.mode == "publish":
                seo.remember_published(s, tenant_id, target.site_id)
        out = {"status": "published", "url": ext_url}
        key = (target.config or {}).get("indexnow_key")
        if key and ext_url and pub.mode == "publish":
            out["indexnow"] = self.indexnow(ext_url, key)
        return out

    def _run_together(self, db, tenant_id: str, pub_id: int, others: list[int]) -> dict:
        p, tg = m.publications.c, m.publish_targets.c

        def where(t):
            c = t.config or {}
            return (t.kind, c.get("repo"), c.get("branch"), c.get("api"), t.secret_env)

        with db.tenant(tenant_id) as s:
            rows = {r.pub_id: r for r in s.execute(select(p.id.label("pub_id"), m.publish_targets).join(
                m.publish_targets, tg.id == p.target_id).where(p.id.in_([pub_id, *others]))).all()}
        first = rows.get(pub_id)
        same = [i for i in others if first is not None and first.kind == "github" and i in rows
                and where(rows[i]) == where(first)]
        alone = [i for i in others if i not in same]
        if not same:
            out = self.run(db, tenant_id, pub_id)
        else:
            out = self._send_together(db, tenant_id, [pub_id, *same])
        for i in alone:
            self.run(db, tenant_id, i)
        return out

    def _send_together(self, db, tenant_id: str, ids: list[int]) -> dict:
        """The publications of `ids`, all for one GitHub repository, in one
        commit. Those no longer approved (already sent) are left out."""
        p = m.publications.c
        with db.tenant(tenant_id) as s:
            pubs = s.execute(update(m.publications).where(
                p.id.in_(ids), p.status == "approved", p.decided_by.is_not(None),
            ).values(status="sending", attempts=p.attempts + 1).returning(m.publications)).all()
            pubs = sorted(pubs, key=lambda r: ids.index(r.id))
            if not pubs:
                cur = s.execute(select(m.publications).where(p.id == ids[0])).first()
                if cur is None:
                    raise PublishError("publication not found")
                return {"status": cur.status, "url": cur.external_url}
            targets = {t.id: t for t in s.execute(select(m.publish_targets).where(
                m.publish_targets.c.id.in_({x.target_id for x in pubs}))).all()}
            covers = {}
            for x in pubs:
                covers[x.id] = images.content(s, x.draft_id)
        head, rest = pubs[0], pubs[1:]
        target = targets[head.target_id]
        more = [(targets[x.target_id], x, covers[x.id]) for x in rest]
        secret = None
        try:
            secret = resolve_secret(db, target) if target.secret_env else None
            for pause in (*GITHUB_AGAIN, None):
                try:
                    ext_id, urls = self._github(target, head, None, secret, covers[head.id], more=more)
                    break
                except (PublishError, httpx.HTTPError) as e:
                    if pause is None or not _github_hiccup(e):
                        raise
                    log.warning("GitHub hiccup publishing %s (%s): trying again in %s s", head.id, e, pause)
                    self.sleep(pause)
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"
            if secret:
                msg = msg.replace(secret, "***")
            with db.tenant(tenant_id) as s:
                s.execute(update(m.publications).where(p.id.in_([x.id for x in pubs])).values(
                    status="failed", error=msg[:500]))
            return {"status": "failed", "error": msg}
        with db.tenant(tenant_id) as s:
            for x, url in zip(pubs, urls):
                s.execute(update(m.publications).where(p.id == x.id).values(
                    status="published", published_at=datetime.now(UTC), external_id=ext_id, external_url=url,
                    error=None))
            if head.mode == "publish":
                seo.remember_published(s, tenant_id, target.site_id)
        out = {"status": "published", "url": urls[0], "together": [x.id for x in rest]}
        if head.mode == "publish":
            for x, url in zip(pubs, urls):
                key = (targets[x.target_id].config or {}).get("indexnow_key")
                if key and url:
                    out.setdefault("indexnow", self.indexnow(url, key))
        return out

    # --- after publication: the director keeps the article in hand ----------------------

    def update(self, db, tenant_id: str, pub_id: int, by: str, draft_id: int | None = None) -> dict:
        """Replace the published article with the current text of its draft,
        or with `draft_id`, a newer approved version of the same article
        (written again): same destination, same address. A withdrawn
        article goes back online."""
        with db.tenant(tenant_id) as s:
            pub = s.execute(select(m.publications).where(m.publications.c.id == pub_id)).first()
            if pub is None:
                raise PublishError("publication not found")
            if pub.status != "published":
                raise PublishError("only a published article can be updated")
            old = s.execute(select(m.drafts).where(m.drafts.c.id == pub.draft_id)).first()
            d = old if draft_id in (None, pub.draft_id) else s.execute(
                select(m.drafts).where(m.drafts.c.id == draft_id)).first()
            if d is None:
                raise PublishError("draft not found")
            if d.id != old.id and (d.proposal_id != old.proposal_id or d.language != old.language
                                   or d.translation_of != old.translation_of):
                raise PublishError("the new version must be of the same article, in the same language")
            if d.status != "approved":
                raise PublishError("approve the draft first: what goes online is what a person approved")
            target = s.execute(select(m.publish_targets).where(m.publish_targets.c.id == pub.target_id)).first()
            payload = build_payload(s, d)
            if (pub.payload or {}).get("slug"):  # the address stays the same
                payload["slug"] = pub.payload["slug"]
            image = images.content(s, d.id)
        result = self._change(db, tenant_id, pub, target, "update", payload, image)
        if result["status"] == "failed":
            return result
        with db.tenant(tenant_id) as s:
            s.execute(update(m.publications).where(m.publications.c.id == pub_id).values(
                draft_id=d.id, payload=payload, updated_at=datetime.now(UTC), updated_by=by,
                withdrawn_at=None, withdrawn_by=None, error=None,
                external_url=result.get("url") or pub.external_url))
        return {"status": "updated", "url": result.get("url") or pub.external_url}

    def withdraw(self, db, tenant_id: str, pub_id: int, by: str) -> dict:
        """Take the published article off the site; for an original, its
        translations published on the site's other destinations too."""
        with db.tenant(tenant_id) as s:
            pub = s.execute(select(m.publications).where(m.publications.c.id == pub_id)).first()
            if pub is None:
                raise PublishError("publication not found")
            if pub.status != "published":
                raise PublishError("only a published article can be withdrawn")
            p, d = m.publications, m.drafts
            family = [pub] + list(s.execute(select(p).join(d, d.c.id == p.c.draft_id).where(
                d.c.translation_of == pub.draft_id, p.c.status == "published", p.c.withdrawn_at.is_(None))).all())
            targets = {x.target_id: s.execute(select(m.publish_targets).where(
                m.publish_targets.c.id == x.target_id)).first() for x in family}
        done, errors = [], []
        for x in family:
            if x.withdrawn_at is not None:
                continue
            result = self._change(db, tenant_id, x, targets[x.target_id], "withdraw", x.payload, None)
            if result["status"] == "failed":
                errors.append(result["error"])
                continue
            with db.tenant(tenant_id) as s:
                s.execute(update(m.publications).where(m.publications.c.id == x.id).values(
                    withdrawn_at=datetime.now(UTC), withdrawn_by=by, error=None))
                if x.external_url:  # no longer further reading for the next articles
                    s.execute(delete(m.site_posts).where(m.site_posts.c.url == x.external_url))
            done.append(x.id)
        if errors:
            return {"status": "failed", "error": "; ".join(errors), "withdrawn": done}
        return {"status": "withdrawn", "withdrawn": done}

    def _change(self, db, tenant_id: str, pub, target, op: str, payload: dict, image) -> dict:
        secret = None
        try:
            secret = resolve_secret(db, target) if target.secret_env else None
            url = getattr(self, f"_{target.kind}_{op}")(target, pub, payload, secret, image)
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"
            if secret:
                msg = msg.replace(secret, "***")
            with db.tenant(tenant_id) as s:
                s.execute(update(m.publications).where(m.publications.c.id == pub.id).values(error=msg[:500]))
            return {"status": "failed", "error": msg}
        return {"status": "ok", "url": url}

    def _wp(self, method: str, base: str, route: str, query: str = "", **kw) -> httpx.Response:
        """A call to the WordPress REST API; where /wp-json/ is not routed
        (wp.aitoday.it, 2026-11-05) the same call goes by ?rest_route=."""
        r = self.http.request(method, wp_url(base, route, query), **kw)
        _ensure_no_redirect(r, base, self.resolver)
        if not_rest(r):
            r = self.http.request(method, wp_url(base, route, query, plain=True), **kw)
            _ensure_no_redirect(r, base, self.resolver)
        return r

    def _wordpress_update(self, target, pub, payload, secret, image=None):
        base = target.config["url"].rstrip("/")
        check_public_url(base, self.resolver)
        if not pub.external_id:
            raise PublishError("the post's id on WordPress is unknown: change it from WordPress")
        auth = (target.config["user"], _need(secret, target))
        post = self._wordpress_post(target, payload, pub.mode)
        if "categories" not in post and payload.get("section"):
            cat = self._wordpress_category(base, auth, payload["section"])
            if cat is not None:
                post["categories"] = [cat]
        if cover_changed(image, pub):
            # A cover made after the last send goes up with the text; an
            # unchanged one is not uploaded again at every update.
            post["featured_media"] = self._wordpress_media(base, auth, payload.get("slug"), image)
        r = self._wp("POST", base, f"wp/v2/posts/{quote(pub.external_id)}", auth=auth, json=post)
        r.raise_for_status()
        return r.json().get("link")

    def _wordpress_withdraw(self, target, pub, payload, secret, image=None):
        base = target.config["url"].rstrip("/")
        check_public_url(base, self.resolver)
        if not pub.external_id:
            raise PublishError("the post's id on WordPress is unknown: remove it from WordPress")
        # To WordPress's bin: it can still be restored from there.
        r = self._wp("DELETE", base, f"wp/v2/posts/{quote(pub.external_id)}",
                     auth=(target.config["user"], _need(secret, target)))
        if r.status_code not in (404, 410):  # gone already counts as withdrawn
            r.raise_for_status()
        return None

    @staticmethod
    def _webhook_answer(r) -> tuple[str | None, str | None]:
        """What the site answered, when it says it: {"id": ..., "url": "https://..."}, the
        article's own id and address, so the dashboard links the article on the site."""
        try:
            body = r.json()
        except ValueError:
            return None, None
        if not isinstance(body, dict):
            return None, None
        url = body.get("url")
        url = url if isinstance(url, str) and url.startswith(("https://", "http://")) and len(url) <= 2000 else None
        ext = body.get("id")
        return (str(ext)[:200] if isinstance(ext, (str, int)) and str(ext) else None), url

    def _webhook_event(self, target, pub, payload, secret, event: str, image=None):
        url = target.config["url"]
        check_public_url(url, self.resolver)
        data = {"event": event, "mode": pub.mode, "publication_id": pub.id, "url": pub.external_url}
        if event == "update":
            data |= {k: payload.get(k) for k in ("title", "subtitle", "markdown", "html", "language", "slug", "section",
                                                 "seo_title", "meta_description", "keyphrase", "jsonld",
                                                 "translation_of", "recipe")}
            # The cover as on publishing: the receiver replaces its own when the link changes.
            data["image"] = ({"url": f"{self.public_url}/media/{image.token}", "alt": image.alt, "type": image.mime}
                             if image is not None and self.public_url else None)
        else:
            data["slug"] = payload.get("slug")
        raw = json.dumps(data, ensure_ascii=False).encode()
        sig = hmac.new(_need(secret, target).encode(), raw, hashlib.sha256).hexdigest()
        r = self.http.post(url, content=raw, headers={"Content-Type": "application/json", "X-Editor-Signature": f"sha256={sig}"})
        _ensure_no_redirect(r, url, self.resolver)
        r.raise_for_status()
        return self._webhook_answer(r)[1] if event == "update" else None

    def _webhook_update(self, target, pub, payload, secret, image=None):
        return self._webhook_event(target, pub, payload, secret, "update", image)

    def _webhook_withdraw(self, target, pub, payload, secret, image=None):
        return self._webhook_event(target, pub, payload, secret, "withdraw")

    def _telegram_channel_update(self, target, pub, payload, secret, image=None):
        return None  # a message announces the article: the link still leads to it

    def _telegram_channel_withdraw(self, target, pub, payload, secret, image=None):
        if not pub.external_id:
            return None
        endpoint = f"{self.telegram_api}/bot{_need(secret, target)}/deleteMessage"
        r = self.http.post(endpoint, json={"chat_id": target.config["chat_id"], "message_id": int(pub.external_id)})
        _ensure_no_redirect(r, endpoint, self.resolver)
        if r.status_code != 400:  # too old to delete, or gone: Telegram keeps it, nothing more to do
            r.raise_for_status()
        return None

    def _github_update(self, target, pub, payload, secret, image=None):
        return self._github(target, pub, None, secret, image, op="update", payload=payload)[1]

    def _github_withdraw(self, target, pub, payload, secret, image=None):
        return self._github(target, pub, None, secret, None, op="withdraw", payload=payload)[1]

    def indexnow(self, url: str, key: str) -> str:
        """Tell Bing, and the engines that share IndexNow, that the page is
        new. The key file must be at the site's root (https://site/<key>.txt).
        A failure is reported, the publication stays published."""
        host = httpx.URL(url).host
        try:
            r = self.http.post(seo.INDEXNOW, json={"host": host, "key": key, "urlList": [url]},
                               headers={"Content-Type": "application/json; charset=utf-8"})
        except httpx.HTTPError as e:
            log.warning("IndexNow for %s failed: %s", url, e)
            return f"failed: {type(e).__name__}"
        if r.status_code >= 300:
            log.warning("IndexNow for %s answered %s", url, r.status_code)
            return f"failed: HTTP {r.status_code}"
        return "sent"

    def _wordpress(self, target, pub, cms_url, secret, image=None):
        base = target.config["url"].rstrip("/")
        check_public_url(base, self.resolver)
        auth = (target.config["user"], _need(secret, target))
        post = self._wordpress_post(target, pub.payload, pub.mode)
        if "categories" not in post and pub.payload.get("section"):
            cat = self._wordpress_category(base, auth, pub.payload["section"])
            if cat is not None:
                post["categories"] = [cat]
        if image is not None:
            post["featured_media"] = self._wordpress_media(base, auth, pub.payload.get("slug"), image)
        r = self._wp("POST", base, "wp/v2/posts", auth=auth, json=post)
        r.raise_for_status()
        body = r.json()
        return str(body["id"]), body.get("link")

    def _wordpress_category(self, base: str, auth, name: str) -> int | None:
        """The category of a section the site did not have when it was
        connected: found by name, or created (Francesco 2026-11-05: the
        sections are decided in Redazia, a new site has none). None when the
        user may not create categories: the article goes out without one."""
        name = name.strip()[:100]
        try:
            r = self._wp("GET", base, "wp/v2/categories", f"search={quote(name)}&per_page=20&_fields=id,name",
                         auth=auth)
            if r.status_code == 200 and isinstance(r.json(), list):
                for c in r.json():
                    if html.unescape(str(c.get("name") or "")).strip().lower() == name.lower():
                        return int(c["id"])
            r = self._wp("POST", base, "wp/v2/categories", auth=auth, json={"name": name})
            if r.status_code in (200, 201):
                return int(r.json()["id"])
            body = r.json() if "json" in r.headers.get("content-type", "") else {}
            term = (body.get("data") or {}).get("term_id") if isinstance(body, dict) else None
            if term:  # created meanwhile by someone else
                return int(term)
            log.warning("category %r not created on %s: HTTP %s", name, base, r.status_code)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("category %r on %s: %s", name, base, e)
        return None

    def wordpress_menu(self, db, target, sections: list[str]) -> dict:
        """The site's menu with an entry for each of Redazia's sections, in
        their order (Francesco 2026-11-05: "creare anche le voci del menu
        dalle categorie scelte dal backoffice"). The menu in the theme's main
        place is used, or one is created there; a missing category is
        created; entries already there are left as they are.

        Returns {menu, location, added, present, categories, missing}, or
        {error, categories, missing}:
        "role" when the user may not change menus (WordPress gives that to
        administrators only), "block_theme" when the theme has no menu places
        (its navigation is edited in the site editor)."""
        base = target.config["url"].rstrip("/")
        check_public_url(base, self.resolver)
        auth = (target.config["user"], _need(resolve_secret(db, target), target))
        # The categories first: an editor may create them, even where the menu is not theirs to change.
        cats, missing = {}, []
        for name in sections:
            cat = self._wordpress_category(base, auth, name)
            if cat is None:
                missing.append(name)
            else:
                cats[name] = cat
        made = {"categories": len(cats), "missing": missing}
        r = self._wp("GET", base, "wp/v2/menu-locations", auth=auth)
        if r.status_code in (401, 403):
            return {"error": "role", **made}
        r.raise_for_status()
        places = r.json() if isinstance(r.json(), dict) else {}
        if not places:
            return {"error": "block_theme", **made}
        location = next((k for k in MENU_PLACES if k in places), next(iter(places)))
        menu = int((places[location] or {}).get("menu") or 0)
        if not menu:
            r = self._wp("GET", base, "wp/v2/menus", "per_page=100&_fields=id,name", auth=auth)
            r.raise_for_status()
            menu = next((int(x["id"]) for x in r.json() if html.unescape(str(x.get("name") or "")) == MENU_NAME), 0)
            if menu:
                r = self._wp("POST", base, f"wp/v2/menus/{menu}", auth=auth, json={"locations": [location]})
            else:
                r = self._wp("POST", base, "wp/v2/menus", auth=auth, json={"name": MENU_NAME, "locations": [location]})
            if r.status_code in (401, 403):
                return {"error": "role", **made}
            r.raise_for_status()
            menu = int(r.json()["id"])
        r = self._wp("GET", base, "wp/v2/menu-items", f"menus={menu}&per_page=100&_fields=id,object,object_id",
                     auth=auth)
        r.raise_for_status()
        there = {int(x.get("object_id") or 0) for x in r.json() if x.get("object") == "category"}
        added, present = [], 0
        for n, name in enumerate(sections):
            cat = cats.get(name)
            if cat is None:
                continue
            if cat in there:
                present += 1
            else:
                r = self._wp("POST", base, "wp/v2/menu-items", auth=auth, json={
                    "title": name, "menus": menu, "type": "taxonomy", "object": "category", "object_id": cat,
                    "status": "publish", "menu_order": n + 1})
                if r.status_code in (401, 403):
                    return {"error": "role", **made}
                r.raise_for_status()
                there.add(cat)
                added.append(name)
        return {"menu": menu, "location": location, "added": added, "present": present, **made}

    def _wordpress_media(self, base: str, auth, slug: str | None, image) -> int:
        """The cover image to the media library, with its alternative text:
        its id, for the post's featured image."""
        ext = "png" if image.mime == "image/png" else "jpg"
        r = self._wp("POST", base, "wp/v2/media", auth=auth, content=image.content, headers={
            "Content-Type": image.mime, "Content-Disposition": f'attachment; filename="{slug or "cover"}.{ext}"'})
        r.raise_for_status()
        media = r.json()["id"]
        if image.alt:
            r = self._wp("POST", base, f"wp/v2/media/{media}", auth=auth, json={"alt_text": image.alt})
        return media

    @staticmethod
    def _wordpress_post(target, payload: dict, mode: str) -> dict:
        # The title goes in WordPress's own field, which the theme shows: in
        # the text too it came out twice (wp.aitoday.it, 2026-11-05).
        content = to_html(without_title(payload["markdown"])) if payload.get("markdown") else payload["html"]
        if (target.config or {}).get("jsonld") and payload.get("jsonld"):
            # Only for sites where the user may publish scripts (unfiltered_html):
            # elsewhere WordPress would show it as text.
            data = json.dumps(payload["jsonld"], ensure_ascii=False).replace("</", "<\\/")
            content += f'\n<script type="application/ld+json">{data}</script>'
        post = {"title": payload["title"], "content": content,
                "excerpt": payload.get("meta_description") or payload["subtitle"],
                "status": "publish" if mode == "publish" else "draft"}
        if payload.get("slug"):
            post["slug"] = payload["slug"]
        # The article's section as the category the site already has (read when the site was connected).
        from .connect import category_id

        cat = category_id(target.config, payload.get("section"))
        if cat is not None:
            post["categories"] = [cat]
        return post

    def _webhook(self, target, pub, cms_url, secret, image=None):
        url = target.config["url"]
        check_public_url(url, self.resolver)
        cover = None
        if image is not None and self.public_url:
            cover = {"url": f"{self.public_url}/media/{image.token}", "alt": image.alt, "type": image.mime}
        data = json.dumps({"event": "publication", "mode": pub.mode, "publication_id": pub.id,
                           "url": cms_url, **{k: pub.payload[k] for k in ("title", "subtitle", "markdown", "html", "language")},
                           **{k: pub.payload.get(k) for k in ("slug", "section", "seo_title", "meta_description", "keyphrase",
                                                              "jsonld", "translation_of", "recipe")},
                           "date": datetime.now(UTC).isoformat(), "image": cover},
                          ensure_ascii=False).encode()
        sig = hmac.new(_need(secret, target).encode(), data, hashlib.sha256).hexdigest()
        r = self.http.post(url, content=data, headers={"Content-Type": "application/json", "X-Editor-Signature": f"sha256={sig}"})
        _ensure_no_redirect(r, url, self.resolver)
        r.raise_for_status()
        return self._webhook_answer(r)

    def _telegram_channel(self, target, pub, cms_url, secret, image=None):
        import html as h
        text = f"<b>{h.escape(pub.payload['title'])}</b>"
        if pub.payload.get("subtitle"):
            text += f"\n{h.escape(pub.payload['subtitle'])}"
        if cms_url:
            text += f'\n\n<a href="{h.escape(cms_url, quote=True)}">{h.escape(cms_url)}</a>'
        endpoint = f"{self.telegram_api}/bot{_need(secret, target)}/sendMessage"
        r = self.http.post(endpoint,
                           json={"chat_id": target.config["chat_id"], "text": text, "parse_mode": "HTML"})
        _ensure_no_redirect(r, endpoint, self.resolver)
        r.raise_for_status()
        return str(r.json().get("result", {}).get("message_id")), None

    def _github(self, target, pub, cms_url, secret, image=None, op: str = "publish", payload: dict | None = None,
                more=()):
        """The article committed to the repository the site is deployed from
        (Vercel, Netlify, GitHub Pages), in the shape of the blog's own posts,
        with its cover, a line in llms.txt and, if the site keeps it as a
        file, the sitemap: one commit, so one deploy (sitegit.py).

        "publish": the commit goes on a branch with a pull request, which is
        merged once the site's checks (the preview deploy) pass; a failed
        build leaves the site as it was and the pull request open. With
        ``verify: false`` it goes straight on the site's branch. "draft":
        the pull request only, for a person to merge. A send interrupted
        after the merge is safe to repeat: the article already there with
        the same content counts as sent, with other content it is an error.

        `more`: other publications, as (target, publication, image), sent in
        the same commit, the translations of the article on the same
        repository: the site builds once for all its languages (Francesco
        2026-11-13, Vercel bills each build). Then the second value returned
        is the list of every article's address, this one first."""
        from . import delivery, sitegit

        api = (target.config.get("api") or GITHUB_API).rstrip("/")
        repo = target.config["repo"]
        cfg = target.config
        headers = {"Authorization": f"Bearer {_need(secret, target)}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "eullm-editor"}

        def call(method: str, path: str, **kw) -> httpx.Response:
            r = self.http.request(method, f"{api}{path}", headers={**headers, **kw.pop("headers", {})}, **kw)
            _ensure_no_redirect(r, api, self.resolver)
            return r

        info = call("GET", f"/repos/{repo}")
        if info.status_code == 404:
            raise PublishError(f"repository {repo} not found, or the token cannot see it")
        if info.status_code in (401, 403):
            raise PublishError("GitHub refused the token (expired, or without write access to this repository)")
        info.raise_for_status()
        base = cfg.get("branch") or info.json()["default_branch"]
        gh = sitegit.Repo(call, repo, sleep=self.sleep)
        # Two articles sent together both add their line to llms.txt and the
        # sitemap: the second pull request conflicts once the first is merged
        # (Francesco 2026-11-11, "405 Pull Request has merge conflicts"). It
        # is built again on top of the site as it is now, at most twice more.
        for attempt in range(3):
            try:
                items = [(cfg, pub, image, payload or pub.payload)] + [(t.config, p, i, p.payload) for t, p, i in more]
                sha, urls = self._github_send(gh, items, base, delivery, sitegit, op)
                return (sha, urls) if more else (sha, urls[0])
            except sitegit.GitHubError as e:
                text_ = str(e).lower()
                if attempt < 2 and "merge" in text_ and ("conflict" in text_ or "base branch was modified" in text_):
                    self.sleep(5)
                    continue
                raise PublishError(str(e)) from None

    def _github_send(self, gh, items, base, delivery, sitegit, op="publish"):
        """One commit with the articles of `items`, (config, publication, image,
        payload) each: each one sees the files of those before it, so their
        lines in llms.txt and the sitemap add up."""
        staged: dict[str, bytes | None] = {}
        view = _Staged(gh, staged)
        urls, titles, first_url = [], [], None
        for cfg, _pub, image, payload in items:
            files, url, blob_url = self._github_files(view, cfg, base, image, delivery, sitegit, op, payload)
            staged.update(files)
            urls.append(url or blob_url)
            titles.append(payload["title"])
            first_url = url if len(urls) == 1 else first_url
        if not staged:  # nothing of it on the site any more, or all there already
            return None, urls
        files = list(staged.items())
        cfg, pub, title, article_url = items[0][0], items[0][1], titles[0], first_url
        message = {"publish": "editor", "update": "editor: update", "withdraw": "editor: withdraw"}[op] + f": {title}"
        if len(titles) > 1:
            message += "".join(f"\n\n{x}" for x in titles[1:])
        if pub.mode == "publish" and cfg.get("verify") is False:
            for attempt in (1, 2):  # the branch moved under us: once more on top of it
                sha = gh.commit(gh.head(base), files, message)
                try:
                    gh.point(base, sha)
                    break
                except sitegit.GitHubError:
                    if attempt == 2:
                        raise
            return sha, urls

        branch = f"editor/{op}-{pub.id}"
        sha = gh.commit(gh.head(base), files, message)
        gh.point(branch, sha, create=True)
        note = {"publish": "Articolo di Redazia.", "update": "Articolo aggiornato da Redazia.",
                "withdraw": "Articolo ritirato da Redazia."}[op] + (f" Indirizzo: {article_url}" if article_url else "")
        pr = gh.pull(branch, base, title, note)
        if pub.mode == "draft":
            return str(pr["number"]), [pr["html_url"]] + urls[1:]
        minutes = float(cfg.get("wait_minutes") or 15)
        state, link = gh.wait(sha, minutes)
        if state == "failure":
            raise PublishError(f"the site's checks failed ({link or pr['html_url']}): the site did not change, "
                               f"pull request {pr['html_url']} is open")
        if state == "timeout":
            raise PublishError(f"the site's checks did not finish in {minutes:g} minutes: the site did not change, "
                               f"pull request {pr['html_url']} is open")
        merged = gh.merge(pr["number"], message)
        gh.drop(branch)
        return merged or sha, [article_url or pr["html_url"]] + urls[1:]

    def _github_files(self, gh, cfg, base, image, delivery, sitegit, op, payload):
        """The files of one article for the commit: the post, its cover, and
        llms.txt and the sitemap as they become with it. Returns them with
        the article's address (None if unknown) and its file on GitHub; no
        files when the site already has it."""
        import posixpath

        title = payload["title"]
        slug = re.sub(r"[^a-z0-9-]+", "-", (payload.get("slug") or "").lower()).strip("-") or "article"
        published = next((x.get("datePublished") for x in payload.get("jsonld") or [] if x.get("datePublished")), None)
        try:
            when = datetime.fromisoformat(published) if published else sitegit.now()
        except ValueError:
            when = sitegit.now()
        articles = cfg.get("path", "content/blog")
        url_pattern, routed = cfg.get("url"), False
        if payload.get("recipe"):
            # A recipe goes where the site keeps its recipes, found by itself (connect.github
            # notes it; else looked for next to the articles), with its own address.
            recipes = cfg.get("recipe_path") or gh.recipe_folder(articles, base)
            if recipes and recipes != articles:
                articles, url_pattern, routed = recipes, cfg.get("recipe_url"), True
        # The blog's other posts: this one, sent before, must not teach its own shape.
        fmt = sitegit.learn([p for p in gh.posts(articles, base, limit=sitegit.SAMPLES + 1) if p.slug != slug][:sitegit.SAMPLES])
        post_dir = f"{articles}/{slug}" if fmt.folder else articles
        post_path = f"{post_dir}/index{fmt.ext}" if fmt.folder else f"{articles}/{slug}{fmt.ext}"

        files: list[tuple[str, bytes]] = []
        image_ref = None
        if image is not None:
            ext = "png" if image.mime == "image/png" else "jpg"
            if fmt.image_mode == "relative" and fmt.image_dir:
                own_folder = fmt.folder and fmt.samples and fmt.image_dir == posixpath.dirname(fmt.samples[0].path)
                img_dir = post_dir if own_folder else fmt.image_dir
                img_path = f"{img_dir}/{'cover' if own_folder else slug}.{ext}"
                rel = posixpath.relpath(img_path, post_dir)
                image_ref = rel if rel.startswith(".") else f"./{rel}"
            else:
                guessed = None
                if fmt.image_sample and fmt.image_sample.startswith("/"):
                    guessed = "public" + posixpath.dirname(fmt.image_sample)
                img_dir = (guessed if routed else None) or cfg.get("image_path") or guessed or "public/images"
                img_path = f"{img_dir}/{slug}.{ext}"
                image_ref = site_path(img_path)
            files.append((img_path, bytes(image.content)))
        body = delivery.body_md(payload["markdown"])
        if fmt.ext == ".json":
            content = sitegit.json_post(fmt, payload, slug, when, image_ref, image.alt if image is not None else None,
                                        body).encode()
        else:
            md = sitegit.front_matter(fmt, payload, slug, when, image_ref, image.alt if image is not None else None) + "\n"
            if not fmt.learned and payload.get("subtitle"):
                body = f"_{payload['subtitle']}_\n\n{body}"
            md += (sitegit.mdx_safe(body) if fmt.ext == ".mdx" else body) + "\n"
            content = md.encode()

        texts = {}
        for path in ("public/llms.txt", "static/llms.txt", "llms.txt", "public/sitemap.xml", "static/sitemap.xml"):
            found = gh.file(path, base)
            if found is not None:
                texts[path] = found.decode("utf-8", "replace")
        pattern = url_pattern or sitegit.guess_url(fmt, articles, payload.get("site_domain"), list(texts.values()))
        article_url = pattern.replace("{slug}", slug) if pattern else None
        blob_url = f"https://github.com/{gh.repo}/blob/{base}/{post_path}"

        existing = gh.file(post_path, base)
        if op == "withdraw":
            files = [(post_path, None)] if existing is not None else []
            # The cover sent with it: the article's own name, in the usual places.
            for folder in dict.fromkeys(x for x in (cfg.get("image_path"), "public/images",
                                                     post_dir if fmt.folder else None) if x):
                for name in (f"{slug}.jpg", f"{slug}.png", "cover.jpg", "cover.png"):
                    if name.startswith("cover") and folder != post_dir:
                        continue
                    if gh.file(f"{folder}/{name}", base) is not None:
                        files.append((f"{folder}/{name}", None))
        elif existing is not None and op == "publish":
            if existing == content:
                return [], article_url, blob_url
            raise PublishError(f"{post_path} already exists in {gh.repo} with other content: not overwritten")
        elif existing == content and not files:
            return [], article_url, blob_url
        if op != "withdraw":
            files.append((post_path, content))
        if article_url:
            for path, text_ in texts.items():
                if path.endswith("llms.txt"):
                    changed = text_ if op == "publish" else (sitegit.remove_from_llms(text_, article_url) or text_)
                    if op != "withdraw":
                        changed = sitegit.add_to_llms(changed, title, article_url,
                                                      payload.get("meta_description") or payload.get("subtitle"),
                                                      pattern.split("{slug}")[0]) or changed
                else:
                    changed = sitegit.remove_from_sitemap(text_, article_url) if op == "withdraw" \
                        else sitegit.add_to_sitemap(text_, article_url, when)
                if changed and changed != text_:
                    files.append((path, changed.encode()))
        return files, article_url, blob_url

