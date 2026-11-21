"""Other outlets' reports of the same event, in any language.

The check of the outlets (drafts.event_items) compares the words of the
titles: a news item told in Italian never met the same event told in
English by the BBC or the Guardian, and two English titles written
differently often missed each other too. On 2026-11-05 every one of 167
proposals counted one outlet, a trial against Meta and Google that was
everywhere included (Francesco: "le fonti sono troppo poche per poter
confrontare").

Here names and numbers, which do not change with the language, find the
reports that may tell the same event; one call to the model per round
says which really do (the same fact, not the same subject), and the
proposal keeps them in basis["same_event"]. Each proposal is checked
once (basis["event_checked"]).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from . import drafts, models as m, usage
from .core_client import CoreClient, CoreError
from .text import tokens

log = logging.getLogger(__name__)

PER_ROUND = 10  # proposals checked in one call
CANDIDATES = 8  # reports shown for each
MIN_SHARED = 1  # names or numbers in common to be shown (country names change with the language)
SCAN = 4000  # latest news items read

SYSTEM = (
    "You check whether news reports tell the same event. For each numbered news item below you get "
    "candidate reports from other outlets, in any language, with their ids. Keep only the candidates "
    "that report the same event: the same fact, about the same people or organisations, at the same "
    "moment. Another story on the same subject, a follow-up days later, or an opinion piece is not the "
    'same event. Reply with JSON only: {"same": {"<n>": [<ids of the candidates that are the same event>]}}'
)


def entities(text: str) -> set[str]:
    """Names, acronyms and numbers of a text, lower case: what stays the
    same from one language to another. The first word counts too: in a
    headline it is often the name ("Meta faces..."); a common word there
    only widens the search, the model decides."""
    out = set()
    for w in re.findall(r"[^\W_][\w'’.,-]*", text):
        w = w.strip(".,'’-")
        if any(c.isdigit() for c in w):
            digits = re.sub(r"\D", "", w)
            if len(digits) >= 2:
                out.add(digits)
        elif len(w) > 2 and w[:1].isupper() and tokens(w):
            out.add(w.lower().removesuffix("'s").removesuffix("\u2019s"))
    return out


def _validator(asked: dict[int, set[int]]):
    def validate(v) -> dict[int, list[int]]:
        if not isinstance(v, dict) or not isinstance(v.get("same"), dict):
            raise ValueError('expected {"same": {"<n>": [ids]}}')
        out = {}
        for k, ids in v["same"].items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                continue
            if n in asked and isinstance(ids, list):
                out[n] = [int(i) for i in ids if isinstance(i, int | str) and str(i).isdigit() and int(i) in asked[n]]
        return out
    return validate


def confirm(db, tenant_id: str, site_id: int, core: CoreClient | None, now: datetime | None = None,
            floor: float = 0.0) -> int:
    """Check the best proposals of the site not checked yet; returns how
    many got at least one more outlet."""
    if core is None:
        return 0
    now = now or datetime.now(UTC)
    pr, si = m.proposals.c, m.source_items.c
    with db.tenant(tenant_id) as s:
        rows = [r for r in s.execute(select(m.proposals).where(
            pr.site_id == site_id, pr.status == "proposed", pr.created_at >= now - timedelta(hours=36),
            pr.opportunity >= floor).order_by(pr.opportunity.desc(), pr.id.desc()).limit(PER_ROUND * 4)).all()
                if not (r.basis or {}).get("event_checked")][:PER_ROUND]
        if not rows:
            return 0
        leads = {}
        for r in rows:
            cited = list(s.execute(select(m.proposal_citations.c.item_id).where(
                m.proposal_citations.c.proposal_id == r.id)).scalars())
            main, _ = drafts.event_items(s, cited, r.title)
            if main:
                leads[r.id] = main
        items = {x.id: x for x in s.execute(select(si.id, si.title, si.summary, si.source_id, si.fetched_at).where(
            si.id.in_([i for ids in leads.values() for i in ids])))}
        recent = s.execute(select(si.id, si.title, si.summary, si.source_id).where(
            si.duplicate_of.is_(None), si.fetched_at >= now - timedelta(days=drafts.EVENT_DAYS))
            .order_by(si.id.desc()).limit(SCAN)).all()
    names = {x.id: entities(f"{x.title} {(x.summary or '')[:300]}") for x in recent}
    asked: dict[int, set[int]] = {}
    lines = []
    order = {}
    for n, r in enumerate(rows, 1):
        main = leads.get(r.id)
        if not main or main[0] not in items:
            continue
        lead = items[main[0]]
        mine = entities(f"{lead.title} {(lead.summary or '')[:300]}")
        own = {items[i].source_id for i in main if i in items}
        shared = sorted(((len(mine & names[x.id]), x.id) for x in recent
                         if x.source_id not in own and len(mine & names[x.id]) >= MIN_SHARED), reverse=True)
        picked = [i for _, i in shared[:CANDIDATES]]
        if not picked:
            continue
        order[n] = r.id
        asked[n] = set(picked)
        by_id = {x.id: x for x in recent if x.id in asked[n]}
        lines.append(f"{n}. {lead.title}\n   {(lead.summary or '')[:300]}\n   Candidates:")
        lines += [f"   [{i}] {by_id[i].title} — {(by_id[i].summary or '')[:160]}" for i in picked]
    found: dict[int, list[int]] = {}
    if lines:
        try:
            with usage.tagged(db, tenant_id, "proposal", site_id=site_id, ref=f"same_event:{site_id}"):
                answer, _ = core.chat_json(SYSTEM, "\n".join(lines), _validator(asked))
        except CoreError as e:
            log.warning("same event check for site %s failed: %s", site_id, e)
            return 0  # checked again next round
        found = {order[n]: ids for n, ids in answer.items() if ids}
    with db.tenant(tenant_id) as s:
        for r in rows:
            s.execute(update(m.proposals).where(pr.id == r.id).values(
                basis={**(r.basis or {}), "event_checked": True, "same_event": found.get(r.id, [])}))
    return len(found)
