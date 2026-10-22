"""The local installation: Editor Mode on one SQLite file."""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from editor import migrate, models as m, repo
from editor.db import Database


@pytest.fixture
def local(tmp_path):
    url = f"sqlite:///{tmp_path}/editor.db"
    assert migrate.upgrade_sqlite(url) == ["0001_schema", "0002_job_queue", "0003_secrets", "0004_accounts",
                                           "0005_billing", "0006_desired_plan", "0007_shared_archive",
                                           "0008_trend_snapshots", "0009_draft_seo"]
    database = Database.from_url(url)
    yield url, database
    database.engine.dispose()


def test_migrations_apply_once(local):
    url, _ = local
    assert migrate.upgrade_sqlite(url) == []


def test_one_tenant_per_file(local):
    _, db = local
    with db.tenant("solo") as s:
        repo.ensure_tenant(s, "solo", "Solo")
    with db.tenant("solo") as s:
        repo.ensure_tenant(s, "solo", "Solo again")  # the same tenant: fine
    with pytest.raises(IntegrityError, match="one tenant"):
        with db.tenant("other") as s:
            repo.ensure_tenant(s, "other", "Other")


def test_every_model_column_exists(local):
    _, db = local
    insp = inspect(db.engine)
    for table in m.metadata.tables.values():
        have = {c["name"] for c in insp.get_columns(table.name, schema="editor")}
        assert {c.name for c in table.columns} <= have, table.name


def test_times_come_back_in_utc(local):
    from datetime import UTC, datetime, timedelta, timezone

    _, db = local
    with db.tenant("solo") as s:
        repo.ensure_tenant(s, "solo", "Solo")
        sid = repo.upsert_source(s, "solo", kind="rss", name="f", url="https://a.example/f")
        rome = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=2)))
        repo.mark_source(s, sid, newest=rome)
        repo.mark_source(s, sid, newest=rome - timedelta(days=1))  # older: kept
    with db.tenant("solo") as s:
        row = s.execute(m.sources.select().where(m.sources.c.id == sid)).one()
    assert row.last_item_at == datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    assert row.status_changed_at.tzinfo is UTC  # a default written by the database


@pytest.mark.postgres
def test_same_columns_as_postgres(local, pg_urls):
    """The SQLite schema follows the Alembic one, column by column."""
    from sqlalchemy import create_engine

    _, db = local
    pg = create_engine(pg_urls[0].replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        pg_insp, lite = inspect(pg), inspect(db.engine)
        tables = set(pg_insp.get_table_names(schema="editor")) - {"alembic_version"}
        assert tables == set(lite.get_table_names(schema="editor")) - {"schema_migrations", "sqlite_sequence"}
        for t in tables:
            assert ({c["name"] for c in pg_insp.get_columns(t, schema="editor")}
                    == {c["name"] for c in lite.get_columns(t, schema="editor")}), t
    finally:
        pg.dispose()

