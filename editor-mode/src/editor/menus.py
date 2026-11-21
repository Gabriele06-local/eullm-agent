"""A whole menu for a site of recipes (Francesco, 2026-11-10: "menù interi",
"in futuro potrebbe non essere necessario pubblicare ma solo assemblare nella
pagina menu le ricette esistenti").

A menu is an article of its own (an opening and a course per section) that
links the site's recipes: the ones the site already has, chosen by the
model from the site's pages, and new ones, written as the site's own recipes
in drafts of their own (one proposal each, tied to the menu by
``basis.menu``). The menu's links to the new recipes are written as
``draft:<id>`` and become the recipes' addresses when they are online: the
menu goes out after them, by itself once the last one is published.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import insert, select, update

from . import models as m

log = logging.getLogger(__name__)

MIN_COURSES, MAX_COURSES = 3, 5
SITE_RECIPES = 150  # the site's pages the model chooses from

MENU = (
    "This article is a whole menu of the site, its own: an opening of one to three sentences on the occasion "
    "and on why these courses go together (citing the ids of the items it rests on; the text never names the "
    "items, their sites or authors, nor speaks of an article, a blog or a source), then the courses, in the "
    "order they are served. Add to the JSON \"menu\": [{\"course\": \"<the course, in the site's language: "
    "starter, first course, main course, side dish, dessert...>\", \"use\": \"<the address of one of the site's "
    "recipes listed below, when it fits the menu>\", \"why\": \"<one sentence on this dish in this menu>\"} or "
    "{\"course\": \"...\", \"new\": {\"title\": \"<a recipe the site does not have yet, max 120 chars>\", "
    "\"angle\": \"<what it is and why it fits, 1-2 sentences>\"}, \"why\": \"...\"}], " + f"{MIN_COURSES} to "
    f"{MAX_COURSES} courses. Use the site's own recipes wherever one fits: write a new one only for a course the "
    "site has nothing right for. The sections hold the opening only: the courses are in \"menu\"."
)

_DRAFT_LINK = re.compile(r"\]\(draft:(\d+)\)")


def is_menu(profile: dict | None, proposal) -> bool:
    """Whether the draft is a whole menu of a site of recipes."""
    from .autopilot import recipe_site

    return getattr(proposal, "format", None) == "menu" and recipe_site(profile)


def site_recipes(s, site_id: int) -> list[dict]:
    """The site's pages that are recipes, as {title, url}: the ones under its
    recipes' address when the site has one (…/ricette/…, …/recipes/…),
    else all its pages."""
    rows = s.execute(select(m.site_posts.c.title, m.site_posts.c.url).where(
        m.site_posts.c.site_id == site_id, m.site_posts.c.title.is_not(None))
        .order_by(m.site_posts.c.published_at.desc().nulls_last()).limit(2000)).all()
    found = [r for r in rows if re.search(r"/(ricett|recipe|rezept|recette|receta)", r.url or "", re.I)]
    return [{"title": r.title, "url": r.url} for r in (found or rows)[:SITE_RECIPES] if r.title and r.url]


def instructions(recipes: list[dict]) -> str:
    lines = [MENU, "", "The site's recipes:" if recipes else "The site has no recipes listed: every course is new."]
    lines += [f"- {x['title']} <{x['url']}>" for x in recipes]
    return "\n".join(lines)


def parse(value, recipes: list[dict]) -> list[dict]:
    """The courses, checked: a site's recipe only from the list, a new one
    with its title. Raises ValueError for the model to fix."""
    known = {x["url"]: x["title"] for x in recipes}
    courses = value.get("menu") if isinstance(value, dict) else None
    if not isinstance(courses, list):
        raise ValueError("the menu needs its courses in \"menu\"")
    out = []
    for c in courses[:MAX_COURSES]:
        if not isinstance(c, dict) or not isinstance(c.get("course"), str) or not c["course"].strip():
            continue
        course = " ".join(c["course"].split())[:60]
        why = " ".join(str(c.get("why") or "").split())[:300]
        use = str(c.get("use") or "").strip().strip("<>")
        new = c.get("new") if isinstance(c.get("new"), dict) else None
        if use:
            if use not in known:
                raise ValueError(f"the course {course!r} uses an address that is not among the site's recipes")
            out.append({"course": course, "title": known[use], "url": use, "why": why})
        elif new and isinstance(new.get("title"), str) and 5 <= len(new["title"].strip()) <= 160:
            out.append({"course": course, "title": " ".join(new["title"].split()), "why": why,
                        "angle": " ".join(str(new.get("angle") or "").split())[:400]})
    if len(out) < MIN_COURSES:
        raise ValueError(f"a menu needs {MIN_COURSES} to {MAX_COURSES} courses, each a site's recipe or a new one")
    return out


def lines(courses: list[dict]) -> list[str]:
    """The courses in Markdown: a heading each, the recipe's link, its line."""
    out = []
    for c in courses:
        target = c.get("url") or (f"draft:{c['draft_id']}" if c.get("draft_id") else None)
        out.append(f"\n## {c['course']}\n")
        out.append(f"**[{c['title']}]({target})**" if target else f"**{c['title']}**")
        if c.get("why"):
            out.append(f"\n{c['why']}")
    return out


