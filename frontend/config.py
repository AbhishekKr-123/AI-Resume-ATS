"""Non-secret frontend configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class FrontendSettings:
    api_base_url: str
    supabase_url: str | None
    supabase_publishable_key: str | None
    max_upload_bytes: int
    auth_configuration_error: str | None = None
    oauth_redirect_url: str = "http://localhost:8501"


def is_publishable_key(value: str | None) -> bool:
    """Reject Supabase's new secret-key format before it can reach Auth."""
    return bool(value and not value.strip().startswith("sb_secret_"))


def _frontend_value(name: str, file_values: dict[str, str | None]) -> str | None:
    """Read only frontend-safe names; never load server-only dotenv values."""
    return os.getenv(name) or file_values.get(name)


def load_frontend_settings() -> FrontendSettings:
    root = Path(__file__).resolve().parents[1]
    file_values = dotenv_values(root / ".env")
    publishable_key = _frontend_value("SUPABASE_PUBLISHABLE_KEY", file_values)
    invalid_key = publishable_key is not None and not is_publishable_key(publishable_key)
    return FrontendSettings(
        api_base_url=(_frontend_value("API_BASE_URL", file_values) or "http://127.0.0.1:8000").rstrip("/"),
        supabase_url=_frontend_value("SUPABASE_URL", file_values) or None,
        supabase_publishable_key=publishable_key if not invalid_key else None,
        max_upload_bytes=int(_frontend_value("MAX_UPLOAD_BYTES", file_values) or 5 * 1024 * 1024),
        auth_configuration_error="The configured Supabase key is a secret key. Replace SUPABASE_PUBLISHABLE_KEY with the project's publishable key." if invalid_key else None,
        oauth_redirect_url=(_frontend_value("FRONTEND_REDIRECT_URL", file_values) or "http://localhost:8501").rstrip("/"),
    )
