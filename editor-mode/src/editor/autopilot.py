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
from sqlalchemy import Text, and_, case, cast, func, literal, or_, select, update

from . import credits, drafts, models as m, proposals, publishing, quotas, translate as tr, usage
from .core_client import CoreClient, CoreError
from .evergreen import is_evergreen
from .i18n import N_, t
from .progress import step
from .text import tokens

log = logging.getLogger(__name__)

AUTO = "redazione automatica"  # who decides, on proposals, drafts and publications
LANGS = ("it", "en", "de", "fr", "es")

DEFAULTS = {"enabled": False, "brief": "", "sections": [], "directives": "", "avoid": [], "per_day": 6,
            "min_opportunity": 40.0, "min_sources": 2, "hours": [7, 23], "target_id": None, "credit": True,
            "on_problem": "fix", "morning_share": 50,
            # Beyond the day's articles: how many it may bring forward from tomorrow (free), and how many
            # more a day it may pay with credits, for news over `extra_min_opportunity`.
            "borrow": 2, "extra_credits": 0, "extra_min_opportunity": 65.0,
            # A last section that reads the facts: connects them, weighs them (drafts.ANALYSIS).
            "analysis": True,
            # Breaking news: a story that many outlets tell within a few hours goes out even when the
            # day's articles are done or out of hours, brought forward from tomorrow (breaking_news).
            "breaking": False, "breaking_outlets": 3, "breaking_hours": 3,
            # No foreign country beyond this share of the day's articles (choose); the site's own country
            # is free: `home_country`, or the one of its language.
            "country_share": 34, "home_country": "",
            # Nor a section beyond this share, when the site lists its sections.
            "section_share": 25}
# "fix": an article with problems is written again once, by the stronger model when the console sets one,
# and discarded if it still does not pass: nothing waits for a person. "hold": it waits for a person.
ON_PROBLEM = ("fix", "hold")
FIX_ROUNDS = 1

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
    "joins separate news events into one article (two companies, two incidents, a figure and an unrelated "
    "deal) instead of telling one event",
    "states a number, a record or superlative (fastest, biggest, first, only) or a cause that the cited sources "
    "do not state, or a number without what it is compared to when the sources give it",
    "has a title, subtitle or opening more certain, larger or newer than the body and the sources (a forecast, "
    "an allegation or a study's result told as a fact, an old event told as new)",
    "does not give the essential information its title promises (the figure, the decision, what the study "
    "found), or tells context (a precedent, a comparison) as a second story",
    "talks about its own sources or its own checking instead of the event (headings or sentences such as "
    "\"what is confirmed\", \"the sources consulted\", \"the available sources do not say\", \"it cannot be "
    "said here\")",
    "gives a central number or a record that differs from an original document among the sources (an "
    "official statistic, a decision, a paper): the document wins over the outlets reporting it",
    "in its analysis or commentary, states as news a cause, a consequence, a forecast, a number or a fact that "
    "no source gives, or presents the newsroom's own reading as an established fact",
    # Francesco 2026-11-11: an energy supplier's offer "expiring today" went out as news (the day after).
    "is in substance an advertisement: its subject is one company's commercial offer, promotion, discount, "
    "tariff, price list, coupon or sales deadline, or it reads as a sponsored text or a press release selling a "
    "product (market news that concerns companies, a regulator's decision on prices or a comparison of many "
    "offers is fine)",
    "tells as still open, or as coming, a deadline, an offer or an event that has already ended by the date of "
    "publication given",
]

def _check_system(rules: list[str], what: str, joined: str, cuts: str = "six") -> str:
    return (
    "You are the editor-in-chief of a news site whose articles are published without human review. Decide "
    f"whether the {what} below can be published as it is. Refuse it if it breaks any of these rules: "
    + "; ".join(f"({n}) {r}" for n, r in enumerate(rules, 1))
    + ". Refuse it also if it goes against the newsroom's directives or is about a topic it must avoid. Do not "
    "refuse for style, length or a dull title. Check every number, superlative and cause against the source "
    "excerpts given after the article; a source given only by its title cannot support details. Before "
    "calling two statements a contradiction, check that they refer to the same moment (a count at 71% of the "
    "votes is not the final result, a forecast is not the outcome), the same measure and the same scope (the "
    "first Black woman governor of a US state is not the first Black governor of Georgia): different moments, "
    "measures or scopes are not an error. A live page read at a given time says what was true then. Reply with JSON "
    "only: "
    '{"publish": true|false, "problems": ["<each rule broken, one sentence each, written for the newsroom\'s '
    'editor in the language named after Reasons in, in plain words: say what is wrong in the article, never '
    'a rule\'s number>"], '
    '"section": "<the one of the site\'s sections the article belongs to, written exactly as listed, or null>", '
    '"headline_fixes": {"title": "<the title with its typos corrected, or null>", "subtitle": "<same, or null>", '
    '"seo_title": "<same, or null>", "seo_description": "<same, or null>"}}. headline_fixes is a last proofread '
    "of the Headlines given after the article: correct only a clear typo, a missing or doubled word or a grammar "
    "error (\"i Senato\" for \"il Senato\"), in the same language, changing nothing else; null when it is right. "
    'Add "cut": ["<sentence copied exactly from the article>"] when removing up to ' + cuts + ' sentences fixes every '
    "problem (a detail the sources do not support, a sentence about the sources, a reading of the newsroom "
    "told as news in the last section that reads the facts: cut those sentences of it): then set publish to true, "
    "because the article goes out without them, shorter. Refuse only what removing sentences cannot fix: the event "
    f"itself wrong, the title or the opening wrong{joined}"
    )


# The article tells again a story the site told in the last two days.
REPEATS = (
    ". The site's articles of the last two days are listed after the sources: add \"repeats\": \"<the title of "
    "the listed article, copied exactly>\" when this article tells the same story again with no new fact in its "
    "title and opening, and set publish to false; a new development (a decision, a result, an answer, a "
    "number that is news itself) is not a repeat, and then repeats is null")
CHECK_SYSTEM = _check_system(RULES, "article", ", two stories joined") + REPEATS

# An article on a theme (evergreen.py) or asked for by the newsroom
# (requested.py) brings several items together by design: the rule on one
# event per article does not apply; what it must not do is make up the
# links between them.
THEMED_RULES = [r for r in RULES if not r.startswith("joins separate")] + [
    "links facts from different sources with a cause, an effect or a comparison that none of the sources states, "
    "written as a fact",
]
THEMED_CHECK_SYSTEM = _check_system(THEMED_RULES, "article (a guide or an explainer on a theme, which may bring "
                                    "several sources together)", "") + REPEATS

# The evening analysis joins the day's stories by design (evening.py): the
# rules on one event per article do not apply; what it must not do is make
# up the links between them.
EVENING_RULES = [r for r in RULES if not r.startswith(("joins separate", "does not give the essential"))] + [
    "joins two stories with a cause, an effect or an influence that none of the articles states, written as a "
    "fact instead of as the newsroom's reading",
    "gives a fact, a number, a name, a quote or a date that none of the articles gives",
]
EVENING_CHECK_SYSTEM = _check_system(EVENING_RULES, "evening analysis of the day's articles (the sources are the "
                                     "site's own articles)", "", "twelve")
# The evening analysis is long (600-1200 words): it may lose more sentences and still be the
# same piece (Francesco 2026-11-13: two evenings in a row left as a draft for "more than six cuts").
EVENING_CUT_MAX = 12

# A story only one outlet told is written from that one (Francesco 2026-11-05: the number of
# outlets is a preference): the editor-in-chief must not refuse it for that alone, even when the
# directives ask for more (2026-11-13: five articles of a day discarded after they were paid for).
ONE_OUTLET = ("Outlets: only one outlet told this story, so the article cites that one. The newsroom's wish "
              "for more outlets is a preference: citing one outlet is not a reason to refuse, nor is the "
              "article following that outlet's account; refuse only for the other rules.")


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
    on_problem: str = Field("fix", pattern="^(fix|hold)$")
    morning_share: int = Field(DEFAULTS["morning_share"], ge=0, le=100)
    borrow: int = Field(DEFAULTS["borrow"], ge=0, le=20)
    extra_credits: int = Field(DEFAULTS["extra_credits"], ge=0, le=20)
    extra_min_opportunity: float = Field(DEFAULTS["extra_min_opportunity"], ge=0, le=100)
    analysis: bool = True
    breaking: bool = False
    breaking_outlets: int = Field(DEFAULTS["breaking_outlets"], ge=2, le=10)
    breaking_hours: int = Field(DEFAULTS["breaking_hours"], ge=1, le=12)
    country_share: int = Field(DEFAULTS["country_share"], ge=0, le=100)
    home_country: str = Field("", max_length=2)
    section_share: int = Field(DEFAULTS["section_share"], ge=0, le=100)


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
    cfg["home_country"] = cfg["home_country"].strip().upper()
    if cfg["home_country"] and not proposals._ISO.match(cfg["home_country"]):
        raise ValueError("home_country: two letters, like IT")
    cfg["sections"] = [x.strip()[:80] for x in cfg["sections"] if x.strip()]
    cfg["avoid"] = list(dict.fromkeys(x.strip().lower()[:80] for x in cfg["avoid"] if x.strip()))
    return cfg


