"""What makes a draft easy for search engines and AI answer engines to
pick up: a key phrase, a search title and description, a slug, an
answer in the first lines, questions as headings with their answers, links
to the site's own articles, and structured data (schema.org JSON-LD)
that says the same as the visible text.

Google asks for nothing special for AI Overviews and AI Mode: an indexed
page, readable text, internal links and structured data that matches the
page. Bing, and the answer engines built on its index, can be told about a
new page at once with IndexNow; Google has no such call for articles.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import select

from . import models as m
from .text import tokens

TITLE_MAX = 60
DESCRIPTION = (70, 160)
INTERNAL_LINKS = 3
INDEXNOW = "https://api.indexnow.org/indexnow"


@dataclass
class Seo:
    keyphrase: str = ""
    title: str = ""
    description: str = ""
    slug: str = ""
    faq: list[str] = field(default_factory=list)  # the sections that are questions with their answer
    links: list[dict] = field(default_factory=list)  # the site's own articles linked as further reading
    checks: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"keyphrase": self.keyphrase, "title": self.title, "description": self.description,
                "slug": self.slug, "faq": self.faq, "links": self.links, "checks": self.checks}


def slugify(text: str, limit: int = 70) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = f"{out}-{w}" if out else w
    return out


def parse(value, title: str) -> Seo:
    """The model's search fields, checked and clamped; missing ones are
    derived from the article."""
    v = value if isinstance(value, dict) else {}

    def text(key: str) -> str:
        x = v.get(key)
        return " ".join(x.split()) if isinstance(x, str) else ""

    seo = Seo(keyphrase=text("keyphrase")[:80], title=text("title") or title, description=text("description"))
    if len(seo.title) > TITLE_MAX:
        cut = seo.title[:TITLE_MAX + 1].rsplit(" ", 1)[0]
        # A title with no space in its first TITLE_MAX+1 characters comes back
        # whole from rsplit: it must still be cut, or it keeps its length.
        seo.title = cut if 20 <= len(cut) <= TITLE_MAX else seo.title[:TITLE_MAX]
    if len(seo.description) > DESCRIPTION[1]:
        seo.description = seo.description[:DESCRIPTION[1] - 1].rsplit(" ", 1)[0] + "…"
    seo.slug = slugify(text("slug") or seo.keyphrase or title)
    return seo


def site_links(s, site_id: int, title: str, keyphrase: str, limit: int = INTERNAL_LINKS) -> list[dict]:
    """The site's own articles closest to this one, to link as further
    reading: internal links help readers and crawlers alike."""
    terms = set(tokens(f"{title} {keyphrase}"))
    if not terms:
        return []
    p = m.site_posts
    rows = s.execute(select(p.c.title, p.c.url).where(p.c.site_id == site_id)
                     .order_by(p.c.published_at.desc().nulls_last()).limit(2000)).all()
    scored = []
    for r in rows:
        shared = len(terms & set(tokens(r.title)))
        if shared >= 2 or (shared == 1 and len(terms) <= 3):
            scored.append((shared, r.title, r.url))
    scored.sort(key=lambda x: -x[0])
    return [{"title": t, "url": u} for _, t, u in scored[:limit]]


def merge_links(*lists: list[dict], limit: int = INTERNAL_LINKS) -> list[dict]:
    """The internal links of an article, first lists first, each page once."""
    seen, out = set(), []
    for links in lists:
        for x in links:
            if x.get("url") and x["url"] not in seen:
                seen.add(x["url"])
                out.append({"title": x["title"], "url": x["url"]})
    return out[:limit]


def _has(phrase: str, text: str) -> bool:
    words = tokens(phrase)
    have = set(tokens(text))
    return bool(words) and sum(w in have for w in words) >= max(1, round(len(words) * 0.6))


def checks(seo: Seo, title: str, lead: str, headings: list[str], words: int, target: int, sources: int) -> list[dict]:
    """What a reviewer checks before publishing, each with its result."""
    k = seo.keyphrase
    out = [
        ("keyphrase", bool(k)),
        ("keyphrase_title", bool(k) and _has(k, f"{title} {seo.title}")),
        ("keyphrase_lead", bool(k) and _has(k, lead)),
        ("title_length", 20 <= len(seo.title) <= TITLE_MAX),
        ("description_length", DESCRIPTION[0] <= len(seo.description) <= DESCRIPTION[1]),
        ("answer_first", 15 <= len(lead.split()) <= 90),
        ("headings", len([h for h in headings if h]) >= 2),
        ("faq", len(seo.faq) >= 2),
        ("internal_links", len(seo.links) >= 1),
        ("sources", sources >= 2),
        ("length", words >= int(target * 0.7)),
    ]
    return [{"key": key, "ok": ok} for key, ok in out]


def jsonld(*, title: str, description: str, language: str | None, url: str | None, keyphrase: str,
           published: str | None, site_name: str | None, site_url: str | None, citations: list[str],
           faq: list[tuple[str, str]]) -> list[dict]:
    """schema.org data that says what the visible page says: the article,
    its sources, and the questions it answers."""
    article = {
        "@context": "https://schema.org", "@type": "NewsArticle", "headline": title[:110],
        "description": description, "inLanguage": language or None, "keywords": keyphrase or None,
        "datePublished": published, "dateModified": published,
        "mainEntityOfPage": url, "url": url,
        "publisher": {"@type": "Organization", "name": site_name, "url": site_url} if site_name else None,
        "citation": citations or None,
    }
    out = [{k: v for k, v in article.items() if v is not None}]
    if len(faq) >= 2:
        out.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]})
    return out
