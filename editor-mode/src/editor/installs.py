"""The installations of Redazia, counted on app.redazia.com.

Every installation asks GET /api/public/latest for the latest release every
six hours (updates.py) with a random identifier and its version. The answer
is GitHub's latest release, read here at most every ten minutes; the
question is recorded, so the console can say how many installations run and
on which version. Nothing else is sent or kept: no address, no newsroom.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from . import models as m
from . import updates

log = logging.getLogger(__name__)

RELEASE_TTL = 600.0
ACTIVE = timedelta(days=7)  # checked within a week: still running
_release: dict = {"at": 0.0, "release": None}


def latest(http=None) -> dict | None:
    """GitHub's latest release, kept for ten minutes."""
    now = time.monotonic()
    if _release["release"] and now - _release["at"] < RELEASE_TTL:
        return _release["release"]
    try:
        client = http or httpx.Client(timeout=15, follow_redirects=True)
        r = client.get(updates.RELEASES, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel = r.json()
        version = str(rel.get("tag_name") or "").removeprefix("v")
        if not updates.VERSION.match(version):
            raise ValueError(f"unexpected tag {rel.get('tag_name')!r}")
        _release.update(at=now, release={"version": version, "url": rel.get("html_url") or "",
                                         "published_at": rel.get("published_at")})
    except (httpx.HTTPError, ValueError) as e:
        log.warning("latest release not read: %s", e)
    return _release["release"]


def record(admin, iid: str, version: str, now: datetime | None = None) -> bool:
    """One check of an installation; False when what it sent is not valid."""
    if not updates.INSTALL_ID.match(iid or "") or not updates.VERSION.match(version or ""):
        return False
    now = now or datetime.now(UTC)
    t = m.installations
    with admin.engine.begin() as conn:
        done = conn.execute(update(t).where(t.c.id == iid).values(
            version=version, last_seen=now, checks=t.c.checks + 1)).rowcount
    if not done:
        try:
            with admin.engine.begin() as conn:
                conn.execute(t.insert().values(id=iid, version=version, first_seen=now, last_seen=now, checks=1))
        except IntegrityError:  # two checks at the same moment
            return record(admin, iid, version, now)
    return True


def summary(admin, now: datetime | None = None) -> dict:
    """What the console shows: installations active in the last week, by
    version, new ones in the last 30 days, all those ever seen."""
    now = now or datetime.now(UTC)
    t = m.installations
    with admin.engine.connect() as conn:
        by_version = conn.execute(select(t.c.version, func.count()).where(t.c.last_seen >= now - ACTIVE)
                                  .group_by(t.c.version)).all()
        seen = conn.execute(select(func.count()).select_from(t)).scalar_one()
        new = conn.execute(select(func.count()).where(t.c.first_seen >= now - timedelta(days=30))).scalar_one()
    versions = sorted(((v, n) for v, n in by_version), key=lambda x: _key(x[0]), reverse=True)
    return {"active": sum(n for _, n in versions), "versions": versions, "new_30": new, "seen": seen}


def _key(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)
