import base64

import httpx
import pytest
import respx

import fakesite
from editor.core_fetch import CoreTransport, FetchRefused
from editor.html_extract import parse_html, same_site
from editor.site import SiteCrawler


def crawl(router, **kw):
    with httpx.Client() as c:
        return SiteCrawler(c, **kw).analyse("blog.example")


def test_same_site_ignores_trailing_dot_and_empties():
    assert same_site("https://example.com./x", "example.com")
    assert same_site("https://sub.example.com./x", "example.com")
    assert same_site("https://example.com/x", "example.com.")
    assert not same_site("not a url", "")
    assert not same_site("https://a.it/x", "")
    assert same_site("https://example.com/x", "example.com")
    assert not same_site("https://other.com/x", "example.com")


def test_menu_div_does_not_swallow_following_text():
    body = '<div class="menu"><a href="/x">m</a></div><p>' + "parola " * 30 + "</p>"
    page = parse_html(body, "https://e.it/")
    assert len(page.paragraphs) == 1
    assert [l.in_nav for l in page.links if l.url == "https://e.it/x"] == [True]


def test_html_is_read_not_run():
    page = parse_html(fakesite.article_html("x", "Titolo", ["Fibra"], "a.it", fakesite.NOW), "https://blog.example/x/")
    assert page.lang == "it" and page.h1 == "Titolo" and page.section == "Fibra"
    assert not any("never run" in p for p in page.paragraphs)
    assert any(l.url == "https://a.it/report" for l in page.links)
    assert any(l.in_nav for l in page.links)


def test_complete_analysis():
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router)
        snap = crawl(router)
    assert snap.language == "it"
    assert snap.feeds == ["https://blog.example/feed/"]
    assert snap.sitemaps[0] == "https://blog.example/sitemap_index.xml"
    assert len(snap.posts) == 8 and snap.sitemap_urls == 8
    assert snap.categories["Fibra"] >= 3 and "Satellite" in snap.categories
    assert snap.posts_per_week and snap.posts_per_week > 0
    assert snap.median_words and snap.median_words > 50
    # outbound sources cited by the site, share buttons excluded
    assert "agcom.it" in snap.outbound_domains and "twitter.com" not in snap.outbound_domains
    assert "fibra" in snap.top_terms
    # 8 posts < 15 wanted: honest partial
    assert snap.status == "partial"
    assert any("8 articles read" in p for p in snap.problems)


def test_robots_is_obeyed():
    robots = "User-agent: *\nDisallow: /\n"
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router, robots=robots)
        snap = crawl(router)
        assert not router.routes[1].called  # home page never requested
    assert snap.status == "failed"
    assert "robots.txt forbids crawling the home page" in snap.problems


def test_robots_server_error_means_no_crawl():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://blog.example/robots.txt").mock(return_value=httpx.Response(503))
        snap = crawl(router)
    assert snap.status == "failed" and "503" in snap.problems[0]


def test_an_unreachable_robots_says_why():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://blog.example/robots.txt").mock(
            side_effect=httpx.ConnectError("Core fetch failed (502): dns error"))
        snap = crawl(router)
    assert snap.status == "failed" and "Core fetch failed (502): dns error" in snap.problems[0]


def test_disallowed_paths_are_skipped():
    robots = "User-agent: *\nDisallow: /wifi-7-router/\nSitemap: https://blog.example/sitemap_index.xml\n"
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router, robots=robots, feed=False)
        router.get(url__regex=r"https://blog\.example/(rss|feed/?|feed\.xml|rss\.xml|atom\.xml|index\.xml)$").mock(
            return_value=httpx.Response(404)
        )
        snap = crawl(router)
    assert "/wifi-7-router/" in snap.robots_disallowed
    assert all("wifi-7-router" not in p.url for p in snap.posts)


def test_page_budget_is_respected():
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router)
        snap = crawl(router, max_pages=6)
    assert snap.pages_fetched == 6
    assert "page budget reached" in snap.problems


def test_empty_site_is_declared_insufficient():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://blog.example/robots.txt").mock(return_value=httpx.Response(404))
        router.get("https://blog.example/").mock(
            return_value=httpx.Response(200, text="<html><head><title>Coming soon</title></head><body></body></html>")
        )
        router.get(url__regex=r".*").mock(return_value=httpx.Response(404))
        snap = crawl(router)
    assert snap.status == "insufficient"
    assert any("not enough to describe the site" in p for p in snap.problems)


