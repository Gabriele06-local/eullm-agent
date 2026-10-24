"""Translations of an article into the other languages a site publishes in.

A translation is a draft of its own (`translation_of` the original): the
same sentences in the same order, each with the same sources, so it goes
through the same checks (numbers found in the sources, nothing copied word
for word), is read and approved like any draft, and counts as one article
of the plan. One model call per language, with the article and its search
fields; the site's "Leggi anche" links are left out, since they point to
pages in the site's own language.

How many languages a site may have, its own included, is the plan's
`max_languages`.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from . import drafts, images, quotas, seo, usage
from . import models as m
from .core_client import CoreClient, CoreError, extract_json
from .i18n import N_
from .progress import step

# The languages offered, by their own name.
LANGUAGES = {
    "en": "English", "it": "Italiano", "de": "Deutsch", "fr": "Français", "es": "Español",
    "pt": "Português", "nl": "Nederlands", "pl": "Polski",
}


class LanguageError(ValueError):
    pass


def main_language(s, site_id: int) -> str | None:
    """The site's own language: its approved profile's, else the one read on the site."""
    body = s.execute(select(m.editorial_profiles.c.body)
                     .where(m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")
                     .order_by(m.editorial_profiles.c.version.desc()).limit(1)).scalar() or {}
    lang = body.get("language") or s.execute(select(m.sites.c.language).where(m.sites.c.id == site_id)).scalar()
    return (lang or "")[:2].lower() or None


def site_languages(s, site_id: int) -> list[str]:
    """The languages the site's articles are translated into."""
    return list(s.execute(select(m.sites.c.languages).where(m.sites.c.id == site_id)).scalar() or [])


def allowed(s) -> int | None:
    """How many languages each site may have, its own included (None: no limit)."""
    u = quotas.usage(s)
    return None if u is None else u.plan.get("max_languages")


def set_languages(db, tenant_id: str, site_id: int, languages: list[str]) -> list[str]:
    """Set the languages a site's articles are translated into; returns them."""
    with db.tenant(tenant_id) as s:
        if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).scalar() is None:
            raise LookupError(site_id)
        own = main_language(s, site_id)
        wanted = [x for x in dict.fromkeys(x.strip().lower()[:2] for x in languages) if x and x != own]
        unknown = [x for x in wanted if x not in LANGUAGES]
        if unknown:
            raise LanguageError(f"unknown language: {', '.join(unknown)}")
        limit = allowed(s)
        if limit is not None and 1 + len(wanted) > limit:
            raise LanguageError(f"the plan allows {limit} languages per site, the site's own included")
        s.execute(update(m.sites).where(m.sites.c.id == site_id).values(languages=wanted))
    return wanted


def missing(s, draft_id: int) -> list[str]:
    """The site's languages an approved original has no translation in yet."""
    d = s.execute(select(m.drafts.c.site_id, m.drafts.c.translation_of).where(m.drafts.c.id == draft_id)).first()
    if d is None or d.translation_of is not None:
        return []
    done = set(s.execute(select(m.drafts.c.language).where(m.drafts.c.translation_of == draft_id)).scalars())
    return [x for x in site_languages(s, d.site_id) if x not in done]


SYSTEM = (
    "You translate a news article for a website into another language. You receive the article as JSON: "
    "title, subtitle, the section headings, the sentences in order and the search fields. Translate every "
    "piece faithfully and naturally, as a native journalist of that language would write it for the same "
    "readers: keep every fact, number, name and date exactly, add nothing and leave nothing out, keep the "
    "same number of sentences in the same order, one translated sentence for each. Write numbers the way the "
    "target language writes them only when it does not change their value. Headings that are questions stay "
    "questions. Choose the key phrase readers of that language would type, and a search title (max 60 "
    "characters), a meta description (120-155 characters) and a slug in that language. Translate the image's "
    "alternative text too, when there is one. Reply with JSON only: "
    '{"title": "...", "subtitle": "...", "headings": ["<one per heading, same order>"], '
    '"sentences": ["<one per sentence, same order>"], '
    '"seo": {"keyphrase": "...", "title": "...", "description": "...", "slug": "..."}, "image_alt": "..."}'
)


def _payload(d, claims: list[dict], image_alt: str | None = None) -> tuple[dict, list[str]]:
    headings = [h for h in dict.fromkeys(c["section"] for c in claims) if h]
    sd = d.seo or {}
    out = {
        "title": d.title, "subtitle": d.subtitle or "", "headings": headings,
        "sentences": [c["text"] for c in claims],
        "seo": {k: sd.get(k) or "" for k in ("keyphrase", "title", "description", "slug")},
    }
    if image_alt:
        out["image_alt"] = image_alt
    return out, headings


