"""Feedback from readers (Francesco 2026-11-11, "in prova" for every newsroom).

Under each article of a site a small form (static/feedback.js) lets a reader
report a mistake or write to the newsroom. Nothing is public and nothing goes
to the model: the message waits until the reader confirms it from the link
sent to their email, then it appears in the panel under Feedback.

What keeps it from being a way in:
- only a site of a newsroom that switched it on, and only from that site:
  the browser's Origin must be the site's domain (or one of its subdomains);
- limits per address and per email, one message waiting per email and site;
- Cloudflare Turnstile when EDITOR_TURNSTILE_SITEKEY and _SECRET are set;
- plain text with a length cap, shown escaped; links are not followed;
- unconfirmed messages are deleted after 48 hours, the others after a year;
- the confirmation is a button, not the link itself: mail scanners that open
  every link do not confirm in place of the reader.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import Form, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, func, select, text, update

from . import i18n, models as m
from .ratelimit import Limiter

log = logging.getLogger(__name__)

KINDS = ("error", "comment", "other")
MESSAGE_MIN, MESSAGE_MAX = 10, 2000
PENDING_HOURS = 48
KEEP_DAYS = 365
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
EMAIL = re.compile(r"^[^@\s<>\"']{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,63}$")
# Ten messages an hour from one address, three from one email: a person
# writing to the newsroom never gets near; a script is stopped.
limiter = Limiter(window=3600, per_email=3, per_address=10)


def turnstile_keys() -> tuple[str, str]:
    return os.environ.get("EDITOR_TURNSTILE_SITEKEY", "").strip(), os.environ.get("EDITOR_TURNSTILE_SECRET", "").strip()


def host_of(origin: str | None) -> str:
    if not origin:
        return ""
    parts = urlsplit(origin)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return ""
    return parts.hostname.lower().removeprefix("www.")


def site_for_origin(db, origin: str | None):
    """The site (tenant_id, id, domain, name, language) that takes feedback
    from this Origin, or None. Read across newsrooms: on Postgres through
    editor.feedback_sites(), since row level security is forced even on the
    owner (migration 0047)."""
    host = host_of(origin)
    if not host:
        return None
    with db.engine.connect() as conn:
        if db.sqlite:
            rows = conn.execute(select(m.sites.c.tenant_id, m.sites.c.id, m.sites.c.domain, m.sites.c.name,
                                       m.sites.c.language).where(m.sites.c.feedback.is_(True))
                                .order_by(m.sites.c.id)).all()
        else:
            rows = conn.execute(text("SELECT tenant_id, id, domain, name, language FROM editor.feedback_sites()")).all()
            conn.rollback()
    for r in rows:
        d = (r.domain or "").lower().removeprefix("www.")
        if d and (host == d or host.endswith("." + d)):
            return r
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def address_hash(address: str | None) -> str | None:
    """The reader's address, not kept: a keyed hash to see repeated senders."""
    if not address:
        return None
    key = os.environ.get("EDITOR_SECRET_KEY", "redazia-readers").encode()
    return hmac.new(key, address.encode(), hashlib.sha256).hexdigest()[:32]


def clean(value: str | None, limit: int) -> str:
    """Plain text: no control characters, at most `limit` characters."""
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value or "")
    return value.strip()[:limit]


def page_url(url: str | None, host: str) -> str:
    """The article's address, only when it is on the same site."""
    url = clean(url, 500)
    if url and host_of(url) and (host_of(url) == host or host_of(url).endswith("." + host)):
        return url
    return ""


def verify_turnstile(token: str | None, address: str | None, client=None) -> bool:
    sitekey, secret = turnstile_keys()
    if not (sitekey and secret):
        return True
    if not token:
        return False
    try:
        c = client or httpx.Client(timeout=10)
        r = c.post(TURNSTILE_URL, data={"secret": secret, "response": token, **({"remoteip": address} if address else {})})
        return bool(r.json().get("success"))
    except Exception as e:  # Cloudflare unreachable: the message is refused, the reader can retry.
        log.warning("turnstile check failed: %s", e)
        return False