def test_core_transport_routes_through_core_fetch():
    with respx.mock() as router:
        def fetch(request):
            import json
            body = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer tok"
            assert body["url"] == "https://blog.example/feed/"
            assert body["headers"] == {"accept": "application/xml"}  # user-agent dropped
            return httpx.Response(200, json={
                "url": "https://blog.example/feed/", "status": 200, "content_type": "application/xml",
                "etag": '"e"', "last_modified": None, "body_base64": base64.b64encode(b"<rss/>").decode(),
                "truncated": False, "redirects": 0,
            })
        router.post("http://core.test/v1/fetch").mock(side_effect=fetch)
        with httpx.Client(transport=CoreTransport("http://core.test", "tok")) as c:
            r = c.get("https://blog.example/feed/", headers={"Accept": "application/xml", "User-Agent": "x"})
        assert r.status_code == 200 and r.content == b"<rss/>" and r.headers["etag"] == '"e"'


def test_core_refusal_is_an_error():
    with respx.mock() as router:
        router.post("http://core.test/v1/fetch").mock(
            return_value=httpx.Response(403, json={"error": "Blocked: 10.0.0.1 is not public"})
        )
        with httpx.Client(transport=CoreTransport("http://core.test", "tok")) as c:
            with pytest.raises(FetchRefused, match="not public"):
                c.get("https://intranet.example/")


def test_gzip_is_inflated_within_a_limit():
    import gzip

    from editor.site import gunzip_limited

    small = b"<urlset></urlset>"
    assert gunzip_limited(gzip.compress(small), 1000) == small
    bomb = gzip.compress(b"\0" * 5_000_000)  # about 5 KB that inflate to 5 MB
    assert gunzip_limited(bomb, 1_000_000) is None
    assert gunzip_limited(gzip.compress(small * 100)[:40], 10_000) is None  # cut by the Core's size limit
    assert gunzip_limited(b"\x1f\x8bnot gzip", 1000) is None


def test_robots_rules_for_the_core_user_agent_are_obeyed():
    from editor.site import RobotsCache

    with respx.mock() as router, httpx.Client() as c:
        router.get("https://closed.example/robots.txt").mock(return_value=httpx.Response(
            200, text="User-agent: eullm-agent\nDisallow: /\n"))
        router.get("https://open.example/robots.txt").mock(return_value=httpx.Response(404))
        router.get("https://down.example/robots.txt").mock(return_value=httpx.Response(503))
        robots = RobotsCache(c)
        assert not robots.allowed("https://closed.example/news/1")
        assert robots.allowed("https://open.example/news/1")
        assert not robots.allowed("https://down.example/news/1")
        assert robots.allowed("https://open.example/news/2") and router.calls.call_count == 3  # read once per host


def test_site_wide_keywords_are_not_subtopics_and_languages_are_counted():
    from editor.profile import baseline
    from editor.site import Post, SiteSnapshot, _languages, _site_wide_labels

    wide = ["EU AI infrastructure", "AI sovrana"]
    posts = [Post(f"https://x.eu/blog/{i}/", f"Titolo {i}", categories=wide + (["RAG"] if i < 3 else ["Edge"]),
                  language="it", alternates=[f"https://x.eu/en/blog/{i}/"]) for i in range(5)]
    posts += [Post(f"https://x.eu/en/blog/{i}/", f"Title {i}", categories=wide + ["RAG"], language="en",
                   alternates=[f"https://x.eu/blog/{i}/"]) for i in range(2)]
    snap = SiteSnapshot(domain="x.eu", posts=posts, categories={c: 7 for c in wide}, status="partial")
    _site_wide_labels(snap)
    _languages(snap)
    assert snap.site_keywords == wide and "EU AI infrastructure" not in snap.categories
    assert snap.languages == {"it": 5, "en": 2} and snap.language == "it" and snap.translations == 2
    for p in snap.posts:
        for c in p.categories:
            snap.categories[c] = snap.categories.get(c, 0) + 1
    subs = baseline(snap)["subtopics"]
    assert {s["name"] for s in subs} == {"RAG", "Edge"}
    assert sum(s["share"] for s in subs) <= 1


def test_hreflang_alternates_are_read():
    page = parse_html('<html lang="it"><head><link rel="alternate" hreflang="en" href="/en/a/">'
                      '<link rel="alternate" hreflang="x-default" href="/a/"></head></html>', "https://x.eu/a/")
    assert page.alternates == {"en": "https://x.eu/en/a/"}


def test_a_domain_that_redirects_is_read_where_it_lands():
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router)
        router.get("https://alias.example/robots.txt").mock(return_value=httpx.Response(200, text=fakesite.ROBOTS))
        router.get("https://alias.example/").mock(return_value=httpx.Response(
            200, text=fakesite.home_html(), headers={"x-final-url": f"{fakesite.BASE}/"}))
        with httpx.Client() as c:
            snap = SiteCrawler(c).analyse("alias.example")
    assert snap.domain == "alias.example" and snap.read_at == "blog.example"
    assert len(snap.posts) == 8 and snap.sitemap_urls == 8
