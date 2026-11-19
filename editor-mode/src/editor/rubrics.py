"""Rubrics the newsroom decides (Francesco 2026-11-19, with his limits).

Every Monday, for a site with the automatic newsroom, the newsroom looks
at what the news talked about in the week just gone: the topics of the
shared archive's trend (trends.py), not the site's own articles, so the
line does not learn from itself. The model files each topic under one of
the site's rubrics, or under none when it is outside the site's context
or on a subject to avoid. Each rubric then weighs as its share of that
hype: a rubric the week talked about more gets more places a day
(autopilot.section_cap), one it talked about less gets fewer.

The limits:
- the owner's rubrics are an anchor: their priority goes down to FLOOR at
  most, never to nothing, and they are never removed; it goes back up the
  week the news comes back;
- at most one new rubric a week, only on a subject in the trend on at
  least MIN_DAYS days, inside the site's context and never on a subject to
  avoid, MAX_ADDED in all; it becomes a topic of the line in use, so its
  news reach the site;
- every change is in the Monday report and in the Automatic newsroom tab,
  where the owner undoes the last one, takes out a rubric the newsroom
  added (it does not come back) or switches all this off for the site;
- on by default only with the automatic newsroom.

State in the profile's settings, `rubrics`: enabled (None: as the
automatic newsroom), weights {rubric: weight}, added [{name, keywords,
at}], refused [names], week (the Monday already decided) and log (the
latest changes first, each with what was there before).
"""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from . import models as m
from . import repo, usage
from .core_client import CoreClient, CoreError

log = logging.getLogger(__name__)

FLOOR = 0.25  # the lowest weight of a rubric: one place a day, at least
TOP = 2.0
STEP = 0.25  # weights in quarters: a small swing of the news changes nothing
MIN_DAYS = 3
MAX_ADDED = 3
MIN_TOPICS = 5  # fewer topics of the week filed under the rubrics: the weights stay
TOPICS = 60
LOG = 8
WEEK = timedelta(days=7)

SYSTEM = (
    "You are the editor-in-chief of a website. You decide how much room each of the site's sections gets in the "
    "coming week, from the topics the news talked about in the week just gone. For each topic listed (each has "
    "an id) give the one section of the site it belongs to, written exactly as listed, or null when the topic "
    "does not fit the site's context and audience or touches a subject to avoid. Then, only when several topics "
    "that fit the site's context belong to none of its sections and together make one subject the site does not "
    "cover, suggest one new section for that subject: a short name in the site's language, like the other "
    "sections, and the keywords of its news; otherwise null. Reply with JSON only: "
    '{"topics": {"<id>": "<section or null>"}, "new": {"name": "<section name>", "keywords": ["<keyword>"], '
    '"topics": [<ids of the topics it gathers>]} or null}.'
)


def state(body: dict | None) -> dict:
    rb = ((body or {}).get("settings") or {}).get("rubrics") or {}
    return {"enabled": rb.get("enabled"), "weights": dict(rb.get("weights") or {}),
            "added": list(rb.get("added") or []), "refused": list(rb.get("refused") or []),
            "week": rb.get("week"), "log": list(rb.get("log") or [])}


def is_on(body: dict | None) -> bool:
    """Whether the newsroom decides the rubrics of this site: as the owner
    said, or, if they said nothing, when the automatic newsroom is on."""
    from .autopilot import settings_of

    on = state(body)["enabled"]
    return bool(settings_of(body)["enabled"] if on is None else on)


def monday(now: datetime) -> str:
    return (now.date() - timedelta(days=now.weekday())).isoformat()


def week_topics(s, now: datetime, limit: int = TOPICS) -> list[dict]:
    """The topics of the shared archive's trend in the last seven days: the
    sum of their hype over the snapshots (eight a day: how much and how long
    the news talked about them) and the days they were in it."""
    t = m.trend_snapshots
    rows = s.execute(select(t.c.taken_at, t.c.topics).where(
        t.c.tenant_id == repo.archive_tenant(s), t.c.taken_at >= now - WEEK, t.c.taken_at < now)).all()
    out: dict[int, dict] = {}
    for r in rows:
        for x in r.topics or []:
            tid = x.get("topic_id")
            if tid is None:
                continue
            o = out.setdefault(tid, {"id": tid, "label": x.get("label") or "", "summary": x.get("summary") or "",
                                     "mass": 0.0, "days": set()})
            o["label"] = x.get("label") or o["label"]
            o["mass"] += float(x.get("hype") or 0) / 8
            o["days"].add(r.taken_at.date())
    return sorted(out.values(), key=lambda o: (-o["mass"], o["id"]))[:limit]


