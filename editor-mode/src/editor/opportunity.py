"""Editorial Opportunity Score: how worth it is for THIS site to write about
a topic now. It is computed per site and kept apart from the Hype Score,
which only says how much the topic is being talked about.

    O = 100 * sum(w_i * c_i) / sum(w_i)    over measured components

Components left unmeasured (no Hype Score yet, no feedback, unrated
sources) are listed as missing, like in the Hype Score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import repo
from .dedup import minhash, similarity
from .text import tokens

FORMULA_VERSION = "opportunity-v1"
WEIGHTS = {
    "relevance": 0.35,  # topic against the site's approved subtopics
    "hype": 0.20,  # latest Hype Score
    "freshness": 0.10,  # age of the newest item
    "novelty": 0.15,  # the site has not covered it already
    "not_repeated": 0.05,  # not proposed to this site in the last days
    "feedback": 0.05,  # what people said about similar proposals
    "source_quality": 0.10,  # rating of the sources behind the topic
}


@dataclass
class Opportunity:
    topic_id: int
    score: float
    components: dict[str, float]
    missing: list[str]
    matched_subtopic: str | None
    version: str = FORMULA_VERSION


def relevance(topic_terms: set[str], profile: dict) -> tuple[float, str | None]:
    exclude = {t for e in profile.get("settings", {}).get("exclude", []) for t in tokens(e)}
    if topic_terms & exclude:
        return 0.0, None
    best, name = 0.0, None
    for sub in profile.get("subtopics", []):
        kw = {t for k in sub.get("keywords", []) + [sub.get("name", "")] for t in tokens(k)}
        if not kw:
            continue
        r = min(1.0, len(topic_terms & kw) / min(len(kw), 3))
        # A subtopic the site writes about a lot weighs a little more.
        r *= 0.8 + 0.2 * min(1.0, sub.get("share", 0) / 0.3)
        if r > best:
            best, name = r, sub.get("name")
    return round(best, 3), name


def novelty(titles: list[str], history: list[tuple[str, list[int]]]) -> float:
    """1 when nothing in the site's history looks like the topic."""
    if not history:
        return 1.0
    worst = 0.0
    for t in titles[:10]:
        sig, terms = minhash(t), set(tokens(t))
        for h_title, h_sig in history:
            h_terms = set(tokens(h_title))
            overlap = len(terms & h_terms) / min(len(terms), len(h_terms)) if terms and h_terms else 0
            worst = max(worst, similarity(sig, h_sig), overlap if len(terms & h_terms) >= 3 else 0)
    return round(1 - min(worst, 1.0), 3)


def opportunity(
    topic_id: int,
    topic_terms: set[str],
    titles: list[str],
    profile: dict,
    *,
    hype: float | None,
    newest: datetime | None,
    history: list[tuple[str, list[int]]],
    proposed_recently: bool,
    feedback: list[str],
    source_scores: list[float],
    now: datetime,
) -> Opportunity:
    comp: dict[str, float] = {}
    comp["relevance"], matched = relevance(topic_terms, profile)
    if hype is not None:
        comp["hype"] = round(hype / 100, 4)
    if newest is not None:
        comp["freshness"] = round(math.exp(-max((now - newest).total_seconds(), 0) / 3600 / 36), 4)
    comp["novelty"] = novelty(titles, history)
    comp["not_repeated"] = 0.0 if proposed_recently else 1.0
    if feedback:
        delta = sum({"up": 0.15, "down": -0.15, "off_topic": -0.25, "already_covered": -0.2}.get(f, 0) for f in feedback)
        comp["feedback"] = round(max(0.0, min(1.0, 0.5 + delta)), 3)
    if source_scores:
        comp["source_quality"] = round(sum(source_scores) / len(source_scores), 3)
    missing = [k for k in WEIGHTS if k not in comp]
    total = sum(WEIGHTS[k] for k in comp)
    score = 100 * sum(WEIGHTS[k] * v for k, v in comp.items()) / total
    return Opportunity(topic_id, round(score, 2), comp, missing, matched)


