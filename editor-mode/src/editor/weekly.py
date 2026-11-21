"""The weekly report: every Monday at the hour of the briefing, the
newsroom's week in one email, in its own language.

Per site: what was published (with the link to each article), the numbers
of the week (news read, proposals, drafts), Google's clicks and searches
when Search Console is connected (searchconsole.py), what is waiting for
a decision and what comes next week. At the bottom the articles of the
month against the plan.

It goes to the newsroom's email recipients (Impostazioni › Chi riceve il
briefing) or, with none, to its directors. It is kept in
`weekly_reports`, one per newsroom and week, and listed in Impostazioni;
the owner can switch it off there. A worker that was down on Monday
still sends it until Wednesday; a mail server that refused it is tried
again at every hourly round in those days.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import i18n
from . import models as m
from .mailstamp import stamp
from . import repo, rubrics, searchconsole
from .autopilot import settings_of
from .i18n import t

log = logging.getLogger(__name__)

WEEKDAY = 0  # Monday
LAST_DAY = 2  # until Wednesday, for a worker that was down

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
TEMPLATES.globals["_"] = i18n.gettext


@dataclass
class SiteWeek:
    id: int
    domain: str
    name: str
    published: list[dict] = field(default_factory=list)
    numbers: dict = field(default_factory=dict)
    waiting: dict = field(default_factory=dict)
    coming: list[str] = field(default_factory=list)
    auto_per_day: int | None = None
    proposals_per_day: int = 0
    search: dict | None = None
    search_problem: str | None = None
    rubrics: list[dict] = field(default_factory=list)


@dataclass
class Week:
    tenant_id: str
    name: str
    language: str
    start: date  # Monday
    end: date  # the Sunday after
    sites: list[SiteWeek]
    drafts_month: int = 0
    max_drafts_month: int | None = None
    link: str = ""

    @property
    def published(self) -> int:
        return sum(len(s.published) for s in self.sites)


def week_of(local: datetime) -> date:
    """The Monday of the week before the local day: the week the report tells."""
    return local.date() - timedelta(days=local.weekday() + 7)


def due(local: datetime, hour: int) -> bool:
    """From Monday at the hour of the briefing to Wednesday night."""
    if local.weekday() == WEEKDAY:
        return local.hour >= hour
    return WEEKDAY < local.weekday() <= LAST_DAY


def collect(s, tenant_id: str, start: date, end: date, tz: ZoneInfo, language: str, link: str = "") -> Week:
    """The newsroom's week [start, end] (local days, end included)."""
    t0 = datetime.combine(start, time(0), tz).astimezone(UTC)
    t1 = datetime.combine(end + timedelta(days=1), time(0), tz).astimezone(UTC)
    ten = s.execute(select(m.tenants)).first()
    p, pr, dr, pt, pub = m.editorial_profiles.c, m.proposals.c, m.drafts.c, m.publish_targets.c, m.publications.c
    sites = []
    for site in s.execute(select(m.sites).order_by(m.sites.c.domain)).all():
        w = SiteWeek(site.id, site.domain, site.name)

        def count(table, *where):
            return s.execute(select(func.count()).select_from(table).where(*where)).scalar() or 0

        seen = set()
        for r in s.execute(
                select(dr.title, dr.language, pub.external_url, pub.published_at, pt.name.label("target"), dr.id)
                .join(m.drafts, m.drafts.c.id == pub.draft_id)
                .join(m.publish_targets, pt.id == pub.target_id)
                .where(pt.site_id == site.id, pub.status == "published", pub.withdrawn_at.is_(None),
                       pub.published_at >= t0, pub.published_at < t1)
                .order_by(pub.published_at)).all():
            if r.id in seen:
                continue
            seen.add(r.id)
            w.published.append({"title": r.title, "url": r.external_url, "target": r.target,
                                "language": r.language, "day": r.published_at.astimezone(tz).date()})
        w.numbers = {
            "news": count(m.source_items, repo.followed_by(site.id), m.source_items.c.fetched_at >= t0,
                          m.source_items.c.fetched_at < t1),
            "proposals": count(m.proposals, pr.site_id == site.id, pr.created_at >= t0, pr.created_at < t1),
            "drafts": count(m.drafts, dr.site_id == site.id, dr.translation_of.is_(None), dr.created_at >= t0,
                            dr.created_at < t1),
            "approved": count(m.drafts, dr.site_id == site.id, dr.status == "approved", dr.decided_at >= t0,
                              dr.decided_at < t1),
        }
        w.waiting = {
            "proposals": count(m.proposals, pr.site_id == site.id, pr.status == "proposed"),
            "drafts": count(m.drafts, dr.site_id == site.id, dr.status.in_(("draft", "needs_review"))),
            "publications": s.execute(select(func.count()).select_from(
                m.publications.join(m.publish_targets, pt.id == pub.target_id))
                .where(pt.site_id == site.id, pub.status == "pending_approval")).scalar() or 0,
            "profile": count(m.editorial_profiles, p.site_id == site.id, p.status == "draft"),
        }
        w.coming = list(s.execute(select(pr.title).where(pr.site_id == site.id, pr.status == "accepted")
                                  .order_by(pr.created_at.desc()).limit(5)).scalars())
        body = s.execute(select(p.body).where(p.site_id == site.id, p.status == "approved")).scalar()
        if body:
            auto = settings_of(body)
            if auto.get("enabled") and ten is not None and ten.autopilot:
                w.auto_per_day = int(auto.get("per_day") or 0)
            w.proposals_per_day = int(body.get("settings", {}).get("proposals_per_day") or 0)
            # The rubrics the newsroom decided for the week to come, on Monday at dawn (rubrics.py).
            w.rubrics = rubrics.since(body, t1 - timedelta(days=1))
        if site.gsc_property:
            if site.gsc_status not in (None, "ok"):
                w.search_problem = searchconsole.problem(site.gsc_status)
            this = searchconsole.totals(s, site.id, start, end)
            if this["impressions"]:
                w.search = {"this": this,
                            "before": searchconsole.totals(s, site.id, start - timedelta(days=7), start - timedelta(days=1)),
                            "pages": searchconsole.top(s, site.id, "page", start, end, 3),
                            "queries": searchconsole.top(s, site.id, "query", start, end, 5)}
        sites.append(w)
    month = datetime.combine(end.replace(day=1), time(0), tz).astimezone(UTC)
    drafts_month = s.execute(select(func.count()).select_from(m.drafts).where(
        m.drafts.c.created_at >= month, m.drafts.c.translation_of.is_(None))).scalar() or 0
    return Week(tenant_id, ten.name if ten else tenant_id, language, start, end, sites, drafts_month,
                ten.max_drafts_month if ten else None, link)


