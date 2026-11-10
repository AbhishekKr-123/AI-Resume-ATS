"""Non-secret frontend configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class FrontendSettings:
    api_base_url: str
    supabase_url: str | None
    supabase_publishable_key: str | None
    max_upload_bytes: int


def load_frontend_settings() -> FrontendSettings:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    return FrontendSettings(
        api_base_url=os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        supabase_url=os.getenv("SUPABASE_URL") or None,
        supabase_publishable_key=os.getenv("SUPABASE_PUBLISHABLE_KEY") or None,
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))),
    )
