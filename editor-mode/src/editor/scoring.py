"""Hype Score: how much a topic is being talked about right now.

    H = 100 * sum(w_i * c_i) / sum(w_i)

over the components that could be measured for the topic. Each component
c_i is in [0, 1]. A component with no data (no Hacker News story, no GitHub
repository...) is left out of both sums and listed as missing, instead of
counting as zero: a topic nobody posted on Hacker News is not less hyped on
Hacker News, it is unmeasured there. Every stored score carries the formula
version, so scores computed with different formulas are never compared
silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import repo
from .db import Database

FORMULA_VERSION = "hype-v1"

WEIGHTS = {
    "velocity": 0.30,  # items in the last 24 hours against the previous days
    "coverage": 0.20,  # distinct sources talking about it
    "community": 0.20,  # Hacker News points and comments
    "code": 0.10,  # GitHub stars gained
    "models": 0.10,  # Hugging Face trending score
    "research": 0.10,  # arXiv papers
}


@dataclass
class Member:
    source_id: int
    kind: str
    at: datetime  # published_at, or fetched_at when the source gives no date
    metrics: dict = field(default_factory=dict)
    # (observed_at, numeric metrics) oldest first
    observations: list[tuple[datetime, dict]] = field(default_factory=list)


@dataclass
class Hype:
    score: float
    components: dict[str, float]
    missing: list[str]
    version: str = FORMULA_VERSION


def _sat(x: float, scale: float) -> float:
    """0 at 0, 0.63 at `scale`, towards 1 after."""
    return 1 - math.exp(-max(x, 0) / scale)


def _growth(obs: list[tuple[datetime, dict]], key: str, now: datetime, hours: int = 24) -> float | None:
    """Increase of a counter over the window, from observations."""
    points = [(t, o[key]) for t, o in obs if key in o]
    if len(points) < 2:
        return None
    since = now - timedelta(hours=hours)
    before = [v for t, v in points if t <= since] or [points[0][1]]
    return points[-1][1] - before[-1]


def components(members: list[Member], now: datetime) -> dict[str, float]:
    out: dict[str, float] = {}
    if not members:
        return out
    day = now - timedelta(days=1)
    recent = [x for x in members if x.at >= day]
    older = [x for x in members if now - timedelta(days=3) <= x.at < day]
    base_per_day = len(older) / 2
    ratio = len(recent) / max(base_per_day, 0.5)
    out["velocity"] = ratio / (ratio + 1)
    window = [x for x in members if x.at >= now - timedelta(days=3)]
    out["coverage"] = min(1.0, max(len({x.source_id for x in window}) - 1, 0) / 4)

    hn = [x for x in members if x.kind == "hackernews"]
    if hn:
        heat = sum(x.metrics.get("points", 0) + 2 * x.metrics.get("comments", 0) for x in hn)
        out["community"] = _sat(heat, 300)

    gh = [x for x in members if x.kind == "github"]
    if gh:
        per_day = 0.0
        for x in gh:
            g = _growth(x.observations, "stars", now)
            if g is None and "stars_today" in x.metrics:  # the Trending page says it
                g = x.metrics["stars_today"]
            if g is None:  # one observation: average since creation
                age_days = max((now - x.at).total_seconds() / 86400, 1)
                g = x.metrics.get("stars", 0) / age_days
            per_day += g
        out["code"] = _sat(per_day, 200)

    hf = [x for x in members if x.kind == "huggingface"]
    if hf:
        out["models"] = _sat(sum(x.metrics.get("trending", 0) for x in hf), 100)

    papers = [x for x in members if x.kind == "arxiv" and x.at >= now - timedelta(days=3)]
    if any(x.kind == "arxiv" for x in members):
        out["research"] = min(1.0, len(papers) / 5)
    return out


def hype(members: list[Member], now: datetime | None = None) -> Hype:
    now = now or datetime.now(UTC)
    comp = components(members, now)
    missing = [k for k in WEIGHTS if k not in comp]
    total_w = sum(WEIGHTS[k] for k in comp)
    score = 100 * sum(WEIGHTS[k] * v for k, v in comp.items()) / total_w if total_w else 0.0
    return Hype(round(score, 2), {k: round(v, 4) for k, v in comp.items()}, missing)


def load_members(s, topic_id: int) -> list[Member]:
    si, src = m.source_items, m.sources
    rows = s.execute(
        select(si.c.id, si.c.source_id, src.c.kind, si.c.published_at, si.c.fetched_at, si.c.metrics)
        .join(m.topic_items, m.topic_items.c.item_id == si.c.id)
        .join(src, src.c.id == si.c.source_id)
        .where(m.topic_items.c.topic_id == topic_id)
    ).all()
    ids = [r.id for r in rows]
    obs: dict[int, list] = {i: [] for i in ids}
    if ids:
        for o in s.execute(
            select(m.observations.c.item_id, m.observations.c.observed_at, m.observations.c.metrics)
            .where(m.observations.c.item_id.in_(ids))
            .order_by(m.observations.c.observed_at)
        ):
            obs[o.item_id].append((o.observed_at, o.metrics))
    return [
        Member(r.source_id, r.kind, r.published_at or r.fetched_at, r.metrics or {}, obs[r.id]) for r in rows
    ]


def score_topics(db: Database, tenant_id: str, days: int = 7, now: datetime | None = None) -> dict[int, Hype]:
    """Compute and store the Hype Score of every topic updated in the last
    days, once, in the archive the newsroom reads."""
    now = now or datetime.now(UTC)
    results = {}
    tenant_id = repo.archive_of(db, tenant_id)
    with db.tenant(tenant_id) as s:
        topic_ids = s.execute(
            select(m.topics.c.id).where(m.topics.c.updated_at >= now - timedelta(days=days))
        ).scalars().all()
        for tid in topic_ids:
            h = hype(load_members(s, tid), now)
            s.execute(
                insert(m.trend_scores).values(
                    tenant_id=tenant_id, topic_id=tid, formula_version=h.version,
                    score=h.score, components=h.components, missing=h.missing,
                )
            )
            results[tid] = h
    return results
