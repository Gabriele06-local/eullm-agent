"""Command line of Editor Mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime

from sqlalchemy import select, text


def _db():
    from .runtime import database
    return database()


def cmd_migrate(a):
    from . import migrate
    from .runtime import settings
    migrate.upgrade(settings().admin_database_url)
    print("editor schema up to date")
    _setup_link(quiet=True)


def _setup_link(quiet: bool = False) -> None:
    """While no administrator exists, print the first-start link."""
    from . import accounts
    from .runtime import settings
    token = accounts.setup_token(_admin())
    if token is None:
        if not quiet:
            print("an administrator exists already: sign in, or ask an operator for a password-reset link")
        return
    base = settings().public_url or f"http://127.0.0.1:{os.environ.get('EDITOR_PORT', '8090')}"
    print(f"no administrator yet: open {base}/setup/{token} to create yours (the link works once)")


def cmd_setup_link(a):
    _setup_link()


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
    if not n:
        sys.exit(f"no tenant {a.tenant}")
    print("updated")
    if "max_llm_cost_month" in fields:
        _core_limits(a.tenant, fields["max_llm_cost_month"])


def _core_limits(tenant: str, max_cost_month: float | None) -> None:
    """With a delegating Core token, the plan's model budget becomes the
    tenant's limit in the Core too: the Core reserves calls under way, so
    work running at the same time cannot overshoot it."""
    from .core_client import CoreError
    from .runtime import core, settings
    client = core()
    if client is None or not settings().core_delegate:
        return
    limits = {} if max_cost_month is None else {"max_cost_per_month": max_cost_month}
    try:
        r = client.set_limits(tenant, limits)
    except CoreError as e:
        sys.exit(f"plan saved, but the Core limits of {tenant} were not updated: {e}")
    print(f"Core limits of {r['tenant']}: {json.dumps(r['effective'])}")


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


def _admin():
    from .runtime import admin_database
    return admin_database()


def cmd_user_add(a):
    """An account that signs in with email and password. Without
    --password-stdin a password is generated and printed once."""
    from . import accounts
    from . import models as m
    password = sys.stdin.readline().rstrip("\n") if a.password_stdin else accounts.random_password()
    try:
        accounts.check_password(password)
        # One transaction: an unknown newsroom leaves no half-made account.
        with _admin().engine.begin() as conn:
            if a.tenant and conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.tenant_id == a.tenant)).first() is None:
                known = ", ".join(conn.execute(select(m.tenants.c.tenant_id).order_by(m.tenants.c.tenant_id)).scalars())
                sys.exit(f"no newsroom {a.tenant}" + (f" (newsrooms: {known})" if known else ""))
            uid = accounts.ensure_user(conn, a.email, a.name or "", a.operator)
            conn.execute(m.users.update().where(m.users.c.id == uid).values(
                password_hash=accounts.hash_password(password), failed_logins=0, locked_until=None))
            if a.tenant:
                accounts.set_member(conn, uid, a.tenant, a.role)
    except accounts.AccountError as e:
        sys.exit(str(e))
    print(f"account {accounts.normalise_email(a.email)} ready" + (" (operator)" if a.operator else "")
          + (f", {a.role} of {a.tenant}" if a.tenant else ""))
    if not a.password_stdin:
        print(f"password: {password}")


def cmd_operator_key(a):
    from . import accounts
    print(accounts.create_operator_token(_admin(), a.name))


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
    from .core_fetch import acting_for
    from .runtime import core, http
    from .site import SiteCrawler
    base = core()
    model = base.for_tenant(_db(), a.tenant) if base is not None else None
    with acting_for(a.tenant), http() as client:
        res = profile.analyse_site(_db(), a.tenant, a.domain, SiteCrawler(client, max_pages=a.max_pages), model)
        print(json.dumps({k: res[k] for k in ("status", "problems", "profile_id", "changes")}, indent=2, default=str))
        if res["status"] != "failed" and not a.no_discovery:
            rep = sources.discover(_db(), a.tenant, res["site_id"], client, model)
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
    """Queue every tenant's tick and run, here, everything that is waiting."""
    from . import queue
    from .runtime import handlers
    db = _db()
    queue.enqueue_ticks(db)
    print(json.dumps({"jobs_run": queue.drain(db, handlers())}))


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
    import logging

    from . import queue
    from .runtime import database, handlers
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = database()
    # SQLite has one tenant, so one job at a time anyway.
    slots = 1 if db.sqlite else a.concurrency
    queue.Worker(db, handlers(), concurrency=slots).run()


