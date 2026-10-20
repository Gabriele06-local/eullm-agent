"""Reading HTML without running it: the standard library parser collects
text, links and metadata; scripts and styles are skipped, never executed."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml", "text/xml")
SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "blockquote", "figcaption", "td"}


@dataclass
class Link:
    url: str
    text: str
    in_nav: bool


@dataclass
class Page:
    url: str
    lang: str | None = None
    title: str = ""
    description: str = ""
    site_name: str = ""
    h1: str = ""
    published: str | None = None
    section: str | None = None
    tags: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    canonical: str | None = None
    alternates: dict[str, str] = field(default_factory=dict)  # hreflang -> URL of the same page in that language
    links: list[Link] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    article_paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.article_paragraphs or self.paragraphs)


class _Parser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.page = Page(url=base)
        self.base = base
        self.skip = 0
        self.nav = 0
        self.nav_tags: list[str] = []
        self.article = 0
        self.in_title = False
        self.in_h1 = False
        self.block: list[str] | None = None
        self.link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "html" and a.get("lang"):
            self.page.lang = a["lang"].split("-")[0].lower()
        elif tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        elif tag == "title":
            self.in_title = True
        elif tag == "h1" and not self.page.h1:
            self.in_h1 = True
        elif tag in ("nav", "header", "footer") or "menu" in a.get("class", "") or a.get("role") == "navigation":
            self.nav += 1
            self.nav_tags.append(tag)
        elif tag in ("article", "main"):
            self.article += 1
        elif tag == "meta":
            self._meta(a)
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href")
            if href and "alternate" in rel and a.get("type", "").lower() in FEED_TYPES:
                self.page.feeds.append(urljoin(self.base, href))
            elif href and "alternate" in rel and a.get("hreflang") and a["hreflang"].lower() != "x-default":
                self.page.alternates[a["hreflang"].split("-")[0].lower()] = urljoin(self.base, href)
            elif href and rel == "canonical":
                self.page.canonical = urljoin(self.base, href)
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            if not href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                self.link = (urljoin(self.base, href), [])
        if tag in BLOCK:
            self.block = []

    def _meta(self, a):
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
        val = a.get("content", "").strip()
        if not val:
            return
        if key in ("description", "og:description") and not self.page.description:
            self.page.description = val
        elif key == "og:site_name":
            self.page.site_name = val
        elif key in ("article:published_time", "datepublished", "date", "dc.date") and not self.page.published:
            self.page.published = val
        elif key == "article:section" and not self.page.section:
            self.page.section = val
        elif key in ("article:tag", "keywords"):
            self.page.tags += [t.strip() for t in val.split(",") if t.strip()]
        elif key == "og:locale" and not self.page.lang:
            self.page.lang = val.split("_")[0].lower()

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        elif tag == "h1":
            self.in_h1 = False
        elif tag in ("nav", "header", "footer") or tag in self.nav_tags:
            self.nav = max(0, self.nav - 1)
            if tag in self.nav_tags:
                self.nav_tags.remove(tag)
        elif tag in ("article", "main"):
            self.article = max(0, self.article - 1)
        elif tag == "a" and self.link:
            url, parts = self.link
            self.page.links.append(Link(url, " ".join(" ".join(parts).split()), self.nav > 0))
            self.link = None
        if tag in BLOCK and self.block is not None:
            text = " ".join(" ".join(self.block).split())
            if len(text) >= 40 and self.nav == 0:
                self.page.paragraphs.append(text)
                if self.article:
                    self.page.article_paragraphs.append(text)
            self.block = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.page.title += data
        if self.in_h1:
            self.page.h1 += data
        if self.link:
            self.link[1].append(data)
        if self.block is not None:
            self.block.append(data)


MAX_HTML = 3_000_000


def parse_html(html: str, url: str) -> Page:
    p = _Parser(url)
    try:
        p.feed(html[:MAX_HTML])
        p.close()
    except Exception:  # malformed markup: keep what was read
        pass
    page = p.page
    page.title = " ".join(page.title.split())
    page.h1 = " ".join(page.h1.split())
    return page


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)
