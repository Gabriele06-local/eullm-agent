"""Settings of the installation entered from the console (/admin ›
Impostazioni) instead of the .env file (Francesco, 2026-11-20: "il file .env
è proprio necessario? Per chi non ha accesso al server potrebbe essere un
problema").

What stays in .env is what the services need before they can read the
database, or what must never sit next to it: the database passwords, the
Core tokens, EDITOR_SECRET_KEY, ports and the version. The operational
settings (email, Mollie, covers, Search Console, Telegram, Turnstile, time
zone, retention, public address) can live in the table editor.app_settings;
the secret ones are encrypted with EDITOR_SECRET_KEY, like the publishing
credentials (vault.py), and are never shown again, only their last four
characters.

A value in the environment (.env) always wins over the one in the panel, and
the console says so. A value in the environment equal to the built-in
default does not count as a choice: the compose files of older installations
pass EDITOR_TIMEZONE=Europe/Rome and similar even when nobody wrote them.

When the services start, `load` puts the stored values into os.environ for
the names the environment leaves empty, so every module keeps reading its
setting as it always did. A change in the panel restarts the dashboard and
the worker (the worker first finishes the jobs it is running): under Docker
the restart policy brings them back with the new values in seconds.
"""

from __future__ import annotations

import base64
import logging
import os
import signal
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, func, insert, select

from . import models as m
from . import vault

log = logging.getLogger(__name__)

N_ = str  # a text marked for translation, translated where it is shown


@dataclass(frozen=True)
class Field:
    name: str  # the environment variable
    group: str
    label: str
    secret: bool = False
    kind: str = "text"  # text, number, email, url, select, textarea, password
    default: str = ""
    hint: str = ""
    placeholder: str = ""
    choices: tuple = ()


GROUPS = (
    ("email", N_("Email"), N_("Il server che spedisce inviti, reimpostazioni della password, briefing e rapporti. "
                             "Senza, i link degli inviti si mostrano a chi li crea, da mandare a mano.")),
    ("general", N_("Indirizzo e orari"), N_("L'indirizzo pubblico del pannello, a cui puntano i link delle email, "
                                            "il fuso orario predefinito e quanto si conservano le notizie raccolte.")),
    ("payments", N_("Pagamenti"), N_("Pagamenti online con Mollie. Senza chiave i piani si pagano con bonifico e si "
                                     "registrano da Clienti.")),
    ("images", N_("Copertine"), N_("Le immagini disegnate per ogni articolo. Senza chiave le bozze non hanno immagine.")),
    ("gsc", N_("Google Search Console"), N_("Un solo account di servizio Google per tutte le redazioni: ogni cliente "
                                            "aggiunge la sua email alla propria proprietà, con permesso Limitato.")),
    ("telegram", N_("Telegram"), N_("Il bot che manda il briefing a chi lo riceve su Telegram.")),
    ("readers", N_("Feedback dei lettori"), N_("Cloudflare Turnstile ferma i messaggi automatici nel modulo dei "
                                               "lettori. Senza, valgono solo i limiti per email e indirizzo.")),
)

