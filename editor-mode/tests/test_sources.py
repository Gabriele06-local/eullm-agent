import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import respx
from sqlalchemy import select, text, update

import fakesite
from conftest import fixture
from editor import profile as prof
from editor import models as m
from editor import repo
from editor import sources as src
from editor.collectors import Item
from editor.core_client import CoreClient

NOW = datetime.now(UTC)


def feed(titles, days_apart=1, author=True):
    items = "".join(
        f"<item><title>{t}</title><link>https://agcom.it/n/{i}</link>"
        f"<pubDate>{format_datetime(NOW - timedelta(days=i * days_apart))}</pubDate>"
        f"{'<author>a@agcom.it (Ufficio stampa)</author>' if author else ''}"
        f"<description>{t}. {'Approfondimento sulla rete in fibra e sulle frequenze 5G in Italia. ' * 5}</description></item>"
        for i, t in enumerate(titles)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>AGCOM</title>{items}</channel></rss>'


def test_rate_rewards_relevant_fresh_original_sources():
    kws = {"fibra", "ftth", "5g", "wifi"}
    good = [Item(url=f"u{i}", title=f"Fibra FTTH e 5G: aggiornamento {i}", summary="x" * 400, author="a",
                 published_at=NOW - timedelta(days=i)) for i in range(12)]
    off = [Item(url=f"o{i}", title=f"Ricette di cucina numero {i}", summary="x" * 400,
                published_at=NOW - timedelta(days=i)) for i in range(12)]
    stale = [Item(url=f"s{i}", title=f"Fibra FTTH {i}", summary="x" * 400,
                  published_at=NOW - timedelta(days=200 + i)) for i in range(12)]
    g, o, s_ = src.rate(good, kws), src.rate(off, kws), src.rate(stale, kws)
    assert g["status"] == "active" and g["components"]["relevance"] == 1.0
    assert o["status"] == "rejected"
    assert s_["score"] < g["score"] and s_["components"]["freshness"] < 0.01
    copies = src.rate(good, kws, known=[src.minhash(f"{i.title} {i.summary}") for i in good])
    assert copies["components"]["originality"] == 0.0
    assert src.rate([], kws)["status"] == "rejected"


def test_freshness_never_exceeds_one_for_future_dates():
    kws = {"fibra", "ftth"}
    future = [Item(url="f", title="Fibra FTTH futura", summary="x" * 400,
                   published_at=NOW + timedelta(days=30))]
    assert src.rate(future, kws)["components"]["freshness"] == 1.0


def test_api_candidates_follow_the_profile():
    body = {"subtopics": [{"name": "Sicurezza", "keywords": ["ransomware", "firewall"], "share": 0.5},
                          {"name": "Cloud", "keywords": ["cloud", "datacenter"], "share": 0.3}]}
    kinds = {(c.kind, c.config.get("query")) for c in src.api_candidates(body)}
    assert ("hackernews", "ransomware firewall") in kinds
    arxiv = next(c for c in src.api_candidates(body) if c.kind == "arxiv")
    assert "cat:cs.CR" in arxiv.config["query"] and "cat:cs.DC" in arxiv.config["query"]
    assert not any(c.kind == "huggingface" for c in src.api_candidates(body))


def test_discovery_and_maintenance(db, tenant):
    class Crawler:
        def analyse(self, domain):
            with respx.mock(assert_all_called=False) as r:
                fakesite.mount(r)
                with httpx.Client() as c:
                    from editor.site import SiteCrawler
                    return SiteCrawler(c).analyse(domain)

    site = prof.analyse_site(db, tenant, "blog.example", Crawler())
    # A draft profile is only a proposal: it never steers the sources.
    with respx.mock() as router, httpx.Client() as c:
        early = src.discover(db, tenant, site["site_id"], c, CoreClient("http://core.test", "tok"))
    assert early.candidates == 0 and "approve" in early.notes[0]
    with db.tenant(tenant) as s:
        version = s.execute(text("SELECT max(version) FROM editor.editorial_profiles WHERE site_id = :i"),
                            {"i": site["site_id"]}).scalar()
        assert prof.approve(s, site["site_id"], version, "francesco")
    with respx.mock(assert_all_called=False) as router:
        # A publication the blog cites, with a feed on topic.
        router.get("https://agcom.it/robots.txt").mock(return_value=httpx.Response(404))
        router.get("https://agcom.it/").mock(return_value=httpx.Response(
            200, text='<html><head><title>AGCOM</title><link rel="alternate" type="application/rss+xml" href="/rss"></head></html>'))
        router.get("https://agcom.it/rss").mock(return_value=httpx.Response(200, text=feed(
            [f"Fibra FTTH e Wi-Fi: relazione {i}" for i in range(10)])))
        # A cited site that forbids crawlers: never read.
        router.get("https://openfiber.it/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
        home_of_blocked = router.get("https://openfiber.it/").mock(return_value=httpx.Response(200, text="<html></html>"))
        # Model suggestions: one invented domain without any feed.
        router.get(url__regex=r"https://(wi-fi\.org|starlink\.com|mikrotik\.com|invented-news\.example)/.*").mock(
            return_value=httpx.Response(404))
        router.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(200, content=fixture("hn.json")))
        router.get("https://api.github.com/search/repositories").mock(return_value=httpx.Response(200, content=fixture("github.json")))
        router.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(200, content=fixture("arxiv.xml")))
        router.post("http://core.test/v1/llm/chat").mock(return_value=httpx.Response(200, json={
            "content": json.dumps({"search_terms": ["fiber broadband", "wifi router"],
                                   "domains": ["invented-news.example", "blog.example"]}),
            "tool_calls": [], "usage": None, "cost": None}))
        with httpx.Client() as c:
            rep = src.discover(db, tenant, site["site_id"], c, CoreClient("http://core.test", "tok"))
        assert not home_of_blocked.called

    with db.tenant(tenant) as s:
        c = m.sources.c
        rows = s.execute(select(c.kind, c.name, c.origin, c.status, c.score, c.evidence).where(repo.own())
                         .order_by(c.id)).all()
    by_origin = {(r.kind, r.origin): r for r in rows}
    agcom = by_origin[("rss", "site_outbound")]
    assert agcom.status == "active" and agcom.evidence["cited_by_site"] >= 1
    assert not any(r.origin == "model_suggestion" for r in rows)  # no feed, no source
    assert any(r.kind == "hackernews" and r.origin == "api_query" for r in rows)
    assert rep.candidates == len(rows) and rep.active >= 1
    assert all(r.score is not None for r in rows)

    # Maintenance: repeated failures suspend; a later re-evaluation reactivates.
    with db.tenant(tenant) as s:
        sid = s.execute(text("SELECT archive_id FROM editor.sources WHERE origin = 'site_outbound' "
                             "AND shared IS NOT TRUE")).scalar()
    with db.tenant(repo.archive_of(db, tenant)) as s:
        s.execute(text("UPDATE editor.sources SET consecutive_errors = :n WHERE id = :i"),
                  {"i": sid, "n": repo.ERROR_LIMIT})
    m1 = src.maintain(db, tenant)
    assert (sid, f"{repo.ERROR_LIMIT} failed fetches in a row") in m1.suspended
    later = NOW + timedelta(days=20)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://agcom.it/rss").mock(return_value=httpx.Response(200, text=feed(
            [f"Fibra FTTH e Wi-Fi: relazione {i}" for i in range(10)])))
        router.get(url__regex=r".*").mock(return_value=httpx.Response(404))
        with httpx.Client() as c:
            m2 = src.maintain(db, tenant, c, now=later)
    assert sid in m2.reactivated
    with db.tenant(tenant) as s:
        status, reason = s.execute(text("SELECT status, status_reason FROM editor.sources WHERE id = :i"), {"i": sid}).one()
    assert (status, reason) == ("active", "re-evaluated")

    # A source a person suspended stays suspended.
    with db.tenant(tenant) as s:
        fid = s.execute(text("SELECT id FROM editor.sources WHERE archive_id = :i"), {"i": sid}).scalar()
        repo.set_source_status(s, fid, "suspended", "set by francesco", by="francesco")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://agcom.it/rss").mock(return_value=httpx.Response(200, text=feed(
            [f"Fibra FTTH e Wi-Fi: relazione {i}" for i in range(10)])))
        with httpx.Client() as c:
            m3 = src.maintain(db, tenant, c, now=later + timedelta(days=30))
    assert fid not in m3.reactivated
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT status FROM editor.sources WHERE id = :i"), {"i": fid}).scalar() == "suspended"


