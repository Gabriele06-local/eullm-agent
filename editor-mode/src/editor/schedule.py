"""What runs when. Every hour the worker queues one tick job per tenant
(queue.py) that calls `Runner.tenant_tick`; every decision about local time
is taken here with zoneinfo, so 08:00 Europe/Rome stays 08:00 across
daylight saving changes whatever the timezone of the server."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from . import briefing, jobs, models as m, opportunity, proposals, repo, sources, tenant_data, trends
from .i18n import N_
from .ingest import collect_archive
from .progress import step as progress
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
        if not self.db.sqlite:  # the shared archive first, once for everyone
            done, failed = report.tenants.setdefault(repo.ARCHIVE, []), []
            try:
                self.archive_tick(now, done, failed)
            except Exception as e:
                log.exception("tick failed for the archive")
                failed.append(f"{type(e).__name__}: {e}")
            if failed:
                report.errors[repo.ARCHIVE] = "; ".join(failed)
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
        with self.db.tenant(tenant) as s:
            last_maintain, last_review = jobs.last_done(s, "maintain"), jobs.last_done(s, "review")
        # On SQLite the newsroom is its own archive: it reads its sources itself.
        own_archive = repo.archive_of(self.db, tenant) == tenant
        with self.http_factory() as client:
            if own_archive:
                self.collect(tenant, client, now, done, failed, tz=tz)
            else:
                with self.db.tenant(tenant) as s:
                    repo.rescue_sources(s)
            if maintenance_due(local, last_maintain):
                def maintain():
                    sources.maintain(self.db, tenant, client, now=now, archive=own_archive)
                    tenant_data.prune(self.db, tenant, now=now)
                self.step(tenant, "maintain", done, failed, maintain)
            self.retry_profiles(tenant, now)
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

    def collect(self, archive: str, client, now: datetime, done: list[str], failed: list[str],
                only: set[int] | None = None, tz: str | None = None, take_trend: bool = True) -> dict:
        """Read the archive sources, then find and score its topics; from
        07:00 every three hours keep the trend of the moment. Returns what
        was read, for the dashboard."""
        out: dict = {}

        def run():
            rep = collect_archive(self.db, archive, client, only)
            out.update(sources=len(rep.sources), new=rep.new, errors=sum(1 for r in rep.sources if r.error))
            progress(N_("Raggruppo le notizie in temi"))
            topics = detect_topics(self.db, archive, self.core_for(archive))
            out["new_topics"] = topics.new_topics
            progress(N_("Calcolo dell'Hype dei temi"))
            score_topics(self.db, archive, now=now)
        self.step(archive, "collect", done, failed, run)
        if not take_trend:
            return out
        tz = tz or self.settings.timezone
        with self.db.tenant(archive) as s:
            take = trends.due(s, now, tz)
        if take:
            def keep():
                with self.db.tenant(archive) as s:
                    trends.snapshot(s, archive, now)
            self.step(archive, "trend", done, failed, keep)
        return out

    def collect_now(self, tenant: str, now: datetime, done: list[str], failed: list[str]) -> dict:
        """A newsroom's collection on request: its sources are read now, then
        topics, Hype, a fresh trend of the moment and each site's Opportunity."""
        archive = repo.archive_of(self.db, tenant)
        with self.db.tenant(tenant) as s:
            repo.rescue_sources(s)
            followed = repo.followed_archive(s)
            sites = s.execute(select(m.sites.c.id, m.sites.c.domain)).all()
        with self.http_factory() as client:
            out = self.collect(archive, client, now, done, failed, followed, take_trend=False)

        def keep():
            with self.db.tenant(archive) as s:
                trends.snapshot(s, archive, now)
        progress(N_("Trend del momento"))
        self.step(archive, "trend", done, failed, keep)
        for site_id, domain in sites:
            progress(N_("Opportunità dei temi per {domain}"), domain=domain)
            self.step(tenant, "opportunity", done, failed,
                      lambda site_id=site_id: opportunity.score_site(self.db, tenant, site_id, now=now))
        return out

    def archive_tick(self, now: datetime, done: list[str], failed: list[str]) -> None:
        """The hour of the shared archive (PostgreSQL): read once every source
        a newsroom follows, find topics and Hype Scores; once a day suspend
        the sources that fail or went silent and retry the suspended ones."""
        archive = repo.ARCHIVE
        followed: set[int] = set()
        for tenant in self.tenants():
            with self.db.tenant(tenant) as s:
                followed |= repo.followed_archive(s)
        with self.db.tenant(archive) as s:
            last_maintain = jobs.last_done(s, "maintain")
        local = now.astimezone(ZoneInfo(self.settings.timezone))
        with self.http_factory() as client:
            self.collect(archive, client, now, done, failed, followed)
            if maintenance_due(local, last_maintain):
                def maintain():
                    sources.maintain_archive(self.db, archive, client, now)
                    tenant_data.prune(self.db, archive, now=now)
                self.step(archive, "maintain", done, failed, maintain)

    def retry_profiles(self, tenant: str, now: datetime) -> list[str]:
        """Queue again the analyses the model left incomplete, without
        anyone pressing "Rianalizza"; they show in Activity like any other."""
        from .profile import retry_due
        from .queue import PRIORITY_TICK, enqueue

        if self.core_for(tenant) is None:
            return []
        with self.db.tenant(tenant) as s:
            sites = s.execute(select(m.sites.c.id, m.sites.c.domain)).all()
            due = [x.domain for x in sites if retry_due(s, x.id, now)]
        for domain in due:
            enqueue(self.db, tenant, "analysis", domain, {"retry": True}, priority=PRIORITY_TICK + 1)
        return due

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