FIELDS = (
    Field("EDITOR_SMTP_HOST", "email", N_("Server SMTP"), placeholder="mail.example.com",
          hint=N_("Con STARTTLS: la porta 587 di quasi tutti i provider.")),
    Field("EDITOR_SMTP_PORT", "email", N_("Porta"), kind="number", default="587"),
    Field("EDITOR_SMTP_USER", "email", N_("Utente"), placeholder="noreply@example.com"),
    Field("EDITOR_SMTP_PASSWORD", "email", N_("Password"), secret=True),
    Field("EDITOR_MAIL_FROM", "email", N_("Mittente"), kind="email", placeholder="Redazia <noreply@example.com>",
          hint=N_("Vuoto: l'utente SMTP.")),
    Field("EDITOR_PUBLIC_URL", "general", N_("Indirizzo pubblico del pannello"), kind="url",
          placeholder="https://app.example.com", hint=N_("Dove si apre il pannello, dietro il proxy con HTTPS.")),
    Field("EDITOR_TIMEZONE", "general", N_("Fuso orario"), default="Europe/Rome",
          hint=N_("Quello delle redazioni che non ne scelgono uno.")),
    Field("EDITOR_RETENTION_DAYS", "general", N_("Giorni di conservazione delle notizie raccolte"), kind="number",
          default="30", hint=N_("Le notizie più vecchie e i lavori finiti si cancellano, tranne quelle citate negli "
                                "articoli. 0 le tiene tutte.")),
    Field("EDITOR_MOLLIE_API_KEY", "payments", N_("Chiave API di Mollie"), secret=True,
          hint=N_("test_… per provare, poi live_…: da Mollie › Sviluppatori › Chiavi API.")),
    Field("EDITOR_IMAGES_API_KEY", "images", N_("Chiave delle immagini"), secret=True,
          hint=N_("OpenRouter (sk-or-…) usa openai/gpt-image-2, circa 5 centesimi a immagine in qualità media; "
                  "Black Forest Labs usa FLUX sul nodo europeo, circa 3 centesimi.")),
    Field("EDITOR_IMAGES_MODEL", "images", N_("Modello (facoltativo)"), placeholder="openai/gpt-image-2",
          hint=N_("Vuoto: quello predefinito del provider.")),
    Field("EDITOR_IMAGES_QUALITY", "images", N_("Qualità"), kind="select", default="medium",
          choices=(("low", N_("bassa")), ("medium", N_("media")), ("high", N_("alta")))),
    Field("EDITOR_GSC_KEY", "gsc", N_("Chiave JSON dell'account di servizio"), secret=True, kind="textarea",
          hint=N_("Incolla il file JSON scaricato da Google Cloud (API Search Console attiva).")),
    Field("EDITOR_TELEGRAM_TOKEN", "telegram", N_("Token del bot"), secret=True, hint=N_("Da @BotFather.")),
    Field("EDITOR_TURNSTILE_SITEKEY", "readers", N_("Chiave del sito"), placeholder="0x4AAAA…"),
    Field("EDITOR_TURNSTILE_SECRET", "readers", N_("Chiave segreta"), secret=True),
)
BY_NAME = {f.name: f for f in FIELDS}

POLL_SECONDS = 15
_AAD = b"app-settings\0"
_real: dict[str, str] | None = None  # the environment as the process found it, before `load`
_loaded_stamp: str | None = None


class Invalid(ValueError):
    """A text to show; {label} in it is the field's name."""

    def __init__(self, text: str, label: str = ""):
        super().__init__(text)
        self.text, self.label = text, label


def _real_env() -> dict[str, str]:
    global _real
    if _real is None:
        _real = {f.name: os.environ.get(f.name, "").strip() for f in FIELDS}
    return _real


def from_environment(name: str, env: dict | None = None) -> bool:
    """Whether the environment (.env) sets this value: then it wins."""
    value = (_real_env() if env is None else {n: (env.get(n) or "").strip() for n in BY_NAME}).get(name, "")
    return bool(value) and value != BY_NAME[name].default


def _seal(key: bytes, name: str, value: str) -> str:
    nonce = os.urandom(vault.NONCE_BYTES)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, value.encode(), _AAD + name.encode())).decode()


def _open(key: bytes, name: str, sealed: str) -> str:
    raw = base64.b64decode(sealed)
    return AESGCM(key).decrypt(raw[:vault.NONCE_BYTES], raw[vault.NONCE_BYTES:], _AAD + name.encode()).decode()


def _rows(admin) -> list:
    with admin.engine.connect() as conn:
        return conn.execute(select(m.app_settings)).all()


def stored(admin) -> tuple[dict[str, str], dict[str, str]]:
    """The values in the panel, decrypted, and the reason of each one that
    cannot be read (another EDITOR_SECRET_KEY)."""
    values, broken = {}, {}
    rows = _rows(admin)
    try:
        key = vault.key_for(admin) if any(r.secret for r in rows) else None
    except vault.VaultError:
        key = None
    for r in rows:
        if r.name not in BY_NAME:
            continue
        if not r.secret:
            values[r.name] = r.value
        elif key is None:
            broken[r.name] = "nokey"
        elif vault.key_id(key) != r.key_id:
            broken[r.name] = "otherkey"
        else:
            try:
                values[r.name] = _open(key, r.name, r.value)
            except (InvalidTag, ValueError):
                broken[r.name] = "otherkey"
    return values, broken


