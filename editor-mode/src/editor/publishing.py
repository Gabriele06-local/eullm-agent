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
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from . import drafts, models as m
from . import seo
from .markdown import to_html

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


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
    for them (default 15) and ``api`` for a GitHub Enterprise server."""
    config = dict(config or {})
    repo = str(config.get("repo", "")).strip()
    if not GITHUB_REPO.fullmatch(repo) or ".." in repo:
        raise PublishError("a GitHub target needs the repository as owner/name, e.g. primoco/miosito")
    config["repo"] = repo
    for key in ("branch", "path", "image_path"):
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
    url = str(config.get("url") or "").strip()
    if url:
        if not url.startswith("https://") or "{slug}" not in url or any(c.isspace() for c in url):
            raise PublishError("the articles' address is an https address with {slug} in it, e.g. https://site.it/blog/{slug}")
        config["url"] = url
    else:
        config.pop("url", None)
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
    return {
        "site_domain": site.domain if site else None, "section": section,
        "slug": sd.get("slug") or seo.slugify(d.title), "seo_title": sd.get("title") or d.title,
        "meta_description": description, "keyphrase": sd.get("keyphrase") or "",
        "jsonld": seo.jsonld(title=d.title, description=description, language=d.language, url=None,
                             keyphrase=sd.get("keyphrase") or "", published=datetime.now(UTC).isoformat(),
                             site_name=site.name if site else None,
                             site_url=f"https://{site.domain}/" if site else None, citations=urls,
                             faq=[x for x in faq if x[1]]),
    }


def request(db, tenant_id: str, draft_id: int, target_id: int, mode: str, by: str) -> int:
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        t = s.execute(select(m.publish_targets).where(m.publish_targets.c.id == target_id)).first()
        if d is None or t is None:
            raise PublishError("draft or target not found")
        if d.status != "approved":
            raise PublishError("only approved drafts can be published")
        if t.site_id != d.site_id or not t.enabled:
            raise PublishError("target not available for this site")
        body = d.body_md
        settings = (s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
            or {}).get("settings") or {}
        if settings.get("sources_style") == "list":
            body = drafts.discreet_sources(body)
        payload = {"title": d.title, "subtitle": d.subtitle, "markdown": body, "html": to_html(body),
                   "language": d.language, **search_fields(s, d)}
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


def mark_uncertain_failed(db, tenant_id: str, pub_id: int, by: str) -> bool:
    """A person checked the CMS after a send whose outcome is unknown and
    found nothing: the publication counts as failed and can be requested
    again."""
    with db.tenant(tenant_id) as s:
        n = s.execute(update(m.publications).where(
            m.publications.c.id == pub_id, m.publications.c.status == "sending",
        ).values(status="failed", error=f"outcome unknown; checked on the CMS by {by}")).rowcount
    return bool(n)


class Publisher:
    def __init__(self, http: httpx.Client | None = None, resolver=None, telegram_api="https://api.telegram.org",
                 public_url: str = "", sleep=None):
        self.http = http or pinned_client(resolver)
        self.public_url = public_url.rstrip("/")  # where the sites fetch the cover image (/media/<token>)
        self.resolver = resolver
        self.telegram_api = telegram_api
        self.sleep = sleep or __import__("time").sleep  # waiting for a site's checks

    def run(self, db, tenant_id: str, pub_id: int) -> dict:
        """Execute an approved publication. Refuses anything not approved.

        The publication is marked ``sending`` before the request leaves: two
        workers cannot both send it, and a process that dies mid-way leaves
        it ``sending`` for a person to check on the CMS (see
        `mark_uncertain_failed`) instead of sending it again."""
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
            image = s.execute(select(m.draft_images.c.mime, m.draft_images.c.content, m.draft_images.c.alt,
                                     m.draft_images.c.token)
                              .where(m.draft_images.c.draft_id == pub.draft_id)).first()
        secret = None
        try:
            secret = resolve_secret(db, target) if target.secret_env else None
            ext_id, ext_url = getattr(self, f"_{target.kind}")(target, pub, cms_url, secret, image)
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
        out = {"status": "published", "url": ext_url}
        key = (target.config or {}).get("indexnow_key")
        if key and ext_url and pub.mode == "publish":
            out["indexnow"] = self.indexnow(ext_url, key)
        return out

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
        if image is not None:
            # The cover image to the media library first, then as the post's featured image.
            ext = "png" if image.mime == "image/png" else "jpg"
            slug = pub.payload.get("slug") or "cover"
            r = self.http.post(f"{base}/wp-json/wp/v2/media", auth=auth, content=image.content, headers={
                "Content-Type": image.mime, "Content-Disposition": f'attachment; filename="{slug}.{ext}"'})
            _ensure_no_redirect(r, base, self.resolver)
            r.raise_for_status()
            media = r.json()["id"]
            if image.alt:
                r = self.http.post(f"{base}/wp-json/wp/v2/media/{media}", auth=auth, json={"alt_text": image.alt})
                _ensure_no_redirect(r, base, self.resolver)
            post["featured_media"] = media
        r = self.http.post(f"{base}/wp-json/wp/v2/posts", auth=auth, json=post)
        _ensure_no_redirect(r, base, self.resolver)
        r.raise_for_status()
        body = r.json()
        return str(body["id"]), body.get("link")

    @staticmethod
    def _wordpress_post(target, payload: dict, mode: str) -> dict:
        content = payload["html"]
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
        return post

    def _webhook(self, target, pub, cms_url, secret, image=None):
        url = target.config["url"]
        check_public_url(url, self.resolver)
        cover = None
        if image is not None and self.public_url:
            cover = {"url": f"{self.public_url}/media/{image.token}", "alt": image.alt, "type": image.mime}
        data = json.dumps({"event": "publication", "mode": pub.mode, "publication_id": pub.id,
                           "url": cms_url, **{k: pub.payload[k] for k in ("title", "subtitle", "markdown", "html", "language")},
                           **{k: pub.payload.get(k) for k in ("slug", "section", "seo_title", "meta_description", "keyphrase", "jsonld")},
                           "image": cover},
                          ensure_ascii=False).encode()
        sig = hmac.new(_need(secret, target).encode(), data, hashlib.sha256).hexdigest()
        r = self.http.post(url, content=data, headers={"Content-Type": "application/json", "X-Editor-Signature": f"sha256={sig}"})
        _ensure_no_redirect(r, url, self.resolver)
        r.raise_for_status()
        return None, None

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

    def _github(self, target, pub, cms_url, secret, image=None):
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
        the same content counts as sent, with other content it is an error."""
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
        try:
            return self._github_send(gh, cfg, pub, base, image, delivery, sitegit)
        except sitegit.GitHubError as e:
            raise PublishError(str(e)) from None

    def _github_send(self, gh, cfg, pub, base, image, delivery, sitegit):
        import posixpath

        payload = pub.payload
        title = payload["title"]
        slug = re.sub(r"[^a-z0-9-]+", "-", (payload.get("slug") or "").lower()).strip("-") or "article"
        published = next((x.get("datePublished") for x in payload.get("jsonld") or [] if x.get("datePublished")), None)
        try:
            when = datetime.fromisoformat(published) if published else sitegit.now()
        except ValueError:
            when = sitegit.now()
        articles = cfg.get("path", "content/blog")
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
                img_dir = cfg.get("image_path") or guessed or "public/images"
                img_path = f"{img_dir}/{slug}.{ext}"
                image_ref = site_path(img_path)
            files.append((img_path, bytes(image.content)))
        md = sitegit.front_matter(fmt, payload, slug, when, image_ref, image.alt if image is not None else None) + "\n"
        body = delivery.body_md(payload["markdown"])
        if not fmt.learned and payload.get("subtitle"):
            body = f"_{payload['subtitle']}_\n\n{body}"
        md += (sitegit.mdx_safe(body) if fmt.ext == ".mdx" else body) + "\n"
        content = md.encode()

        texts = {}
        for path in ("public/llms.txt", "static/llms.txt", "llms.txt", "public/sitemap.xml", "static/sitemap.xml"):
            found = gh.file(path, base)
            if found is not None:
                texts[path] = found.decode("utf-8", "replace")
        pattern = cfg.get("url") or sitegit.guess_url(fmt, articles, payload.get("site_domain"), list(texts.values()))
        article_url = pattern.replace("{slug}", slug) if pattern else None
        blob_url = f"https://github.com/{gh.repo}/blob/{base}/{post_path}"

        existing = gh.file(post_path, base)
        if existing is not None:
            if existing == content:
                return None, article_url or blob_url
            raise PublishError(f"{post_path} already exists in {gh.repo} with other content: not overwritten")
        files.append((post_path, content))
        if article_url:
            for path, text_ in texts.items():
                if path.endswith("llms.txt"):
                    changed = sitegit.add_to_llms(text_, title, article_url,
                                                  payload.get("meta_description") or payload.get("subtitle"),
                                                  pattern.split("{slug}")[0])
                else:
                    changed = sitegit.add_to_sitemap(text_, article_url, when)
                if changed:
                    files.append((path, changed.encode()))

        message = f"editor: {title}"
        if pub.mode == "publish" and cfg.get("verify") is False:
            for attempt in (1, 2):  # the branch moved under us: once more on top of it
                sha = gh.commit(gh.head(base), files, message)
                try:
                    gh.point(base, sha)
                    break
                except sitegit.GitHubError:
                    if attempt == 2:
                        raise
            return sha, article_url or blob_url

        branch = f"editor/publish-{pub.id}"
        sha = gh.commit(gh.head(base), files, message)
        gh.point(branch, sha, create=True)
        note = "Articolo di Editor Mode." + (f" Indirizzo: {article_url}" if article_url else "")
        pr = gh.pull(branch, base, title, note)
        if pub.mode == "draft":
            return str(pr["number"]), pr["html_url"]
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
        return merged or sha, article_url or pr["html_url"]
