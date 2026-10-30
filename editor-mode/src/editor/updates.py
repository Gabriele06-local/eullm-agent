"""Updates of the server installation, from the console.

The backoffice never touches Docker: it has no socket and no root. It reads
the latest release from GitHub (every six hours from the worker, and on
"Controlla adesso"), and when an operator asks for an update it leaves a
request in a folder shared with the host (EDITOR_UPDATES_DIR). On the host,
deploy/server/update.sh, run every minute by root's cron, takes the request,
makes a backup, pulls the images of that version, restarts and writes how it
went in the same folder; the console shows it.

The latest release is asked first to app.redazia.com (EDITOR_UPDATES_URL),
which answers with GitHub's latest release and counts the installation:
only a random identifier derived from this installation's secret key and the
installed version are sent, nothing about newsrooms or users.
EDITOR_TELEMETRY=off asks GitHub directly and sends nothing. If our address
does not answer, GitHub is asked.

Files in the folder: available.json (the latest release, written here),
request.json (written here, removed by the script), status.json and
update.log (written by the script).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

RELEASES = "https://api.github.com/repos/eullm/eullm-agent/releases/latest"
LATEST = "https://app.redazia.com/api/public/latest"
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
CHECK_EVERY = timedelta(hours=6)
STALE_REQUEST = timedelta(minutes=5)  # a request nobody took: the script is not installed
INSTALL_ID = re.compile(r"^[0-9a-f]{24}$")
_memory: dict = {}  # what check() knows on an installation without the shared folder


def install_id(seed: str) -> str:
    """A stable identifier of this installation that says nothing about it
    (seed: the secret key, or the database address)."""
    return hashlib.sha256(f"redazia-install:{seed}".encode()).hexdigest()[:24]


def this_install() -> str:
    env = os.environ
    return install_id(env.get("EDITOR_SECRET_KEY") or env.get("EDITOR_DATABASE_URL") or "")


def telemetry() -> bool:
    return os.environ.get("EDITOR_TELEMETRY", "on").strip().lower() not in ("0", "off", "false", "no")


def _release(client, installed: str, iid: str) -> dict:
    """The latest release: from our address (which counts the installation)
    when allowed, otherwise or if it fails from GitHub."""
    if iid and telemetry():
        try:
            r = client.get(os.environ.get("EDITOR_UPDATES_URL", "").strip() or LATEST,
                           params={"v": installed, "id": iid})
            r.raise_for_status()
            rel = r.json()
            if VERSION.match(str(rel.get("version") or "")):
                return rel
            raise ValueError(f"unexpected answer {rel!r:.80}")
        except (httpx.HTTPError, ValueError) as e:
            log.info("latest release not read from %s, asking GitHub: %s", LATEST, e)
    r = client.get(RELEASES, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    rel = r.json()
    version = str(rel.get("tag_name") or "").removeprefix("v")
    if not VERSION.match(version):
        raise ValueError(f"unexpected tag {rel.get('tag_name')!r}")
    return {"version": version, "url": rel.get("html_url") or "", "published_at": rel.get("published_at")}


def folder(path: str) -> Path | None:
    """The shared folder, when this installation has one we can write in
    (Docker creates a missing one owned by root: update.sh install fixes it)."""
    if not path:
        return None
    p = Path(path)
    return p if p.is_dir() and os.access(p, os.W_OK) else None


def newer(a: str, b: str) -> bool:
    """Is version a newer than b?"""
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
    except ValueError:
        return False


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, data: dict) -> None:
    part = path.with_suffix(".part")
    part.write_text(json.dumps(data), encoding="utf-8")
    os.replace(part, path)


def check(where: Path | None, http=None, now: datetime | None = None, force: bool = False,
          installed: str = "", iid: str = "") -> dict | None:
    """The latest release, read at most every six hours (or now, with force).
    A failed read keeps what was known and says why. Without the shared
    folder it is kept in memory: the installation is still counted."""
    now = now or datetime.now(UTC)
    path = where / "available.json" if where is not None else None
    known = (_read(path) if path else _memory.get("known")) or {}
    checked = known.get("checked_at")
    if not force and checked and now - datetime.fromisoformat(checked) < CHECK_EVERY:
        return known
    try:
        client = http or httpx.Client(timeout=15, follow_redirects=True)
        rel = _release(client, installed, iid)
        known = {"version": rel["version"], "url": rel.get("url") or "", "published_at": rel.get("published_at"),
                 "checked_at": now.isoformat(), "error": None}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("latest release not read: %s", e)
        known = {**known, "checked_at": now.isoformat(), "error": str(e)[:200]}
    if path:
        _write(path, known)
    else:
        _memory["known"] = known
    return known


def request(where: Path, version: str, by: str, now: datetime | None = None) -> None:
    """Ask the host to move to this version (update.sh does it)."""
    if not VERSION.match(version):
        raise ValueError("version")
    _write(where / "request.json", {"version": version, "by": by, "at": (now or datetime.now(UTC)).isoformat()})


def state(where: Path | None, installed: str, now: datetime | None = None) -> dict:
    """What the console shows: installed and available versions, whether an
    update is asked or running, how the last one went."""
    out = {"enabled": where is not None, "installed": installed, "available": None, "newer": False,
           "pending": None, "stale": False, "status": None, "log": ""}
    if where is None:
        return out
    now = now or datetime.now(UTC)
    out["available"] = _read(where / "available.json")
    v = (out["available"] or {}).get("version")
    out["newer"] = bool(v) and newer(v, installed)
    out["pending"] = _read(where / "request.json")
    if out["pending"] and out["pending"].get("at"):
        out["stale"] = now - datetime.fromisoformat(out["pending"]["at"]) > STALE_REQUEST
    out["status"] = _read(where / "status.json")
    try:
        out["log"] = "\n".join((where / "update.log").read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
    except OSError:
        pass
    return out
