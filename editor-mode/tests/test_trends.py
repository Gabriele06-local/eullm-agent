"""The trend of the day: snapshots from 07:00 every three hours, and the
trend of the moment offered to a site with titles to choose from."""

import json
from datetime import UTC, datetime

import respx
from sqlalchemy import text

from editor import opportunity as opp
from editor import proposals as props
from editor import repo, trends
from editor.core_client import CoreClient

from test_drafts import chat
from test_proposals import setup_site
from test_ui import logged_in


def test_slots_start_at_seven_every_three_hours():
    rome = "Europe/Rome"
    # 06:30 in Rome is still the 04:00 slot; 07:05 is the 07:00 one.
    assert trends.slot_start(datetime(2026, 10, 22, 4, 30, tzinfo=UTC), rome) == datetime(2026, 10, 22, 2, tzinfo=UTC)
    assert trends.slot_start(datetime(2026, 10, 22, 5, 5, tzinfo=UTC), rome) == datetime(2026, 10, 22, 5, tzinfo=UTC)
    # 00:30 belongs to the 22:00 slot of the day before.
    assert trends.slot_start(datetime(2026, 10, 21, 22, 30, tzinfo=UTC), rome) == datetime(2026, 10, 21, 20, tzinfo=UTC)


def test_a_snapshot_keeps_the_rising_topics_once_a_slot(db, tenant):
    setup_site(db, tenant)
    archive = repo.archive_of(db, tenant)
    now = datetime.now(UTC)
    with db.tenant(archive) as s:
        assert trends.due(s, now, "Europe/Rome")
        trends.snapshot(s, archive, now)
        assert not trends.due(s, now, "Europe/Rome")
        snap = trends.latest(s, archive)
    labels = [x["label"] for x in snap.topics]
    assert labels and all(x["news"] and x["hype"] >= 0 for x in snap.topics)
    bari = next(x for x in snap.topics if x["sources"] == 2)
    assert bari["items"] == 2 and {n["title"] for n in bari["news"]} >= {"Open Fiber completa la fibra FTTH a Bari"}


def test_the_moment_offers_titles_to_choose_from(db, tenant):
    site_id = setup_site(db, tenant)
    opp.score_site(db, tenant, site_id)
    archive = repo.archive_of(db, tenant)
    with db.tenant(archive) as s:
        trends.snapshot(s, archive)
        snap = trends.latest(s, archive)
    with db.tenant(tenant) as s:
        cited = [r[0] for r in s.execute(text(
            "SELECT si.id FROM editor.source_items si JOIN editor.topic_items ti ON ti.item_id = si.id "
            "WHERE ti.topic_id = :t"), {"t": snap.topics[0]["topic_id"]})]
    topic = snap.topics[0]["topic_id"]
    answer = json.dumps({"options": [
        {"title": "Bari tutta in fibra: cosa cambia per chi ci vive", "angle": "Cosa offre la rete nuova.",
         "why_now": "Lavori finiti ora.", "format": "news", "citations": cited[:1]},
        {"title": "Bari tutta in fibra: cosa cambia per chi ci vive", "angle": "Doppione.",
         "why_now": "", "format": "news", "citations": cited[:1]},
        {"title": "FTTH a Bari: come verificare la copertura di casa", "angle": "Guida pratica.",
         "why_now": "", "format": "guide", "citations": cited},
    ]})
    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(answer))
        ids = props.propose_titles(db, tenant, site_id, topic, CoreClient("http://core.test", "tok"))
    assert len(ids) == 2  # the same title twice is one option
    c = logged_in(db, tenant, "editor")
    page = c.get("/trends").text
    assert "Il trend del momento" in page and "quale vuoi che scriva?" in page
    assert "Bari tutta in fibra" in page and "come verificare la copertura" in page
    assert page.count("Scrivi questo") == 2
    r = c.post(f"/ui/trends/{topic}/titles", follow_redirects=False)
    assert r.status_code == 303
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT count(*) FROM editor.jobs WHERE kind = 'titles'")).scalar() == 1


def test_without_a_model_the_titles_are_the_latest_headlines(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(repo.archive_of(db, tenant)) as s:
        topic = s.execute(text("SELECT topic_id FROM editor.topic_items GROUP BY topic_id ORDER BY count(*) DESC LIMIT 1")).scalar()
    ids = props.propose_titles(db, tenant, site_id, topic, None)
    with db.tenant(tenant) as s:
        titles = list(s.execute(text("SELECT title FROM editor.proposals WHERE id = ANY(:i)" if not db.sqlite else
                                     f"SELECT title FROM editor.proposals WHERE id IN ({','.join(map(str, ids))})"),
                                {"i": ids} if not db.sqlite else {}).scalars())
    assert len(ids) == 2 and any("Bari" in t for t in titles)
