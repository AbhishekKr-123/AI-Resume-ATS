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
