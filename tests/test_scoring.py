from dataclasses import replace
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from backend.app.config import ConfigurationError, load_settings
from backend.app.main import create_app
from backend.app.model_service import LocalModelService
from backend.app.scoring.ats_scorer import ATSScorer
from backend.app.scoring.semantic_scorer import score_semantic
from backend.app.text_processing import detect_resume_sections


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_docx(text: str) -> bytes:
    document = Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def resume_text(skills: str = "Python SQL AWS Docker") -> str:
    return f"""Synthetic Candidate
candidate@example.test | +1 555 010 2000
Summary
Python data engineer building reliable platforms.
Skills
{skills}
Work Experience
4 years Python SQL AWS data pipelines and REST API delivery.
Education
Synthetic University Computer Science
Projects
Built Docker data analytics API project.
"""


def analyze(service, text: str, jd: str) -> dict:
    sections = detect_resume_sections(text)
    return ATSScorer(service, {"semantic_similarity": 35, "technical_skills": 25, "keywords": 15, "experience_relevance": 15, "formatting_completeness": 10}).analyze(text, sections, jd)


def test_perfect_partial_and_poor_matches(loaded_model_service):
    jd = "Python SQL AWS required. Docker preferred. 3 years experience building data pipelines and REST API."
    perfect = analyze(loaded_model_service, resume_text(), jd)
    partial_resume = resume_text("Python SQL").replace("AWS", "cloud platform").replace("Docker", "deployment tooling")
    partial = analyze(loaded_model_service, partial_resume, jd)
    poor = analyze(loaded_model_service, "Summary\nCreative writer and illustrator.\nEducation\nArts degree.", jd)
    assert perfect["overall_ats_score"] > partial["overall_ats_score"] > poor["overall_ats_score"]
    assert "aws" in partial["missing_skills"]
    assert "docker" in perfect["matched_skills"]


def test_component_evidence_and_weighted_overall_score(loaded_model_service):
    result = analyze(loaded_model_service, resume_text(), "Python SQL AWS required. Docker preferred.")
    assert set(result["components"]) == {"semantic_similarity", "technical_skills", "keywords", "experience_relevance", "formatting_completeness"}
    for item in result["components"].values():
        assert set(item) == {"score", "maximum_score", "weight", "explanation", "evidence", "missing_requirements"}
    expected = sum(item["score"] * item["weight"] / 100 for item in result["components"].values())
    assert result["overall_ats_score"] == round(expected, 2)
    assert result["disclaimer"].startswith("This deterministic ATS indicator")


def test_fresher_projects_are_relevant_without_work_penalty(loaded_model_service):
    fresher = """Synthetic Candidate
candidate@example.test
Summary
Entry-level data engineer.
Skills
Python SQL Docker
Education
Synthetic University
Projects
Built Python SQL Docker data pipeline API.
"""
    result = analyze(loaded_model_service, fresher, "Python SQL Docker required. Build data pipelines.")
    assert result["components"]["formatting_completeness"]["evidence"]["checks"]["experience_or_projects"] > 0
    assert result["components"]["experience_relevance"]["evidence"]["has_work_experience"] is False
    assert result["components"]["experience_relevance"]["evidence"]["has_projects"] is True


def test_long_and_duplicate_text_do_not_inflate_semantic_score(loaded_model_service):
    jd = "Python SQL AWS data pipeline engineering."
    section = "Python SQL AWS data pipeline engineering delivered reliable systems. "
    single = score_semantic(loaded_model_service, {"projects": section}, section, jd, 35)
    duplicate = score_semantic(loaded_model_service, {"projects": section * 200}, section * 200, jd, 35)
    assert duplicate["evidence"]["resume_chunk_count"] == 1
    assert duplicate["score"] == single["score"]


def test_invalid_scoring_configuration_is_rejected(monkeypatch, settings):
    monkeypatch.setenv("SEMANTIC_WEIGHT", "34")
    with pytest.raises(ConfigurationError, match="sum to 100"):
        load_settings(settings.project_root)


def test_analyze_api_and_empty_job_description(settings, loaded_model_service):
    app = create_app(settings=settings, model_service=loaded_model_service)
    with TestClient(app) as client:
        response = client.post(
            "/resumes/analyze", content=make_docx(resume_text()),
            headers={"content-type": DOCX_MIME, "x-upload-filename": "synthetic.docx", "x-job-description": "Python SQL AWS required. Docker preferred."},
        )
        blank = client.post(
            "/resumes/analyze", content=make_docx(resume_text()),
            headers={"content-type": DOCX_MIME, "x-upload-filename": "synthetic.docx", "x-job-description": "  "},
        )
    assert response.status_code == 200
    assert response.json()["analysis"]["overall_ats_score"] <= 100
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "unreadable_text"

    app = create_app(settings=replace(settings, max_job_description_chars=4), model_service=loaded_model_service)
    with TestClient(app) as limited_client:
        too_large = limited_client.post(
            "/resumes/analyze", content=make_docx(resume_text()),
            headers={"content-type": DOCX_MIME, "x-upload-filename": "synthetic.docx", "x-job-description": "Python required"},
        )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "job_description_too_large"


def test_model_unavailable_returns_structured_error(settings, loaded_model_service):
    app = create_app(settings=settings, model_service=loaded_model_service)
    with TestClient(app) as client:
        app.state.model_service = LocalModelService(settings.model_path)
        response = client.post(
            "/resumes/analyze", content=make_docx(resume_text()),
            headers={"content-type": DOCX_MIME, "x-upload-filename": "synthetic.docx", "x-job-description": "Python required."},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_not_ready"