def weigh(mass: dict[str, float], sections: list[str]) -> dict[str, float]:
    """Each rubric's weight: its share of the week's hype against an equal
    share, between FLOOR and TOP, in quarters."""
    total = sum(mass.get(x, 0.0) for x in sections)
    if not total:
        return {}
    out = {}
    for x in sections:
        w = mass.get(x, 0.0) / total * len(sections)
        out[x] = round(min(TOP, max(FLOOR, w)) / STEP) * STEP
    return out


def _validator(ids: set[int], sections: list[str]):
    names = {x.lower(): x for x in sections}

    def check(v):
        if not isinstance(v, dict) or not isinstance(v.get("topics"), dict):
            raise ValueError('expected {"topics": {...}, "new": ...}')
        filed = {}
        for k, sec in v["topics"].items():
            try:
                tid = int(k)
            except (TypeError, ValueError):
                continue
            if tid in ids and isinstance(sec, str) and sec.strip().lower() in names:
                filed[tid] = names[sec.strip().lower()]
        new = v.get("new")
        if isinstance(new, dict) and isinstance(new.get("name"), str) and new["name"].strip():
            kws = [str(x).strip().lower()[:60] for x in new.get("keywords") or [] if str(x).strip()][:12]
            tids = [int(x) for x in new.get("topics") or [] if str(x).lstrip("-").isdigit() and int(x) in ids]
            new = {"name": " ".join(new["name"].split())[:40], "keywords": kws, "topics": tids} if kws and tids else None
        else:
            new = None
        return {"topics": filed, "new": new}
    return check


def _prompt(body: dict, cfg: dict, topics: list[dict]) -> str:
    from .autopilot import brief_lines, sections_of

    lines = [f"Site: {body.get('name')} ({body.get('domain')}), language: {body.get('language')}",
             f"Sector: {(body.get('sector') or {}).get('value') or 'not determined'}",
             *brief_lines(body),
             f"Sections of the site: {', '.join(sections_of(cfg))}",
             "Topics of the week:"]
    for o in topics:
        lines.append(f"[{o['id']}] {o['label']}" + (f": {o['summary'][:200]}" if o["summary"] else "")
                     + f" ({len(o['days'])} days in the trend)")
    return "\n".join(lines)


def _edit_line(s, site_id: int, change) -> bool:
    """Change the profile in use, and the version waiting for approval, in
    place: a rubric is a setting of the newsroom, not a new line."""
    p = m.editorial_profiles
    rows = s.execute(select(p.c.id, p.c.body).where(p.c.site_id == site_id, p.c.status.in_(("approved", "draft")))
                     .order_by(p.c.id).with_for_update()).all()
    for r in rows:
        body = copy.deepcopy(dict(r.body))
        body.setdefault("settings", {})
        change(body)
        s.execute(update(p).where(p.c.id == r.id).values(body=body))
    return bool(rows)


def _store(s, site_id: int, rb: dict, topic: dict | None = None, drop: str | None = None) -> bool:
    def change(body):
        body["settings"]["rubrics"] = rb
        subs = list(body.get("subtopics") or [])
        if drop:
            subs = [x for x in subs if not (x.get("by") == "rubrics" and x.get("name", "").lower() == drop.lower())]
        if topic and not any(x.get("name", "").lower() == topic["name"].lower() for x in subs):
            subs.append(dict(topic))
        body["subtopics"] = subs
    return _edit_line(s, site_id, change)


