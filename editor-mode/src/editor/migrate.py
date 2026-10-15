"""Migrations of the editor schema: Alembic on PostgreSQL, the numbered SQL
files of migrations_sqlite/ on SQLite."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import sqlalchemy_url
from .db import make_engine, sqlite_path

HERE = Path(__file__).parent
SQLITE_DIR = HERE / "migrations_sqlite"


def alembic_config(url: str) -> Config:
    cfg = Config(str(HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(url).replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    if sqlite_path(url) is not None:
        if revision != "head":
            raise SystemExit("SQLite migrations only go to the latest version")
        upgrade_sqlite(url)
        return
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    if sqlite_path(url) is not None:
        raise SystemExit("SQLite migrations cannot be undone: restore a copy of the file")
    command.downgrade(alembic_config(url), revision)


def upgrade_sqlite(url: str) -> list[str]:
    """Apply, each in its own transaction, the files not applied yet; returns their names."""
    sqlite_path(url).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(url)
    applied = []
    try:
        conn = engine.raw_connection()
        try:
            raw = conn.driver_connection
            raw.execute("CREATE TABLE IF NOT EXISTS editor.schema_migrations "
                        "(version text PRIMARY KEY, applied_at text NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            done = {r[0] for r in raw.execute("SELECT version FROM editor.schema_migrations")}
            for f in sorted(SQLITE_DIR.glob("*.sql")):
                if f.stem in done:
                    continue
                version = f.stem.replace("'", "''")
                raw.executescript(f"BEGIN IMMEDIATE;\n{f.read_text()}\n"
                                  f"INSERT INTO editor.schema_migrations (version) VALUES ('{version}');\nCOMMIT;")
                applied.append(f.stem)
        finally:
            conn.close()
    finally:
        engine.dispose()
    return applied
