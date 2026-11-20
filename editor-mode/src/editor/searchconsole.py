"""Google Search Console per site: what people search on Google to find the
site, and which of its pages they click.

One Google service account for the whole installation (EDITOR_GSC_KEY, the
JSON key, raw or base64; or EDITOR_GSC_KEY_FILE, its path). The customer
adds the account's email as a user of the property in Search Console
(permission "Restricted" is enough) and writes the property in Sito e
profilo: `sc-domain:example.com` for a domain property, or the URL prefix
`https://example.com/`. Nothing else to sign in to, and the customer can
take the access back from Search Console at any time.

Every day, at the newsroom's maintenance, the last READ_DAYS days of pages
and queries are read again (Google completes its numbers in two or three
days) and kept in `search_stats`. They are used by:

- the Opportunity Score, component "search": how much a topic matches what
  people search to reach the site (opportunity.py);
- the weekly report and the site's page: clicks, impressions, position,
  the pages and the searches that bring the most visits.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import case, delete, func, select, update

from . import models as m
from .text import tokens

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/webmasters/v3"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
READ_DAYS = 10
KEEP_DAYS = 480  # sixteen months, like Search Console itself
ROW_LIMIT = 5000


class SearchConsoleError(Exception):
    pass


@dataclass
class Key:
    client_email: str
    private_key: str
    token_uri: str = TOKEN_URL


def load_key(env=None) -> Key | None:
    """The service account of the installation, or None when not configured."""
    env = os.environ if env is None else env
    raw = (env.get("EDITOR_GSC_KEY") or "").strip()
    path = (env.get("EDITOR_GSC_KEY_FILE") or "").strip()
    if not raw and path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            log.warning("EDITOR_GSC_KEY_FILE %s cannot be read", path)
            return None
    if not raw:
        return None
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except ValueError:
            log.warning("EDITOR_GSC_KEY is neither JSON nor base64")
            return None
    try:
        data = json.loads(raw)
        return Key(data["client_email"], data["private_key"], data.get("token_uri") or TOKEN_URL)
    except (ValueError, KeyError):
        log.warning("EDITOR_GSC_KEY is not a Google service account key")
        return None


def normalise_property(value: str, domain: str = "") -> str:
    """What the customer wrote, as Search Console names the property."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("sc-domain:"):
        return "sc-domain:" + v.split(":", 1)[1].strip().lower().strip("/")
    if v.startswith(("http://", "https://")):
        return v if v.endswith("/") else v + "/"
    # A bare domain: the domain property, the one that covers www and every protocol.
    return "sc-domain:" + v.lower().strip("/")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Client:
    """The Search Console API with the installation's service account."""

    def __init__(self, key: Key, http: httpx.Client | None = None, clock=time.time):
        self.key = key
        self.http = http or httpx.Client(timeout=30)
        self.clock = clock
        self._token: tuple[str, float] | None = None

    def token(self) -> str:
        now = self.clock()
        if self._token and self._token[1] > now + 60:
            return self._token[0]
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({"iss": self.key.client_email, "scope": SCOPE, "aud": self.key.token_uri,
                                  "iat": int(now), "exp": int(now) + 3600}).encode())
        pk = serialization.load_pem_private_key(self.key.private_key.encode(), password=None)
        signature = pk.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        r = self.http.post(self.key.token_uri, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": f"{header}.{claims}.{_b64(signature)}"})
        if r.status_code != 200:
            raise SearchConsoleError(f"Google refused the service account ({r.status_code})")
        body = r.json()
        self._token = (body["access_token"], now + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _call(self, method: str, path: str, **kwargs) -> dict:
        r = self.http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {self.token()}"}, **kwargs)
        if r.status_code in (401, 403):
            raise SearchConsoleError("no_access")
        if r.status_code == 404:
            raise SearchConsoleError("not_found")
        if r.status_code >= 400:
            raise SearchConsoleError(f"Search Console answered {r.status_code}")
        return r.json()

    def check(self, prop: str) -> str:
        """The permission the account has on the property."""
        body = self._call("GET", f"/sites/{quote(prop, safe='')}")
        level = body.get("permissionLevel", "")
        if level in ("", "siteUnverifiedUser"):
            raise SearchConsoleError("no_access")
        return level

    def rows(self, prop: str, start: date, end: date, dimension: str) -> list[dict]:
        body = self._call("POST", f"/sites/{quote(prop, safe='')}/searchAnalytics/query", json={
            "startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date", dimension],
            "rowLimit": ROW_LIMIT, "dataState": "all"})
        return body.get("rows", [])


def problem(code: str | None) -> str | None:
    """The stored status, as the customer reads it (translated where shown)."""
    return {
        None: None, "ok": None,
        "no_access": "L'account di Redazia non ha accesso alla proprietà: aggiungilo in Search Console › "
                     "Impostazioni › Utenti e autorizzazioni.",
        "not_found": "Search Console non conosce questa proprietà: controlla come è scritta.",
    }.get(code, code)


def set_property(db, tenant_id: str, site_id: int, value: str, client: Client | None) -> str | None:
    """Save the site's property and check the access at once; returns the
    status code ("ok", "no_access", ...), None when the property was removed."""
    with db.tenant(tenant_id) as s:
        domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise LookupError(site_id)
        prop = normalise_property(value, domain)
        if not prop:
            s.execute(update(m.sites).where(m.sites.c.id == site_id).values(
                gsc_property=None, gsc_status=None, gsc_checked_at=None))
            s.execute(delete(m.search_stats).where(m.search_stats.c.site_id == site_id))
            return None
    status = "ok"
    if client is not None:
        try:
            client.check(prop)
        except SearchConsoleError as e:
            status = str(e)
        except httpx.HTTPError as e:
            status = f"Search Console non risponde ({type(e).__name__})"
    with db.tenant(tenant_id) as s:
        s.execute(update(m.sites).where(m.sites.c.id == site_id).values(
            gsc_property=prop, gsc_status=status, gsc_checked_at=datetime.now(UTC)))
    return status


def sync(db, tenant_id: str, client: Client | None, today: date | None = None, site_id: int | None = None) -> dict:
    """Read the last days of every site with a property. Returns rows per site."""
    if client is None:
        return {}
    today = today or datetime.now(UTC).date()
    start, end = today - timedelta(days=READ_DAYS), today - timedelta(days=1)
    with db.tenant(tenant_id) as s:
        q = select(m.sites.c.id, m.sites.c.gsc_property).where(m.sites.c.gsc_property.is_not(None))
        if site_id is not None:
            q = q.where(m.sites.c.id == site_id)
        sites = s.execute(q).all()
    out = {}
    for site in sites:
        status, rows = "ok", []
        try:
            for kind in ("page", "query"):
                for r in client.rows(site.gsc_property, start, end, kind):
                    day, key = r["keys"][0], r["keys"][1]
                    rows.append({"day": date.fromisoformat(day), "kind": kind, "key": key[:1000],
                                 "clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                                 "position": round(float(r.get("position", 0)), 2) if r.get("position") else None})
        except SearchConsoleError as e:
            status = str(e)
        except httpx.HTTPError as e:
            status = f"Search Console non risponde ({type(e).__name__})"
        with db.tenant(tenant_id) as s:
            if status == "ok":
                st = m.search_stats
                s.execute(delete(st).where(st.c.site_id == site.id, st.c.day >= start))
                s.execute(delete(st).where(st.c.site_id == site.id, st.c.day < today - timedelta(days=KEEP_DAYS)))
                if rows:
                    s.execute(st.insert(), [{"tenant_id": tenant_id, "site_id": site.id, **r} for r in rows])
            s.execute(update(m.sites).where(m.sites.c.id == site.id).values(
                gsc_status=status, gsc_checked_at=datetime.now(UTC)))
        out[site.id] = len(rows) if status == "ok" else status
    return out


def client_from_env(env=None) -> Client | None:
    key = load_key(env)
    return Client(key) if key else None


# --- reading what was kept -------------------------------------------------------


def totals(s, site_id: int, start: date, end: date) -> dict:
    """Clicks, impressions and average position of the site's pages in [start, end]."""
    st = m.search_stats.c
    r = s.execute(select(func.coalesce(func.sum(st.clicks), 0), func.coalesce(func.sum(st.impressions), 0),
                         func.sum(st.position * st.impressions),
                         func.sum(case((st.position.is_not(None), st.impressions), else_=0)))
                  .where(st.site_id == site_id, st.kind == "page", st.day >= start, st.day <= end)).one()
    clicks, impressions, weighted, weighted_impressions = int(r[0]), int(r[1]), r[2], int(r[3] or 0)
    return {"clicks": clicks, "impressions": impressions,
            "position": round(weighted / weighted_impressions, 1) if weighted_impressions and weighted is not None else None}


def top(s, site_id: int, kind: str, start: date, end: date, limit: int = 5) -> list[dict]:
    st = m.search_stats.c
    rows = s.execute(select(st.key, func.sum(st.clicks).label("clicks"), func.sum(st.impressions).label("impressions"),
                            func.sum(st.position * st.impressions).label("w"),
                            func.sum(case((st.position.is_not(None), st.impressions), else_=0)).label("wi"))
                     .where(st.site_id == site_id, st.kind == kind, st.day >= start, st.day <= end)
                     .group_by(st.key).order_by(func.sum(st.clicks).desc(), func.sum(st.impressions).desc())
                     .limit(limit)).all()
    return [{"key": r.key, "clicks": int(r.clicks), "impressions": int(r.impressions),
             "position": round(r.w / r.wi, 1) if r.wi and r.w is not None else None} for r in rows]


def demand(s, site_id: int, today: date | None = None, days: int = 28, limit: int = 300) -> list[tuple[frozenset, float]]:
    """The searches that bring people to the site in the last weeks, as
    (words, weight): a click counts like ten impressions."""
    today = today or datetime.now(UTC).date()
    st = m.search_stats.c
    rows = s.execute(select(st.key, func.sum(st.clicks).label("c"), func.sum(st.impressions).label("i"))
                     .where(st.site_id == site_id, st.kind == "query", st.day >= today - timedelta(days=days))
                     .group_by(st.key).order_by((func.sum(st.clicks) * 10 + func.sum(st.impressions)).desc())
                     .limit(limit)).all()
    out = []
    for r in rows:
        words = frozenset(tokens(r.key))
        if words:
            out.append((words, float(r.c) * 10 + float(r.i)))
    return out


def match(terms: set[str], queries: list[tuple[frozenset, float]]) -> float:
    """How much a topic matches the site's searches, from 0 to 1: the
    weighted share of each search's words found in the topic, against the
    strongest search. A topic that matches the search bringing the most
    visits scores 1; one that matches nothing, 0."""
    if not queries or not terms:
        return 0.0
    best = max(w for _, w in queries)
    got = sum(w * len(q & terms) / len(q) for q, w in queries if q & terms)
    return round(min(1.0, math.sqrt(got / best)), 4) if best else 0.0
