"""Local-only section detection and job-description text normalization."""

from __future__ import annotations

import re


SECTION_ALIASES = {
    "contact_information": {"contact", "contact information", "personal information"},
    "summary": {"summary", "professional summary", "profile", "objective"},
    "education": {"education", "academic background", "qualifications"},
    "work_experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment history",
    },
    "skills": {"skills", "technical skills", "core competencies", "competencies"},
    "projects": {"projects", "project experience", "selected projects"},
    "certifications": {"certifications", "certificates", "licenses and certifications"},
}

_SPACE_RE = re.compile(r"[ \t]+")
_EMAIL_OR_PHONE_RE = re.compile(
    r"(?:\b[\w.+-]+@[\w-]+\.[\w.-]+\b)|(?:\+?\d[\d(). -]{6,}\d)"
)


def _normalize_heading(line: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", line.lower()).strip()


def _section_name(line: str) -> str | None:
    normalized = _normalize_heading(line)
    for name, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return name
    return None


def detect_resume_sections(text: str) -> dict[str, str]:
    """Return recognized resume sections without persisting or logging text."""
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    preamble: list[str] = []

    for raw_line in text.splitlines():
        line = _SPACE_RE.sub(" ", raw_line).strip()
        if not line:
            continue
        section = _section_name(line)
        if section:
            current_section = section
            sections.setdefault(section, [])
            continue
        if current_section:
            sections[current_section].append(line)
        else:
            preamble.append(line)

    if preamble and any(_EMAIL_OR_PHONE_RE.search(line) for line in preamble):
        sections.setdefault("contact_information", preamble)

    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def preprocess_job_description(text: str) -> str:
    """Normalize job-description text locally for a later scoring phase."""
    normalized_lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    normalized = "\n".join(line for line in normalized_lines if line)
    if not normalized:
        raise ValueError("Job-description text must contain readable characters.")
    return normalized
