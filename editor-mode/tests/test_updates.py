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


def test_the_check_goes_through_redazia_and_counts_the_installation(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url)
        if request.url.host == "app.redazia.com":
            return httpx.Response(200, json={"version": "99.1.0", "url": "https://github.com/r/v99.1.0"})
        return httpx.Response(200, json={"tag_name": "v99.0.0", "html_url": "https://github.com/r/v99.0.0"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    iid = updates.install_id("a secret key")
    assert updates.INSTALL_ID.match(iid) and iid == updates.install_id("a secret key") != updates.install_id("other")
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    assert updates.check(tmp_path, http, now, installed="0.24.0", iid=iid)["version"] == "99.1.0"
    sent = calls[-1]
    assert sent.host == "app.redazia.com" and sent.params["id"] == iid and sent.params["v"] == "0.24.0"
    assert set(sent.params) == {"id", "v"}  # nothing else leaves the installation
    # Telemetry off: GitHub directly, nothing sent to us.
    monkeypatch.setenv("EDITOR_TELEMETRY", "off")
    calls.clear()
    assert updates.check(tmp_path, http, now, force=True, installed="0.24.0", iid=iid)["version"] == "99.0.0"
    assert [c.host for c in calls] == ["api.github.com"]
    monkeypatch.delenv("EDITOR_TELEMETRY")
    # Our address down: GitHub answers instead.
    def down(request):
        if request.url.host == "app.redazia.com":
            return httpx.Response(502)
        return handler(request)
    http = httpx.Client(transport=httpx.MockTransport(down))
    assert updates.check(tmp_path, http, now, force=True, installed="0.24.0", iid=iid)["version"] == "99.0.0"
    # Without the shared folder the check still runs (and is kept in memory).
    assert updates.check(None, http, now, force=True, installed="0.24.0", iid=iid)["version"] == "99.0.0"


def test_the_console_counts_the_installations(db, monkeypatch):
    import time

    from editor import accounts, installs
    from editor.api import create_app
    from editor.db import Database
    from fastapi.testclient import TestClient
    from test_billing import unique

    admin = Database.from_url(db.admin_url)
    monkeypatch.setitem(installs._release, "at", time.monotonic())
    monkeypatch.setitem(installs._release, "release", {"version": "99.0.0", "url": "https://github.com/r/v99.0.0",
                                                         "published_at": None})
    c = TestClient(create_app(db))
    a, b = updates.install_id(unique("a")), updates.install_id(unique("b"))
    before = installs.summary(admin)
    r = c.get("/api/public/latest", params={"v": "0.24.0", "id": a})
    assert r.status_code == 200 and r.json()["version"] == "99.0.0"
    c.get("/api/public/latest", params={"v": "0.24.0", "id": a})
    c.get("/api/public/latest", params={"v": "0.23.0", "id": b})
    # Nonsense is answered but not counted.
    assert c.get("/api/public/latest", params={"v": "x; drop", "id": "nope"}).status_code == 200
    now = installs.summary(admin)
    assert now["seen"] == before["seen"] + 2 and now["active"] == before["active"] + 2
    assert installs.record(admin, a, "0.24.1") and installs.summary(admin)["seen"] == now["seen"]

    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin").text
    assert "Installazioni di Redazia" in page and "Attive negli ultimi 7 giorni" in page and "versione 0.24.1" in page
