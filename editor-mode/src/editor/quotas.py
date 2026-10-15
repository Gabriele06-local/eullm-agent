"""Per-tenant limits. The plan lives on the tenant row (set by the operator
with `editor tenant-plan`); usage comes from the editor.tenant_usage view.
A missing limit means unlimited."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text

from . import models as m


class QuotaExceeded(RuntimeError):
    pass


LIMITS = {
    "sites": ("max_sites", "sites"),
    "sources": ("max_active_sources", "active_sources"),
    "items": ("max_items_per_day", "items_today"),
    "llm_cost": ("max_llm_cost_month", "llm_cost_month"),
    "drafts": ("max_drafts_month", "drafts_month"),
}


@dataclass
class Usage:
    plan: dict
    used: dict

    def remaining(self, what: str) -> float | None:
        limit_col, used_col = LIMITS[what]
        limit = self.plan.get(limit_col)
        return None if limit is None else max(0, limit - (self.used.get(used_col) or 0))


def lock(s, key: str) -> None:
    """Serialise, until this transaction ends, everything that takes the same key."""
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def usage(s, hold: bool = False) -> Usage | None:
    """The tenant's plan and usage. With ``hold``, other quota checks of the
    tenant wait until this transaction ends, so a check and the insert that
    follows it are not interleaved with another worker's (read-then-act)."""
    t = s.execute(select(m.tenants)).first()
    if t is None:
        return None
    if hold:
        lock(s, f"editor-quota:{t.tenant_id}")
    u = s.execute(text("SELECT * FROM editor.tenant_usage")).first()
    return Usage(dict(t._mapping), dict(u._mapping) if u else {})


def check(s, what: str, adding: float = 1) -> None:
    """Raise QuotaExceeded when `adding` more would pass the tenant's limit.
    The check holds the tenant's quota lock until the transaction ends: do
    the insert it guards in the same transaction."""
    u = usage(s, hold=True)
    if u is None:
        return
    left = u.remaining(what)
    if left is not None and (left < adding or left <= 0):
        limit_col, used_col = LIMITS[what]
        raise QuotaExceeded(f"{what} limit reached ({u.used.get(used_col)} of {u.plan[limit_col]}, plan {u.plan.get('plan')})")


def remaining(s, what: str) -> float | None:
    u = usage(s, hold=True)
    return None if u is None else u.remaining(what)
