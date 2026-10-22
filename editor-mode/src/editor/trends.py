"""The trend of the day. Eight times a day, from 07:00 every three hours,
the archive keeps the topics that rise most with their latest news: the
day's snapshots catalogue what the sector talked about, and the newest is
the trend of the moment each newsroom is offered, with titles to choose
from."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select

from . import models as m

# 07:00 first, then every three hours.
HOURS = (7, 10, 13, 16, 19, 22, 1, 4)
TOPICS = 20
WINDOW = timedelta(hours=48)


def slot_start(now: datetime, tz: str) -> datetime:
    """The start, in UTC, of the snapshot slot `now` falls in."""
    local = now.astimezone(ZoneInfo(tz))
    for back in range(0, 24):
        t = (local - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        if t.hour in HOURS:
            return t.astimezone(UTC)
    raise AssertionError("no slot in a day")


def due(s, now: datetime, tz: str) -> bool:
    last = s.execute(select(func.max(m.trend_snapshots.c.taken_at))).scalar()
    return last is None or last < slot_start(now, tz)


def snapshot(s, archive: str, now: datetime | None = None) -> int:
    """Keep the topics of the archive rising most right now, by Hype Score."""
    now = now or datetime.now(UTC)
    ts = m.trend_scores
    latest = (select(ts.c.topic_id, func.max(ts.c.computed_at).label("at"))
              .group_by(ts.c.topic_id).subquery())
    rows = s.execute(
        select(m.topics.c.id, m.topics.c.label, m.topics.c.summary, ts.c.score)
        .join(latest, latest.c.topic_id == m.topics.c.id)
        .join(ts, (ts.c.topic_id == latest.c.topic_id) & (ts.c.computed_at == latest.c.at))
        .where(m.topics.c.tenant_id == archive, m.topics.c.updated_at >= now - WINDOW)
        .order_by(ts.c.score.desc(), m.topics.c.id.desc())
        .limit(TOPICS)
    ).all()
    out = []
    for r in rows:
        si = m.source_items
        news = s.execute(
            select(si.c.id, si.c.title, si.c.url, si.c.source_id)
            .join(m.topic_items, m.topic_items.c.item_id == si.c.id)
            .where(m.topic_items.c.topic_id == r.id, si.c.duplicate_of.is_(None))
            .order_by(si.c.published_at.desc().nulls_last(), si.c.id.desc())
        ).all()
        out.append({"topic_id": r.id, "label": r.label, "summary": r.summary or "", "hype": round(r.score, 1),
                    "items": len(news), "sources": len({n.source_id for n in news}),
                    "news": [{"id": n.id, "title": n.title, "url": n.url} for n in news[:3]]})
    return s.execute(insert(m.trend_snapshots).values(tenant_id=archive, taken_at=now, topics=out)
                     .returning(m.trend_snapshots.c.id)).scalar_one()


def today(s, archive: str, start: datetime) -> list:
    """The day's snapshots (from `start`, the newsroom's midnight), newest first."""
    t = m.trend_snapshots
    return s.execute(select(t).where(t.c.tenant_id == archive, t.c.taken_at >= start)
                     .order_by(t.c.taken_at.desc())).all()


def latest(s, archive: str):
    t = m.trend_snapshots
    return s.execute(select(t).where(t.c.tenant_id == archive).order_by(t.c.taken_at.desc()).limit(1)).first()
