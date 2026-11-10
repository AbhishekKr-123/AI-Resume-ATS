"""Shared deterministic text and result helpers for ATS components."""

from __future__ import annotations

import re

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "our", "the", "to", "with", "will", "you", "your",
    "we", "this", "that", "have", "has", "years", "year", "experience", "required",
    "preferred", "candidate", "role", "team", "work", "working", "ability", "strong",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#./ -]", " ", text.lower())).strip()


def contains_term(text: str, term: str) -> bool:
    normalized_text = normalize(text)
    normalized_term = normalize(term)
    return bool(re.search(r"(?<![a-z0-9+#])" + re.escape(normalized_term) + r"(?![a-z0-9+#])", normalized_text))


def component(score: float, weight: float, explanation: str, evidence: dict, missing: list[str] | None = None) -> dict:
    return {
        "score": round(max(0.0, min(100.0, score)), 2),
        "maximum_score": 100.0,
        "weight": weight,
        "explanation": explanation,
        "evidence": evidence,
        "missing_requirements": missing or [],
    }


def unique_terms(text: str) -> list[str]:
    return [word for word in dict.fromkeys(re.findall(r"[a-z][a-z0-9+#.-]{2,}", normalize(text))) if word not in STOP_WORDS]