def settings_of(body: dict | None) -> dict:
    """The automatic newsroom's settings, with the rubrics the newsroom
    itself added (`added_sections`) and the weight of each rubric for the
    week (`section_weights`), both from rubrics.py: never written back."""
    st = (body or {}).get("settings") or {}
    cfg = {**DEFAULTS, **(st.get("auto") or {})}
    rb = st.get("rubrics") or {}
    own = {x.lower() for x in cfg.get("sections") or []}
    on = rb.get("enabled") if rb.get("enabled") is not None else cfg.get("enabled")
    cfg["added_sections"] = [x["name"] for x in rb.get("added") or []
                             if isinstance(x, dict) and x.get("name") and x["name"].lower() not in own] if on else []
    cfg["section_weights"] = dict(rb.get("weights") or {}) if on else {}
    return cfg


def sections_of(cfg: dict) -> list[str]:
    """The site's rubrics: the owner's, then those the newsroom added."""
    return [*(cfg.get("sections") or []), *(cfg.get("added_sections") or [])]


def save(s, site_id: int, values: dict) -> bool:
    """Store the automatic newsroom's settings on the profile in use and on
    the version waiting for approval; False when the site has neither."""
    from .profile import _set_settings

    return _set_settings(s, site_id, auto=check_settings(values))


# A recipe site (Francesco, 2026-11-08: "redazia deve leggere che è un sito di
# ricette e si regola da sola"): told by its repository (a folder of recipes,
# connect.github) or by the sector of its profile. Its articles are its own
# recipes: the news only says what people cook and look for now.
RECIPE_WORDS = re.compile(r"\b(ricett\w*|recipes?|rezept\w*|recettes?|recetas?|food blog)\b", re.I)

RECIPE_SITE = (
    "This is a recipe site: it publishes its own recipes. The items only show what people cook, look for and "
    "talk about now (a dish, an ingredient, a season, a trend, a festivity): take them as the idea, then the "
    "article is a complete recipe of the site (format recipe), filed under the site's section for its course. "
    "A technique, an ingredient or a method explained without one dish to cook is an article of format guide."
)


def recipe_site(body: dict | None) -> bool:
    """Whether the site publishes recipes of its own."""
    st = (body or {}).get("settings") or {}
    if st.get("site_kind"):
        return st["site_kind"] == "recipes"
    sector = ((body or {}).get("sector") or {}).get("value") or ""
    summary = (body or {}).get("summary")
    summary = (summary.get("value") if isinstance(summary, dict) else summary) or ""
    return bool(RECIPE_WORDS.search(f"{sector} {summary}"[:800]))


def brief_lines(body: dict | None) -> list[str]:
    """The newsroom's brief and directives, for the prompts that plan and
    write its articles; set with or without the automatic mode."""
    cfg = settings_of(body)
    out = [RECIPE_SITE] if recipe_site(body) else []
    if cfg["brief"]:
        out.append(f"Newsroom brief: {cfg['brief'][:2000]}")
    if secs := sections_of(cfg):
        out.append(f"Sections of the site: {', '.join(secs)}")
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


def due_by(cfg: dict, local: datetime) -> int:
    """Articles the day should have by the end of this hour: the morning's
    share in the first hour, then the rest evenly over the window."""
    total = hours_left(cfg, local.replace(hour=cfg["hours"][0] % 24))
    done = total - hours_left(cfg, local) + 1  # hours of the window gone, this one included
    morning = cfg["per_day"] * cfg["morning_share"] / 100
    return math.ceil(morning + (cfg["per_day"] - morning) * (done - 1) / max(1, total - 1))


def _auto_jobs(proposal_id, statuses: tuple[str, ...]):
    """Whether the proposal has an `auto` job in one of these states."""
    j = m.jobs.c
    return select(j.id).where(j.kind == "auto", j.status.in_(statuses),
                              j.subject == literal("proposal ").concat(cast(proposal_id, Text))).exists()


def made_since(s, site_id: int, since: datetime, fix: bool = False,
               until: datetime | None = None) -> tuple[int, int]:
    """Articles of the automatic newsroom since the given time, as (kept,
    tried): kept are those not discarded (published, on their way or held
    for a person), tried every article it took on, discarded ones included.
    An article written again counts once; those accepted and still being
    written count in both.

    Not kept, so another article takes their place (Francesco 2026-11-05:
    20 a day, 9 published by 13:00, then nothing): an accepted proposal
    whose job ended in an error without a draft, and, with `fix` (nothing
    waits for a person), a draft no job is working on any more that was
    neither approved nor discarded. With `until`, a past period: only its
    drafts count, nothing is still being written."""
    pr, dr = m.proposals.c, m.drafts.c
    working = _auto_jobs(dr.proposal_id, ("queued", "running"))
    alive = (dr.status != "rejected") if not fix else or_(dr.status == "approved", and_(dr.status != "rejected", working))
    mine = (select(dr.proposal_id, func.max(case((alive, 1), else_=0)).label("kept"))
            .join(m.proposals, pr.id == dr.proposal_id)
            .where(pr.site_id == site_id, pr.decided_by == AUTO, dr.translation_of.is_(None), dr.created_at >= since,
                   *([dr.created_at < until] if until is not None else []))
            .group_by(dr.proposal_id).subquery())
    tried, kept = s.execute(select(func.count(), func.coalesce(func.sum(mine.c.kept), 0)).select_from(mine)).one()
    if until is not None:
        return int(kept), int(tried)
    accepted = (pr.site_id == site_id, pr.decided_by == AUTO, pr.status == "accepted",
                pr.created_at >= since - timedelta(days=1))
    writing = s.execute(select(func.count()).select_from(m.proposals).where(*accepted)).scalar() or 0
    lost = s.execute(select(func.count()).select_from(m.proposals).where(
        *accepted, _auto_jobs(pr.id, ("failed",)), ~_auto_jobs(pr.id, ("queued", "running")))).scalar() or 0
    return int(kept) + writing - lost, int(tried) + writing


# How many articles a day the newsroom may try, for each one it should keep:
# discarded articles are replaced, within this limit, so the cost stays known.
TRIES_PER_ARTICLE = 2


def source_count(s, proposal_id: int, need: int = 0) -> int:
    """Distinct sources telling the event a proposal is about: the news the
    article is written from (drafts.event_items), that is its main news
    and the other outlets' reports of the same event. Items of the same
    sector that are about something else do not count: counted, they made
    articles join separate stories to reach two sources. With `need`, the
    wider search runs only when the main news alone is short of it."""
    pc = m.proposal_citations.c
    row = s.execute(select(m.proposals.c.title, m.proposals.c.basis).where(m.proposals.c.id == proposal_id)).first()
    cited = list(s.execute(select(pc.item_id).where(pc.proposal_id == proposal_id)).scalars())
    if row is not None and is_evergreen(row.basis):  # on a theme: every item cited is the subject
        return s.execute(select(func.count(func.distinct(m.source_items.c.source_id)))
                         .where(m.source_items.c.id.in_(cited or [0]))).scalar() or 0
    return drafts.outlets(s, cited, row.title or "" if row else "", need,
                          also=((row.basis if row else None) or {}).get("same_event") or ())


# Below the minimum opportunity, the best proposals still go out down to
# this share of it: the minimum says what to prefer, it must not leave the
# day empty (a site with a broad profile scores lower on each single news).
OPPORTUNITY_FLOOR = 0.5


# A proposal is left for later when it only tells again what an article of
# the last two days (or one chosen in the same round) already told. The same
# subject is not the same news: on a night of elections many articles are
# right for a site that follows the hype, so a proposal with at least one
# news item no recent article used, and a title that is not nearly the same,
# is a new development and goes ahead. Left out: the same words almost
# entirely (the same story retold), or most of the words with no new item
# (an external review, 2026-11-04: four pieces on the same midterm night).
OVERLAP_DAYS = 2
OVERLAP_WORDS = 3
OVERLAP_SHARE = 0.4  # with no new news item
SAME_STORY_SHARE = 0.65  # even with new news items
SAME_STORY_WORDS = 4


def _angle_terms(title: str, angle: str | None) -> set[str]:
    return set(tokens(title)) | set(tokens(angle or ""))


# Two titles that say the same thing in other words: most of the words of
# the shorter one (aitoday.it, 2026-11-06: "Fine vita, il governo impugna la
# legge del Veneto davanti alla Consulta" and "Il governo impugna la legge del
# Veneto sul fine vita e la manda alla Consulta" went out two minutes apart;
# their proposals' angles, longer and different, hid it).
SAME_TITLE_WORDS = 4
SAME_TITLE_SHARE = 0.6
# Or the same names: "Vance attribuisce alla guerra in Iran la sconfitta
# repubblicana, Trump esclude di licenziare Hegseth" and, the next morning,
# "Vance e la guerra in Iran dopo le midterm: Teheran replica, Hegseth sotto
# pressione" (aitoday.it, 2026-11-06) share few words but three names.
SAME_TITLE_NAMES = 3
TITLE_CASE = 0.6  # more capitalised words than this: a title in Title Case, whose capitals are not names
_CAPITAL = re.compile(r"[A-ZÀ-ÖØ-Þ][\w'’-]*")


