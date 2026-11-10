"""Composition of deterministic ATS scoring components."""

from __future__ import annotations

from ..model_service import LocalModelService
from .experience_scorer import score_experience
from .formatting_scorer import score_formatting
from .keyword_scorer import score_keywords
from .semantic_scorer import score_semantic
from .skills_scorer import score_skills

METHODOLOGY_VERSION = "deterministic-ats-v1"


class ATSScorer:
    def __init__(self, model_service: LocalModelService, weights: dict[str, float]) -> None:
        self._model_service = model_service
        self._weights = weights

    def analyze(self, resume_text: str, resume_sections: dict[str, str], job_description: str) -> dict:
        components = {
            "semantic_similarity": score_semantic(self._model_service, resume_sections, resume_text, job_description, self._weights["semantic_similarity"]),
            "technical_skills": score_skills(resume_text, resume_sections, job_description, self._weights["technical_skills"]),
            "keywords": score_keywords(resume_text, job_description, self._weights["keywords"]),
            "experience_relevance": score_experience(resume_sections, job_description, self._weights["experience_relevance"]),
            "formatting_completeness": score_formatting(resume_text, resume_sections, job_description, self._weights["formatting_completeness"]),
        }
        overall = sum(item["score"] * item["weight"] / 100 for item in components.values())
        skills_evidence = components["technical_skills"]["evidence"]
        keyword_evidence = components["keywords"]["evidence"]
        areas = []
        for name, item in components.items():
            if item["score"] < 70:
                areas.append({"component": name, "missing_requirements": item["missing_requirements"]})
        return {
            "overall_ats_score": round(overall, 2),
            "maximum_score": 100.0,
            "methodology_version": METHODOLOGY_VERSION,
            "disclaimer": "This deterministic ATS indicator is not a hiring probability or a guarantee of selection.",
            "components": components,
            "matched_skills": skills_evidence["matched_skills"],
            "missing_skills": components["technical_skills"]["missing_requirements"],
            "matched_keywords": keyword_evidence["matched_keywords"],
            "missing_keywords": components["keywords"]["missing_requirements"],
            "relevant_resume_evidence": components["semantic_similarity"]["evidence"]["matches"],
            "areas_needing_improvement": areas,
        }
