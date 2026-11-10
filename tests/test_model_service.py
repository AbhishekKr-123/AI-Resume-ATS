import numpy as np
import pytest

from backend.app.model_service import ModelLoadError, validate_embedding


def test_local_model_loads_once_and_produces_valid_embeddings(loaded_model_service):
    assert loaded_model_service.is_ready
    assert loaded_model_service.embedding_dimension == 768

    loaded_model_service.load()
    assert loaded_model_service.embedding_dimension == 768


def test_embedding_validation_rejects_empty_or_non_finite_values():
    with pytest.raises(ModelLoadError, match="empty"):
        validate_embedding(np.empty((0, 768)), 768)

    with pytest.raises(ModelLoadError, match="non-finite"):
        validate_embedding(np.array([[float("nan")]]), 1)