def title_words(title: str | None) -> set[str]:
    """The words of a title, and its names (capitalised words) marked with @."""
    words = set(tokens(title or ""))
    caps = _CAPITAL.findall(title or "")
    if not caps or len(caps) > TITLE_CASE * max(1, len((title or "").split())):
        return words
    return words | {"@" + n for n in tokens(" ".join(caps))}


def same_title(a: set[str], b: set[str]) -> bool:
    names = sum(1 for x in a & b if x.startswith("@"))
    if names >= SAME_TITLE_NAMES:
        return True
    a, b = {x for x in a if not x.startswith("@")}, {x for x in b if not x.startswith("@")}
    shared = len(a & b)
    return shared >= SAME_TITLE_WORDS and shared / max(1, min(len(a), len(b))) >= SAME_TITLE_SHARE


def overlaps(terms: set[str], items: set[int], others: list[tuple], title: set[str] | None = None) -> bool:
    """`others`: (words, news items[, title words]) of what was taken on."""
    for other in others:
        o_terms, o_items = other[0], other[1]
        if title and len(other) > 2 and same_title(title, other[2]):
            return True
        shared = len(terms & o_terms)
        share = shared / max(1, min(len(terms), len(o_terms)))
        if shared >= SAME_STORY_WORDS and share >= SAME_STORY_SHARE:
            return True
        if shared >= OVERLAP_WORDS and share >= OVERLAP_SHARE and items <= o_items:
            return True
    return False


def recent_angles(s, site_id: int, now: datetime) -> list[tuple[set[str], set[int]]]:
    """Words and news items of the proposals the newsroom took on in the
    last days, those still being written included, not the ones discarded."""
    pr, dr, pc = m.proposals.c, m.drafts.c, m.proposal_citations.c
    kept = select(dr.proposal_id).where(dr.status != "rejected", dr.translation_of.is_(None))
    rows = s.execute(select(pr.id, pr.title, pr.angle).where(
        pr.site_id == site_id, pr.decided_by == AUTO, pr.created_at >= now - timedelta(days=OVERLAP_DAYS),
        (pr.status == "accepted") | ((pr.status == "drafted") & pr.id.in_(kept)))).all()
    used: dict[int, set[int]] = {}
    if rows:
        for pid, item in s.execute(select(pc.proposal_id, pc.item_id).where(pc.proposal_id.in_([r.id for r in rows]))):
            used.setdefault(pid, set()).add(item)
    return [(_angle_terms(r.title, r.angle), used.get(r.id, set()), title_words(r.title)) for r in rows]


# The country of a site that does not say it, from its language; English is spoken in too many.
HOME = {"it": "IT", "de": "DE", "fr": "FR", "es": "ES"}


def home_of(s, site_id: int, cfg: dict) -> str:
    return cfg.get("home_country") or HOME.get(tr.main_language(s, site_id) or "", "")


def _cap(cfg: dict, key: str) -> int | None:
    share = cfg.get(key, DEFAULTS[key])
    return None if not share or share >= 100 else max(1, round(cfg["per_day"] * share / 100))


def country_cap(cfg: dict) -> int | None:
    """Articles a day one foreign country may have; None: no limit."""
    return _cap(cfg, "country_share")


def section_cap(cfg: dict, section: str | None = None) -> int | None:
    """Articles a day one section may have, when the site lists its
    sections; None: no limit. With `section`, its own: the share times the
    weight the newsroom gave it for the week (rubrics.py), never below one
    a day, so a rubric with a lowered priority is never shut."""
    base = _cap(cfg, "section_share") if len(sections_of(cfg)) > 1 else None
    if base is None or section is None:
        return base
    w = (cfg.get("section_weights") or {}).get(section, 1.0)
    return max(1, round(base * w))


def section_of(cfg: dict, name: str | None) -> str | None:
    """The site's section `name` says, as the site writes it, or None."""
    name = (name or "").strip().lower()
    return next((x for x in sections_of(cfg) if x.lower() == name), None) if name else None


def mix_today(s, site_id: int, since: datetime, now: datetime) -> tuple[dict[str, int], dict[str, int]]:
    """The day's articles of the automatic newsroom by country of their story
    and by section: those written since `since` (under the section the
    editor-in-chief gave them) and those accepted and not written yet (under
    the planned one)."""
    pr, dr = m.proposals.c, m.drafts.c
    written = dict(s.execute(select(dr.proposal_id, dr.seo).where(
        dr.site_id == site_id, dr.created_at >= since, dr.translation_of.is_(None),
        dr.proposal_id.is_not(None))).all())
    anyd = select(dr.proposal_id).where(dr.proposal_id.is_not(None))
    rows = s.execute(select(pr.id, pr.basis).where(
        pr.site_id == site_id, pr.decided_by == AUTO, pr.status == "accepted",
        or_(pr.id.in_(list(written) or [0]), and_(pr.created_at >= now - timedelta(hours=36), ~pr.id.in_(anyd))))).all()
    countries: dict[str, int] = {}
    sections: dict[str, int] = {}
    for pid, b in rows:
        b = b or {}
        if c := b.get("country"):
            countries[c] = countries.get(c, 0) + 1
        if sec := (written.get(pid) or {}).get("section") or b.get("section"):
            sections[sec] = sections.get(sec, 0) + 1
    return countries, sections


def full_today(s, site_id: int, cfg: dict, now: datetime, since: datetime | None = None) -> list[str]:
    """What the day already has enough of, for the planner to look elsewhere:
    the foreign countries and the sections at their limit."""
    ccap, scap = country_cap(cfg), section_cap(cfg)
    if not ccap and not scap:
        return []
    countries, sections = mix_today(s, site_id, since or now - timedelta(hours=24), now)
    home = home_of(s, site_id, cfg)
    return [*(f"news about {c}" for c, n in countries.items() if ccap and c != home and n >= ccap),
            *(f"the section {x}" for x, n in sections.items() if scap and n >= section_cap(cfg, x))]


# Proposals read each round: enough to get past a day of news that only one outlet tells,
# kept until they lapse, to the ones planned for the automatic newsroom.
CHOOSE_FROM = 250


def choose(s, site_id: int, cfg: dict, now: datetime, n: int, minimum: float | None = None, dry: bool = False,
           since: datetime | None = None) -> list:
    """The best proposals of the last day and a half that pass the
    guardrails, those over the minimum opportunity first; those on a topic
    to avoid are turned down for good, those only telling again what a
    recent article told wait; a new development of the same subject goes on.
    News told by the outlets the site asks for come first, then, to make
    up the number, the best ones fewer outlets tell. A foreign country or
    a section already at its share of the day's articles (country_cap,
    section_cap) waits for tomorrow; the site's own country has no limit
    (Francesco 2026-11-07: too many American stories, too much Mondo)."""
    pr = m.proposals.c
    seen = recent_angles(s, site_id, now)
    ccap, scap, home = country_cap(cfg), section_cap(cfg), home_of(s, site_id, cfg)
    countries, sections = mix_today(s, site_id, since or now - timedelta(hours=24), now) \
        if ccap or scap else ({}, {})

    def full(r) -> bool:
        b = r.basis or {}
        c, sec = b.get("country"), section_of(cfg, b.get("section"))
        return bool(ccap and c and c != home and countries.get(c, 0) >= ccap) \
            or bool(scap and sec and sections.get(sec, 0) >= section_cap(cfg, sec))

    def take(r) -> None:
        out.append(r)
        b = r.basis or {}
        if c := b.get("country"):
            countries[c] = countries.get(c, 0) + 1
        if sec := section_of(cfg, b.get("section")):
            sections[sec] = sections.get(sec, 0) + 1

    rows = s.execute(select(m.proposals).where(
        pr.site_id == site_id, pr.status == "proposed", pr.created_at >= now - timedelta(hours=36),
        pr.opportunity >= (cfg["min_opportunity"] * OPPORTUNITY_FLOOR if minimum is None else minimum))
        .order_by(pr.opportunity.desc(), pr.id.desc()).limit(CHOOSE_FROM)).all()
    out, fewer = [], []
    for r in rows:
        if len(out) >= n:
            break
        word = avoided(f"{r.title} {r.angle or ''}", cfg["avoid"])
        if word and dry:
            continue
        if word:
            s.execute(update(m.proposals).where(pr.id == r.id, pr.status == "proposed")
                      .values(status="rejected", decided_by=AUTO))
            proposals.record_feedback(s, r.tenant_id, "proposal", r.id, "down", AUTO, t(
                "argomento da evitare: {word}", _language_in(s, r.tenant_id, site_id), word=word), site_id)
            continue
        if full(r):
            continue
        terms, title = _angle_terms(r.title, r.angle), title_words(r.title)
        items = set(s.execute(select(m.proposal_citations.c.item_id).where(
            m.proposal_citations.c.proposal_id == r.id)).scalars())
        if overlaps(terms, items, seen, title):
            continue
        if source_count(s, r.id, cfg["min_sources"]) >= cfg["min_sources"]:
            take(r)
            seen.append((terms, items, title))
        else:
            fewer.append((r, terms, items, title))
    # The outlets the site asks for are a preference, not a wall (Francesco 2026-11-05: "se non
    # c'è la seconda testata va fatto comunque una scrematura e la pubblicazione"): when the news
    # told by enough outlets are not enough, the best of the others follow, and the
    # editor-in-chief reads them like any other.
    for r, terms, items, title in fewer:
        if len(out) >= n:
            break
        if not full(r) and not overlaps(terms, items, seen, title):
            take(r)
            seen.append((terms, items, title))
    return out


