from pathlib import Path

import pytest

from backend.app.config import ConfigurationError, load_settings


def test_settings_resolve_the_required_model_from_project_root(settings):
    assert settings.model_relative_path == Path("models/finetuned-bert")
    assert settings.model_path == settings.project_root / "models" / "finetuned-bert"
    assert settings.model_path.is_dir()


def test_settings_reject_missing_local_model(tmp_path):
    with pytest.raises(ConfigurationError, match="models/finetuned-bert"):
        load_settings(tmp_path)


def test_settings_reject_inconsistent_docx_archive_limits(monkeypatch, settings):
    monkeypatch.setenv("MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES", "10")
    monkeypatch.setenv("MAX_DOCX_ENTRY_UNCOMPRESSED_BYTES", "11")
    with pytest.raises(ConfigurationError, match="cannot exceed"):
        load_settings(settings.project_root)


def test_settings_loads_the_pdf_decoded_stream_limit(settings):
    assert settings.max_pdf_decoded_stream_bytes == 10 * 1024 * 1024


def test_production_auth_configuration_requires_secure_same_origin_cookies(monkeypatch, settings):
    monkeypatch.setenv("AUTH_PUBLIC_URL", "https://ats.example.test")
    monkeypatch.setenv("AUTH_ALLOWED_REDIRECT_URLS", "https://ats.example.test")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    with pytest.raises(ConfigurationError, match="AUTH_COOKIE_SECURE"):
        load_settings(settings.project_root)

    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("AUTH_ALLOWED_REDIRECT_URLS", "https://frontend.example.test")
    with pytest.raises(ConfigurationError, match="same origin"):
        load_settings(settings.project_root)


def test_local_auth_configuration_keeps_exact_redirect_allowlist(monkeypatch, settings):
    monkeypatch.setenv("AUTH_PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("AUTH_ALLOWED_REDIRECT_URLS", "http://localhost:8501,http://localhost:8501/approved")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    configured = load_settings(settings.project_root)
    assert configured.auth_allowed_redirect_urls == ("http://localhost:8501", "http://localhost:8501/approved")