def stamp(admin) -> str:
    """Changes whenever a value of the panel changes."""
    with admin.engine.connect() as conn:
        n, last = conn.execute(select(func.count(), func.max(m.app_settings.c.updated_at))).one()
    return f"{n}:{last}"


def effective(admin, env: dict | None = None) -> dict[str, str]:
    """The environment with the values of the panel where it leaves them
    empty: what the services use after their next start."""
    out = dict(os.environ if env is None else env)
    base = _real_env() if env is None else {n: (env.get(n) or "").strip() for n in BY_NAME}
    out.update(base)
    values, _ = stored(admin)
    for name, value in values.items():
        if not from_environment(name, None if env is None else env):
            out[name] = value
    return out


def load(admin) -> None:
    """At the start of a service: the values of the panel into os.environ.
    Never fails the start: without the table (before the migrations) or the
    key, the environment alone is used."""
    global _loaded_stamp
    _real_env()
    try:
        values, broken = stored(admin)
        _loaded_stamp = stamp(admin)
    except Exception as e:
        log.info("settings of the panel not read (%s): the environment alone is used", type(e).__name__)
        return
    for name, value in values.items():
        if not from_environment(name):
            os.environ[name] = value
    for name, why in broken.items():
        log.warning("setting %s of the panel cannot be read (%s): enter it again in /admin › Impostazioni", name, why)


def pending(admin) -> bool:
    """Whether the panel changed since this process read it."""
    try:
        return _loaded_stamp is not None and stamp(admin) != _loaded_stamp
    except Exception:
        return False


def _check(f: Field, value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) > (16384 if f.kind == "textarea" else 500):
        raise Invalid(N_("{label}: valore troppo lungo."))
    if f.kind == "number":
        if not value.isdigit() or int(value) > (65535 if f.name == "EDITOR_SMTP_PORT" else 3650):
            raise Invalid(N_("{label}: serve un numero intero."))
        if f.name == "EDITOR_SMTP_PORT" and int(value) < 1:
            raise Invalid(N_("{label}: serve un numero intero."))
        value = str(int(value))
    if f.kind == "url":
        if not value.startswith(("https://", "http://")) or " " in value:
            raise Invalid(N_("{label}: serve un indirizzo che inizia con https://."))
        value = value.rstrip("/")
    if f.kind == "select" and value not in {c[0] for c in f.choices}:
        raise Invalid(N_("{label}: valore non previsto."))
    if f.name == "EDITOR_TIMEZONE":
        try:
            ZoneInfo(value)
        except Exception:
            raise Invalid(N_("{label}: fuso orario sconosciuto (per esempio Europe/Rome).")) from None
    if f.name == "EDITOR_MOLLIE_API_KEY" and not value.startswith(("test_", "live_")):
        raise Invalid(N_("{label}: la chiave di Mollie inizia con test_ o live_."))
    if f.name == "EDITOR_GSC_KEY":
        from . import searchconsole

        if searchconsole.load_key({"EDITOR_GSC_KEY": value}) is None:
            raise Invalid(N_("{label}: non è la chiave JSON di un account di servizio Google."))
    if f.kind not in ("textarea",) and any(c in value for c in "\r\n\0"):
        raise Invalid(N_("{label}: valore non valido."))
    return value