def lapse(db, tenant_id: str, site_id: int, now: datetime) -> int:
    """The proposals the newsroom did not use: past the day and a half it
    chooses from they are closed, so nobody has to decide them one by one
    (Francesco: if it is automatic, it decides)."""
    pr = m.proposals.c
    with db.tenant(tenant_id) as s:
        res = s.execute(update(m.proposals).where(
            pr.site_id == site_id, pr.status == "proposed", pr.created_at < now - timedelta(hours=36))
            .values(status="rejected", decided_by=AUTO))
    return res.rowcount or 0


# Breaking news a day at most, beyond the day's articles: each is one fewer tomorrow.
BREAKING_PER_DAY = 3
BREAKING_CANDIDATES = 40  # fresh proposals looked at each round, the best first


def breaking_news(s, site_id: int, cfg: dict, now: datetime):
    """The proposal of a story that at least `breaking_outlets` outlets told
    in the last `breaking_hours` hours (Francesco 2026-11-06: "se ci sarà una
    notizia che esce 3/4 volte nel giro di poco la riprendiamo anche se
    abbiamo finito la disponibilità"), or None. Not one the site already told,
    nor on a topic to avoid; the editor-in-chief reads it like any other."""
    pr, si = m.proposals.c, m.source_items.c
    since = now - timedelta(hours=cfg["breaking_hours"])
    seen = recent_angles(s, site_id, now)
    rows = s.execute(select(m.proposals).where(
        pr.site_id == site_id, pr.status == "proposed", pr.created_at >= since,
        pr.opportunity >= cfg["min_opportunity"] * OPPORTUNITY_FLOOR)
        .order_by(pr.opportunity.desc(), pr.id.desc()).limit(BREAKING_CANDIDATES)).all()
    for r in rows:
        if avoided(f"{r.title} {r.angle or ''}", cfg["avoid"]):
            continue
        cited = list(s.execute(select(m.proposal_citations.c.item_id).where(
            m.proposal_citations.c.proposal_id == r.id)).scalars())
        terms, title = _angle_terms(r.title, r.angle), title_words(r.title)
        if overlaps(terms, set(cited), seen, title):
            continue
        main, same = drafts.event_items(s, cited, r.title or "", also=(r.basis or {}).get("same_event") or ())
        told = s.execute(select(func.count(func.distinct(si.source_id))).where(
            si.id.in_(main + same or [0]), si.fetched_at >= since)).scalar() or 0
        if told >= cfg["breaking_outlets"]:
            return r
    return None


def over_today(settings: dict, local, kept_over: int | None = None) -> dict:
    """What the newsroom brought forward from tomorrow and paid with credits
    today, and what it owes from yesterday: {day, borrowed, paid, debt}.
    The record rolls over with the day, carrying yesterday's articles beyond
    its limit as today's debt (tomorrow it makes fewer). `kept_over`: those
    yesterday actually kept beyond its limit; without it, every extra it
    accepted (some may have been discarded: Francesco 2026-11-07, 10 of 12
    every day after two extras that never came out)."""
    rec = dict(settings.get("auto_over") or {})
    today = local.date().isoformat()
    if rec.get("day") == today:
        return {"day": today, "borrowed": int(rec.get("borrowed", 0)), "paid": int(rec.get("paid", 0)),
                "breaking": int(rec.get("breaking", 0)), "debt": int(rec.get("debt", 0))}
    yesterday = (local.date() - timedelta(days=1)).isoformat()
    owed = int(rec.get("borrowed", 0)) + int(rec.get("breaking", 0)) if rec.get("day") == yesterday else 0
    if kept_over is not None and rec.get("day") == yesterday:
        owed = min(owed, max(0, kept_over))
    return {"day": today, "borrowed": 0, "paid": 0, "breaking": 0, "debt": owed}


def run(db, tenant_id: str, site_id: int, core: CoreClient | None, now: datetime, tz: str, enqueue) -> dict:
    """One hour of the automatic newsroom for a site: accept the articles
    of this hour and queue them. `enqueue(proposal_id)` queues one."""
    with db.tenant(tenant_id) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()
        allowed = quotas.autopilot_allowed(s)
        month_left = quotas.remaining(s, "drafts")
    cfg = settings_of(body)
    if body is None or not cfg["enabled"]:
        return {}
    lapse(db, tenant_id, site_id, now)
    if not allowed:
        return {"waiting": "plan"}
    local = now.astimezone(ZoneInfo(tz))
    rec = (body.get("settings") or {}).get("auto_over") or {}
    kept_over = None
    if rec.get("day") == (local.date() - timedelta(days=1)).isoformat():
        start = datetime.combine(local.date(), time(), local.tzinfo).astimezone(UTC)
        with db.tenant(tenant_id) as s:
            kept_y = made_since(s, site_id, start - timedelta(days=1), fix=cfg["on_problem"] == "fix", until=start)[0]
        kept_over = kept_y - max(1, cfg["per_day"] - int(rec.get("debt", 0)))
    over = over_today((body.get("settings") or {}), local, kept_over)
    with db.tenant(tenant_id) as s:
        have = credits.state(s)
        price = credits.price(s, "article")
    can_pay = bool(have.get("enabled")) and cfg["extra_credits"] > over["paid"] \
        and have.get("left", 0) >= price
    # The plan's articles of the month, set by the console: the automatic
    # newsroom spends credits only on the extras it was allowed (`extra_credits`),
    # so without them it waits for the next month.
    month_over = month_left is not None and month_left <= 0
    if month_over and not can_pay:
        return {"waiting": "month"}
    def breaking(why: dict) -> dict:
        """Before saying the day is done (or out of hours): breaking news, if any."""
        if not cfg["breaking"] or month_over or over["breaking"] >= BREAKING_PER_DAY:
            return why
        with db.tenant(tenant_id) as s:
            r = breaking_news(s, site_id, cfg, now)
        if r is None or not proposals.decide(db, tenant_id, r.id, True, AUTO, "automatic newsroom, breaking"):
            return why
        enqueue(r.id)
        from .profile import _set_settings

        with db.tenant(tenant_id) as s:
            _set_settings(s, site_id, auto_over={**over, "breaking": over["breaking"] + 1})
        return {"accepted": [r.id], "wanted": 1, "extra": "breaking"}

    if not in_hours(cfg, local):
        return breaking({"waiting": "hours"})
    if cfg["min_sources"] > 1 and not month_over:
        # Other outlets telling the same event in other words or languages, confirmed by the
        # model once per proposal, so that choose finds the second outlet where it is.
        from . import same_event

        same_event.confirm(db, tenant_id, site_id, core, now, floor=cfg["min_opportunity"] * OPPORTUNITY_FLOOR)
    since = datetime.combine(local.date(), time(), local.tzinfo).astimezone(UTC)
    paid = False
    with db.tenant(tenant_id) as s:
        kept, tried = made_since(s, site_id, since, fix=cfg["on_problem"] == "fix")
        # What it brought forward yesterday it does not make today.
        target = max(1, cfg["per_day"] - over["debt"])
        extra = None
        if month_over:
            extra = "paid"
        elif kept >= target:
            # Brought forward: the articles kept beyond the day's limit (breaking news
            # aside), not those accepted, which may be discarded; tries capped.
            if (kept - target - over["breaking"] < cfg["borrow"]
                    and over["borrowed"] < cfg["borrow"] * TRIES_PER_ARTICLE):
                extra = "borrowed"
            elif can_pay:
                extra = "paid"
            else:
                # The day is done and no extra is allowed: how many good news stay out, counted
                # here once an hour (the Desk only shows it; counting takes a while).
                done = {"done": target, "left_out": left_out(s, site_id, cfg, now)} \
                    | ({"borrowed": over["borrowed"]} if over["borrowed"] else {}) \
                    | ({"debt": over["debt"]} if over["debt"] else {})
                extra = "done"
        if extra is None and tried >= cfg["per_day"] * TRIES_PER_ARTICLE:
            return {"tried": tried, "kept": kept}
        if extra == "done":
            picked = None
        elif extra is not None:
            # One at a time, only for news that is worth it, a round after the other.
            picked = choose(s, site_id, cfg, now, 1, minimum=cfg["extra_min_opportunity"], since=since)
            if not picked:  # nothing worth more than the day's articles: the day is done
                done = {"done": target} | ({"borrowed": over["borrowed"]} if over["borrowed"] else {}) \
                    | ({"debt": over["debt"]} if over["debt"] else {})
                extra = "done"
            want = 1
            paid = extra == "paid"
            planned_today = proposals.made_today(s, site_id, since, outlets=cfg["min_sources"] > 1)
        else:
            left = min(target - kept, cfg["per_day"] * TRIES_PER_ARTICLE - tried)
            # Spread over the hours left, so the day does not go in the first hour.
            want = math.ceil(left / hours_left(cfg, local))
            # The morning goes first: by the end of the first hour the day has
            # its share of articles (half, by default), the rest follows hour by
            # hour. A newsroom behind that line (a slow night, a restart) catches up.
            want = max(want, min(left, due_by(cfg, local) - kept))
            if month_left is not None:
                want = min(want, int(month_left))
            picked = choose(s, site_id, cfg, now, want, since=since)
            planned_today = proposals.made_today(s, site_id, since, outlets=cfg["min_sources"] > 1)
    if extra == "done":
        return breaking(done)
    if extra is None and len(picked) < want and core is not None and planned_today < cfg["per_day"] * 4:
        # Only news it can take: told by as many outlets as the site asks (Francesco 2026-11-05: 167
        # proposals from single news, none with two outlets, and nothing written from 13:00 on).
        with db.tenant(tenant_id) as s:
            full = full_today(s, site_id, cfg, now, since)
        proposals.plan(db, tenant_id, site_id, core, limit=max(3, 2 * want), now=now, outlets=cfg["min_sources"],
                       full=full)
        with db.tenant(tenant_id) as s:
            picked = choose(s, site_id, cfg, now, want, since=since)
    accepted = []
    for r in picked:
        if proposals.decide(db, tenant_id, r.id, True, AUTO, "automatic newsroom" if not extra else f"automatic newsroom, {extra}"):
            enqueue(r.id, True) if paid else enqueue(r.id)
            accepted.append(r.id)
    if extra and accepted:
        from .profile import _set_settings

        key = "paid" if paid else "borrowed"
        with db.tenant(tenant_id) as s:
            _set_settings(s, site_id, auto_over={**over, key: over[key] + len(accepted)})
    elif over != dict(((body.get("settings") or {}).get("auto_over")) or {}):
        from .profile import _set_settings

        with db.tenant(tenant_id) as s:
            _set_settings(s, site_id, auto_over=over)
    out = {"accepted": accepted, "wanted": want}
    return out | ({"extra": extra} if extra else {})


