"""Topic detection: group recent items that talk about the same thing, then
give each group a label (through the Core when available)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import repo
from .core_client import CoreClient, CoreError
from .db import Database
from .text import item_terms, top_terms

log = logging.getLogger(__name__)

MATCH = 0.5  # share of an item's terms found in a topic
MIN_SHARED = 2  # and at least this many shared terms


@dataclass
class Cluster:
    terms: set[str]
    item_ids: list[int] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    topic_id: int | None = None


def _match(terms: set[str], cluster_terms: set[str]) -> float:
    if not terms:
        return 0.0
    shared = len(terms & cluster_terms)
    return shared / len(terms) if shared >= MIN_SHARED else 0.0


def assign(items: list[tuple[int, str, str]], clusters: list[Cluster]) -> list[Cluster]:
    """Greedy single pass: each item joins the best matching cluster or opens
    a new one. `items` are (id, title, summary); clusters may be preloaded."""
    for item_id, title, summary in items:
        terms = item_terms(title, summary)
        best, best_score = None, MATCH
        for c in clusters:
            score = _match(terms, c.terms)
            if score >= best_score:
                best, best_score = c, score
        if best is None:
            best = Cluster(set())
            clusters.append(best)
        best.item_ids.append(item_id)
        best.titles.append(title)
        best.terms |= terms
    return clusters


def validate_label(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    label = value.get("label")
    if not isinstance(label, str) or not 3 <= len(label.strip()) <= 90:
        raise ValueError("label must be a string of 3 to 90 characters")
    summary = value.get("summary", "")
    if not isinstance(summary, str) or len(summary) > 600:
        raise ValueError("summary must be a string of at most 600 characters")
    keywords = value.get("keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        raise ValueError("keywords must be a list of strings")
    return {"label": label.strip(), "summary": summary.strip(), "keywords": [k.strip().lower() for k in keywords][:10]}


LABEL_SYSTEM = (
    "You name news topics for an editorial team. Given headlines that belong to one topic, "
    'reply with JSON only: {"label": "<short topic name, max 80 chars, in the headlines\' language>", '
    '"summary": "<one or two neutral sentences>", "keywords": ["<up to 8 lowercase keywords>"]}. '
    "Use only what the headlines say."
)


def label_topic(core: CoreClient, titles: list[str]) -> tuple[dict, list]:
    user = "Headlines:\n" + "\n".join(f"- {t}" for t in titles[:20])
    return core.chat_json(LABEL_SYSTEM, user, validate_label)


def keyword_label(titles: list[str]) -> tuple[str, list[str]]:
    terms = top_terms(titles)
    return (" ".join(terms[:4]) or titles[0][:80]), terms


@dataclass
class TopicReport:
    assigned: int = 0
    new_topics: int = 0
    labelled: int = 0
    label_errors: int = 0


def detect_topics(
    db: Database,
    tenant_id: str,
    core: CoreClient | None = None,
    window_hours: int = 72,
    topic_days: int = 7,
) -> TopicReport:
    report = TopicReport()
    now = datetime.now(UTC)
    # Topics are found once, in the archive the newsroom reads.
    tenant_id = repo.archive_of(db, tenant_id)
    with db.tenant(tenant_id) as s:
        si, ti = m.source_items, m.topic_items
        items = s.execute(
            select(si.c.id, si.c.title, si.c.summary)
            .where(
                si.c.fetched_at >= now - timedelta(hours=window_hours),
                si.c.duplicate_of.is_(None),
                ~si.c.id.in_(select(ti.c.item_id)),
            )
            .order_by(si.c.id)
        ).all()
        if not items:
            return report
        clusters = []
        for t in s.execute(
            select(m.topics.c.id, m.topics.c.keywords).where(m.topics.c.updated_at >= now - timedelta(days=topic_days))
        ):
            clusters.append(Cluster(set(t.keywords or []), topic_id=t.id))
        preloaded = {id(c) for c in clusters}
        assign([(r.id, r.title, r.summary or "") for r in items], clusters)

        for c in clusters:
            if not c.item_ids:
                continue
            if id(c) not in preloaded:
                label, terms = keyword_label(c.titles)
                c.topic_id = s.execute(
                    insert(m.topics)
                    .values(tenant_id=tenant_id, label=label, keywords=terms)
                    .returning(m.topics.c.id)
                ).scalar_one()
                report.new_topics += 1
            s.execute(
                insert(ti).values([{"tenant_id": tenant_id, "topic_id": c.topic_id, "item_id": i} for i in c.item_ids])
                .on_conflict_do_nothing()
            )
            all_titles = s.execute(
                select(si.c.title).join(ti, ti.c.item_id == si.c.id).where(ti.c.topic_id == c.topic_id)
            ).scalars().all()
            s.execute(
                update(m.topics)
                .where(m.topics.c.id == c.topic_id)
                .values(keywords=top_terms(all_titles), updated_at=now)
            )
            report.assigned += len(c.item_ids)
        touched = [c.topic_id for c in clusters if c.item_ids]

    if core is not None:
        for topic_id in touched:
            relabel(db, tenant_id, core, topic_id, report)
    return report


def relabel(db: Database, tenant_id: str, core: CoreClient, topic_id: int, report: TopicReport) -> None:
    with db.tenant(tenant_id) as s:
        titles = s.execute(
            select(m.source_items.c.title)
            .join(m.topic_items, m.topic_items.c.item_id == m.source_items.c.id)
            .where(m.topic_items.c.topic_id == topic_id)
            .order_by(m.source_items.c.id.desc())
            .limit(20)
        ).scalars().all()
    try:
        value, calls = label_topic(core, titles)
    except CoreError as e:
        log.warning("labelling topic %s failed: %s", topic_id, e)
        report.label_errors += 1
        return
    with db.tenant(tenant_id) as s:
        for c in calls:
            repo.record_llm_usage(s, tenant_id, "topic_label", c.model, c.usage, c.cost)
        values = {"label": value["label"], "summary": value["summary"], "labelled_by": "model"}
        s.execute(update(m.topics).where(m.topics.c.id == topic_id).values(**values))
    report.labelled += 1
