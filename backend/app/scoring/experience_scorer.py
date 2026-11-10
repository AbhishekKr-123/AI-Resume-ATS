"""Transparent experience and project relevance heuristic."""

from __future__ import annotations

import re

from .common import component, contains_term, unique_terms
from .skills_scorer import SKILL_ALIASES


def _years(text: str) -> int | None:
    matches = re.findall(r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", text.lower())
    return max(map(int, matches)) if matches else None


def score_experience(resume_sections: dict[str, str], job_description: str, weight: float) -> dict:
    work = resume_sections.get("work_experience", "")
    projects = resume_sections.get("projects", "")
    evidence_text = "\n".join(part for part in (work, projects) if part)
    jd_terms = list(dict.fromkeys(unique_terms(job_description)[:20] + [skill for skill, aliases in SKILL_ALIASES.items() if any(contains_term(job_description, alias) for alias in aliases)]))
    matched = [term for term in jd_terms if contains_term(evidence_text, term)]
    relevance = len(matched) / len(jd_terms) if jd_terms else 1.0
    required_years = _years(job_description)
    stated_years = _years(work)
    if required_years is not None:
        duration = min(stated_years / required_years, 1.0) if stated_years is not None else 0.0
        score = 100 * (0.7 * relevance + 0.3 * duration)
    else:
        score = 100 * relevance
    if not work and projects:
        score = max(score, 100 * relevance)
    return component(
        score, weight,
        "Relevance is based on unique JD terms in work experience or projects. Duration is used only when explicit years are present in both the JD and work evidence.",
        {"matched_relevance_terms": matched, "has_work_experience": bool(work), "has_projects": bool(projects), "required_years": required_years, "explicit_resume_years": stated_years},
        [term for term in jd_terms if term not in matched],
    )
