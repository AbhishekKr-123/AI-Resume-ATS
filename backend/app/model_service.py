"""Local Sentence Transformer lifecycle and embedding verification."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer


class ModelLoadError(RuntimeError):
    """Raised when the approved local model cannot be loaded or verified."""


def validate_embedding(embedding: np.ndarray, expected_dimension: int) -> None:
    """Ensure a model embedding is populated, finite, and correctly shaped."""
    if embedding.ndim != 2 or embedding.shape[0] == 0:
        raise ModelLoadError("Local model produced an empty embedding batch.")
    if embedding.shape[1] != expected_dimension or expected_dimension <= 0:
        raise ModelLoadError("Local model produced an embedding with an unexpected dimension.")
    if not np.isfinite(embedding).all():
        raise ModelLoadError("Local model produced non-finite embedding values.")


class LocalModelService:
    """Owns the one local model instance shared by all API requests."""

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path
        self._model: SentenceTransformer | None = None
        self._embedding_dimension: int | None = None

    @property
    def is_ready(self) -> bool:
        return self._model is not None and self._embedding_dimension is not None

    @property
    def embedding_dimension(self) -> int:
        if self._embedding_dimension is None:
            raise ModelLoadError("Local fine-tuned model has not been loaded.")
        return self._embedding_dimension

    def encode(self, texts: list[str]) -> np.ndarray:
        """Embed text with the single verified local model instance."""
        if self._model is None:
            raise ModelLoadError("Local fine-tuned model has not been loaded.")
        return np.asarray(
            self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        )

    def token_count(self, text: str) -> int:
        """Count model tokens without loading or substituting another tokenizer."""
        if self._model is None:
            raise ModelLoadError("Local fine-tuned model has not been loaded.")
        return len(self._model.tokenizer(text, add_special_tokens=False, verbose=False)["input_ids"])

    def split_by_tokens(self, text: str, max_tokens: int) -> list[str]:
        """Split text using the loaded local tokenizer's vocabulary."""
        if self._model is None:
            raise ModelLoadError("Local fine-tuned model has not been loaded.")
        token_ids = self._model.tokenizer.encode(text, add_special_tokens=False)
        return [
            self._model.tokenizer.decode(token_ids[index : index + max_tokens], skip_special_tokens=True).strip()
            for index in range(0, len(token_ids), max_tokens)
            if token_ids[index : index + max_tokens]
        ]

    def load(self) -> None:
        """Load and verify only the approved local fine-tuned model."""
        if self.is_ready:
            return

        try:
            model = SentenceTransformer(str(self._model_path), local_files_only=True)
            expected_dimension = model.get_embedding_dimension()
            if expected_dimension is None:
                raise ModelLoadError("Local model did not report an embedding dimension.")

            embedding = np.asarray(
                model.encode(
                    ["Local model startup verification."],
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            )
            validate_embedding(embedding, int(expected_dimension))
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(
                "Unable to load the required local fine-tuned model from "
                "models/finetuned-bert/."
            ) from exc

        self._model = model
        self._embedding_dimension = int(expected_dimension)
