"""Credentials of the publishing targets, stored encrypted in the database:
entered from the dashboard, never shown again, bound to their tenant and
name, unreadable with another key."""

import hashlib
import hmac
import os
import socket
import stat
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from editor import auth, models as m, publishing as pub, vault
from editor.api import create_app
from test_publishing import approved_draft, public_resolver


@pytest.fixture
def key(monkeypatch):
    k = vault.new_key()
    monkeypatch.setenv(vault.KEY_ENV, k)
    return k


def test_a_secret_is_stored_encrypted_and_read_back(db, tenant, key):
    vault.put(db, tenant, "WP_PASS", "abcd efgh ijkl")
    with db.tenant(tenant) as s:
        row = s.execute(select(m.secrets)).one()
        assert vault.stored(s) == {"WP_PASS"}
    assert "abcd" not in row.ciphertext and row.key_id == vault.key_id(vault.key_for(db))
    assert vault.get(db, tenant, "WP_PASS") == "abcd efgh ijkl"
    vault.put(db, tenant, "WP_PASS", "new")  # replaced, not added
    assert vault.get(db, tenant, "WP_PASS") == "new"
    assert vault.get(db, tenant, "OTHER") is None
    assert vault.remove(db, tenant, "WP_PASS") and not vault.remove(db, tenant, "WP_PASS")
    assert vault.get(db, tenant, "WP_PASS") is None


def test_bad_names_and_values_are_refused(db, tenant, key):
    for name in ("lower", "A__B", "_X", "X" * 41):
        with pytest.raises(vault.VaultError, match="name"):
            vault.put(db, tenant, name, "v")
    for value in ("", "x" * 4097):
        with pytest.raises(vault.VaultError, match="empty or longer"):
            vault.put(db, tenant, "OK", value)


def test_a_ciphertext_moved_to_another_name_does_not_decrypt(db, tenant, key):
    vault.put(db, tenant, "A", "one")
    vault.put(db, tenant, "B", "two")
    with db.tenant(tenant) as s:
        a = s.execute(select(m.secrets.c.ciphertext).where(m.secrets.c.name == "A")).scalar()
        s.execute(update(m.secrets).where(m.secrets.c.name == "B").values(ciphertext=a))
    with pytest.raises(vault.VaultError, match="does not decrypt"):
        vault.get(db, tenant, "B")


def test_another_key_says_so(db, tenant, key, monkeypatch):
    vault.put(db, tenant, "A", "one")
    monkeypatch.setenv(vault.KEY_ENV, vault.new_key())
    with pytest.raises(vault.VaultError, match="another"):
        vault.get(db, tenant, "A")
    monkeypatch.setenv(vault.KEY_ENV, "too-short")
    with pytest.raises(vault.VaultError, match="32 bytes"):
        vault.get(db, tenant, "A")


@pytest.mark.postgres
def test_a_server_without_key_stores_nothing(db, tenant, monkeypatch):
    monkeypatch.delenv(vault.KEY_ENV, raising=False)
    assert vault.key_for(db) is None
    with pytest.raises(vault.VaultError, match="no EDITOR_SECRET_KEY"):
        vault.put(db, tenant, "A", "one")
    assert vault.get(db, tenant, "A") is None


def test_the_local_installation_keeps_its_key_next_to_the_database(tmp_path, monkeypatch):
    from editor.db import Database
    from editor import migrate

    monkeypatch.delenv(vault.KEY_ENV, raising=False)
    url = f"sqlite:///{tmp_path}/editor.db"
    migrate.upgrade(url)
    db = Database.from_url(url)
    first = vault.key_for(db)
    keyfile = Path(tmp_path) / vault.KEY_FILE
    assert len(first) == 32 and keyfile.exists()
    if os.name != "nt":  # Windows has no POSIX modes: os.open's 0o600 is ignored there
        assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600
    assert vault.key_for(db) == first  # the same key on every start


def published_hook(db, tenant, did, target):
    pid = pub.request(db, tenant, did, target, "publish", "redazione")
    pub.decide(db, tenant, pid, True, "francesco")
    with respx.mock() as router:
        route = router.post("https://hooks.example/in").mock(return_value=httpx.Response(200))
        out = pub.Publisher(resolver=public_resolver).run(db, tenant, pid)
    return out, route


def test_the_stored_secret_comes_before_the_environment(db, tenant, key, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "HOOK"), "from-env")
    target = pub.add_target(db, tenant, site_id, "webhook", "social", {"url": "https://hooks.example/in"}, "HOOK")
    out, route = published_hook(db, tenant, did, target)
    req = route.calls[0].request
    assert out["status"] == "published"
    assert req.headers["x-editor-signature"] == "sha256=" + hmac.new(b"from-env", req.content, hashlib.sha256).hexdigest()
    vault.put(db, tenant, "HOOK", "from-vault")
    with db.tenant(tenant) as s:
        s.execute(text("DELETE FROM editor.publications"))
    out, route = published_hook(db, tenant, did, target)
    req = route.calls[0].request
    assert req.headers["x-editor-signature"] == "sha256=" + hmac.new(b"from-vault", req.content, hashlib.sha256).hexdigest()


def test_the_owner_enters_credentials_and_never_reads_them(db, tenant, key, monkeypatch):
    _, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    owner = auth.create_token(db, tenant, "boss", "owner")
    editor = auth.create_token(db, tenant, "ed", "editor")
    c = TestClient(create_app(db))
    h = {"Authorization": f"Bearer {owner}"}
    r = c.post("/api/targets", headers=h, json={"site_id": site_id, "kind": "wordpress", "name": "wp",
                                                "config": {"url": "https://blog.example", "user": "redazione"},
                                                "secret_env": "WP", "secret": "app pass word"})
    assert r.status_code == 201
    r = c.post("/api/targets", headers=h, json={"site_id": site_id, "kind": "webhook", "name": "h",
                                                "config": {"url": "https://hooks.example/in"}, "secret": "x"})
    assert r.status_code == 400
    assert c.get("/api/secrets", headers=h).json() == {"secrets": ["WP"]}
    assert "app pass word" not in c.get("/api/targets", headers=h).text
    assert c.put("/api/secrets/WP", headers=h, json={"value": "changed"}).status_code == 200
    assert vault.get(db, tenant, "WP") == "changed"
    assert c.put("/api/secrets/bad", headers=h, json={"value": "x"}).status_code == 400
    eh = {"Authorization": f"Bearer {editor}"}
    assert c.get("/api/secrets", headers=eh).status_code == 403
    assert c.put("/api/secrets/WP", headers=eh, json={"value": "x"}).status_code == 403
    assert c.delete("/api/secrets/WP", headers=h).status_code == 200
    assert c.delete("/api/secrets/WP", headers=h).status_code == 404


def test_the_dashboard_form_stores_the_credential(db, tenant, key, monkeypatch):
    _, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    token = auth.create_token(db, tenant, "boss", "owner")
    c = TestClient(create_app(db))
    c.post("/login", data={"token": token})
    r = c.post("/ui/targets", data={"site_id": site_id, "kind": "wordpress", "name": "Blog",
                                    "address": "https://blog.example", "user": "redazione",
                                    "secret_env": "WP", "secret": "app pass word"})
    assert "salvata cifrata" in r.text
    page = c.get("/publications").text
    assert "app pass word" not in page and "WP" in page
    r = c.post("/ui/secrets/WP", data={"value": "nuova"})
    assert "Credenziale WP aggiornata" in r.text
    assert vault.get(db, tenant, "WP") == "nuova"
