"""Section-aware local-model semantic relevance scoring."""

from __future__ import annotations

import re

import numpy as np

from ..model_service import LocalModelService
from .common import component

MAX_MODEL_TOKENS = 384


def _chunks(text: str, model: LocalModelService) -> list[tuple[str, int]]:
    """Build sentence-aware, non-duplicated chunks within the model token limit."""
    raw_sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", text) if item.strip()]
    sentences = list(dict.fromkeys(re.sub(r"\s+", " ", item).strip() for item in raw_sentences))
    chunks: list[tuple[str, int]] = []
    current: list[str] = []
    for sentence in sentences:
        if model.token_count(sentence) > MAX_MODEL_TOKENS:
            if current:
                value = " ".join(current)
                chunks.append((value, model.token_count(value)))
                current = []
            chunks.extend((part, model.token_count(part)) for part in model.split_by_tokens(sentence, MAX_MODEL_TOKENS))
            continue
        candidate = " ".join(current + [sentence])
        if current and model.token_count(candidate) > MAX_MODEL_TOKENS:
            value = " ".join(current)
            chunks.append((value, model.token_count(value)))
            current = [sentence]
        else:
            current.append(sentence)
    if current:
        value = " ".join(current)
        chunks.append((value, model.token_count(value)))
    unique: dict[str, tuple[str, int]] = {}
    for text_value, token_count in chunks:
        unique.setdefault(re.sub(r"\s+", " ", text_value.lower()).strip(), (text_value, token_count))
    return list(unique.values())


def score_semantic(
    model: LocalModelService,
    resume_sections: dict[str, str],
    resume_text: str,
    job_description: str,
    weight: float,
) -> dict:
    resume_parts = resume_sections or {"other": resume_text}
    jd_chunks = _chunks(job_description, model)
    resume_chunks: list[tuple[str, str, int]] = []
    for section, section_text in resume_parts.items():
        for chunk, token_count in _chunks(section_text, model):
            resume_chunks.append((section, chunk, token_count))
    if not resume_chunks or not jd_chunks:
        return component(0, weight, "No usable text was available for local semantic comparison.", {"matches": []})

    resume_embeddings = model.encode([chunk for _, chunk, _ in resume_chunks])
    jd_embeddings = model.encode([chunk for chunk, _ in jd_chunks])
    resume_embeddings /= np.linalg.norm(resume_embeddings, axis=1, keepdims=True)
    jd_embeddings /= np.linalg.norm(jd_embeddings, axis=1, keepdims=True)
    similarities = jd_embeddings @ resume_embeddings.T
    best_indices = similarities.argmax(axis=1)
    best_scores = similarities.max(axis=1)
    weights = np.array([token_count for _, token_count in jd_chunks], dtype=float)
    coverage = float(np.average(np.clip(best_scores, 0, 1), weights=weights))
    matches = []
    for jd_index in np.argsort(best_scores)[::-1][:5]:
        resume_index = int(best_indices[jd_index])
        section, _, _ = resume_chunks[resume_index]
        matches.append({"resume_section": section, "similarity": round(float(best_scores[jd_index]), 4)})
    return component(
        coverage * 100,
        weight,
        "Each unique job-description chunk is matched to its best unique resume chunk; the token-weighted mean of those best matches measures JD coverage.",
        {"resume_chunk_count": len(resume_chunks), "job_description_chunk_count": len(jd_chunks), "matches": matches},
    )
