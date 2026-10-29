"""Editorial profile of a site, built from what the site shows.

A deterministic baseline comes first (categories, terms, frequency, length):
it needs no model and every element points at the articles it comes from.
The Core's model then names the sector, the audience and the style and
groups the subtopics, but each statement must cite articles of the site that
were actually read; statements without valid evidence are dropped. When the
site gives too little to go on, the profile says so and leaves the sector
empty instead of guessing.

Profiles are versioned. Analysis and re-analysis only ever write drafts; a
person approves the version that drives proposals.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m
from . import quotas, repo, usage
from .core_client import CoreClient, CoreError
from .dedup import minhash
from .i18n import N_, t
from .normalize import url_hash
from .progress import step
from .site import SiteSnapshot, is_note
from .text import tokens

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {"proposals_per_day": 6, "min_relevance": 0.15, "exclude": [],
                    "cover_style": "impact", "cover_text": False, "sources_style": "notes"}
# Raised when the analysis itself improves: a profile approved under an
# older method is offered again, rebuilt, even if the site did not change.
METHOD = "profile-v2"


# --- manual edits ---------------------------------------------------------------
# A person's edit is checked before it is stored: a malformed body would be
# kept and then break the dashboard and the drafts every time they read it.
# Known parts are typed; other keys (analysis, gaps, ...) are kept as they are.

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Part(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Labelled(_Part):
    value: str | None = Field(None, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=50)


class _Subtopic(_Part):
    name: _Text
    keywords: list[_Text] = Field(default_factory=list, max_length=50)
    share: float | None = Field(None, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=50)


class _Style(_Part):
    tone: str | None = Field(None, max_length=200)
    formats: list[_Text] = Field(default_factory=list, max_length=20)
    typical_words: int | None = Field(None, ge=0, le=100_000)
    evidence: list[str] = Field(default_factory=list, max_length=50)


class _Settings(_Part):
    proposals_per_day: int = Field(DEFAULT_SETTINGS["proposals_per_day"], ge=0, le=50)
    min_relevance: float = Field(DEFAULT_SETTINGS["min_relevance"], ge=0, le=1)
    exclude: list[_Text] = Field(default_factory=list, max_length=200)
    cover_style: Literal["impact", "editorial"] = DEFAULT_SETTINGS["cover_style"]
    cover_text: bool = DEFAULT_SETTINGS["cover_text"]
    sources_style: Literal["notes", "list"] = DEFAULT_SETTINGS["sources_style"]


class _ProfileEdit(_Part):
    subtopics: list[_Subtopic] = Field(..., max_length=50)
    sector: _Labelled | None = None
    audience: _Labelled | None = None
    style: _Style | None = None
    settings: _Settings | None = None
    search_terms: list[_Text] = Field(default_factory=list, max_length=100)


def validate_edit(body) -> dict:
    """The edit as it will be stored, or ValueError saying what is wrong."""
    try:
        return _ProfileEdit.model_validate(body).model_dump(exclude_unset=True)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'body'}: {err['msg']}"
                             for err in e.errors()[:5])
        raise ValueError(problems) from None


# --- baseline -------------------------------------------------------------


def _post_terms(post) -> set[str]:
    return set(tokens(post.title)) | {t for c in post.categories for t in tokens(c)}


def share_of(keywords: list[str], posts) -> tuple[float, list[str]]:
    """Fraction of posts matching any keyword, and the matching URLs."""
    kw = {t for k in keywords for t in tokens(k)}
    if not posts or not kw:
        return 0.0, []
    hits = [p.url for p in posts if _post_terms(p) & kw]
    return round(len(hits) / len(posts), 3), hits


def assign_shares(subtopics: list[dict], posts) -> list[tuple[float, list[str]]]:
    """Each post goes to the subtopic its title and categories match best, so
    the shares add up to at most 1. A word found in every post (the site's
    own name, its main theme) tells the subtopics apart from nothing and
    weighs nothing; rarer words weigh more."""
    if not posts:
        return [(0.0, []) for _ in subtopics]
    terms = [_post_terms(p) for p in posts]
    df = Counter(t for ts in terms for t in ts)
    weight = {t: math.log(len(posts) / c) for t, c in df.items()}
    kws = [{t for k in s["keywords"] for t in tokens(k)} for s in subtopics]
    hits: list[list[str]] = [[] for _ in subtopics]
    for post, ts in zip(posts, terms):
        scores = [sum(weight[t] for t in ts & kw) for kw in kws]
        best = max(range(len(scores)), key=lambda i: scores[i], default=None)
        if best is not None and scores[best] > 0:
            hits[best].append(post.url)
    return [(round(len(h) / len(posts), 3), h) for h in hits]


def baseline_subtopics(snap: SiteSnapshot, limit: int = 8) -> list[dict]:
    out = []
    for cat, _count in Counter(snap.categories).most_common():
        members = [p for p in snap.posts if cat in p.categories]
        if not members:
            continue
        terms = Counter(t for p in members for t in tokens(p.title))
        keywords = list(dict.fromkeys(tokens(cat) + [t for t, c in terms.most_common(6) if c >= 2]))
        if not keywords:
            continue
        out.append({"name": cat, "keywords": keywords, "by": "categories"})
        if len(out) >= limit:
            break
    # Each article counts for one subtopic only, as for the model's.
    for sub, (share, evidence) in zip(out, assign_shares(out, snap.posts)):
        sub["share"], sub["evidence"] = share, evidence[:5]
    return sorted(out, key=lambda s: -s["share"])


def baseline(snap: SiteSnapshot) -> dict:
    gaps = [p for p in snap.problems if not is_note(p)]
    return {
        "domain": snap.domain,
        "method": METHOD,
        "name": snap.site_name or snap.title or snap.domain,
        "language": snap.language,
        "languages": snap.languages,
        "site_keywords": snap.site_keywords,
        "read_at": snap.read_at,
        "summary": None,
        "analysis": {
            "status": snap.status,
            "posts_read": len(snap.posts),
            "translations": snap.translations,
            "posts_per_week": snap.posts_per_week,
            "median_words": snap.median_words,
            "feeds": snap.feeds,
            "analysed_at": datetime.now(UTC).isoformat(),
        },
        "sector": {"value": None, "evidence": [], "by": None},
        "subtopics": baseline_subtopics(snap),
        "audience": {"value": None, "evidence": [], "by": None},
        "style": {
            "tone": None,
            "formats": [],
            "typical_words": snap.median_words,
            "evidence": [],
        },
        "categories": snap.categories,
        "top_terms": snap.top_terms,
        "gaps": gaps,
        "settings": dict(DEFAULT_SETTINGS),
    }


# --- model enrichment -------------------------------------------------------

SYSTEM = (
    "You describe the editorial line of a website for its editors, using ONLY the evidence given: "
    "the site's own title, description, categories and the articles that were read. Every statement must "
    "cite article URLs from the list as evidence. If the evidence does not support a statement, use null "
    "and add a line to gaps. Never invent a sector, audience or source. Reply with JSON only:\n"
    '{"summary": {"value": "<two or three sentences, in the site language, on what the site publishes, for whom and how; or null>", "evidence": ["<url>"]},\n'
    ' "sector": {"value": "<sector or null>", "evidence": ["<url>"]},\n'
    ' "subtopics": [{"name": "<subtopic>", "keywords": ["<lowercase keywords: 3-6 in the site language, then the same ones in English, so news in both languages are found; a site in English lists them once>"], "evidence": ["<url>"]}],\n'
    ' "audience": {"value": "<who reads it, or null>", "evidence": ["<url>"]},\n'
    ' "style": {"tone": "<tone, or null>", "formats": ["news|guide|review|analysis|opinion|tutorial|interview|data"], "evidence": ["<url>"]},\n'
    ' "gaps": ["<what could not be determined>"]}'
)


def _evidence_prompt(snap: SiteSnapshot, max_posts: int = 40) -> str:
    lines = [
        f"Domain: {snap.domain}",
        f"Title: {snap.title}",
        f"Description: {snap.description}",
        f"Language: {snap.language or 'unknown'}",
        f"Articles read per language: {', '.join(f'{k} ({v})' for k, v in snap.languages.items()) or 'unknown'}",
        f"Site-wide keywords (on every page): {', '.join(snap.site_keywords) or 'none'}",
        f"Categories (count): {', '.join(f'{k} ({v})' for k, v in snap.categories.items()) or 'none'}",
        f"Articles per week: {snap.posts_per_week}",
        f"Median article length (words): {snap.median_words}",
        "Articles read:",
    ]
    for p in snap.posts[:max_posts]:
        cats = f" [{', '.join(p.categories)}]" if p.categories else ""
        lang = f" ({p.language})" if p.language else ""
        lines.append(f"- {p.url} | {p.title}{lang}{cats}")
    return "\n".join(lines)


def make_validator(allowed_urls: set[str]):
    def ok_evidence(v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [u for u in v if isinstance(u, str) and u in allowed_urls]

    def validate(value) -> dict:
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        out = {"gaps": [g for g in value.get("gaps", []) if isinstance(g, str)][:10]}
        for key in ("summary", "sector", "audience"):
            item = value.get(key) or {}
            if not isinstance(item, dict):
                raise ValueError(f"{key} must be an object")
            val, ev = item.get("value"), ok_evidence(item.get("evidence"))
            if val is not None and not isinstance(val, str):
                raise ValueError(f"{key}.value must be a string or null")
            if key == "summary" and val:
                val = val.strip()[:800]
            out[key] = {"value": val if (val and ev) else None, "evidence": ev}
            if val and not ev:
                out["gaps"].append(f"{key} proposed without valid evidence: dropped")
        style = value.get("style") or {}
        if not isinstance(style, dict):
            raise ValueError("style must be an object")
        sev = ok_evidence(style.get("evidence"))
        out["style"] = {
            "tone": style.get("tone") if (isinstance(style.get("tone"), str) and sev) else None,
            "formats": [f for f in style.get("formats", []) if isinstance(f, str)][:6] if sev else [],
            "evidence": sev,
        }
        subs = value.get("subtopics", [])
        if not isinstance(subs, list):
            raise ValueError("subtopics must be a list")
        out["subtopics"] = []
        for s in subs[:12]:
            if not isinstance(s, dict) or not isinstance(s.get("name"), str):
                raise ValueError("each subtopic needs a name")
            kws = [k.lower() for k in s.get("keywords", []) if isinstance(k, str)]
            ev = ok_evidence(s.get("evidence"))
            if kws and ev:
                out["subtopics"].append({"name": s["name"], "keywords": list(dict.fromkeys(kws))[:14], "evidence": ev[:5]})
        return out

    return validate


def enrich(body: dict, snap: SiteSnapshot, core: CoreClient) -> tuple[dict, list]:
    allowed = {p.url for p in snap.posts} | {snap.home_url}
    value, calls = core.chat_json(SYSTEM, _evidence_prompt(snap), make_validator(allowed))
    body = dict(body)
    for key in ("sector", "audience"):
        body[key] = {**value[key], "by": "model" if value[key]["value"] else None}
    body["summary"] = value["summary"]["value"]
    body["style"] = {**body["style"], **value["style"]}
    model_subs = []
    measured = assign_shares(value["subtopics"], snap.posts)  # measured, not claimed
    for s, (share, hits) in zip(value["subtopics"], measured):
        model_subs.append({**s, "share": share, "evidence": list(dict.fromkeys(s["evidence"] + hits))[:5], "by": "model"})
    if model_subs:
        body["subtopics"] = sorted(model_subs, key=lambda s: -s["share"])
    body["gaps"] = list(dict.fromkeys(body["gaps"] + value["gaps"]))
    return body, calls


def build_profile(snap: SiteSnapshot, core: CoreClient | None = None) -> tuple[dict, list]:
    """Profile body plus the model calls made (to record their usage)."""
    body = baseline(snap)
    if snap.status in ("failed", "insufficient"):
        body["gaps"].append("not enough material: sector, audience and style left undetermined")
        return body, []
    if core is None:
        body["gaps"].append("model analysis not run: sector, audience and style left undetermined")
        return body, []
    try:
        return enrich(body, snap, core)
    except CoreError as e:
        log.warning("profile enrichment failed for %s: %s", snap.domain, e)
        body["gaps"].append(f"model analysis failed ({str(e)[:200]}): sector, audience and style left undetermined")
        return body, []


# --- drift between two versions ----------------------------------------------


def _match(sub: dict, candidates: list[dict]) -> dict | None:
    kw = set(sub["keywords"]) | set(tokens(sub["name"]))
    best, best_j = None, 0.3
    for c in candidates:
        ckw = set(c["keywords"]) | set(tokens(c["name"]))
        j = len(kw & ckw) / len(kw | ckw) if kw | ckw else 0
        if j >= best_j:
            best, best_j = c, j
    return best


def drift(old: dict, new: dict, min_share: float = 0.1, min_change: float = 0.15) -> list[dict]:
    """What changed in the site's line, as a list of observations."""
    changes = []
    old_sector, new_sector = (old.get("sector") or {}).get("value"), (new.get("sector") or {}).get("value")
    if new_sector and old_sector and new_sector.strip().lower() != old_sector.strip().lower():
        changes.append({"kind": "sector", "from": old_sector, "to": new_sector, "evidence": new["sector"]["evidence"]})
    if old.get("language") and new.get("language") and old["language"] != new["language"]:
        changes.append({"kind": "language", "from": old["language"], "to": new["language"]})
    old_subs, new_subs = old.get("subtopics", []), new.get("subtopics", [])
    for s in new_subs:
        o = _match(s, old_subs)
        if o is None and s.get("share", 0) >= min_share:
            changes.append({"kind": "emerging", "subtopic": s["name"], "share": s["share"], "evidence": s.get("evidence", [])})
        elif o is not None and s.get("share", 0) - o.get("share", 0) >= min_change:
            changes.append({"kind": "growing", "subtopic": s["name"], "from": o.get("share", 0), "to": s["share"], "evidence": s.get("evidence", [])})
    for o in old_subs:
        n = _match(o, new_subs)
        if o.get("share", 0) >= min_share and (n is None or o["share"] - n.get("share", 0) >= min_change):
            changes.append({"kind": "declining", "subtopic": o["name"], "from": o["share"], "to": n.get("share", 0) if n else 0.0})
    return changes


