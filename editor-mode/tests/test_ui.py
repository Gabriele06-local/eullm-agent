import socket

from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from editor import auth
from editor import models as m
from editor.api import create_app
from editor.ui import safe_path
from test_publishing import approved_draft, public_resolver

PAGES = ["/", "/activity", "/index", "/site", "/site?edit=1", "/sources", "/sources?status=all", "/trends", "/drafts",
         "/publications", "/feedback", "/feedback?show=all", "/plan", "/settings", "/guide", "/start"]


def logged_in(db, tenant, role):
    token = auth.create_token(db, tenant, f"{role}-user", role)
    c = TestClient(create_app(db))
    assert c.post("/login", data={"token": token}, follow_redirects=False).status_code == 303
    return c


def test_every_page_renders_for_every_role(db, tenant):
    did, _ = approved_draft(db, tenant)
    for role in ("viewer", "editor", "owner"):
        c = logged_in(db, tenant, role)
        for path in PAGES + [f"/drafts/{did}"]:
            r = c.get(path)
            assert r.status_code == 200, (role, path)
            assert "blog.example" in r.text or path == "/settings"
        draft = c.get(f"/drafts/{did}").text
        assert 'href="#src-1"' in draft and 'id="src-1"' in draft
        desk = c.get("/").text
        assert "Ultime notizie" in desk
        assert "Siti più costosi" not in desk and "Costo modello" not in desk  # euros only for operators
        assert 'data-mode="desk"' in desk and c.get("/ui/activity?mode=page").status_code == 200
        settings = c.get("/settings").text
        assert ("Crea token" in settings) == (role == "owner")
        assert ("Persone della redazione" in settings) == (role == "owner")


