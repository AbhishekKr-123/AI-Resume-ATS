from __future__ import annotations

import base64
import httpx
import pytest

from frontend.api_client import ApiClient, ApiError, DOCX_MIME, PDF_MIME
from frontend.app import _history_analysis, _validate_upload, check_backend
from frontend.auth import AuthError, SupabaseAuth, _safe_auth_message, _safe_diagnostic_message, clear_session, normalize_email
from frontend.config import is_publishable_key, load_frontend_settings
from frontend.oauth_storage import OAuthFlowStorage, consume_verifier


def response_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/resumes/analyze"
    assert request.headers["content-type"] == PDF_MIME
    assert base64.urlsafe_b64decode(request.headers["x-upload-filename-base64"]).decode("utf-8") == "resume.pdf"
    assert base64.urlsafe_b64decode(request.headers["x-job-description-base64"]).decode("utf-8") == "Python role"
    assert "x-upload-filename" not in request.headers
    assert "x-job-description" not in request.headers
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


@pytest.mark.parametrize("job_description", [
    "Build reliable systems — Python required.",
    "Développeur expérimenté en données et sécurité.",
    "分析と機械学習の経験が必要です。",
])
def test_api_client_transports_unicode_job_descriptions_as_ascii_base64(job_description):
    def handler(request: httpx.Request) -> httpx.Response:
        encoded = request.headers["x-job-description-base64"]
        assert encoded.isascii()
        assert base64.urlsafe_b64decode(encoded).decode("utf-8") == job_description
        return httpx.Response(200, json={"analysis": {}})
    client = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"))
    client.analyze(b"pdf", "résumé—2026.pdf", job_description)


