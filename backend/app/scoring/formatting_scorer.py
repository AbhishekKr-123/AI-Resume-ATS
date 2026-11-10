"""Fair resume completeness checks based on extracted sections."""

from __future__ import annotations

from .common import component


def score_formatting(resume_text: str, sections: dict[str, str], job_description: str, weight: float) -> dict:
    certification_relevant = "certif" in job_description.lower() or "license" in job_description.lower()
    checks = {
        "contact_information": 15 if sections.get("contact_information") else 0,
        "summary": 15 if sections.get("summary") else 0,
        "education": 15 if sections.get("education") else 0,
        "skills": 20 if sections.get("skills") else 0,
        "experience_or_projects": 25 if sections.get("work_experience") or sections.get("projects") else 0,
        "readable_text": 10 if len(resume_text.strip()) >= 80 else 0,
    }
    if certification_relevant:
        checks["experience_or_projects"] = 20 if checks["experience_or_projects"] else 0
        checks["certifications"] = 5 if sections.get("certifications") else 0
    return component(
        sum(checks.values()), weight,
        "Completeness checks reward clear sections and readable text. Projects satisfy the experience/project check, so a fresher is not penalized solely for no employment history.",
        {"checks": checks, "recognized_sections": sorted(sections)},
        [name for name, points in checks.items() if not points],
    )
