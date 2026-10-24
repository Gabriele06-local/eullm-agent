"""Settings, read from the environment (EDITOR_* variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def sqlalchemy_url(url: str) -> str:
    """Accept postgres:// and postgresql:// URLs and select the psycopg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@dataclass(frozen=True)
class Settings:
    # Connection used by the application: a role without BYPASSRLS.
    database_url: str = ""
    # Connection used for migrations (owner of the editor schema).
    admin_database_url: str = ""
    core_url: str = "http://127.0.0.1:8088"
    core_token: str = ""
    core_model: str = "default"
    # The Core token delegates (`delegate: true`): every tenant is a Core
    # tenant of its own, with its usage and the limits of its plan.
    core_delegate: bool = False
    timezone: str = "Europe/Rome"
    briefing_hour: int = 8
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    telegram_token: str = ""
    # The address people open the dashboard at (https://editor.example.com),
    # for the links of invites and password resets.
    public_url: str = ""
    # Online payments (Mollie); empty: plans are paid by bank transfer and
    # recorded by the operator.
    mollie_api_key: str = ""
    # The news base (news_base.py): general sources from around the world read
    # for every newsroom. On by default on a server; tests build Settings
    # without it.
    base_news: bool = False
    # Cover images (images.py): Black Forest Labs' FLUX, on its EU endpoint.
    # No key: drafts have no image.
    images_api_key: str = ""
    images_url: str = "https://api.eu.bfl.ai/v1"
    images_model: str = "flux-2-pro"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        db = env.get("EDITOR_DATABASE_URL", "")
        return cls(
            database_url=db,
            admin_database_url=env.get("EDITOR_ADMIN_DATABASE_URL", db),
            core_url=env.get("EDITOR_CORE_URL", cls.core_url),
            core_token=env.get("EDITOR_CORE_TOKEN", ""),
            core_model=env.get("EDITOR_CORE_MODEL", cls.core_model),
            core_delegate=env.get("EDITOR_CORE_DELEGATE", "").strip().lower() in ("1", "true", "yes"),
            timezone=env.get("EDITOR_TIMEZONE", cls.timezone),
            briefing_hour=int(env.get("EDITOR_BRIEFING_HOUR", cls.briefing_hour)),
            smtp_host=env.get("EDITOR_SMTP_HOST", ""),
            smtp_port=int(env.get("EDITOR_SMTP_PORT", cls.smtp_port)),
            smtp_user=env.get("EDITOR_SMTP_USER", ""),
            smtp_password=env.get("EDITOR_SMTP_PASSWORD", ""),
            mail_from=env.get("EDITOR_MAIL_FROM", ""),
            telegram_token=env.get("EDITOR_TELEGRAM_TOKEN", ""),
            public_url=env.get("EDITOR_PUBLIC_URL", "").strip().rstrip("/"),
            mollie_api_key=env.get("EDITOR_MOLLIE_API_KEY", "").strip(),
            base_news=env.get("EDITOR_BASE_NEWS", "1").strip().lower() not in ("0", "false", "no"),
            images_api_key=env.get("EDITOR_IMAGES_API_KEY", "").strip(),
            images_url=env.get("EDITOR_IMAGES_URL", cls.images_url).strip().rstrip("/"),
            images_model=env.get("EDITOR_IMAGES_MODEL", cls.images_model).strip(),
        )