def compare(old: dict, new: dict) -> list[dict]:
    """Side by side, what a proposed version changes in the approved one:
    rows of {what, before, after, note} for the profile page."""
    rows = []

    def value(body, key):
        return ((body.get(key) or {}).get("value") or "").strip() or None

    for key, label in (("sector", t("Settore")), ("audience", t("Pubblico"))):
        a, b = value(old, key), value(new, key)
        if a != b:
            rows.append({"what": label, "before": a or t("non determinato"), "after": b or t("non determinato")})
    ta, tb = (old.get("style") or {}).get("tone"), (new.get("style") or {}).get("tone")
    if ta != tb:
        rows.append({"what": t("Tono"), "before": ta or t("non determinato"), "after": tb or t("non determinato")})
    la = old.get("languages") or ({old["language"]: None} if old.get("language") else {})
    lb = new.get("languages") or ({new["language"]: None} if new.get("language") else {})
    if set(la) != set(lb):
        rows.append({"what": t("Lingue"), "before": ", ".join(la) or t("non determinate"),
                     "after": ", ".join(lb) or t("non determinate")})
    if bool(old.get("summary")) != bool(new.get("summary")) and new.get("summary"):
        rows.append({"what": t("Descrizione della linea"), "before": t("assente"),
                     "after": t("scritta dal modello sulle pagine lette")})
    old_subs, new_subs = old.get("subtopics", []), new.get("subtopics", [])
    pct = lambda x: f"{round(100 * (x or 0))}%"  # noqa: E731
    for sub in new_subs:
        o = _match(sub, old_subs)
        if o is None:
            rows.append({"what": t("Tema «{name}»", name=sub["name"]), "before": t("non c'era"),
                         "after": t("nuovo, {pct} degli articoli", pct=pct(sub.get("share")))})
        elif abs((sub.get("share") or 0) - (o.get("share") or 0)) >= 0.05 or o["name"] != sub["name"]:
            before = (t("{pct} come «{name}»", pct=pct(o.get("share")), name=o["name"])
                      if o["name"] != sub["name"] else pct(o.get("share")))
            rows.append({"what": t("Tema «{name}»", name=sub["name"]), "before": before, "after": pct(sub.get("share"))})
    gone = [o for o in old_subs if _match(o, new_subs) is None]
    for o in gone:
        rows.append({"what": t("Tema «{name}»", name=o["name"]), "before": pct(o.get("share")), "after": t("esce dal profilo")})
    if gone and sum(1 for o in gone if (o.get("share") or 0) >= 0.9) >= 2:
        rows.append({"what": t("Perché quei temi escono"), "before": "", "after": "",
                     "note": t("Nella versione precedente erano parole chiave presenti su quasi tutte le pagine del sito "
                               "(per questo vicine al 100%): descrivono il sito nel suo insieme, non di cosa parlano gli "
                               "articoli. Ora restano tra le parole chiave del sito e i temi sono misurati sugli articoli.")})
    so, sn = old.get("settings") or {}, new.get("settings") or {}
    for key, label in (("proposals_per_day", t("Proposte al giorno")), ("min_relevance", t("Pertinenza minima"))):
        if key in so and key in sn and so[key] != sn[key]:
            rows.append({"what": label, "before": str(so[key]), "after": str(sn[key])})
    return rows


