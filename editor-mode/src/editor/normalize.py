"""Canonical URLs and content fingerprints."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track the visitor and never change the page.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
    "igshid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "ref", "ref_src",
    "ref_url", "si", "spm", "cmpid", "ncid", "ocid", "vero_id", "oly_enc_id",
}
TRACKING_PREFIXES = ("utm_", "pk_", "hsa_", "__s")

_ARXIV = re.compile(r"^/(?:abs|pdf|html)/([^/]+?)(?:v\d+)?(?:\.pdf)?/?$")
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k.startswith(TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """Same page, same string: https, lowercase host without www and default
    port, no fragment, no tracking parameters, sorted query, no trailing slash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if host in ("arxiv.org", "export.arxiv.org"):
        host = netloc = "arxiv.org"
        m = _ARXIV.match(path)
        if m:
            path = f"/abs/{m.group(1)}"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    )
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    return sha256_hex(canonical_url(url))


def clean_text(text: str | None) -> str:
    """Plain text from a feed field: no tags, entities decoded, spaces collapsed."""
    if not text:
        return ""
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", text))).strip()


def normalize_text(text: str) -> str:
    """Text for comparison: NFKC, lowercase, no punctuation, single spaces."""
    t = unicodedata.normalize("NFKC", clean_text(text)).lower()
    t = "".join(c if c.isalnum() else " " for c in t)
    return _SPACE.sub(" ", t).strip()


def content_hash(title: str, summary: str = "") -> str:
    return sha256_hex(normalize_text(f"{title} {summary}"))
