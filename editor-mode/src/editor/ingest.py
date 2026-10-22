"""Collection run: fetch every active source of a tenant, store new items,
mark near-duplicates and record an observation of their metrics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from numbers import Number

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import repo
from .collectors import COLLECTORS, Item, SourceSpec
from .core_fetch import CoreTrouble
from .db import Database
from .dedup import find_duplicate, minhash
from .i18n import N_
from .normalize import canonical_url, content_hash, sha256_hex
from .progress import step

log = logging.getLogger(__name__)


@dataclass
class SourceReport:
    source_id: int
    name: str
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    over_quota: int = 0
    not_modified: bool = False
    error: str | None = None


@dataclass
class CollectReport:
    tenant_id: str
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(r.new for r in self.sources)


def numeric_metrics(metrics: dict) -> dict:
    return {k: v for k, v in metrics.items() if isinstance(v, Number) and not isinstance(v, bool)}


def store_items(s, tenant_id: str, source_id: int, items: list[Item], report: SourceReport) -> None:
    from .quotas import remaining

    candidates = repo.recent_signatures(s)
    room = remaining(s, "items")
    for it in items:
        canon = canonical_url(it.url)
        uhash = sha256_hex(canon)
        chash = content_hash(it.title, it.summary)
        sig = minhash(f"{it.title} {it.summary}")
        existing = s.execute(
            select(m.source_items.c.id).where(m.source_items.c.url_hash == uhash)
        ).scalar()
        if existing is None and room is not None and room <= 0:
            report.over_quota += 1
            continue
        if existing is None:
            if room is not None:
                room -= 1
            same = s.execute(
                select(m.source_items.c.id)
                .where(m.source_items.c.content_hash == chash, m.source_items.c.duplicate_of.is_(None))
                .limit(1)
            ).scalar()
            dup = same or find_duplicate(sig, candidates)
            item_id = s.execute(
                insert(m.source_items)
                .values(
                    tenant_id=tenant_id, source_id=source_id, external_id=it.external_id,
                    url=it.url, canonical_url=canon, url_hash=uhash, content_hash=chash,
                    title=it.title, summary=it.summary, author=it.author,
                    published_at=it.published_at, metrics=it.metrics, minhash=sig, duplicate_of=dup,
                )
                .returning(m.source_items.c.id)
            ).scalar_one()
            report.new += 1
            if dup:
                report.duplicates += 1
            else:
                candidates.append((item_id, sig))
        else:
            item_id = existing
            s.execute(
                m.source_items.update().where(m.source_items.c.id == item_id).values(metrics=it.metrics)
            )
        nums = numeric_metrics(it.metrics)
        if nums:
            s.execute(insert(m.observations).values(tenant_id=tenant_id, item_id=item_id, metrics=nums))


def collect_tenant(db: Database, tenant_id: str, client: httpx.Client) -> CollectReport:
    """Read the archive sources this newsroom follows."""
    with db.tenant(tenant_id) as s:
        repo.rescue_sources(s)
        only = repo.followed_archive(s)
    return collect_archive(db, repo.archive_of(db, tenant_id), client, only)


def collect_archive(db: Database, archive: str, client: httpx.Client, only: set[int] | None = None) -> CollectReport:
    """Read the active archive sources (those in `only`, when given) once
    for every newsroom that follows them."""
    tenant_id = archive
    report = CollectReport(tenant_id)
    with db.tenant(tenant_id) as s:
        repo.rescue_sources(s)
        sources = repo.active_sources(s, only)
    for n, src in enumerate(sources, 1):
        step(N_("Lettura delle fonti: {n} di {total} ({name})"), n=n, total=len(sources), name=src.name)
        r = SourceReport(src.id, src.name)
        report.sources.append(r)
        collector = COLLECTORS.get(src.kind)
        spec = SourceSpec(src.kind, src.url, src.config or {}, src.etag, src.last_modified)
        try:
            result = collector.fetch(client, spec)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            r.error = f"{type(e).__name__}: {e}"[:500]
            log.warning("source %s failed: %s", src.name, r.error)
            with db.tenant(tenant_id) as s:
                repo.mark_source(s, src.id, error=r.error, counted=not isinstance(e, CoreTrouble))
            continue
        r.fetched, r.not_modified = len(result.items), result.not_modified
        with db.tenant(tenant_id) as s:
            store_items(s, tenant_id, src.id, result.items, r)
            dates = [i.published_at for i in result.items if i.published_at]
            repo.mark_source(
                s, src.id, etag=result.etag, last_modified=result.last_modified, newest=max(dates, default=None)
            )
    return report
