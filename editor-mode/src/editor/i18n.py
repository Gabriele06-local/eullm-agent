"""The dashboard's five languages: English, Italian, German, French, Spanish.

The texts are written in Italian in the templates and in the code, wrapped
in ``_()`` (templates) or ``t()`` (Python); ``N_()`` only marks a text kept
in a table, translated where it is shown. The other languages live in
``i18n/<lang>.json``, keyed by the Italian text; a text missing there falls
back to Italian, and tests/test_i18n.py fails until it is added.

Placeholders are written ``{name}`` and filled by keyword. In templates the
catalogue is trusted markup (it may hold <strong>), the values are escaped.

The language of a request comes from the ``editor_lang`` cookie, chosen at
the bottom of every page, else from the browser's Accept-Language, else
EDITOR_LANGUAGE (English by default).
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from functools import cache
from pathlib import Path

from markupsafe import Markup

LANGS = ("en", "it", "de", "fr", "es")
NAMES = {"en": "English", "it": "Italiano", "de": "Deutsch", "fr": "Français", "es": "Español"}
SOURCE = "it"
DEFAULT = "en"
COOKIE = "editor_lang"
_DIR = Path(__file__).parent / "i18n"

MONTHS = {
    "it": ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."],
    "fr": ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."],
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"],
}
DAYS = {
    "it": ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "fr": ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"],
    "es": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"],
}

_current: ContextVar[str] = ContextVar("editor_lang", default=SOURCE)


@cache
def catalog(lang: str) -> dict[str, str]:
    if lang == SOURCE:
        return {}
    path = _DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def pick(cookie: str | None, accept: str | None) -> str:
    """The cookie's language if valid, else the browser's best, else English."""
    if cookie in LANGS:
        return cookie
    best = []
    for i, part in enumerate((accept or "").split(",")):
        tag, _, q = part.strip().partition(";q=")
        code = tag.split("-")[0].lower()
        if code in LANGS:
            try:
                best.append((float(q or 1), -i, code))
            except ValueError:
                continue
    if best:
        return max(best)[2]
    fallback = os.environ.get("EDITOR_LANGUAGE", DEFAULT)
    return fallback if fallback in LANGS else DEFAULT


def use(lang: str):
    """Set the language of the current request (or job); returns a token."""
    return _current.set(lang if lang in LANGS else DEFAULT)


def current() -> str:
    return _current.get()


def _lookup(text: str, lang: str | None) -> str:
    lang = lang or _current.get()
    return catalog(lang).get(text, text)


def t(text: str, lang: str | None = None, **values) -> str:
    """A text for Python code (notices, errors, emails), as plain text."""
    out = _lookup(text, lang)
    return out.format(**values) if values else out


def gettext(text, **values) -> Markup:
    """``_()`` in templates: the translation as markup, values escaped."""
    if text is None:
        return Markup("")
    out = Markup(_lookup(str(text), None))
    return out.format(**values) if values else out


def N_(text: str) -> str:
    """Marks a text for translation without translating it here."""
    return text


def install(env) -> None:
    env.globals["_"] = gettext
    env.globals["lang"] = current
    env.globals["lang_names"] = NAMES
    env.globals["lang_codes"] = LANGS
