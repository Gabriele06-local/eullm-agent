"""Newsrooms as the operator sees them: create one (from the console, or
from the billing system once a payment goes through), change its plan and
limits, suspend it, list them all with their usage, delete one.

Plans, limits and the tenant row change only through the owner connection
(`admin`); usage is read inside each tenant's own scope (`db`), as the
newsroom's own pages read it.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from . import accounts, models as m, quotas, repo, tenant_data
from .publishing import TENANT_ID

LIMITS = ("max_sites", "max_active_sources", "max_items_per_day", "max_llm_cost_month", "max_drafts_month",
          "max_languages")
SETTINGS = ("plan", "timezone", "briefing_hour", "language")


class NewsroomError(ValueError):
    pass


def tenant_id_from(name: str) -> str:
    """A tenant id from a newsroom name: "Redazione Città" -> redazione-citta."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:40].strip("-")


def _free_id(conn, base: str) -> str:
    base = base or "redazione"
    taken = set(conn.execute(select(m.tenants.c.tenant_id).where(
        (m.tenants.c.tenant_id == base) | m.tenants.c.tenant_id.like(f"{base}-%"))).scalars())
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _plan_fields(spec: dict) -> dict:
    out = {}
    for k in SETTINGS:
        if spec.get(k) is not None:
            out[k] = spec[k]
    limits = spec.get("limits") or {}
    for k in LIMITS:
        if k in limits:
            v = limits[k]
            if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0):
                raise NewsroomError(f"{k} must be a number of zero or more, or null for no limit")
            out[k] = v
    if "briefing_hour" in out and not (isinstance(out["briefing_hour"], int) and 0 <= out["briefing_hour"] <= 23):
        raise NewsroomError("briefing_hour is an hour from 0 to 23")
    return out


def _core_limits(core, tenant_id: str, fields: dict) -> str | None:
    """The plan's model budget becomes the newsroom's limit in the Core too.
    Returns a warning when the Core could not be told."""
    if core is None or not getattr(core, "delegate", False) or "max_llm_cost_month" not in fields:
        return None
    cost = fields["max_llm_cost_month"]
    try:
        core.set_limits(tenant_id, {} if cost is None else {"max_cost_per_month": cost})
    except Exception as e:  # the newsroom exists: say what is left to do
        return f"Core limits not updated: {e}"
    return None


def create(admin, spec: dict, by: str, core=None) -> dict:
    """spec: name, owner_email, and optionally tenant_id, owner_name,
    external_ref, plan, timezone, briefing_hour, language, limits{...}.
    With an external_ref already known, the existing newsroom is returned
    (created False): a payment notice delivered twice makes one newsroom."""
    name = (spec.get("name") or "").strip()[:100]
    if not name:
        raise NewsroomError("the newsroom needs a name")
    owner = accounts.normalise_email(spec.get("owner_email") or "")
    ref = (spec.get("external_ref") or "").strip()[:200] or None
    fields = _plan_fields(spec)
    with admin.engine.begin() as conn:
        if ref is not None:
            old = conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.external_ref == ref)).scalar()
            if old is not None:
                return {"tenant_id": old, "created": False, "owner_email": owner, "invite": None, "warning": None}
        wanted = spec.get("tenant_id")
        if wanted:
            if not TENANT_ID.match(wanted) or len(wanted) > 40:
                raise NewsroomError("tenant ids are lowercase letters and digits, words joined by single dashes")
            if conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.tenant_id == wanted)).first():
                raise NewsroomError(f"the newsroom {wanted} exists already")
            tid = wanted
        else:
            tid = _free_id(conn, tenant_id_from(name))
        try:
            conn.execute(insert(m.tenants).values(tenant_id=tid, name=name, external_ref=ref, **fields))
        except IntegrityError as e:
            if "one tenant" in str(e):
                raise NewsroomError("this installation (SQLite) has one newsroom: several need the server installation") from e
            raise
        uid = accounts.ensure_user(conn, owner, spec.get("owner_name") or "")
        accounts.set_member(conn, uid, tid, "owner")
        has_password = bool(conn.execute(select(m.users.c.password_hash).where(m.users.c.id == uid)).scalar())
    token = None if has_password else accounts.create_invite(admin, owner, by, tenant_id=tid, role="owner")
    return {"tenant_id": tid, "created": True, "owner_email": owner, "invite": token,
            "warning": _core_limits(core, tid, fields)}


def update_newsroom(admin, tenant_id: str, spec: dict, core=None) -> dict:
    """Change name, plan, limits, settings or active (false suspends: its
    people and scheduled work stop at once, nothing is deleted)."""
    fields = _plan_fields(spec)
    if spec.get("name") is not None:
        if not str(spec["name"]).strip():
            raise NewsroomError("the newsroom needs a name")
        fields["name"] = str(spec["name"]).strip()[:100]
    if spec.get("active") is not None:
        fields["active"] = bool(spec["active"])
    if not fields:
        raise NewsroomError("nothing to change")
    with admin.engine.begin() as conn:
        n = conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(**fields)).rowcount
    if not n:
        raise LookupError(tenant_id)
    return {"tenant_id": tenant_id, "warning": _core_limits(core, tenant_id, fields)}


def _describe(row, used: dict, members: int) -> dict:
    t = dict(row._mapping)
    t.pop("core_token_env", None)
    return {**t, "members": members, "usage": {
        what: {"used": used.get(used_col), "limit": t.get(limit_col)}
        for what, (limit_col, used_col) in quotas.LIMITS.items()}}


def listing(admin, db, tenant_id: str | None = None, external_ref: str | None = None) -> list[dict]:
    """Every newsroom (or one) with its plan, usage and number of people."""
    q = select(m.tenants).where(m.tenants.c.tenant_id != repo.ARCHIVE).order_by(m.tenants.c.name)
    if tenant_id is not None:
        q = q.where(m.tenants.c.tenant_id == tenant_id)
    if external_ref is not None:
        q = q.where(m.tenants.c.external_ref == external_ref)
    ms = m.memberships
    with accounts._read(admin) as conn:
        rows = conn.execute(q).all()
        counts = dict(conn.execute(select(ms.c.tenant_id, func.count()).group_by(ms.c.tenant_id)).all())
    out = []
    for r in rows:
        with db.tenant(r.tenant_id) as s:
            used = quotas._used(s, r)
        out.append(_describe(r, used, counts.get(r.tenant_id, 0)))
    return out


def delete(admin, tenant_id: str) -> dict[str, int]:
    return tenant_data.delete_tenant(admin, tenant_id)