def cmd_tenant_export(a):
    from . import tenant_data
    data = tenant_data.export_json(_db(), a.tenant)
    if a.out:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(data)
        print(f"tenant {a.tenant} exported to {a.out}")
    else:
        print(data)


def cmd_tenant_delete(a):
    """Irreversible: asks for the tenant id twice."""
    from . import tenant_data
    from .db import Database
    from .runtime import settings
    if a.confirm != a.tenant:
        sys.exit(f"this deletes every row of {a.tenant} for good: repeat it with --confirm {a.tenant}")
    try:
        counts = tenant_data.delete_tenant(Database.from_url(settings().admin_database_url), a.tenant)
    except LookupError as e:
        sys.exit(str(e))
    print(f"tenant {a.tenant} deleted: {sum(counts.values())} rows")
    print("its usage and audit in the Core (tenant editor/<id>) are kept there")


def cmd_secret_key(a):
    from . import vault
    print(vault.new_key())


def main(argv=None):
    p = argparse.ArgumentParser(prog="editor", description="EuLLM Agent Editor Mode")
    sub = p.add_subparsers(required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("migrate", cmd_migrate, "create or update the editor schema (prints the first-start link while there is no administrator)")
    add("setup-link", cmd_setup_link, "print a new first-start link, to create the first administrator from the browser")
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
    sp = add("user-add", cmd_user_add, "create an account (email and password), optionally operator or member of a tenant")
    sp.add_argument("email"); sp.add_argument("--name"); sp.add_argument("--operator", action="store_true")
    sp.add_argument("--tenant"); sp.add_argument("--role", choices=["owner", "editor", "viewer"], default="owner")
    sp.add_argument("--password-stdin", action="store_true", help="read the password from stdin instead of generating one")
    sp = add("operator-key", cmd_operator_key, "create an operator API key for /api/operator (printed once)")
    sp.add_argument("--name", required=True)
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
    add("tick", cmd_tick, "queue every tenant's tick and run the waiting work now")
    sp = add("briefing", cmd_briefing, "build (and with --send, also propose and send) a briefing")
    sp.add_argument("--tenant", required=True); sp.add_argument("--date"); sp.add_argument("--send", action="store_true")
    sp = add("serve", cmd_serve, "dashboard and API"); sp.add_argument("--host", default="127.0.0.1"); sp.add_argument("--port", type=int, default=8090)
    sp = add("worker", cmd_worker, "run queued work and every tenant's hourly tick")
    sp.add_argument("--concurrency", type=int, default=int(os.environ.get("EDITOR_WORKER_CONCURRENCY", "2")),
                    help="jobs run at the same time, each for a different tenant (PostgreSQL only)")
    sp = add("tenant-export", cmd_tenant_export, "every row of a tenant as JSON, credentials left out")
    sp.add_argument("tenant"); sp.add_argument("--out", help="file to create (default: print)")
    sp = add("tenant-delete", cmd_tenant_delete, "delete a tenant and all its data, for good (owner connection)")
    sp.add_argument("tenant"); sp.add_argument("--confirm", required=True, help="the tenant id again")
    add("secret-key", cmd_secret_key, "print a new EDITOR_SECRET_KEY (key of the credentials stored in the database)")
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
