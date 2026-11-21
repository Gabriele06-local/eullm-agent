"""Proposals during the day for a site its director runs (Francesco
2026-11-10): after the morning, a round every few hours for the places of
the day still free, none once the day's articles are written; and no
outlet takes more than its share of the day."""

from collections import namedtuple
from datetime import datetime, timedelta

from sqlalchemy import select, update

from editor import evergreen, picks
from editor import models as m
from editor import proposals as props

from test_proposals import NOW, setup_site


def test_the_rounds_come_every_three_hours_after_the_morning():
    day = datetime(2026, 11, 10)
    due = [h for h in range(24) if props.round_due(day.replace(hour=h), 7, None)]
    assert due == [10, 13, 16, 19]
    assert props.round_due(day.replace(hour=13, minute=40), 7, "2026-11-10T13") is None  # once per round
    assert props.round_due(day.replace(hour=13), 7, "2026-11-10T10") == "2026-11-10T13"


def test_a_round_fills_only_the_free_places_and_stops_at_the_days_articles(db, tenant, monkeypatch):
    site_id = setup_site(db, tenant)  # 2 articles a day
    since = NOW - timedelta(hours=12)
    asked = []
    real = props.plan

    def plan(*a, limit=None, **k):
        asked.append(limit)
        return real(*a, limit=limit, **k)

    monkeypatch.setattr(props, "plan", plan)
    made = props.assisted_round(db, tenant, site_id, since, now=NOW)
    assert asked == [2] and len(made) == 2
    # Both still waiting and fresh: no place is free.
    assert props.assisted_round(db, tenant, site_id, since, now=NOW) == [] and asked == [2]
    # Waiting for hours, they leave their places to new ones (and stay to decide).
    with db.tenant(tenant) as s:
        s.execute(update(m.proposals).values(created_at=NOW - timedelta(hours=props.FRESH_HOURS + 1)))
    props.assisted_round(db, tenant, site_id, since, now=NOW)
    assert asked[-1] == 2
    with db.tenant(tenant) as s:
        assert s.execute(select(m.proposals.c.status)).scalars().all().count("proposed") >= 2
    # One article written today: one place left; two written: the day is done.
    monkeypatch.setattr(props, "written_since", lambda s, site, since: 1)
    props.assisted_round(db, tenant, site_id, since, now=NOW)
    assert asked[-1] == 1
    monkeypatch.setattr(props, "written_since", lambda s, site, since: 2)
    n = len(asked)
    assert props.assisted_round(db, tenant, site_id, since, now=NOW) == [] and len(asked) == n


def test_an_outlet_keeps_its_share_over_the_whole_day(db, tenant):
    site_id = setup_site(db, tenant)
    # Without a cap, Starlink from «news» is the single news item to propose.
    with db.tenant(tenant) as s:
        assert props.outlets_used(s, site_id, NOW - timedelta(days=1)) == {}
    ids = picks.fill(db, tenant, site_id, 2, now=NOW, used={"news": 2}, day_n=6)
    with db.tenant(tenant) as s:
        outlets = s.execute(select(m.sources.c.name).distinct()
                            .join(m.source_items, m.source_items.c.source_id == m.sources.c.id)
                            .join(m.proposal_citations, m.proposal_citations.c.item_id == m.source_items.c.id)
                            .where(m.proposal_citations.c.proposal_id.in_(ids or [0]))).scalars().all()
    assert "news" not in outlets
    # Proposals made today count each outlet they cite.
    made = picks.fill(db, tenant, site_id, 2, now=NOW)
    with db.tenant(tenant) as s:
        assert made and props.outlets_used(s, site_id, NOW - timedelta(days=1)).get("news", 0) >= 1


def test_outlets_used_counts_the_site_source_name(db, tenant):
    site_id = setup_site(db, tenant)
    picks.fill(db, tenant, site_id, 2, now=NOW)
    with db.tenant(tenant) as s:
        assert props.outlets_used(s, site_id, NOW - timedelta(days=1)).get("news", 0) >= 1
        s.execute(update(m.sources).where(m.sources.c.name == "news", m.sources.c.shared.is_(False))
                  .values(name="Fonti principali"))
        outlets = props.outlets_used(s, site_id, NOW - timedelta(days=1))
    assert outlets.get("Fonti principali", 0) >= 1 and "news" not in outlets


def test_a_theme_is_not_written_from_one_blog_alone():
    It = namedtuple("It", "id source_id")
    items = [It(i, 1) for i in range(1, 8)] + [It(8, 2), It(9, 3)]
    kept = evergreen.spread_items(items)
    assert [x.id for x in kept] == [1, 2, 3, 8, 9]
    check = evergreen._validator({x.id for x in kept}, {x.id: x.source_id for x in kept})
    base = {"title": "Zucca al forno", "angle": "", "why_now": "", "format": "recipe", "country": "", "section": ""}
    try:
        check(base | {"citations": [1, 2]})
        raise AssertionError("two items of the same blog passed")
    except ValueError as e:
        assert "two different outlets" in str(e)
    assert check(base | {"citations": [1, 8]})["citations"] == [1, 8]
