"""A tenant's data as a whole: export it, delete it, and keep what is
collected from growing for ever.

- Export: every row of the tenant, table by table, as JSON. Credentials are
  left out (secret names only, token names and roles only): an export can be
  handed to the customer as it is.
- Delete: every row of the tenant, the tenant itself last, in one
  transaction, with the owner connection. What the Core holds for the
  tenant (usage and audit of `editor/<tenant>`) stays there.
- Retention: collected items, their observations and finished jobs older
  than EDITOR_RETENTION_DAYS (default 30) are deleted at the daily
  maintenance. Items a proposal or a draft cites are kept, so every
  sentence keeps its source; model usage is kept for accounting.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select, text, union

from . import models as m

# Children before parents, the tenant last.
DELETE_ORDER = [
    "publications", "publish_targets", "feeds",
    "draft_images", "draft_claim_sources", "draft_claims", "drafts",
    "proposal_citations", "proposals", "opportunity_scores",
    "reader_messages", "feedback", "profile_reviews", "editorial_profiles", "site_analyses", "site_posts",
    "briefings", "weekly_reports", "recipients", "search_stats",
    "trend_snapshots", "trend_scores", "topic_items", "topics",
    "observations", "source_items", "sources", "sites",
    "llm_usage", "credit_ledger", "jobs", "access_tokens", "secrets",
    "tenants",
]

# Who belongs to the tenant, their sessions and invites, its subscription:
# owner connection only (accounts.py, billing.py), never exported, deleted
# with the tenant row. Its payments stay, without the tenant, for the books.
ACCOUNT_TABLES = ("memberships", "sessions", "invites", "subscriptions", "payments")

# Never exported: what would let someone act as the tenant.
WITHHELD = {"access_tokens": {"token_sha256"}, "reader_messages": {"token_sha256", "address_hash"}, "feeds": {"token"}, "secrets": {"ciphertext", "key_id"}}

DEFAULT_RETENTION_DAYS = 30


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):  # the cover images
        return base64.b64encode(bytes(value)).decode()
    return value


def export(db, tenant_id: str, images: bool = True) -> dict:
    """Every row of the tenant, read in the tenant's own scope; without the
    image bytes when `images` is False (the ZIP has them as files)."""
    out = {"tenant": tenant_id, "exported_at": datetime.now(UTC).isoformat(), "tables": {}}
    with db.tenant(tenant_id) as s:
        for name in reversed(DELETE_ORDER):
            table = m.metadata.tables[f"editor.{name}"]
            cols = [c for c in table.c if c.name not in WITHHELD.get(name, ())
                    and (images or (name, c.name) != ("draft_images", "content"))]
            order = list(table.primary_key.columns)
            rows = s.execute(select(*cols).where(table.c.tenant_id == tenant_id).order_by(*order)).all()
            out["tables"][name] = [{k: _plain(v) for k, v in r._mapping.items()} for r in rows]
    return out


def export_json(db, tenant_id: str) -> str:
    return json.dumps(export(db, tenant_id), ensure_ascii=False, indent=1, default=str)


README = """Redazia: export of the newsroom {name}, {day}

articles/<site>/<language>/   the approved and published articles: .md
                              (front matter, as static site generators
                              read it), .html (a page of its own) and the
                              cover image
drafts/<site>/<language>/     the other drafts, in the same formats
sites/<site>/profile.json     the editorial profile approved, with the
                              settings of the automatic newsroom
sources.csv                   the sources followed, site by site
destinations.csv              where the articles are published (no
                              passwords or tokens: they stay on the server)
publications.csv              every publication, with its address
data.json                     every row of the newsroom, table by table,
                              for a technician or another system
