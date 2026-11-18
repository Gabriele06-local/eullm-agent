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

import hashlib
import math

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

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


def duration(minutes: int | None) -> str | None:
    """ISO 8601 duration, as schema.org wants it: 90 -> PT1H30M."""
    if not minutes:
        return None
    h, m = divmod(int(minutes), 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "")


def recipe_card(meta: dict | None, claims) -> dict | None:
    """A recipe as the site and search engines read it, from the draft's
    sentences under its headings (the director's corrections included)."""
    if not meta:
        return None
    if meta.get("own"):  # the site's own recipe (drafts.OWN_RECIPE): every field of a recipe card
        from .drafts import ingredient_line

        prep, cook = meta.get("prep_minutes"), meta.get("cook_minutes")
        card = {"yield": meta.get("yield"), "prepTime": duration(prep), "cookTime": duration(cook),
                "totalTime": duration((prep or 0) + (cook or 0)) if prep or cook else None,
                "ingredients": [ingredient_line(x) for x in meta["items"]], "steps": list(meta["method"]),
                "servings": meta.get("servings"), "prep_minutes": prep, "cook_minutes": cook,
                "difficulty": meta.get("difficulty"), "category": meta.get("category"), "tags": meta.get("tags") or None,
                "nutrition": meta.get("nutrition"), "ingredient_list": meta["items"]}
        return {k: v for k, v in card.items() if v is not None}
    ingredients = [c.text for c in claims if c.section == meta.get("ingredients")]
    steps = [c.text for c in claims if c.section == meta.get("steps")]
    if len(ingredients) < 2 or not steps:
        return None
    prep, cook = meta.get("prep_minutes"), meta.get("cook_minutes")
    card = {"yield": meta.get("yield"), "prepTime": duration(prep), "cookTime": duration(cook),
            "totalTime": duration((prep or 0) + (cook or 0)) if prep and cook else None,
            "ingredients": ingredients, "steps": steps}
    return {k: v for k, v in card.items() if v is not None}


def slugify(text: str, limit: int = 70) -> str:
    original = text
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = f"{out}-{w}" if out else w
    if not out and original.strip():
        return hashlib.sha256(original.encode("utf-8")).hexdigest()[:min(12, limit)]
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


# Where a published article becomes a page of the site itself.
SITE_TARGETS = ("wordpress", "github", "webhook")


def remember_published(s, tenant_id: str, site_id: int) -> int:
    """The articles this newsroom published on the site, among the site's
    own posts: a new article links them as further reading, and the
    proposals know the site already told that story. Only the original
    language: a translation lives at another address in another language.
    Returns how many were added."""
    from .dedup import minhash
    from .normalize import url_hash

    pub, d, tg = m.publications, m.drafts, m.publish_targets
    rows = s.execute(
        select(pub.c.external_url, pub.c.published_at, d.c.title, d.c.subtitle)
        .join(d, d.c.id == pub.c.draft_id).join(tg, tg.c.id == pub.c.target_id)
        .where(d.c.site_id == site_id, d.c.translation_of.is_(None), pub.c.status == "published",
               pub.c.mode == "publish", tg.c.kind.in_(SITE_TARGETS), pub.c.external_url.like("http%"))).all()
    if not rows:
        return 0
    have = {r.url_hash: r.title for r in s.execute(
        select(m.site_posts.c.url_hash, m.site_posts.c.title).where(m.site_posts.c.site_id == site_id))}
    added = 0
    for r in rows:
        h = url_hash(r.external_url)
        if h in have:
            if have[h] != r.title:  # the article was edited after it went out: the link shows its new title
                s.execute(update(m.site_posts).where(m.site_posts.c.site_id == site_id, m.site_posts.c.url_hash == h)
                          .values(title=r.title, summary=r.subtitle, minhash=minhash(f"{r.title} {r.subtitle or ''}")))
                have[h] = r.title
            continue
        have[h] = r.title
        s.execute(insert(m.site_posts).values(
            tenant_id=tenant_id, site_id=site_id, url=r.external_url, url_hash=h, title=r.title,
            summary=r.subtitle, published_at=r.published_at or datetime.now(UTC),
            minhash=minhash(f"{r.title} {r.subtitle or ''}"),
        ).on_conflict_do_nothing(index_elements=["site_id", "url_hash"]))
        added += 1
    return added


# A word in more than this share of the site's titles says little on its
# own ("intelligenza artificiale" on a site about AI).
COMMON = 0.08
# Share of the article's own weighted words a linked title must share.
RELEVANCE = 0.3


