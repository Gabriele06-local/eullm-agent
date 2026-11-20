"""Objects built from the environment for the worker, the CLI and the API."""

from __future__ import annotations

from functools import lru_cache

from .briefing import Sender
from .config import Settings
from .core_client import CoreClient
from .core_fetch import core_http_client
from .db import Database
from .publishing import Publisher
from .schedule import Runner


def environment() -> Settings:
    """The environment alone: the database connections never come from the panel."""
    return Settings.from_env()


@lru_cache(maxsize=1)
def settings() -> Settings:
    """The environment, completed with the settings of /admin › Impostazioni
    (appsettings.py), which also go into os.environ for the modules that
    read it."""
    from . import appsettings

    url = environment().admin_database_url
    if url:  # a connection of its own: the cached ones follow the environment as before
        db = Database.from_url(url)
        try:
            appsettings.load(db)
        finally:
            db.engine.dispose()
    return Settings.from_env()


@lru_cache(maxsize=1)
def database() -> Database:
    return Database.from_url(environment().database_url)


@lru_cache(maxsize=1)
def admin_database() -> Database:
    """The owner connection: plans, accounts, the operator's work."""
    st = environment()
    if not st.admin_database_url or st.admin_database_url == st.database_url:
        return database()
    return Database.from_url(st.admin_database_url)


def core() -> CoreClient | None:
    st = settings()
    return CoreClient(st.core_url, st.core_token, st.core_model, delegate=st.core_delegate) if st.core_token else None


def http():
    st = settings()
    if not st.core_token:
        raise SystemExit("EDITOR_CORE_TOKEN is not set: Editor Mode reads the web only through the Core")
    return core_http_client(st.core_url, st.core_token, delegate=st.core_delegate)


def runner() -> Runner:
    return Runner(database(), settings(), http, core(), Sender(settings()))


def handlers():
    """What the worker runs for each kind of queued job."""
    from . import delivery, images, work

    st = settings()
    return work.handlers(database(), http, core(), Publisher(public_url=st.public_url), runner(),
                         images.from_settings(st), delivery.smtp_sender(st), st.public_url)
