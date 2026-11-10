from pathlib import Path

import pytest

from backend.app.config import load_settings
from backend.app.model_service import LocalModelService


@pytest.fixture(scope="session")
def settings():
    return load_settings(Path(__file__).resolve().parents[1])


@pytest.fixture(scope="session")
def loaded_model_service(settings):
    service = LocalModelService(settings.model_path)
    service.load()
    return service