def score_site(db, tenant_id: str, site_id: int, now: datetime | None = None, days: int = 3) -> list[Opportunity]:
    """Opportunity of every recent topic for one site with an approved profile."""
    now = now or datetime.now(UTC)
    p = m.editorial_profiles
    with db.tenant(tenant_id) as s:
        prof = s.execute(select(p).where(p.c.site_id == site_id, p.c.status == "approved")).first()
        if prof is None:
            return []
        body = prof.body
        min_rel = body.get("settings", {}).get("min_relevance", 0.15)
        history = [(r.title, list(r.minhash)) for r in s.execute(
            select(m.site_posts.c.title, m.site_posts.c.minhash).where(
                m.site_posts.c.site_id == site_id,
                (m.site_posts.c.published_at >= now - timedelta(days=180)) | m.site_posts.c.published_at.is_(None),
            ))]
        recent_props = set(s.execute(select(m.proposals.c.topic_id).where(
            m.proposals.c.site_id == site_id, m.proposals.c.created_at >= now - timedelta(days=14))).scalars())
        fb_rows = s.execute(
            select(m.feedback.c.verdict, m.proposals.c.topic_id)
            .join(m.proposals, (m.feedback.c.target == "proposal") & (m.feedback.c.target_id == m.proposals.c.id))
            .where(m.proposals.c.site_id == site_id)
        ).all()
        # Topics of the archive with news from a source this site follows.
        mine = (select(m.topic_items.c.topic_id)
                .join(m.source_items, m.source_items.c.id == m.topic_items.c.item_id)
                .where(repo.followed_by(site_id)))
        topics = s.execute(select(m.topics).where(m.topics.c.updated_at >= now - timedelta(days=days),
                                                  m.topics.c.id.in_(mine))).all()
        f = m.sources.alias("f")
        out = []
        for t in topics:
            members = s.execute(
                select(m.source_items.c.id, m.source_items.c.title, m.source_items.c.published_at,
                       m.source_items.c.fetched_at, func.coalesce(f.c.score, m.sources.c.score).label("score"))
                .join(m.topic_items, m.topic_items.c.item_id == m.source_items.c.id)
                .join(m.sources, m.sources.c.id == m.source_items.c.source_id)
                # The site's own rating of the source, when it follows it.
                .outerjoin(f, and_(or_(f.c.id == m.source_items.c.source_id, f.c.archive_id == m.source_items.c.source_id),
                                   f.c.shared.is_not(True), f.c.site_id == site_id))
                .where(m.topic_items.c.topic_id == t.id)
            ).all()
            members = list({x.id: x for x in members}.values())
            if not members:
                continue
            titles = [x.title for x in members]
            terms = set(t.keywords or []) | {w for x in titles for w in tokens(x)} | set(tokens(t.label))
            hype = s.execute(select(m.trend_scores.c.score).where(m.trend_scores.c.topic_id == t.id)
                             .order_by(m.trend_scores.c.computed_at.desc()).limit(1)).scalar()
            t_terms = set(t.keywords or [])
            feedback = [v for v, tid in fb_rows if tid == t.id] + [
                v for v, tid in fb_rows if tid != t.id and v in ("off_topic", "already_covered") and t_terms
                and _overlaps(s, tid, t_terms)
            ]
            o = opportunity(
                t.id, terms, titles, body, hype=hype,
                newest=max((x.published_at or x.fetched_at for x in members), default=None),
                history=history, proposed_recently=t.id in recent_props, feedback=feedback,
                source_scores=[x.score for x in members if x.score is not None], now=now,
            )
            if o.components["relevance"] < min_rel:
                continue
            s.execute(insert(m.opportunity_scores).values(
                tenant_id=tenant_id, topic_id=t.id, site_id=site_id, profile_id=prof.id,
                formula_version=o.version, score=o.score,
                components={**o.components, "matched_subtopic": o.matched_subtopic}, missing=o.missing))
            out.append(o)
    return sorted(out, key=lambda o: -o.score)


def _overlaps(s, topic_id: int, terms: set[str]) -> bool:
    kw = s.execute(select(m.topics.c.keywords).where(m.topics.c.id == topic_id)).scalar() or []
    return len(set(kw) & terms) >= 3
