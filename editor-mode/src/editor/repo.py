"""Small data access helpers. Every function takes a session opened with
Database.tenant(), so row level security already scopes the rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import models as m
from .db import is_sqlite

# The reserved tenant holding the shared archive on PostgreSQL. On SQLite the
# only newsroom is its own archive.
ARCHIVE = "_archive"


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


def add_site(s: Session, tenant_id: str, domain: str) -> int:
    """The site as soon as a person adds it, before its analysis: it shows
    in the menus and on the Desk while it is read. The analysis fills in
    its name and language; the plan's number of sites is checked here."""
    sid = s.execute(select(m.sites.c.id).where(m.sites.c.domain == domain)).scalar()
    if sid is not None:
        return sid
    from .quotas import check

    ensure_tenant(s, tenant_id, tenant_id)
    check(s, "sites")
    return s.execute(insert(m.sites).values(tenant_id=tenant_id, domain=domain, name=domain, language="und")
                     .on_conflict_do_nothing(index_elements=["tenant_id", "domain"])
                     .returning(m.sites.c.id)).scalar() or s.execute(
        select(m.sites.c.id).where(m.sites.c.domain == domain)).scalar_one()


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
    """Insert a source a site follows, or update name and weight of the same
    (site, kind, url, config). Status and evaluation of an existing source
    are kept. The source it follows in the shared archive is created when
    missing."""
    config = config or {}
    src = m.sources
    aid = archive_source(s, kind=kind, name=name, url=url, config=config, origin=origin)
    existing = s.execute(
        select(src.c.id).where(
            src.c.kind == kind,
            src.c.url == url,
            src.c.config == config,
            own(),
            src.c.site_id.is_(None) if site_id is None else src.c.site_id == site_id,
        )
    ).scalar()
    if existing is not None:
        s.execute(update(src).where(src.c.id == existing).values(name=name, weight=weight, archive_id=aid))
        return existing
    return s.execute(
        insert(src)
        .values(
            tenant_id=tenant_id, kind=kind, name=name, url=url, config=config, weight=weight,
            site_id=site_id, status=status, origin=origin, evidence=evidence or {}, shared=False, archive_id=aid,
        )
        .returning(src.c.id)
    ).scalar_one()


def archive_of(db, tenant_id: str) -> str:
    """The tenant whose rows are the archive a newsroom reads."""
    return tenant_id if db.sqlite else ARCHIVE


def archive_tenant(s: Session) -> str:
    return s.info["tenant_id"] if is_sqlite(s) else ARCHIVE


def own():
    """The newsroom's own sources, not those of the shared archive."""
    return m.sources.c.shared.is_not(True)


def archive_source(s: Session, *, kind: str, name: str, url: str, config: dict | None = None,
                   origin: str = "manual") -> int:
    """The archive source for (kind, url, config), created when missing."""
    config = config or {}
    src = m.sources
    tenant = archive_tenant(s)

    def find():
        return s.execute(select(src.c.id).where(
            src.c.tenant_id == tenant, src.c.shared.is_(True), src.c.kind == kind, src.c.url == url,
            src.c.config == config)).scalar()

    found = find()
    if found is None:
        s.execute(insert(src).values(
            tenant_id=tenant, kind=kind, name=name, url=url, config=config, status="active", origin=origin,
            evidence={}, shared=True).on_conflict_do_nothing())
        found = find()
    return found


def followed_by(site_id: int | None = None):
    """SQL condition on an item: one of the newsroom's active sources (for
    one site, or any) is its source or follows it in the archive."""
    f = m.sources.alias("f")
    cond = [f.c.status == "active", f.c.shared.is_not(True),
            or_(f.c.id == m.source_items.c.source_id, f.c.archive_id == m.source_items.c.source_id)]
    if site_id is not None:
        cond.append(or_(f.c.site_id == site_id, f.c.site_id.is_(None)))
    return select(f.c.id).where(*cond).exists()


# Failed reads in a row that suspend an archive source: about a day of
# hourly reads, so a site down for a few hours, or the server's own network,
# does not stop a feed for everyone. Before 0.17.1 five were enough.
ERROR_LIMIT = 24

# Errors of the Core, not of the source, as earlier versions recorded them;
# before 0.9.3 the Core took a "304 Not Modified" for a redirect.
CORE_TROUBLE = ("CoreTrouble", "Core unreachable", "Core fetch failed (429)", "limit reached",
                "Redirect without Location")


def rescue_sources(s: Session) -> list[int]:
    """Sources suspended for failed fetches that were the Core's fault (it
    was down, or the newsroom was out of daily fetches) go back to work.
    A status a person chose stays as it is."""
    src = m.sources
    # A newsroom's source no longer reads anything itself (the archive does),
    # so failed reads never keep it suspended.
    back = [r.id for r in s.execute(select(src.c.id, src.c.status_reason, src.c.last_error, src.c.shared).where(
        src.c.status == "suspended", src.c.status_set_by.is_(None))).all()
        if (r.status_reason or "").endswith("failed fetches in a row")
        and (not r.shared or any(k in (r.last_error or "") for k in CORE_TROUBLE)
             or _failures(r.status_reason) < ERROR_LIMIT)]
    if back:
        s.execute(update(src).where(src.c.id.in_(back)).values(
            status="active", status_reason=None, status_changed_at=datetime.now(UTC), consecutive_errors=0))
    return back


def _failures(reason: str) -> int:
    """The number in "5 failed fetches in a row": a source suspended by an
    earlier, stricter rule goes back to work."""
    head = reason.split(" ", 1)[0]
    return int(head) if head.isdigit() else ERROR_LIMIT


def active_sources(s: Session, only: set[int] | None = None) -> list:
    """The archive sources to read: active ones, and with `only` those a
    newsroom follows."""
    q = select(m.sources).where(m.sources.c.status == "active", m.sources.c.shared.is_(True))
    if only is not None:
        q = q.where(m.sources.c.id.in_(only))
    return list(s.execute(q.order_by(m.sources.c.id)))


def followed_archive(s: Session) -> set[int]:
    """The archive sources this newsroom's active sources follow."""
    src = m.sources
    return set(s.execute(select(src.c.archive_id).where(
        src.c.status == "active", own(), src.c.archive_id.is_not(None))).scalars())


def set_source_status(s: Session, source_id: int, status: str, reason: str | None = None, by: str | None = None) -> None:
    """``by`` names the person who decided; automatic changes leave it empty."""
    stmt = update(m.sources).where(m.sources.c.id == source_id)
    if by is None:  # an automatic change never rewrites a status that is already right
        stmt = stmt.where(m.sources.c.status != status)
    s.execute(stmt.values(status=status, status_reason=reason, status_changed_at=datetime.now(UTC), status_set_by=by))


def mark_source(
    s: Session, source_id: int, *, etag=None, last_modified=None, error: str | None = None, newest=None,
    counted: bool = True,
) -> None:
    """Record a fetch. An error that is not the source's (`counted=False`:
    the Core was down or out of fetches) is shown but does not bring the
    source closer to suspension."""
    src = m.sources
    values = {"last_fetched_at": datetime.now(UTC), "last_error": error}
    if error is None:
        values |= {"etag": etag, "last_modified": last_modified, "consecutive_errors": 0}
        if newest is not None:
            new = literal(newest, src.c.last_item_at.type)
            values["last_item_at"] = case((or_(src.c.last_item_at.is_(None), src.c.last_item_at < new), new),
                                          else_=src.c.last_item_at)
    elif counted:
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
