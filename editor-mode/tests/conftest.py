"""Shared fixtures. Every database test runs twice: on SQLite (a new file per
test) and on PostgreSQL. The PostgreSQL run needs EDITOR_TEST_DATABASE_URL (a
superuser connection to a disposable server, e.g.
postgres://postgres@127.0.0.1:5432/postgres) and is skipped without it; each
test session creates its own database and drops it at the end."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from editor import migrate
from editor.db import Database

# The pages are checked in Italian, the language their texts are written in.
os.environ.setdefault("EDITOR_LANGUAGE", "it")

FIXTURES = Path(__file__).parent / "fixtures"
APP_PASSWORD = "editor_app_test"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _with_db(url: str, dbname: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if user is None:
        netloc = parts.netloc
    else:
        netloc = f"{user}:{password}@{netloc}"
    return urlunsplit((parts.scheme, netloc, "/" + dbname, parts.query, ""))


@pytest.fixture(scope="session")
def pg_urls():
    admin = os.environ.get("EDITOR_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("EDITOR_TEST_DATABASE_URL not set: database tests not run")
    plain = admin.replace("postgresql+psycopg://", "postgresql://").replace("postgres://", "postgresql://")
    name = f"editor_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    admin_db = _with_db(plain, name)
    migrate.upgrade(admin_db)
    with psycopg.connect(admin_db, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE editor_app WITH LOGIN PASSWORD '{APP_PASSWORD}'")
        conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO editor_app')
    app_db = _with_db(plain, name, "editor_app", APP_PASSWORD)
    yield admin_db, app_db
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def pg_db(pg_urls) -> Database:
    database = Database.from_url(pg_urls[1])
    database.admin_url = pg_urls[0]
    yield database
    database.engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path) -> Database:
    """Tests marked ``postgres`` (row level security, roles, several tenants)
    run on PostgreSQL only."""
    if request.param == "sqlite" and request.node.get_closest_marker("postgres"):
        pytest.skip("PostgreSQL only")
    if request.param == "postgres":
        yield request.getfixturevalue("pg_db")
        return
    url = f"sqlite:///{tmp_path}/editor.db"
    migrate.upgrade(url)
    database = Database.from_url(url)
    database.admin_url = url
    yield database
    database.engine.dispose()


@pytest.fixture
def tenant(db) -> str:
    """A fresh tenant with one site."""
    from editor import repo

    tid = f"t-{uuid.uuid4().hex[:8]}"
    if not db.sqlite:
        # The shared archive outlives a test on the session's database: each
        # test starts from an empty one, as a fresh installation does.
        with psycopg.connect(db.admin_url, autocommit=True) as conn:
            conn.execute("TRUNCATE editor.sources, editor.source_items, editor.observations, editor.topics, "
                         "editor.topic_items, editor.trend_scores, editor.jobs, editor.llm_usage CASCADE")
    with db.tenant(tid) as s:
        repo.ensure_tenant(s, tid, name=tid)
    return tid


def set_plan(db, tenant, **fields):
    """Change the tenant row as the operator does (`editor tenant-plan`)."""
    from sqlalchemy import text

    admin = Database.from_url(db.admin_url)
    try:
        with admin.engine.begin() as conn:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            conn.execute(text(f"UPDATE editor.tenants SET {sets} WHERE tenant_id = :t"), fields | {"t": tenant})
    finally:
        admin.engine.dispose()


def pause(s, seconds: float) -> None:
    """Wait inside the transaction, holding what it holds."""
    import time

    from sqlalchemy import text

    if s.get_bind().dialect.name == "sqlite":
        time.sleep(seconds)
    else:
        s.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})


def feed(db, tenant, source_id, items):
    """Store items as the hourly collection does: in the archive, under the
    archive source that `source_id` (a source a newsroom follows) follows."""
    from sqlalchemy import text

    from editor import repo
    from editor.ingest import SourceReport, store_items

    with db.tenant(tenant) as s:
        aid = s.execute(text("SELECT archive_id FROM editor.sources WHERE id = :i"), {"i": source_id}).scalar()
    archive = repo.archive_of(db, tenant)
    report = SourceReport(aid, "test")
    with db.tenant(archive) as s:
        store_items(s, archive, aid, items, report)
    return report
