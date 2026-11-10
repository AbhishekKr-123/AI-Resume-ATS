"""Important-term coverage without repetition inflation."""

from __future__ import annotations

from .common import component, contains_term, unique_terms
from .skills_scorer import SKILL_ALIASES


def score_keywords(resume_text: str, job_description: str, weight: float) -> dict:
    terms = unique_terms(job_description)
    skill_phrases = [skill for skill, aliases in SKILL_ALIASES.items() if any(contains_term(job_description, alias) for alias in aliases)]
    weighted = {term: 1 for term in terms[:30]}
    for phrase in skill_phrases:
        weighted[phrase] = 3
    matched = [term for term in weighted if contains_term(resume_text, term)]
    total_weight = sum(weighted.values())
    score = 100 * sum(weighted[term] for term in matched) / total_weight if total_weight else 100.0
    return component(
        score, weight,
        "Unique JD terms are counted once; recognized technical skill phrases receive triple weight.",
        {"matched_keywords": matched, "keyword_weights": weighted},
        [term for term in weighted if term not in matched],
    )