# --- persistence ---------------------------------------------------------------


def _next_version(s, site_id: int) -> int:
    # Analysis, reviews and manual edits can save a version at the same time.
    quotas.lock(s, f"editor-profile-version:{site_id}")
    v = s.execute(select(func.max(m.editorial_profiles.c.version)).where(m.editorial_profiles.c.site_id == site_id)).scalar()
    return (v or 0) + 1


def _set_settings(s, site_id: int, **values) -> bool:
    """Settings changed in place on the line in use and on a version waiting
    for approval: a setting, not a new version."""
    p = m.editorial_profiles
    rows = s.execute(select(p.c.id, p.c.body).where(p.c.site_id == site_id, p.c.status.in_(("approved", "draft")))).all()
    for r in rows:
        body = dict(r.body)
        body["settings"] = {**DEFAULT_SETTINGS, **(body.get("settings") or {}), **values}
        s.execute(update(p).where(p.c.id == r.id).values(body=body))
    return bool(rows)


def set_proposals_per_day(s, site_id: int, n: int) -> bool:
    """The number of proposals a day."""
    return _set_settings(s, site_id, proposals_per_day=max(0, min(50, int(n))))


def set_cover(s, site_id: int, style: str, text: bool) -> bool:
    """The style of the cover images: "impact" or "editorial", and whether
    an impact cover carries a short headline."""
    if style not in ("impact", "editorial"):
        raise ValueError(style)
    return _set_settings(s, site_id, cover_style=style, cover_text=bool(text) and style == "impact")


