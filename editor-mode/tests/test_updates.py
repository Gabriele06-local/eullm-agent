"""Updates from the console: the latest release is read from GitHub, an
operator asks for it, the request waits in the folder update.sh reads."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from editor import updates
from editor.console import INSTALLED


def github(tag="v99.0.0", status=200):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, json={"tag_name": tag, "html_url": f"https://github.com/r/{tag}",
                                            "published_at": "2026-10-29T10:00:00Z"})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_the_latest_release_is_read_every_hour(tmp_path):
    http, calls = github()
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    assert updates.check(tmp_path, http, now)["version"] == "99.0.0"
    assert updates.check(tmp_path, http, now + timedelta(minutes=40))["version"] == "99.0.0"
    assert len(calls) == 1
    updates.check(tmp_path, http, now + timedelta(minutes=55))  # the worker's next hourly round
    assert len(calls) == 2
    # Opening the page of updates checks again past ten minutes.
    updates.check(tmp_path, http, now + timedelta(minutes=60), every=updates.ON_PAGE)
    assert len(calls) == 2
    updates.check(tmp_path, http, now + timedelta(minutes=66), every=updates.ON_PAGE)
    assert len(calls) == 3
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
    page = c.get("/admin/updates").text
    assert "Aggiornamenti" in page and INSTALLED in page and "Il server è aggiornato" in page

    (tmp_path / "available.json").write_text(json.dumps({
        "version": "99.0.0", "url": "https://github.com/r/v99.0.0", "checked_at": datetime.now(UTC).isoformat()}))
    page = c.get("/admin/updates").text
    assert "È disponibile la versione 99.0.0" in page and "Aggiorna alla 99.0.0" in page
    # Only the announced release: nothing else reaches the host.
    c.post("/admin/updates/apply", data={"version": "98.0.0"})
    c.post("/admin/updates/apply", data={"version": "99.0.0; rm -rf /"})
    assert not (tmp_path / "request.json").exists()
    r = c.post("/admin/updates/apply", data={"version": "99.0.0"}, follow_redirects=False)
    # Back to the page of the updates, not to the top of the console.
    assert r.status_code == 303 and r.headers["location"] == "/admin/updates"
    asked = json.loads((tmp_path / "request.json").read_text())
    assert asked["version"] == "99.0.0" and asked["by"] == op
    page = c.get("/admin/updates").text
    assert "chiesto: il server lo prende in pochi secondi" in page and 'id="update-progress"' in page
    p = c.get("/admin/updates/progress").json()
    assert p["busy"] and p["pending"] and p["percent"] == 0
    # A second click while it waits asks nothing more.
    c.post("/admin/updates/apply", data={"version": "99.0.0"})
    assert json.loads((tmp_path / "request.json").read_text())["at"] == asked["at"]
    # update.sh took it and wrote how it went.
    (tmp_path / "request.json").unlink()
    (tmp_path / "status.json").write_text(json.dumps({"state": "failed", "step": "pull", "version": "99.0.0",
                                                      "from": INSTALLED, "at": "2026-10-29T20:00:00Z"}))
    (tmp_path / "update.log").write_text("manifest unknown\nfailed during: pull\n")
    page = c.get("/admin/updates").text
    assert "non riuscito durante: download della versione" in page and "manifest unknown" in page
    assert c.get("/admin/updates/progress").json()["state"] == "failed"

    # Done, but the server still answers the old version (v0.33.4 tagged on 0.33.3, 2026-11-09):
    # once the release is made again, the update can be asked once more.
    (tmp_path / "status.json").write_text(json.dumps({"state": "done", "step": "done", "version": "99.0.0",
                                                      "from": INSTALLED, "at": "2026-10-29T20:00:00Z"}))
    page = c.get("/admin/updates").text
    assert "Riprova l&#39;aggiornamento alla 99.0.0" in page or "Riprova l'aggiornamento alla 99.0.0" in page
    c.post("/admin/updates/apply", data={"version": "99.0.0"})
    assert json.loads((tmp_path / "request.json").read_text())["version"] == "99.0.0"
    (tmp_path / "request.json").unlink()

    # The last five updates in view, the older ones folded away.
    (tmp_path / "history.json").write_text(json.dumps({"updates": [
        {"state": "done", "from": f"1.0.{i}", "version": f"1.0.{i + 1}", "at": f"2026-11-0{9 - i}T10:00:00+00:00"}
        for i in range(9)]}))
    page = c.get("/admin/updates").text
    shown, older = page.split("Ultimi aggiornamenti", 1)[1].split("<details", 1)
    assert shown.count("<td class=\"num\" data-label=\"Dalla versione\">") == 5
    assert "Aggiornamenti precedenti (" in older and older.index("</details>") > older.index("1.0.8")

    # Not reachable without the console.
    other = TestClient(create_app(db, updates_dir=str(tmp_path)))
    assert other.post("/admin/updates/apply", data={"version": "99.0.0"}).status_code in (401, 403)
    assert other.get("/admin/updates/progress", follow_redirects=False).status_code in (303, 401, 403)


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
    assert "update.sh" in c.get("/admin/updates").text


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
    page = c.get("/admin/updates").text
    assert "Installazioni di Redazia" in page and "Attive negli ultimi 7 giorni" in page and "versione 0.24.1" in page


def test_an_update_in_progress_or_gone_wrong_is_not_offered_again(tmp_path):
    now = datetime(2026, 11, 3, 7, tzinfo=UTC)
    (tmp_path / "available.json").write_text(json.dumps({"version": "0.28.13"}))
    assert updates.state(tmp_path, "0.28.12", now)["newer"]
    updates.request(tmp_path, "0.28.13", "francesco", now)
    st = updates.state(tmp_path, "0.28.12", now)
    assert st["busy"]
    (tmp_path / "request.json").unlink()
    (tmp_path / "status.json").write_text(json.dumps({"state": "running", "step": "pull", "version": "0.28.13"}))
    st = updates.state(tmp_path, "0.28.12", now)
    assert st["busy"] and st["percent"] == 10  # an older script writes no percentage: the step gives one
    (tmp_path / "status.json").write_text(json.dumps({"state": "running", "step": "pull", "percent": 47,
                                                      "version": "0.28.13"}))
    assert updates.state(tmp_path, "0.28.12", now)["percent"] == 47
    # Done, but the images of 0.28.13 held 0.28.12: the console says so and offers a retry, for a release republished.
    (tmp_path / "status.json").write_text(json.dumps({"state": "done", "version": "0.28.13", "from": "0.28.13"}))
    st = updates.state(tmp_path, "0.28.12", now)
    assert st["mismatch"] == "0.28.13" and not st["newer"] and not st["busy"]
    (tmp_path / "available.json").write_text(json.dumps({"version": "0.28.14"}))
    assert updates.state(tmp_path, "0.28.12", now)["newer"]  # a new version is offered again


def test_backups_are_set_from_the_console_and_checked(db, tmp_path):
    from editor import accounts, backups
    from editor.api import create_app
    from editor.db import Database
    from fastapi.testclient import TestClient
    from test_billing import unique

    # The folder is written by root on the host: plain paths under the allowed roots only.
    for bad in ("/etc/redazia", "/home/app/public_html/b", "/home/../etc", "backup", "/home/a b", "/opt"):
        with pytest.raises(backups.Invalid):
            backups.check_dir(bad)
    assert backups.check_dir("/home/app/backup/") == "/home/app/backup"
    with pytest.raises(backups.Invalid):
        backups.check({"target": "-oProxyCommand=x@h:/b", "remote": "1"})
    with pytest.raises(backups.Invalid):
        backups.check({"remote": "1"})
    assert backups.check({"dir": ""})["dir"] == ""

    admin = Database.from_url(db.admin_url)
    c = TestClient(create_app(db, updates_dir=str(tmp_path)))
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin/backups").text
    assert "Backup del server" in page and "Nessun archivio ancora" in page
    c.post("/admin/backups/settings", data={"enabled": "1", "frequency": "weekly", "time": "04:15", "weekday": "1",
                                             "dir": "/home/app/backup", "keep": "30", "owner": "app:app",
                                             "remote": "1", "target": "backup@nas.example.it:/backup/redazia"})
    conf = (tmp_path / "backup.conf").read_text()
    assert "ENABLED=1" in conf and "HOUR=4" in conf and "MINUTE=15" in conf and "DIR=/home/app/backup" in conf
    assert "TARGET=backup@nas.example.it:/backup/redazia" in conf
    # A refused setting leaves the saved ones as they were.
    r = c.post("/admin/backups/settings", data={"dir": "/var/www/html"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/backups" and "DIR=/home/app/backup" in (tmp_path / "backup.conf").read_text()

    c.post("/admin/backups/now")
    assert (tmp_path / "backup-now.json").exists() and c.get("/admin/backups/state").json()["pending"]
    # The script took it and wrote how it went.
    (tmp_path / "backup-now.json").unlink()
    (tmp_path / "backup-status.json").write_text(json.dumps({
        "state": "idle", "bad": "", "dir": "/home/app/backup", "pubkey": "ssh-ed25519 AAAA redazia-backup@host",
        "result": {"at": "2026-11-03T10:00:00Z", "ok": True, "file": "redazia-2026-11-03_1000.tgz",
                   "size": 20971520, "remote": "done"},
        "archives": [{"name": "redazia-2026-11-03_1000.tgz", "size": 20971520, "at": "2026-11-03T10:00"}]}))
    page = c.get("/admin/backups").text
    assert "redazia-2026-11-03_1000.tgz" in page and "20.0 MB" in page and "ssh-ed25519 AAAA" in page
    assert "Copiato sul server remoto" in page
    c.post("/admin/backups/delete", data={"file": "../../etc/passwd"})
    assert not (tmp_path / "backup-delete.json").exists()
    assert 'id="bk-all"' in page and 'name="file" value="redazia-2026-11-03_1000.tgz"' in page
    r = c.post("/admin/backups/delete", data={})
    assert "Spunta almeno un archivio" in r.text and not (tmp_path / "backup-delete.json").exists()
    c.post("/admin/backups/delete", data={"file": "redazia-2026-11-03_1000.tgz"})
    assert json.loads((tmp_path / "backup-delete.json").read_text())["files"] == ["redazia-2026-11-03_1000.tgz"]
    # Several at once, added to those still waiting for the script; one bad name refuses them all.
    r = c.post("/admin/backups/delete", data={"file": ["redazia-2026-11-02_1000.tgz", "redazia-2026-11-01_1000.tgz"]})
    assert "2 archivi eliminati" in r.text
    assert json.loads((tmp_path / "backup-delete.json").read_text())["files"] == [
        "redazia-2026-11-03_1000.tgz", "redazia-2026-11-02_1000.tgz", "redazia-2026-11-01_1000.tgz"]
    c.post("/admin/backups/delete", data={"file": ["redazia-2026-10-31_1000.tgz", "../x.tgz"]})
    assert len(json.loads((tmp_path / "backup-delete.json").read_text())["files"]) == 3

    other = TestClient(create_app(db, updates_dir=str(tmp_path)))
    assert other.post("/admin/backups/now").status_code in (401, 403)


def test_opening_the_page_of_updates_shows_a_new_release(db, tmp_path, monkeypatch):
    from editor import accounts
    from editor.api import create_app
    from editor.db import Database
    from fastapi.testclient import TestClient
    from test_billing import unique

    http, calls = github("v99.2.0")
    monkeypatch.setenv("EDITOR_TELEMETRY", "off")
    monkeypatch.setattr(updates, "refresh", lambda where, installed, iid: updates.check(
        where, http, installed=installed, iid=iid, every=updates.ON_PAGE))
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    (tmp_path / "available.json").write_text(json.dumps({"version": INSTALLED, "checked_at": old}))
    admin = Database.from_url(db.admin_url)
    c = TestClient(create_app(db, updates_dir=str(tmp_path)))
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    assert "È disponibile la versione 99.2.0" in c.get("/admin/updates").text and len(calls) == 1
    c.get("/admin/updates")  # checked a moment ago: not again
    assert len(calls) == 1