def test_forms_check_the_role_and_leave_a_notice(db, tenant):
    setup = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        source = s.execute(text("SELECT id FROM editor.sources WHERE shared IS NOT TRUE LIMIT 1")).scalar()
    viewer = logged_in(db, tenant, "viewer")
    r = viewer.post(f"/ui/sources/{source}/status", data={"status": "suspended"})
    assert r.status_code == 403 and "permesso" in r.text
    owner = logged_in(db, tenant, "owner")
    r = owner.post(f"/ui/sources/{source}/status", data={"status": "suspended", "back": "/sources?status=suspended"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sources?status=suspended"
    page = owner.get("/sources?status=suspended")
    assert "news sospesa." in page.text and "Sospesa" in page.text
    # The notice shows once.
    assert "news sospesa." not in owner.get("/sources?status=suspended").text
    # Unknown back paths fall back to a local page.
    r = owner.post(f"/ui/sources/{source}/status", data={"status": "active", "back": "https://evil.example"},
                   follow_redirects=False)
    assert r.headers["location"] == "/sources"
    assert setup


def test_publish_flow_from_the_pages(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    token = auth.create_token(db, tenant, "boss", "owner")
    c = TestClient(create_app(db))
    c.post("/login", data={"token": token})
    r = c.post("/ui/targets", data={"site_id": site_id, "kind": "webhook", "name": "Hook",
                                    "address": "https://hooks.example/in", "secret_env": "lower"})
    assert "MAIUSCOLO" in r.text
    r = c.post("/ui/targets", data={"site_id": site_id, "kind": "webhook", "name": "Hook",
                                    "address": "https://hooks.example/in"})
    assert "Destinazione Hook aggiunta." in r.text
    with db.tenant(tenant) as s:
        tid = s.execute(text("SELECT id FROM editor.publish_targets")).scalar()
    # An editor asks, a director approves on Pubblicazioni.
    r = logged_in(db, tenant, "editor").post(f"/ui/drafts/{did}/publish", data={"target_id": tid, "mode": "draft"})
    assert "Pubblicazione chiesta" in r.text
    assert "Approva e invia" in c.get("/publications").text
    # A director publishes in one step: no request to approve himself.
    with db.tenant(tenant) as s:
        s.execute(text("DELETE FROM editor.publications"))
    r = c.post(f"/ui/drafts/{did}/publish", data={"target_id": tid, "mode": "publish"})
    assert "Pubblicazione partita" in r.text
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT status FROM editor.publications")).scalar() == "approved"
        assert s.execute(text("SELECT count(*) FROM editor.jobs WHERE kind = 'publish'")).scalar() == 1


def test_a_director_approves_and_publishes_in_one_click(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.drafts SET status = 'draft', decided_by = NULL WHERE id = :d"), {"d": did})
    c = logged_in(db, tenant, "owner")
    c.post("/ui/targets", data={"site_id": site_id, "kind": "webhook", "name": "Hook",
                                "address": "https://hooks.example/in"})
    with db.tenant(tenant) as s:
        tid = s.execute(text("SELECT id FROM editor.publish_targets")).scalar()
    page = c.get(f"/drafts/{did}").text
    assert "Approva e pubblica" in page and "Approva senza pubblicare" in page
    assert "Approva e pubblica" not in logged_in(db, tenant, "editor").get(f"/drafts/{did}").text
    r = c.post(f"/ui/drafts/{did}/decision", data={"accept": "1", "publish_to": tid})
    assert "Pubblicazione partita" in r.text
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT status FROM editor.drafts WHERE id = :d"), {"d": did}).scalar() == "approved"
        assert s.execute(text("SELECT status FROM editor.publications")).scalar() == "approved"


def test_on_a_github_site_the_director_authorizes_and_publishes_them_together(db, tenant, monkeypatch):
    # Francesco 2026-11-14: five recipes one after the other made ten Vercel builds.
    from editor import publishing as pub
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "GH"), "ghp_test123")
    it = pub.add_target(db, tenant, site_id, "github", "Sito", {"repo": "me/site", "path": "content/a"}, "GH")
    other = pub.add_target(db, tenant, site_id, "github", "Sito bis", {"repo": "me/site", "path": "content/b"}, "GH")
    c = logged_in(db, tenant, "owner")
    assert "Pubblica le autorizzate" not in c.get("/").text
    for tid in (it, other):
        r = c.post(f"/ui/drafts/{did}/publish", data={"target_id": tid, "mode": "publish"})
        assert "Autorizzata" in r.text
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT count(*) FROM editor.jobs WHERE kind = 'publish'")).scalar() == 0
    page = c.get("/sources").text  # on every page, until it is pressed
    assert "Pubblica le autorizzate" in page and "2 articoli autorizzati" in page
    assert "Autorizzata, da pubblicare" in c.get(f"/drafts/{did}").text
    assert "Pubblica le autorizzate" not in logged_in(db, tenant, "editor").get("/").text
    r = c.post("/ui/publications/release", data={"back": "/sources"})
    assert "Pubblicazione partita per 2 articoli" in r.text and "Pubblica le autorizzate" not in r.text
    with db.tenant(tenant) as s:
        jobs = s.execute(text("SELECT args FROM editor.jobs WHERE kind = 'publish'")).scalars().all()
    assert len(jobs) == 1
    args = jobs[0] if isinstance(jobs[0], dict) else __import__("json").loads(jobs[0])
    assert len(args["together"]) == 1
    assert "niente da pubblicare" in c.post("/ui/publications/release", data={"back": "/"}).text


def test_site_switch_and_tokens(db, tenant):
    from editor import repo
    approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        other = repo.ensure_site(s, tenant, "altro.example", "Altro")
    c = logged_in(db, tenant, "owner")
    r = c.get(f"/switch?site={other}&back=/sources", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sources"
    assert "Fonti · altro.example" in c.get("/sources").text
    assert c.get("/switch?site=999999").status_code == 404
    page = c.post("/ui/tokens", data={"name": "Giulia", "role": "editor"})
    assert "non verrà più mostrato" in page.text and "Giulia" in page.text
    assert c.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/", follow_redirects=False).status_code == 303


def test_safe_path_rejects_crlf():
    assert safe_path("/sources") == "/sources"
    assert safe_path("/\nX") == "/"
    assert safe_path("/\r\nX") == "/"
    assert safe_path("https://evil.example") == "/"
    assert safe_path(None) == "/"


def test_owner_removes_a_topic_and_deletes_a_version_from_the_page(db, tenant):
    _, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        name = s.execute(text("SELECT body->'subtopics'->0->>'name' FROM editor.editorial_profiles "
                              "WHERE status = 'approved'")).scalar()
    owner = logged_in(db, tenant, "owner")
    assert "Togli il tema" in owner.get("/site").text
    r = owner.post(f"/ui/sites/{site_id}/topics/remove", data={"name": name}, follow_redirects=False)
    assert r.status_code == 303
    page = owner.get("/site").text
    assert "tolto" in page and "Temi tolti" in page and "Rimetti" in page
    with db.tenant(tenant) as s:
        old = s.execute(text("SELECT min(version) FROM editor.editorial_profiles")).scalar()
    r = owner.post(f"/ui/sites/{site_id}/versions/{old}/delete", follow_redirects=False)
    assert r.status_code == 303
    page = owner.get("/site").text
    assert "eliminata" in page or "storico" in page
    viewer = logged_in(db, tenant, "viewer")
    assert viewer.post(f"/ui/sites/{site_id}/topics/restore", data={"name": name}).status_code == 403


def test_suspended_source_of_another_site_opens_on_its_site(db, tenant):
    from editor import repo
    _, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        source = s.execute(text("SELECT id FROM editor.sources WHERE shared IS NOT TRUE LIMIT 1")).scalar()
        # Sorted first, so it is the site in view: the source is not its own.
        repo.ensure_site(s, tenant, "aaa.example", "Primo")
    c = logged_in(db, tenant, "owner")
    c.post(f"/ui/sources/{source}/status", data={"status": "suspended"})
    home = c.get("/").text
    link = f"/switch?site={site_id}&amp;back=/sources%3Fstatus%3Dsuspended"
    assert link in home and "· blog.example" in home
    # The register of the site in view is empty and points to the right one.
    empty = c.get("/sources?status=suspended").text
    assert "Nessuna fonte in questo stato per aaa.example" in empty and f"/switch?site={site_id}&amp;" in empty
    r = c.get(link.replace("&amp;", "&"), follow_redirects=False)
    assert r.headers["location"] == "/sources?status=suspended"
    assert "news" in c.get("/sources?status=suspended").text


def test_propose_now_queues_the_work(db, tenant):
    approved_draft(db, tenant)
    c = logged_in(db, tenant, "editor")
    assert "Proponi adesso" in c.get("/trends").text
    page = c.post("/ui/proposals/now")
    assert "in preparazione" in page.text or "già state fatte" in page.text
    with db.tenant(tenant) as s:
        kinds = list(s.execute(text("SELECT kind FROM editor.jobs")).scalars())
    assert "proposals" in kinds or "già state fatte" in page.text
    assert "Notizie in 7 giorni" in c.get("/sources").text


def test_a_proposal_says_what_it_is_in_plain_words(db, tenant):
    from datetime import UTC, datetime

    from editor import opportunity as opp, proposals as props
    from test_proposals import setup_site

    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.sources SET score = 0.8 WHERE name = 'news'"))
    props.propose(db, tenant, site_id, opp.score_site(db, tenant, site_id, now=datetime.now(UTC)), None)
    c = logged_in(db, tenant, "editor")
    for page in (c.get("/").text, c.get("/trends").text):
        assert "Articolo proposto" in page and "Cosa dicono i punteggi" in page
        # The news behind it, with its link, and why it fits the site.
        assert 'href="https://news.example/3"' in page and "Nasce da questa notizia" in page
        assert "Nasce da queste notizie" in page and 'href="https://rete.example/2"' in page
        assert "Rientra in «Satellite»" in page and "Rientra in «Fibra»" in page
        # A single source explains its low Hype.
        assert "per ora ne parla una sola fonte" in page


def test_home_says_when_the_reads_of_the_day_ran_out(db, tenant):
    from datetime import UTC, datetime

    from sqlalchemy import update

    from editor import models as m

    from editor import repo

    approved_draft(db, tenant)
    # The archive reads the sources the newsroom follows.
    with db.tenant(repo.archive_of(db, tenant)) as s:
        s.execute(update(m.sources).where(m.sources.c.shared.is_(True)).values(
            status="active", last_fetched_at=datetime.now(UTC),
            last_error="CoreTrouble: Core fetch limit: daily fetches limit reached (2000 of 2000)"))
    c = logged_in(db, tenant, "editor")
    assert "finito le letture del giorno" in c.get("/").text
    page = c.get("/sources").text
    assert "Ultimo errore di lettura" in page and "daily fetches limit reached" in page


def test_the_desk_shows_every_site_or_the_one_chosen(db, tenant):
    from editor import repo
    approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        other = repo.ensure_site(s, tenant, "altro.example", "Altro")
    c = logged_in(db, tenant, "owner")
    # Every site by default, "Tutti i siti" chosen in the menu.
    page = c.get("/").text
    assert "· tutti i siti</span>" in page and '<option value="all" selected>' in page
    assert page.count('href="/switch?site=') >= 2 or page.count("altro.example") >= 2
    # One site chosen: the Desk is that site only, and the menu says so.
    r = c.get(f"/switch?site={other}&back=/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = c.get("/").text
    assert "· altro.example</span>" in page and f'<option value="{other}" selected>' in page
    assert "Siti più attivi" not in page and "1 sito" in page
    # Its numbers, not the newsroom's: the new site has no sources yet.
    import re
    kpi = re.search(r'<span class="num">(\d+)</span><small>fonti attive</small>', page)
    assert kpi and kpi.group(1) == "0"
    # Back to all: to the Desk, whatever page it was chosen on.
    r = c.get("/switch?site=all&back=/sources", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "· tutti i siti</span>" in c.get("/").text
    # The pages of one site keep the last site chosen.
    assert "Fonti · altro.example" in c.get("/sources").text
    assert c.get("/switch?site=nope").status_code == 404


def test_the_navigation_folds_into_a_hamburger_on_a_narrow_screen(db, tenant):
    """The sections of a dashboard with a dozen links filled the whole screen
    on a phone: below 820px they fold into a hamburger, opened by its label
    (a checkbox, so no script is needed)."""
    owner = logged_in(db, tenant, "owner")
    page = owner.get("/").text
    assert 'id="nav-toggle"' in page and 'class="nav-burger"' in page
    assert '<div class="nav-body">' in page
    css = page.split("</style>")[0]
    assert ".nav-toggle:checked ~ .nav-body" in css and "max-width: 820px" in css


def test_the_logo_is_an_image_on_every_shell(db, tenant):
    """The logo is a real <img> in the newsroom and in the console, never
    markup shown as text."""
    from editor import accounts
    from editor.db import Database
    from test_billing import unique

    page = logged_in(db, tenant, "owner").get("/").text
    assert '<img class="logo" src="/static/redazia-logo.webp"' in page and "&amp;lt;" not in page and "&lt;img" not in page
    admin = Database.from_url(db.admin_url)
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c = TestClient(create_app(db))
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin").text
    assert '<img class="logo" src="/static/redazia-logo.webp"' in page and "&lt;img" not in page
    assert c.get("/static/redazia-logo.webp").headers["content-type"] == "image/webp"


def test_the_desk_counts_the_proposals_that_came_out_as_articles(db, tenant, monkeypatch):
    """Instead of the model's cost (Francesco 2026-11-15): of the proposals of
    the last 30 days, how many came out as an article."""
    from editor import models as m
    from editor import publishing as pub
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    target = pub.add_target(db, tenant, site_id, "webhook", "hook", {"url": "https://hooks.example/x"}, "HOOK")
    c = logged_in(db, tenant, "owner")
    assert "Pubblicate su proposte, 30 giorni" in c.get("/").text and "0/1" in c.get("/").text
    with db.tenant(tenant) as s:
        s.execute(m.publications.insert().values(tenant_id=tenant, draft_id=did, target_id=target, mode="publish",
                                                 payload={}, status="published", requested_by="francesco",
                                                 decided_by="francesco"))
    page = c.get("/").text
    assert "1/1" in page and "100% delle proposte è uscito come articolo" in page
    assert "Costo modello, 30 giorni" not in page


def test_owner_chooses_row_by_row_and_never_sees_the_json(db, tenant):
    from test_profile import V2, V4
    _, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        p = m.editorial_profiles
        s.execute(update(p).where(p.c.status == "approved").values(body=dict(V2)))
        approved = s.execute(text("SELECT version FROM editor.editorial_profiles WHERE status = 'approved'")).scalar()
        from editor import profile as prof
        prof.save_draft(s, tenant, site_id, dict(V4), "reanalysis", based_on=approved)
        draft = s.execute(text("SELECT max(version) FROM editor.editorial_profiles")).scalar()
    owner = logged_in(db, tenant, "owner")
    page = owner.get("/site").text
    assert "Approva le scelte" in page and 'name="take" value="gone:Satelliti"' in page and "checked" in page
    assert "Profilo (JSON)" not in page and "<textarea" not in owner.get("/site?edit=1").text
    assert 'name="sector"' in owner.get("/site?edit=1").text
    r = owner.post(f"/ui/sites/{site_id}/choose", follow_redirects=False,
                   data={"draft": draft, "approved": approved, "take": ["tone", "topic:Fibra", "topic:Politica"]})
    assert r.status_code == 303
    assert "con le tue scelte" in owner.get("/site").text
    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.status == "approved")).scalar()
    assert body["sector"]["value"] == "Tecnologia" and body["style"]["tone"] == "informativo"
    assert {x["name"] for x in body["subtopics"]} == {"Fibra", "Wi-Fi", "Politica", "Satelliti"}
    # The page of an older choice: nothing changes, the owner is told.
    r = owner.post(f"/ui/sites/{site_id}/choose", data={"draft": draft, "approved": approved, "take": ["tone"]})
    assert "Nel frattempo il profilo è cambiato" in r.text
    assert logged_in(db, tenant, "editor").post(f"/ui/sites/{site_id}/choose", data={"draft": 1, "approved": 1}).status_code == 403
    r = owner.post(f"/ui/sites/{site_id}/profile", data={"sector": "Telecomunicazioni", "audience": "Professionisti",
                                                         "tone": "informativo"}, follow_redirects=False)
    assert r.status_code == 303 and "Profilo aggiornato" in owner.get("/site").text
