"""Automatic newsroom: a site that writes and publishes by itself, inside
the brief, the directives and the guardrails its owner set.

Every hourly round, inside the site's hours, the best proposals that pass
the guardrails are accepted and go to an `auto` job: the draft is written
with the usual checks (every sentence sourced, numbers found in the
sources, nothing copied), it gets its cover, an AI editor-in-chief reads it
against the guardrails, and only then is it approved and sent to the
site's destination. Nobody reads it first, so it always carries the AI
label (AI Act, art. 50(4)). An article the editor-in-chief stops stays a
draft, with the reasons, for a person to look at. One switch stops all.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func, select, update

from . import drafts, models as m, proposals, publishing, translate as tr, usage
from .core_client import CoreClient, CoreError
from .i18n import N_
from .progress import step

log = logging.getLogger(__name__)

AUTO = "redazione automatica"  # who decides, on proposals, drafts and publications
LANGS = ("it", "en", "de", "fr", "es")

DEFAULTS = {"enabled": False, "brief": "", "sections": [], "directives": "", "avoid": [], "per_day": 6,
            "min_opportunity": 40.0, "min_sources": 2, "hours": [7, 23], "target_id": None, "credit": True}

# What the AI editor-in-chief never lets through, whatever the directives say.
RULES = [
    "accuses a named person or company of a crime or wrongdoing as a fact, when the cited sources only report "
    "it as an allegation or do not attribute it to an official source",
    "names or exposes a private individual who is not a public figure, or a minor",
    "gives the reader medical, legal or financial advice (reporting what experts or authorities say is fine)",
    "insults, mocks or spreads hate against people for their origin, religion, gender, disability or orientation",
    "describes violence, suicide or self-harm in graphic detail",
    "presents a rumour or a single unconfirmed claim as an established fact",
    "has a title that promises something the article does not say",
]

CHECK_SYSTEM = (
    "You are the editor-in-chief of a news site whose articles are published without human review. Decide "
    "whether the article below can be published as it is. Refuse it if it breaks any of these rules: "
    + "; ".join(f"({n}) {r}" for n, r in enumerate(RULES, 1))
    + ". Refuse it also if it goes against the newsroom's directives or is about a topic it must avoid. Do not "
    "refuse for style, length or a dull title. Reply with JSON only: "
    '{"publish": true|false, "problems": ["<each rule broken, one sentence each, in English>"], '
    '"section": "<the one of the site\'s sections the article belongs to, written exactly as listed, or null>"}'
)

LABELS = {
    "it": "Articolo scritto con l'intelligenza artificiale a partire dalle fonti citate, senza revisione umana.",
    "en": "This article was written by artificial intelligence from the sources cited, without human review.",
    "de": "Dieser Artikel wurde von künstlicher Intelligenz auf Grundlage der genannten Quellen verfasst, "
          "ohne menschliche Prüfung.",
    "fr": "Cet article a été rédigé par une intelligence artificielle à partir des sources citées, "
          "sans relecture humaine.",
    "es": "Este artículo fue escrito por inteligencia artificial a partir de las fuentes citadas, "
          "sin revisión humana.",
}
CREDITS = {  # after the label, when the owner keeps the credit on
    "it": "Redazione automatica di [Redazia](https://redazia.com).",
    "en": "Automatic newsroom by [Redazia](https://redazia.com).",
    "de": "Automatische Redaktion von [Redazia](https://redazia.com).",
    "fr": "Rédaction automatique par [Redazia](https://redazia.com).",
    "es": "Redacción automática de [Redazia](https://redazia.com).",
}


class _Auto(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    brief: str = Field("", max_length=4000)
    sections: list[str] = Field(default_factory=list, max_length=30)
    directives: str = Field("", max_length=4000)
    avoid: list[str] = Field(default_factory=list, max_length=100)
    per_day: int = Field(DEFAULTS["per_day"], ge=1, le=48)
    min_opportunity: float = Field(DEFAULTS["min_opportunity"], ge=0, le=100)
    min_sources: int = Field(DEFAULTS["min_sources"], ge=1, le=3)
    hours: list[int] = Field(DEFAULTS["hours"], min_length=2, max_length=2)
    target_id: int | None = None
    credit: bool = True


def check_settings(values: dict) -> dict:
    """The settings as stored, or ValueError saying what is wrong."""
    try:
        cfg = _Auto.model_validate(values).model_dump()
    except ValidationError as e:
        raise ValueError("; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                                   for err in e.errors()[:3])) from None
    start, end = cfg["hours"]
    if not (0 <= start <= 23 and 1 <= end <= 24 and start != end):
        raise ValueError("hours: from 0-23 to 1-24")
    cfg["sections"] = [x.strip()[:80] for x in cfg["sections"] if x.strip()]
    cfg["avoid"] = list(dict.fromkeys(x.strip().lower()[:80] for x in cfg["avoid"] if x.strip()))
    return cfg


def settings_of(body: dict | None) -> dict:
    return {**DEFAULTS, **(((body or {}).get("settings") or {}).get("auto") or {})}


def save(s, site_id: int, values: dict) -> bool:
    """Store the automatic newsroom's settings on the profile in use and on
    the version waiting for approval; False when the site has neither."""
    from .profile import _set_settings

    return _set_settings(s, site_id, auto=check_settings(values))


def brief_lines(body: dict | None) -> list[str]:
    """The newsroom's brief and directives, for the prompts that plan and
    write its articles; set with or without the automatic mode."""
    cfg = settings_of(body)
    out = []
    if cfg["brief"]:
        out.append(f"Newsroom brief: {cfg['brief'][:2000]}")
    if cfg["sections"]:
        out.append(f"Sections of the site: {', '.join(cfg['sections'])}")
    if cfg["directives"]:
        out.append(f"Directives of the newsroom: {cfg['directives'][:2000]}")
    if cfg["avoid"]:
        out.append(f"Never write about: {', '.join(cfg['avoid'])}")
    return out


def avoided(text: str, avoid: list[str]) -> str | None:
    """The first topic to avoid the text names, or None."""
    low = (text or "").lower()
    for word in avoid:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low):
            return word
    return None


def in_hours(cfg: dict, local: datetime) -> bool:
    start, end = cfg["hours"]
    return start <= local.hour < end if start < end else (local.hour >= start or local.hour < end)


def hours_left(cfg: dict, local: datetime) -> int:
    """Hours from this one to the end of the window, this one included."""
    n, h = 0, local.hour
    while n < 24 and in_hours(cfg, local.replace(hour=h)):
        n, h = n + 1, (h + 1) % 24
    return max(1, n)


def made_since(s, site_id: int, since: datetime) -> int:
    """Articles the automatic newsroom took on since the given time: those
    written, and those accepted and still being written."""
    pr, dr = m.proposals.c, m.drafts.c
    written = s.execute(select(func.count()).select_from(m.drafts.join(m.proposals, pr.id == dr.proposal_id)).where(
        pr.site_id == site_id, pr.decided_by == AUTO, dr.translation_of.is_(None), dr.created_at >= since)).scalar()
    writing = s.execute(select(func.count()).select_from(m.proposals).where(
        pr.site_id == site_id, pr.decided_by == AUTO, pr.status == "accepted",
        pr.created_at >= since - timedelta(days=1))).scalar()
    return (written or 0) + (writing or 0)


def source_count(s, proposal_id: int) -> int:
    """Distinct sources behind a proposal's cited news."""
    pc, si = m.proposal_citations.c, m.source_items.c
    return s.execute(select(func.count(func.distinct(si.source_id))).select_from(
        m.proposal_citations.join(m.source_items, si.id == pc.item_id)).where(pc.proposal_id == proposal_id)).scalar() or 0