def _parse(value, n_headings: int, n_sentences: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("the reply is not a JSON object")
    title = " ".join(str(value.get("title") or "").split())
    heads = value.get("headings") or []
    sents = value.get("sentences") or []
    if not 5 <= len(title) <= 160:
        raise ValueError("the title is missing or too long")
    if not isinstance(heads, list) or len(heads) != n_headings:
        raise ValueError(f"expected {n_headings} headings, got {len(heads) if isinstance(heads, list) else 0}")
    if not isinstance(sents, list) or len(sents) != n_sentences:
        raise ValueError(f"expected {n_sentences} sentences, got {len(sents) if isinstance(sents, list) else 0}")
    texts = [" ".join(str(x or "").split()) for x in sents]
    if not all(texts):
        raise ValueError("a sentence is empty")
    return {"title": title, "subtitle": " ".join(str(value.get("subtitle") or "").split())[:300],
            "headings": [" ".join(str(x or "").split()) or None for x in heads], "sentences": texts,
            "seo": value.get("seo") if isinstance(value.get("seo"), dict) else {},
            "image_alt": " ".join(str(value.get("image_alt") or "").split())[:300]}


def translate(db, tenant_id: str, draft_id: int, language: str, core: CoreClient) -> int:
    """Write the translation of a draft into `language`; returns the new draft's id."""
    language = language.strip().lower()[:2]
    if language not in LANGUAGES:
        raise LanguageError(f"unknown language: {language}")
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
    if d is None:
        raise LookupError(draft_id)
    if d.translation_of is not None:  # translate the original, not a translation
        return translate(db, tenant_id, d.translation_of, language, core)
    if (d.language or "")[:2] == language:
        raise LanguageError("the article is already in this language")
    with db.tenant(tenant_id) as s:
        quotas.check(s, "drafts")
        claims = drafts.stored_claims(s, draft_id)
        sources = drafts._sources(s, list({i for c in claims for i in c["sources"]}))
        image = images.of(s, draft_id)
    if not claims:
        raise LanguageError("the draft has no sentences to translate")
    payload, headings = _payload(d, claims, image.alt if image else None)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Target language: {LANGUAGES[language]} ({language})\n"
                                            f"Article (in {d.language or 'its language'}):\n"
                                            + json.dumps(payload, ensure_ascii=False)}]
    out = article = None
    for attempt in range(3):
        step(N_("Il modello traduce l'articolo in {language}") if attempt == 0
             else N_("Il modello corregge la traduzione"), language=LANGUAGES[language])
        with usage.tagged(db, tenant_id, "translate", site_id=d.site_id, ref=f"draft:{draft_id}:{language}"):
            res = core.chat(messages)
        try:
            out = _parse(extract_json(res.content), len(headings), len(claims))
        except ValueError as e:
            problem = str(e)
        else:
            heading = dict(zip(headings, out["headings"]))
            article = drafts.check(drafts.Article(
                out["title"], out["subtitle"],
                [drafts.Claim(text, c["sources"], heading.get(c["section"]) if c["section"] else None)
                 for text, c in zip(out["sentences"], claims)]), sources)
            if not article.flags:
                break
            problem = "; ".join(f"sentence {f['claim']}: {', '.join(f['problems'])}" for f in article.flags)
        messages += [{"role": "assistant", "content": res.content},
                     {"role": "user", "content": f"Fix these problems and reply with the whole JSON again: {problem}"}]
    if article is None:
        raise CoreError("the model did not produce a valid translation")

    old = d.seo or {}
    faq = set(old.get("faq") or [])
    sd = seo.parse(out["seo"], article.title)
    sd.faq = [heading for h, heading in zip(headings, out["headings"]) if h in faq and heading]
    article.faq = sd.faq
    first = next((c.section for c in article.claims), None)
    lead = " ".join(c.text for c in article.claims if c.section == first)
    words = sum(len(c.text.split()) for c in article.claims)
    # The length of the original is the target; no internal links (see above).
    sd.checks = [x for x in seo.checks(sd, article.title, lead, list(dict.fromkeys(c.section for c in article.claims)),
                                       words, sum(len(c["text"].split()) for c in claims),
                                       len({i for c in article.claims for i in c.sources}))
                 if x["key"] != "internal_links"]
    body = drafts.render(article, sources, language, [])
    with db.tenant(tenant_id) as s:
        quotas.check(s, "drafts")  # again: other drafts may have been written meanwhile
        quotas.lock(s, f"editor-draft-version:{d.proposal_id}")
        version = (s.execute(select(func.max(m.drafts.c.version)).where(
            m.drafts.c.proposal_id == d.proposal_id)).scalar() or 0) + 1
        did = s.execute(insert(m.drafts).values(
            tenant_id=tenant_id, proposal_id=d.proposal_id, site_id=d.site_id, version=version,
            title=article.title, subtitle=article.subtitle, body_md=body, language=language,
            status="needs_review" if article.flags else "draft", flags=article.flags, generated_by="model",
            seo=sd.as_dict(), translation_of=draft_id,
        ).returning(m.drafts.c.id)).scalar_one()
        for n, c in enumerate(article.claims):
            cid = s.execute(insert(m.draft_claims).values(
                tenant_id=tenant_id, draft_id=did, ordinal=n, section=c.section, text=c.text,
            ).returning(m.draft_claims.c.id)).scalar_one()
            s.execute(insert(m.draft_claim_sources).values(
                [{"tenant_id": tenant_id, "claim_id": cid, "item_id": i} for i in c.sources]))
        images.copy(s, tenant_id, draft_id, did, out["image_alt"] or None)  # the original's image, if any
    return did
