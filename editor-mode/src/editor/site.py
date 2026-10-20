"""Reading a site the way an editor would: robots.txt, sitemaps, feeds,
home page, categories and a sample of recent articles.

The crawler is polite and bounded: it obeys robots.txt (including
Crawl-delay), fetches at most `max_pages` pages, never runs scripts, and in
production every request goes through the Core (see core_fetch). What it
could not read is recorded as a problem instead of being guessed.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import feedparser
import httpx

from .collectors.base import from_iso, from_struct_time
from .html_extract import Page, parse_html, same_site
from .normalize import canonical_url, clean_text
from .text import tokens

log = logging.getLogger(__name__)

# The product token of the User-Agent the Core sends (``eullm-agent/<version>``),
# so that a site's ``User-agent: eullm-agent`` rules are the ones we obey.
AGENT = "eullm-agent"
MIN_POSTS = 5
GOOD_POSTS = 15
MAX_SITEMAP_BYTES = 10_000_000
FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feed/")
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")
CATEGORY_HINTS = ("/category/", "/categoria/", "/categorie/", "/tag/", "/topics/", "/topic/", "/section/",
                  "/sezione/", "/argomenti/", "/argomento/", "/rubrica/", "/c/")
NOT_SOURCES = (
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "youtu.be",
    "t.me", "telegram.me", "whatsapp.com", "wa.me", "pinterest.com", "reddit.com", "tiktok.com",
    "google.com", "goo.gl", "bit.ly", "gravatar.com", "wordpress.org", "wordpress.com", "wp.com",
    "cloudflare.com", "addtoany.com", "disqus.com", "mailchimp.com", "feedburner.com", "apple.com",
    "play.google.com", "amazon.com", "amzn.to", "doubleclick.net", "w3.org", "schema.org", "creativecommons.org",
)
STOP_LANG = {
    "it": {"il", "di", "che", "la", "per", "non", "una", "sono", "del", "della", "con", "anche", "nel"},
    "en": {"the", "and", "of", "to", "is", "in", "that", "for", "with", "this", "are", "on", "be"},
    "de": {"der", "die", "und", "das", "ist", "nicht", "mit", "ein", "eine", "auf", "für", "den"},
    "fr": {"le", "la", "les", "et", "des", "est", "une", "pour", "que", "dans", "pas", "sur"},
    "es": {"el", "la", "de", "que", "los", "las", "una", "para", "con", "por", "es", "del"},
}


@dataclass
class Post:
    url: str
    title: str
    summary: str = ""
    published_at: str | None = None  # ISO 8601
    categories: list[str] = field(default_factory=list)
    words: int = 0
    language: str | None = None
    alternates: list[str] = field(default_factory=list)  # the same article in other languages


@dataclass
class SiteSnapshot:
    domain: str
    home_url: str = ""
    title: str = ""
    description: str = ""
    site_name: str = ""
    language: str | None = None
    language_evidence: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)  # articles read per language
    translations: int = 0  # articles read in more than one language
    site_keywords: list[str] = field(default_factory=list)  # labels on every page: the site's, not a subtopic's
    read_at: str | None = None  # the host actually read, when the domain redirects elsewhere
    feeds: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    sitemap_urls: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    posts: list[Post] = field(default_factory=list)
    posts_per_week: float | None = None
    median_words: int | None = None
    outbound_domains: dict[str, int] = field(default_factory=dict)
    top_terms: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    robots_disallowed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    status: str = "failed"

    def to_dict(self) -> dict:
        return asdict(self)


def guess_language(text: str) -> str | None:
    words = text.lower().split()
    if len(words) < 30:
        return None
    counts = {lang: sum(1 for w in words if w in stop) for lang, stop in STOP_LANG.items()}
    lang, best = max(counts.items(), key=lambda kv: kv[1])
    return lang if best >= max(5, len(words) * 0.03) else None


def gunzip_limited(data: bytes, limit: int) -> bytes | None:
    """Inflate gzip data without ever producing more than ``limit`` bytes.

    A few MB of gzip can expand to gigabytes, so the output is bounded while
    inflating, not after. Returns None for corrupt, truncated (the Core cuts
    bodies at its size limit) or oversized input.
    """
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or not d.eof:
        return None
    return out


class RobotsCache:
    """robots.txt per host, read once through the Core. Unreachable or 5xx
    means "do not read", a missing file means "read"."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self._robots: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        u = httpx.URL(url)
        rp = self._robots.get(u.host)
        if rp is None:
            rp = RobotFileParser()
            try:
                r = self.client.get(f"{u.scheme}://{u.host}/robots.txt", headers={"Accept": "text/plain"})
                if r.status_code >= 500:
                    rp.parse(["User-agent: *", "Disallow: /"])
                else:
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots[u.host] = rp
        return rp.can_fetch(AGENT, url)