def render(w: Week) -> tuple[str, str, str]:
    """Subject, text and HTML, in the newsroom's language."""
    token = i18n.use(w.language)
    try:
        months = i18n.MONTHS[i18n.current()]

        def day(d: date) -> str:
            return f"{d.day} {months[d.month - 1]}"
        subject = t("{name}: la settimana dal {start} al {end}", name=w.name, start=day(w.start), end=day(w.end))
        ctx = {"w": w, "day": day, "subject": subject}
        return (subject, TEMPLATES.get_template("weekly.txt.j2").render(**ctx),
                TEMPLATES.get_template("weekly.html.j2").render(**ctx))
    finally:
        i18n._current.reset(token)


def recipients(s, admin, tenant_id: str) -> list[str]:
    emails = list(s.execute(select(m.recipients.c.address).where(
        m.recipients.c.channel == "email", m.recipients.c.enabled.is_not(False))).scalars())
    if emails or admin is None:
        return emails
    from .billing import owners
    return owners(admin, tenant_id)


def send_mail(settings, to: list[str], subject: str, body: str, page: str, smtp_factory=smtplib.SMTP) -> None:
    msg = EmailMessage()
    sender = settings.mail_from or settings.smtp_user
    # Recipients in Bcc: they do not see each other's addresses.
    msg["Subject"], msg["From"], msg["To"] = subject, sender, sender
    msg["Bcc"] = ", ".join(to)
    msg.set_content(body)
    msg.add_alternative(page, subtype="html")
    with smtp_factory(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(stamp(msg, sender))


def _zone(name: str | None, default: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or default)
    except (KeyError, ValueError):
        return ZoneInfo(default)


def build(db, tenant_id: str, start: date, settings, store: bool = True) -> tuple[int | None, Week, tuple[str, str, str]]:
    """The report of the week starting on `start`; stored (once) unless `store` is false."""
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.tenants.c.timezone, m.tenants.c.language)).first()
        tz = _zone(row.timezone if row else None, settings.timezone)
        lang = (row.language if row else None) or "it"
        link = f"{settings.public_url}/" if getattr(settings, "public_url", "") else ""
        w = collect(s, tenant_id, start, start + timedelta(days=6), tz, lang, link)
        out = render(w)
        if not store:
            return None, w, out
        rid = s.execute(select(m.weekly_reports.c.id).where(m.weekly_reports.c.week_start == start)).scalar()
        if rid is None:
            values = {"tenant_id": tenant_id, "week_start": start, "body_md": out[1], "body_html": out[2]}
            if db.sqlite:
                rid = s.execute(m.weekly_reports.insert().values(**values).returning(m.weekly_reports.c.id)).scalar_one()
            else:
                rid = s.execute(pg_insert(m.weekly_reports).values(**values).on_conflict_do_nothing()
                                .returning(m.weekly_reports.c.id)).scalar()
                if rid is None:
                    rid = s.execute(select(m.weekly_reports.c.id).where(m.weekly_reports.c.week_start == start)).scalar()
    return rid, w, out


