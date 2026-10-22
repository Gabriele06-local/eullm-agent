from datetime import UTC, datetime

import httpx
import pytest
import respx

from conftest import fixture
from editor.collectors import COLLECTORS, SourceSpec


@respx.mock
def test_rss_parses_and_skips_untitled():
    respx.get("https://telecom.example/feed").mock(
        return_value=httpx.Response(200, content=fixture("rss.xml"), headers={"ETag": '"v1"'})
    )
    with httpx.Client() as c:
        res = COLLECTORS["rss"].fetch(c, SourceSpec("rss", "https://telecom.example/feed"))
    assert res.etag == '"v1"'
    assert [i.title for i in res.items] == [
        "Open Fiber accelera il cablaggio FTTH nelle aree bianche",
        "MikroTik rilascia RouterOS 7.20 con supporto Wi-Fi 7",
    ]
    first = res.items[0]
    assert first.summary == "Il piano prevede 2 milioni di nuove unità immobiliari."
    assert first.published_at == datetime(2026, 10, 8, 5, 30, tzinfo=UTC)


@respx.mock
def test_rss_conditional_get():
    route = respx.get("https://telecom.example/feed").mock(return_value=httpx.Response(304))
    with httpx.Client() as c:
        res = COLLECTORS["rss"].fetch(c, SourceSpec("rss", "https://telecom.example/feed", etag='"v1"'))
    assert res.not_modified and res.items == [] and res.etag == '"v1"'
    assert route.calls[0].request.headers["If-None-Match"] == '"v1"'


@respx.mock
def test_hackernews():
    route = respx.get("https://hn.algolia.com/api/v1/search").mock(
        return_value=httpx.Response(200, content=fixture("hn.json"))
    )
    with httpx.Client() as c:
        res = COLLECTORS["hackernews"].fetch(
            c, SourceSpec("hackernews", "https://hn.algolia.com/api/v1/search", {"query": "llm", "min_points": 50})
        )
    params = route.calls[0].request.url.params
    assert params["query"] == "llm" and params["numericFilters"] == "points>=50"
    assert len(res.items) == 2
    ask = res.items[1]
    assert ask.url == "https://news.ycombinator.com/item?id=45100002"
    assert ask.summary == "We have 2M documents..."
    assert res.items[0].metrics["points"] == 412
    assert res.items[0].published_at == datetime.fromtimestamp(1791446400, tz=UTC)


@respx.mock
def test_github():
    route = respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, content=fixture("github.json"))
    )
    with httpx.Client() as c:
        res = COLLECTORS["github"].fetch(
            c, SourceSpec("github", "", {"query": "topic:rag created:>{since}", "since_days": 7, "min_stars": 10})
        )
    q = route.calls[0].request.url.params["q"]
    assert q.startswith("topic:rag created:>") and "{since}" not in q and q.endswith(" stars:>=10")
    # The description says what a repository is; the name alone does not.
    assert [i.title for i in res.items] == ["acme/fast-rag: Agentic RAG with pgvector", "lab/tiny-llm"]
    assert res.items[0].metrics["stars"] == 5300
    assert res.items[1].summary == ""


def test_hackernews_rejects_non_dict_json():
    with respx.mock() as router:
        router.get("https://hn.algolia.com/api/v1/search").mock(
            return_value=httpx.Response(200, json=["hit"]))
        with httpx.Client() as c:
            with pytest.raises(ValueError, match="unexpected Hacker News response"):
                COLLECTORS["hackernews"].fetch(c, SourceSpec("hackernews", ""))


def test_github_rejects_non_dict_json():
    with respx.mock() as router:
        router.get("https://api.github.com/search/repositories").mock(
            return_value=httpx.Response(200, json=["acme/fast-rag"]))
        with httpx.Client() as c:
            with pytest.raises(ValueError, match="unexpected GitHub response"):
                COLLECTORS["github"].fetch(c, SourceSpec("github", ""))


@respx.mock
def test_huggingface():
    respx.get("https://huggingface.co/api/models").mock(
        return_value=httpx.Response(200, content=fixture("hf.json"))
    )
    with httpx.Client() as c:
        res = COLLECTORS["huggingface"].fetch(c, SourceSpec("huggingface", ""))
    assert res.items[0].url == "https://huggingface.co/mistralai/Example-24B-Instruct"
    assert res.items[0].author == "mistralai"
    assert res.items[0].metrics == {"likes": 1800, "downloads": 250000, "trending": 320}
    assert res.items[1].title == "bge-tiny" and res.items[1].author is None


def test_huggingface_rejects_error_envelope():
    with respx.mock() as router:
        router.get("https://huggingface.co/api/models").mock(
            return_value=httpx.Response(200, json={"error": "Rate limit exceeded"}))
        with httpx.Client() as c:
            with pytest.raises(ValueError, match="unexpected Hugging Face response"):
                COLLECTORS["huggingface"].fetch(c, SourceSpec("huggingface", ""))


@respx.mock
def test_arxiv():
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, content=fixture("arxiv.xml"))
    )
    with httpx.Client() as c:
        res = COLLECTORS["arxiv"].fetch(c, SourceSpec("arxiv", "", {"query": "cat:cs.CL"}))
    (paper,) = res.items
    assert paper.title == "Distilling Long-Context Reasoning into Small Language Models"
    assert paper.external_id == "arxiv:2610.01234v2"
    assert paper.author == "Maria Rossi, John Smith"
    assert paper.metrics["categories"] == ["cs.CL", "cs.LG"]


@respx.mock
def test_http_errors_raise():
    respx.get("https://api.github.com/search/repositories").mock(return_value=httpx.Response(403))
    with httpx.Client() as c:
        try:
            COLLECTORS["github"].fetch(c, SourceSpec("github", ""))
        except httpx.HTTPStatusError:
            return
    raise AssertionError("expected an error")


def test_github_trending():
    with respx.mock() as router:
        route = router.get("https://github.com/trending").mock(
            return_value=httpx.Response(200, content=fixture("github_trending.html")))
        with httpx.Client() as c:
            res = COLLECTORS["github"].fetch(c, SourceSpec("github", "https://github.com/trending",
                                                           {"trending": True, "since": "daily"}))
    assert route.calls.last.request.url.params["since"] == "daily"
    first, second = res.items
    assert first.url == "https://github.com/vllm-project/vllm"
    assert first.title.startswith("vllm-project/vllm: A high-throughput")
    assert first.metrics == {"stars": 95088, "stars_today": 441, "rank": 1, "language": "Python"}
    assert "GitHub Trending #1: +441 stars today" in first.summary
    assert second.title == "someone/tool-x" and second.metrics["stars_today"] == 1100


def test_github_trending_without_repositories_is_an_error():
    with respx.mock:
        respx.get("https://github.com/trending").mock(return_value=httpx.Response(200, text="<html>login</html>"))
        with httpx.Client() as c, pytest.raises(ValueError, match="layout"):
            COLLECTORS["github"].fetch(c, SourceSpec("github", "https://github.com/trending", {"trending": True}))