def set_sources_style(s, site_id: int, style: str) -> bool:
    """How a published article shows its sources: "notes" (a number after
    each sentence, the notes at the end) or "list" (one line at the end)."""
    if style not in ("notes", "list"):
        raise ValueError(style)
    return _set_settings(s, site_id, sources_style=style)


def approved_profile(s, site_id: int):
    return s.execute(
        select(m.editorial_profiles).where(m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")
    ).first()


def save_draft(s, tenant_id: str, site_id: int, body: dict, origin: str, analysis_id=None, based_on=None, changes=None) -> int:
    return s.execute(
        insert(m.editorial_profiles)
        .values(
            tenant_id=tenant_id, site_id=site_id, version=_next_version(s, site_id), status="draft",
            body=body, origin=origin, analysis_id=analysis_id, based_on=based_on, changes=changes or [],
        )
        .returning(m.editorial_profiles.c.id)
    ).scalar_one()


def approve(s, site_id: int, version: int, by: str) -> bool:
    """Make one version the approved profile of its site; the previous one and older drafts are retired."""
    p = m.editorial_profiles
    row = s.execute(select(p.c.id, p.c.status).where(p.c.site_id == site_id, p.c.version == version)).first()
    if row is None:
        return False
    if row.status == "approved":
        return True
    # The approved version replaces the previous one and the older drafts
    # still waiting: they no longer ask for a decision.
    s.execute(update(p).where(p.c.site_id == site_id, p.c.status == "approved").values(status="retired"))
    s.execute(update(p).where(p.c.site_id == site_id, p.c.status == "draft", p.c.version < version)
              .values(status="retired"))
    s.execute(update(p).where(p.c.id == row.id).values(status="approved", approved_by=by, approved_at=datetime.now(UTC)))
    return True


def edit(s, tenant_id: str, site_id: int, body: dict) -> int:
    """A manual edit becomes a new draft based on the approved version."""
    current = approved_profile(s, site_id)
    if current is not None:
        # Like reanalysis: the owner's settings survive a manual edit that
        # does not mention them (the API only requires a subtopics list).
        body.setdefault("settings", current.body.get("settings", dict(DEFAULT_SETTINGS)))
    changes = drift(current.body, body) if current else []
    return save_draft(s, tenant_id, site_id, body, "manual", based_on=current.version if current else None, changes=changes)


def _without_removed(body: dict) -> dict:
    """The topics the owner took out stay out, also when an analysis finds them again."""
    removed = (body.get("settings") or {}).get("removed") or []
    if removed:
        body["subtopics"] = [x for x in body.get("subtopics", []) if _match(x, removed) is None]
    return body


def _editable(s, site_id: int):
    p = m.editorial_profiles
    rows = s.execute(select(p).where(p.c.site_id == site_id, p.c.status.in_(("draft", "approved")))
                     .order_by(p.c.version.desc())).all()
    return next((x for x in rows if x.status == "draft"), None), next((x for x in rows if x.status == "approved"), None)


def _change_topics(s, tenant_id: str, site_id: int, by: str, change) -> int | None:
    """Apply an owner's choice of topics to the profile on screen.

    A draft waiting for approval is changed in place; an approved profile
    becomes a new approved version, since the owner is the one deciding.
    Returns the version changed, None when there is nothing to change."""
    draft, approved = _editable(s, site_id)
    target = draft or approved
    if target is None:
        return None
    body = copy.deepcopy(dict(target.body))
    body["settings"] = {**DEFAULT_SETTINGS, **(body.get("settings") or {})}
    if not change(body):
        return None
    p = m.editorial_profiles
    if draft is not None:
        changes = drift(approved.body, body) if approved else []
        if draft.origin == "reanalysis" and approved and not changes:
            changes = list(draft.changes or [])
        s.execute(update(p).where(p.c.id == draft.id).values(body=body, changes=changes))
        return draft.version
    pid = save_draft(s, tenant_id, site_id, body, "manual", based_on=approved.version,
                     changes=drift(approved.body, body))
    version = s.execute(select(p.c.version).where(p.c.id == pid)).scalar_one()
    approve(s, site_id, version, by)
    return version


def remove_topic(s, tenant_id: str, site_id: int, name: str, by: str) -> int | None:
    def change(body):
        sub = next((x for x in body.get("subtopics", []) if x.get("name") == name), None)
        if sub is None:
            return False
        body["subtopics"] = [x for x in body["subtopics"] if x is not sub]
        kept = [r for r in body["settings"].get("removed", []) if r.get("name") != name]
        body["settings"]["removed"] = kept + [sub]
        return True
    return _change_topics(s, tenant_id, site_id, by, change)


def restore_topic(s, tenant_id: str, site_id: int, name: str, by: str) -> int | None:
    def change(body):
        removed = body["settings"].get("removed", [])
        sub = next((r for r in removed if r.get("name") == name), None)
        if sub is None:
            return False
        body["settings"]["removed"] = [r for r in removed if r is not sub]
        if all(x.get("name") != name for x in body.get("subtopics", [])):
            body["subtopics"] = sorted(body.get("subtopics", []) + [sub], key=lambda x: -(x.get("share") or 0))
        return True
    return _change_topics(s, tenant_id, site_id, by, change)


def delete_version(s, site_id: int, version: int) -> str | None:
    """Delete a version that is not the line in use. None when deleted, else why not."""
    p = m.editorial_profiles
    row = s.execute(select(p.c.id, p.c.status).where(p.c.site_id == site_id, p.c.version == version)).first()
    if row is None:
        return "not found"
    if row.status == "approved":
        return "approved"
    used = s.execute(select(func.count()).select_from(m.proposals).where(m.proposals.c.profile_id == row.id)).scalar()
    used += s.execute(select(func.count()).select_from(m.opportunity_scores)
                      .where(m.opportunity_scores.c.profile_id == row.id)).scalar()
    if used:
        return "used"
    s.execute(update(m.profile_reviews).where(m.profile_reviews.c.proposed_profile_id == row.id)
              .values(proposed_profile_id=None))
    s.execute(delete(p).where(p.c.id == row.id))
    return None


def store_snapshot(s, tenant_id: str, site_id: int, snap: SiteSnapshot) -> int:
    analysis_id = s.execute(
        insert(m.site_analyses)
        .values(
            tenant_id=tenant_id, site_id=site_id, status=snap.status, snapshot=snap.to_dict(),
            problems=snap.problems, pages_fetched=snap.pages_fetched,
        )
        .returning(m.site_analyses.c.id)
    ).scalar_one()
    for p in snap.posts:
        s.execute(
            insert(m.site_posts)
            .values(
                tenant_id=tenant_id, site_id=site_id, url=p.url, url_hash=url_hash(p.url), title=p.title,
                summary=p.summary, categories=p.categories, published_at=p.published_at,
                minhash=minhash(f"{p.title} {p.summary}"),
            )
            .on_conflict_do_nothing(index_elements=["site_id", "url_hash"])
        )
    return analysis_id


# An analysis the model could not finish is tried again by the hourly tick,
# waiting longer each time: 1, 1, 2, 4, 8 hours, then the Monday review.
RETRY_PER_DAY = 6


def retry_due(s, site_id: int, now: datetime) -> bool:
    """Whether the site's last reading was left incomplete by the model (a
    profile waiting with "model analysis failed", or a review of an approved
    line where the model did not answer) and it is time to try again."""
    a = m.site_analyses.c
    starts = list(s.execute(select(a.started_at).where(
        a.site_id == site_id, a.started_at >= now - timedelta(days=1))).scalars())
    if not starts or len(starts) >= RETRY_PER_DAY:
        return False
    last = max(starts)
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    if now - last < timedelta(minutes=50) * 2 ** max(len(starts) - 2, 0):
        return False
    if approved_profile(s, site_id) is not None:
        r = m.profile_reviews.c
        drift_ = s.execute(select(r.drift).where(r.site_id == site_id).order_by(r.id.desc()).limit(1)).scalar()
        if isinstance(drift_, str):
            drift_ = json.loads(drift_)
        return bool((drift_ or {}).get("model_failed"))
    p = m.editorial_profiles.c
    body = s.execute(select(p.body).where(p.site_id == site_id, p.status == "draft")
                     .order_by(p.id.desc()).limit(1)).scalar()
    if isinstance(body, str):
        body = json.loads(body)
    return any(g.startswith("model analysis failed") for g in (body or {}).get("gaps") or [])


def analyse_site(db, tenant_id: str, domain: str, crawler, core: CoreClient | None = None) -> dict:
    """First analysis or periodic review of a site. Returns what happened."""
    snap = crawler.analyse(domain)
    if snap.status not in ("failed", "insufficient") and core is not None:
        step(N_("Il modello ricava settore, temi, pubblico e tono da {n} articoli letti (di solito 1-3 minuti)"),
             n=len(snap.posts))
    with db.tenant(tenant_id) as s:
        known = s.execute(select(m.sites.c.id).where(m.sites.c.domain == snap.domain)).scalar()
    with usage.tagged(db, tenant_id, "site_profile", site_id=known, ref=f"site:{snap.domain}"):
        body, calls = build_profile(snap, core)
    with db.tenant(tenant_id) as s:
        repo.ensure_tenant(s, tenant_id, tenant_id)
        site_id = repo.ensure_site(s, tenant_id, snap.domain, body["name"], snap.language or "und")
        analysis_id = store_snapshot(s, tenant_id, site_id, snap)
        current = approved_profile(s, site_id)
        draft, _ = _editable(s, site_id)
        # Topics taken out stay out, whether on the line in use or on a proposal.
        removed = []
        for prev in (current, draft):
            for r in ((prev.body.get("settings") or {}).get("removed") or []) if prev else []:
                if all(r.get("name") != x.get("name") for x in removed):
                    removed.append(r)
        if removed:
            body["settings"] = {**body["settings"], "removed": removed}
            body = _without_removed(body)
        model_failed = next((g for g in body["gaps"] if g.startswith("model analysis failed")), None)
        result = {"site_id": site_id, "analysis_id": analysis_id, "status": snap.status, "problems": snap.problems,
                  "profile_id": None, "changes": [], "significant": False, "model_failed": model_failed}
        if current is None:
            if snap.status != "failed":
                # The newer analysis replaces the drafts of the older ones
                # still waiting: one profile per site asks for a decision.
                p = m.editorial_profiles
                s.execute(update(p).where(p.c.site_id == site_id, p.c.status == "draft", p.c.origin == "analysis")
                          .values(status="retired"))
                result["profile_id"] = save_draft(s, tenant_id, site_id, body, "analysis", analysis_id)
            return result
        # A model that did not answer says nothing about the site: the line
        # in use stays, and the reading is tried again (retry_due).
        changes = drift(current.body, body) if snap.status in ("complete", "partial") and not model_failed else []
        if not changes and snap.status in ("complete", "partial") and not model_failed and (
                current.body.get("method") != METHOD
                or (not (current.body.get("sector") or {}).get("value") and (body.get("sector") or {}).get("value"))):
            changes = [{"kind": "method"}]  # same site, better reading of it
        result["changes"], result["significant"] = changes, bool(changes)
        proposed = None
        if changes:
            body["settings"] = current.body.get("settings", body["settings"])  # owner's settings are kept
            # The newer reading replaces the proposals of older ones still waiting.
            p = m.editorial_profiles
            s.execute(update(p).where(p.c.site_id == site_id, p.c.status == "draft", p.c.origin == "reanalysis")
                      .values(status="retired"))
            proposed = save_draft(s, tenant_id, site_id, body, "reanalysis", analysis_id, current.version, changes)
            result["profile_id"] = proposed
        s.execute(
            insert(m.profile_reviews).values(
                tenant_id=tenant_id, site_id=site_id, analysis_id=analysis_id,
                drift={"changes": changes, **({"model_failed": model_failed} if model_failed else {})},
                significant=bool(changes), proposed_profile_id=proposed,
            )
        )
        return result