def sources_due(last: str | None, now: datetime) -> bool:
    """Whether the automatic newsroom looks for new sources: once a day."""
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(hours=24)
    except ValueError:
        return True


def remember(db, tenant_id: str, site_id: int, result: dict, now: datetime) -> None:
    """The last hour of the automatic newsroom, for the site page: what it
    did or what it waits for."""
    from .profile import _set_settings

    if not result:
        return
    with db.tenant(tenant_id) as s:
        _set_settings(s, site_id, auto_last={**result, "at": now.astimezone(UTC).isoformat()})


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
    fixes = v.get("headline_fixes") if isinstance(v.get("headline_fixes"), dict) else {}
    fixes = {k: " ".join(str(x).split()) for k, x in fixes.items()
             if k in ("title", "subtitle", "seo_title", "seo_description") and isinstance(x, str) and x.strip()}
    cut = [" ".join(str(x).split()) for x in v.get("cut") or [] if isinstance(x, str) and x.strip()][:EVENING_CUT_MAX]
    repeats = " ".join(str(v.get("repeats") or "").split())[:300] if isinstance(v.get("repeats"), str) else ""
    return {"publish": v["publish"] and not repeats, "problems": problems,
            "section": str(v.get("section") or "").strip()[:80], "fixes": fixes, "cut": cut, "repeats": repeats}


# The article's sentences the checks or the editor-in-chief find wrong are
# cut, when they are few, instead of writing the article again or throwing
# it away after it was paid for (2026-11-04: seven of eight articles of a
# day discarded, most for one sentence). Never the opening, never so many
# that the article is no longer the one written.
# Details the sources do not give are cut, not written again (2026-11-05: the
# rewrite of an article padded from thin sources padded it again): up to six
# sentences and two fifths of the article, which goes out shorter.
CUT_MAX_SHARE = 0.4
CUT_MAX = 6
CUT_MIN_LEFT = 4


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def cut(db, tenant_id: str, d, drop: set[int], most: int = CUT_MAX) -> bool:
    """The draft without the sentences at these positions, and with the
    headings about its own sources removed (their sentences join the
    section before). False, and nothing changed, when cutting them would
    take the opening or too much of the article."""
    with db.tenant(tenant_id) as s:
        claims = drafts.stored_claims(s, d.id)
    if not claims:
        return False
    first = claims[0]["section"]
    if any(c["ordinal"] in drop and c["section"] == first for c in claims):
        return False  # the opening answers the reader: rewritten, not cut
    keep = [dict(c) for c in claims if c["ordinal"] not in drop]
    if drop and (len(drop) > max(most, 1) or len(drop) > max(1, round(len(claims) * CUT_MAX_SHARE))
                 or len(keep) < CUT_MIN_LEFT):
        return False
    heads = False
    previous = first
    for c in keep:
        if c["section"] and drafts.about_sources(c["section"]):
            c["section"], heads = previous, True
        else:
            previous = c["section"]
    if not drop and not heads:
        return False
    drafts.edit_text(db, tenant_id, d.id, d.title, d.subtitle or "", drafts.as_text(keep))
    return True


def cut_sentences(db, tenant_id: str, d, sentences: list[str], most: int = CUT_MAX) -> bool:
    """The editor-in-chief's cuts, found among the draft's sentences."""
    with db.tenant(tenant_id) as s:
        claims = drafts.stored_claims(s, d.id)
    drop = set()
    for x in sentences:
        k = _key(x)
        hit = [c["ordinal"] for c in claims if k and (k in _key(c["text"]) or _key(c["text"]) in k)]
        if not hit:
            return False
        drop.update(hit[:1])
    return cut(db, tenant_id, d, drop, most)


def proofread(db, tenant_id: str, d, fixes: dict) -> bool:
    """Apply the editor-in-chief's corrections of typos to the title, the
    subtitle and the search title and description, in the draft and in its
    text. A correction that is not a small change of the original (more
    than a fifth of its characters) is not applied: it is a rewrite."""
    import difflib

    seo = dict(d.seo or {})
    new = {"title": d.title, "subtitle": d.subtitle, "seo_title": seo.get("title"), "seo_description": seo.get("description")}
    changed = False
    for k, fixed in fixes.items():
        old = new.get(k)
        if not old or fixed == old or difflib.SequenceMatcher(None, old, fixed).ratio() < 0.8:
            continue
        new[k], changed = fixed, True
    if not changed:
        return False
    body = d.body_md
    if new["title"] != d.title:
        body = body.replace(f"# {d.title}", f"# {new['title']}", 1)
    if new["subtitle"] != d.subtitle and d.subtitle:
        body = body.replace(f"_{d.subtitle}_", f"_{new['subtitle']}_", 1)
    seo.update({"title": new["seo_title"], "description": new["seo_description"]})
    with db.tenant(tenant_id) as s:
        s.execute(update(m.drafts).where(m.drafts.c.id == d.id).values(
            title=new["title"], subtitle=new["subtitle"], body_md=body, seo=seo))
    return True


# Characters of each cited source the editor-in-chief reads: as much as the
# writer read of the main news (drafts._prompt). With 2500 it refused, as
# "not in the source", details the writer had read further on in a paper
# (aitoday.it, 2026-11-05: articles from arXiv discarded one after the other).
EXCERPT = 6000

# A refused article is corrected where it stands before it is written again
# or thrown away (Francesco 2026-11-05: "sono motivi che si possono
# correggere, non credo sia tutta da buttare la notizia"). The correction
# changes only what the reasons name and adds nothing: a sentence that is
# not one of the article's, reworded, makes it a rewrite and it is refused.
FIX_SYSTEM = (
    "You are the editor-in-chief of a news site. The article below was refused for the problems listed after "
    "it. Correct it so that every problem is gone, changing only what the problems name: remove a detail, a "
    "sentence or a section that the sources do not support or that belongs to another story; reword a sentence "
    "that says more than its sources (a forecast told as a fact, a cause or a figure they do not give); make "
    "the title and the subtitle say what the article says. Never add a fact, a number, a name or a sentence "
    "that is not already in the article, and check what you keep against the numbered source excerpts. Keep "
    "the article's language and its format: one sentence per line, \"## \" before a heading, and after each "
    "sentence its sources as [1] [2], numbered as in the article; every sentence keeps at least one. Reply "
    'with JSON only: {"title": "<the title>", "subtitle": "<the subtitle>", '
    '"text": "<the whole corrected article in that format>"}'
)
FIX_MIN_LIKENESS = 0.5  # how close each sentence stays to one of the article's


def _fix_validator(v) -> dict:
    if not isinstance(v, dict) or not isinstance(v.get("text"), str) or not v["text"].strip():
        raise ValueError('expected {"title": ..., "subtitle": ..., "text": "<the article>"}')
    return {"title": " ".join(str(v.get("title") or "").split()), "subtitle": " ".join(str(v.get("subtitle") or "").split()),
            "text": v["text"]}


