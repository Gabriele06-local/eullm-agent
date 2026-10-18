"""Publishing behind approval.

A publication is requested for an approved draft and a target, waits in
`pending_approval` until a person decides, and only then is executed. The
target's address must resolve to public addresses (a tenant cannot point
Editor Mode at an internal service) and credentials come from environment
variables, never from the database. A tenant only names its secret (``WP``);
the variable actually read is ``EDITOR_SECRET_<TENANT>__WP``, which the
operator sets on the server, so a tenant can never reach the server's own
variables or another tenant's. Every outcome is recorded.

Adapters: WordPress REST API (as draft or published post), a signed webhook
(for automation tools that post to LinkedIn, X, newsletters...), and a
Telegram channel.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from .markdown import to_html


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
        payload = {"title": d.title, "subtitle": d.subtitle, "markdown": d.body_md, "html": to_html(d.body_md),
                   "language": d.language}
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
    def __init__(self, http: httpx.Client | None = None, resolver=None, telegram_api="https://api.telegram.org"):
        self.http = http or pinned_client(resolver)
        self.resolver = resolver
        self.telegram_api = telegram_api

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
        secret = None
        try:
            secret = resolve_secret(db, target) if target.secret_env else None
            ext_id, ext_url = getattr(self, f"_{target.kind}")(target, pub, cms_url, secret)
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
        return {"status": "published", "url": ext_url}

    def _wordpress(self, target, pub, cms_url, secret):
        base = target.config["url"].rstrip("/")
        check_public_url(base, self.resolver)
        r = self.http.post(
            f"{base}/wp-json/wp/v2/posts",
            auth=(target.config["user"], _need(secret, target)),
            json={"title": pub.payload["title"], "content": pub.payload["html"], "excerpt": pub.payload["subtitle"],
                  "status": "publish" if pub.mode == "publish" else "draft"},
        )
        _ensure_no_redirect(r, base, self.resolver)
        r.raise_for_status()
        body = r.json()
        return str(body["id"]), body.get("link")

    def _webhook(self, target, pub, cms_url, secret):
        url = target.config["url"]
        check_public_url(url, self.resolver)
        data = json.dumps({"event": "publication", "mode": pub.mode, "publication_id": pub.id,
                           "url": cms_url, **{k: pub.payload[k] for k in ("title", "subtitle", "markdown", "html", "language")}},
                          ensure_ascii=False).encode()
        sig = hmac.new(_need(secret, target).encode(), data, hashlib.sha256).hexdigest()
        r = self.http.post(url, content=data, headers={"Content-Type": "application/json", "X-Editor-Signature": f"sha256={sig}"})
        _ensure_no_redirect(r, url, self.resolver)
        r.raise_for_status()
        return None, None

    def _telegram_channel(self, target, pub, cms_url, secret):
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
