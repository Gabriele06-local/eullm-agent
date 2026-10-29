"""Updates of the server installation, from the console.

The backoffice never touches Docker: it has no socket and no root. It reads
the latest release from GitHub (every six hours from the worker, and on
"Controlla adesso"), and when an operator asks for an update it leaves a
request in a folder shared with the host (EDITOR_UPDATES_DIR). On the host,
deploy/server/update.sh, run every minute by root's cron, takes the request,
makes a backup, pulls the images of that version, restarts and writes how it
went in the same folder; the console shows it.

Files in the folder: available.json (the latest release, written here),
request.json (written here, removed by the script), status.json and
update.log (written by the script).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

RELEASES = "https://api.github.com/repos/eullm/eullm-agent/releases/latest"
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
CHECK_EVERY = timedelta(hours=6)
STALE_REQUEST = timedelta(minutes=5)  # a request nobody took: the script is not installed


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


def check(where: Path, http=None, now: datetime | None = None, force: bool = False) -> dict | None:
    """The latest release, read from GitHub at most every six hours (or now,
    with force). A failed read keeps what was known and says why."""
    now = now or datetime.now(UTC)
    path = where / "available.json"
    known = _read(path) or {}
    checked = known.get("checked_at")
    if not force and checked and now - datetime.fromisoformat(checked) < CHECK_EVERY:
        return known
    try:
        client = http or httpx.Client(timeout=15, follow_redirects=True)
        r = client.get(RELEASES, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel = r.json()
        version = str(rel.get("tag_name") or "").removeprefix("v")
        if not VERSION.match(version):
            raise ValueError(f"unexpected tag {rel.get('tag_name')!r}")
        known = {"version": version, "url": rel.get("html_url") or "", "published_at": rel.get("published_at"),
                 "checked_at": now.isoformat(), "error": None}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("latest release not read: %s", e)
        known = {**known, "checked_at": now.isoformat(), "error": str(e)[:200]}
    _write(path, known)
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