class Refused(Exception):
    """A message not taken, with the reason for the reader."""


def submit(db, site, data: dict, address: str | None, now: datetime | None = None) -> str:
    """Store the message as waiting for confirmation; returns the token for
    the email link. Raises Refused."""
    now = now or datetime.now(UTC)
    email = clean(data.get("email"), 254).lower()
    message = clean(data.get("message"), MESSAGE_MAX + 1)
    kind = data.get("kind") if data.get("kind") in KINDS else "other"
    if not EMAIL.match(email):
        raise Refused("email")
    if len(message) < MESSAGE_MIN or len(message) > MESSAGE_MAX:
        raise Refused("length")
    host = (site.domain or "").lower().removeprefix("www.")
    r = m.reader_messages
    token = secrets.token_urlsafe(32)
    with db.tenant(site.tenant_id) as s:
        waiting = s.execute(select(r.c.id).where(r.c.site_id == site.id, r.c.email == email, r.c.status == "pending",
                                                 r.c.created_at > now - timedelta(hours=PENDING_HOURS))).first()
        if waiting is not None:
            raise Refused("pending")
        s.execute(r.insert().values(
            tenant_id=site.tenant_id, site_id=site.id, url=page_url(data.get("url"), host),
            title=clean(data.get("title"), 300), kind=kind, message=message, email=email,
            language=clean(data.get("lang"), 5) or site.language, status="pending",
            token_sha256=token_hash(token), address_hash=address_hash(address), created_at=now))
    return token


def pending(db, token: str, now: datetime | None = None):
    """The message of this token (any newsroom) if sent in the last 48
    hours, or None (on Postgres through editor.reader_message_by_token())."""
    now = now or datetime.now(UTC)
    r = m.reader_messages
    with db.engine.connect() as conn:
        if db.sqlite:
            row = conn.execute(select(r.c.id, r.c.tenant_id, r.c.status, r.c.message, r.c.created_at, m.sites.c.name,
                                      m.sites.c.domain)
                               .join(m.sites, m.sites.c.id == r.c.site_id)
                               .where(r.c.token_sha256 == token_hash(token))).first()
        else:
            row = conn.execute(text("SELECT id, tenant_id, status, message, created_at, name, domain "
                                    "FROM editor.reader_message_by_token(:d)"), {"d": token_hash(token)}).first()
            conn.rollback()
    if row is None:
        return None
    created = row.created_at if isinstance(row.created_at, datetime) else datetime.fromisoformat(str(row.created_at))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return row if created > now - timedelta(hours=PENDING_HOURS) else None


def confirm(db, token: str, now: datetime | None = None) -> str:
    """"confirmed", "already" or "unknown"."""
    now = now or datetime.now(UTC)
    row = pending(db, token, now)
    if row is None:
        return "unknown"
    if row.status != "pending":
        return "already"
    r = m.reader_messages
    with db.tenant(row.tenant_id) as s:
        s.execute(update(r).where(r.c.id == row.id, r.c.status == "pending")
                  .values(status="new", confirmed_at=now))
    return "confirmed"


def mail_text(site, link: str, lang: str | None) -> tuple[str, str]:
    lang = lang if lang in i18n.LANGS else None
    name = site.name or site.domain

    def t(text: str, **values) -> str:
        return i18n.t(text, lang, **values)

    subject = t("Conferma il tuo messaggio a {site}", site=name)
    body = "\n\n".join([
        t("Hai scritto alla redazione di {site}. Per farlo arrivare, conferma che l'indirizzo è tuo.", site=name),
        f"{t('Conferma il messaggio')}: {link}",
        t("Il link vale 48 ore. Il messaggio arriva solo alla redazione e non viene pubblicato. "
          "Se non sei stato tu, ignora questa email: senza conferma il messaggio viene cancellato."),
    ])
    return subject, body


def unread(s, site_id: int | None = None) -> int:
    r = m.reader_messages
    q = select(func.count()).select_from(r).where(r.c.status == "new")
    if site_id is not None:
        q = q.where(r.c.site_id == site_id)
    return s.execute(q).scalar() or 0


