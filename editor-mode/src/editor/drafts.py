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
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import quotas, repo
from .core_client import CoreClient, CoreError
from .html_extract import parse_html
from .site import RobotsCache
from .normalize import normalize_text

log = logging.getLogger(__name__)

MAX_CONTENT = 20_000
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
    return Article(title.strip(), str(value.get("subtitle") or "").strip()[:300], claims)


def render(article: Article, sources: dict[int, Source], language: str | None) -> str:
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
    lines.append("\n" + ("## Fonti" if (language or "").startswith("it") else "## Sources"))
    for i in order:
        s = sources[i]
        lines.append(f"\n[^{ref[i]}]: [{s.title}]({s.url}), {s.name}" + (f", {s.published}" if s.published else ""))
    return "\n".join(lines) + "\n"


SYSTEM = (
    "You write original articles for a website. You receive the editorial profile, the accepted proposal and "
    "the source items, each with an id and its text. Write the article as claims: every claim is one or two "
    "sentences and lists the ids of the items that support it. Use only facts found in the cited items; do not "
    "add facts, numbers or quotes from elsewhere, and do not copy sentences from the sources: write in your own "
    "words, in the site's language and tone. Reply with JSON only: "
    '{"title": "...", "subtitle": "...", "sections": [{"heading": "<or null>", "claims": '
    '[{"text": "...", "sources": [<ids>]}]}]}'
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
        with db.tenant(tenant_id) as s:
            s.execute(update(m.source_items).where(m.source_items.c.id == row.id).values(
                content=content, content_fetched_at=datetime.now(UTC)))


def _sources(s, ids: list[int]) -> dict[int, Source]:
    rows = s.execute(
        select(m.source_items, m.sources.c.name.label("source_name"))
        .join(m.sources, m.sources.c.id == m.source_items.c.source_id)
        .where(m.source_items.c.id.in_(ids))).all()
    return {r.id: Source(r.id, r.title, r.url, r.source_name, r.summary or "", r.content or "",
                         r.published_at.date().isoformat() if r.published_at else None) for r in rows}


def _prompt(profile: dict, proposal, sources: dict[int, Source]) -> str:
    style = profile.get("style", {})
    lines = [
        f"Site: {profile.get('name')} ({profile.get('domain')}), language: {profile.get('language')}",
        f"Audience: {(profile.get('audience') or {}).get('value') or 'not determined'}",
        f"Tone: {style.get('tone') or 'not determined'}; typical length: {style.get('typical_words') or 'unknown'} words",
        f"Proposal: {proposal.title}",
        f"Angle: {proposal.angle}",
        f"Format: {proposal.format}",
        "Items:",
    ]
    for src in sources.values():
        lines.append(f"[{src.id}] {src.title} ({src.name}, {src.published or 'undated'})\n{(src.summary + ' ' + src.content)[:6000]}")
    return "\n".join(lines)


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
        profile = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.id == prop.profile_id)).scalar()
    fetch_contents(db, tenant_id, ids, client)
    with db.tenant(tenant_id) as s:
        sources = _sources(s, ids)

    allowed = set(sources)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(profile, prop, sources)}]
    article, calls = None, []
    from .core_client import extract_json

    for attempt in range(3):
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
        with db.tenant(tenant_id) as s:
            for c in calls:
                repo.record_llm_usage(s, tenant_id, "draft", c.model, c.usage, c.cost)
        raise CoreError("the model did not produce a valid article")

    body = render(article, sources, profile.get("language"))
    with db.tenant(tenant_id) as s:
        # The model was paid for whatever happens next.
        for c in calls:
            repo.record_llm_usage(s, tenant_id, "draft", c.model, c.usage, c.cost)
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
