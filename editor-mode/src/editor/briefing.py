"""Daily briefing: per site, the proposals of the day with their sources,
the topics that are rising, and anything waiting for a decision (profiles to
approve, proposed changes of editorial line, sources suspended)."""

from __future__ import annotations

import html
import smtplib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from . import models as m

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

LABELS = {
    "it": {
        "title": "Briefing editoriale", "proposals": "Proposte di oggi", "none": "Nessuna proposta oggi.",
        "why": "Perché ora", "angle": "Taglio", "sources": "Fonti", "trends": "Temi in crescita",
        "pending": "In attesa di una decisione", "approve_profile": "Profilo editoriale da approvare",
        "line_change": "Proposta di aggiornamento della linea editoriale", "suspended": "Fonti sospese",
        "gaps": "Analisi incompleta", "opportunity": "Opportunità", "hype": "Hype",
        "emerging": "tema emergente", "growing": "in crescita", "declining": "in calo", "sector": "settore",
        "language": "lingua",
    },
    "en": {
        "title": "Editorial briefing", "proposals": "Today's proposals", "none": "No proposals today.",
        "why": "Why now", "angle": "Angle", "sources": "Sources", "trends": "Rising topics",
        "pending": "Waiting for a decision", "approve_profile": "Editorial profile to approve",
        "line_change": "Proposed update of the editorial line", "suspended": "Suspended sources",
        "gaps": "Incomplete analysis", "opportunity": "Opportunity", "hype": "Hype",
        "emerging": "emerging topic", "growing": "growing", "declining": "declining", "sector": "sector",
        "language": "language",
    },
}


@dataclass
class SiteSection:
    domain: str
    name: str
    proposals: list[dict] = field(default_factory=list)
    trends: list[dict] = field(default_factory=list)
    profile_pending: dict | None = None
    line_change: dict | None = None
    suspended: list[dict] = field(default_factory=list)


@dataclass
class Briefing:
    tenant_id: str
    day: date
    language: str
    sites: list[SiteSection]

    @property
    def proposal_ids(self) -> list[int]:
        return [p["id"] for s in self.sites for p in s.proposals]


def collect(s, tenant_id: str, day: date, since: datetime, language: str = "it") -> Briefing:
    sites = []
    for site in s.execute(select(m.sites).order_by(m.sites.c.domain)).all():
        sec = SiteSection(site.domain, site.name)
        p = m.editorial_profiles
        profiles = s.execute(select(p).where(p.c.site_id == site.id).order_by(p.c.version.desc())).all()
        approved = next((x for x in profiles if x.status == "approved"), None)
        newest = profiles[0] if profiles else None
        if approved is None and newest is not None:
            sec.profile_pending = {"version": newest.version, "status": newest.body.get("analysis", {}).get("status"),
                                   "gaps": newest.body.get("gaps", [])[:5],
                                   "subtopics": [x["name"] for x in newest.body.get("subtopics", [])][:6]}
        if approved is not None and newest is not None and newest.status == "draft" and newest.origin == "reanalysis":
            sec.line_change = {"version": newest.version, "changes": newest.changes}
        for prop in s.execute(select(m.proposals).where(
                m.proposals.c.site_id == site.id, m.proposals.c.created_at >= since,
                m.proposals.c.status == "proposed").order_by(m.proposals.c.opportunity.desc())).all():
            cites = s.execute(
                select(m.source_items.c.title, m.source_items.c.url, m.sources.c.name)
                .join(m.proposal_citations, m.proposal_citations.c.item_id == m.source_items.c.id)
                .join(m.sources, m.sources.c.id == m.source_items.c.source_id)
                .where(m.proposal_citations.c.proposal_id == prop.id)).all()
            sec.proposals.append({"id": prop.id, "title": prop.title, "angle": prop.angle, "why_now": prop.why_now,
                                  "format": prop.format, "opportunity": round(prop.opportunity),
                                  "sources": [{"title": c.title, "url": c.url, "source": c.name} for c in cites]})
        rows = s.execute(
            select(m.topics.c.label, m.opportunity_scores.c.score, m.opportunity_scores.c.components)
            .join(m.opportunity_scores, m.opportunity_scores.c.topic_id == m.topics.c.id)
            .where(m.opportunity_scores.c.site_id == site.id, m.opportunity_scores.c.computed_at >= since)
            .order_by(m.opportunity_scores.c.score.desc()).limit(15)).all()
        seen = set()
        for r in rows:
            if r.label in seen or r.label in {x["title"] for x in sec.proposals}:
                continue
            seen.add(r.label)
            sec.trends.append({"label": r.label, "opportunity": round(r.score),
                               "hype": round(100 * r.components.get("hype", 0)) if "hype" in r.components else None})
            if len(sec.trends) >= 5:
                break
        sec.suspended = [{"name": x.name, "reason": x.status_reason} for x in s.execute(
            select(m.sources).where(m.sources.c.site_id == site.id, m.sources.c.status == "suspended",
                                    m.sources.c.status_changed_at >= since)).all()]
        sites.append(sec)
    return Briefing(tenant_id, day, language, sites)


def render(b: Briefing) -> tuple[str, str]:
    ctx = {"b": b, "t": LABELS.get(b.language, LABELS["en"])}
    return TEMPLATES.get_template("briefing.md.j2").render(**ctx), TEMPLATES.get_template("briefing.html.j2").render(**ctx)