def choose(s, site_id: int, cfg: dict, now: datetime, n: int) -> list:
    """The best proposals of the last day and a half that pass the
    guardrails; those on a topic to avoid are turned down for good."""
    pr = m.proposals.c
    rows = s.execute(select(m.proposals).where(
        pr.site_id == site_id, pr.status == "proposed", pr.created_at >= now - timedelta(hours=36),
        pr.opportunity >= cfg["min_opportunity"]).order_by(pr.opportunity.desc(), pr.id.desc()).limit(40)).all()
    out = []
    for r in rows:
        if len(out) >= n:
            break
        word = avoided(f"{r.title} {r.angle or ''}", cfg["avoid"])
        if word:
            s.execute(update(m.proposals).where(pr.id == r.id, pr.status == "proposed")
                      .values(status="rejected", decided_by=AUTO))
            proposals.record_feedback(s, r.tenant_id, "proposal", r.id, "down", AUTO, f"topic to avoid: {word}", site_id)
            continue
        if source_count(s, r.id) >= cfg["min_sources"]:
            out.append(r)
    return out


def run(db, tenant_id: str, site_id: int, core: CoreClient | None, now: datetime, tz: str, enqueue) -> dict:
    """One hour of the automatic newsroom for a site: accept the articles
    of this hour and queue them. `enqueue(proposal_id)` queues one."""
    with db.tenant(tenant_id) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
    cfg = settings_of(body)
    if body is None or not cfg["enabled"]:
        return {}
    local = now.astimezone(ZoneInfo(tz))
    if not in_hours(cfg, local):
        return {"waiting": "hours"}
    since = datetime.combine(local.date(), time(), local.tzinfo).astimezone(UTC)
    with db.tenant(tenant_id) as s:
        left = cfg["per_day"] - made_since(s, site_id, since)
        if left <= 0:
            return {"done": cfg["per_day"]}
        # Spread over the hours left, so the day does not go in the first hour.
        want = math.ceil(left / hours_left(cfg, local))
        picked = choose(s, site_id, cfg, now, want)
        planned_today = proposals.made_today(s, site_id, since)
    if len(picked) < want and core is not None and planned_today < cfg["per_day"] * 4:
        proposals.plan(db, tenant_id, site_id, core, limit=max(3, 2 * want), now=now)
        with db.tenant(tenant_id) as s:
            picked = choose(s, site_id, cfg, now, want)
    accepted = []
    for r in picked:
        if proposals.decide(db, tenant_id, r.id, True, AUTO, "automatic newsroom"):
            enqueue(r.id)
            accepted.append(r.id)
    return {"accepted": accepted}


