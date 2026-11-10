"""Dictionary-backed deterministic technical-skill matching."""

from __future__ import annotations

import re

from .common import component, contains_term

SKILL_ALIASES = {
    "python": {"python"}, "sql": {"sql", "postgresql", "mysql"}, "java": {"java"},
    "javascript": {"javascript", "js", "ecmascript"}, "typescript": {"typescript", "ts"},
    "react": {"react", "reactjs", "react.js"}, "node.js": {"node", "nodejs", "node.js"},
    "aws": {"aws", "amazon web services"}, "azure": {"azure", "microsoft azure"},
    "docker": {"docker", "containers"}, "kubernetes": {"kubernetes", "k8s"},
    "git": {"git", "github", "gitlab"}, "linux": {"linux"}, "tensorflow": {"tensorflow", "tf"},
    "pytorch": {"pytorch", "torch"}, "machine learning": {"machine learning", "ml"},
    "data analysis": {"data analysis", "data analytics"}, "pandas": {"pandas"},
    "numpy": {"numpy"}, "tableau": {"tableau"}, "power bi": {"power bi", "powerbi"},
    "ci/cd": {"ci/cd", "continuous integration", "continuous delivery"}, "terraform": {"terraform"},
    "rest api": {"rest api", "restful api", "restful services"}, "agile": {"agile", "scrum"},
}


def _found_skills(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for canonical, aliases in SKILL_ALIASES.items():
        for alias in aliases:
            if contains_term(text, alias):
                found[canonical] = alias
                break
    return found


def _requirements(job_description: str) -> tuple[list[str], list[str]]:
    required: set[str] = set()
    preferred: set[str] = set()
    lowered = job_description.lower()
    for canonical, aliases in SKILL_ALIASES.items():
        positions = [match.start() for alias in aliases for match in re.finditer(r"(?<![a-z0-9+#])" + re.escape(alias.lower()) + r"(?![a-z0-9+#])", lowered)]
        if not positions:
            continue
        is_preferred = all(any(marker in lowered[max(0, position - 45) : position + 20] for marker in ("preferred", "nice to have", "bonus", "plus")) for position in positions)
        (preferred if is_preferred else required).add(canonical)
    return sorted(required), sorted(preferred - required)


def score_skills(resume_text: str, resume_sections: dict[str, str], job_description: str, weight: float) -> dict:
    required, preferred = _requirements(job_description)
    resume_skills = _found_skills(resume_text)
    matched_required = [skill for skill in required if skill in resume_skills]
    matched_preferred = [skill for skill in preferred if skill in resume_skills]
    required_coverage = len(matched_required) / len(required) if required else 1.0
    preferred_coverage = len(matched_preferred) / len(preferred) if preferred else 1.0
    score = (required_coverage * 80 + preferred_coverage * 20) if required or preferred else 100.0
    locations = {}
    for skill in matched_required + matched_preferred:
        locations[skill] = [name for name, text in resume_sections.items() if contains_term(text, resume_skills[skill])]
    return component(
        score, weight,
        "Required skills contribute 80% and preferred skills contribute 20%; absent requirement groups receive full coverage.",
        {"required_skills": required, "preferred_skills": preferred, "matched_skills": sorted(set(matched_required + matched_preferred)), "skill_locations": locations},
        [skill for skill in required if skill not in resume_skills] + [skill for skill in preferred if skill not in resume_skills],
    )