def telegram_text(b: Briefing) -> str:
    """Telegram HTML subset: <b>, <i>, <a>."""
    t = LABELS.get(b.language, LABELS["en"])
    e = html.escape
    lines = [f"<b>{e(t['title'])} · {b.day.isoformat()}</b>"]
    for s in b.sites:
        lines.append(f"\n<b>{e(s.name)}</b> ({e(s.domain)})")
        if s.profile_pending:
            lines.append(f"⚠ {e(t['approve_profile'])} (v{s.profile_pending['version']})")
        if s.line_change:
            lines.append(f"↗ {e(t['line_change'])} (v{s.line_change['version']})")
        for p in s.proposals:
            lines.append(f"• <b>{e(p['title'])}</b> [{p['opportunity']}]")
            for c in p["sources"][:3]:
                lines.append(f'  – <a href="{e(c["url"], quote=True)}">{e(c["source"])}</a>')
        if not s.proposals and not s.profile_pending:
            lines.append(e(t["none"]))
    return "\n".join(lines)


def chunks(text: str, size: int = 4000) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur += line[:size] + "\n"
    if cur.strip():
        out.append(cur)
    return out


def build(db, tenant_id: str, day: date, language: str = "it", since: datetime | None = None) -> int:
    """Create (or return) the stored briefing of a tenant for a local day."""
    with db.tenant(tenant_id) as s:
        existing = s.execute(select(m.briefings.c.id).where(m.briefings.c.briefing_date == day)).scalar()
        if existing:
            return existing
        if since is None:
            last = s.execute(select(m.briefings.c.created_at).order_by(m.briefings.c.briefing_date.desc()).limit(1)).scalar()
            since = last or datetime.now(UTC) - timedelta(days=1)
        b = collect(s, tenant_id, day, since, language)
        md, page = render(b)
        return s.execute(insert(m.briefings).values(
            tenant_id=tenant_id, briefing_date=day, body_md=md, body_html=page, proposal_ids=b.proposal_ids,
        ).returning(m.briefings.c.id)).scalar_one()


class Sender:
    """Delivery of a stored briefing; each channel is sent at most once."""

    def __init__(self, settings, http: httpx.Client | None = None, smtp_factory=smtplib.SMTP):
        self.settings = settings
        self.http = http or httpx.Client(timeout=30)
        self.smtp_factory = smtp_factory

    def send(self, db, tenant_id: str, briefing_id: int) -> dict:
        with db.tenant(tenant_id) as s:
            b = s.execute(select(m.briefings).where(m.briefings.c.id == briefing_id)).first()
            recips = s.execute(select(m.recipients).where(m.recipients.c.enabled)).all()
        emails = [r.address for r in recips if r.channel == "email"]
        chats = [r.address for r in recips if r.channel == "telegram"]
        result, errors, values = {"email": 0, "telegram": 0}, [], {}
        if emails and b.sent_email_at is None and self.settings.smtp_host:
            try:
                self._email(emails, f"Briefing {b.briefing_date.isoformat()}", b.body_md, b.body_html)
                values["sent_email_at"] = datetime.now(UTC)
                result["email"] = len(emails)
            except (OSError, smtplib.SMTPException, ValueError) as e:
                errors.append(f"email: {e}")
        if chats and b.sent_telegram_at is None and self.settings.telegram_token:
            try:
                text = self._telegram_text(db, tenant_id, b)
                for chat in chats:
                    for part in chunks(text):
                        r = self.http.post(
                            f"https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage",
                            json={"chat_id": chat, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True},
                        )
                        r.raise_for_status()
                        body = r.json()
                        if not body.get("ok", False):
                            raise ValueError(f"telegram refused the message: {body.get('description', body)}"[:200])
                values["sent_telegram_at"] = datetime.now(UTC)
                result["telegram"] = len(chats)
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"telegram: {type(e).__name__}")
        if emails and b.sent_email_at is None and not self.settings.smtp_host:
            errors.append("email: smtp is not configured")
        if chats and b.sent_telegram_at is None and not self.settings.telegram_token:
            errors.append("telegram: token is not configured")
        values["send_error"] = "; ".join(errors) or None
        with db.tenant(tenant_id) as s:
            s.execute(update(m.briefings).where(m.briefings.c.id == briefing_id).values(**values))
        result["errors"] = errors
        return result

    def _email(self, to: list[str], subject: str, text: str, page: str) -> None:
        msg = EmailMessage()
        # Recipients in Bcc: they do not see each other's addresses.
        msg["Subject"], msg["From"], msg["To"] = subject, self.settings.mail_from, self.settings.mail_from
        msg["Bcc"] = ", ".join(to)
        msg.set_content(text)
        msg.add_alternative(page, subtype="html")
        with self.smtp_factory(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if self.settings.smtp_user:
                smtp.login(self.settings.smtp_user, self.settings.smtp_password)
            smtp.send_message(msg)

    def _telegram_text(self, db, tenant_id: str, b) -> str:
        with db.tenant(tenant_id) as s:
            return telegram_text(collect(s, tenant_id, b.briefing_date, b.created_at - timedelta(days=1)))