def test_api_client_transports_unicode_filename_as_ascii_base64():
    def handler(request: httpx.Request) -> httpx.Response:
        encoded = request.headers["x-upload-filename-base64"]
        assert encoded.isascii()
        assert base64.urlsafe_b64decode(encoded).decode("utf-8") == "résumé—final.docx"
        return httpx.Response(200, json={"analysis": {}})
    client = ApiClient("https://api.test", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"))
    client.feedback(b"docx", "résumé—final.docx", "Python role")


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
    evidence = [{"resume_section": "projects", "similarity": 0.82}]
    analysis = _history_analysis({
        "overall_score": 71.5,
        "component_scores": {
            "semantic_similarity": {"score": 81, "weight": 35, "evidence": {"matches": evidence}, "missing_requirements": []},
            "keywords": {"score": 60, "weight": 15, "missing_requirements": ["kubernetes"]},
        },
        "matched_skills": ["Python"], "missing_skills": [], "matched_keywords": [], "missing_keywords": [],
    })
    assert analysis["overall_ats_score"] == 71.5
    assert analysis["components"]["keywords"]["weight"] == 15
    assert analysis["relevant_resume_evidence"] == evidence
    assert analysis["areas_needing_improvement"] == [{"component": "keywords", "missing_requirements": ["kubernetes"]}]


@pytest.mark.parametrize("record", [
    {},
    {"overall_score": 50, "component_scores": None},
    {"overall_score": 50, "component_scores": {"semantic_similarity": {"score": 40, "evidence": None, "missing_requirements": "invalid"}, "legacy": "invalid"}},
])
def test_history_mapping_handles_older_or_incomplete_records_without_fabrication(record):
    analysis = _history_analysis(record)
    assert isinstance(analysis["components"], dict)
    assert analysis["relevant_resume_evidence"] == []
    assert all(isinstance(area["missing_requirements"], list) for area in analysis["areas_needing_improvement"])
    assert "feedback" not in analysis


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


def test_signup_uses_publishable_key_and_normalized_email(monkeypatch):
    captured: dict[str, object] = {}
    class CapturedAuth:
        def sign_up(self, payload):
            captured["payload"] = payload
            return type("Response", (), {"session": None})()
    class CapturedClient: auth = CapturedAuth()
    def create(url, key):
        captured["url"] = url; captured["key"] = key
        return CapturedClient()
    monkeypatch.setattr("frontend.auth.create_client", create)
    auth = SupabaseAuth("https://supabase.test", "sb_publishable_safe-test-key")
    assert auth.signup("  person@example.test  ", "password") is False
    assert captured == {"url": "https://supabase.test", "key": "sb_publishable_safe-test-key", "payload": {"email": "person@example.test", "password": "password"}}


def test_secret_key_is_rejected_before_supabase_client_is_created(monkeypatch):
    monkeypatch.setattr("frontend.auth.create_client", lambda *_: pytest.fail("secret key must not create a client"))
    with pytest.raises(AuthError, match="publishable key"):
        SupabaseAuth("https://supabase.test", "sb_secret_not-allowed")
    assert is_publishable_key("sb_publishable_allowed")
    assert not is_publishable_key("sb_secret_not-allowed")


def test_frontend_settings_never_selects_secret_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setattr("frontend.config.dotenv_values", lambda _: {"SUPABASE_URL": "https://supabase.test", "SUPABASE_SECRET_KEY": "sb_secret_do-not-use"})
    settings = load_frontend_settings()
    assert settings.supabase_publishable_key is None
    assert settings.auth_configuration_error is None


def test_frontend_settings_blocks_secret_key_misconfigured_as_publishable(monkeypatch):
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setattr("frontend.config.dotenv_values", lambda _: {"SUPABASE_URL": "https://supabase.test", "SUPABASE_PUBLISHABLE_KEY": "sb_secret_misconfigured"})
    settings = load_frontend_settings()
    assert settings.supabase_publishable_key is None
    assert settings.auth_configuration_error is not None


@pytest.mark.parametrize(("email", "expected"), [
    (" person@example.test ", "person@example.test"),
    ("person @example.test", None),
    ("not-an-email", None),
])
def test_email_normalization_and_validation(email, expected):
    if expected is None:
        with pytest.raises(AuthError, match="valid email"):
            normalize_email(email)
    else:
        assert normalize_email(email) == expected


def test_signup_rejects_invalid_email_before_provider_call(monkeypatch):
    class Client:
        class auth:
            @staticmethod
            def sign_up(_): pytest.fail("invalid email must not call Supabase")
    monkeypatch.setattr("frontend.auth.create_client", lambda *_: Client())
    with pytest.raises(AuthError, match="valid email"):
        SupabaseAuth("https://supabase.test", "sb_publishable_safe").signup("invalid email", "password")


def test_google_oauth_uses_publishable_client_and_exchanges_then_verifies_user(monkeypatch):
    captured: dict[str, object] = {}
    class GoogleAuth:
        def sign_in_with_oauth(self, payload):
            captured["oauth"] = payload
            return type("OAuth", (), {"url": "https://project.supabase.co/auth/v1/authorize?provider=google"})()
        def exchange_code_for_session(self, payload):
            captured["exchange"] = payload
            return type("Response", (), {"session": type("Session", (), {"access_token": "oauth-access-token"})()})()
        def get_user(self, token):
            captured["verified_token"] = token
            return type("UserResponse", (), {"user": type("User", (), {"email": "google.user@example.test"})()})()
    class GoogleClient: auth = GoogleAuth()
    def create(url, key, options=None):
        captured["url"] = url; captured["key"] = key
        if options is not None:
            captured["oauth_storage"] = options.storage
        return GoogleClient()
    monkeypatch.setattr("frontend.auth.create_client", create)
    auth = SupabaseAuth("https://supabase.test", "sb_publishable_google-key")
    oauth_url, flow_id = auth.google_authorization_url("http://localhost:8501")
    assert oauth_url == "https://project.supabase.co/auth/v1/authorize?provider=google"
    storage = captured["oauth_storage"]
    storage.set_item("supabase-auth-code-verifier", "persisted-verifier")
    assert auth.complete_google_callback("one-time-code", flow_id) == ("oauth-access-token", "google.user@example.test")
    assert captured["oauth"] == {"provider": "google", "options": {"redirect_to": f"http://localhost:8501?oauth_flow={flow_id}"}}
    assert captured["exchange"] == {"auth_code": "one-time-code", "code_verifier": "persisted-verifier"}
    assert captured["verified_token"] == "oauth-access-token"


def test_google_oauth_callback_failure_is_safe_and_diagnostic_is_redacted(monkeypatch, caplog):
    class GoogleAuth:
        def exchange_code_for_session(self, _): raise RuntimeError("PKCE verifier missing code=one-time-code")
    class GoogleClient: auth = GoogleAuth()
    monkeypatch.setattr("frontend.auth.create_client", lambda *_: GoogleClient())
    OAuthFlowStorage("failed-flow").set_item("supabase-auth-code-verifier", "verifier")
    with pytest.raises(AuthError, match="Unable to complete Google sign-in"):
        SupabaseAuth("https://supabase.test", "sb_publishable_safe").complete_google_callback("code", "failed-flow")
    assert "RuntimeError" in caplog.text
    assert "PKCE verifier missing" in caplog.text
    assert "one-time-code" not in caplog.text


def test_google_oauth_missing_verifier_and_repeated_callback_are_rejected(monkeypatch):
    class GoogleAuth:
        def exchange_code_for_session(self, _): pytest.fail("missing or consumed verifier must stop before Supabase")
    class GoogleClient: auth = GoogleAuth()
    monkeypatch.setattr("frontend.auth.create_client", lambda *_: GoogleClient())
    auth = SupabaseAuth("https://supabase.test", "sb_publishable_safe")
    with pytest.raises(AuthError, match="expired or was already used"):
        auth.complete_google_callback("code", "missing-flow")
    OAuthFlowStorage("single-use-flow").set_item("supabase-auth-code-verifier", "verifier")
    assert consume_verifier("single-use-flow") == "verifier"
    with pytest.raises(AuthError, match="expired or was already used"):
        auth.complete_google_callback("same-code", "single-use-flow")


def test_pkce_verifier_persists_across_storage_instances_and_is_single_use():
    first_rerun = OAuthFlowStorage("rerun-flow")
    first_rerun.set_item("project-auth-code-verifier", "server-only-verifier")
    second_rerun = OAuthFlowStorage("rerun-flow")
    assert second_rerun.get_item("project-auth-code-verifier") == "server-only-verifier"
    assert consume_verifier("rerun-flow") == "server-only-verifier"
    assert second_rerun.get_item("project-auth-code-verifier") is None
    assert consume_verifier("rerun-flow") is None


def test_oauth_diagnostics_redact_codes_tokens_and_jwts():
    diagnostic = _safe_diagnostic_message(RuntimeError("exchange failed code=one-time-code access_token=secret-value eyJheader.payload.signature"))
    assert "one-time-code" not in diagnostic
    assert "secret-value" not in diagnostic
    assert "eyJheader.payload.signature" not in diagnostic
    assert "<redacted>" in diagnostic


@pytest.mark.parametrize(("provider_message", "safe_message"), [
    ("Invalid login credentials", "email or password is incorrect"),
    ("Email not confirmed", "Confirm your email"),
    ("User already registered", "account already exists"),
    ("Password should contain at least 8 characters", "stronger password"),
    ("network timeout", "temporarily unavailable"),
    ("email_address_invalid", "valid email"),
    ("over_email_send_rate_limit", "Too many confirmation"),
])
def test_auth_provider_errors_are_classified_without_provider_detail(provider_message, safe_message):
    assert safe_message.casefold() in _safe_auth_message(RuntimeError(provider_message), "sign in").casefold()