def add_label(body_md: str, language: str | None, credit: bool = True) -> str:
    """The AI label, before the sources; once."""
    lang = (language or "")[:2]
    label = LABELS.get(lang, LABELS["en"])
    if label in body_md:
        return body_md
    line = " " + CREDITS.get(lang, CREDITS["en"]) if credit else ""
    for heading in ("\n## Fonti", "\n## Sources"):
        at = body_md.rfind(heading)
        if at >= 0:
            return f"{body_md[:at]}\n_{label}{line}_\n{body_md[at:]}"
    return f"{body_md.rstrip()}\n\n_{label}{line}_\n"


def _check_validator(v) -> dict:
    if not isinstance(v, dict) or not isinstance(v.get("publish"), bool):
        raise ValueError('expected {"publish": true|false, "problems": [...]}')
    problems = [str(x)[:300] for x in v.get("problems") or [] if str(x).strip()][:6]
    if not v["publish"] and not problems:
        problems = ["refused without a reason"]
    return {"publish": v["publish"], "problems": problems, "section": str(v.get("section") or "").strip()[:80]}


def editor_check(db, tenant_id: str, d, body: dict, core: CoreClient) -> dict:
    """The AI editor-in-chief's verdict on a draft."""
    lines = brief_lines(body) or ["Newsroom brief: none given"]
    prompt = "\n".join(lines + [f"Language: {d.language}", "Article:", d.body_md[:14000]])
    with usage.tagged(db, tenant_id, "editor_check", site_id=d.site_id, ref=f"draft:{d.id}"):
        verdict, _calls = core.chat_json(CHECK_SYSTEM, prompt, _check_validator)
    return verdict


def target_for(s, site_id: int, cfg: dict, language: str | None, main: bool = True):
    """Where an article in this language goes: a destination set for the
    language, else, for the site's own language, one set for none. The
    one chosen in the settings wins among them."""
    t = m.publish_targets.c
    rows = s.execute(select(m.publish_targets).where(
        t.site_id == site_id, t.enabled.is_not(False), t.kind.in_(("github", "wordpress", "webhook")))
        .order_by(t.id)).all()
    exact = [r for r in rows if (r.config or {}).get("language") == language]
    fits = exact or ([r for r in rows if not (r.config or {}).get("language")] if main else [])
    return next((r for r in fits if r.id == cfg.get("target_id")), fits[0] if fits else None)


def _send(db, tenant_id: str, d, cfg: dict, main: bool, publish) -> dict:
    """Label, approve and publish a draft that passed."""
    with db.tenant(tenant_id) as s:
        s.execute(update(m.drafts).where(m.drafts.c.id == d.id).values(
            body_md=add_label(d.body_md, d.language, cfg["credit"])))
    drafts.decide(db, tenant_id, d.id, True, AUTO)
    with db.tenant(tenant_id) as s:
        target = target_for(s, d.site_id, cfg, d.language, main)
    if target is None:
        return {"approved": True}
    step(N_("Pubblicazione su {name}"), name=target.name)
    pub_id = publishing.request(db, tenant_id, d.id, target.id, "publish", AUTO)
    publishing.decide(db, tenant_id, pub_id, True, AUTO)
    publish(pub_id)
    return {"approved": True, "target": target.name, "publication_id": pub_id}


