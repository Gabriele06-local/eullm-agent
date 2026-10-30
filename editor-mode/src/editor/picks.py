"""Proposals every day: when the topics told by several sources
(proposals.propose) are not enough, the best single news of the last days
for the site fill the day's number.

1. Candidates, without the model: every news item of the last 72 hours the
   newsroom can read, the site's own sources first and then the shared
   archive, minus those already proposed to the site. Each is scored for the
   site: closeness to the profile's subtopics (word roots, so an Italian
   profile still meets English news: "inferenza" and "inference"), whether
   the site follows its source, the source's rating, freshness, how many
   sources tell the same story and whether the site already covered it.
2. With the model: one call reads the first 30 (one per story), picks the
   best and writes their title and angle. Without the model, or when it
   fails, the first ones of the formula are taken as they are.

Nothing is proposed only when no news at all came in for 72 hours: that is a
stopped collection, and the dashboard says so.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import usage
from .core_client import CoreClient, CoreError
from .opportunity import novelty
from .proposals import FORMATS, _options_validator
from .text import tokens

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=72)
NOT_AGAIN = timedelta(days=14)  # a story proposed to the site is not proposed again for this long
SHOWN = 30  # candidates the model reads
WEIGHTS = {"relevance": 0.40, "followed": 0.15, "source": 0.10, "freshness": 0.15, "sources": 0.10, "novelty": 0.10}

SYSTEM = (
    "You are the editorial planner of a website. Below are the site and a list of recent news items, each "
    "with an id. Choose the {n} items most worth an article for THIS site's readers now, and propose one "
    "article for each. News that matters to the whole sector counts even outside the site's usual topics "
    "(a security flaw in widely used software, a major model or product release, a new law). Each choice "
    "must be a different story. Write in the site's language. Reply with JSON only: "
    '{{"options": [{{"title": "<headline, max 120 chars>", "angle": "<what the article says, for this site, '
    '1-3 sentences>", "why_now": "<why it matters to these readers now>", "format": "<one of: '
    + ", ".join(FORMATS) + '>", "citations": [<the id of the item; first the main one>]}}]}}. '
    "Cite only ids from the list."
)


@dataclass
class Candidate:
    item_id: int
    topic_id: int
    title: str
    summary: str
    source: str
    published_at: datetime | None
    followed: bool
    sources: int  # sources telling the same story
    relevance: float
    matched: str | None
    source_score: float | None
    age_hours: float
    score: float = 0.0


def roots(words) -> set[str]:
    """Word roots: the first six letters, so close words of two languages
    meet (inferenza / inference, vulnerabilità / vulnerability)."""
    return {w[:6] for w in words}


def closeness(terms: set[str], profile: dict) -> tuple[float, str | None] | None:
    """How close a news item is to the site's subtopics, 0 to 1, and which
    one; None when it touches an excluded subject."""
    r = roots(terms)
    exclude = roots(t for e in profile.get("settings", {}).get("exclude", []) for t in tokens(e))
    if r & exclude:
        return None
    best, name = 0.0, None
    for sub in profile.get("subtopics", []):
        kw = roots(t for k in sub.get("keywords", []) + [sub.get("name", "")] for t in tokens(k))
        if not kw:
            continue
        c = min(1.0, len(r & kw) / min(len(kw), 3))
        c *= 0.8 + 0.2 * min(1.0, sub.get("share", 0) / 0.3)
        if c > best:
            best, name = c, sub.get("name")
    sector = roots(tokens((profile.get("sector") or {}).get("value") or ""))
    if best < 0.3 and r & sector:
        best = 0.3
    return round(best, 3), name


def _aware(v: datetime | None) -> datetime | None:
    return v if v is None or v.tzinfo else v.replace(tzinfo=UTC)


def candidates(s, site_id: int, profile: dict, now: datetime) -> list[Candidate]:
    """The news of the last 72 hours for the site, best first, one per story."""
    si, ti, p, pc = m.source_items, m.topic_items, m.proposals, m.proposal_citations
    f = m.sources.alias("f")
    since = now - NOT_AGAIN
    cited = select(pc.c.item_id).join(p, p.c.id == pc.c.proposal_id).where(p.c.site_id == site_id, p.c.created_at >= since)
    told = select(p.c.topic_id).where(p.c.site_id == site_id, p.c.created_at >= since)
    rows = s.execute(
        select(si.c.id, si.c.title, si.c.summary, si.c.published_at, si.c.fetched_at, ti.c.topic_id,
               func.coalesce(f.c.name, m.sources.c.name).label("source"),
               func.coalesce(f.c.score, m.sources.c.score).label("source_score"), f.c.id.label("followed"))
        .join(ti, ti.c.item_id == si.c.id)
        .join(m.sources, m.sources.c.id == si.c.source_id)
        # The site's own source, or its follow of the archive's one.
        .outerjoin(f, and_(or_(f.c.id == si.c.source_id, f.c.archive_id == si.c.source_id), f.c.shared.is_not(True),
                           f.c.status == "active", or_(f.c.site_id == site_id, f.c.site_id.is_(None))))
        .where(si.c.fetched_at >= now - WINDOW, si.c.duplicate_of.is_(None),
               ~si.c.id.in_(cited), ~ti.c.topic_id.in_(told))
        .order_by(si.c.fetched_at.desc(), si.c.id).limit(800)
    ).all()
    seen: dict[int, object] = {}
    for r in rows:
        if r.id not in seen or (r.followed and not seen[r.id].followed):
            seen[r.id] = r
    rows = list(seen.values())
    if not rows:
        return []
    topics = {r.topic_id for r in rows}
    told_by = dict(s.execute(
        select(ti.c.topic_id, func.count(func.distinct(si.c.source_id)))
        .join(si, si.c.id == ti.c.item_id).where(ti.c.topic_id.in_(topics)).group_by(ti.c.topic_id)).all())
    out = []
    for r in rows:
        near = closeness(set(tokens(r.title)) | set(tokens((r.summary or "")[:400])), profile)
        if near is None:
            continue
        when = _aware(r.published_at) or _aware(r.fetched_at) or now
        c = Candidate(r.id, r.topic_id, r.title, r.summary or "", r.source, r.published_at, bool(r.followed),
                      told_by.get(r.topic_id, 1), near[0], near[1], r.source_score,
                      max((now - when).total_seconds(), 0) / 3600)
        c.score = _score(c, 1.0)
        out.append(c)
    out.sort(key=lambda c: (-c.score, -c.item_id))
    # The site's history only for the first ones: it is the slow part.
    history = [(h.title, list(h.minhash)) for h in s.execute(
        select(m.site_posts.c.title, m.site_posts.c.minhash).where(
            m.site_posts.c.site_id == site_id,
            or_(m.site_posts.c.published_at >= now - timedelta(days=180), m.site_posts.c.published_at.is_(None))))]
    best: dict[int, Candidate] = {}
    for c in out[:SHOWN * 4]:
        if history:
            c.score = _score(c, novelty([c.title], history))
        if c.topic_id not in best or c.score > best[c.topic_id].score:
            best[c.topic_id] = c
    for c in out[SHOWN * 4:]:
        best.setdefault(c.topic_id, c)
    return sorted(best.values(), key=lambda c: (-c.score, -c.item_id))


def _score(c: Candidate, new: float) -> float:
    """0 to 100; `new`: 1 when the site never covered the story."""
    parts = {
        "relevance": c.relevance,
        "followed": 1.0 if c.followed else 0.4,
        "source": c.source_score if c.source_score is not None else 0.5,
        "freshness": math.exp(-c.age_hours / 36),
        "sources": min(1.0, (c.sources - 1) / 3),
        "novelty": new,
    }
    return round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()), 2)


def _prompt(profile: dict, cands: list[Candidate], learned: list[str] | None = None) -> str:
    style = profile.get("style", {})
    lines = [
        f"Site: {profile.get('name')} ({profile.get('domain')}), language: {profile.get('language')}",
        f"Sector: {(profile.get('sector') or {}).get('value') or 'not determined'}",
        f"Audience: {(profile.get('audience') or {}).get('value') or 'not determined'}",
        f"Tone: {style.get('tone') or 'not determined'}",
        f"Subtopics: {', '.join(s['name'] for s in profile.get('subtopics', []))}",
        *(learned or []),
        "News:",
    ]
    for c in cands:
        when = c.published_at.date().isoformat() if c.published_at else "undated"
        told = f", told by {c.sources} sources" if c.sources > 1 else ""
        lines.append(f"[{c.item_id}] {c.title} ({c.source}, {when}{told})\n    {c.summary[:300]}")
    return "\n".join(lines)


def fill(db, tenant_id: str, site_id: int, n: int, core: CoreClient | None = None,
         now: datetime | None = None) -> list[int]:
    """Up to `n` proposals from single news items; returns their ids."""
    if n <= 0:
        return []
    now = now or datetime.now(UTC)
    pr = m.editorial_profiles
    with db.tenant(tenant_id) as s:
        prof = s.execute(select(pr).where(pr.c.site_id == site_id, pr.c.status == "approved")).first()
        if prof is None:
            return []
        cands = candidates(s, site_id, prof.body, now)
        from .proposals import lessons
        learned = lessons(s, site_id)
    if not cands:
        return []
    shown = cands[:SHOWN]
    by_id = {c.item_id: c for c in shown}
    chosen: list[tuple[Candidate, dict, str]] = []
    if core is not None:
        try:
            with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"pick:{site_id}"):
                options, _ = core.chat_json(SYSTEM.format(n=n), _prompt(prof.body, shown, learned),
                                            _options_validator(set(by_id), n))
            for o in options:
                c = by_id[o["citations"][0]]
                if all(c.topic_id != x.topic_id for x, _, _ in chosen):
                    chosen.append((c, o, "model"))
        except CoreError as e:
            log.warning("picking news for site %s failed: %s", site_id, e)
    for c in shown:
        if len(chosen) >= n:
            break
        # Without the model a news item far from every subtopic is not proposed:
        # only the model can tell that it still matters to the site's readers.
        if c.relevance > 0 and all(c.topic_id != x.topic_id for x, _, _ in chosen):
            chosen.append((c, {"title": c.title[:160], "angle": "", "why_now": "", "format": "news",
                               "citations": [c.item_id]}, "formula"))
    created = []
    with db.tenant(tenant_id) as s:
        for c, o, by in chosen[:n]:
            # The other news of the same story give the draft more to work with.
            more = s.execute(select(m.topic_items.c.item_id).join(
                m.source_items, m.source_items.c.id == m.topic_items.c.item_id).where(
                m.topic_items.c.topic_id == c.topic_id, m.source_items.c.duplicate_of.is_(None),
                m.topic_items.c.item_id.not_in(o["citations"])).order_by(m.topic_items.c.item_id.desc()).limit(2)).scalars()
            cites = list(dict.fromkeys([*o["citations"], *more]))
            pid = s.execute(insert(m.proposals).values(
                tenant_id=tenant_id, site_id=site_id, topic_id=c.topic_id, profile_id=prof.id, opportunity=c.score,
                title=o["title"], angle=o["angle"], why_now=o["why_now"], format=o["format"],
                generated_by="model" if by == "model" else "heuristic",
                basis={"kind": "news", "picked_by": by, "score": c.score, "matched": c.matched,
                       "sources": c.sources, "followed": c.followed},
            ).returning(m.proposals.c.id)).scalar_one()
            s.execute(insert(m.proposal_citations).values(
                [{"tenant_id": tenant_id, "proposal_id": pid, "item_id": i} for i in cites]))
            created.append(pid)
    return created


def why_none(db, tenant_id: str, site_id: int, now: datetime | None = None) -> str:
    """Why nothing was proposed: no approved profile, no news at all in 72
    hours (the collection is stopped), or every news already proposed."""
    now = now or datetime.now(UTC)
    with db.tenant(tenant_id) as s:
        approved = s.execute(select(m.editorial_profiles.c.id).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).first()
        if approved is None:
            return "no_profile"
        news = s.execute(select(func.count()).select_from(m.source_items).where(
            m.source_items.c.fetched_at >= now - WINDOW)).scalar() or 0
    return "no_news" if not news else "all_proposed"
