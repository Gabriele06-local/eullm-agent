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


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def database() -> Database:
    return Database.from_url(settings().database_url)


@lru_cache(maxsize=1)
def admin_database() -> Database:
    """The owner connection: plans, accounts, the operator's work."""
    st = settings()
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
    from . import work

    return work.handlers(database(), http, core(), Publisher(), runner())