def finish(db, tenant_id: str, draft_id: int, core: CoreClient, publish, translate=lambda language: None) -> dict:
    """After the draft: the guardrails, the editor-in-chief, the label, the
    approval and the publication. `publish(pub_id)` queues the sending,
    `translate(language)` a translation into each of the site's languages."""
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
        cfg = settings_of(body)
        cited = s.execute(select(func.count(func.distinct(m.source_items.c.source_id))).select_from(
            m.draft_claim_sources.join(m.draft_claims, m.draft_claims.c.id == m.draft_claim_sources.c.claim_id)
            .join(m.source_items, m.source_items.c.id == m.draft_claim_sources.c.item_id))
            .where(m.draft_claims.c.draft_id == draft_id)).scalar() or 0
    out = {"draft_id": draft_id, "title": d.title, "site_id": d.site_id}
    held = []
    if not cfg["enabled"]:
        held.append("the automatic newsroom was stopped")
    if d.status == "needs_review":
        held.append("some sentences failed the source checks")
    if cited < cfg["min_sources"]:
        held.append(f"{cited} sources cited, {cfg['min_sources']} needed")
    word = avoided(f"{d.title}\n{d.body_md}", cfg["avoid"])
    if word:
        held.append(f"topic to avoid: {word}")
    if not held:
        step(N_("Il caporedattore AI rilegge l'articolo"))
        try:
            verdict = editor_check(db, tenant_id, d, body, core)
        except CoreError as e:
            held.append(f"the editor-in-chief did not answer: {e}")
        else:
            held += verdict["problems"] if not verdict["publish"] else []
            section = next((x for x in cfg["sections"] if x.lower() == verdict["section"].lower()), None)
            if section:  # the site files the article under it
                with db.tenant(tenant_id) as s:
                    s.execute(update(m.drafts).where(m.drafts.c.id == draft_id).values(
                        seo={**(d.seo or {}), "section": section}))
    if held:
        with db.tenant(tenant_id) as s:
            proposals.record_feedback(s, tenant_id, "draft", draft_id, "down", AUTO, "; ".join(held)[:1000], d.site_id)
        return {**out, "held": held}
    res = {**out, **_send(db, tenant_id, d, cfg, True, publish)}
    with db.tenant(tenant_id) as s:
        langs = tr.missing(s, draft_id)
    for language in langs:
        translate(language)
    if langs:
        res["translations"] = langs
    return res


def finish_translation(db, tenant_id: str, draft_id: int, publish) -> dict:
    """A translation of an article the editor-in-chief let through: the
    same sentences and sources, so it only has to pass its own checks."""
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
    cfg = settings_of(body)
    out = {"draft_id": draft_id, "title": d.title, "site_id": d.site_id, "language": d.language}
    held = []
    if not cfg["enabled"]:
        held.append("the automatic newsroom was stopped")
    if d.status == "needs_review":
        held.append("some sentences failed the source checks")
    if held:
        with db.tenant(tenant_id) as s:
            proposals.record_feedback(s, tenant_id, "draft", draft_id, "down", AUTO, "; ".join(held), d.site_id)
        return {**out, "held": held}
    return {**out, **_send(db, tenant_id, d, cfg, False, publish)}


def today(s, site_id: int, since: datetime) -> dict:
    """The automatic newsroom's day for a site, for its page."""
    pr, dr, f = m.proposals.c, m.drafts.c, m.feedback.c
    auto_drafts = (select(dr.id, dr.title, dr.status, dr.created_at)
                   .join(m.proposals, pr.id == dr.proposal_id)
                   .where(pr.site_id == site_id, pr.decided_by == AUTO, dr.translation_of.is_(None),
                          dr.created_at >= since).order_by(dr.id.desc()))
    rows = s.execute(auto_drafts).all()
    notes = dict(s.execute(select(f.target_id, f.note).where(
        f.target == "draft", f.given_by == AUTO, f.target_id.in_([r.id for r in rows] or [0]))).all())
    pubs = dict(s.execute(select(m.publications.c.draft_id, m.publications.c.status).where(
        m.publications.c.draft_id.in_([r.id for r in rows] or [0]))).all())
    items = [{"id": r.id, "title": r.title, "status": r.status, "held": notes.get(r.id),
              "publication": pubs.get(r.id)} for r in rows]
    return {"items": items, "published": sum(1 for x in items if x["publication"] == "published"),
            "held": sum(1 for x in items if x["held"])}