def deliver(db, admin, tenant_id: str, report_id: int, settings, smtp_factory=smtplib.SMTP) -> dict:
    """Send a stored report if it was not sent yet."""
    with db.tenant(tenant_id) as s:
        r = s.execute(select(m.weekly_reports).where(m.weekly_reports.c.id == report_id)).first()
        if r is None or r.sent_at is not None:
            return {"sent": 0}
        to = recipients(s, admin, tenant_id)
    values: dict = {}
    if not to:
        values["send_error"] = "no recipients"
    elif not settings.smtp_host:
        values["send_error"] = "smtp is not configured"
    else:
        subject = r.body_md.split("\n", 1)[0].strip()
        try:
            send_mail(settings, to, subject, r.body_md, r.body_html, smtp_factory)
            values = {"sent_at": datetime.now(UTC), "sent_to": to, "send_error": None}
        except (OSError, smtplib.SMTPException, ValueError) as e:
            values["send_error"] = f"email: {type(e).__name__}: {e}"[:300]
    with db.tenant(tenant_id) as s:
        s.execute(update(m.weekly_reports).where(m.weekly_reports.c.id == report_id).values(**values))
    return {"sent": len(to) if "sent_at" in values else 0, "error": values.get("send_error")}


def run(db, admin, settings, now: datetime | None = None, smtp_factory=smtplib.SMTP) -> dict:
    """Run every hour by the worker: build and send the reports that are due."""
    now = now or datetime.now(UTC)
    with admin.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT tenant_id, timezone, briefing_hour, weekly_report FROM editor.tenants "
            "WHERE active AND tenant_id <> :archive"), {"archive": repo.ARCHIVE}).all()
    done = {}
    for r in rows:
        if r.weekly_report is not None and not r.weekly_report:
            continue
        local = now.astimezone(_zone(r.timezone, settings.timezone))
        hour = r.briefing_hour if r.briefing_hour is not None else settings.briefing_hour
        if not due(local, hour):
            continue
        start = week_of(local)
        try:
            with db.tenant(r.tenant_id) as s:
                if not s.execute(select(m.sites.c.id).limit(1)).first():
                    continue  # nothing to tell yet
                old = s.execute(select(m.weekly_reports.c.id, m.weekly_reports.c.sent_at)
                                .where(m.weekly_reports.c.week_start == start)).first()
            if old is not None and old.sent_at is not None:
                continue
            rid = old.id if old is not None else build(db, r.tenant_id, start, settings)[0]
            done[r.tenant_id] = deliver(db, admin, r.tenant_id, rid, settings, smtp_factory)
        except Exception:  # one newsroom must not stop the others
            log.exception("weekly report of %s failed", r.tenant_id)
    return done
