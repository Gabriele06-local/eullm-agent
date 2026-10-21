import socket

from fastapi.testclient import TestClient
from sqlalchemy import text

from editor import auth
from editor.api import create_app
from editor.ui import safe_path
from test_publishing import approved_draft, public_resolver

PAGES = ["/", "/site", "/site?edit=1", "/sources", "/sources?status=all", "/trends", "/drafts",
         "/publications", "/settings"]


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
        settings = c.get("/settings").text
        assert ("Crea token" in settings) == (role == "owner")
        assert ("Persone della redazione" in settings) == (role == "owner")


def test_forms_check_the_role_and_leave_a_notice(db, tenant):
    setup = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        source = s.execute(text("SELECT id FROM editor.sources LIMIT 1")).scalar()
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
    r = c.post(f"/ui/drafts/{did}/publish", data={"target_id": tid, "mode": "draft"})
    assert "Pubblicazione chiesta" in r.text and "Approva e invia" in r.text


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
        source = s.execute(text("SELECT id FROM editor.sources LIMIT 1")).scalar()
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