def _numbered(db, tenant_id: str, order: list[int]) -> list[str]:
    """The cited sources with the numbers the article gives them."""
    si = m.source_items.c
    with db.tenant(tenant_id) as s:
        rows = {r.id: r for r in s.execute(select(si.id, si.title, si.summary, si.content, si.published_at)
                                           .where(si.id.in_(order or [0])))}
    out = ["Sources (the number the article cites, title, date and what was read of each):"]
    for n, i in enumerate(order, 1):
        r = rows.get(i)
        if r is None:
            continue
        text = " ".join(f"{r.summary or ''} {r.content or ''}".split())[:EXCERPT]
        date = r.published_at.date().isoformat() if r.published_at else "undated"
        out.append(f"[{n}] {r.title} ({date}): {text or 'only the title was read'}")
    return out


def correct(db, tenant_id: str, d, problems: list[str], core: CoreClient) -> bool:
    """The article corrected in place for these problems, by the model the
    console sets for corrections. False, and nothing changed, when the
    answer is not a correction: a sentence without sources, one that is not
    among the article's, or too little left."""
    import difflib

    with db.tenant(tenant_id) as s:
        claims = drafts.stored_claims(s, d.id)
    if not claims:
        return False
    order = drafts.source_order(claims)
    prompt = "\n".join([f"Language: {d.language}", f"Title: {d.title}", f"Subtitle: {d.subtitle or ''}", "Article:",
                        drafts.as_text(claims), "Problems:", *[f"- {p}" for p in problems],
                        *_numbered(db, tenant_id, order)])
    try:
        with usage.tagged(db, tenant_id, "auto_fix", site_id=d.site_id, ref=f"draft:{d.id}"):
            fixed, _calls = core.chat_json(FIX_SYSTEM, prompt, _fix_validator)
        new = drafts.parse_text(fixed["text"], order)
    except (CoreError, ValueError) as e:
        log.info("draft %s not corrected: %s", d.id, e)
        return False
    old = [c["text"] for c in claims]
    if len(new) < CUT_MIN_LEFT or any(not c.sources for c in new):
        return False
    for c in new:
        if c.text not in old and max(difflib.SequenceMatcher(None, c.text, o).ratio() for o in old) < FIX_MIN_LIKENESS:
            return False  # a sentence of its own: that is writing again, not correcting
    try:
        drafts.edit_text(db, tenant_id, d.id, fixed["title"] or d.title, fixed["subtitle"] or (d.subtitle or ""),
                         fixed["text"])
    except (ValueError, LookupError):
        return False
    return True


def _excerpts(db, tenant_id: str, draft_id: int) -> list[str]:
    """What the cited sources say, in the order of their notes, so the
    editor-in-chief checks the article against them and not against itself."""
    cs, c, si = m.draft_claim_sources.c, m.draft_claims.c, m.source_items.c
    with db.tenant(tenant_id) as s:
        rows = s.execute(select(si.id, si.title, si.summary, si.content, si.published_at, m.sources.c.url.label("feed"))
                         .join(m.sources, m.sources.c.id == si.source_id)
                         .where(si.id.in_(select(cs.item_id).join(m.draft_claims, c.id == cs.claim_id)
                                          .where(c.draft_id == draft_id)))).all()
    out = ["Cited sources (title, date and what was read of each):"]
    for r in rows:
        text = " ".join(f"{r.summary or ''} {r.content or ''}".split())[:EXCERPT]
        date = r.published_at.date().isoformat() if r.published_at else "undated"
        kind = "ORIGINAL DOCUMENT, " if r.feed == drafts.DOCUMENTS_SOURCE else ""
        out.append(f"- {r.title} ({kind}{date}): {text or 'only the title was read'}")
    return out if rows else []


def newsroom_language(db, tenant_id: str, site_id: int) -> str:
    """The language the reasons of a refusal are written in: the newsroom's
    (the one of its emails and of its pages), else the site's, else
    English. A translation refused is explained in the newsroom's language
    too: the reasons are read in the panel, not on the site."""
    with db.tenant(tenant_id) as s:
        return _language_in(s, tenant_id, site_id)


def _language_in(s, tenant_id: str, site_id: int) -> str:
    lang = s.execute(select(m.tenants.c.language).where(m.tenants.c.tenant_id == tenant_id)).scalar()
    lang = (lang or "").strip().lower()[:2] or tr.main_language(s, site_id) or ""
    return lang if lang in tr.LANGUAGES else "en"


def reasons_language(db, tenant_id: str, d) -> str:
    return tr.LANGUAGES[newsroom_language(db, tenant_id, d.site_id)]


def is_evening(s, proposal_id: int | None) -> bool:
    """Whether a draft's proposal is the evening analysis (evening.py)."""
    return bool(proposal_id) and s.execute(select(m.proposals.c.format).where(
        m.proposals.c.id == proposal_id)).scalar() == "evening"


def themed(db, tenant_id: str, d) -> bool:
    """Whether a draft is an article on a theme or one the newsroom asked
    for, not the telling of one event."""
    pid = getattr(d, "proposal_id", None)
    if pid is None:
        return False
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.proposals.c.format, m.proposals.c.basis).where(m.proposals.c.id == pid)).first()
    return row is not None and (row.format == "request" or is_evergreen(row.basis))


def editor_check(db, tenant_id: str, d, body: dict, core: CoreClient, evening: bool = False,
                 one_outlet: bool = False) -> dict:
    """The AI editor-in-chief's verdict on a draft."""
    lines = brief_lines(body) or ["Newsroom brief: none given"]
    seo = d.seo or {}
    heads = [f"Headlines: title: {d.title} | subtitle: {d.subtitle or ''} | search title: {seo.get('title') or ''} | "
             f"search description: {seo.get('description') or ''}"]
    recent = [] if evening else recent_titles(db, tenant_id, d)
    prompt = "\n".join(lines + [f"Language: {d.language}", f"Reasons in: {reasons_language(db, tenant_id, d)}",
                               f"Date of publication: {datetime.now(UTC).date().isoformat()}",
                               *([ONE_OUTLET] if one_outlet else []),
                               "Article:", d.body_md[:14000], *heads, *_excerpts(db, tenant_id, d.id),
                               *(["The site's articles of the last two days:", *(f"- {x}" for x in recent)]
                                 if recent else [])])
    system = EVENING_CHECK_SYSTEM if evening else THEMED_CHECK_SYSTEM if themed(db, tenant_id, d) else CHECK_SYSTEM
    with usage.tagged(db, tenant_id, "editor_check", site_id=d.site_id, ref=f"draft:{d.id}"):
        verdict, _calls = core.chat_json(system, prompt, _check_validator)
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


def _send(db, tenant_id: str, d, cfg: dict, main: bool, publish, hold: bool = False) -> dict:
    """Label, approve and publish a draft that passed; with `hold` the
    publication is only made ready, for the caller to send with others."""
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
    if not hold:
        publish(pub_id)
    return {"approved": True, "target": target.name, "publication_id": pub_id}


def one_commit(s, site_id: int, cfg: dict, language: str | None, languages: list[str]) -> bool:
    """Whether the article and its translations go to the same GitHub
    repository and branch: then they are sent in one commit, so the site
    builds once (Francesco 2026-11-13: Vercel bills every build)."""
    def where(t):
        c = (t.config or {}) if t is not None else {}
        return t is not None and t.kind == "github" and (c.get("repo"), c.get("branch"), c.get("api"), t.secret_env)

    first = where(target_for(s, site_id, cfg, language, True))
    return bool(first) and all(where(target_for(s, site_id, cfg, x, False)) == first for x in languages)


