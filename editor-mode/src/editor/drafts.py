"""Article drafts where every claim is tied to stored sources.

The model writes the article as a list of claims, each with the ids of the
items that support it; there is no free text outside claims. Before a draft
is saved it is checked:

- every claim cites at least one item, and only items given to the model;
- every number in a claim appears in one of its cited items;
- no claim copies a long run of words from a source (originality).

A draft that still fails after a retry is saved as `needs_review` with the
problems listed next to the claims, never silently published.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import quotas, repo, seo, usage
from .core_client import CoreClient, CoreError
from .html_extract import parse_html
from .i18n import N_
from .progress import step
from .site import RobotsCache
from .normalize import normalize_text
from .text import item_terms, tokens

log = logging.getLogger(__name__)

MAX_CONTENT = 20_000
RELATED = 12  # more items on the same subject, for context
RELATED_DAYS = 30
TARGET_WORDS = 1000  # when the site's own length is unknown
# An article is never shorter than this, whatever the site's pages are
# (their median counts short pages too): below it a piece reads as a note
# and gives search engines and AI answers little to take.
MIN_WORDS, MAX_WORDS = 900, 2000
EXPAND_BELOW = 0.75  # under this share of the target, the model is asked once to expand


def target_words(profile: dict) -> int:
    return max(MIN_WORDS, min((profile.get("style") or {}).get("typical_words") or TARGET_WORDS, MAX_WORDS))
COPY_RUN = 12  # words in a row taken from a source count as copying
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?:%|‰)?")


@dataclass
class Source:
    id: int
    title: str
    url: str
    name: str
    summary: str
    content: str
    published: str | None

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}\n{self.content}"


@dataclass
class Claim:
    text: str
    sources: list[int]
    section: str | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class Article:
    title: str
    subtitle: str
    claims: list[Claim]
    faq: list[str] = field(default_factory=list)  # sections that are a reader's question and its answer
    seo: dict = field(default_factory=dict)  # the model's search fields, see seo.parse

    @property
    def flags(self) -> list[dict]:
        return [{"claim": i, "text": c.text[:200], "problems": c.flags} for i, c in enumerate(self.claims) if c.flags]


def numbers(text: str) -> set[str]:
    """Numbers as written, normalised so 1.200 / 1,200 / 1200 compare equal."""
    out = set()
    for n in _NUMBER.findall(text):
        digits = re.sub(r"[.,](?=\d{3}\b)", "", n.rstrip("%‰"))
        out.add(digits.replace(",", "."))
    return out


def copied_run(claim: str, source: str, run: int = COPY_RUN) -> bool:
    words = normalize_text(claim).split()
    if len(words) < run:
        return False
    src = " " + normalize_text(source) + " "
    return any(" " + " ".join(words[i : i + run]) + " " in src for i in range(len(words) - run + 1))


def check(article: Article, sources: dict[int, Source]) -> Article:
    for c in article.claims:
        c.flags = []
        cited = [sources[i] for i in c.sources if i in sources]
        if not cited:
            c.flags.append("no valid source")
            continue
        support = numbers(" ".join(s.text for s in cited))
        missing = sorted(n for n in numbers(c.text) if n not in support)
        if missing:
            c.flags.append(f"numbers not found in the cited sources: {', '.join(missing)}")
        if any(copied_run(c.text, s.text) for s in cited):
            c.flags.append("copies a passage of a source word for word")
    return article


def parse_article(value, allowed: set[int]) -> Article:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    title = value.get("title")
    if not isinstance(title, str) or not 5 <= len(title.strip()) <= 160:
        raise ValueError("title must be 5 to 160 characters")
    sections = value.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty list")
    claims = []
    for sec in sections:
        if not isinstance(sec, dict) or not isinstance(sec.get("claims"), list):
            raise ValueError("each section needs a claims list")
        heading = sec.get("heading") if isinstance(sec.get("heading"), str) else None
        for c in sec["claims"]:
            if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
                raise ValueError("each claim needs a text")
            try:
                ids = [int(x) for x in c.get("sources", [])]
            except (TypeError, ValueError):
                raise ValueError("sources must be item ids") from None
            if not ids:
                raise ValueError(f"claim without sources: {c['text'][:80]!r}")
            unknown = [i for i in ids if i not in allowed]
            if unknown:
                raise ValueError(f"claim cites ids not in the list: {unknown}")
            claims.append(Claim(c["text"].strip(), list(dict.fromkeys(ids)), heading))
    if len(claims) < 3:
        raise ValueError("an article needs at least 3 claims")
    # Questions readers ask, each answered with sourced claims like the rest.
    faq = []
    for q in value.get("faq") or []:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"].strip():
            continue
        question = " ".join(q["question"].split())[:200]
        answer = [c for c in q.get("claims") or [] if isinstance(c, dict) and isinstance(c.get("text"), str)
                  and c["text"].strip()]
        if not answer:
            continue
        for c in answer:
            try:
                ids = [int(x) for x in c.get("sources", [])]
            except (TypeError, ValueError):
                raise ValueError("sources must be item ids") from None
            if not ids or any(i not in allowed for i in ids):
                raise ValueError(f"FAQ answer without valid sources: {c['text'][:80]!r}")
            claims.append(Claim(c["text"].strip(), list(dict.fromkeys(ids)), question))
        faq.append(question)
    seo = value.get("seo") if isinstance(value.get("seo"), dict) else {}
    return Article(title.strip(), str(value.get("subtitle") or "").strip()[:300], claims, faq, seo)


READ_MORE = {"it": "Leggi anche", "de": "Weiterlesen", "fr": "À lire aussi", "es": "Lee también"}


def render(article: Article, sources: dict[int, Source], language: str | None, links: list[dict] | None = None) -> str:
    order: list[int] = []
    for c in article.claims:
        for i in c.sources:
            if i not in order:
                order.append(i)
    ref = {i: n + 1 for n, i in enumerate(order)}
    lines = [f"# {article.title}"]
    if article.subtitle:
        lines.append(f"\n_{article.subtitle}_")
    section, para = object(), []

    def flush():
        if para:
            lines.append("\n" + " ".join(para))
            para.clear()

    for c in article.claims:
        if c.section != section:
            flush()
            section = c.section
            if c.section:
                lines.append(f"\n## {c.section}")
        marks = "".join(f"[^{ref[i]}]" for i in c.sources)
        para.append(f"{c.text}{marks}")
    flush()
    if links:
        lines.append(f"\n## {READ_MORE.get((language or '')[:2], 'Read more')}\n")
        lines += [f"- [{x['title']}]({x['url']})" for x in links]
    lines.append("\n" + ("## Fonti" if (language or "").startswith("it") else "## Sources"))
    for i in order:
        s = sources[i]
        lines.append(f"\n[^{ref[i]}]: [{s.title}]({s.url}), {s.name}" + (f", {s.published}" if s.published else ""))
    return "\n".join(lines) + "\n"


SOURCES_HEADING = re.compile(r"\n## (Fonti|Sources)\n")
NOTE = re.compile(r"^\[\^(\d+)\]: \[(?P<title>[^\]]*)\]\((?P<url>[^)\s]+)\)(?:, (?P<name>[^,\n]+))?", re.M)


def discreet_sources(md: str) -> str:
    """The article for a site that shows its sources discreetly: no note
    numbers in the text, and at the end one line naming each source once,
    linked to the first of its articles. The sentences stay tied to their
    sources in the draft; only the published text changes."""
    found = list(SOURCES_HEADING.finditer(md))
    if not found:
        return md
    head, notes = md[:found[-1].start()], md[found[-1].end():]
    links: dict[str, str] = {}
    for n in NOTE.finditer(notes):
        name = (n.group("name") or n.group("title")).strip()
        links.setdefault(name, n.group("url"))
    if not links:
        return md
    text = re.sub(r"\[\^\d+\]", "", head).rstrip()
    label = found[-1].group(1)
    return f"{text}\n\n**{label}:** " + " · ".join(f"[{k}]({v})" for k, v in links.items()) + "\n"


SYSTEM = (
    "You write original articles for a website. You receive the editorial profile, the accepted proposal and "
    "the source items, each with an id and its text. Write the article as claims: every claim is one to three "
    "sentences and lists the ids of the items that support it. Use only facts found in the cited items; do not "
    "add facts, numbers or quotes from elsewhere, and do not copy sentences from the sources: write in your own "
    "words, in the site's language and tone. Write a full article, not a summary of one source: what happened, "
    "the context and background the other items give (earlier releases, competitors, the market, regulation), "
    "why it matters to the site's audience and what they can do with it. Use every item that is relevant and "
    "skip those that are not. Reach the target length when the items allow it: give each section several claims "
    "with the details the items carry (who, what, numbers, dates, how it works, what changes for the reader); "
    "never pad with vague sentences. Write like a journalist of the site, not like an assistant: vary the length "
    "of sentences, prefer concrete nouns and verbs to adjectives, no stock openings or closings (\"in today's "
    "world\", \"it is worth noting\", \"in conclusion\", \"game changer\", their equivalents in any language), no "
    "em dashes, no triplets of adjectives, no rhetorical questions in the text, no summary of what was just said. "
    "Write it so search engines and AI answer engines pick it up: the first section has no heading and answers "
    "the reader's main question in two or three sentences; the other headings are the questions people search "
    "for, in plain words; then two to four frequently asked questions, each answered with sourced claims like "
    "the rest. Choose the key phrase readers would type to find this article and use it naturally in the title "
    "and the first sentence, never stuffed. Reply with JSON only: "
    '{"title": "...", "subtitle": "...", "sections": [{"heading": "<or null>", "claims": '
    '[{"text": "...", "sources": [<ids>]}]}], '
    '"faq": [{"question": "...", "claims": [{"text": "...", "sources": [<ids>]}]}], '
    '"seo": {"keyphrase": "<2-4 words>", "title": "<search title, max 60 chars>", '
    '"description": "<meta description, 120-155 chars>", "slug": "<url-words-with-hyphens>"}}'
)


def fetch_contents(db, tenant_id: str, item_ids: list[int], client: httpx.Client | None) -> None:
    """Read the full text of cited items once, through the Core."""
    if client is None:
        return
    with db.tenant(tenant_id) as s:
        todo = s.execute(select(m.source_items.c.id, m.source_items.c.url).where(
            m.source_items.c.id.in_(item_ids), m.source_items.c.content_fetched_at.is_(None))).all()
    robots = RobotsCache(client)
    for row in todo:
        content = None
        try:
            if robots.allowed(row.url):
                r = client.get(row.url, headers={"Accept": "text/html"})
                if r.status_code == 200 and "html" in r.headers.get("content-type", "html"):
                    content = parse_html(r.text, row.url).text[:MAX_CONTENT] or None
            else:
                log.info("robots.txt forbids reading %s: drafting from its summary", row.url)
        except httpx.HTTPError as e:
            log.info("cannot read %s: %s", row.url, e)
        # Read once for everyone: the item is in the archive, or (collected
        # before it) the newsroom's own.
        for tenant in dict.fromkeys((repo.archive_of(db, tenant_id), tenant_id)):
            with db.tenant(tenant) as s:
                s.execute(update(m.source_items).where(m.source_items.c.id == row.id).values(
                    content=content, content_fetched_at=datetime.now(UTC)))


def _sources(s, ids: list[int]) -> dict[int, Source]:
    rows = s.execute(
        select(m.source_items, m.sources.c.name.label("source_name"))
        .join(m.sources, m.sources.c.id == m.source_items.c.source_id)
        .where(m.source_items.c.id.in_(ids))).all()
    return {r.id: Source(r.id, r.title, r.url, r.source_name, r.summary or "", r.content or "",
                         r.published_at.date().isoformat() if r.published_at else None) for r in rows}


def _prompt(profile: dict, proposal, sources: dict[int, Source], main: set[int] | None = None,
            posts: list[dict] | None = None) -> str:
    from .autopilot import brief_lines

    style = profile.get("style", {})
    target = target_words(profile)
    lines = [
        f"Site: {profile.get('name')} ({profile.get('domain')}), language: {profile.get('language')}",
        f"Audience: {(profile.get('audience') or {}).get('value') or 'not determined'}",
        f"Tone: {style.get('tone') or 'not determined'}; target length: about {target} words",
        f"Proposal: {proposal.title}",
        f"Angle: {proposal.angle}",
        f"Format: {proposal.format}",
        *brief_lines(profile),
        *_post_lines(posts or []),
        "Items (those marked MAIN are the news the article is about; the others give context):",
    ]
    for src in sources.values():
        mark = " MAIN" if main and src.id in main else ""
        lines.append(f"[{src.id}]{mark} {src.title} ({src.name}, {src.published or 'undated'})\n"
                     f"{(src.summary + ' ' + src.content)[:6000 if mark else 2500]}")
    return "\n".join(lines)


def _post_lines(posts: list[dict]) -> list[str]:
    """What the site already published on the subject: an article this close
    is updated, not retold; the others are only completed."""
    from .opportunity import UPDATE_SIMILARITY

    if not posts:
        return []
    top = posts[0]
    if top["score"] >= UPDATE_SIMILARITY:
        return [f"The site already published «{top['title']}» ({top['url']}) on this subject: write the updated "
                "version, leading with what is new since then; do not retell it as a fresh story."]
    return ["The site already published these related articles; complete them, do not repeat them: "
            + "; ".join(f"«{p['title']}»" for p in posts)]


def related_items(s, ids: list[int], limit: int = RELATED, now: datetime | None = None) -> list[int]:
    """Other recent items on the same subject (sharing at least two words
    with the news the article is about), so the article has context and
    not only one source to retell."""
    si = m.source_items
    base = s.execute(select(si.c.title, si.c.summary).where(si.c.id.in_(ids))).all()
    terms = set().union(*(item_terms(r.title, r.summary or "") for r in base)) if base else set()
    if not terms:
        return []
    since = (now or datetime.now(UTC)) - timedelta(days=RELATED_DAYS)
    rows = s.execute(select(si.c.id, si.c.title, si.c.summary).where(
        si.c.duplicate_of.is_(None), si.c.fetched_at > since, si.c.id.not_in(ids))
        .order_by(si.c.id.desc()).limit(3000)).all()
    scored = []
    for r in rows:
        shared = len(terms & (set(tokens(r.title)) | set(tokens(r.summary or ""))))
        if shared >= 2:
            scored.append((shared, r.id))
    return [i for _, i in sorted(scored, reverse=True)[:limit]]


def _words(article) -> int:
    return sum(len(c.text.split()) for c in article.claims)


def expand(db, tenant_id: str, prop, core: CoreClient, messages: list, last, article, sources: dict, target: int):
    """A draft well under its length is sent back once, with the items it
    left out: the longer version is kept only if it passes every check."""
    words = _words(article)
    if article.flags or words >= target * EXPAND_BELOW:
        return article
    from .core_client import extract_json

    used = {i for c in article.claims for i in c.sources}
    unused = [i for i in sources if i not in used]
    step(N_("Il modello arricchisce la bozza: {words} parole su {target}"), words=words, target=target)
    ask = (f"The article has {words} words; the target is about {target}. Expand it to the target with facts from the "
           f"items: background and context, earlier events, competitors and market, numbers and dates, how it works, "
           f"what it means for the site's audience and what they can do. "
           + (f"Items you have not used yet: {', '.join(map(str, unused))}. " if unused else "")
           + "Keep every rule: only facts from the cited items, every claim with its sources, your own words. "
           "Reply with the whole JSON again.")
    with usage.tagged(db, tenant_id, "draft", site_id=prop.site_id, ref=f"proposal:{prop.id}"):
        res = core.chat(messages + [{"role": "assistant", "content": last.content}, {"role": "user", "content": ask}])
    try:
        longer = check(parse_article(extract_json(res.content), set(sources)), sources)
    except ValueError as e:
        log.info("expanded draft refused: %s", e)
        return article
    if longer.flags or _words(longer) <= words:
        log.info("expanded draft not kept: %s flags, %d words", len(longer.flags), _words(longer))
        return article
    return longer


def write_draft(db, tenant_id: str, proposal_id: int, core: CoreClient, client: httpx.Client | None = None) -> int:
    """Write and store a draft for an accepted proposal; returns its id."""
    with db.tenant(tenant_id) as s:
        prop = s.execute(select(m.proposals).where(m.proposals.c.id == proposal_id)).first()
        if prop is None or prop.status not in ("accepted", "drafted"):
            raise ValueError("drafts are written for accepted proposals only")
        quotas.check(s, "drafts")
        cited = list(s.execute(select(m.proposal_citations.c.item_id).where(
            m.proposal_citations.c.proposal_id == proposal_id)).scalars())
        more = list(s.execute(select(m.topic_items.c.item_id).where(m.topic_items.c.topic_id == prop.topic_id)
                              .limit(12)).scalars())
        ids = list(dict.fromkeys(cited + more))
        ids += related_items(s, ids)
        profile = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.id == prop.profile_id)).scalar()
        from .opportunity import related_posts

        news = list(s.execute(select(m.source_items.c.title).where(m.source_items.c.id.in_(cited))).scalars())
        posts = related_posts(s, prop.site_id, [prop.title, *news])
    step(N_("Lettura degli articoli di {n} notizie collegate"), n=len(ids))
    fetch_contents(db, tenant_id, ids, client)
    with db.tenant(tenant_id) as s:
        sources = _sources(s, ids)

    allowed = set(sources)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(profile, prop, sources, set(cited), posts)}]
    article, calls = None, []
    from .core_client import extract_json

    for attempt in range(3):
        step(N_("Il modello scrive la bozza") if attempt == 0
             else N_("Il modello corregge la bozza: frasi senza fonte o numeri che le fonti non riportano"))
        with usage.tagged(db, tenant_id, "draft", site_id=prop.site_id, ref=f"proposal:{proposal_id}"):
            res = core.chat(messages)
        calls.append(res)
        try:
            article = check(parse_article(extract_json(res.content), allowed), sources)
        except ValueError as e:
            problem = str(e)
        else:
            if not article.flags:
                break
            problem = "; ".join(f"claim {f['claim']}: {', '.join(f['problems'])}" for f in article.flags)
        messages += [{"role": "assistant", "content": res.content},
                     {"role": "user", "content": f"Fix these problems and reply with the whole JSON again: {problem}"}]
    if article is None:
        raise CoreError("the model did not produce a valid article")
    article = expand(db, tenant_id, prop, core, messages, calls[-1], article, sources, target_words(profile))

    sd = seo.parse(article.seo, article.title)
    sd.faq = article.faq
    with db.tenant(tenant_id) as s:
        sd.links = seo.merge_links(posts, seo.site_links(s, prop.site_id, article.title, sd.keyphrase))
    body = render(article, sources, profile.get("language"), sd.links)
    first = next((c.section for c in article.claims), None)
    lead = " ".join(c.text for c in article.claims if c.section == first)
    style = profile.get("style", {})
    target = target_words(profile)
    sd.checks = seo.checks(sd, article.title, lead, list(dict.fromkeys(c.section for c in article.claims)),
                           sum(len(c.text.split()) for c in article.claims), target,
                           len({i for c in article.claims for i in c.sources}))
    with db.tenant(tenant_id) as s:
        # Checked again with the insert: other drafts may have been written
        # while this one was with the model.
        quotas.check(s, "drafts")
        quotas.lock(s, f"editor-draft-version:{proposal_id}")
        version = (s.execute(select(func.max(m.drafts.c.version)).where(m.drafts.c.proposal_id == proposal_id)).scalar() or 0) + 1
        did = s.execute(insert(m.drafts).values(
            tenant_id=tenant_id, proposal_id=proposal_id, site_id=prop.site_id, version=version,
            title=article.title, subtitle=article.subtitle, body_md=body, language=profile.get("language"),
            status="needs_review" if article.flags else "draft", flags=article.flags, generated_by="model",
            seo=sd.as_dict(),
        ).returning(m.drafts.c.id)).scalar_one()
        for n, c in enumerate(article.claims):
            cid = s.execute(insert(m.draft_claims).values(
                tenant_id=tenant_id, draft_id=did, ordinal=n, section=c.section, text=c.text,
            ).returning(m.draft_claims.c.id)).scalar_one()
            s.execute(insert(m.draft_claim_sources).values(
                [{"tenant_id": tenant_id, "claim_id": cid, "item_id": i} for i in c.sources]))
        s.execute(update(m.proposals).where(m.proposals.c.id == proposal_id).values(status="drafted"))
    return did


def decide(db, tenant_id: str, draft_id: int, approve: bool, by: str) -> bool:
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.drafts.c.status).where(m.drafts.c.id == draft_id)).first()
        if row is None or row.status in ("approved", "rejected"):
            return False
        s.execute(update(m.drafts).where(m.drafts.c.id == draft_id).values(
            status="approved" if approve else "rejected", decided_by=by, decided_at=datetime.now(UTC)))
    return True


def stored_claims(s, draft_id: int) -> list[dict]:
    """The claims of a draft in order, each with the ids of its sources."""
    c, cs = m.draft_claims, m.draft_claim_sources
    sources: dict[int, list[int]] = {}
    for r in s.execute(select(cs.c.claim_id, cs.c.item_id).join(c, c.c.id == cs.c.claim_id)
                       .where(c.c.draft_id == draft_id).order_by(cs.c.item_id)):
        sources.setdefault(r.claim_id, []).append(r.item_id)
    rows = s.execute(select(c.c.id, c.c.ordinal, c.c.section, c.c.text)
                     .where(c.c.draft_id == draft_id).order_by(c.c.ordinal))
    return [dict(r._mapping, sources=sources.get(r.id, [])) for r in rows]


def edit(db, tenant_id: str, draft_id: int, title: str, subtitle: str, texts: dict[int, str],
         search: dict | None = None) -> dict:
    """A person's corrections to a draft: title, subtitle, the text of each
    sentence and the search fields. Every sentence keeps its sources and is
    checked again like the model's (numbers in the sources, no copying); an
    approved draft goes back to be approved, because what was approved has
    changed. Returns the new status and the problems found."""
    title = " ".join((title or "").split())
    if not 5 <= len(title) <= 160:
        raise ValueError("title must be 5 to 160 characters")
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        if d is None:
            raise LookupError(draft_id)
        stored = stored_claims(s, draft_id)
        sources = _sources(s, list({i for c in stored for i in c["sources"]}))
        profile = s.execute(select(m.editorial_profiles.c.body).join(
            m.proposals, m.proposals.c.profile_id == m.editorial_profiles.c.id)
            .where(m.proposals.c.id == d.proposal_id)).scalar() or {}
    claims = []
    for c in stored:
        text = " ".join((texts.get(c["ordinal"], c["text"]) or "").split())
        if not text:
            raise ValueError("a sentence cannot be empty: delete the draft or write it again")
        claims.append(Claim(text, c["sources"], c["section"]))
    old = d.seo or {}
    sd = seo.parse({**{k: old.get(k) for k in ("keyphrase", "title", "description", "slug")}, **(search or {})}, title)
    sd.faq, sd.links = old.get("faq") or [], old.get("links") or []
    article = check(Article(title, " ".join((subtitle or "").split())[:300], claims, sd.faq, {}), sources)
    first = next((c.section for c in claims), None)
    lead = " ".join(c.text for c in claims if c.section == first)
    target = target_words(profile)
    sd.checks = seo.checks(sd, title, lead, list(dict.fromkeys(c.section for c in claims)),
                           sum(len(c.text.split()) for c in claims), target, len({i for c in claims for i in c.sources}))
    status = "needs_review" if article.flags else "draft"
    with db.tenant(tenant_id) as s:
        for c, new in zip(stored, claims):
            if new.text != c["text"]:
                s.execute(update(m.draft_claims).where(m.draft_claims.c.id == c["id"]).values(text=new.text))
        s.execute(update(m.drafts).where(m.drafts.c.id == draft_id).values(
            title=title, subtitle=article.subtitle, body_md=render(article, sources, d.language, sd.links),
            flags=article.flags, seo=sd.as_dict(), status=status, decided_by=None, decided_at=None))
    return {"status": status, "flags": article.flags, "was_approved": d.status == "approved"}
