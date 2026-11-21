"""Evergreen proposals: articles that stay useful for months, for sites that
do not live on the news of the day (Francesco 2026-11-07: recipes,
lifestyle, company blogs; "la metrica non è solo l'urgenza della notizia,
ma la pertinenza tematica, la stagionalità o la copertura di un cluster").

A site chooses what it receives in Sito e profilo (`settings.content_mode`):

- "news": the news of the day, as before (proposals.plan);
- "evergreen": only articles on a theme;
- "mixed": `settings.evergreen_share` per cent of them on a theme, the rest news.

A theme comes from one of three places, each with its reason:

- gap: a subtopic of the approved profile the site has written little or
  nothing about, or nothing for months;
- search: a search that brings people to the site from Google (Search
  Console) and that no article of the site answers;
- season: what the site's readers look for this month (season, holidays,
  recurring events), asked to the model once a month and kept in the profile.

The rule does not change: the article is written from the items the site
follows, every sentence citing one. A theme without at least two items from
two sources in the last weeks is not proposed: there is nothing to write it
from, and the newsroom never invents.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from . import models as m, usage
from .core_client import CoreClient, CoreError
from .text import tokens

log = logging.getLogger(__name__)

MODES = ("news", "evergreen", "mixed")
SHARE = 50  # mixed: the share of the day's proposals on a theme
WINDOW = 30  # days of items a theme is written from (the archive keeps about as many)
FOUND = 8  # items cited by an evergreen proposal, the closest first
MIN_ITEMS = 2
MIN_SOURCES = 2
STALE_DAYS = 120  # a subtopic with nothing newer than this is a gap again
REPEAT_DAYS = 21  # a theme proposed is not proposed again for this long
MAX_SEASON = 6
TOPIC = "Contenuti senza scadenza"
KIND = "evergreen"


def mode_of(body: dict | None) -> str:
    mode = ((body or {}).get("settings") or {}).get("content_mode")
    return mode if mode in MODES else "news"


def share_of(body: dict | None) -> int:
    try:
        return max(0, min(100, int(((body or {}).get("settings") or {}).get("evergreen_share", SHARE))))
    except (TypeError, ValueError):
        return SHARE


def split(body: dict | None, limit: int) -> tuple[int, int]:
    """How many of `limit` proposals are on a theme, and how many are news."""
    mode = mode_of(body)
    if mode == "news":
        return 0, limit
    if mode == "evergreen":
        return limit, 0
    n = int(limit * share_of(body) / 100 + 0.5)
    return n, limit - n


@dataclass
class Theme:
    reason: str  # gap, search, season
    label: str
    terms: set[str]
    strength: float  # 0 to 1: how much the site needs it
    facts: dict = field(default_factory=dict)  # what the reason stands on, for the dashboard

    @property
    def opportunity(self) -> float:
        return round(40 + 45 * max(0.0, min(1.0, self.strength)), 2)


# --- where themes come from -------------------------------------------------


def gaps(s, site_id: int, body: dict, now: datetime) -> list[Theme]:
    """Subtopics of the profile the site's own articles cover least, or not
    for months."""
    subs = [x for x in body.get("subtopics") or [] if x.get("name") and x.get("keywords")]
    if not subs:
        return []
    sp = m.site_posts.c
    posts = s.execute(select(sp.url, sp.title, sp.categories, sp.published_at).where(
        sp.site_id == site_id, (sp.published_at >= now - timedelta(days=365)) | sp.published_at.is_(None))).all()
    # Each post goes to the subtopic whose keywords it shares most (a small
    # site has too few posts to weigh words by how rare they are).
    kws = [{w for k in x["keywords"] for w in tokens(k)} for x in subs]
    hits: list[list[str]] = [[] for _ in subs]
    for r in posts:
        words = set(tokens(r.title)) | {w for c in r.categories or [] for w in tokens(c)}
        counts = [len(words & k) for k in kws]
        best = max(range(len(subs)), key=lambda i: counts[i])
        if counts[best]:
            hits[best].append(r.url)
    total = sum(len(h) for h in hits)
    shares = [(len(h) / total if total else 0.0, h) for h in hits]
    when = {r.url: r.published_at for r in posts}
    out = []
    fair = 1 / len(subs)
    for sub, (share, urls) in zip(subs, shares):
        newest = max((when[u] for u in urls if when.get(u)), default=None)
        if newest is not None and newest.tzinfo is None:
            newest = newest.replace(tzinfo=UTC)
        strength = 1.0 if not urls else max(0.0, 1 - share / fair)
        stale = newest is not None and now - newest > timedelta(days=STALE_DAYS)
        if stale:
            strength = max(strength, 0.6)
        if strength < 0.3:
            continue
        terms = {w for k in sub["keywords"] for w in tokens(k)}
        out.append(Theme("gap", sub["name"], terms, strength, {
            "posts": len(urls), "last": newest.date().isoformat() if newest else None}))
    return out


def searches(s, site_id: int, now: datetime, days: int = 28, limit: int = 8) -> list[Theme]:
    """Searches that bring people to the site and that no article of the
    site answers: the words of the search are not most of a title."""
    st, sp = m.search_stats.c, m.site_posts.c
    rows = s.execute(select(st.key, func.sum(st.clicks).label("c"), func.sum(st.impressions).label("i"))
                     .where(st.site_id == site_id, st.kind == "query", st.day >= now.date() - timedelta(days=days))
                     .group_by(st.key).order_by((func.sum(st.clicks) * 10 + func.sum(st.impressions)).desc())
                     .limit(200)).all()
    if not rows:
        return []
    titles = [set(tokens(t)) for t in s.execute(select(sp.title).where(sp.site_id == site_id)).scalars()]
    best = max(float(r.c or 0) * 10 + float(r.i or 0) for r in rows) or 1.0
    out = []
    for r in rows:
        words = set(tokens(r.key))
        if len(words) < 2:  # one word is a subject, not a question
            continue
        if any(len(words & t) >= math.ceil(len(words) * 2 / 3) for t in titles):
            continue
        weight = float(r.c or 0) * 10 + float(r.i or 0)
        out.append(Theme("search", r.key, words, round(math.sqrt(weight / best), 3),
                         {"query": r.key, "impressions": int(r.i or 0), "clicks": int(r.c or 0)}))
        if len(out) >= limit:
            break
    return out


SEASON_SYSTEM = (
    "You are the editorial planner of a website. List what its readers look for in the month given, because of "
    "the season, holidays, recurring events or the calendar of its sector: themes for articles that stay useful, "
    "not news. Count also what readers start preparing now for the holidays of the next weeks: a site of "
    "recipes or gifts plans Christmas in November, Easter a month before. Reply with JSON only: "
    '{"themes": [{"name": "<the theme, in the site\'s language, max 80 chars>", "keywords": ["<3-6 lowercase '
    "keywords in the site's language, then the same in English>\"]}]}. At most " + str(MAX_SEASON) + " themes, "
    "only ones close to the site's subjects; none when the month means nothing special for this site."
)


def _season_valid(v) -> list[dict]:
    if not isinstance(v, dict) or not isinstance(v.get("themes"), list):
        raise ValueError("expected {themes: [...]}")
    out = []
    for x in v["themes"][:MAX_SEASON]:
        if isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"].strip():
            kws = [str(k).strip().lower() for k in x.get("keywords") or [] if str(k).strip()][:12]
            if kws:
                out.append({"name": x["name"].strip()[:80], "keywords": kws})
    return out


def season(db, tenant_id: str, site_id: int, body: dict, core: CoreClient | None, now: datetime) -> list[Theme]:
    """This month's themes, asked to the model once a month and kept in the
    profile's settings (`season`)."""
    month = now.strftime("%Y-%m")
    kept = ((body.get("settings") or {}).get("season")) or {}
    themes = kept.get("themes") if kept.get("month") == month else None
    if themes is None:
        if core is None:
            return []
        from .autopilot import brief_lines

        prompt = "\n".join([
            f"Site: {body.get('name')} ({body.get('domain')}), language: {body.get('language')}",
            f"Sector: {(body.get('sector') or {}).get('value') or 'not determined'}",
            f"Audience: {(body.get('audience') or {}).get('value') or 'not determined'}",
            f"Subtopics: {', '.join(x['name'] for x in body.get('subtopics', []))}",
            *brief_lines(body),
            f"Month: {month}",
        ])
        try:
            with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"season:{month}"):
                themes, _ = core.chat_json(SEASON_SYSTEM, prompt, _season_valid)
        except CoreError as e:
            log.warning("season themes for site %s failed: %s", site_id, e)
            return []
        from .profile import _set_settings

        with db.tenant(tenant_id) as s:
            _set_settings(s, site_id, season={"month": month, "themes": themes})
    return [Theme("season", x["name"], {w for k in x["keywords"] for w in tokens(k)}, 0.7, {"month": month})
            for x in themes]


