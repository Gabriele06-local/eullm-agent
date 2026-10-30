"""Editorial proposals: for the best opportunities of a site, a title, an
angle and why now, citing only items stored in the database.

The model sees the items with their database ids and must cite ids from
that list; anything else is rejected and asked again. The citations table
has a foreign key to the items, so a proposal can never point at a source
that is not stored.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import quotas, repo, usage
from .core_client import CoreClient, CoreError
from .opportunity import Opportunity

PER_DAY = 6  # profile.DEFAULT_SETTINGS, for a profile saved without it

log = logging.getLogger(__name__)

# Why a proposal was discarded: each reason teaches the newsroom something
# different (opportunity.opportunity weighs them; "unreliable_source" also
# lowers the site's rating of the sources it cited).
REASONS = ("already_covered", "off_topic", "too_generic", "unreliable_source")
SOURCE_PENALTY = 0.15

FORMATS = ["news", "analysis", "guide", "tutorial", "review", "comparison", "opinion", "interview", "data"]
MIN_OPPORTUNITY = 35.0
MIN_ITEMS = 2
MIN_SOURCES = 2  # the items come from at least two different sources
# ... unless one news item is strong enough on its own: a topic this close to
# the site, from a source this reliable, is worth an article the same day.
STRONG_OPPORTUNITY = 50.0
STRONG_SOURCE = 0.6

SYSTEM = (
    "You are the editorial planner of a website. Using ONLY the items listed (each has an id), propose one "
    "original article for the site described. Write in the site's language. Reply with JSON only: "
    '{"title": "<headline, max 120 chars>", "angle": "<the specific angle for this site, 1-3 sentences>", '
    '"why_now": "<why this matters now, from the items>", "format": "<one of: ' + ", ".join(FORMATS) + '>", '
    '"citations": [<ids of the items the proposal relies on>]}. Cite at least one id; cite only ids from the list.'
)


def _validator(allowed_ids: set[int]):
    def validate(v) -> dict:
        if not isinstance(v, dict):
            raise ValueError("expected an object")
        title = v.get("title")
        if not isinstance(title, str) or not 5 <= len(title.strip()) <= 160:
            raise ValueError("title must be 5 to 160 characters")
        cites = v.get("citations")
        if not isinstance(cites, list) or not cites:
            raise ValueError("citations must be a non-empty list of item ids")
        try:
            ids = [int(c) for c in cites]
        except (TypeError, ValueError):
            raise ValueError("citations must be item ids (integers)") from None
        unknown = [i for i in ids if i not in allowed_ids]
        if unknown:
            raise ValueError(f"ids {unknown} are not in the list")
        fmt = v.get("format") if v.get("format") in FORMATS else "analysis"
        return {
            "title": title.strip(),
            "angle": str(v.get("angle") or "").strip()[:1000],
            "why_now": str(v.get("why_now") or "").strip()[:1000],
            "format": fmt,
            "citations": list(dict.fromkeys(ids)),
        }

    return validate


def _items_for(s, topic_id: int, site_id: int | None = None, limit: int = 12):
    si = m.source_items
    f = m.sources.alias("f")
    rows = s.execute(
        select(si.c.id, si.c.title, si.c.url, si.c.summary, si.c.published_at, si.c.source_id,
               func.coalesce(f.c.name, m.sources.c.name).label("source"),
               func.coalesce(f.c.score, m.sources.c.score).label("source_score"))
        .join(m.topic_items, m.topic_items.c.item_id == si.c.id)
        .join(m.sources, m.sources.c.id == si.c.source_id)
        # The site's own name and rating of the source, when it follows it.
        .outerjoin(f, and_(or_(f.c.id == si.c.source_id, f.c.archive_id == si.c.source_id),
                           f.c.shared.is_not(True), f.c.site_id == site_id))
        .where(m.topic_items.c.topic_id == topic_id, si.c.duplicate_of.is_(None))
        .order_by(si.c.published_at.desc().nulls_last(), si.c.id)
        .limit(limit * 2)
    ).all()
    return list({r.id: r for r in rows}.values())[:limit]


def discards(s, site_id: int, since: datetime) -> list[tuple[str, int]]:
    """Why this site's proposals were discarded since a date, most frequent
    first: a plain discard counts as "down"."""
    p, f = m.proposals, m.feedback
    rows = s.execute(select(f.c.verdict, func.count()).join(p, (f.c.target == "proposal") & (f.c.target_id == p.c.id))
                     .where(p.c.site_id == site_id, f.c.given_at >= since, f.c.verdict.in_((*REASONS, "down")))
                     .group_by(f.c.verdict)).all()
    return sorted(((r[0], r[1]) for r in rows), key=lambda x: -x[1])


def lessons(s, site_id: int, days: int = 60) -> list[str]:
    """What the editors said about earlier proposals of this site, for the
    model that writes the next ones."""
    p, f = m.proposals, m.feedback
    since = datetime.now(UTC) - timedelta(days=days)
    rows = s.execute(select(f.c.verdict, p.c.title).join(p, (f.c.target == "proposal") & (f.c.target_id == p.c.id))
                     .where(p.c.site_id == site_id, f.c.given_at >= since,
                            f.c.verdict.in_(("too_generic", "already_covered", "off_topic")))
                     .order_by(f.c.given_at.desc()).limit(30)).all()
    said = {"too_generic": "Rejected as too generic: propose a narrower, more specific angle than these",
            "already_covered": "Rejected because the site already covered them: do not propose the same again",
            "off_topic": "Rejected as off topic for this site: stay away from these subjects"}
    out = []
    for verdict, intro in said.items():
        titles = [r.title for r in rows if r.verdict == verdict][:5]
        if titles:
            out.append(f"{intro}: " + "; ".join(f"«{x}»" for x in titles))
    return out


def _prompt(profile: dict, label: str, items, learned: list[str] | None = None) -> str:
    from .autopilot import brief_lines

    style = profile.get("style", {})
    lines = [
        f"Site: {profile.get('name')} ({profile.get('domain')}), language: {profile.get('language')}",
        f"Sector: {(profile.get('sector') or {}).get('value') or 'not determined'}",
        f"Audience: {(profile.get('audience') or {}).get('value') or 'not determined'}",
        f"Tone: {style.get('tone') or 'not determined'}; usual formats: {', '.join(style.get('formats', [])) or 'unknown'}",
        f"Subtopics: {', '.join(s['name'] for s in profile.get('subtopics', []))}",
        *brief_lines(profile),
        *(learned or []),
        f"Topic: {label}",
        "Items:",
    ]
    for it in items:
        when = it.published_at.date().isoformat() if it.published_at else "undated"
        lines.append(f"[{it.id}] {it.title} ({it.source}, {when}) {it.url}\n    {(it.summary or '')[:400]}")
    return "\n".join(lines)


def is_story(opportunity: float, items) -> bool:
    """Two items from two sources, or one strong item from a reliable source."""
    if len(items) >= MIN_ITEMS and len({it.source_id for it in items}) >= MIN_SOURCES:
        return True
    return bool(items) and opportunity >= STRONG_OPPORTUNITY and max(it.source_score or 0 for it in items) >= STRONG_SOURCE


def made_today(s, site_id: int, since: datetime) -> int:
    return s.execute(select(func.count()).select_from(m.proposals).where(
        m.proposals.c.site_id == site_id, m.proposals.c.created_at >= since)).scalar() or 0


def propose(db, tenant_id: str, site_id: int, opportunities: list[Opportunity], core: CoreClient | None = None,
            limit: int | None = None) -> list[int]:
    """Create proposals for the best opportunities; returns their ids."""
    p = m.editorial_profiles
    created = []
    with db.tenant(tenant_id) as s:
        prof = s.execute(select(p).where(p.c.site_id == site_id, p.c.status == "approved")).first()
        if prof is None:
            return []
        limit = limit or prof.body.get("settings", {}).get("proposals_per_day", PER_DAY)
        picks = [o for o in opportunities if o.score >= MIN_OPPORTUNITY and o.components.get("not_repeated", 1)]
        work = []
        for o in picks:
            if len(work) >= limit:
                break
            label = s.execute(select(m.topics.c.label).where(m.topics.c.id == o.topic_id)).scalar()
            items = _items_for(s, o.topic_id, site_id)
            # A single weak item (one new repository, one post) is not yet a
            # story, nor are two items from the same source (the same
            # repository found by two searches).
            if is_story(o.score, items):
                work.append((o, label, items))
        learned = lessons(s, site_id)
    for o, label, items in work:
        allowed = {it.id for it in items}
        value, calls, by = None, [], "heuristic"
        if core is not None:
            try:
                with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"topic:{o.topic_id}"):
                    value, calls = core.chat_json(SYSTEM, _prompt(prof.body, label, items, learned), _validator(allowed))
                by = "model"
            except CoreError as e:
                log.warning("proposal for topic %s failed: %s", o.topic_id, e)
        if value is None:
            value = {
                # The newest headline says more than the topic's keywords.
                "title": items[0].title if items else label,
                "angle": "",
                # The dashboard explains a proposal made without the model.
                "why_now": "",
                "format": "analysis",
                "citations": [it.id for it in items[:3]],
            }
        with db.tenant(tenant_id) as s:
            pid = s.execute(insert(m.proposals).values(
                tenant_id=tenant_id, site_id=site_id, topic_id=o.topic_id, profile_id=prof.id,
                opportunity=o.score, title=value["title"], angle=value["angle"], why_now=value["why_now"],
                format=value["format"], generated_by=by, basis={"kind": "topic"},
            ).returning(m.proposals.c.id)).scalar_one()
            s.execute(insert(m.proposal_citations).values(
                [{"tenant_id": tenant_id, "proposal_id": pid, "item_id": i} for i in value["citations"]]))
            created.append(pid)
    return created


def plan(db, tenant_id: str, site_id: int, core: CoreClient | None = None, limit: int | None = None,
         now: datetime | None = None) -> list[int]:
    """The day's proposals: first the topics told by several sources, then,
    for what is left of the number, the best single news for the site
    (picks.fill). Never empty while news comes in."""
    from . import opportunity, picks

    with db.tenant(tenant_id) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
        only = quotas.proposals_only(s)
    if body is None:
        return []
    limit = limit or body.get("settings", {}).get("proposals_per_day", PER_DAY)
    if only:
        limit = min(limit, quotas.PROPOSALS_ONLY_PER_DAY)
    ids = propose(db, tenant_id, site_id, opportunity.score_site(db, tenant_id, site_id, now=now), core, limit=limit)
    return ids + picks.fill(db, tenant_id, site_id, limit - len(ids), core, now=now)


TITLES_SYSTEM = (
    "You are the editorial planner of a website. The items listed (each has an id) are the trend of the moment "
    "in the site's sector. Using ONLY these items, propose {n} different articles the site could write on it: "
    "different angles, not the same headline reworded. Write in the site's language. Reply with JSON only: "
    '{{"options": [{{"title": "<headline, max 120 chars>", "angle": "<what the article says, for this site, '
    '1-3 sentences>", "why_now": "<why this matters now, from the items>", "format": "<one of: '
    + ", ".join(FORMATS) + '>", "citations": [<ids of the items it relies on>]}}]}}. '
    "Cite at least one id in each option; cite only ids from the list."
)


def _options_validator(allowed_ids: set[int], n: int):
    one = _validator(allowed_ids)

    def validate(v) -> list[dict]:
        if not isinstance(v, dict) or not isinstance(v.get("options"), list) or not v["options"]:
            raise ValueError('expected {"options": [...]}')
        out, seen = [], set()
        for o in v["options"][:n]:
            o = one(o)
            if o["title"].lower() not in seen:
                seen.add(o["title"].lower())
                out.append(o)
        return out

    return validate


def propose_titles(db, tenant_id: str, site_id: int, topic_id: int, core: CoreClient | None = None,
                   n: int = 3) -> list[int]:
    """Titles to choose from for one trend, asked for by a person: they
    become proposals of the site, each with a short summary (its angle)."""
    from . import opportunity

    p = m.editorial_profiles
    with db.tenant(tenant_id) as s:
        prof = s.execute(select(p).where(p.c.site_id == site_id, p.c.status == "approved")).first()
        if prof is None:
            return []
        label = s.execute(select(m.topics.c.label).where(m.topics.c.id == topic_id)).scalar()
        items = _items_for(s, topic_id, site_id)
    if label is None or not items:
        return []
    score = next((o.score for o in opportunity.score_site(db, tenant_id, site_id) if o.topic_id == topic_id), None)
    allowed = {it.id for it in items}
    options, calls, by = None, [], "heuristic"
    if core is not None:
        try:
            with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"topic:{topic_id}"):
                options, calls = core.chat_json(TITLES_SYSTEM.format(n=n), _prompt(prof.body, label, items),
                                                _options_validator(allowed, n))
            by = "model"
        except CoreError as e:
            log.warning("titles for topic %s failed: %s", topic_id, e)
    if options is None:
        # Without the model, the latest headlines of the trend.
        options = [{"title": it.title[:160], "angle": "", "why_now": "", "format": "news", "citations": [it.id]}
                   for it in items[:n]]
    created = []
    with db.tenant(tenant_id) as s:
        for o in options:
            pid = s.execute(insert(m.proposals).values(
                tenant_id=tenant_id, site_id=site_id, topic_id=topic_id, profile_id=prof.id,
                opportunity=score or 0.0, title=o["title"], angle=o["angle"], why_now=o["why_now"],
                format=o["format"], generated_by=by,
            ).returning(m.proposals.c.id)).scalar_one()
            s.execute(insert(m.proposal_citations).values(
                [{"tenant_id": tenant_id, "proposal_id": pid, "item_id": i} for i in o["citations"]]))
            created.append(pid)
    return created


def decide(db, tenant_id: str, proposal_id: int, accept: bool, by: str, note: str | None = None,
           reason: str | None = None) -> bool:
    """Accept or reject a proposal; the decision is also feedback, and a
    rejection may say why (REASONS)."""
    verdict = "up" if accept else reason if reason in REASONS else "down"
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.proposals.c.site_id, m.proposals.c.status).where(m.proposals.c.id == proposal_id)).first()
        if row is None or row.status != "proposed":
            return False
        s.execute(update(m.proposals).where(m.proposals.c.id == proposal_id).values(
            status="accepted" if accept else "rejected", decided_by=by))
        record_feedback(s, tenant_id, "proposal", proposal_id, verdict, by, note, row.site_id)
        if verdict == "unreliable_source":
            _distrust_sources(s, proposal_id, row.site_id)
    return True


def _distrust_sources(s, proposal_id: int, site_id: int) -> None:
    """The site's own rating of the sources the proposal cited goes down."""
    src = m.sources.c
    cited = select(m.source_items.c.source_id).join(
        m.proposal_citations, m.proposal_citations.c.item_id == m.source_items.c.id).where(
        m.proposal_citations.c.proposal_id == proposal_id)
    for sid, score, evidence in s.execute(select(src.id, src.score, src.evidence).where(
            src.site_id == site_id, src.shared.is_not(True), or_(src.id.in_(cited), src.archive_id.in_(cited)))).all():
        # Counted in its evidence, so the next evaluation (sources.rate) keeps it.
        evidence = {**(evidence or {}), "distrusted": (evidence or {}).get("distrusted", 0) + 1}
        s.execute(update(m.sources).where(src.id == sid).values(
            evidence=evidence, score=round(max(0.0, (0.5 if score is None else score) - SOURCE_PENALTY), 3)))


def record_feedback(s, tenant_id: str, target: str, target_id: int, verdict: str, by: str,
                    note: str | None = None, site_id: int | None = None) -> None:
    s.execute(insert(m.feedback).values(
        tenant_id=tenant_id, site_id=site_id, target=target, target_id=target_id, verdict=verdict,
        note=note, given_by=by, given_at=datetime.now(UTC)))


def top_up(db, tenant_id: str, site_id: int, since: datetime, core: CoreClient | None = None,
           now: datetime | None = None, manual: bool = False) -> list[int]:
    """Proposals now, on request, up to the day's number still left; asked
    for by a person (`manual`), a full day's number whatever was made."""
    with db.tenant(tenant_id) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
        if body is None:
            return []
        per_day = body.get("settings", {}).get("proposals_per_day", PER_DAY)
        if quotas.proposals_only(s):  # after the trial: a couple a day, asked for or not
            per_day, manual = min(per_day, quotas.PROPOSALS_ONLY_PER_DAY), False
        left = per_day if manual else per_day - made_today(s, site_id, since)
    if left <= 0:
        return []
    return plan(db, tenant_id, site_id, core, limit=left, now=now)