def site_links(s, site_id: int, title: str, keyphrase: str, limit: int = INTERNAL_LINKS) -> list[dict]:
    """The site's own articles closest to this one, to link as further
    reading: internal links help readers and crawlers alike. A link needs a
    word the site rarely uses (a company, a person, a product, a law)
    shared with the article: common words alone ("stati uniti", "mercato")
    linked unrelated pieces; the closest first, then the newest."""
    terms = set(tokens(f"{title} {keyphrase}"))
    if not terms:
        return []
    p = m.site_posts
    rows = s.execute(select(p.c.title, p.c.url).where(p.c.site_id == site_id)
                     .order_by(p.c.published_at.desc().nulls_last()).limit(2000)).all()
    words = [set(tokens(r.title)) for r in rows]
    df: dict[str, int] = {}
    for w in words:
        for t in w:
            df[t] = df.get(t, 0) + 1
    n = max(len(rows), 1)
    rare = {t for t in terms if len(rows) < 25 or df.get(t, 0) / n <= COMMON}
    # What each word says: the rarer in the site's titles, the more.
    weight = {t: math.log(1 + n / (1 + df[t])) for t in terms if t in df}  # words the site's titles know
    total = sum(weight.values()) or 1.0
    scored = []
    for i, (r, w) in enumerate(zip(rows, words, strict=True)):
        shared = terms & w
        if r.title.strip().lower() == title.strip().lower():
            continue
        # A rare word is the first filter; the link is worth it only if the
        # shared words carry a real share of what the article is about.
        relevance = sum(weight.get(t, 0) for t in shared) / total
        if shared & rare and relevance >= RELEVANCE:
            scored.append((-relevance, i, r.title, r.url))
    scored.sort()
    return [{"title": t, "url": u} for *_, t, u in scored[:limit]]


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


def checks(seo: Seo, title: str, lead: str, headings: list[str], sources: int,
           own_recipe: bool = False) -> list[dict]:
    """What a reviewer checks before publishing, each with its result.
    Neither length nor questions and answers are asked: an article is as
    long as its sources allow, and padding it hurts more than it helps.
    The site's own recipe has its two headings (ingredients, steps) and no
    sources to cite: those two checks do not apply to it."""
    k = seo.keyphrase
    out = [
        ("keyphrase", bool(k)),
        ("keyphrase_title", bool(k) and _has(k, f"{title} {seo.title}")),
        ("keyphrase_lead", bool(k) and _has(k, lead)),
        ("title_length", 20 <= len(seo.title) <= TITLE_MAX),
        ("description_length", DESCRIPTION[0] <= len(seo.description) <= DESCRIPTION[1]),
        ("answer_first", 15 <= len(lead.split()) <= 90),
        ("headings", own_recipe or len([h for h in headings if h]) >= 2),
        ("internal_links", len(seo.links) >= 1),
    ] + ([] if own_recipe else [("sources", sources >= 2)])
    return [{"key": key, "ok": ok} for key, ok in out]


def jsonld(*, title: str, description: str, language: str | None, url: str | None, keyphrase: str,
           published: str | None, site_name: str | None, site_url: str | None, citations: list[str],
           faq: list[tuple[str, str]], recipe: dict | None = None) -> list[dict]:
    """schema.org data that says what the visible page says: the article,
    its sources, the questions it answers and, for a recipe, the recipe
    (the card search engines show: ingredients, steps, servings, times)."""
    article = {
        "@context": "https://schema.org", "@type": "NewsArticle", "headline": title[:110],
        "description": description, "inLanguage": language or None, "keywords": keyphrase or None,
        "datePublished": published, "dateModified": published,
        "mainEntityOfPage": url, "url": url,
        "publisher": {"@type": "Organization", "name": site_name, "url": site_url} if site_name else None,
        "citation": citations or None,
    }
    out = [{k: v for k, v in article.items() if v is not None}]
    if recipe:
        card = {
            "@context": "https://schema.org", "@type": "Recipe", "name": title[:110], "description": description,
            "inLanguage": language or None, "keywords": keyphrase or None, "datePublished": published,
            "author": {"@type": "Organization", "name": site_name, "url": site_url} if site_name else None,
            "recipeYield": recipe.get("yield"), "prepTime": recipe.get("prepTime"), "cookTime": recipe.get("cookTime"),
            "totalTime": recipe.get("totalTime"), "recipeIngredient": recipe["ingredients"],
            "recipeInstructions": [{"@type": "HowToStep", "position": n, "text": x}
                                   for n, x in enumerate(recipe["steps"], 1)],
            "recipeCategory": recipe.get("category"),
            "nutrition": ({"@type": "NutritionInformation", "calories": recipe["nutrition"].get("calories"),
                           "proteinContent": recipe["nutrition"].get("protein"),
                           "carbohydrateContent": recipe["nutrition"].get("carbs"),
                           "fatContent": recipe["nutrition"].get("fat"),
                           "sugarContent": recipe["nutrition"].get("sugars")} if recipe.get("nutrition") else None),
            "mainEntityOfPage": url, "url": url,
        }
        if card["nutrition"]:
            card["nutrition"] = {k: v for k, v in card["nutrition"].items() if v}
        out.append({k: v for k, v in card.items() if v is not None})
    if len(faq) >= 2:
        out.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]})
    return out