BRIEF_SYSTEM = (
    "You set up the editorial profile of a news site from its owner's brief. Use only what the brief and the "
    "sections say; where they say nothing, choose what a site like this would do and keep it plain. Write the "
    "values in the site's language. Reply with JSON only: "
    '{"name": "<the site\'s name>", "summary": "<two or three sentences on what it publishes, for whom and how>", '
    '"sector": "<sector>", "audience": "<who reads it>", "tone": "<tone>", '
    '"formats": ["news|guide|review|analysis|opinion|tutorial|interview|data"], '
    '"subtopics": [{"name": "<one per section, or the main subjects when no sections are given>", '
    '"keywords": ["<lowercase keywords: 4-8 in the site language, then the same ones in English, so news in '
    'both languages are found; a site in English lists them once>"]}]}'
)


def _brief_validator(v) -> dict:
    if not isinstance(v, dict):
        raise ValueError("expected an object")
    subs = []
    for x in v.get("subtopics") or []:
        if isinstance(x, dict) and str(x.get("name") or "").strip():
            kw = [str(k).strip().lower()[:60] for k in x.get("keywords") or [] if str(k).strip()][:16]
            subs.append({"name": str(x["name"]).strip()[:80], "keywords": kw})
    if not subs or not str(v.get("sector") or "").strip():
        raise ValueError("sector and at least one subtopic are needed")
    fmts = [f for f in v.get("formats") or [] if f in proposals.FORMATS] or ["news", "analysis"]
    return {k: str(v.get(k) or "").strip()[:600] for k in ("name", "summary", "sector", "audience", "tone")} | {
        "formats": fmts, "subtopics": subs[:30]}


def profile_from_brief(db, tenant_id: str, site_id: int, language: str, core: CoreClient,
                       cfg: dict | None = None) -> int:
    """A profile written from the owner's brief, for a site with nothing
    to read yet (a new site): a version to approve like any other."""
    from . import profile as prof

    with db.tenant(tenant_id) as s:
        site = s.execute(select(m.sites.c.domain, m.sites.c.name).where(m.sites.c.id == site_id)).first()
        current = prof.approved_profile(s, site_id)
        base = dict(current.body) if current else {}
    cfg = check_settings(cfg if cfg is not None else settings_of(base))
    if not cfg["brief"].strip():
        raise ValueError("the brief is empty")
    prompt = "\n".join([f"Domain: {site.domain}", f"Language: {language}", f"Brief: {cfg['brief']}",
                        f"Sections: {', '.join(cfg['sections']) or 'not given'}",
                        f"Directives: {cfg['directives'] or 'none'}"])
    with usage.tagged(db, tenant_id, "profile", site_id=site_id, ref="brief"):
        v, _calls = core.chat_json(BRIEF_SYSTEM, prompt, _brief_validator)
    share = round(1 / len(v["subtopics"]), 3)
    body = {
        **base,
        "domain": site.domain, "method": prof.METHOD, "name": v["name"] or site.name or site.domain,
        "language": language, "languages": {language: 1},
        "summary": {"value": v["summary"] or None, "evidence": [], "by": "brief"},
        "sector": {"value": v["sector"], "evidence": [], "by": "brief"},
        "audience": {"value": v["audience"] or None, "evidence": [], "by": "brief"},
        "style": {**(base.get("style") or {}), "tone": v["tone"] or None, "formats": v["formats"], "evidence": []},
        "subtopics": [{**x, "share": share, "evidence": []} for x in v["subtopics"]],
        "gaps": [],
        "settings": {**prof.DEFAULT_SETTINGS, **(base.get("settings") or {}), "auto": cfg},
    }
    with db.tenant(tenant_id) as s:
        if site.name in (None, "", site.domain) and v["name"]:
            s.execute(update(m.sites).where(m.sites.c.id == site_id).values(name=v["name"][:200]))
        if language in LANGS:
            s.execute(update(m.sites).where(m.sites.c.id == site_id, m.sites.c.language.in_(("und", "")))
                      .values(language=language))
        pid = prof.edit(s, tenant_id, site_id, body)
        return s.execute(select(m.editorial_profiles.c.version).where(m.editorial_profiles.c.id == pid)).scalar_one()
