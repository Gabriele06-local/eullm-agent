"""Tokenising for topic detection (Italian and English)."""

from __future__ import annotations

import re
from collections import Counter

from .normalize import normalize_text

STOPWORDS = set(
    """
    a about above after again against all also am an and any are as at be because been before being
    below between both but by can could did do does doing down during each few for from further had
    has have having he her here hers him his how i if in into is it its itself just me more most my
    new no nor not now of off on once only or other our out over own same she should so some such
    than that the their them then there these they this those through to too under until up very
    was we were what when where which while who whom why will with would you your yours via vs
    show ask tell hn using use used how first one two get gets make makes based way ways
    il lo la i gli le un uno una di da del dello della dei degli delle al allo alla ai agli alle
    dal dallo dalla dai dagli dalle nel nello nella nei negli nelle sul sullo sulla sui sugli sulle
    con per tra fra che chi cui non come dove quando anche ma ed se più piu meno molto tutto tutti
    questo questa questi queste quello quella quelli quelle sono era essere stato stata ha hanno
    abbiamo avere fa fare può puo nuovo nuova nuovi nuove ecco ora già gia oggi anni anno sempre
    sua suo suoi sue loro nostro nostra cosa cose solo dopo prima ancora così cosi verso
    """.split()
)


_SHORT_HYPHEN = re.compile(r"\b(\w{1,3})-(\w{1,3})\b")
_ACRONYM = re.compile(r"\b[A-Z]{2}\b")


def tokens(text: str) -> list[str]:
    """Content words; short hyphenated pairs are joined (Wi-Fi -> wifi)."""
    acronyms = {a.lower() for a in _ACRONYM.findall(text or "")}
    out = []
    for t in normalize_text(_SHORT_HYPHEN.sub(r"\1\2", text or "")).split():
        if t in STOPWORDS and t not in acronyms:
            continue
        if len(t) >= 2:
            out.append(t)
    return out


def item_terms(title: str, summary: str = "", summary_terms: int = 8) -> set[str]:
    """Title words plus the most frequent summary words."""
    terms = set(tokens(title))
    common = Counter(tokens(summary)).most_common(summary_terms)
    return terms | {t for t, _ in common}


def top_terms(texts: list[str], n: int = 12) -> list[str]:
    # Ties keep the order of first appearance, so the same titles always give
    # the same terms (a set would follow the per-process string hash).
    counts = Counter()
    for t in texts:
        counts.update(dict.fromkeys(tokens(t), 1))
    return [t for t, _ in counts.most_common(n)]


def humanize(text: str) -> str:
    """Take out the marks that give away a machine-written text: dashes used
    as pauses (em dash, en dash, double hyphen) become commas, ellipses a
    full stop, typographic oddities plain characters. Ranges between numbers
    (2020–2024) and hyphenated words stay."""
    if not text:
        return text
    t = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", text)
    t = re.sub(r"\s*(?:—|–|--)\s*", ", ", t)
    t = re.sub(r"\s*(?:…|\.{3,})(?=\s+[a-zà-ÿ])", ",", t)  # a pause inside the sentence
    t = re.sub(r"\s*(?:…|\.{3,})", ".", t)
    t = re.sub(r",\s*([,.;:!?])", r"\1", t)
    t = re.sub(r"^\s*,\s*", "", t)
    t = re.sub(r"\.\.+", ".", t)
    return " ".join(t.split())



TITLE_COPY = 0.88  # this close to an outlet's headline, a title is a copy


def _title_words(title: str) -> str:
    return " ".join(re.findall(r"\w+", (title or "").lower()))


def copied_title(title: str, headlines) -> str | None:
    """The outlet's headline the title copies (the same words, or nearly),
    or None. A dish's own name alone is no copy: «Cotoletta alla milanese»
    stays its name, only the words around it must be the site's own
    (Francesco 2026-11-11)."""
    from difflib import SequenceMatcher

    mine = _title_words(title)
    if len(mine.split()) < 4:
        return None
    for h in headlines:
        theirs = _title_words(h)
        if theirs and (mine == theirs or SequenceMatcher(None, mine, theirs).ratio() >= TITLE_COPY):
            return h
    return None


# An item that says it is paid for (Francesco 2026-11-11: an energy offer
# went out as news): no proposal is made from it.
SPONSORED = re.compile(
    r"\b(sponsori[sz]{1,2}at[oaie]|sponsored|pubbli-?redazional[ei]|publiredazional[ei]|advertorial|"
    r"contenuto (?:pubblicitario|promozionale|sponsorizzato)|paid (?:post|content)|partner content|"
    r"in collaborazione con|in partnership con|anzeige|publireportage|publirreportaje|contenido patrocinado)\b"
    r"|\[(?:ad|adv|pr|promo)\]", re.I)


def sponsored(*texts: str) -> bool:
    return any(SPONSORED.search(x or "") for x in texts)
