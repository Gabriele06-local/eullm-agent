"""Collection to topics to Hype Score on a real database, with recorded
HTTP responses and a fake Core."""

import json

import httpx
import respx
from sqlalchemy import text

from conftest import fixture
from editor import repo
from editor.core_client import CoreClient
from editor.core_fetch import core_http_client
from editor.ingest import collect_tenant
from editor.scoring import score_topics
from editor.topics import detect_topics

FEED = "https://telecom.example/feed"
FEED2 = "https://mirror.example/rss"
MIRROR = """<?xml version="1.0"?><rss version="2.0"><channel><title>m</title>
<item><title>Open Fiber accelera il cablaggio FTTH nelle aree bianche</title>
<link>https://telecom.example/2026/10/open-fiber-ftth</link>
<description>Il piano prevede 2 milioni di nuove unità immobiliari.</description></item>
<item><title>Open Fiber accelera il cablaggio FTTH nelle aree bianche</title>
<link>https://syndication.example/news/open-fiber</link>
<description>Il piano prevede 2 milioni di nuove unità immobiliari in Italia.</description></item>
</channel></rss>""".encode()


def add_sources(db, tenant):
    with db.tenant(tenant) as s:
        repo.upsert_source(s, tenant, kind="rss", name="telecom", url=FEED)
        repo.upsert_source(s, tenant, kind="rss", name="mirror", url=FEED2)
        repo.upsert_source(s, tenant, kind="hackernews", name="hn", url="https://hn.algolia.com/api/v1/search")


@respx.mock
def test_collect_dedups_and_observes(db, tenant):
    add_sources(db, tenant)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=fixture("rss.xml"), headers={"ETag": '"a"'}))
    respx.get(FEED2).mock(return_value=httpx.Response(200, content=MIRROR))
    respx.get("https://hn.algolia.com/api/v1/search").mock(return_value=httpx.Response(200, content=fixture("hn.json")))
    with httpx.Client() as c:
        report = collect_tenant(db, tenant, c)
    assert [r.error for r in report.sources] == [None, None, None]
    # rss: 2 items; mirror: same URL as the first (tracking params stripped) + a syndicated copy; hn: 2
    assert report.new == 5
    with db.tenant(tenant) as s:
        dups = s.execute(text("SELECT count(*) FROM editor.source_items WHERE duplicate_of IS NOT NULL")).scalar()
        obs = s.execute(text("SELECT count(*) FROM editor.observations")).scalar()
        etag = s.execute(text("SELECT etag FROM editor.sources WHERE url = :u"), {"u": FEED}).scalar()
    assert dups == 1 and obs == 2 and etag == '"a"'

    # Second run: the feed answers 304, HN items are updated and observed again.
    respx.get(FEED).mock(return_value=httpx.Response(304))
    with httpx.Client() as c:
        again = collect_tenant(db, tenant, c)
    assert again.new == 0 and again.sources[0].not_modified
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT count(*) FROM editor.observations")).scalar() == 4


@respx.mock
def test_failing_source_is_recorded_and_others_continue(db, tenant):
    add_sources(db, tenant)
    respx.get(FEED).mock(return_value=httpx.Response(500))
    respx.get(FEED2).mock(return_value=httpx.Response(200, content=MIRROR))
    respx.get("https://hn.algolia.com/api/v1/search").mock(side_effect=httpx.ConnectError("down"))
    with httpx.Client() as c:
        report = collect_tenant(db, tenant, c)
    assert report.sources[0].error and report.sources[2].error and report.sources[1].new == 2
    with db.tenant(tenant) as s:
        errors = s.execute(text("SELECT count(*) FROM editor.sources WHERE last_error IS NOT NULL")).scalar()
    assert errors == 2


