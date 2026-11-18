import pytest

from editor.dedup import find_duplicate, minhash, similarity
from editor.normalize import canonical_url, clean_text, content_hash, url_hash


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTP://WWW.Example.com:80/a/b/?utm_source=x&b=2&a=1#top", "https://example.com/a/b?a=1&b=2"),
        ("https://example.com/?fbclid=abc", "https://example.com/"),
        ("https://example.com//x//y/", "https://example.com/x/y"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://example.com:abc/x", "https://example.com/x"),
        ("https://example.com:99999/x", "https://example.com/x"),
        ("http://arxiv.org/pdf/2610.01234v2", "https://arxiv.org/abs/2610.01234"),
        ("https://export.arxiv.org/abs/2610.01234v1", "https://arxiv.org/abs/2610.01234"),
        ("https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=1"),
        ("https://example.com/p?q=", "https://example.com/p?q="),
    ],
)
def test_canonical_url(url, expected):
    assert canonical_url(url) == expected


def test_same_page_same_hash():
    assert url_hash("https://www.a.it/x/?utm_medium=feed") == url_hash("http://a.it/x")


def test_clean_text():
    assert clean_text("<p>A &amp; <b>B</b></p>\n\n c") == "A & B c"


def test_content_hash_ignores_case_and_punctuation():
    assert content_hash("Open Fiber: FTTH!", "") == content_hash("open fiber ftth", "")


def test_minhash_finds_near_duplicates():
    a = minhash("Open Fiber accelera il cablaggio FTTH nelle aree bianche del sud Italia entro il 2027")
    b = minhash("Open Fiber accelera il cablaggio FTTH nelle aree bianche del sud Italia entro fine 2027")
    c = minhash("MikroTik rilascia RouterOS 7.20 con supporto Wi-Fi 7 per hAP e cAP")
    assert similarity(a, b) > 0.6
    assert similarity(a, c) < 0.2
    assert find_duplicate(a, [(1, c), (2, a)]) == 2
    assert find_duplicate(a, [(1, c)]) is None


def test_minhash_is_stable_and_bigint_safe():
    sig = minhash("stable text for signatures")
    assert sig == minhash("stable text for signatures")
    assert all(0 <= x < 2**63 for x in sig)
