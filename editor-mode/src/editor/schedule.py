"""What runs when. The worker calls `Runner.tick` once an hour (procrastinate
periodic task, see tasks.py); every decision about local time is taken here
with zoneinfo, so 08:00 Europe/Rome stays 08:00 across daylight saving
changes whatever the timezone of the server."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from . import briefing, jobs, models as m, opportunity, proposals, sources
from .ingest import collect_tenant
from .profile import analyse_site
from .scoring import score_topics
from .topics import detect_topics

log = logging.getLogger(__name__)

BRIEFING_WINDOW_HOURS = 3  # a worker that was down at 08:00 still sends until 11:00
MAINTENANCE_HOUR = 3
REVIEW_WEEKDAY, REVIEW_HOUR = 0, 4  # Monday 04:00: re-read the sites, look for new sources


def maintenance_due(local: datetime, last: datetime | None) -> bool:
    """Once a local day, from 03:00; a tick that comes late still runs it."""
    if local.hour < MAINTENANCE_HOUR:
        return False
    return last is None or last.astimezone(local.tzinfo).date() < local.date()


def review_due(local: datetime, last: datetime | None) -> bool:
    """Once a week, from Monday 04:00; if Monday was missed, as soon as possible."""
    if last is None:
        return local.weekday() == REVIEW_WEEKDAY and local.hour >= REVIEW_HOUR
    since = local - last.astimezone(local.tzinfo)
    if since >= timedelta(days=8):
        return True
    return since >= timedelta(days=6) and local.weekday() == REVIEW_WEEKDAY and local.hour >= REVIEW_HOUR


def briefing_due(now_utc: datetime, tz: str, hour: int, already_sent: set[date]) -> date | None:
    """The local day whose briefing is due now, or None."""
    local = now_utc.astimezone(ZoneInfo(tz))
    if hour <= local.hour < hour + BRIEFING_WINDOW_HOURS and local.date() not in already_sent:
        return local.date()
    return None


@dataclass
class TickReport:
    tenants: dict[str, list[str]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class Runner:
    def __init__(self, db, settings, http_factory, core=None, sender=None):
        self.db = db
        self.settings = settings
        self.http_factory = http_factory  # () -> httpx.Client (through the Core in production)
        self.core = core
        self.sender = sender

    def tenants(self) -> list[str]:
        # On PostgreSQL only this function reads across tenants.
        q = ("SELECT tenant_id FROM editor.tenants WHERE active ORDER BY tenant_id" if self.db.sqlite
             else "SELECT * FROM editor.active_tenants()")
        with self.db.engine.connect() as conn:
            return list(conn.execute(text(q)).scalars())

    def tick(self, now: datetime | None = None) -> TickReport:
        now = now or datetime.now(UTC)
        report = TickReport()
        for tenant in self.tenants():
            done = report.tenants.setdefault(tenant, [])
            failed: list[str] = []
            try:
                self.tenant_tick(tenant, now, done, failed)
            except Exception as e:  # one tenant's failure must not stop the others
                log.exception("tick failed for tenant %s", tenant)
                failed.append(f"{type(e).__name__}: {e}")
            if failed:
                report.errors[tenant] = "; ".join(failed)
        return report

    def tenant_settings(self, tenant: str) -> dict:
        with self.db.tenant(tenant) as s:
            row = s.execute(select(m.tenants.c.timezone, m.tenants.c.briefing_hour, m.tenants.c.language)).first()
        st = self.settings
        tz = (row.timezone if row else None) or st.timezone
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            log.warning("tenant %s has invalid timezone %r, using default %s", tenant, tz, st.timezone)
            tz = st.timezone
        return {
            "timezone": tz,
            "briefing_hour": row.briefing_hour if row and row.briefing_hour is not None else st.briefing_hour,
            "language": (row.language if row else None) or "it",
        }

    def core_for(self, tenant: str):
        return self.core.for_tenant(self.db, tenant) if self.core is not None else None

    def step(self, tenant: str, kind: str, done: list[str], failed: list[str], fn, label: str | None = None) -> bool:
        """Run one step in its own job: a failure is recorded and does not
        stop the other steps."""
        ok = False
        with jobs.track(self.db, tenant, kind):
            fn()
            ok = True
        if ok:
            done.append(label or kind)
        else:
            failed.append(f"{kind} failed")
        return ok

    def tenant_tick(self, tenant: str, now: datetime, done: list[str], failed: list[str] | None = None) -> None:
        failed = [] if failed is None else failed
        conf = self.tenant_settings(tenant)
        tz = conf["timezone"]
        local = now.astimezone(ZoneInfo(tz))
        core = self.core_for(tenant)
        with self.db.tenant(tenant) as s:
            last_maintain, last_review = jobs.last_done(s, "maintain"), jobs.last_done(s, "review")
        with self.http_factory() as client:
            def collect():
                collect_tenant(self.db, tenant, client)
                detect_topics(self.db, tenant, core)
                score_topics(self.db, tenant, now=now)
            self.step(tenant, "collect", done, failed, collect)
            if maintenance_due(local, last_maintain):
                self.step(tenant, "maintain", done, failed, lambda: sources.maintain(self.db, tenant, client, now=now))
            if review_due(local, last_review):
                self.step(tenant, "review", done, failed, lambda: self.review(tenant, client))
            with self.db.tenant(tenant) as s:
                built = {r.briefing_date: r for r in s.execute(
                    select(m.briefings.c.id, m.briefings.c.briefing_date, m.briefings.c.send_error).where(
                        m.briefings.c.briefing_date >= local.date() - timedelta(days=1))).all()}
            day = briefing_due(now, tz, conf["briefing_hour"], set(built))
            if day is not None:
                self.step(tenant, "briefing", done, failed, lambda: self.morning(tenant, day, now, conf["language"]),
                          f"briefing {day.isoformat()}")
            else:
                # Built but not delivered (mail server down, Telegram error):
                # try again within the same window; channels already sent are skipped.
                today = built.get(local.date())
                in_window = briefing_due(now, tz, conf["briefing_hour"], set()) is not None
                if today is not None and today.send_error and in_window and self.sender is not None:
                    self.step(tenant, "briefing", done, failed, lambda: self.sender.send(self.db, tenant, today.id),
                              f"briefing {today.briefing_date.isoformat()} resent")

    def review(self, tenant: str, client) -> None:
        from .site import SiteCrawler

        with self.db.tenant(tenant) as s:
            sites = s.execute(select(m.sites.c.id, m.sites.c.domain)).all()
        core = self.core_for(tenant)
        for site in sites:
            analyse_site(self.db, tenant, site.domain, SiteCrawler(client), core)
            sources.discover(self.db, tenant, site.id, client, core)

    def morning(self, tenant: str, day: date, now: datetime, language: str = "it") -> int:
        core = self.core_for(tenant)
        with self.db.tenant(tenant) as s:
            site_ids = list(s.execute(select(m.sites.c.id)).scalars())
        for site_id in site_ids:
            ops = opportunity.score_site(self.db, tenant, site_id, now=now)
            proposals.propose(self.db, tenant, site_id, ops, core)
        bid = briefing.build(self.db, tenant, day, language)
        if self.sender is not None:
            self.sender.send(self.db, tenant, bid)
        return bid