@respx.mock
def test_the_cores_trouble_is_not_the_sources_fault(db, tenant):
    """Out of daily fetches or the Core down: the sources stay active and
    those an earlier version suspended for it go back to work."""
    add_sources(db, tenant)
    respx.post("http://core.test/v1/fetch").mock(
        return_value=httpx.Response(429, json={"error": "daily fetches limit reached (2000 of 2000)"}))
    archive = repo.archive_of(db, tenant)
    suspend = ("UPDATE editor.sources SET status = 'suspended', status_reason = '5 failed fetches in a row', "
               "last_error = :e WHERE name = :n AND shared")
    with db.tenant(archive) as s:
        s.execute(text(suspend), {"n": "telecom", "e": "ConnectError: Core fetch failed (502): Redirect without Location"})
        s.execute(text(suspend), {"n": "mirror", "e": "HTTPStatusError: 404 Not Found"})
    with db.tenant(tenant) as s:
        # A newsroom's source suspended by an earlier version for failed reads.
        s.execute(text("UPDATE editor.sources SET status = 'suspended', status_reason = '5 failed fetches in a row' "
                       "WHERE name = 'mirror' AND NOT shared"))
    with core_http_client("http://core.test", "tok") as c:
        for _ in range(6):
            report = collect_tenant(db, tenant, c)
    assert all(r.error.startswith("CoreTrouble") for r in report.sources) and len(report.sources) == 2
    with db.tenant(archive) as s:
        shared = {r.name: r for r in s.execute(text(
            "SELECT name, status, consecutive_errors FROM editor.sources WHERE shared"))}
    assert shared["telecom"].status == "active" and shared["telecom"].consecutive_errors == 0
    assert shared["hn"].status == "active" and shared["hn"].consecutive_errors == 0
    # A real failure of the site stays a failure, for everyone.
    assert shared["mirror"].status == "suspended"
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT status FROM editor.sources WHERE name = 'mirror' AND NOT shared")).scalar() == "active"


@respx.mock
def test_topics_labels_and_scores(db, tenant):
    add_sources(db, tenant)
    respx.get(FEED).mock(return_value=httpx.Response(200, content=fixture("rss.xml")))
    respx.get(FEED2).mock(return_value=httpx.Response(200, content=MIRROR))
    respx.get("https://hn.algolia.com/api/v1/search").mock(return_value=httpx.Response(200, content=fixture("hn.json")))
    with httpx.Client() as c:
        collect_tenant(db, tenant, c)

    answers = iter([
        "Sure! not json",  # first answer is invalid: the client asks again
        '```json\n{"label": "Open Fiber e FTTH nelle aree bianche", "summary": "Piano di cablaggio.", "keywords": ["ftth"]}\n```',
    ])

    def core_answer(request):
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer tok"
        assert body["messages"][0]["role"] == "system"
        content = next(answers, '{"label": "Altro argomento", "summary": "", "keywords": []}')
        return httpx.Response(200, json={"content": content, "tool_calls": [],
                                         "usage": {"input_tokens": 50, "output_tokens": 10}, "cost": 0.001})

    respx.post("http://core.test/v1/llm/chat").mock(side_effect=core_answer)
    core = CoreClient("http://core.test", "tok")
    report = detect_topics(db, tenant, core)
    assert report.assigned == 4  # 5 items, 1 duplicate
    assert report.label_errors == 0
    with db.tenant(tenant) as s:
        rows = s.execute(text(
            "SELECT t.label, t.labelled_by, count(*) FROM editor.topics t JOIN editor.topic_items ti ON ti.topic_id = t.id "
            "GROUP BY t.id ORDER BY count(*) DESC, t.id"
        )).all()
    # The labels are the archive's, paid once for every newsroom.
    with db.tenant(repo.archive_of(db, tenant)) as s:
        usage = s.execute(text("SELECT count(*), sum(cost) FROM editor.llm_usage")).one()
    assert rows[0][0] == "Open Fiber e FTTH nelle aree bianche" and rows[0][1] == "model"
    assert usage[0] == report.labelled + 1  # the retry is recorded too

    # Nothing new: a second pass assigns nothing.
    assert detect_topics(db, tenant).assigned == 0

    scores = score_topics(db, tenant)
    assert len(scores) == len(rows)
    hn_topic = [h for h in scores.values() if "community" in h.components]
    assert hn_topic and all("community" not in h.missing for h in hn_topic)
    with db.tenant(tenant) as s:
        stored = s.execute(text("SELECT formula_version, missing FROM editor.trend_scores LIMIT 1")).one()
    assert stored[0] == "hype-v1"


def test_topics_without_core_use_keyword_labels(db, tenant):
    from conftest import feed
    from editor.collectors import Item

    with db.tenant(tenant) as s:
        sid = repo.upsert_source(s, tenant, kind="rss", name="x", url="https://x.example/feed")
    feed(db, tenant, sid, [
        Item(url="https://x.example/1", title="Starlink lancia il servizio direct to cell in Italia"),
        Item(url="https://x.example/2", title="Direct to cell: Starlink arriva in Italia con TIM"),
    ])
    report = detect_topics(db, tenant, None)
    assert report.new_topics == 1 and report.assigned == 2
    with db.tenant(tenant) as s:
        label, by = s.execute(text("SELECT label, labelled_by FROM editor.topics")).one()
    assert by == "keywords" and "starlink" in label
