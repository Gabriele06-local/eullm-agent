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
    # Not forced on the owner, whose SECURITY DEFINER functions read across
    # tenants: editor.active_tenants() (tenants), resolve_token (access_tokens),
    # claim_job and requeue_stale (jobs), image_by_token (draft_images, the
    # cover images the sites fetch without a login), feed_by_token (feeds, the
    # same for the tools that read the approved articles). editor_app is still filtered
    # (see test_tenants_are_isolated). Memberships, sessions and invites are
    # not granted to editor_app at all: the owner connection reads them, as
    # subscriptions and payments.
    assert sorted(r[0] for r in rows if not r[2]) == ["access_tokens", "draft_images", "feeds", "invites", "jobs", "memberships", "payments",
                                                       "sessions", "subscriptions", "tenants"]


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
        # The shared archive is readable by every newsroom; the sources a
        # newsroom follows are its own.
        assert s.execute(text("SELECT count(*) FROM editor.sources WHERE shared IS NOT TRUE")).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM editor.tenants")).scalar() == 1
    with db.tenant("iso-a") as s:
        assert s.execute(text("SELECT count(*) FROM editor.sources WHERE shared IS NOT TRUE")).scalar() == 1


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


def test_copies_of_a_cover_become_pointers_on_upgrade(pg_urls):
    """0048 on a server with covers already copied for the translations: the
    copies point at the first image, the bytes stay once; as the server's
    owner, who is not a superuser."""
    import uuid

    from editor import migrate, repo
    from test_publishing import approved_draft

    admin = pg_urls[0].rsplit("/", 1)[0]
    name = f"editor_mig_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin + "/postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = f"{admin}/{name}"
    try:
        migrate.upgrade(url, "0047")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("ALTER ROLE editor_app WITH LOGIN PASSWORD 'editor_app_test'")
            conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO editor_app')
        app = pg_urls[1].rsplit("/", 1)[0] + "/" + name
        db = Database.from_url(app)
        db.admin_url = url
        tenant = "t-mig"
        with db.tenant(tenant) as s:
            repo.ensure_tenant(s, tenant, name=tenant)
        did, _ = approved_draft(db, tenant)
        with db.tenant(tenant) as s:
            cols = [c for c in s.execute(text("SELECT column_name FROM information_schema.columns WHERE "
                                              "table_schema = 'editor' AND table_name = 'drafts'")).scalars() if c != "id"]
            picked = ", ".join("version + 1" if c == "version" else c for c in cols)
            other = s.execute(text(f"INSERT INTO editor.drafts ({', '.join(cols)}) SELECT {picked} "
                                   "FROM editor.drafts WHERE id = :d RETURNING id"), {"d": did}).scalar()
            for d, tok in ((did, "a"), (other, "b")):
                s.execute(text("INSERT INTO editor.draft_images (tenant_id, draft_id, token, prompt, model, mime, content) "
                               "VALUES (:t, :d, :k, 'p', 'm', 'image/jpeg', :c)"),
                          {"t": tenant, "d": d, "k": tok, "c": b"\xff\xd8same"})
        db.engine.dispose()
        migrate.upgrade(url, "head")
        db = Database.from_url(app)
        with db.tenant(tenant) as s:
            rows = s.execute(text("SELECT draft_id, length(content), same_as FROM editor.draft_images ORDER BY id")).all()
            assert [tuple(r) for r in rows] == [(did, 6, None), (other, 0, did)]
            assert bytes(s.execute(text("SELECT content FROM editor.image_by_token('b')")).scalar()) == b"\xff\xd8same"
        db.engine.dispose()
    finally:
        with psycopg.connect(admin + "/postgres", autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
