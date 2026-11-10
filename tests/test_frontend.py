from __future__ import annotations

import httpx
import pytest

from frontend.api_client import ApiClient, ApiError, DOCX_MIME, PDF_MIME
from frontend.app import _history_analysis, _validate_upload, check_backend
from frontend.auth import AuthError, SupabaseAuth, _safe_auth_message, clear_session


def response_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/resumes/analyze"
    assert request.headers["content-type"] == PDF_MIME
    assert request.headers["x-upload-filename"] == "resume.pdf"
    assert request.headers["x-job-description"] == "Python role"
    assert request.headers["authorization"] == "Bearer access-token"
    assert request.content == b"raw-pdf-bytes"
    return httpx.Response(200, json={"filename": "resume.pdf", "analysis": {"overall_ats_score": 81}})


def test_api_client_sends_raw_pdf_with_required_headers():
    client = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(response_handler), base_url="https://api.test"))
    result = client.analyze(b"raw-pdf-bytes", "resume.pdf", "Python role", "access-token")
    assert result["analysis"]["overall_ats_score"] == 81


def test_api_client_sends_raw_docx_and_rejects_unsupported_file():
    received: dict[str, str] = {}
    def handler(request: httpx.Request) -> httpx.Response:
        received["type"] = request.headers["content-type"]
        return httpx.Response(200, json={})
    client = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"))
    client.feedback(b"docx", "resume.docx", "Role")
    assert received["type"] == DOCX_MIME
    with pytest.raises(ApiError, match="Only PDF"):
        client.analyze(b"x", "resume.txt", "Role")


def test_api_client_exposes_safe_backend_and_network_errors():
    error_client = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"error": {"code": "groq_rate_limited", "message": "Try later"}})), base_url="https://api.test"))
    with pytest.raises(ApiError) as error:
        error_client.feedback(b"pdf", "resume.pdf", "Role")
    assert error.value.code == "groq_rate_limited"
    failing = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(httpx.ConnectError("offline"))), base_url="https://api.test"))
    with pytest.raises(ApiError, match="backend is unavailable"):
        failing.history("token")


class Upload:
    def __init__(self, name: str, size: int) -> None: self.name, self.size = name, size


def health_handler(paths: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/health/live":
            return httpx.Response(200, json={"status": "alive"})
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ready", "model": "local-finetuned", "embedding_dimension": 768})
        return httpx.Response(404, json={})
    return handler


def test_health_checks_use_existing_endpoints_only():
    paths: list[str] = []
    status = check_backend(ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(health_handler(paths)), base_url="https://api.test")))
    assert status["live"] is True
    assert status["ready"] is True
    assert status["model"] == "local-finetuned"
    assert set(paths) == {"/health/live", "/health/ready"}
    assert "/health" not in paths


def test_backend_status_reports_outage_and_partial_readiness():
    offline_plans = iter([httpx.ConnectError("offline")])
    offline = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(next(offline_plans))), base_url="https://api.test"))
    assert check_backend(offline) == {"live": False, "ready": False, "model": None}
    not_ready = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503, json={})), base_url="https://api.test"))
    assert check_backend(not_ready) == {"live": False, "ready": False, "model": None}
    partial_paths: list[str] = []
    def partial_handler(request: httpx.Request) -> httpx.Response:
        partial_paths.append(request.url.path)
        return httpx.Response(200, json={"status": "alive"}) if request.url.path == "/health/live" else httpx.Response(503, json={"status": "not_ready"})
    partial = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(partial_handler), base_url="https://api.test"))
    assert check_backend(partial) == {"live": True, "ready": False, "model": None}
    assert set(partial_paths) == {"/health/live", "/health/ready"}


@pytest.mark.parametrize(("upload", "description", "expected"), [
    (None, "Role", "valid PDF or DOCX"),
    (Upload("resume.txt", 10), "Role", "valid PDF or DOCX"),
    (Upload("resume.pdf", 11), "Role", "exceeds"),
    (Upload("resume.pdf", 10), "   ", "Enter a job"),
    (Upload("resume.docx", 10), "Role", None),
])
def test_upload_validation(upload, description, expected):
    result = _validate_upload(upload, description, 10)
    assert (expected in result) if expected else result is None


def test_history_mapping_uses_only_persisted_response_fields():
    analysis = _history_analysis({"overall_score": 71.5, "component_scores": {"keywords": {"score": 60, "weight": 15}}, "matched_skills": ["Python"], "missing_skills": [], "matched_keywords": [], "missing_keywords": []})
    assert analysis["overall_ats_score"] == 71.5
    assert analysis["components"]["keywords"]["weight"] == 15
    assert analysis["relevant_resume_evidence"] == []


def test_clear_session_removes_token_and_transient_resume_state():
    session = {"access_token": "secret", "auth_email": "a@example.test", "latest_result": {}, "latest_upload": b"x", "other": True}
    clear_session(session)
    assert session == {"other": True}


def test_auth_wraps_provider_errors_without_leaking_details(monkeypatch):
    class BrokenAuth:
        def sign_in_with_password(self, _): raise RuntimeError("provider secret detail")
        def sign_up(self, _): raise RuntimeError("provider secret detail")
        def sign_out(self): raise RuntimeError("provider secret detail")
    class BrokenClient: auth = BrokenAuth()
    monkeypatch.setattr("frontend.auth.create_client", lambda *_: BrokenClient())
    auth = SupabaseAuth("https://supabase.test", "publishable-key")
    with pytest.raises(AuthError, match="Unable to sign in"):
        auth.login("a@example.test", "password")
    with pytest.raises(AuthError, match="Unable to create"):
        auth.signup("a@example.test", "password")
    auth.logout()


@pytest.mark.parametrize(("provider_message", "safe_message"), [
    ("Invalid login credentials", "email or password is incorrect"),
    ("Email not confirmed", "Confirm your email"),
    ("User already registered", "account already exists"),
    ("Password should contain at least 8 characters", "stronger password"),
    ("network timeout", "temporarily unavailable"),
])
def test_auth_provider_errors_are_classified_without_provider_detail(provider_message, safe_message):
    assert safe_message.casefold() in _safe_auth_message(RuntimeError(provider_message), "sign in").casefold()