def save(admin, form: dict[str, str], clear: set[str], who: str) -> list[str]:
    """Store the values of the form: an empty secret keeps the stored one,
    an empty plain value goes back to the default; `clear` deletes stored
    secrets. Names set in the environment are left alone. Returns the
    names changed. Raises Invalid with a text to show (placeholder {label})."""
    checked: dict[str, str] = {}
    for f in FIELDS:
        if from_environment(f.name) or f.name not in form:
            continue
        try:
            checked[f.name] = _check(f, form[f.name] or "")
        except Invalid as e:
            raise Invalid(e.text, f.label) from None
    values, _ = stored(admin)
    secret_names = {f.name for f in FIELDS if f.secret}
    key = None
    if any(v for n, v in checked.items() if n in secret_names):
        key = vault.key_for(admin)
        if key is None:
            raise Invalid(N_("Questo server non ha EDITOR_SECRET_KEY: le chiavi non si possono salvare cifrate."), "")
    changed, now = [], datetime.now(UTC)
    with admin.engine.begin() as conn:
        for f in FIELDS:
            if from_environment(f.name):
                continue
            if f.secret and f.name in clear:
                if conn.execute(delete(m.app_settings).where(m.app_settings.c.name == f.name)).rowcount:
                    changed.append(f.name)
                continue
            if f.name not in checked:
                continue
            value = checked[f.name]
            if f.secret and not value:
                continue  # empty: the stored one stays
            if value == values.get(f.name, "") or (not f.secret and value in ("", f.default)
                                                    and f.name not in values):
                continue
            conn.execute(delete(m.app_settings).where(m.app_settings.c.name == f.name))
            if not f.secret and value in ("", f.default):
                changed.append(f.name)  # back to the default
                continue
            conn.execute(insert(m.app_settings).values(
                name=f.name, value=_seal(key, f.name, value) if f.secret else value, secret=f.secret,
                key_id=vault.key_id(key) if f.secret else None, updated_at=now, updated_by=who[:200]))
            changed.append(f.name)
    if changed:
        log.info("settings changed from the console by %s: %s", who, ", ".join(changed))
    return changed


def mask(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def view(admin) -> dict:
    """What the console shows: each group with its fields, where each value
    comes from (env, panel, default) and what is in use."""
    values, broken = stored(admin)
    with admin.engine.connect() as conn:
        meta = {r.name: r for r in conn.execute(select(m.app_settings.c.name, m.app_settings.c.updated_at,
                                                       m.app_settings.c.updated_by)).all()}
    real = _real_env()
    groups = []
    for code, title, lead in GROUPS:
        items = []
        for f in (x for x in FIELDS if x.group == code):
            env = from_environment(f.name)
            value = real[f.name] if env else values.get(f.name, "")
            source = "env" if env else ("panel" if f.name in values else ("broken" if f.name in broken else "default"))
            item = {"f": f, "source": source, "value": "" if f.secret else value,
                    "shown": mask(value) if (f.secret and value) else value,
                    "set": bool(value), "why": broken.get(f.name), "meta": meta.get(f.name)}
            if f.name == "EDITOR_GSC_KEY" and value:
                from . import searchconsole

                k = searchconsole.load_key({"EDITOR_GSC_KEY": value})
                item["account"] = k.client_email if k else ""
            items.append(item)
        groups.append({"code": code, "title": title, "lead": lead, "items": items})
    try:
        has_key = vault.key_for(admin) is not None
    except vault.VaultError:
        has_key = False
    return {"groups": groups, "has_key": has_key, "restarts": supervised(), "pending": pending(admin)}


def supervised() -> bool:
    """Whether a restart brings the service back by itself (Docker's restart
    policy): only then a change in the panel restarts the services."""
    flag = os.environ.get("EDITOR_RESTART_ON_SETTINGS", "").strip().lower()
    if flag in ("0", "no", "false", "off"):
        return False
    return flag in ("1", "yes", "true", "on") or os.path.exists("/.dockerenv")


def watch(admin, on_change, every: float = POLL_SECONDS) -> threading.Thread | None:
    """A thread that calls on_change once the panel changes."""
    if not supervised() or _loaded_stamp is None:
        return None

    def loop():
        while True:
            threading.Event().wait(every)
            if pending(admin):
                log.info("the settings of the panel changed: restarting to use them")
                on_change()
                return

    th = threading.Thread(target=loop, name="settings-watch", daemon=True)
    th.start()
    return th


def restart_self() -> None:
    """The web process: uvicorn stops gracefully on SIGTERM, Docker starts it again."""
    os.kill(os.getpid(), signal.SIGTERM)