def review(db, tenant_id: str, site_id: int, core: CoreClient | None, now: datetime | None = None) -> dict | None:
    """This week's rubrics of a site: the weights and, at most, one rubric
    more. Returns the change logged, None when nothing was decided."""
    from .autopilot import avoided, sections_of, settings_of

    now = now or datetime.now(UTC)
    week = monday(now)
    with db.tenant(tenant_id) as s:
        ten = s.execute(select(m.tenants.c.autopilot)).first()
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
        if body is None or core is None or (ten is not None and not ten.autopilot) or not is_on(body):
            return None
        cfg = settings_of(body)
        rb = state(body)
        if rb["week"] == week or not cfg["sections"]:
            return None
        topics = week_topics(s, now)
    if not topics:
        return None
    sections = sections_of(cfg)
    try:
        with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"rubrics:{week}"):
            value, _ = core.chat_json(SYSTEM, _prompt(body, cfg, topics),
                                      _validator({o["id"] for o in topics}, sections))
    except CoreError as e:
        log.warning("rubrics of site %s failed: %s", site_id, e)
        return None
    by_id = {o["id"]: o for o in topics}
    mass: dict[str, float] = defaultdict(float)
    for tid, sec in value["topics"].items():
        mass[sec] += by_id[tid]["mass"]
    before = {"weights": dict(rb["weights"]), "added": copy.deepcopy(rb["added"])}
    weights = weigh(mass, sections) if len(value["topics"]) >= MIN_TOPICS else dict(rb["weights"])
    changes = [{"section": x, "from": rb["weights"].get(x, 1.0), "to": w}
               for x, w in weights.items() if w != rb["weights"].get(x, 1.0)]
    added = None
    new = value["new"]
    if new and len(rb["added"]) < MAX_ADDED:
        days = set().union(*(by_id[t]["days"] for t in new["topics"] if t not in value["topics"]))
        taken = {x.lower() for x in sections} | {x.lower() for x in rb["refused"]}
        if (len(days) >= MIN_DAYS and new["name"].lower() not in taken
                and not avoided(" ".join([new["name"], *new["keywords"]]), cfg["avoid"])):
            added = {"name": new["name"], "keywords": new["keywords"], "at": now.isoformat(),
                     "days": len(days), "topics": [by_id[t]["label"] for t in new["topics"]][:5]}
    entry = {"at": now.isoformat(), "week": week, "changes": changes, "added": added["name"] if added else None,
             "added_topics": added["topics"] if added else [], "before": before}
    rb = {**rb, "weights": weights, "week": week,
          "added": rb["added"] + ([{k: added[k] for k in ("name", "keywords", "at", "days")}] if added else []),
          "log": ([entry] if changes or added else []) + rb["log"][:LOG - 1]}
    topic = {"name": added["name"], "keywords": added["keywords"], "share": 0.1, "by": "rubrics"} if added else None
    with db.tenant(tenant_id) as s:
        _store(s, site_id, rb, topic)
    return entry if changes or added else None


def _current(s, site_id: int):
    return s.execute(select(m.editorial_profiles.c.body).where(
        m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()


def undo(s, site_id: int) -> bool:
    """Back to the rubrics before the newsroom's last change; a rubric it
    added then leaves, and is not offered again."""
    body = _current(s, site_id)
    rb = state(body)
    if not rb["log"]:
        return False
    last, *rest = rb["log"]
    gone = last.get("added")
    rb = {**rb, "weights": dict(last["before"]["weights"]), "added": list(last["before"]["added"]), "log": rest,
          "refused": rb["refused"] + ([gone] if gone and gone not in rb["refused"] else [])}
    return _store(s, site_id, rb, drop=gone)


def take_out(s, site_id: int, name: str) -> bool:
    """The owner takes out a rubric the newsroom added: it is not offered
    again. The owner's own rubrics are not touched here."""
    body = _current(s, site_id)
    rb = state(body)
    hit = next((x for x in rb["added"] if x.get("name", "").lower() == name.strip().lower()), None)
    if hit is None:
        return False
    rb = {**rb, "added": [x for x in rb["added"] if x is not hit],
          "weights": {k: v for k, v in rb["weights"].items() if k.lower() != hit["name"].lower()},
          "refused": rb["refused"] + [hit["name"]]}
    return _store(s, site_id, rb, drop=hit["name"])


def set_enabled(s, site_id: int, on: bool) -> bool:
    """Switched off, the rubrics weigh the same again and the added ones
    rest (their topic stays in the line); switched on, they come back."""
    body = _current(s, site_id)
    if body is None:
        return False
    return _store(s, site_id, {**state(body), "enabled": bool(on)})


def panel(body: dict | None) -> dict:
    """What the Automatic newsroom tab shows."""
    from .autopilot import settings_of

    cfg, rb = settings_of(body), state(body)
    own = cfg.get("sections") or []
    rows = [{"name": x, "weight": rb["weights"].get(x, 1.0), "added": False} for x in own]
    rows += [{"name": x["name"], "weight": rb["weights"].get(x["name"], 1.0), "added": True, "at": x.get("at"),
              "days": x.get("days")} for x in rb["added"] if x.get("name", "").lower() not in {y.lower() for y in own}]
    return {"on": is_on(body), "set": rb["enabled"] is not None, "rows": rows, "last": rb["log"][0] if rb["log"] else None,
            "week": rb["week"], "full": len(rb["added"]) >= MAX_ADDED}


def since(body: dict | None, start: datetime) -> list[dict]:
    """The newsroom's changes to the rubrics from `start`, for the report."""
    out = []
    for e in state(body)["log"]:
        try:
            at = datetime.fromisoformat(e["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if at >= start:
            out.append(e)
    return out