# --- proposals ---------------------------------------------------------------


SYSTEM = (
    "You are the editorial planner of a website. The site needs an article on the theme given, one that stays "
    "useful for months: a guide, an explainer, a comparison, a how-to, a list of practical advice. It is not news "
    "of the day. Using ONLY the items listed (each has an id), propose one original article on the theme for the "
    "site described, bringing several items together. Write in the site's language. Reply with JSON only: "
    '{"title": "<headline, max 120 chars, no date and no \'today\'>", "angle": "<what the article explains and '
    'for whom, 1-3 sentences>", "why_now": "<why this theme is worth an article on this site, 1 sentence>", '
    '"format": "<one of: guide, tutorial, comparison, analysis, data, review, recipe (only when an item gives a '
    'whole recipe: its ingredients and its steps; on a site of recipes, its own recipe on the theme), menu (only on '
    'a site of recipes, for a holiday or an occasion: a whole menu of several courses)>", '
    '"country": "<the ISO 3166-1 alpha-2 code of the country the theme is about, empty when it is not about one>", '
    '"section": "<the one of the site\'s sections it belongs to, written exactly as listed; empty when none are '
    'listed>", "citations": [<ids of the items the article relies on>]}. '
    "Cite at least two ids, from at least two different outlets when the list has them; cite only ids from the list. "
    "Call the dish by its traditional name exactly as it is known (Cotoletta alla milanese, Saltimbocca alla romana, Pasta e patate), never a made-up or descriptive name for it; but never copy an item's headline: the words around the dish's name are the site's own."
)

