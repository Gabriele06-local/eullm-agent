"""People and operators: accounts with email and password, the newsrooms
each person belongs to (with a role), sessions of the dashboard, invites,
password resets, and the API keys of the operator.

Every function takes `admin`, the owner connection: the application role
cannot read these tables, so a page of one newsroom cannot list the
accounts of another. Secrets (passwords, session, invite and API tokens)
are never stored: passwords as scrypt hashes, tokens as SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from sqlalchemy import delete, func, insert, select, update

from . import models as m
from .auth import ROLES, digest

SESSION_DAYS = 30
INVITE_DAYS = 7
RESET_HOURS = 24
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
EMAIL = re.compile(r"^[^@\s,;<>\"']{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class AccountError(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    user_id: int
    email: str
    name: str
    operator: bool
    tenant_id: str | None
    role: str | None


def _read(admin):
    """A connection for reading: on SQLite it does not wait for the write
    lock a page's own transaction may be holding."""
    return admin.engine.connect().execution_options(read_only=True)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def normalise_email(value: str) -> str:
    e = (value or "").strip().lower()
    if len(e) > 254 or not EMAIL.match(e):
        raise AccountError("indirizzo email non valido")
    return e


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"la password deve avere almeno {MIN_PASSWORD} caratteri")
    if len(password) > 1024:
        raise AccountError("password troppo lunga")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b = base64.b64encode
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b(salt).decode()}${b(h).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        kind, n, r, p, salt, h = (stored or "").split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32, n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


# Spent on unknown emails too, so the answer time does not tell which exist.
_DUMMY = hash_password(secrets.token_hex(8))


def random_password() -> str:
    return secrets.token_urlsafe(12)


# --- users ----------------------------------------------------------------------

def find_user(conn, email: str):
    return conn.execute(select(m.users).where(m.users.c.email == email)).first()


def ensure_user(conn, email: str, name: str = "", operator: bool = False) -> int:
    """The id of the account with this email, created (without a password)
    when missing. `operator` only ever adds the operator flag."""
    email = normalise_email(email)
    u = find_user(conn, email)
    if u is None:
        return conn.execute(insert(m.users).values(email=email, name=name.strip()[:100], operator=operator,
                                                   failed_logins=0).returning(m.users.c.id)).scalar_one()
    if operator and not u.operator:
        conn.execute(update(m.users).where(m.users.c.id == u.id).values(operator=True))
    if name.strip() and not u.name:
        conn.execute(update(m.users).where(m.users.c.id == u.id).values(name=name.strip()[:100]))
    return u.id


def create_user(admin, email: str, name: str = "", password: str | None = None, operator: bool = False) -> int:
    if password is not None:
        check_password(password)
    with admin.engine.begin() as conn:
        uid = ensure_user(conn, email, name, operator)
        if password is not None:
            conn.execute(update(m.users).where(m.users.c.id == uid).values(password_hash=hash_password(password)))
    return uid


def authenticate(admin, email: str, password: str, now: datetime | None = None):
    """The account, or None. After MAX_FAILED_LOGINS wrong passwords the
    account is locked for LOCK_MINUTES (AccountError says so)."""
    now = _now(now)
    try:
        email = normalise_email(email)
    except AccountError:
        verify_password(password, _DUMMY)
        return None
    with admin.engine.begin() as conn:
        u = find_user(conn, email)
        if u is None or u.disabled_at is not None or not u.password_hash:
            verify_password(password, _DUMMY)
            return None
        if u.locked_until is not None and u.locked_until > now:
            raise AccountError("troppi tentativi sbagliati: riprova fra qualche minuto")
        if verify_password(password, u.password_hash):
            conn.execute(update(m.users).where(m.users.c.id == u.id).values(failed_logins=0, locked_until=None))
            return u
        failed = (u.failed_logins or 0) + 1
        values = {"failed_logins": failed}
        if failed >= MAX_FAILED_LOGINS:
            values |= {"failed_logins": 0, "locked_until": now + timedelta(minutes=LOCK_MINUTES)}
        conn.execute(update(m.users).where(m.users.c.id == u.id).values(**values))
    return None


def operators(admin) -> list:
    with _read(admin) as conn:
        return conn.execute(select(m.users).where(m.users.c.operator).order_by(m.users.c.email)).all()


# --- memberships ---------------------------------------------------------------

def memberships(admin, user_id: int) -> list:
    """The newsrooms of a person: tenant_id, name, role, active."""
    t, ms = m.tenants, m.memberships
    with _read(admin) as conn:
        return conn.execute(select(ms.c.tenant_id, t.c.name, ms.c.role, t.c.active)
                            .join(t, t.c.tenant_id == ms.c.tenant_id)
                            .where(ms.c.user_id == user_id).order_by(t.c.name)).all()


def members(admin, tenant_id: str) -> list:
    u, ms = m.users, m.memberships
    with _read(admin) as conn:
        return conn.execute(select(u.c.id, u.c.email, u.c.name, ms.c.role, ms.c.created_at,
                                   u.c.password_hash.is_not(None).label("active"))
                            .join(ms, ms.c.user_id == u.c.id)
                            .where(ms.c.tenant_id == tenant_id).order_by(u.c.email)).all()


def set_member(conn, user_id: int, tenant_id: str, role: str) -> None:
    if role not in ROLES:
        raise AccountError("ruolo non valido")
    ms = m.memberships
    row = conn.execute(select(ms.c.role).where(ms.c.user_id == user_id, ms.c.tenant_id == tenant_id)).first()
    if row is None:
        conn.execute(insert(ms).values(user_id=user_id, tenant_id=tenant_id, role=role))
    elif row.role != role:
        if row.role == "owner":
            _keep_an_owner(conn, tenant_id, user_id)
        conn.execute(update(ms).where(ms.c.user_id == user_id, ms.c.tenant_id == tenant_id).values(role=role))


def _keep_an_owner(conn, tenant_id: str, leaving: int) -> None:
    ms = m.memberships
    others = conn.execute(select(func.count()).select_from(ms).where(
        ms.c.tenant_id == tenant_id, ms.c.role == "owner", ms.c.user_id != leaving)).scalar()
    if not others:
        raise AccountError("la redazione deve avere almeno un owner")


def change_role(admin, tenant_id: str, user_id: int, role: str) -> None:
    with admin.engine.begin() as conn:
        if not conn.execute(select(m.memberships.c.role).where(
                m.memberships.c.user_id == user_id, m.memberships.c.tenant_id == tenant_id)).first():
            raise AccountError("persona non trovata in questa redazione")
        set_member(conn, user_id, tenant_id, role)


def remove_member(admin, tenant_id: str, user_id: int) -> None:
    ms = m.memberships
    with admin.engine.begin() as conn:
        row = conn.execute(select(ms.c.role).where(ms.c.user_id == user_id, ms.c.tenant_id == tenant_id)).first()
        if row is None:
            raise AccountError("persona non trovata in questa redazione")
        if row.role == "owner":
            _keep_an_owner(conn, tenant_id, user_id)
        conn.execute(delete(ms).where(ms.c.user_id == user_id, ms.c.tenant_id == tenant_id))
        conn.execute(delete(m.sessions).where(m.sessions.c.user_id == user_id, m.sessions.c.tenant_id == tenant_id))


# --- sessions ---------------------------------------------------------------------

def start_session(admin, user_id: int, tenant_id: str | None, now: datetime | None = None) -> str:
    token = "eds_" + secrets.token_urlsafe(32)
    now = _now(now)
    with admin.engine.begin() as conn:
        conn.execute(delete(m.sessions).where(m.sessions.c.expires_at < now))
        conn.execute(insert(m.sessions).values(token_sha256=digest(token), user_id=user_id, tenant_id=tenant_id,
                                               created_at=now, expires_at=now + timedelta(days=SESSION_DAYS)))
    return token


def end_session(admin, token: str | None) -> None:
    if token and token.startswith("eds_"):
        with admin.engine.begin() as conn:
            conn.execute(delete(m.sessions).where(m.sessions.c.token_sha256 == digest(token)))


def resolve_session(admin, token: str | None, now: datetime | None = None) -> Session | None:
    """Who a session belongs to and in which newsroom, with the role the
    person has there now (a removed member or a deactivated newsroom stops
    at once). An operator enters any newsroom as owner."""
    if not token or not token.startswith("eds_"):
        return None
    s, u = m.sessions, m.users
    with _read(admin) as conn:
        row = conn.execute(select(u.c.id, u.c.email, u.c.name, u.c.operator, s.c.tenant_id)
                           .join(u, u.c.id == s.c.user_id)
                           .where(s.c.token_sha256 == digest(token), s.c.expires_at > _now(now),
                                  u.c.disabled_at.is_(None))).first()
        if row is None:
            return None
        role = None
        if row.tenant_id is not None:
            t = conn.execute(select(m.tenants.c.active).where(m.tenants.c.tenant_id == row.tenant_id)).first()
            ms = m.memberships
            member = conn.execute(select(ms.c.role).where(ms.c.user_id == row.id,
                                                         ms.c.tenant_id == row.tenant_id)).scalar()
            if row.operator and t is not None:
                role = "owner"
            elif t is not None and t.active and member:
                role = member
            else:
                return None
    return Session(row.id, row.email, row.name, bool(row.operator), row.tenant_id, role)


# --- invites and resets --------------------------------------------------------------

def create_invite(admin, email: str, created_by: str, tenant_id: str | None = None, role: str | None = None,
                  operator: bool = False, kind: str = "invite", now: datetime | None = None) -> str:
    """A link token for `email`: an invite to a newsroom (and/or to operate
    the service), or a password reset. Valid INVITE_DAYS (a reset
    RESET_HOURS) and once."""
    email = normalise_email(email)
    if kind == "invite" and tenant_id is None and not operator:
        raise AccountError("un invito è per una redazione o per un operatore")
    if role is not None and role not in ROLES:
        raise AccountError("ruolo non valido")
    now = _now(now)
    ttl = timedelta(hours=RESET_HOURS) if kind == "reset" else timedelta(days=INVITE_DAYS)
    token = "edi_" + secrets.token_urlsafe(32)
    with admin.engine.begin() as conn:
        conn.execute(insert(m.invites).values(token_sha256=digest(token), kind=kind, email=email, tenant_id=tenant_id,
                                              role=role, operator=operator, created_by=created_by[:200],
                                              created_at=now, expires_at=now + ttl))
    return token


def invite(admin, token: str | None, now: datetime | None = None, setup: bool = False):
    """The invite (with `newsroom` and `has_password`), if it can still be
    used. The first-start link is not an invite (see complete_setup)."""
    if not token or not token.startswith("edi_"):
        return None
    i, t = m.invites, m.tenants
    with _read(admin) as conn:
        row = conn.execute(select(i, t.c.name.label("newsroom")).outerjoin(t, t.c.tenant_id == i.c.tenant_id)
                           .where(i.c.token_sha256 == digest(token), i.c.used_at.is_(None),
                                  i.c.expires_at > _now(now))).first()
        if row is None or (row.email == SETUP_EMAIL) != setup:
            return None
        u = find_user(conn, row.email)
    return {**row._mapping, "has_password": bool(u is not None and u.password_hash), "user_name": u.name if u else ""}


def accept(admin, token: str, password: str, name: str = "", now: datetime | None = None) -> Session:
    """Use an invite or a reset. A person who already has a password confirms
    it to join; anyone else chooses one. Returns the session to start."""
    now = _now(now)
    info = invite(admin, token, now)
    if info is None:
        raise AccountError("il link non è valido o è scaduto")
    with admin.engine.begin() as conn:
        u = find_user(conn, info["email"])
        if info["kind"] == "invite" and u is not None and u.password_hash:
            if not verify_password(password, u.password_hash):
                raise AccountError("password sbagliata: usa quella del tuo account")
            uid = u.id
        else:
            check_password(password)
            uid = ensure_user(conn, info["email"], name)
            conn.execute(update(m.users).where(m.users.c.id == uid).values(
                password_hash=hash_password(password), failed_logins=0, locked_until=None))
            if info["kind"] == "reset":
                conn.execute(delete(m.sessions).where(m.sessions.c.user_id == uid))
        if info["operator"]:
            ensure_user(conn, info["email"], operator=True)
        if info["tenant_id"] is not None and info["role"]:
            set_member(conn, uid, info["tenant_id"], info["role"])
        n = conn.execute(update(m.invites).where(m.invites.c.token_sha256 == digest(token), m.invites.c.used_at.is_(None))
                         .values(used_at=now)).rowcount
        if not n:
            raise AccountError("il link è già stato usato")
        u = conn.execute(select(m.users).where(m.users.c.id == uid)).first()
    tenant = info["tenant_id"]
    if tenant is None and not u.operator:
        ms = memberships(admin, uid)
        tenant = next((x.tenant_id for x in ms if x.active), None)
    return Session(uid, u.email, u.name, bool(u.operator), tenant, info["role"])


def pending_invites(admin, tenant_id: str | None, now: datetime | None = None) -> list:
    i = m.invites
    q = select(i).where(i.c.used_at.is_(None), i.c.expires_at > _now(now), i.c.kind == "invite",
                        i.c.email != SETUP_EMAIL)
    q = q.where(i.c.tenant_id == tenant_id) if tenant_id is not None else q.where(i.c.operator)
    with _read(admin) as conn:
        return conn.execute(q.order_by(i.c.created_at.desc())).all()


def revoke_invite(admin, tenant_id: str | None, token_sha256: str) -> bool:
    i = m.invites
    q = delete(i).where(i.c.token_sha256 == token_sha256, i.c.used_at.is_(None))
    q = q.where(i.c.tenant_id == tenant_id) if tenant_id is not None else q
    with admin.engine.begin() as conn:
        return bool(conn.execute(q).rowcount)


# --- first start ---------------------------------------------------------------------

# The first administrator is created from the browser, with a link printed
# when the service starts (editor migrate) and as long as there is none:
# whoever can read the server's logs or run its commands, nobody else.
SETUP_EMAIL = "setup@eullm.invalid"
SETUP_DAYS = 30


def needs_setup(admin) -> bool:
    """No operator can sign in yet."""
    u = m.users
    with _read(admin) as conn:
        return conn.execute(select(u.c.id).where(u.c.operator, u.c.password_hash.is_not(None),
                                                 u.c.disabled_at.is_(None)).limit(1)).first() is None


def setup_token(admin, now: datetime | None = None) -> str | None:
    """A new first-start link token (the older ones stop working), or None
    when an operator exists already."""
    if not needs_setup(admin):
        return None
    now = _now(now)
    token = "edi_" + secrets.token_urlsafe(32)
    i = m.invites
    with admin.engine.begin() as conn:
        conn.execute(delete(i).where(i.c.email == SETUP_EMAIL))
        conn.execute(insert(i).values(token_sha256=digest(token), kind="invite", email=SETUP_EMAIL, operator=True,
                                      created_by="first start", created_at=now,
                                      expires_at=now + timedelta(days=SETUP_DAYS)))
    return token


def setup_valid(admin, token: str | None, now: datetime | None = None) -> bool:
    return invite(admin, token, now, setup=True) is not None and needs_setup(admin)


def complete_setup(admin, token: str, email: str, name: str, password: str, now: datetime | None = None) -> Session:
    """Create the first administrator (operator) with the first-start link."""
    now = _now(now)
    if not setup_valid(admin, token, now):
        raise AccountError("il link non è valido, è scaduto o l'amministratore esiste già")
    email = normalise_email(email)
    check_password(password)
    with admin.engine.begin() as conn:
        n = conn.execute(update(m.invites).where(m.invites.c.token_sha256 == digest(token), m.invites.c.used_at.is_(None))
                         .values(used_at=now)).rowcount
        if not n:
            raise AccountError("il link è già stato usato")
        uid = ensure_user(conn, email, name, operator=True)
        conn.execute(update(m.users).where(m.users.c.id == uid).values(
            password_hash=hash_password(password), failed_logins=0, locked_until=None, disabled_at=None))
        u = conn.execute(select(m.users).where(m.users.c.id == uid)).first()
    return Session(uid, u.email, u.name, True, None, None)


# --- operator API keys ---------------------------------------------------------------

def create_operator_token(admin, name: str) -> str:
    name = name.strip()[:100]
    if not name:
        raise AccountError("serve un nome")
    token = "edo_" + secrets.token_urlsafe(32)
    with admin.engine.begin() as conn:
        conn.execute(insert(m.operator_tokens).values(name=name, token_sha256=digest(token)))
    return token


def resolve_operator_token(admin, token: str | None) -> str | None:
    if not token or not token.startswith("edo_"):
        return None
    o = m.operator_tokens
    with _read(admin) as conn:
        return conn.execute(select(o.c.name).where(o.c.token_sha256 == digest(token), o.c.revoked_at.is_(None))).scalar()


def operator_tokens(admin) -> list:
    o = m.operator_tokens
    with _read(admin) as conn:
        return conn.execute(select(o.c.id, o.c.name, o.c.created_at, o.c.revoked_at)
                            .order_by(o.c.revoked_at.is_not(None), o.c.created_at)).all()


def revoke_operator_token(admin, token_id: int) -> bool:
    o = m.operator_tokens
    with admin.engine.begin() as conn:
        return bool(conn.execute(update(o).where(o.c.id == token_id, o.c.revoked_at.is_(None))
                                 .values(revoked_at=datetime.now(UTC))).rowcount)


# --- mail ----------------------------------------------------------------------------

def smtp_mailer(settings, smtp_factory=smtplib.SMTP):
    """A function (to, subject, text) that sends one email, or None when
    SMTP is not configured: the link is then shown to whoever created it."""
    if not settings.smtp_host:
        return None

    def send(to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, settings.mail_from or settings.smtp_user, to
        msg.set_content(body)
        with smtp_factory(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)

    return send


def link_text(kind: str, url: str, newsroom: str | None, by: str) -> tuple[str, str]:
    """Subject and body of the email that carries a link."""
    if kind == "reset":
        return ("Reimposta la password di EuLLM Editor",
                f"Per scegliere una nuova password apri questo link entro {RESET_HOURS} ore:\n\n{url}\n\n"
                "Se non l'hai chiesto tu, ignora questa email: la password resta quella di prima.\n")
    where = f"la redazione {newsroom}" if newsroom else "la console di EuLLM Editor"
    return (f"Invito a {where}",
            f"{by} ti ha invitato in {where} su EuLLM Editor.\n\n"
            f"Apri questo link entro {INVITE_DAYS} giorni per entrare:\n\n{url}\n")
