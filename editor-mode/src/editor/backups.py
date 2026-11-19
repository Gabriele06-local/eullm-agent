"""Backups of the server installation, set from the console.

Like updates (updates.py), the backoffice never runs them: it has no Docker
socket and no root. It writes the settings in the folder shared with the
host (EDITOR_UPDATES_DIR); deploy/server/update.sh, started by systemd as
soon as a file there changes and every minute by cron, runs backup.sh at
the time set, on "Backup adesso", copies the archives to another server
with rsync over SSH when asked, and writes how it went.

Files in the folder:
  backup.conf         the settings, KEY=value lines the script reads (written here)
  backup.json         the same settings for the console (written here)
  backup-now.json     a backup asked now (written here, removed by the script)
  backup-delete.json  the archives to delete (written here, removed by the script)
  backup-status.json  the last backup, the archives in the folder, the
                      public key for the remote copy (written by the script)
  backup.log          the log of the last backup (written by the script)

Every value is checked here and again by the script: the folder on the host
is written by root, so only plain absolute paths under the allowed roots,
never a folder a web server publishes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .i18n import N_
from .updates import _read, _write

DEFAULTS = {"enabled": False, "frequency": "daily", "hour": 3, "minute": 30, "weekday": 7,
            "dir": "", "keep": 14, "owner": "", "name": "redazia",
            "remote": False, "target": "", "port": 22}
FREQUENCIES = ("daily", "weekly")
# Where root may write the archives (the script checks the same list).
ROOTS = ("/home/", "/var/backups/", "/backup/", "/backups/", "/mnt/", "/media/", "/srv/", "/opt/")
WEB = re.compile(r"/(public_html|www|htdocs|html|web|httpdocs|wwwroot)(/|$)", re.I)
PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
OWNER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}(:[a-z_][a-z0-9_-]{0,31})?$")
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TARGET = re.compile(r"^[A-Za-z0-9._-]{1,64}@[A-Za-z0-9.-]{1,253}:[A-Za-z0-9._/~-]{1,200}$")
ARCHIVE = re.compile(r"^[A-Za-z0-9_-]{1,40}-\d{4}-\d{2}-\d{2}_\d{4}\.tgz$")
STALE = 300  # seconds: a request nobody took, the script is not installed or too old


class Invalid(ValueError):
    """A setting that cannot be used; the message says which, in Italian."""


def check_dir(path: str) -> str:
    path = (path or "").strip().rstrip("/")
    if not PATH.match(path) or "/../" in path + "/" or "/./" in path + "/" or "//" in path:
        raise Invalid(N_("La cartella deve essere un percorso assoluto semplice, come /home/app/backup."))
    if not (path + "/").startswith(ROOTS) or path.count("/") < 2:
        raise Invalid(N_("La cartella deve stare sotto /home, /var/backups, /backup, /backups, /mnt, /media, /srv o /opt."))
    if WEB.search(path):
        raise Invalid(N_("Non in una cartella pubblicata dal web server: gli archivi contengono tutte le chiavi."))
    return path


def check(values: dict) -> dict:
    """Settings from the console's form, checked; Invalid says what is wrong."""
    out = dict(DEFAULTS)
    out["enabled"] = bool(values.get("enabled"))
    freq = values.get("frequency") or "daily"
    if freq not in FREQUENCIES:
        raise Invalid(N_("Frequenza non valida."))
    out["frequency"] = freq
    try:
        out["hour"], out["minute"] = int(values.get("hour", 3)), int(values.get("minute", 30))
        out["weekday"], out["keep"] = int(values.get("weekday", 7)), int(values.get("keep", 14))
        out["port"] = int(values.get("port") or 22)
    except (TypeError, ValueError) as e:
        raise Invalid(N_("Ora, giorno, giorni da tenere e porta sono numeri.")) from e
    if not (0 <= out["hour"] <= 23 and 0 <= out["minute"] <= 59 and 1 <= out["weekday"] <= 7):
        raise Invalid(N_("Ora o giorno non validi."))
    if not 1 <= out["keep"] <= 3650:
        raise Invalid(N_("I giorni da tenere vanno da 1 a 3650."))
    if not 1 <= out["port"] <= 65535:
        raise Invalid(N_("Porta non valida."))
    # Empty: the backup folder of the installation (./backup).
    out["dir"] = check_dir(values["dir"]) if (values.get("dir") or "").strip() else ""
    owner = (values.get("owner") or "").strip()
    if owner and not OWNER.match(owner):
        raise Invalid(N_("Il proprietario è un utente, o utente:gruppo, del server (per esempio app:app)."))
    out["owner"] = owner
    name = (values.get("name") or DEFAULTS["name"]).strip()
    if not NAME.match(name):
        raise Invalid(N_("Il nome dei file usa lettere, numeri, - e _."))
    out["name"] = name
    out["remote"] = bool(values.get("remote"))
    target = (values.get("target") or "").strip()
    if target and not TARGET.match(target):
        raise Invalid(N_("Il server remoto si scrive utente@server:/cartella, per esempio backup@nas.esempio.it:/backup/redazia."))
    if out["remote"] and not target:
        raise Invalid(N_("Per la copia remota serve il server, nella forma utente@server:/cartella."))
    out["target"] = target
    return out