REASONS = {
    "gap": "The site has written little or nothing about this subject of its profile.",
    "search": "People reach the site from Google searching this, and no article of the site answers it.",
    "season": "Readers of the site look for this in the month ahead.",
}


def _topic(s, tenant_id: str) -> int:
    """The topic evergreen proposals hang on: one per newsroom, with an old
    date, so no clustering and no list of topics picks it up."""
    tp = m.topics.c
    found = s.execute(select(tp.id).where(tp.label == TOPIC, tp.labelled_by == KIND)).scalar()
    if found is not None:
        return found
    old = datetime(2000, 1, 1, tzinfo=UTC)
    return s.execute(insert(m.topics).values(tenant_id=tenant_id, label=TOPIC, keywords=[], labelled_by=KIND,
                                             created_at=old, updated_at=old).returning(tp.id)).scalar_one()


def recent_themes(s, site_id: int, now: datetime) -> set[str]:
    pr = m.proposals.c
    return {(b.get("theme") or "").lower() for b in s.execute(select(pr.basis).where(
        pr.site_id == site_id, pr.created_at >= now - timedelta(days=REPEAT_DAYS))).scalars()
            if b and b.get("kind") == KIND}


def themes(db, tenant_id: str, site_id: int, body: dict, core: CoreClient | None, now: datetime) -> list[Theme]:
    """Every theme the site could use today, the most needed first, without
    those proposed in the last weeks."""
    found = season(db, tenant_id, site_id, body, core, now)
    with db.tenant(tenant_id) as s:
        found += gaps(s, site_id, body, now) + searches(s, site_id, now)
        done = recent_themes(s, site_id, now)
    seen, out = set(done), []
    for th in sorted(found, key=lambda x: -x.strength):
        if th.label.lower() not in seen:
            seen.add(th.label.lower())
            out.append(th)
    return out


def items_for(s, site_id: int, terms: set[str], now: datetime) -> list:
    """The items the site follows on a theme in the last weeks, the closest
    first, with their source."""
    from .requested import search

    ids = search(s, site_id, terms, [], limit=FOUND, now=now, days=WINDOW)
    if not ids:
        return []
    si = m.source_items.c
    rows = {r.id: r for r in s.execute(select(si.id, si.title, si.url, si.summary, si.published_at, si.source_id,
                                              m.sources.c.name.label("source"))
                                       .join(m.sources, m.sources.c.id == si.source_id).where(si.id.in_(ids)))}
    return [rows[i] for i in ids if i in rows]


def _prompt(body: dict, th: Theme, items, learned: list[str]) -> str:
    from .autopilot import brief_lines

    style = body.get("style", {})
    lines = [
        f"Site: {body.get('name')} ({body.get('domain')}), language: {body.get('language')}",
        f"Sector: {(body.get('sector') or {}).get('value') or 'not determined'}",
        f"Audience: {(body.get('audience') or {}).get('value') or 'not determined'}",
        f"Tone: {style.get('tone') or 'not determined'}",
        f"Subtopics: {', '.join(x['name'] for x in body.get('subtopics', []))}",
        *brief_lines(body),
        *learned,
        f"Theme: {th.label}",
        f"Why the site needs it: {REASONS[th.reason]}",
        "Items:",
    ]
    for it in items:
        when = it.published_at.date().isoformat() if it.published_at else "undated"
        lines.append(f"[{it.id}] {it.title} ({it.source}, {when}) {it.url}\n    {(it.summary or '')[:400]}")
    return "\n".join(lines)


