import json
from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import select, text

from editor import opportunity as opp
from editor import profile as prof
from editor import picks
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
    # PROFILE asks for 2 a day: the Bari story, and Starlink as a single news item.
    ids = props.top_up(db, tenant, site_id, since, now=NOW)
    assert len(ids) == 2
    with db.tenant(tenant) as s:
        rows = s.execute(select(m.proposals.c.title, m.proposals.c.basis).order_by(m.proposals.c.id)).all()
    assert rows[0].basis == {"kind": "topic"}
    assert "Starlink" in rows[1].title and rows[1].basis["kind"] == "news"
    assert rows[1].basis["picked_by"] == "formula" and rows[1].basis["matched"] == "Satellite"
    assert props.top_up(db, tenant, site_id, since, now=NOW) == []
    # Asked by a person: the pizza is far from every subtopic and the calcio
    # excluded, so without the model there is nothing more to propose.
    assert props.top_up(db, tenant, site_id, since, now=NOW, manual=True) == []
    assert picks.why_none(db, tenant, site_id, now=NOW) == "all_proposed"


def test_an_italian_profile_meets_english_news(db, tenant):
    with db.tenant(tenant) as s:
        site_id = repo.ensure_site(s, tenant, "ai.example", "AI Blog")
        prof.save_draft(s, tenant, site_id, {
            **PROFILE, "domain": "ai.example",
            "sector": {"value": "intelligenza artificiale", "evidence": []},
            "subtopics": [{"name": "inferenza locale", "keywords": ["inferenza", "quantizzazione"], "share": 0.4,
                           "evidence": []}]}, "manual")
        prof.approve(s, site_id, 1, "francesco")
        sid = repo.upsert_source(s, tenant, kind="rss", name="hn", url="https://hn.example/feed", site_id=site_id)
    feed(db, tenant, sid, [
        Item(url="https://hn.example/1", title="A faster inference engine for quantized models",
             published_at=NOW - timedelta(hours=2)),
        Item(url="https://hn.example/2", title="Best pasta recipes of the year", published_at=NOW - timedelta(hours=1)),
    ])
    detect_topics(db, tenant)
    created = props.top_up(db, tenant, site_id, NOW - timedelta(hours=12), now=NOW)
    with db.tenant(tenant) as s:
        titles = list(s.execute(select(m.proposals.c.title)).scalars())
    assert len(created) == 1 and "inference" in titles[0]


def test_the_model_picks_the_news_in_one_call(db, tenant):
    site_id = setup_site(db, tenant)
    calls = []

    def answer(request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        calls.append(prompt)
        ids = {line[1:line.index("]")]: line for line in prompt.splitlines() if line.startswith("[")}
        pizza = next(int(i) for i, line in ids.items() if "pizza" in line)
        content = json.dumps({"options": [{"title": "Pizza e connessione: un caso", "angle": "Curiosità",
                                           "why_now": "Se ne parla", "format": "news", "citations": [pizza]}]})
        return httpx.Response(200, json={"content": content, "tool_calls": [],
                                         "usage": {"input_tokens": 1, "output_tokens": 1}, "cost": 0.0})

    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.editorial_profiles SET body = :b"),
                  {"b": json.dumps({**PROFILE, "settings": {**PROFILE["settings"], "proposals_per_day": 3}})})
    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        ids = picks.fill(db, tenant, site_id, 2, CoreClient("http://core.test", "tok"), now=NOW)
    with db.tenant(tenant) as s:
        rows = s.execute(select(m.proposals.c.title, m.proposals.c.basis, m.proposals.c.generated_by)
                         .order_by(m.proposals.c.id)).all()
    # One call; the model's pick first, then the formula's best for the second.
    assert len(calls) == 1 and "Open Fiber" in calls[0] and "calcio" not in calls[0]
    assert len(ids) == 2 and rows[0].title == "Pizza e connessione: un caso"
    assert rows[0].basis["picked_by"] == "model" and rows[0].generated_by == "model"
    assert rows[1].basis["picked_by"] == "formula"


def test_no_news_for_three_days_is_a_stopped_collection(db, tenant):
    site_id = setup_site(db, tenant)
    later = NOW + timedelta(days=4)
    assert props.top_up(db, tenant, site_id, later - timedelta(hours=12), now=later) == []
    assert picks.why_none(db, tenant, site_id, now=later) == "no_news"


def _one_proposal(db, tenant, site_id):
    def answer(request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        ids = [int(line[1:line.index("]")]) for line in prompt.splitlines() if line.startswith("[")]
        return httpx.Response(200, json={"content": json.dumps({
            "title": "Bari cablata in fibra: cosa cambia", "angle": "Guida", "why_now": "Lavori chiusi",
            "format": "guide", "citations": ids[:2]}), "tool_calls": [], "usage": {"input_tokens": 1, "output_tokens": 1}, "cost": 0.0})

    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        return props.propose(db, tenant, site_id, opp.score_site(db, tenant, site_id), CoreClient("http://core.test", "tok"))


def test_a_discard_reason_teaches_the_next_proposals(db, tenant):
    site_id = setup_site(db, tenant)
    [pid] = _one_proposal(db, tenant, site_id)
    with db.tenant(tenant) as s:
        before = dict(s.execute(select(m.sources.c.id, m.sources.c.score)).all())
    assert props.decide(db, tenant, pid, False, "francesco", reason="unreliable_source")
    with db.tenant(tenant) as s:
        verdict = s.execute(select(m.feedback.c.verdict).where(m.feedback.c.target_id == pid)).scalar()
        rows = s.execute(select(m.sources.c.id, m.sources.c.score, m.sources.c.evidence)).all()
        assert verdict == "unreliable_source"
        lowered = [r for r in rows if (r.evidence or {}).get("distrusted")]
        assert lowered and all(r.evidence["distrusted"] == 1 and r.score < before[r.id] for r in lowered)
        assert props.discards(s, site_id, NOW - timedelta(days=30)) == [("unreliable_source", 1)]
        # An unknown reason is a plain discard; the lessons name what to avoid.
        s.execute(m.feedback.insert().values(tenant_id=tenant, site_id=site_id, target="proposal", target_id=pid,
                                             verdict="too_generic", given_by="francesco", given_at=datetime.now(UTC)))
        learned = props.lessons(s, site_id)
    assert learned and "too generic" in learned[0] and "Bari cablata in fibra" in learned[0]
    # A proposal the site already covered is better as an update; others are completed.
    from editor.drafts import _post_lines
    from editor.seo import merge_links
    with db.tenant(tenant) as s:
        for url, title in (("https://blog.example/bari", "Bari cablata in fibra: cosa cambia per chi abita"),
                           ("https://blog.example/sat", "Starlink in Italia: prezzi e copertura")):
            s.execute(m.site_posts.insert().values(tenant_id=tenant, site_id=site_id, url=url, url_hash=url_hash(url),
                                                   title=title, minhash=minhash(title), published_at=NOW))
        posts = opp.related_posts(s, site_id, ["Bari cablata in fibra: cosa cambia"])
    assert posts[0]["url"] == "https://blog.example/bari" and posts[0]["score"] >= opp.UPDATE_SIMILARITY
    assert "updated version" in _post_lines(posts)[0]
    assert "complete them" in _post_lines([{**posts[0], "score": 0.3}])[0]
    links = merge_links(posts, [{"title": "x", "url": "https://blog.example/bari"}, {"title": "y", "url": "https://blog.example/y"}])
    assert [x["url"] for x in links].count("https://blog.example/bari") == 1
