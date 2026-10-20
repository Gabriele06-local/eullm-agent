"""Who is calling: an access token of a newsroom (`edt_`, for the API and
integrations) or a session of a person signed in with email and password
(`eds_`, see accounts.py). Only the SHA-256 of a token is stored."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import text

ROLES = ("viewer", "editor", "owner")


@dataclass(frozen=True)
class Caller:
    tenant_id: str
    name: str
    role: str
    user_id: int | None = None
    operator: bool = False

    def can(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


def digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def new_token() -> str:
    return "edt_" + secrets.token_urlsafe(32)


def create_token(db, tenant_id: str, name: str, role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    token = new_token()
    with db.tenant(tenant_id) as s:
        s.execute(
            text("INSERT INTO editor.access_tokens (tenant_id, name, token_sha256, role) VALUES (:t, :n, :d, :r)"),
            {"t": tenant_id, "n": name, "d": digest(token), "r": role},
        )
    return token


def resolve(db, token: str | None, admin=None) -> Caller | None:
    """`admin` (the owner connection) is needed for sessions of people."""
    if not token:
        return None
    if token.startswith("eds_"):
        from .accounts import resolve_session
        s = resolve_session(admin, token) if admin is not None else None
        if s is None or s.tenant_id is None:
            return None
        return Caller(s.tenant_id, s.email, s.role, s.user_id, s.operator)
    # On PostgreSQL only this function reads tokens across tenants.
    q = ("SELECT a.tenant_id, a.name, a.role FROM editor.access_tokens a "
         "JOIN editor.tenants t ON t.tenant_id = a.tenant_id "
         "WHERE a.token_sha256 = :d AND a.revoked_at IS NULL AND t.active" if db.sqlite
         else "SELECT * FROM editor.resolve_token(:d)")
    with db.engine.connect() as conn:
        row = conn.execute(text(q), {"d": digest(token)}).first()
    return Caller(row.tenant_id, row.name, row.role) if row else None
