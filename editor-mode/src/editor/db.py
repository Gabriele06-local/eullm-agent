"""Database access. Two backends, same code:

- PostgreSQL (a server with several tenants): every query runs inside a
  tenant scope, the tenant id is set with SET LOCAL and row level security
  does the filtering, so a missing WHERE clause cannot leak another
  tenant's rows.
- SQLite (the local installation): one file, one tenant (the schema refuses
  a second one), so there is nothing to isolate. The file is attached as
  schema `editor`, so the same statements work on both, and every
  transaction takes the write lock at its start (BEGIN IMMEDIATE), which
  serialises what PostgreSQL serialises with advisory locks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .config import sqlalchemy_url

SQLITE_BUSY_SECONDS = 30


def sqlite_path(url: str) -> Path | None:
    """The file of a SQLite url, or None for any other url: sqlite:///editor.db
    is relative to the working directory, sqlite:////var/lib/editor.db absolute."""
    if not url.startswith("sqlite:"):
        return None
    path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else ""
    if not path or path == ":memory:":
        raise ValueError("Editor Mode needs a SQLite file: sqlite:///path/to/editor.db")
    return Path(path)


def make_engine(url: str, **kwargs) -> Engine:
    path = sqlite_path(url)
    if path is None:
        return create_engine(sqlalchemy_url(url), pool_pre_ping=True, **kwargs)
    return _sqlite_engine(path, **kwargs)


def _sqlite_engine(path: Path, **kwargs) -> Engine:
    path = path.expanduser().resolve()

    def connect():
        conn = sqlite3.connect(":memory:", timeout=SQLITE_BUSY_SECONDS, check_same_thread=False,
                               isolation_level=None)
        conn.execute("ATTACH DATABASE ? AS editor", (str(path),))
        conn.execute("PRAGMA editor.journal_mode = WAL")
        conn.execute("PRAGMA editor.synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_SECONDS * 1000}")
        return conn

    kwargs.setdefault("poolclass", QueuePool)
    engine = create_engine("sqlite://", creator=connect, **kwargs)

    @event.listens_for(engine, "begin")
    def _begin(conn):
        # A connection marked read_only reads beside a transaction that
        # holds the write lock (WAL), instead of waiting for it.
        conn.exec_driver_sql("BEGIN" if conn.get_execution_options().get("read_only") else "BEGIN IMMEDIATE")

    return engine


def is_sqlite(bind) -> bool:
    """bind: an Engine, Connection or Session."""
    if isinstance(bind, Session):
        bind = bind.get_bind()
    return bind.dialect.name == "sqlite"


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.sqlite = is_sqlite(engine)
        self._factory = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "Database":
        return cls(make_engine(url))

    @contextmanager
    def tenant(self, tenant_id: str) -> Iterator[Session]:
        """A transaction scoped to one tenant; commits on success."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self._factory() as session, session.begin():
            session.info["tenant_id"] = tenant_id
            if not self.sqlite:
                session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
                )
            yield session