def spread_items(items: list) -> list:
    """The items of a theme with no outlet over a third of them (two at
    least), the closest first: one blog that publishes a lot does not write
    every theme (Francesco 2026-11-10)."""
    cap = max(2, -(-len(items) // 3))
    seen: dict[int, int] = {}
    out = []
    for it in items:
        seen[it.source_id] = seen.get(it.source_id, 0) + 1
        if seen[it.source_id] <= cap:
            out.append(it)
    return out


def _validator(allowed: set[int], outlet: dict[int, int] | None = None, headlines: list[str] | None = None):
    from .proposals import _validator as base
    from .text import copied_title

    check = base(allowed)
    outlets = set((outlet or {}).values())

    def validate(v) -> dict:
        out = check(v)
        if (copy := copied_title(out["title"], headlines or [])) is not None:
            raise ValueError(f"the title copies the headline «{copy}»: keep the dish's traditional name as it is "
                             "and write the rest of the title in the site's own words")
        if len(out["citations"]) < 2 and len(allowed) >= 2:
            raise ValueError("cite at least two ids")
        if len(outlets) >= 2 and len({outlet[i] for i in out["citations"] if i in outlet}) < 2:
            raise ValueError("cite items of at least two different outlets")
        return out

    return validate


def plan(db, tenant_id: str, site_id: int, core: CoreClient | None, limit: int, now: datetime | None = None,
         full: list[str] | None = None) -> list[int]:
    """Up to `limit` evergreen proposals for the site; their ids."""
    from .autopilot import recipe_site, settings_of
    from .proposals import full_line, lessons, mix

    now = now or datetime.now(UTC)
    if limit <= 0:
        return []
    with db.tenant(tenant_id) as s:
        prof = s.execute(select(m.editorial_profiles.c.id, m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).first()
    if prof is None:
        return []
    body = prof.body or {}
    created = []
    for th in themes(db, tenant_id, site_id, body, core, now):
        if len(created) >= limit:
            break
        with db.tenant(tenant_id) as s:
            items = items_for(s, site_id, th.terms, now)
            learned = lessons(s, site_id)
        if len(items) < MIN_ITEMS or len({it.source_id for it in items}) < MIN_SOURCES:
            continue  # nothing to write it from
        items = spread_items(items)
        allowed = {it.id for it in items}
        value, by = None, "heuristic"
        if core is not None:
            try:
                with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"evergreen:{th.reason}"):
                    value, _ = core.chat_json(SYSTEM, _prompt(body, th, items, learned + full_line(full)),
                                              _validator(allowed, {it.id: it.source_id for it in items}, [it.title for it in items]))
                by = "model"
            except CoreError as e:
                log.warning("evergreen proposal on %r failed: %s", th.label, e)
                continue
        if value is None:
            value = {"title": th.label, "angle": "", "why_now": "", "format": "guide", "country": "", "section": "",
                     "citations": [it.id for it in items[:4]]}
        if recipe_site(body):
            # A site of recipes writes its own recipe on every theme, or a whole menu for an occasion
            # (menus.py) when a person reads it first: the automatic newsroom writes recipes only.
            menu = value["format"] == "menu" and not settings_of(body)["enabled"]
            value["format"] = "menu" if menu else "guide" if value["format"] == "guide" else "recipe"
        with db.tenant(tenant_id) as s:
            topic = _topic(s, tenant_id)
            pid = s.execute(insert(m.proposals).values(
                tenant_id=tenant_id, site_id=site_id, topic_id=topic, profile_id=prof.id,
                opportunity=th.opportunity, title=value["title"], angle=value["angle"], why_now=value["why_now"],
                format=value["format"], generated_by=by,
                basis={"kind": KIND, "reason": th.reason, "theme": th.label, **th.facts} | mix(value, body),
            ).returning(m.proposals.c.id)).scalar_one()
            s.execute(insert(m.proposal_citations).values(
                [{"tenant_id": tenant_id, "proposal_id": pid, "item_id": i} for i in value["citations"]]))
        created.append(pid)
    return created


def is_evergreen(basis: dict | None) -> bool:
    return (basis or {}).get("kind") == KIND
