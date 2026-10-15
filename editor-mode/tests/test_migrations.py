import psycopg
import pytest
from sqlalchemy import text

from editor.db import Database

# Roles and row level security: the server installation.
pytestmark = pytest.mark.postgres


def test_app_role_cannot_bypass_rls(pg_urls):
    with psycopg.connect(pg_urls[1]) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False)


def test_every_tenant_table_forces_rls(pg_urls):
    with psycopg.connect(pg_urls[0]) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
            WHERE n.nspname = 'editor' AND c.relkind = 'r'
            """
        ).fetchall()
    assert len(rows) >= 15
    assert all(r[1] for r in rows), rows
    # editor.tenants is not forced so the owner's editor.active_tenants() can list ids;
    # editor_app is still filtered (see test_tenants_are_isolated).
    assert sorted(r[0] for r in rows if not r[2]) == ["access_tokens", "tenants"]


def test_scheduler_lists_tenant_ids_only(db: Database):
    from editor import repo

    for t in ("list-a", "list-b"):
        with db.tenant(t) as s:
            repo.ensure_tenant(s, t, t)
    with db.engine.connect() as conn:
        ids = conn.execute(text("SELECT * FROM editor.active_tenants()")).scalars().all()
        assert {"list-a", "list-b"} <= set(ids)
        assert conn.execute(text("SELECT count(*) FROM editor.tenants")).scalar() == 0


def test_tenants_are_isolated(db: Database):
    from editor import repo

    with db.tenant("iso-a") as s:
        repo.ensure_tenant(s, "iso-a", "A")
        repo.upsert_source(s, "iso-a", kind="rss", name="feed", url="https://a.example/feed")
    with db.tenant("iso-b") as s:
        repo.ensure_tenant(s, "iso-b", "B")
        assert s.execute(text("SELECT count(*) FROM editor.sources")).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM editor.tenants")).scalar() == 1
    with db.tenant("iso-a") as s:
        assert s.execute(text("SELECT count(*) FROM editor.sources")).scalar() == 1


def test_cannot_write_rows_for_another_tenant(db: Database):
    from editor import repo

    with db.tenant("w-a") as s:
        repo.ensure_tenant(s, "w-a", "A")
    with pytest.raises(Exception, match="row-level security"):
        with db.tenant("w-b") as s:
            s.execute(
                text("INSERT INTO editor.sites (tenant_id, domain, name) VALUES ('w-a', 'x.it', 'x')")
            )


def test_no_tenant_sees_nothing(pg_urls):
    with psycopg.connect(pg_urls[1]) as conn:
        assert conn.execute("SELECT count(*) FROM editor.tenants").fetchone()[0] == 0