class SiteCrawler:
    def __init__(self, client: httpx.Client, max_pages: int = 60, article_samples: int = 25, max_delay: float = 10):
        self.client = client
        self.max_pages = max_pages
        self.article_samples = article_samples
        self.max_delay = max_delay
        self.robots: RobotFileParser | None = None
        self.delay = 0.0
        self.snap: SiteSnapshot | None = None
        self.home_tags: set[str] = set()
        self.host = ""  # the host the site is read at: the domain, or where it redirects

    # --- fetching ---------------------------------------------------------

    def _get(self, url: str, accept: str = "*/*") -> httpx.Response | None:
        snap = self.snap
        if snap.pages_fetched >= self.max_pages:
            if "page budget reached" not in snap.problems:
                snap.problems.append("page budget reached")
            return None
        if self.robots is not None and not self.robots.can_fetch(AGENT, url):
            snap.robots_disallowed.append(urlsplit(url).path or "/")
            return None
        if self.delay and snap.pages_fetched:
            time.sleep(self.delay)
        snap.pages_fetched += 1
        try:
            r = self.client.get(url, headers={"Accept": accept})
        except httpx.HTTPError as e:
            log.info("fetch %s failed: %s", url, e)
            return None
        return r if r.status_code == 200 else None

    def _final_url(self, r: httpx.Response) -> str:
        return r.headers.get("x-final-url") or str(r.url)

    def _ours(self, url: str) -> bool:
        return same_site(url, self.snap.domain) or bool(self.host and same_site(url, self.host))

    # --- steps ------------------------------------------------------------

    def _robots(self, base: str) -> bool:
        """False when robots.txt forbids reading the site at all."""
        snap = self.snap
        snap.pages_fetched += 1
        try:
            r = self.client.get(base + "/robots.txt", headers={"Accept": "text/plain"})
        except httpx.HTTPError as e:
            snap.problems.append(f"robots.txt unreachable ({type(e).__name__}: {str(e)[:300]}): not crawling")
            return False
        rp = RobotFileParser()
        if r.status_code >= 500:
            snap.problems.append(f"robots.txt answered {r.status_code}: not crawling")
            return False
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            snap.sitemaps += [u for u in (rp.site_maps() or []) if u not in snap.sitemaps]
            delay = rp.crawl_delay(AGENT)
            if delay:
                self.delay = min(float(delay), self.max_delay)
        else:  # 4xx: no rules (RFC 9309)
            rp.parse([])
        self.robots = rp
        if not rp.can_fetch(AGENT, base + "/"):
            snap.problems.append("robots.txt forbids crawling the home page")
            return False
        return True

    def _home(self, base: str) -> Page | None:
        r = self._get(base + "/", "text/html")
        if r is None:
            self.snap.problems.append("home page unreachable")
            return None
        snap = self.snap
        snap.home_url = self._final_url(r)
        # A domain that redirects to another host (rag-pro.eu -> rag.i3k.eu)
        # is read there: its sitemap, links and articles live on that host.
        final = (urlsplit(snap.home_url).hostname or "").lower()
        if final and not same_site(snap.home_url, snap.domain):
            self.host = final.removeprefix("www.")
            snap.read_at = final
        page = parse_html(r.text, snap.home_url)
        snap.title, snap.description, snap.site_name = page.title, page.description, page.site_name
        # Keywords the home page has too are the site's own (a site-wide
        # meta keywords): on an article they say nothing about its subject.
        self.home_tags = {t.lower() for t in page.tags}
        if page.lang:
            snap.language = page.lang
            snap.language_evidence.append(f"html lang={page.lang}")
        snap.feeds += [f for f in page.feeds if f not in snap.feeds]
        for link in page.links:
            if self._ours(link.url) and any(h in urlsplit(link.url).path.lower() for h in CATEGORY_HINTS):
                name = link.text.strip() or urlsplit(link.url).path.rstrip("/").rsplit("/", 1)[-1]
                if name and len(name) <= 60:
                    snap.categories[name] = snap.categories.get(name, 0) + 1
        return page

    def _feeds(self, base: str) -> list[Post]:
        snap = self.snap
        if not snap.feeds:
            for path in FEED_PATHS:
                r = self._get(base + path, "application/rss+xml, application/atom+xml, application/xml")
                if r is not None and feedparser.parse(r.content).entries:
                    snap.feeds.append(self._final_url(r))
                    break
        posts: list[Post] = []
        for feed_url in snap.feeds[:3]:
            r = self._get(feed_url, "application/rss+xml, application/atom+xml, application/xml")
            if r is None:
                continue
            parsed = feedparser.parse(r.content)
            if parsed.feed.get("language") and not snap.language:
                snap.language = parsed.feed["language"].split("-")[0].lower()
                snap.language_evidence.append(f"feed language={snap.language}")
            for e in parsed.entries:
                link, title = e.get("link"), clean_text(e.get("title"))
                if not link or not title or not self._ours(link):
                    continue
                cats = [t.get("term") for t in e.get("tags", []) if t.get("term")]
                dt = from_struct_time(e.get("published_parsed") or e.get("updated_parsed"))
                posts.append(Post(link, title, clean_text(e.get("summary"))[:600], dt.isoformat() if dt else None, cats))
        if not snap.feeds:
            snap.problems.append("no RSS or Atom feed found")
        return posts

    def _sitemap_entries(self, base: str) -> list[tuple[str, datetime | None]]:
        snap = self.snap
        queue = list(snap.sitemaps) or [base + p for p in SITEMAP_PATHS]
        seen, entries = set(), []
        while queue and len(seen) < 6:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            r = self._get(url, "application/xml, text/xml")
            if r is None:
                continue
            data = r.content[:MAX_SITEMAP_BYTES]
            if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
                data = gunzip_limited(data, MAX_SITEMAP_BYTES)
                if data is None:
                    snap.problems.append(f"sitemap {url}: unreadable or oversized gzip, skipped")
                    continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            if url not in snap.sitemaps:
                snap.sitemaps.append(url)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            if root.tag.endswith("sitemapindex"):
                children = []
                for sm in root.findall(f"{ns}sitemap"):
                    loc = (sm.findtext(f"{ns}loc") or "").strip()
                    if loc:
                        children.append((from_iso(sm.findtext(f"{ns}lastmod")), loc))
                # newest first, post sitemaps before pages and tags
                children.sort(key=lambda c: (("post" not in c[1]), -(c[0].timestamp() if c[0] else 0)))
                queue += [loc for _, loc in children[:4]]
            else:
                for u in root.findall(f"{ns}url"):
                    loc = (u.findtext(f"{ns}loc") or "").strip()
                    if loc and self._ours(loc):
                        entries.append((loc, from_iso(u.findtext(f"{ns}lastmod"))))
        snap.sitemap_urls = len(entries)
        if not entries:
            snap.problems.append("no usable sitemap")
        return entries

    def _article(self, url: str) -> tuple[Page, Post] | None:
        r = self._get(url, "text/html")
        if r is None:
            return None
        page = parse_html(r.text, self._final_url(r))
        title = page.h1 or page.title
        if not title:
            return None
        tags = [t for t in page.tags if t.lower() not in self.home_tags]
        cats = ([page.section] if page.section else []) + tags[:5]
        dt = from_iso(page.published)
        text = page.text
        post = Post(url, title, page.description[:600], dt.isoformat() if dt else None, cats, len(text.split()),
                    language=page.lang or guess_language(text),
                    alternates=[u for lang, u in page.alternates.items() if lang != page.lang])
        return page, post

    # --- the whole analysis ------------------------------------------------

    def analyse(self, domain: str) -> SiteSnapshot:
        domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        self.snap = snap = SiteSnapshot(domain=domain)
        self.host = ""
        base = f"https://{domain}"
        if not self._robots(base):
            snap.status = "failed"
            return snap
        home = self._home(base)
        if home is None:
            snap.status = "failed"
            return snap

        posts = {canonical_url(p.url): p for p in self._feeds(base)}
        sitemap = self._sitemap_entries(base)
        recent = sorted(
            (e for e in sitemap if e[1] is not None), key=lambda e: e[1], reverse=True
        ) or sitemap[:50]
        # Article samples: feed posts first, then the newest sitemap URLs.
        candidates = [p.url for p in posts.values()] + [u for u, _ in recent if canonical_url(u) not in posts]
        if not candidates:  # fall back to links on the home page that look like articles
            candidates = [l.url for l in home.links if self._ours(l.url) and not l.in_nav
                          and urlsplit(l.url).path.count("/") >= 2 and len(l.text) > 25]
        outbound: Counter = Counter()
        texts = [home.description, home.title]
        words = []
        for url in candidates[: self.article_samples]:
            got = self._article(url)
            if got is None:
                continue
            page, post = got
            key = canonical_url(url)
            known = posts.get(key)
            if known:
                known.words = post.words
                known.categories = known.categories or post.categories
                known.published_at = known.published_at or post.published_at
                known.language, known.alternates = post.language, post.alternates
            else:
                posts[key] = post
            words.append(post.words)
            texts.append(page.text[:5000])
            for link in page.links:
                host = (urlsplit(link.url).hostname or "").lower().removeprefix("www.")
                if link.in_nav or not host or self._ours(link.url):
                    continue
                if any(host == d or host.endswith("." + d) for d in NOT_SOURCES):
                    continue
                outbound[host] += 1
        # Titles from the sitemap alone are unknown; URLs without a fetched
        # page are not counted as posts.
        snap.posts = sorted(posts.values(), key=lambda p: p.published_at or "", reverse=True)
        _site_wide_labels(snap)
        for p in snap.posts:
            for c in p.categories:
                snap.categories[c] = snap.categories.get(c, 0) + 1
        _languages(snap)
        snap.outbound_domains = dict(outbound.most_common(40))
        snap.top_terms = _top_terms([p.title for p in snap.posts] + list(snap.categories))
        if words:
            snap.median_words = sorted(words)[len(words) // 2]
        dates = [d for d in (from_iso(p.published_at) for p in snap.posts) if d]
        if dates:
            since = datetime.now(UTC) - timedelta(days=90)
            recent_dates = [d for d in dates if d >= since]
            span_weeks = max((max(dates) - min(dates)).days / 7, 1)
            snap.posts_per_week = round(len(recent_dates) / 13 if recent_dates else len(dates) / span_weeks, 2)
        else:
            snap.problems.append("no publication dates found")
        guessed = guess_language(" ".join(texts))
        if guessed and not snap.languages:
            snap.language_evidence.append(f"text looks {guessed}")
            snap.language = snap.language or guessed
        if not snap.language:
            snap.problems.append("language not determined")
        snap.categories = dict(Counter(snap.categories).most_common(30))

        n = len(snap.posts)
        if n < MIN_POSTS and not snap.categories:
            snap.status = "insufficient"
            snap.problems.append(f"only {n} articles readable and no categories: not enough to describe the site")
        elif n < GOOD_POSTS or snap.problems:
            snap.status = "partial"
            if n < GOOD_POSTS:
                snap.problems.append(f"{n} articles read (at least {GOOD_POSTS} wanted)")
        else:
            snap.status = "complete"
        return snap


def _site_wide_labels(snap: SiteSnapshot, min_posts: int = 5, share: float = 0.8) -> None:
    """A label found on (nearly) every article is the site's own: a site-wide
    meta keywords or a tag put on everything. As a subtopic it would cover
    100% of the site and tell nothing apart; it is kept as a site keyword."""
    if len(snap.posts) < min_posts:
        return
    # dict.fromkeys, not set: equal counts keep the order the site gives them.
    counts = Counter(c for p in snap.posts for c in dict.fromkeys(p.categories))
    wide = {c for c, n in counts.items() if n >= share * len(snap.posts)}
    if not wide:
        return
    snap.site_keywords = [c for c, _ in counts.most_common() if c in wide]
    for p in snap.posts:
        p.categories = [c for c in p.categories if c not in wide]
    for c in wide:
        snap.categories.pop(c, None)


def _languages(snap: SiteSnapshot) -> None:
    """Articles read per language, and how many are translations of each
    other (an article and its hreflang alternate both read count once)."""
    langs = Counter(p.language for p in snap.posts if p.language)
    if not langs:
        return
    snap.languages = dict(langs.most_common())
    main = langs.most_common(1)[0][0]
    if snap.language != main:
        snap.language_evidence.append(f"most articles are in {main}")
        snap.language = main
    read = {canonical_url(p.url) for p in snap.posts}
    pairs = {frozenset((canonical_url(p.url), canonical_url(u))) for p in snap.posts for u in p.alternates
             if canonical_url(u) in read}
    snap.translations = len(pairs)


def _top_terms(texts: list[str], n: int = 25) -> list[str]:
    counts = Counter()
    for t in texts:
        counts.update(set(tokens(t)))
    return [t for t, c in counts.most_common(n) if c >= 2]
