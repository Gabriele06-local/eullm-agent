"""The same event told by another outlet in another language: found by
names and numbers, confirmed by the model (Francesco 2026-11-05: every
proposal counted one outlet, a trial against Meta and Google included)."""

import json
from datetime import timedelta

import respx
from sqlalchemy import select

from editor import autopilot as ap, models as m, proposals as props, repo, same_event
from editor import opportunity as opp
from editor.collectors import Item
from editor.core_client import CoreClient
from test_drafts import chat
from test_proposals import NOW, feed, setup_site


def test_names_and_numbers_stay_the_same_across_languages():
    it = same_event.entities("Dipendenza dai social, la causa contro Meta e Google arriva a processo: 1.200 famiglie")
    en = same_event.entities("Meta and Google face first jury trial over social media addiction, 1,200 families")
    assert {"meta", "google", "1200"} <= it & en


def test_a_possessive_name_is_the_same_entity():
    assert "starlink" in same_event.entities("Starlink's satellites cut prices")
    assert "starlink" in same_event.entities("Starlink cuts prices")
    assert "starlink" in same_event.entities("Starlink’s satellites cut prices")


def test_a_report_in_another_language_counts_as_a_second_outlet(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        bbc = repo.upsert_source(s, tenant, kind="rss", name="BBC", url="https://bbc.example/feed", site_id=site_id)
    feed(db, tenant, bbc, [Item(url="https://bbc.example/starlink",
                                title="Starlink cuts satellite prices for customers in Italy, Musk says",
                                published_at=NOW - timedelta(hours=6))])
    pid = props.propose(db, tenant, site_id, opp.score_site(db, tenant, site_id), None, limit=5)
    with db.tenant(tenant) as s:
        star = s.execute(select(m.proposals.c.id).where(m.proposals.c.title.ilike("%starlink%"))).scalar()
        if star is None:  # made from the single news, as the planner does
            star = pid[0]
            item = s.execute(select(m.source_items.c.id).where(m.source_items.c.url == "https://news.example/3")).scalar()
            s.execute(m.proposal_citations.delete().where(m.proposal_citations.c.proposal_id == star))
            s.execute(m.proposal_citations.insert().values(tenant_id=tenant, proposal_id=star, item_id=item))
            s.execute(m.proposals.update().where(m.proposals.c.id == star).values(
                title="Starlink abbassa i prezzi del satellite in Italia"))
        before = ap.source_count(s, star)
        english = s.execute(select(m.source_items.c.id).where(m.source_items.c.url == "https://bbc.example/starlink")).scalar()
    asked = []

    def answer(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        asked.append(prompt)
        n = next(line.split(".")[0] for line in prompt.splitlines() if "Starlink abbassa" in line)
        return chat(json.dumps({"same": {n: [english]}}))

    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        same_event.confirm(db, tenant, site_id, CoreClient("http://core.test", "tok"), NOW)
        again = same_event.confirm(db, tenant, site_id, CoreClient("http://core.test", "tok"), NOW)
    assert len(asked) == 1 and again == 0  # each proposal is checked once
    assert f"[{english}] Starlink cuts satellite prices" in asked[0]
    with db.tenant(tenant) as s:
        basis = s.execute(select(m.proposals.c.basis).where(m.proposals.c.id == star)).scalar()
        assert basis["event_checked"] and english in basis["same_event"]
        assert ap.source_count(s, star) == before + 1