def write_courses(db, tenant_id: str, menu_draft: int, prop, courses: list[dict], write) -> list[dict]:
    """The new recipes of the menu, each in a proposal and a draft of its own;
    the courses with the draft of each new one. A recipe that cannot be
    written (the plan is over, the model failed) leaves its course out."""
    from . import quotas

    with db.tenant(tenant_id) as s:
        cited = list(s.execute(select(m.proposal_citations.c.item_id).where(
            m.proposal_citations.c.proposal_id == prop.id)).scalars())
    kept = []
    for c in courses:
        if c.get("url"):
            kept.append(c)
            continue
        with db.tenant(tenant_id) as s:
            pid = s.execute(insert(m.proposals).values(
                tenant_id=tenant_id, site_id=prop.site_id, topic_id=prop.topic_id, profile_id=prop.profile_id,
                opportunity=prop.opportunity, title=c["title"][:160],
                angle=(c.get("angle") or "") + f" ({c['course']} del menù «{prop.title}»)",
                why_now=prop.why_now, format="recipe", generated_by="model", status="accepted",
                decided_by=prop.decided_by,
                basis={**(prop.basis or {}), "menu": prop.id, "menu_draft": menu_draft, "course": c["course"]},
            ).returning(m.proposals.c.id)).scalar_one()
            if cited:
                s.execute(insert(m.proposal_citations).values(
                    [{"tenant_id": tenant_id, "proposal_id": pid, "item_id": i} for i in cited]))
        try:
            kept.append({**c, "draft_id": write(pid)})
        except (quotas.QuotaExceeded, Exception) as e:  # the menu goes on with its other courses
            log.warning("recipe %r of menu %s not written: %s", c["title"], menu_draft, e)
            with db.tenant(tenant_id) as s:
                s.execute(update(m.proposals).where(m.proposals.c.id == pid).values(status="rejected"))
    return kept


def children(s, draft_id: int) -> list[int]:
    """The drafts of the menu's new recipes."""
    menu = ((s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == draft_id)).scalar() or {}).get("menu")) or []
    return [c["draft_id"] for c in menu if c.get("draft_id")]


def online(s, draft_id: int) -> str | None:
    """The address a recipe of the menu has on the site, once published."""
    return s.execute(select(m.publications.c.external_url).where(
        m.publications.c.draft_id == draft_id, m.publications.c.status == "published",
        m.publications.c.mode == "publish", m.publications.c.withdrawn_at.is_(None),
        m.publications.c.external_url.like("http%")).order_by(m.publications.c.id.desc()).limit(1)).scalar()


def missing(s, draft_id: int) -> list[int]:
    """The menu's new recipes not online yet."""
    return [x for x in children(s, draft_id) if not online(s, x)]


def resolve(s, body: str) -> str:
    """The menu with the addresses of its new recipes; ValueError while one
    is not online."""
    def addr(match):
        url = online(s, int(match.group(1)))
        if not url:
            raise ValueError("the menu's new recipes are not online yet")
        return f"]({url})"

    return _DRAFT_LINK.sub(addr, body)


def publish_first(db, tenant_id: str, draft_id: int, target_id: int, mode: str, by: str, approve: bool, run) -> int:
    """A menu asked to go out while its new recipes are not online: the
    recipes go first (approved by the director's yes, when `approve`), and
    the menu waits for them (``seo.publish_after``) to go out by itself.
    Returns how many recipes were sent."""
    from . import drafts, publishing

    sent = 0
    with db.tenant(tenant_id) as s:
        todo = missing(s, draft_id)
    for child in todo:
        with db.tenant(tenant_id) as s:
            st = s.execute(select(m.drafts.c.status).where(m.drafts.c.id == child)).scalar()
            busy = s.execute(select(m.publications.c.id).where(
                m.publications.c.draft_id == child, m.publications.c.target_id == target_id,
                m.publications.c.status.in_(("pending_approval", "approved", "sending")))).first()
        if busy:
            continue
        if st != "approved":
            if not approve or st not in ("draft", "needs_review"):
                continue
            drafts.decide(db, tenant_id, child, True, by)
        pid = publishing.request(db, tenant_id, child, target_id, mode, by)
        if approve:
            publishing.decide(db, tenant_id, pid, True, by)
            run(pid)
        sent += 1
    with db.tenant(tenant_id) as s:
        sd = s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == draft_id)).scalar() or {}
        s.execute(update(m.drafts).where(m.drafts.c.id == draft_id).values(
            seo={**sd, "publish_after": {"target_id": target_id, "mode": mode, "by": by, "approve": approve}}))
    return sent


def after_publication(db, tenant_id: str, pub_id: int, run) -> int | None:
    """A recipe of a menu went online: when it was the last one the menu
    waited for, the menu goes out. Returns the menu's publication."""
    from . import publishing

    with db.tenant(tenant_id) as s:
        basis = s.execute(select(m.proposals.c.basis).join(m.drafts, m.drafts.c.proposal_id == m.proposals.c.id)
                          .join(m.publications, m.publications.c.draft_id == m.drafts.c.id)
                          .where(m.publications.c.id == pub_id)).scalar() or {}
        menu = basis.get("menu_draft")
        if not menu:
            return None
        sd = s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == menu)).scalar() or {}
        wait = sd.get("publish_after")
        if not wait or missing(s, menu):
            return None
    try:
        pid = publishing.request(db, tenant_id, menu, wait["target_id"], wait["mode"], wait["by"])
    except publishing.PublishError as e:
        log.warning("menu %s not sent after its recipes: %s", menu, e)
        return None
    with db.tenant(tenant_id) as s:
        sd = s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == menu)).scalar() or {}
        if "publish_after" in sd:
            s.execute(update(m.drafts).where(m.drafts.c.id == menu).values(
                seo={k: v for k, v in sd.items() if k != "publish_after"}))
    if wait.get("approve"):
        publishing.decide(db, tenant_id, pid, True, wait["by"])
        run(pid)
    return pid
