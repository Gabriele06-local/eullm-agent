"""Small data access helpers. Every function takes a session opened with
Database.tenant(), so row level security already scopes the rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import models as m


def ensure_tenant(s: Session, tenant_id: str, name: str) -> None:
    s.execute(insert(m.tenants).values(tenant_id=tenant_id, name=name).on_conflict_do_nothing())


def ensure_site(s: Session, tenant_id: str, domain: str, name: str, language: str = "it") -> int:
    from .quotas import check

    if s.execute(select(m.sites.c.id).where(m.sites.c.domain == domain)).scalar() is None:
        check(s, "sites")
    stmt = (
        insert(m.sites)
        .values(tenant_id=tenant_id, domain=domain, name=name, language=language)
        .on_conflict_do_update(index_elements=["tenant_id", "domain"], set_={"name": name, "language": language})
        .returning(m.sites.c.id)
    )
    return s.execute(stmt).scalar_one()


def upsert_source(
    s: Session,
    tenant_id: str,
    *,
    kind: str,
    name: str,
    url: str,
    config: dict | None = None,
    weight: float = 1.0,
    site_id: int | None = None,
    status: str = "active",
    origin: str = "manual",
    evidence: dict | None = None,
) -> int:
    """Insert a source, or update name and weight of the same (site, kind,
    url, config). Status and evaluation of an existing source are kept."""
    config = config or {}
    src = m.sources
    existing = s.execute(
        select(src.c.id).where(
            src.c.kind == kind,
            src.c.url == url,
            src.c.config == config,
            src.c.site_id.is_(None) if site_id is None else src.c.site_id == site_id,
        )
    ).scalar()
    if existing is not None:
        s.execute(update(src).where(src.c.id == existing).values(name=name, weight=weight))
        return existing
    return s.execute(
        insert(src)
        .values(
            tenant_id=tenant_id, kind=kind, name=name, url=url, config=config, weight=weight,
            site_id=site_id, status=status, origin=origin, evidence=evidence or {},
        )
        .returning(src.c.id)
    ).scalar_one()


def active_sources(s: Session) -> list:
    return list(s.execute(select(m.sources).where(m.sources.c.status == "active").order_by(m.sources.c.id)))


def set_source_status(s: Session, source_id: int, status: str, reason: str | None = None, by: str | None = None) -> None:
    """``by`` names the person who decided; automatic changes leave it empty."""
    stmt = update(m.sources).where(m.sources.c.id == source_id)
    if by is None:  # an automatic change never rewrites a status that is already right
        stmt = stmt.where(m.sources.c.status != status)
    s.execute(stmt.values(status=status, status_reason=reason, status_changed_at=datetime.now(UTC), status_set_by=by))


def mark_source(
    s: Session, source_id: int, *, etag=None, last_modified=None, error: str | None = None, newest=None
) -> None:
    src = m.sources
    values = {"last_fetched_at": datetime.now(UTC), "last_error": error}
    if error is None:
        values |= {"etag": etag, "last_modified": last_modified, "consecutive_errors": 0}
        if newest is not None:
            new = literal(newest, src.c.last_item_at.type)
            values["last_item_at"] = case((or_(src.c.last_item_at.is_(None), src.c.last_item_at < new), new),
                                          else_=src.c.last_item_at)
    else:
        values["consecutive_errors"] = src.c.consecutive_errors + 1
    s.execute(update(src).where(src.c.id == source_id).values(**values))


def recent_signatures(s: Session, days: int = 14, limit: int = 5000) -> list[tuple[int, list[int]]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = s.execute(
        select(m.source_items.c.id, m.source_items.c.minhash)
        .where(m.source_items.c.fetched_at >= since, m.source_items.c.duplicate_of.is_(None))
        .order_by(m.source_items.c.id.desc())
        .limit(limit)
    )
    return [(r.id, list(r.minhash)) for r in rows]


def items_by_ids(s: Session, ids: list[int]) -> list:
    if not ids:
        return []
    return list(s.execute(select(m.source_items).where(m.source_items.c.id.in_(ids))))


def record_llm_usage(s: Session, tenant_id: str, purpose: str, model: str, usage: dict | None, cost) -> None:
    usage = usage or {}
    s.execute(
        insert(m.llm_usage).values(
            tenant_id=tenant_id, purpose=purpose, model=model,
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cost=cost,
        )
    )