def settings(where: Path | None) -> dict:
    return {**DEFAULTS, **(_read(where / "backup.json") or {})} if where else dict(DEFAULTS)


def save(where: Path, values: dict, by: str, now: datetime | None = None) -> dict:
    cfg = check(values)
    lines = [f"{k.upper()}={'1' if v is True else '0' if v is False else v}" for k, v in cfg.items()]
    part = where / "backup.conf.part"
    part.write_text("\n".join(lines) + "\n", encoding="utf-8")
    part.replace(where / "backup.conf")
    _write(where / "backup.json", {**cfg, "by": by, "at": (now or datetime.now(UTC)).isoformat()})
    return cfg


def ask_now(where: Path, by: str, now: datetime | None = None) -> None:
    _write(where / "backup-now.json", {"by": by, "at": (now or datetime.now(UTC)).isoformat()})


def ask_delete(where: Path, names, by: str) -> list[str]:
    """One or more archives to delete; added to those still waiting, so two
    requests before the script runs both count."""
    names = [names] if isinstance(names, str) else list(names)
    names = list(dict.fromkeys(n for n in names if n))
    if not names or len(names) > 500 or not all(ARCHIVE.match(n) for n in names):
        raise Invalid(N_("Archivio non valido."))
    waiting = (_read(where / "backup-delete.json") or {}).get("files") or []
    files = list(dict.fromkeys([n for n in waiting if isinstance(n, str) and ARCHIVE.match(n)] + names))
    _write(where / "backup-delete.json", {"files": files, "by": by})
    return names


def state(where: Path | None, now: datetime | None = None) -> dict:
    """What the console shows: settings, last backup, archives, key."""
    out = {"enabled": where is not None, "settings": settings(where), "status": {}, "archives": [],
           "pending": False, "stale": False, "log": "", "pubkey": ""}
    if where is None:
        return out
    now = now or datetime.now(UTC)
    st = _read(where / "backup-status.json") or {}
    out["status"] = st
    out["archives"] = sorted(st.get("archives") or [], key=lambda a: a.get("name", ""), reverse=True)
    out["pubkey"] = st.get("pubkey") or ""
    asked = _read(where / "backup-now.json")
    out["pending"] = bool(asked) or st.get("state") == "running"
    if asked and asked.get("at"):
        out["stale"] = (now - datetime.fromisoformat(asked["at"])).total_seconds() > STALE
    try:
        out["log"] = "\n".join((where / "backup.log").read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
    except OSError:
        pass
    return out
