"""A tenant's data: exported whole without credentials, deleted whole, and
what was collected long ago and is not cited pruned."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from editor import auth, models as m, repo, tenant_data
from editor.api import create_app
from editor.db import Database

from test_tenancy import populate, tenant_tables


def test_every_tenant_table_is_covered():
    tables = {t.name for t in m.metadata.sorted_tables if "tenant_id" in t.c}
    assert set(tenant_data.DELETE_ORDER) == tables


@pytest.mark.postgres
def test_every_table_in_the_database_is_covered(pg_urls):
    assert set(tenant_data.DELETE_ORDER) == set(tenant_tables(pg_urls[0]))


def test_the_export_has_everything_but_credentials(db, tenant, monkeypatch):
    populate(db, tenant, monkeypatch)
    data = json.loads(tenant_data.export_json(db, tenant))
    assert data["tenant"] == tenant and set(data["tables"]) == set(tenant_data.DELETE_ORDER)
    filled = {name for name, rows in data["tables"].items() if rows}
    assert {"sites", "sources", "source_items", "drafts", "publications", "secrets", "access_tokens"} <= filled
    assert all(r["tenant_id"] == tenant for rows in data["tables"].values() for r in rows)
    assert data["tables"]["secrets"] == [{"tenant_id": tenant, "name": "X", "updated_at": data["tables"]["secrets"][0]["updated_at"]}]
    assert all("token_sha256" not in r for r in data["tables"]["access_tokens"])


@pytest.mark.postgres
def test_the_export_holds_no_other_tenant(db, tenant, monkeypatch):
    populate(db, tenant, monkeypatch)
    other = f"{tenant}-b"
    with db.tenant(other) as s:
        repo.ensure_tenant(s, other, "B")
    data = tenant_data.export(db, other)
    assert data["tables"]["tenants"][0]["tenant_id"] == other
    assert sum(len(rows) for rows in data["tables"].values()) == 1


def test_a_deleted_tenant_leaves_nothing(db, tenant, monkeypatch):
    populate(db, tenant, monkeypatch)
    admin = Database.from_url(db.admin_url)
    keep = None
    if not db.sqlite:
        keep = f"{tenant}-b"
        with db.tenant(keep) as s:
            repo.ensure_tenant(s, keep, "B")
    counts = tenant_data.delete_tenant(admin, tenant)
    assert counts["tenants"] == 1 and counts["source_items"] > 0
    with admin.engine.connect() as conn:
        for name in tenant_data.DELETE_ORDER:
            assert conn.execute(text(f"SELECT count(*) FROM editor.{name} WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0, name
        if keep:
            assert conn.execute(text("SELECT count(*) FROM editor.tenants WHERE tenant_id = :t"), {"t": keep}).scalar() == 1
    with pytest.raises(LookupError):
        tenant_data.delete_tenant(admin, tenant)
    admin.engine.dispose()


def test_old_uncited_items_and_finished_jobs_are_pruned(db, tenant, monkeypatch):
    from test_publishing import approved_draft

    approved_draft(db, tenant)
    now = datetime.now(UTC)
    with db.tenant(tenant) as s:
        s.execute(update(m.source_items).values(fetched_at=now - timedelta(days=400)))
        s.execute(m.jobs.insert().values(tenant_id=tenant, kind="collect", status="done",
                                         started_at=now - timedelta(days=400), finished_at=now - timedelta(days=400)))
        s.execute(m.jobs.insert().values(tenant_id=tenant, kind="collect", status="done",
                                         started_at=now, finished_at=now))
        before = set(s.execute(select(m.source_items.c.id)).scalars())
        cited = set(s.execute(select(m.draft_claim_sources.c.item_id)).scalars()) | \
            set(s.execute(select(m.proposal_citations.c.item_id)).scalars())
    assert before - cited, "the test needs some uncited items"
    assert tenant_data.prune(db, tenant, now=now, days=0) == {}  # 0 keeps everything
    out = tenant_data.prune(db, tenant, now=now, days=365)
    with db.tenant(tenant) as s:
        after = set(s.execute(select(m.source_items.c.id)).scalars())
        jobs = s.execute(select(m.jobs.c.finished_at)).scalars().all()
    assert after == before & cited and out["items"] == len(before - after)
    assert out["jobs"] == 1 and len(jobs) == 1
    # Recent items stay.
    assert tenant_data.prune(db, tenant, now=now - timedelta(days=100), days=365)["items"] == 0


def test_the_owner_downloads_the_data(db, tenant, monkeypatch):
    populate(db, tenant, monkeypatch)
    owner = auth.create_token(db, tenant, "boss", "owner")
    editor = auth.create_token(db, tenant, "ed", "editor")
    c = TestClient(create_app(db))
    r = c.get("/api/export", headers={"Authorization": f"Bearer {owner}"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.json()["tenant"] == tenant
    assert c.get("/api/export", headers={"Authorization": f"Bearer {editor}"}).status_code == 403
    c.post("/login", data={"token": owner})
    assert "Scarica i dati" in c.get("/settings").text
    assert c.get("/ui/export").json()["tenant"] == tenant


def test_the_cli_asks_for_the_tenant_twice(db, tenant, monkeypatch):
    from editor import cli

    monkeypatch.setenv("EDITOR_DATABASE_URL", db.admin_url)
    monkeypatch.setenv("EDITOR_ADMIN_DATABASE_URL", db.admin_url)
    from editor import runtime
    runtime.settings.cache_clear()
    with pytest.raises(SystemExit, match="--confirm"):
        cli.main(["tenant-delete", tenant, "--confirm", "other"])
    cli.main(["tenant-delete", tenant, "--confirm", tenant])
    admin = Database.from_url(db.admin_url)
    with admin.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM editor.tenants WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0
    admin.engine.dispose()
    runtime.settings.cache_clear()


def test_the_daily_maintenance_prunes(db, tenant, monkeypatch):
    from editor import schedule, sources
    from editor.config import Settings

    pruned = []
    monkeypatch.setattr(schedule, "collect_tenant", lambda *a, **k: None)
    monkeypatch.setattr(sources, "maintain", lambda *a, **k: None)
    monkeypatch.setattr(tenant_data, "prune", lambda db_, t, now=None: pruned.append(t))
    runner = schedule.Runner(db, Settings(), http_factory=lambda: __import__("httpx").Client())
    runner.tenant_tick(tenant, datetime(2026, 10, 9, 2, tzinfo=UTC), [])  # 04:00 in Rome
    assert pruned == [tenant]
    runner.tenant_tick(tenant, datetime(2026, 10, 9, 5, tzinfo=UTC), [])  # once a day
    assert pruned == [tenant]
