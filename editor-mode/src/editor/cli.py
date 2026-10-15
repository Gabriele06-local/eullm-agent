"""Command line of Editor Mode."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime

from sqlalchemy import select, text


def _db():
    from .runtime import database
    return database()


def cmd_migrate(a):
    from . import migrate
    from .db import make_engine, sqlite_path
    from .runtime import settings
    url = settings().admin_database_url
    migrate.upgrade(url)
    print("editor schema up to date")
    if sqlite_path(url) is None:  # the job queue of `editor worker`
        engine = make_engine(url)
        try:
            with engine.connect() as conn:
                queue = conn.execute(text("SELECT to_regclass('procrastinate_jobs')")).scalar()
        finally:
            engine.dispose()
        if queue is None:
            from .tasks import app
            with app.open():
                app.schema_manager.apply_schema()
            print("job queue schema created")


def cmd_tenant_add(a):
    from . import repo
    from .publishing import TENANT_ID
    if not TENANT_ID.match(a.tenant):
        sys.exit("tenant ids are lowercase letters and digits, words joined by single dashes (e.g. i3k or rag-enterprise)")
    from sqlalchemy.exc import IntegrityError
    try:
        with _db().tenant(a.tenant) as s:
            repo.ensure_tenant(s, a.tenant, a.name or a.tenant)
    except IntegrityError as e:
        if "one tenant" in str(e):
            sys.exit("this installation (SQLite) has one tenant already: several tenants need PostgreSQL")
        raise
    print(f"tenant {a.tenant} ready")


def cmd_tenant_plan(a):
    """Plans and limits are set with the owner connection, not by tenants."""
    from .db import Database
    from .runtime import settings
    fields = {k: v for k, v in {
        "plan": a.plan, "max_sites": a.max_sites, "max_active_sources": a.max_sources,
        "max_items_per_day": a.max_items_day, "max_llm_cost_month": a.max_cost_month,
        "max_drafts_month": a.max_drafts_month, "timezone": a.timezone, "briefing_hour": a.briefing_hour,
        "language": a.language, "core_token_env": a.core_token_env,
    }.items() if v is not None}
    if a.unlimited:
        fields |= {"max_sites": None, "max_active_sources": None, "max_items_per_day": None,
                   "max_llm_cost_month": None, "max_drafts_month": None}
    if not fields:
        sys.exit("nothing to change")
    admin = Database.from_url(settings().admin_database_url)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    with admin.engine.begin() as conn:
        n = conn.execute(text(f"UPDATE editor.tenants SET {sets} WHERE tenant_id = :tenant"), fields | {"tenant": a.tenant}).rowcount
    print("updated" if n else f"no tenant {a.tenant}")


def cmd_usage(a):
    from . import quotas
    with _db().tenant(a.tenant) as s:
        u = quotas.usage(s)
    if u is None:
        sys.exit("no such tenant")
    for what, (limit_col, used_col) in quotas.LIMITS.items():
        print(f"{what:<10} {u.used.get(used_col)!s:>10} / {u.plan.get(limit_col) if u.plan.get(limit_col) is not None else 'unlimited'}")


def cmd_token_new(a):
    from .auth import create_token
    print(create_token(_db(), a.tenant, a.name, a.role))


def cmd_recipient_add(a):
    with _db().tenant(a.tenant) as s:
        s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES (:t, :c, :a) "
                       "ON CONFLICT DO NOTHING"), {"t": a.tenant, "c": a.channel, "a": a.address})
    print("recipient added")


def _site_id(s, domain):
    from . import models as m
    sid = s.execute(select(m.sites.c.id).where(m.sites.c.domain == domain)).scalar()
    if sid is None:
        sys.exit(f"unknown site {domain}: run `editor site add {domain}` first")
    return sid


def cmd_site_add(a):
    from . import profile, sources
    from .runtime import core, http
    from .site import SiteCrawler
    with http() as client:
        res = profile.analyse_site(_db(), a.tenant, a.domain, SiteCrawler(client, max_pages=a.max_pages), core())
        print(json.dumps({k: res[k] for k in ("status", "problems", "profile_id", "changes")}, indent=2, default=str))
        if res["status"] != "failed" and not a.no_discovery:
            rep = sources.discover(_db(), a.tenant, res["site_id"], client, core())
            print(f"sources: {rep.candidates} rated, {rep.active} active, {rep.candidate} candidates, "
                  f"{rep.rejected} rejected, {rep.failed} unreadable")
            for n in rep.notes:
                print("  note:", n)


def cmd_profile_show(a):
    from . import models as m
    with _db().tenant(a.tenant) as s:
        sid = _site_id(s, a.domain)
        p = m.editorial_profiles
        q = select(p).where(p.c.site_id == sid)
        q = q.where(p.c.version == a.version) if a.version else q.order_by(p.c.version.desc()).limit(1)
        row = s.execute(q).first()
    if row is None:
        sys.exit("no profile")
    print(f"version {row.version} · {row.status} · {row.origin}")
    if row.changes:
        print("changes:", json.dumps(row.changes, ensure_ascii=False, indent=2))
    print(json.dumps(row.body, ensure_ascii=False, indent=2))


def cmd_profile_approve(a):
    from . import profile
    with _db().tenant(a.tenant) as s:
        if not profile.approve(s, _site_id(s, a.domain), a.version, a.by):
            sys.exit("version not found")
    print(f"{a.domain} v{a.version} approved by {a.by}")


def cmd_profile_edit(a):
    from . import profile
    body = json.load(open(a.file, encoding="utf-8"))
    with _db().tenant(a.tenant) as s:
        pid = profile.edit(s, a.tenant, _site_id(s, a.domain), body)
    print(f"new draft {pid}: approve it to use it")


def cmd_sources(a):
    from . import models as m
    with _db().tenant(a.tenant) as s:
        rows = s.execute(select(m.sources).where(m.sources.c.site_id == _site_id(s, a.domain))
                         .order_by(m.sources.c.status, m.sources.c.score.desc().nulls_last())).all()
    for r in rows:
        print(f"{r.id:>5} {r.status:<10} {r.score if r.score is not None else '-':<6} {r.kind:<12} {r.origin:<16} {r.name}"
              + (f"  ({r.status_reason})" if r.status_reason else ""))


def cmd_tick(a):
    from .runtime import runner
    rep = runner().tick()
    print(json.dumps({"tenants": rep.tenants, "errors": rep.errors}, indent=2))


def cmd_briefing(a):
    from . import briefing
    from .runtime import runner
    day = date.fromisoformat(a.date) if a.date else date.today()
    r = runner()
    bid = r.morning(a.tenant, day, datetime.now(UTC)) if a.send else briefing.build(_db(), a.tenant, day)
    with _db().tenant(a.tenant) as s:
        print(s.execute(text("SELECT body_md FROM editor.briefings WHERE id = :i"), {"i": bid}).scalar())


def cmd_serve(a):
    import uvicorn
    uvicorn.run("editor.api:default_app", factory=True, host=a.host, port=a.port, proxy_headers=True)


def cmd_worker(a):
    from .db import sqlite_path
    from .runtime import settings
    if sqlite_path(settings().database_url) is not None:
        return _local_worker()
    from .tasks import app
    with app.open():
        if a.apply_schema:
            app.schema_manager.apply_schema()
        app.run_worker()


def _local_worker():
    """SQLite has no job queue: one process ticks at start and at every hour."""
    import logging
    import time
    from datetime import timedelta

    from .runtime import runner

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("editor.worker")
    while True:
        try:
            report = runner().tick()
            log.info("tick: %s", {"tenants": report.tenants, "errors": report.errors})
        except Exception:
            log.exception("tick failed")
        now = datetime.now(UTC)
        time.sleep(((now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now).total_seconds())


def main(argv=None):
    p = argparse.ArgumentParser(prog="editor", description="EuLLM Agent Editor Mode")
    sub = p.add_subparsers(required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("migrate", cmd_migrate, "create or update the editor schema")
    sp = add("tenant-add", cmd_tenant_add, "create a tenant"); sp.add_argument("tenant"); sp.add_argument("--name")
    sp = add("tenant-plan", cmd_tenant_plan, "set a tenant's plan, limits and settings (owner connection)")
    sp.add_argument("tenant"); sp.add_argument("--plan"); sp.add_argument("--max-sites", type=int)
    sp.add_argument("--max-sources", type=int); sp.add_argument("--max-items-day", type=int)
    sp.add_argument("--max-cost-month", type=float); sp.add_argument("--max-drafts-month", type=int)
    sp.add_argument("--timezone"); sp.add_argument("--briefing-hour", type=int); sp.add_argument("--language")
    sp.add_argument("--core-token-env"); sp.add_argument("--unlimited", action="store_true")
    sp = add("usage", cmd_usage, "usage of a tenant against its plan"); sp.add_argument("--tenant", required=True)
    sp = add("token-new", cmd_token_new, "create a dashboard/API token (printed once)")
    sp.add_argument("--tenant", required=True); sp.add_argument("--name", required=True)
    sp.add_argument("--role", choices=["owner", "editor", "viewer"], default="owner")
    sp = add("recipient-add", cmd_recipient_add, "send the briefing to an address")
    sp.add_argument("--tenant", required=True); sp.add_argument("--channel", choices=["email", "telegram"], required=True)
    sp.add_argument("--address", required=True)
    sp = add("site-add", cmd_site_add, "analyse a domain, write a draft profile, discover sources")
    sp.add_argument("domain"); sp.add_argument("--tenant", required=True); sp.add_argument("--max-pages", type=int, default=40)
    sp.add_argument("--no-discovery", action="store_true")
    sp = add("profile-show", cmd_profile_show, "print a profile version")
    sp.add_argument("domain"); sp.add_argument("--tenant", required=True); sp.add_argument("--version", type=int)
    sp = add("profile-approve", cmd_profile_approve, "approve a profile version")
    sp.add_argument("domain"); sp.add_argument("version", type=int); sp.add_argument("--tenant", required=True)
    sp.add_argument("--by", required=True)
    sp = add("profile-edit", cmd_profile_edit, "save an edited profile (JSON file) as a new draft")
    sp.add_argument("domain"); sp.add_argument("file"); sp.add_argument("--tenant", required=True)
    sp = add("sources", cmd_sources, "list the sources of a site"); sp.add_argument("domain"); sp.add_argument("--tenant", required=True)
    add("tick", cmd_tick, "run one scheduler tick now")
    sp = add("briefing", cmd_briefing, "build (and with --send, also propose and send) a briefing")
    sp.add_argument("--tenant", required=True); sp.add_argument("--date"); sp.add_argument("--send", action="store_true")
    sp = add("serve", cmd_serve, "dashboard and API"); sp.add_argument("--host", default="127.0.0.1"); sp.add_argument("--port", type=int, default=8090)
    sp = add("worker", cmd_worker, "hourly tick: procrastinate on PostgreSQL, a plain loop on SQLite"); sp.add_argument("--apply-schema", action="store_true")
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
