import json
from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import text

from editor import opportunity as opp
from editor import profile as prof
from editor import proposals as props
from editor import models as m
from editor import repo
from editor.collectors import Item
from editor.core_client import CoreClient
from editor.dedup import minhash
from editor.normalize import url_hash
from editor.scoring import score_topics
from editor.topics import detect_topics
from conftest import feed

NOW = datetime.now(UTC)
PROFILE = {
    "domain": "blog.example", "name": "Blog Rete", "language": "it",
    "sector": {"value": "Connettività", "evidence": []},
    "subtopics": [
        {"name": "Fibra", "keywords": ["fibra", "ftth", "open fiber"], "share": 0.5, "evidence": []},
        {"name": "Satellite", "keywords": ["starlink", "satellite"], "share": 0.2, "evidence": []},
    ],
    "style": {"tone": "divulgativo", "formats": ["news", "guide"]},
    "settings": {"proposals_per_day": 2, "min_relevance": 0.15, "exclude": ["calcio"]},
}


def setup_site(db, tenant):
    with db.tenant(tenant) as s:
        site_id = repo.ensure_site(s, tenant, "blog.example", "Blog Rete")
        prof.save_draft(s, tenant, site_id, PROFILE, "manual")
        prof.approve(s, site_id, 1, "francesco")
        sid = repo.upsert_source(s, tenant, kind="rss", name="news", url="https://news.example/feed", site_id=site_id)
        s.execute(text("UPDATE editor.sources SET score = 0.55 WHERE id = :i"), {"i": sid})
        # The same story from a second source: one source alone is not a story.
        other = repo.upsert_source(s, tenant, kind="rss", name="rete", url="https://rete.example/feed", site_id=site_id)
        s.execute(text("UPDATE editor.sources SET score = 0.55 WHERE id = :i"), {"i": other})
    feed(db, tenant, sid, [
        Item(url="https://news.example/1", title="Open Fiber completa la fibra FTTH a Bari", published_at=NOW - timedelta(hours=3)),
        Item(url="https://news.example/3", title="Starlink abbassa i prezzi del satellite in Italia", published_at=NOW - timedelta(hours=8)),
        Item(url="https://news.example/4", title="Il calcio in streaming soffre la latenza", published_at=NOW - timedelta(hours=2)),
        Item(url="https://news.example/5", title="Nuova ricetta della pizza napoletana premiata", published_at=NOW - timedelta(hours=1)),
    ])
    feed(db, tenant, other, [
        Item(url="https://rete.example/2", title="Fibra FTTH a Bari: Open Fiber chiude i lavori", published_at=NOW - timedelta(hours=5)),
    ])
    detect_topics(db, tenant)
    score_topics(db, tenant)
    return site_id


def test_null_angle_and_why_now_become_empty():
    v = props._validator({7})({"title": "Fiber rollout accelerates in the south", "angle": None,
                               "why_now": None, "format": "news", "citations": [7]})
    assert v["angle"] == "" and v["why_now"] == ""


def test_opportunity_ranks_relevant_topics_and_skips_others(db, tenant):
    site_id = setup_site(db, tenant)
    ops = opp.score_site(db, tenant, site_id)
    with db.tenant(tenant) as s:
        keywords = {o.topic_id: s.execute(text("SELECT keywords FROM editor.topics WHERE id = :i"), {"i": o.topic_id}).scalar() for o in ops}
    assert len(ops) == 2  # fibra and starlink; pizza irrelevant, calcio excluded
    assert ops[0].matched_subtopic == "Fibra" and "bari" in keywords[ops[0].topic_id]
    assert ops[0].version == "opportunity-v1" and "feedback" in ops[0].missing
    assert set(ops[0].components) >= {"relevance", "hype", "freshness", "novelty", "source_quality"}


def test_site_history_lowers_novelty(db, tenant):
    site_id = setup_site(db, tenant)
    before = {o.matched_subtopic: o for o in opp.score_site(db, tenant, site_id)}
    with db.tenant(tenant) as s:
        title = "Open Fiber completa la fibra FTTH a Bari"
        s.execute(m.site_posts.insert().values(
            tenant_id=tenant, site_id=site_id, url="https://blog.example/bari", url_hash=url_hash("https://blog.example/bari"),
            title=title, minhash=minhash(title), published_at=datetime.now(UTC)))
    after = {o.matched_subtopic: o for o in opp.score_site(db, tenant, site_id)}
    assert after["Fibra"].components["novelty"] < before["Fibra"].components["novelty"]
    assert after["Fibra"].score < before["Fibra"].score
    assert after["Satellite"].components["novelty"] == before["Satellite"].components["novelty"]