def finish(db, tenant_id: str, draft_id: int, core: CoreClient, publish, translate=lambda language, draft_id: None,
           rewrite=None, cover=None, rounds: int = 0, corrected: bool = False, translate_now=None) -> dict:
    """After the draft: the guardrails, the editor-in-chief, the cover, the
    label, the approval and the publication. `publish(pub_id)` queues the
    sending, `translate(language, draft_id)` a translation into each of the site's
    languages, `translate_now(language, draft_id)` writes it here and returns
    its draft: when the article and its translations go to the same GitHub
    repository they are then published together, `publish(pub_id, others)`,
    in one commit. `rewrite(draft_id, notes)` writes the article again with the
    stronger model and returns the new draft, `cover(draft_id)` draws it.

    With on_problem "fix" nothing waits for a person: what can be fixed is
    corrected where it stands, then written again once, and only what still
    does not pass is discarded. The cover is drawn only
    for an article that passed everything, so a bad day costs no images."""
    def load():
        with db.tenant(tenant_id) as s:
            row = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
            n = s.execute(select(func.count(func.distinct(m.source_items.c.source_id))).select_from(
                m.draft_claim_sources.join(m.draft_claims, m.draft_claims.c.id == m.draft_claim_sources.c.claim_id)
                .join(m.source_items, m.source_items.c.id == m.draft_claim_sources.c.item_id))
                .where(m.draft_claims.c.draft_id == draft_id)).scalar() or 0
        return row, n

    d, cited = load()
    with db.tenant(tenant_id) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
        cfg = settings_of(body)
        evening = is_evening(s, d.proposal_id)
        # The outlets the article must cite: those the site asks for, when the news has them
        # (a story one outlet tells goes on, with one: choose). The evening analysis cites the site.
        need = 1 if evening else min(cfg["min_sources"], max(1, source_count(s, d.proposal_id, cfg["min_sources"]))) \
            if d.proposal_id else cfg["min_sources"]
    out = {"draft_id": draft_id, "title": d.title, "site_id": d.site_id}
    lang = newsroom_language(db, tenant_id, d.site_id)  # the reasons are read in the panel
    if not cfg["enabled"]:
        return _stop(db, tenant_id, d, [t("la redazione automatica è spenta", lang)], out, discard=False)
    with db.tenant(tenant_id) as s:
        twin = None if evening else told_already(s, d)
    if twin:  # nothing to fix: the site already has this story
        return _stop(db, tenant_id, d, [t("la stessa notizia è già uscita: «{title}»", lang, title=twin)], out,
                     discard=cfg["on_problem"] == "fix")
    word = avoided(f"{d.title}\n{d.body_md}", cfg["avoid"])
    if word:  # nothing to fix: the subject itself is out
        return _stop(db, tenant_id, d, [t("argomento da evitare: {word}", lang, word=word)], out,
                     discard=cfg["on_problem"] == "fix")
    if cfg["on_problem"] == "fix":
        # A few sentences the checks found wrong, and headings about the sources: cut, for free.
        flagged = {f.get("claim") for f in (d.flags or []) if isinstance(f.get("claim"), int)}
        if cut(db, tenant_id, d, flagged, EVENING_CUT_MAX if evening else CUT_MAX):
            step(N_("Tolte le frasi che non superano i controlli"))
            d, cited = load()
            out["cut"] = len(flagged)
    fixable = []
    if d.status == "needs_review":
        fixable.append(t("alcune frasi non superano i controlli sulle fonti: {detail}", lang, detail="; ".join(
            t("frase {n}: {problems}", lang, n=f.get("claim"), problems=", ".join(drafts.flag_text(x, lang) for x in f.get("problems") or []))
            for f in (d.flags or [])))[:1500])
    if cited < need:
        fixable.append(t("fonti diverse citate: {n}, ne servono almeno {min}", lang, n=cited, min=need))
    if not fixable:
        step(N_("Il caporedattore AI rilegge l'articolo"))
        try:
            verdict = editor_check(db, tenant_id, d, body, core, evening,
                                   one_outlet=not evening and need == 1 < cfg["min_sources"])
        except CoreError as e:
            return _stop(db, tenant_id, d, [t("il caporedattore AI non ha risposto: {error}", lang, error=e)], out,
                         discard=False)
        if verdict.get("repeats") and not evening:  # nothing to fix: the site already told it
            return _stop(db, tenant_id, d, [t("la stessa notizia è già uscita: «{title}»", lang,
                                              title=verdict["repeats"])], out, discard=cfg["on_problem"] == "fix")
        if not verdict["publish"]:
            fixable += verdict["problems"]
        elif verdict.get("cut"):  # publishable without a few sentences: cut, not written again
            if cfg["on_problem"] != "fix" or not cut_sentences(db, tenant_id, d, verdict["cut"],
                                                                EVENING_CUT_MAX if evening else CUT_MAX):
                fixable += verdict["problems"] or [t("il caporedattore AI vuole togliere: {sentences}", lang,
                                                     sentences=" / ".join(verdict["cut"]))[:600]]
            else:
                d, cited = load()
                out["cut"] = out.get("cut", 0) + len(verdict["cut"])
                if d.status == "needs_review" or cited < need:
                    fixable += verdict["problems"] or [t("dopo i tagli l'articolo non supera i controlli", lang)]
        section = None if evening else next((x for x in sections_of(cfg) if x.lower() == verdict["section"].lower()), None)
        if section:  # the site files the article under it
            with db.tenant(tenant_id) as s:
                s.execute(update(m.drafts).where(m.drafts.c.id == draft_id).values(
                    seo={**(d.seo or {}), "section": section}))
        if verdict.get("fixes") and not fixable:
            with db.tenant(tenant_id) as s:
                fresh = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
            proofread(db, tenant_id, fresh, verdict["fixes"])
    if fixable:
        if cfg["on_problem"] != "fix":
            return _stop(db, tenant_id, d, fixable, out, discard=False)
        if not corrected and cited >= need:  # a missing outlet is not corrected: it is written again
            step(N_("Il caporedattore AI corregge l'articolo invece di scartarlo: {why}"), why="; ".join(fixable)[:300])
            if correct(db, tenant_id, d, fixable, core):
                res = finish(db, tenant_id, draft_id, core, publish, translate, rewrite, cover, rounds, corrected=True)
                return res if res.get("discarded") or res.get("held") else {**res, "corrected": fixable}
        if evening:
            # The evening analysis is never lost: first its headline goes plain (its name and date), the
            # text unchanged, and the editor-in-chief reads it again; still refused, it is written again
            # once, and then it waits for a person instead of being discarded.
            from . import evening as ev

            if ev.plain_headline(db, tenant_id, d):
                step(N_("Il caporedattore AI rilegge il Punto della Sera con titolo e sommario neutri"))
                res = finish(db, tenant_id, draft_id, core, publish, translate, rewrite, cover, rounds, corrected=True)
                return res if res.get("held") or res.get("discarded") else {**res, "plain_headline": fixable}
            if rewrite is None or rounds >= FIX_ROUNDS:
                return _stop(db, tenant_id, d, fixable, out, discard=False)
        if rewrite is None or rounds >= FIX_ROUNDS:
            return _stop(db, tenant_id, d, fixable, out, discard=True)
        step(N_("Il modello più forte riscrive l'articolo per correggere: {why}"), why="; ".join(fixable)[:300])
        _stop(db, tenant_id, d, fixable, out, discard=True)
        try:
            again = rewrite(draft_id, "; ".join(fixable)[:3000])
        except (CoreError, ValueError, quotas.QuotaExceeded) as e:
            return {**out, "discarded": fixable + [t("non riscritto: {error}", lang, error=e)]}
        res = finish(db, tenant_id, again, core, publish, translate, rewrite, cover, rounds + 1)
        return {**res, "rewritten_from": draft_id}
    if cover is not None:
        step(N_("Immagine di copertina"))
        try:
            cover(draft_id)
        except Exception as e:  # an article without a cover still goes out
            log.warning("cover for draft %s failed: %s", draft_id, e)
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
    with db.tenant(tenant_id) as s:
        langs = tr.missing(s, draft_id)
        together = bool(translate_now and langs and one_commit(s, d.site_id, cfg, d.language, langs))
    res = {**out, **_send(db, tenant_id, d, cfg, True, publish, hold=together)}
    if together and res.get("publication_id"):
        others = []
        for language in langs:
            step(N_("Traduzione dell'articolo"))
            try:
                tid = translate_now(language, draft_id)
            except Exception as e:  # the article does not wait: this language follows on its own
                log.warning("translation of draft %s into %s failed: %s", draft_id, language, e)
                translate(language, draft_id)
                continue
            done = finish_translation(db, tenant_id, tid, publish, hold=True)
            if done.get("publication_id"):
                others.append(done["publication_id"])
        if others:
            publish(res["publication_id"], others)
        else:
            publish(res["publication_id"])
        res["translations"] = langs
        return res
    for language in langs:
        translate(language, draft_id)
    if langs:
        res["translations"] = langs
    return res


def too_thin(db, tenant_id: str, proposal_id: int, words: int) -> dict:
    """A proposal of which too little was read to write an article: closed
    before the writing is paid for, so another one takes its place."""
    pr = m.proposals.c
    with db.tenant(tenant_id) as s:
        p = s.execute(select(pr.title, pr.site_id).where(pr.id == proposal_id)).one()
        s.execute(update(m.proposals).where(pr.id == proposal_id).values(status="rejected", decided_by=AUTO))
        why = t("della notizia si sono lette solo {n} parole: troppo poche per scriverne un articolo senza inventare",
                _language_in(s, tenant_id, p.site_id), n=words)
        proposals.record_feedback(s, tenant_id, "proposal", proposal_id, "down", AUTO, why, p.site_id)
    return {"title": p.title, "site_id": p.site_id, "proposal_id": proposal_id, "skipped": [why]}


RECENT_TITLES = 80


# Before writing, a short look at the proposal (Francesco 2026-11-14: «gli ultimi 8 sono 4 scarti e
# 4 pubblicazioni, pago sempre il doppio»): a story the site told yesterday in other words, a
# columnist's thesis, a list of separate events were written, refused by the editor-in-chief,
# written again by the stronger model and refused again. Now they are closed before any of it.
PRECHECK_SYSTEM = (
    "You decide, before an article is written and paid for, whether a proposed story is worth writing for a "
    "news site. You receive the proposal (title, angle, why now), the titles of the news items it rests on and "
    "the titles of the site's articles of the last two days. Answer write false only when: (a) one of the "
    "site's articles already tells the same story and the proposal brings no new fact in its title (a "
    "decision, a result, an answer, a number that is news itself; a new comment or a new outlet on the same "
    "event is not new); (b) the news items are an opinion piece, an editorial, a column or someone's thesis, "
    "with no event of their own to report; (c) the proposal bundles several separate events (a list of "
    "examples, a round-up) instead of telling one. An explainer, an analysis of facts, an interview, an "
    "exhibition, a show, a decision of a government, a court or a company are stories to write, not (b). In "
    "any other case, and when in doubt, write true. Reply "
    'with JSON only: {"write": true|false, "rule": "<a, b or c when false; else null>", "reason": "<when '
    'false, one plain sentence for the newsroom\'s editor in the language named after Reasons in; else '
    'null>", "repeats": "<for a, the site\'s title it repeats, copied exactly; else null>"}')