def test_stale_source_is_suspended(db, tenant):
    from editor import repo
    with db.tenant(tenant) as s:
        sid = repo.upsert_source(s, tenant, kind="rss", name="old", url="https://old.example/feed")
        aid = s.execute(select(m.sources.c.archive_id).where(m.sources.c.id == sid)).scalar()
    # Silence is the source's, for everyone: it is suspended in the archive.
    with db.tenant(repo.archive_of(db, tenant)) as s:
        s.execute(update(m.sources).where(m.sources.c.id == aid).values(last_item_at=datetime.now(UTC) - timedelta(days=90)))
    rep = src.maintain(db, tenant)
    assert rep.suspended == [(aid, "nothing new for more than 45 days")]


def test_old_hacker_news_searches_follow_what_is_new(db, tenant):
    from editor import repo
    from editor.sources import HN_RECENT, HN_SEARCH, maintain
    from sqlalchemy import select, update
    from editor import models as m
    with db.tenant(tenant) as s:
        repo.ensure_tenant(s, tenant, tenant)
        sid = repo.upsert_source(s, tenant, kind="hackernews", name="Hacker News: eu ai", url=HN_SEARCH,
                                 config={"query": "eu ai"})
        s.execute(update(m.sources).where(m.sources.c.id == sid).values(
            origin="api_query", status="suspended", status_reason="nothing new for more than 45 days"))
    maintain(db, tenant)
    with db.tenant(tenant) as s:
        r = s.execute(select(m.sources).where(m.sources.c.id == sid)).one()
    assert r.url == HN_RECENT and r.status == "active"