def test_proposals_cite_only_stored_items(db, tenant):
    site_id = setup_site(db, tenant)
    ops = opp.score_site(db, tenant, site_id)
    seen_prompts = []

    def answer(request):
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        seen_prompts.append(prompt)
        ids = [int(line[1:line.index("]")]) for line in prompt.splitlines() if line.startswith("[")]
        if len(body["messages"]) == 2:  # first try cites an item that does not exist
            content = json.dumps({"title": "Una proposta", "citations": [999999]})
        else:
            content = json.dumps({"title": "Bari cablata in fibra: cosa cambia", "angle": "Guida per chi abita a Bari",
                                  "why_now": "Lavori chiusi questa settimana", "format": "guide", "citations": ids[:2]})
        return httpx.Response(200, json={"content": content, "tool_calls": [], "usage": {"input_tokens": 1, "output_tokens": 1}, "cost": 0.0})

    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        created = props.propose(db, tenant, site_id, ops, CoreClient("http://core.test", "tok"))
    assert len(created) == 1  # fibra; starlink rests on a single item, not yet a story
    with db.tenant(tenant) as s:
        rows = s.execute(text(
            "SELECT p.id, p.title, p.format, p.generated_by, count(c.item_id) AS n FROM editor.proposals p "
            "JOIN editor.proposal_citations c ON c.proposal_id = p.id GROUP BY p.id ORDER BY p.id")).all()
        orphans = s.execute(text(
            "SELECT count(*) FROM editor.proposal_citations c LEFT JOIN editor.source_items i ON i.id = c.item_id WHERE i.id IS NULL")).scalar()
    assert rows[0].title == "Bari cablata in fibra: cosa cambia" and rows[0].format == "guide" and rows[0].n >= 1
    assert all(r.generated_by == "model" for r in rows) and orphans == 0
    assert "Tone: divulgativo" in seen_prompts[0]

    # Proposed topics are not proposed again for a while.
    again = opp.score_site(db, tenant, site_id)
    assert all(o.components["not_repeated"] == 0.0 for o in again if o.matched_subtopic == "Fibra")
    assert props.propose(db, tenant, site_id, again, None) == []

    # A decision is recorded as feedback and counted next time.
    assert props.decide(db, tenant, rows[0].id, False, "francesco", "already covered")
    assert not props.decide(db, tenant, rows[0].id, True, "francesco")
    later = opp.score_site(db, tenant, site_id)
    fibra = next(o for o in later if o.matched_subtopic == "Fibra")
    assert fibra.components["feedback"] < 0.5


def test_citation_to_missing_item_is_impossible(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        topic = s.execute(text("SELECT id FROM editor.topics LIMIT 1")).scalar()
        pid = s.execute(text(
            "INSERT INTO editor.proposals (tenant_id, site_id, topic_id, profile_id, opportunity, title, generated_by) "
            "VALUES (:t, :s, :topic, (SELECT id FROM editor.editorial_profiles LIMIT 1), 50, 'x', 'test') RETURNING id"),
            {"t": tenant, "s": site_id, "topic": topic}).scalar()
    try:
        with db.tenant(tenant) as s:
            s.execute(text("INSERT INTO editor.proposal_citations VALUES (:t, :p, 987654321)"), {"t": tenant, "p": pid})
    except Exception as e:
        assert "foreign key" in str(e).lower()
    else:
        raise AssertionError("a citation to a missing item was accepted")


def test_one_source_alone_is_not_a_story(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(repo.archive_of(db, tenant)) as s:
        # Both items about Bari now come from the same source.
        s.execute(text("UPDATE editor.source_items SET source_id = (SELECT id FROM editor.sources "
                       "WHERE name = 'news' AND shared)"))
    ops = opp.score_site(db, tenant, site_id, now=NOW)
    assert ops and props.propose(db, tenant, site_id, ops, None) == []


def test_one_strong_item_from_a_reliable_source_is_a_story(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.sources SET score = 0.8 WHERE name = 'news'"))
    ops = opp.score_site(db, tenant, site_id, now=NOW)
    created = props.propose(db, tenant, site_id, ops, None)
    with db.tenant(tenant) as s:
        titles = list(s.execute(text("SELECT title FROM editor.proposals")).scalars())
    assert len(created) == 2 and any("Starlink" in t or "satellite" in t.lower() for t in titles)


def test_proposals_now_stop_at_the_day_number(db, tenant):
    site_id = setup_site(db, tenant)
    since = NOW - timedelta(hours=12)
    assert len(props.top_up(db, tenant, site_id, since, now=NOW)) == 1
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.sources SET score = 0.8"))
    # PROFILE asks for 2 a day: one is left.
    assert len(props.top_up(db, tenant, site_id, since, now=NOW)) == 1
    assert props.top_up(db, tenant, site_id, since, now=NOW) == []