def prune(s, now: datetime | None = None) -> int:
    """Unconfirmed after 48 hours and anything after a year go."""
    now = now or datetime.now(UTC)
    r = m.reader_messages
    return s.execute(delete(r).where(
        ((r.c.status == "pending") & (r.c.created_at < now - timedelta(hours=PENDING_HOURS)))
        | (r.c.created_at < now - timedelta(days=KEEP_DAYS)))).rowcount


# --- the web side -------------------------------------------------------------------


def register(app, db, admin, pages, mail, public_url: str) -> None:
    def cors(resp, origin: str | None):
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Max-Age"] = "86400"
        resp.headers["Vary"] = "Origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def answer(origin, status: int, **body):
        return cors(JSONResponse(body, status_code=status), origin)

    @app.options("/api/public/feedback", include_in_schema=False)
    def feedback_preflight(request: Request):
        origin = request.headers.get("origin")
        site = site_for_origin(db, origin)
        return cors(Response(status_code=204), origin if site else None)

    @app.get("/api/public/feedback", include_in_schema=False)
    def feedback_config(request: Request):
        origin = request.headers.get("origin")
        site = site_for_origin(db, origin)
        if site is None or mail is None:
            return answer(origin if site else None, 200, enabled=False)
        return answer(origin, 200, enabled=True, sitekey=turnstile_keys()[0] or None,
                      language=site.language)

    @app.post("/api/public/feedback", include_in_schema=False)
    async def feedback_post(request: Request):
        origin = request.headers.get("origin")
        site = site_for_origin(db, origin)
        if site is None:
            # Not a site that takes feedback, or not from it: no CORS header, the browser drops it.
            return cors(JSONResponse({"error": "closed"}, status_code=403), None)
        if mail is None:
            return answer(origin, 503, error="closed")
        raw = await request.body()
        if len(raw) > 8192:
            return answer(origin, 413, error="length")
        try:
            data = json.loads(raw or b"{}")
            assert isinstance(data, dict)
        except Exception:
            return answer(origin, 400, error="form")
        if data.get("website"):  # the field people do not see: only a script fills it
            return answer(origin, 202, ok=True)
        address = request.client.host if request.client else None
        email = clean(data.get("email"), 254).lower()
        if limiter.blocked(address, email):
            return answer(origin, 429, error="limit")
        limiter.failed(address, email)  # every message counts, not only wrong ones
        if not verify_turnstile(data.get("turnstile"), address):
            return answer(origin, 403, error="captcha")
        try:
            token = submit(db, site, data, address)
        except Refused as e:
            return answer(origin, 400 if str(e) != "pending" else 409, error=str(e))
        link = f"{(public_url or str(request.base_url)).rstrip('/')}/feedback/confirm?t={token}"
        subject, body = mail_text(site, link, data.get("lang"))
        try:
            mail(email, subject, body)
        except Exception:
            r = m.reader_messages
            with db.tenant(site.tenant_id) as s:
                s.execute(delete(r).where(r.c.token_sha256 == token_hash(token)))
            return answer(origin, 503, error="mail")
        return answer(origin, 202, ok=True)

    def confirm_page(request, state: str, row=None, token: str = ""):
        return pages.TemplateResponse(request, "reader_confirm.html.j2", {
            "state": state, "token": token, "site": (row.name or row.domain) if row else "",
            "back": row.domain if row else "", "message": row.message if row else ""},
            headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    @app.get("/feedback/confirm", include_in_schema=False)
    def feedback_confirm_page(request: Request, t: str = ""):
        row = pending(db, t) if t else None
        if row is None:
            return confirm_page(request, "unknown")
        return confirm_page(request, "ask" if row.status == "pending" else "already", row, t)

    @app.post("/feedback/confirm", include_in_schema=False)
    def feedback_confirm(request: Request, t: str = Form("")):
        row = pending(db, t) if t else None
        state = confirm(db, t) if row is not None else "unknown"
        return confirm_page(request, state, row)
