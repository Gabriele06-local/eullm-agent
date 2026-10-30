"""Updates from the console: the latest release is read from GitHub, an
operator asks for it, the request waits in the folder update.sh reads."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from editor import updates
from editor.console import INSTALLED


def github(tag="v99.0.0", status=200):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, json={"tag_name": tag, "html_url": f"https://github.com/r/{tag}",
                                            "published_at": "2026-10-29T10:00:00Z"})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_the_latest_release_is_read_every_six_hours(tmp_path):
    http, calls = github()
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    assert updates.check(tmp_path, http, now)["version"] == "99.0.0"
    assert updates.check(tmp_path, http, now + timedelta(hours=5))["version"] == "99.0.0"
    assert len(calls) == 1
    updates.check(tmp_path, http, now + timedelta(hours=7))
    assert len(calls) == 2
    # GitHub down: what was known stays, with the reason.
    down, _ = github(status=503)
    out = updates.check(tmp_path, down, now + timedelta(hours=8), force=True)
    assert out["version"] == "99.0.0" and out["error"]
    assert updates.newer("0.21.0", "0.20.9") and updates.newer("1.0.0", "0.99.99") and not updates.newer("0.20.1", "0.20.1")


def test_an_operator_asks_for_the_update_from_the_console(db, tmp_path):
    from editor import accounts
    from editor.api import create_app
    from editor.db import Database
    from fastapi.testclient import TestClient
    from test_billing import unique

    admin = Database.from_url(db.admin_url)
    c = TestClient(create_app(db, updates_dir=str(tmp_path)))
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin").text
    assert "Aggiornamenti" in page and INSTALLED in page and "Il server è aggiornato" in page

    (tmp_path / "available.json").write_text(json.dumps({
        "version": "99.0.0", "url": "https://github.com/r/v99.0.0", "checked_at": datetime.now(UTC).isoformat()}))
    page = c.get("/admin").text
    assert "È disponibile la versione 99.0.0" in page and "Aggiorna alla 99.0.0" in page
    # Only the announced release: nothing else reaches the host.
    c.post("/admin/updates/apply", data={"version": "98.0.0"})
    c.post("/admin/updates/apply", data={"version": "99.0.0; rm -rf /"})
    assert not (tmp_path / "request.json").exists()
    r = c.post("/admin/updates/apply", data={"version": "99.0.0"}, follow_redirects=False)
    # Back to the updates section, not to the top of the console.
    assert r.status_code == 303 and r.headers["location"] == "/admin#aggiornamenti"
    asked = json.loads((tmp_path / "request.json").read_text())
    assert asked["version"] == "99.0.0" and asked["by"] == op
    assert "chiesto: il server lo prende entro un minuto" in c.get("/admin").text
    # A second click while it waits asks nothing more.
    c.post("/admin/updates/apply", data={"version": "99.0.0"})
    assert json.loads((tmp_path / "request.json").read_text())["at"] == asked["at"]
    # update.sh took it and wrote how it went.
    (tmp_path / "request.json").unlink()
    (tmp_path / "status.json").write_text(json.dumps({"state": "failed", "step": "pull", "version": "99.0.0",
                                                      "from": INSTALLED, "at": "2026-10-29T20:00:00Z"}))
    (tmp_path / "update.log").write_text("manifest unknown\nfailed during: pull\n")
    page = c.get("/admin").text
    assert "non riuscito durante: download della versione" in page and "manifest unknown" in page

    # Not reachable without the console.
    other = TestClient(create_app(db, updates_dir=str(tmp_path)))
    assert other.post("/admin/updates/apply", data={"version": "99.0.0"}).status_code in (401, 403)


def test_without_the_folder_the_console_says_how(db):
    from editor import accounts
    from editor.api import create_app
    from editor.db import Database
    from fastapi.testclient import TestClient
    from test_billing import unique

    admin = Database.from_url(db.admin_url)
    c = TestClient(create_app(db))
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    assert "update.sh" in c.get("/admin").text