"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80] or "article"


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def export_zip(db, tenant_id: str) -> bytes:
    """The newsroom in a ZIP anyone can open: articles as Markdown and HTML
    with their covers, profiles, sources, destinations, publications, and
    the whole data for a technician. Credentials are left out, as in
    export()."""
    from . import delivery

    day = datetime.now(UTC).date().isoformat()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, db.tenant(tenant_id) as s:
        name = s.execute(select(m.tenants.c.name).where(m.tenants.c.tenant_id == tenant_id)).scalar() or tenant_id
        z.writestr("README.txt", README.format(name=name, day=day))
        domains = dict(s.execute(select(m.sites.c.id, m.sites.c.domain)).all())
        used = set()
        for d in s.execute(select(m.drafts.c.id, m.drafts.c.status, m.drafts.c.site_id).order_by(m.drafts.c.id)).all():
            a = delivery.article(s, d.id)
            if a is None:
                continue
            folder = "articles" if d.status in ("approved", "published") else "drafts"
            date = a["approved_at"].date().isoformat() if a["approved_at"] else ""
            base = f"{folder}/{domains.get(d.site_id, 'site')}/{a['language'] or 'xx'}/" \
                   f"{(date + '-') if date else ''}{_slug(a['slug'] or a['title'])}"
            if base in used:
                base = f"{base}-{d.id}"
            used.add(base)
            image = None
            if a["image"]:
                image = base.rsplit("/", 1)[1] + "." + a["image"]["file"].rsplit(".", 1)[1]
                z.writestr(f"{base}.{image.rsplit('.', 1)[1]}", a["image"]["content"], compress_type=zipfile.ZIP_STORED)
                a["image"] = {**a["image"], "file": image}
            z.writestr(f"{base}.md", delivery.markdown_file(a))
            z.writestr(f"{base}.html", delivery.page(a))
        for site_id, domain in domains.items():
            body = s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")
                .order_by(m.editorial_profiles.c.version.desc()).limit(1)).scalar()
            if body is not None:
                z.writestr(f"sites/{domain}/profile.json", json.dumps(body, ensure_ascii=False, indent=1, default=str))
        src = m.sources.c
        rows = [["site", "name", "url", "kind", "status", "origin"]]
        for r in s.execute(select(src.site_id, src.name, src.url, src.kind, src.status, src.origin)
                           .where(src.tenant_id == tenant_id).order_by(src.site_id, src.name)).all():
            rows.append([domains.get(r.site_id, ""), r.name, r.url, r.kind, r.status, r.origin or ""])
        z.writestr("sources.csv", _csv(rows))
        pt = m.publish_targets.c
        targets = {}
        rows = [["site", "name", "kind", "enabled", "settings"]]
        for r in s.execute(select(pt.id, pt.site_id, pt.name, pt.kind, pt.enabled, pt.config).order_by(pt.id)).all():
            targets[r.id] = r.name
            rows.append([domains.get(r.site_id, ""), r.name, r.kind, r.enabled is not False,
                         json.dumps(r.config or {}, ensure_ascii=False)])
        z.writestr("destinations.csv", _csv(rows))
        pb, dr = m.publications.c, m.drafts.c
        rows = [["article", "site", "language", "destination", "status", "requested_at", "published_at", "address"]]
        for r in s.execute(select(dr.title, dr.site_id, dr.language, pb.target_id, pb.status, pb.requested_at,
                                  pb.published_at, pb.external_url)
                           .join(m.drafts, dr.id == pb.draft_id).order_by(pb.id)).all():
            rows.append([r.title, domains.get(r.site_id, ""), r.language or "", targets.get(r.target_id, ""),
                         r.status or "", _plain(r.requested_at) or "", _plain(r.published_at) or "",
                         r.external_url or ""])
        z.writestr("publications.csv", _csv(rows))
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(export(db, tenant_id, images=False), ensure_ascii=False, indent=1,
                                           default=str))
    return buf.getvalue()


def delete_tenant(admin_db, tenant_id: str) -> dict[str, int]:
    """Delete every row of the tenant. `admin_db` is the owner connection:
    the application role cannot delete tenants."""
    counts = {}
    with admin_db.engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            # Row level security is forced on the owner too (a server's owner
            # is not a superuser): without the tenant in scope every DELETE
            # would match nothing.
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        if not conn.execute(text("SELECT 1 FROM editor.tenants WHERE tenant_id = :t"), {"t": tenant_id}).first():
            raise LookupError(f"no tenant {tenant_id}")
        # An administrator who entered this newsroom from the console is
        # still signed in after deleting it: back to the console, not to the login.
        conn.execute(text("UPDATE editor.sessions SET tenant_id = NULL WHERE tenant_id = :t AND user_id IN "
                          "(SELECT id FROM editor.users WHERE operator)"), {"t": tenant_id})
        for name in DELETE_ORDER:
            counts[name] = conn.execute(text(f"DELETE FROM editor.{name} WHERE tenant_id = :t"), {"t": tenant_id}).rowcount
    return counts


def retention_days() -> int:
    import os

    raw = os.environ.get("EDITOR_RETENTION_DAYS", "").strip()
    return int(raw) if raw else DEFAULT_RETENTION_DAYS


def prune(db, tenant_id: str, now: datetime | None = None, days: int | None = None) -> dict[str, int]:
    """Delete what the tenant collected more than `days` ago and is not cited."""
    days = retention_days() if days is None else days
    if days <= 0:
        return {}
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    i = m.source_items
    cited = union(select(m.proposal_citations.c.item_id), select(m.draft_claim_sources.c.item_id),
                  select(i.c.duplicate_of).where(i.c.duplicate_of.is_not(None)))
    with db.tenant(tenant_id) as s:
        old = select(i.c.id).where(i.c.fetched_at < cutoff, i.c.id.not_in(cited))
        observations = s.execute(delete(m.observations).where(
            (m.observations.c.observed_at < cutoff) | m.observations.c.item_id.in_(old))).rowcount
        s.execute(delete(m.topic_items).where(m.topic_items.c.item_id.in_(old)))
        items = s.execute(delete(i).where(i.c.id.in_(old))).rowcount
        jobs = s.execute(delete(m.jobs).where(m.jobs.c.status.in_(("done", "failed")),
                                              m.jobs.c.finished_at < cutoff)).rowcount
        # Hype scores: only the latest of each topic is read (the hourly
        # scoring removes the older ones; this clears what earlier versions kept).
        ts, newer = m.trend_scores, m.trend_scores.alias("newer")
        scores = s.execute(delete(ts).where(select(newer.c.id).where(
            newer.c.topic_id == ts.c.topic_id, newer.c.id > ts.c.id).exists())).rowcount
    return {"items": items, "observations": observations, "jobs": jobs, "scores": scores}
