"""A tenant's data as a whole: export it, delete it, and keep what is
collected from growing for ever.

- Export: every row of the tenant, table by table, as JSON. Credentials are
  left out (secret names only, token names and roles only): an export can be
  handed to the customer as it is.
- Delete: every row of the tenant, the tenant itself last, in one
  transaction, with the owner connection. What the Core holds for the
  tenant (usage and audit of `editor/<tenant>`) stays there.
- Retention: collected items, their observations and finished jobs older
  than EDITOR_RETENTION_DAYS (default 365) are deleted at the daily
  maintenance. Items a proposal or a draft cites are kept, so every
  sentence keeps its source; model usage is kept for accounting.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select, text, union

from . import models as m

# Children before parents, the tenant last.
DELETE_ORDER = [
    "publications", "publish_targets",
    "draft_claim_sources", "draft_claims", "drafts",
    "proposal_citations", "proposals", "opportunity_scores",
    "feedback", "profile_reviews", "editorial_profiles", "site_analyses", "site_posts",
    "briefings", "recipients",
    "trend_scores", "topic_items", "topics",
    "observations", "source_items", "sources", "sites",
    "llm_usage", "jobs", "access_tokens", "secrets",
    "tenants",
]

# Who belongs to the tenant, their sessions and invites: owner connection
# only (accounts.py), never exported, deleted with the tenant row.
ACCOUNT_TABLES = ("memberships", "sessions", "invites")

# Never exported: what would let someone act as the tenant.
WITHHELD = {"access_tokens": {"token_sha256"}, "secrets": {"ciphertext", "key_id"}}

DEFAULT_RETENTION_DAYS = 365


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def export(db, tenant_id: str) -> dict:
    """Every row of the tenant, read in the tenant's own scope."""
    out = {"tenant": tenant_id, "exported_at": datetime.now(UTC).isoformat(), "tables": {}}
    with db.tenant(tenant_id) as s:
        for name in reversed(DELETE_ORDER):
            table = m.metadata.tables[f"editor.{name}"]
            cols = [c for c in table.c if c.name not in WITHHELD.get(name, ())]
            order = list(table.primary_key.columns)
            rows = s.execute(select(*cols).where(table.c.tenant_id == tenant_id).order_by(*order)).all()
            out["tables"][name] = [{k: _plain(v) for k, v in r._mapping.items()} for r in rows]
    return out


def export_json(db, tenant_id: str) -> str:
    return json.dumps(export(db, tenant_id), ensure_ascii=False, indent=1, default=str)


def delete_tenant(admin_db, tenant_id: str) -> dict[str, int]:
    """Delete every row of the tenant. `admin_db` is the owner connection:
    the application role cannot delete tenants."""
    counts = {}
    with admin_db.engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            # Row level security is forced on the owner too (a server's owner
            # is not a superuser): without the tenant in scope every DELETE
            # would match nothing.
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        if not conn.execute(text("SELECT 1 FROM editor.tenants WHERE tenant_id = :t"), {"t": tenant_id}).first():
            raise LookupError(f"no tenant {tenant_id}")
        for name in DELETE_ORDER:
            counts[name] = conn.execute(text(f"DELETE FROM editor.{name} WHERE tenant_id = :t"), {"t": tenant_id}).rowcount
    return counts


def retention_days() -> int:
    import os

    raw = os.environ.get("EDITOR_RETENTION_DAYS", "").strip()
    return int(raw) if raw else DEFAULT_RETENTION_DAYS


def prune(db, tenant_id: str, now: datetime | None = None, days: int | None = None) -> dict[str, int]:
    """Delete what the tenant collected more than `days` ago and is not cited."""
    days = retention_days() if days is None else days
    if days <= 0:
        return {}
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    i = m.source_items
    cited = union(select(m.proposal_citations.c.item_id), select(m.draft_claim_sources.c.item_id),
                  select(i.c.duplicate_of).where(i.c.duplicate_of.is_not(None)))
    with db.tenant(tenant_id) as s:
        old = select(i.c.id).where(i.c.fetched_at < cutoff, i.c.id.not_in(cited))
        observations = s.execute(delete(m.observations).where(
            (m.observations.c.observed_at < cutoff) | m.observations.c.item_id.in_(old))).rowcount
        s.execute(delete(m.topic_items).where(m.topic_items.c.item_id.in_(old)))
        items = s.execute(delete(i).where(i.c.id.in_(old))).rowcount
        jobs = s.execute(delete(m.jobs).where(m.jobs.c.status.in_(("done", "failed")),
                                              m.jobs.c.finished_at < cutoff)).rowcount
    return {"items": items, "observations": observations, "jobs": jobs}