# Proposals of these formats bring several items together by design, or were asked for.
PRECHECK_SKIP = ("evening", "request", "menu", "recipe")


# Francesco 2026-11-15: from 11 to 17 nothing came out, every proposal closed before
# writing. Past this many closed in a day for a site the check stands aside: the
# editor-in-chief still reads what is written.
PRECHECK_PER_DAY = 3


def _precheck_validator(v) -> dict:
    if not isinstance(v, dict) or not isinstance(v.get("write"), bool):
        raise ValueError('expected {"write": true|false, "reason": ...}')
    return {"write": v["write"], "rule": str(v.get("rule") or "").strip().lower()[:1],
            "reason": str(v.get("reason") or "").strip()[:400],
            "repeats": str(v.get("repeats") or "").strip()[:300]}


def _closed_today(s, site_id: int, day: str) -> int:
    pr = m.proposals.c
    rows = s.execute(select(pr.basis).where(
        pr.site_id == site_id, pr.status == "rejected", pr.decided_by == AUTO,
        pr.created_at >= datetime.now(UTC) - timedelta(days=3))).scalars()
    return sum(1 for b in rows if (b or {}).get("precheck_on") == day)


def worth_writing(db, tenant_id: str, proposal_id: int, core: CoreClient) -> dict | None:
    """None when the proposal is worth writing; else it is closed, like a thin
    one, and the reason returned. A model that does not answer lets it through."""
    pr, dr = m.proposals.c, m.drafts.c
    with db.tenant(tenant_id) as s:
        p = s.execute(select(pr.title, pr.angle, pr.why_now, pr.format, pr.site_id, pr.basis)
                      .where(pr.id == proposal_id)).first()
        if p is None or (p.format or "") in PRECHECK_SKIP or is_evergreen(p.basis):
            return None
        items = list(s.execute(select(m.source_items.c.title).join(
            m.proposal_citations, m.proposal_citations.c.item_id == m.source_items.c.id)
            .where(m.proposal_citations.c.proposal_id == proposal_id).limit(12)).scalars())
        recent = list(s.execute(select(dr.title).where(
            dr.site_id == p.site_id, dr.status == "approved", dr.translation_of.is_(None),
            dr.proposal_id.is_distinct_from(proposal_id),
            dr.created_at >= datetime.now(UTC) - timedelta(days=OVERLAP_DAYS))
            .order_by(dr.id.desc()).limit(RECENT_TITLES)).scalars())
        lang = _language_in(s, tenant_id, p.site_id)
        day = datetime.now(UTC).date().isoformat()
        if _closed_today(s, p.site_id, day) >= PRECHECK_PER_DAY:
            return None
    prompt = "\n".join([f"Reasons in: {tr.LANGUAGES[lang]}", f"Proposal: {p.title}",
                        f"Angle: {p.angle or ''}", f"Why now: {p.why_now or ''}", "News items:",
                        *(f"- {x}" for x in items), "The site's articles of the last two days:",
                        *(f"- {x}" for x in recent or ["(none)"])])
    try:
        with usage.tagged(db, tenant_id, "precheck", site_id=p.site_id, ref=f"proposal:{proposal_id}"):
            verdict, _calls = core.chat_json(PRECHECK_SYSTEM, prompt, _precheck_validator)
    except (CoreError, ValueError) as e:
        log.warning("precheck of proposal %s failed: %s", proposal_id, e)
        return None
    if verdict["write"] or verdict["rule"] not in ("a", "b", "c"):
        return None
    # A repeat must name one of the site's titles: one it made up is no repeat.
    told = {x.strip().lower(): x for x in recent}
    if verdict["rule"] == "a" and verdict["repeats"].lower() not in told:
        return None
    why = (t("la stessa notizia è già uscita: «{title}»", lang, title=told[verdict["repeats"].lower()])
           if verdict["rule"] == "a" else verdict["reason"] or t("non adatta a un articolo", lang))
    with db.tenant(tenant_id) as s:
        s.execute(update(m.proposals).where(pr.id == proposal_id).values(
            status="rejected", decided_by=AUTO, basis={**(p.basis or {}), "precheck_on": day,
                                                         "precheck": verdict["rule"]}))
        proposals.record_feedback(s, tenant_id, "proposal", proposal_id, "down", AUTO, why, p.site_id)
    return {"title": p.title, "site_id": p.site_id, "proposal_id": proposal_id, "skipped": [why]}




def recent_titles(db, tenant_id: str, d) -> list[str]:
    """The titles of the site's articles of the last days, newest first,
    for the editor-in-chief to see a story told again in other words."""
    dr = m.drafts.c
    with db.tenant(tenant_id) as s:
        return list(s.execute(select(dr.title).where(
            dr.site_id == d.site_id, dr.id != d.id, dr.status == "approved", dr.translation_of.is_(None),
            dr.proposal_id.is_distinct_from(d.proposal_id),
            dr.created_at >= datetime.now(UTC) - timedelta(days=OVERLAP_DAYS))
            .order_by(dr.id.desc()).limit(RECENT_TITLES)).scalars())


def told_already(s, d) -> str | None:
    """The title of an article of the site that already tells the same story
    (approved in the last days, any of its versions), or None. Proposals
    written at the same time pass `choose` together; the second to finish is
    caught here, before the editor-in-chief and the cover are paid for."""
    dr = m.drafts.c
    mine = title_words(d.title)
    rows = s.execute(select(dr.title).where(
        dr.site_id == d.site_id, dr.id != d.id, dr.status == "approved", dr.translation_of.is_(None),
        dr.proposal_id.is_distinct_from(d.proposal_id),
        dr.created_at >= datetime.now(UTC) - timedelta(days=OVERLAP_DAYS))).scalars()
    return next((x for x in rows if same_title(mine, title_words(x))), None)


def _stop(db, tenant_id: str, d, why: list[str], out: dict, discard: bool) -> dict:
    """An article that does not go out: discarded, or held for a person."""
    note = "; ".join(why)[:1000]
    with db.tenant(tenant_id) as s:
        proposals.record_feedback(s, tenant_id, "draft", d.id, "down", AUTO, note, d.site_id)
    if not discard:
        return {**out, "held": why}
    drafts.decide(db, tenant_id, d.id, False, AUTO)
    return {**out, "discarded": why}


def owns(db, tenant_id: str, draft_id: int) -> bool:
    """Whether a translation belongs to the automatic newsroom: its original
    was approved by it and the newsroom is still on. A translation asked by
    hand of such an article then goes out by itself too, like the original."""
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts.c.translation_of, m.drafts.c.site_id).where(m.drafts.c.id == draft_id)).first()
        if d is None or d.translation_of is None:
            return False
        by = s.execute(select(m.drafts.c.decided_by).where(m.drafts.c.id == d.translation_of)).scalar()
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
    return by == AUTO and settings_of(body)["enabled"]


def finish_translation(db, tenant_id: str, draft_id: int, publish, hold: bool = False) -> dict:
    """A translation of an article the editor-in-chief let through: the
    same sentences and sources, so it only has to pass its own checks."""
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        body = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
    cfg = settings_of(body)
    out = {"draft_id": draft_id, "title": d.title, "site_id": d.site_id, "language": d.language}
    lang = newsroom_language(db, tenant_id, d.site_id)
    if not cfg["enabled"]:
        return _stop(db, tenant_id, d, [t("la redazione automatica è spenta", lang)], out, discard=False)
    if d.status == "needs_review":  # the original stays published; this language is skipped
        return _stop(db, tenant_id, d, [t("alcune frasi non superano i controlli sulle fonti", lang)], out,
                     discard=cfg["on_problem"] == "fix")
    return {**out, **_send(db, tenant_id, d, cfg, False, publish, hold)}


def left_out(s, site_id: int, cfg: dict, now: datetime) -> int:
    """Proposals of the last day and a half that would pass the bar of the
    extras (opportunity, distinct sources, topics) and that the day's limit
    left unused."""
    return len(choose(s, site_id, cfg, now, 50, minimum=cfg["extra_min_opportunity"], dry=True))


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
    items = [{"id": r.id, "title": r.title, "status": r.status,
              "held": notes.get(r.id) if r.status != "rejected" else None,
              "discarded": notes.get(r.id) or "" if r.status == "rejected" else None,
              "publication": pubs.get(r.id)} for r in rows]
    return {"items": items, "published": sum(1 for x in items if x["publication"] == "published"),
            "held": sum(1 for x in items if x["held"]),
            "discarded": sum(1 for x in items if x["discarded"] is not None)}


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
