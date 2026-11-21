"""Articles on a theme for sites that do not live on the news of the day
(evergreen.py): where the themes come from, what they are written from,
and how the newsroom explains them."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from editor import auth, autopilot, drafts, evergreen, models as m, profile as prof, proposals as props
from editor.collectors import Item
from editor.core_client import CoreClient
from editor.dedup import minhash
from editor.normalize import url_hash
from editor.api import create_app
from conftest import feed
from test_proposals import setup_site

NOW = datetime.now(UTC)
CORE = "http://core.test/v1/llm/chat"


def chat(content):
    return httpx.Response(200, json={"content": json.dumps(content), "tool_calls": [],
                                     "usage": {"input_tokens": 1, "output_tokens": 1}, "cost": 0.0})


def post(s, tenant, site_id, title, when=NOW):
    url = "https://blog.example/" + url_hash(title)[:8]
    s.execute(m.site_posts.insert().values(tenant_id=tenant, site_id=site_id, url=url, url_hash=url_hash(url),
                                           title=title, minhash=minhash(title), published_at=when))


def starlink_from_two_sources(db, tenant):
    with db.tenant(tenant) as s:
        other = s.execute(text("SELECT id FROM editor.sources WHERE name = 'rete' AND site_id IS NOT NULL")).scalar()
    feed(db, tenant, other, [Item(url="https://rete.example/9", title="Starlink: come funziona il satellite a casa",
                                  published_at=NOW - timedelta(days=6))])


def test_the_mode_says_how_many_proposals_are_on_a_theme():
    assert evergreen.split({}, 6) == (0, 6)
    assert evergreen.split({"settings": {"content_mode": "evergreen"}}, 6) == (6, 0)
    assert evergreen.split({"settings": {"content_mode": "mixed"}}, 6) == (3, 3)
    assert evergreen.split({"settings": {"content_mode": "mixed", "evergreen_share": 34}}, 6) == (2, 4)
    assert evergreen.split({"settings": {"content_mode": "mixed"}}, 5) == (3, 2)
    assert evergreen.mode_of({"settings": {"content_mode": "nonsense"}}) == "news"


def test_themes_come_from_what_the_site_lacks_and_what_people_search(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        post(s, tenant, site_id, "Open Fiber completa la fibra FTTH a Bari")
        for key, impressions in (("prezzi starlink italia", 500), ("fibra bari open fiber", 900), ("starlink", 2000)):
            s.execute(m.search_stats.insert().values(tenant_id=tenant, site_id=site_id, day=NOW.date() - timedelta(days=2),
                                                     kind="query", key=key, clicks=3, impressions=impressions))
        body = prof.approved_profile(s, site_id).body
        gaps = evergreen.gaps(s, site_id, body, NOW)
        searches = evergreen.searches(s, site_id, NOW)
    # Fibra has its article, Satellite none.
    assert [(g.label, g.strength, g.facts["posts"]) for g in gaps] == [("Satellite", 1.0, 0)]
    # One word is a subject, not a question; a search a title answers is covered.
    assert [x.label for x in searches] == ["prezzi starlink italia"]
    assert searches[0].facts["impressions"] == 500 and 0 < searches[0].strength <= 1


def test_an_old_subject_is_a_gap_again(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        post(s, tenant, site_id, "Open Fiber completa la fibra FTTH a Bari", NOW - timedelta(days=200))
        post(s, tenant, site_id, "Starlink in Italia: i prezzi", NOW - timedelta(days=3))
        body = prof.approved_profile(s, site_id).body
        gaps = {g.label: g for g in evergreen.gaps(s, site_id, body, NOW)}
    assert gaps["Fibra"].strength >= 0.6 and gaps["Fibra"].facts["last"] == (NOW - timedelta(days=200)).date().isoformat()


def test_an_evergreen_proposal_is_written_from_two_sources_and_not_repeated(db, tenant):
    site_id = setup_site(db, tenant)
    starlink_from_two_sources(db, tenant)
    with db.tenant(tenant) as s:
        post(s, tenant, site_id, "Open Fiber completa la fibra FTTH a Bari")
        assert prof.set_content_mode(s, site_id, "evergreen", 50)
    prompts = []

    def answer(request):
        body = json.loads(request.content)
        system, prompt = body["messages"][0]["content"], body["messages"][1]["content"]
        prompts.append(prompt)
        if "in the month given" in system:
            return chat({"themes": [{"name": "Regali tech di Natale", "keywords": ["regali", "natale", "gifts"]}]})
        ids = [int(line[1:line.index("]")]) for line in prompt.splitlines() if line.startswith("[")]
        return chat({"title": "Starlink a casa: come funziona e quanto costa", "angle": "Una guida per chi vive fuori città",
                     "why_now": "Il sito non ne ha mai parlato", "format": "guide", "citations": ids[:2]})

    with respx.mock() as router:
        router.post(CORE).mock(side_effect=answer)
        ids = props.plan(db, tenant, site_id, CoreClient("http://core.test", "tok"), now=NOW)
    assert len(ids) == 1  # Satellite; the season's theme has nothing to be written from
    with db.tenant(tenant) as s:
        p = s.execute(select(m.proposals).where(m.proposals.c.id == ids[0])).one()
        cited = list(s.execute(text("SELECT s.source_id FROM editor.proposal_citations c JOIN editor.source_items s "
                                    "ON s.id = c.item_id WHERE c.proposal_id = :p"), {"p": p.id}).scalars())
        settings = prof.approved_profile(s, site_id).body["settings"]
        assert autopilot.source_count(s, p.id, 2) == 2
    assert p.basis["kind"] == "evergreen" and p.basis["reason"] == "gap" and p.basis["theme"] == "Satellite"
    assert p.opportunity == 85.0 and len(set(cited)) == 2
    assert "Theme: Satellite" in prompts[-1] and "little or nothing" in prompts[-1]
    assert settings["season"]["month"] == NOW.strftime("%Y-%m")  # asked once a month

    # Not the same theme again for weeks, and the month's themes are not asked again.
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CORE).mock(side_effect=answer)
        assert props.plan(db, tenant, site_id, CoreClient("http://core.test", "tok"), now=NOW) == []
    assert not route.called


def test_a_theme_is_written_as_a_guide_and_checked_as_one():
    body = {"name": "Blog Rete", "domain": "blog.example", "language": "it", "settings": {"auto": {"analysis": True}}}
    themed = SimpleNamespace(title="Starlink a casa", angle="", format="guide", basis={"kind": "evergreen"})
    news = SimpleNamespace(title="Starlink abbassa i prezzi", angle="", format="news", basis={"kind": "topic"})
    assert "not on one event" in drafts._prompt(body, themed, {}) and drafts.ANALYSIS not in drafts._prompt(body, themed, {})
    assert "not on one event" not in drafts._prompt(body, news, {}) and drafts.ANALYSIS in drafts._prompt(body, news, {})
    assert "joins separate news events" in autopilot.CHECK_SYSTEM
    assert "joins separate news events" not in autopilot.THEMED_CHECK_SYSTEM
    assert autopilot.THEMED_CHECK_SYSTEM.endswith(autopilot.REPEATS)


def test_the_plan_page_says_why_the_site_needs_the_theme(db, tenant):
    site_id = setup_site(db, tenant)
    starlink_from_two_sources(db, tenant)
    with db.tenant(tenant) as s:
        prof.set_content_mode(s, site_id, "mixed", 50)
    with respx.mock(assert_all_called=False) as router:
        router.post(CORE).mock(return_value=httpx.Response(503))
        ids = props.plan(db, tenant, site_id, None, now=NOW)
    with db.tenant(tenant) as s:
        kinds = [b.get("kind") for b in s.execute(select(m.proposals.c.basis).where(m.proposals.c.id.in_(ids))).scalars()]
    assert kinds[0] == "evergreen" and len(ids) == 2  # one on a theme, the rest news
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db))
    c.post("/login", data={"token": token})
    page = c.get("/trends").text
    assert "Si scrive da queste fonti" in page and "senza scadenza" in page
    assert "Il tuo sito non ha articoli su «" in page
    # A dict's own update() is not a similar article (the site has none here).
    assert "Meglio aggiornare un tuo articolo" not in page
    site = c.get("/site?tab=auto").text
    assert 'name="mode"' in site and "Contenuti senza scadenza" in site
    r = c.post(f"/ui/sites/{site_id}/content-mode", data={"mode": "news", "share": "30"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tenant(tenant) as s:
        settings = prof.approved_profile(s, site_id).body["settings"]
    assert settings["content_mode"] == "news" and settings["evergreen_share"] == 30
