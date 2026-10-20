"""Accounts, the operator console and the provisioning API: newsrooms made
by the billing system, people invited by email, roles, suspension."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from editor import accounts, quotas
from editor.api import create_app
from editor.db import Database


@pytest.fixture
def admin(db):
    a = Database.from_url(db.admin_url)
    yield a
    a.engine.dispose()


def app_for(db, **kw):
    sent = []
    app = create_app(db, mail=(lambda to, subject, body: sent.append((to, subject, body))) if kw.pop("mail", False)
                     else None, **kw)
    return TestClient(app), sent


def operator_key(db):
    return {"Authorization": f"Bearer {accounts.create_operator_token(Database.from_url(db.admin_url), 'billing')}"}


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def test_passwords_are_hashed_and_wrong_ones_lock_the_account(db, admin):
    uid = accounts.create_user(admin, "Anna@Example.com", "Anna", "una password lunga")
    with admin.engine.connect() as conn:
        u = accounts.find_user(conn, "anna@example.com")
    assert u.id == uid and u.password_hash.startswith("scrypt$") and "lunga" not in u.password_hash
    assert accounts.authenticate(admin, "anna@example.com", "una password lunga").id == uid
    now = datetime.now(UTC)
    for _ in range(accounts.MAX_FAILED_LOGINS):
        assert accounts.authenticate(admin, "anna@example.com", "sbagliata", now) is None
    with pytest.raises(accounts.AccountError, match="troppi tentativi"):
        accounts.authenticate(admin, "anna@example.com", "una password lunga", now)
    later = now + timedelta(minutes=accounts.LOCK_MINUTES + 1)
    assert accounts.authenticate(admin, "anna@example.com", "una password lunga", later).id == uid
    assert accounts.authenticate(admin, "nessuno@example.com", "x") is None
    with pytest.raises(accounts.AccountError):
        accounts.create_user(admin, "corta@example.com", password="corta")


@pytest.mark.postgres
def test_the_billing_system_creates_a_newsroom_and_its_owner_joins(db, admin):
    c, sent = app_for(db, mail=True, public_url="https://editor.example.com")
    key = operator_key(db)
    assert c.get("/api/operator/tenants").status_code == 401
    assert c.get("/api/operator/tenants", headers={"Authorization": "Bearer edo_nope"}).status_code == 401
    body = {"name": "Redazione Città", "owner_email": "Capo@Example.com", "external_ref": "sub_123",
            "plan": "pro", "limits": {"max_sites": 2, "max_drafts_month": 30}}
    r = c.post("/api/operator/tenants", json=body, headers=key)
    assert r.status_code == 201, r.text
    out = r.json()
    tid = out["tenant"]["tenant_id"]
    assert tid.startswith("redazione-citta") and out["created"]
    assert out["tenant"]["plan"] == "pro" and out["tenant"]["usage"]["sites"] == {"used": 0, "limit": 2}
    invite = out["owner"]["invite"]
    assert invite["sent"] and invite["url"].startswith("https://editor.example.com/invite/edi_")
    assert sent[0][0] == "capo@example.com" and invite["url"] in sent[0][2]
    # The same payment notice again: the same newsroom, no second invite.
    again = c.post("/api/operator/tenants", json=body, headers=key)
    assert again.status_code == 200 and again.json()["tenant"]["tenant_id"] == tid and not again.json()["created"]
    assert [t["tenant_id"] for t in c.get("/api/operator/tenants?external_ref=sub_123", headers=key).json()["tenants"]] == [tid]
    # The owner opens the link, chooses a password and is in.
    page = c.get(f"/invite/{token_of(invite['url'])}")
    assert page.status_code == 200 and "Redazione Città" in page.text
    r = c.post(f"/invite/{token_of(invite['url'])}", data={"password": "password del capo", "again": "password del capo",
                                                            "name": "Capo"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = c.get("/")
    assert home.status_code == 200 and "capo@example.com · owner" in home.text
    # A link works once.
    assert c.get(f"/invite/{token_of(invite['url'])}").text.count("Link non valido") == 1
    # Later, with email and password.
    c.post("/logout")
    assert c.post("/login", data={"email": "capo@example.com", "password": "sbagliata"}).status_code == 401
    r = c.post("/login", data={"email": "capo@example.com", "password": "password del capo"}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/settings").status_code == 200
    # The plan is the operator's: the limit holds.
    with db.tenant(tid) as s:
        assert quotas.usage(s).plan["max_sites"] == 2


@pytest.mark.postgres
def test_suspending_a_newsroom_closes_it_and_patch_changes_limits(db, admin):
    class Core:
        delegate = True
        calls = []

        def set_limits(self, tenant, limits):
            self.calls.append((tenant, limits))
            return {"tenant": tenant, "effective": limits}

    core = Core()
    c, _ = app_for(db, core=core)
    key = operator_key(db)
    tid = c.post("/api/operator/tenants", json={"name": "Sospesa", "owner_email": "s@example.com",
                                                "limits": {"max_llm_cost_month": 10}}, headers=key).json()["tenant"]["tenant_id"]
    assert core.calls == [(tid, {"max_cost_per_month": 10})]
    uid = accounts.create_user(admin, "s@example.com", password="password di s")
    session = accounts.start_session(admin, uid, tid)
    assert accounts.resolve_session(admin, session).role == "owner"
    r = c.patch(f"/api/operator/tenants/{tid}", json={"active": False, "limits": {"max_llm_cost_month": None}}, headers=key)
    assert r.status_code == 200 and r.json()["tenant"]["active"] is False
    assert core.calls[-1] == (tid, {})
    assert accounts.resolve_session(admin, session) is None
    assert c.patch("/api/operator/tenants/nope", json={"active": True}, headers=key).status_code == 404
    assert c.patch(f"/api/operator/tenants/{tid}", json={"timezone": "Mars/Base"}, headers=key).status_code == 422
    assert c.delete(f"/api/operator/tenants/{tid}", headers=key).status_code == 400
    assert c.delete(f"/api/operator/tenants/{tid}?confirm={tid}", headers=key).status_code == 200
    assert c.get(f"/api/operator/tenants/{tid}", headers=key).status_code == 404
    # People stay; their membership went with the newsroom.
    assert accounts.memberships(admin, uid) == []


def test_owners_invite_people_with_a_role(db, tenant, admin):
    c, _ = app_for(db)
    owner = accounts.create_user(admin, "owner@example.com", password="password owner")
    with admin.engine.begin() as conn:
        accounts.set_member(conn, owner, tenant, "owner")
    c.post("/login", data={"email": "owner@example.com", "password": "password owner"})
    page = c.post("/ui/people/invite", data={"email": "giulia@example.com", "role": "editor"})
    assert page.status_code == 200 and "Mandagli questo link" in page.text
    url = page.text.split('<div class="token">')[1].split("<")[0]
    assert "giulia@example.com" in c.get("/settings").text
    g = TestClient(c.app)
    assert g.post(f"/invite/{token_of(url)}", data={"password": "corta", "again": "corta"}).status_code == 400
    assert g.post(f"/invite/{token_of(url)}", data={"password": "password di giulia", "again": "diversa!!!!"}).status_code == 400
    r = g.post(f"/invite/{token_of(url)}", data={"password": "password di giulia", "again": "password di giulia"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "giulia@example.com · editor" in g.get("/").text
    assert "Persone della redazione" not in g.get("/settings").text
    assert g.post("/ui/people/invite", data={"email": "x@example.com", "role": "owner"}).status_code in (303, 403)
    giulia = next(p for p in accounts.members(admin, tenant) if p.email == "giulia@example.com")
    # The last owner stays; a removed person is out at once.
    assert "almeno un owner" in c.post(f"/ui/people/{owner}/role", data={"role": "editor"}, follow_redirects=True).text
    c.post(f"/ui/people/{giulia.id}/remove")
    assert g.get("/", follow_redirects=False).headers["location"] == "/login"


def test_operator_console_and_entering_a_newsroom(db, tenant, admin):
    c, _ = app_for(db)
    accounts.create_user(admin, "op@example.com", password="password operatore", operator=True)
    accounts.create_user(admin, "anna@example.com", password="password di anna")
    other = TestClient(c.app)
    other.post("/login", data={"email": "anna@example.com", "password": "password di anna"})
    assert other.get("/admin", follow_redirects=False).headers["location"] == "/login"
    r = c.post("/login", data={"email": "op@example.com", "password": "password operatore"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"
    page = c.get("/admin")
    assert page.status_code == 200 and tenant in page.text and "Chiavi API" in page.text
    key = c.post("/admin/keys", data={"name": "pagamenti"})
    assert "edo_" in key.text
    assert tenant in {t["tenant_id"] for t in c.get("/api/operator/tenants").json()["tenants"]}
    r = c.get(f"/admin/enter/{tenant}", follow_redirects=False)
    assert r.headers["location"] == "/"
    home = c.get("/")
    assert "op@example.com · owner" in home.text and "Amministrazione" in home.text
    reset = c.post("/admin/reset", data={"email": "anna@example.com"})
    url = reset.text.split('<div class="token">')[1].split("<")[0]
    TestClient(c.app).post(f"/invite/{token_of(url)}", data={"password": "nuova password", "again": "nuova password"})
    assert accounts.authenticate(admin, "anna@example.com", "nuova password") is not None
    # A form from another site is refused.
    evil = c.post("/admin/keys", data={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


def test_the_local_installation_has_one_newsroom(db, tenant, admin):
    if not db.sqlite:
        pytest.skip("SQLite only")
    c, _ = app_for(db)
    r = c.post("/api/operator/tenants", json={"name": "Seconda", "owner_email": "x@example.com"}, headers=operator_key(db))
    assert r.status_code == 400 and "one newsroom" in r.json()["error"]


@pytest.mark.postgres
def test_on_a_server_the_owner_role_runs_it_all(pg_urls):
    """Tests migrate as a superuser; the server migrates and runs the
    console as editor_owner, an ordinary role. Do the same on a database of
    its own: create a newsroom, sign in, list, delete."""
    import uuid

    import psycopg

    from conftest import APP_PASSWORD, _with_db
    from editor import migrate, newsrooms, repo

    plain = pg_urls[0].rsplit("/", 1)[0] + "/postgres"
    name, role = f"editor_owner_{uuid.uuid4().hex[:8]}", f"owner_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER NOBYPASSRLS")
        conn.execute(f'CREATE DATABASE "{name}"')
        conn.execute(f'GRANT CONNECT, CREATE ON DATABASE "{name}" TO {role}, editor_app')
    with psycopg.connect(_with_db(plain, name), autocommit=True) as conn:
        conn.execute(f"GRANT CREATE ON SCHEMA public TO {role}")
    owner_url = _with_db(plain, name, role, "pw")
    try:
        migrate.upgrade(owner_url)
        admin, app = Database.from_url(owner_url), Database.from_url(_with_db(plain, name, "editor_app", APP_PASSWORD))
        try:
            res = newsrooms.create(admin, {"name": "Server", "owner_email": "own@example.com",
                                           "limits": {"max_sites": 1}}, "test")
            tid = res["tenant_id"]
            s = accounts.accept(admin, res["invite"], "password del server")
            assert s.tenant_id == tid
            session = accounts.start_session(admin, s.user_id, tid)
            assert accounts.resolve_session(admin, session).role == "owner"
            with app.tenant(tid) as ss:
                repo.ensure_site(ss, tid, "server.example", "Server")
            assert newsrooms.listing(admin, app, tid)[0]["usage"]["sites"] == {"used": 1, "limit": 1}
            # The application role reads no account of anyone.
            with psycopg.connect(_with_db(plain, name, "editor_app", APP_PASSWORD)) as conn:
                for table in ("users", "memberships", "sessions", "invites", "operator_tokens"):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute(f"SELECT count(*) FROM editor.{table}")
                    conn.rollback()
            newsrooms.delete(admin, tid)
            assert accounts.resolve_session(admin, session) is None
        finally:
            admin.engine.dispose()
            app.engine.dispose()
    finally:
        with psycopg.connect(plain, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            conn.execute(f"DROP ROLE {role}")


def test_user_add_refuses_an_unknown_newsroom(db, tenant, admin, monkeypatch, capsys):
    from editor import cli, runtime

    monkeypatch.setattr(runtime, "admin_database", lambda: admin)
    with pytest.raises(SystemExit, match=f"no newsroom ID .*{tenant}"):
        cli.main(["user-add", "boss@example.com", "--operator", "--tenant", "ID"])
    with admin.engine.connect() as conn:
        assert accounts.find_user(conn, "boss@example.com") is None
    cli.main(["user-add", "boss@example.com", "--operator", "--tenant", tenant])
    password = capsys.readouterr().out.split("password: ")[1].strip()
    assert accounts.authenticate(admin, "boss@example.com", password).operator
    assert [x.role for x in accounts.memberships(admin, accounts.authenticate(admin, "boss@example.com", password).id)] == ["owner"]


def test_the_first_administrator_is_made_from_the_browser(db, admin, capsys, monkeypatch):
    from editor import cli, runtime

    if not db.sqlite:
        pytest.skip("needs a database with no administrator: the PostgreSQL one is shared by the tests")
    monkeypatch.setattr(runtime, "admin_database", lambda: admin)
    c, _ = app_for(db)
    assert "Prima configurazione" in c.get("/login").text
    cli.main(["setup-link"])
    first = capsys.readouterr().out.rsplit("/setup/", 1)[1].split()[0]
    cli.main(["setup-link"])
    link = capsys.readouterr().out.rsplit("/setup/", 1)[1].split()[0]
    # A new link replaces the old one; the link is no invite.
    assert "Link non valido" in c.get(f"/setup/{first}").text
    assert "Link non valido" in c.get(f"/invite/{link}").text
    assert "Crea l'amministratore" in c.get(f"/setup/{link}").text
    bad = c.post(f"/setup/{link}", data={"email": "boss@example.com", "password": "password boss", "again": "altra"})
    assert bad.status_code == 400
    r = c.post(f"/setup/{link}", data={"email": "Boss@Example.com", "name": "Boss", "password": "password boss",
                                       "again": "password boss"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert "Redazioni" in c.get("/admin").text
    # Once there is an administrator, no more links and no more setup.
    assert c.post(f"/setup/{link}", data={"email": "x@example.com", "password": "password xxxx",
                                          "again": "password xxxx"}).status_code == 400
    cli.main(["setup-link"])
    assert "exists already" in capsys.readouterr().out
    assert "Prima configurazione" not in TestClient(c.app).get("/login").text
    assert accounts.pending_invites(admin, None) == []
