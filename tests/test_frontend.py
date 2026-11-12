from __future__ import annotations

import base64
from datetime import datetime
import httpx
import pytest
from streamlit.testing.v1 import AppTest

from frontend.api_client import ApiClient, ApiError, DOCX_MIME, PDF_MIME
from frontend.app import LOGO_PATH, PAGES, STYLESHEET_PATH, _asset_data_uri, _browser_logout_html, _dashboard_history, _dashboard_metrics, _display_name, _history_analysis, _latest_breakdown, _oauth_link_html, _render_password_reset, _score_chart_data, _upload_limit_mib, _validate_upload, check_backend
from frontend.auth import AuthError, SupabaseAuth, _safe_auth_message, _safe_diagnostic_message, clear_session, normalize_email
from frontend.config import FrontendSettings, is_publishable_key, load_frontend_settings
from frontend.oauth_storage import OAuthFlowStorage, consume_verifier


def reset_form_app() -> None:
    import streamlit as st
    from frontend.app import _render_password_reset

    class ResetAuthStub:
        def reset_password(self, email: str) -> None:
            calls = st.session_state.get("reset_calls", [])
            st.session_state.reset_calls = [*calls, email]

    _render_password_reset(ResetAuthStub())


def navigation_query_app() -> None:
    import streamlit as st
    from frontend.app import PAGES, _page_from_query, _set_page

    current = _page_from_query()
    page = st.radio("Navigation", PAGES, index=PAGES.index(current))
    if page != current:
        _set_page(page)
    st.write(f"Selected: {page}")


def dashboard_test_app(populated: bool = True, display_name: str | None = None) -> None:
    import streamlit as st
    from frontend.app import render_dashboard

    class Client:
        def history(self, _token, limit=100, offset=0):
            assert limit == 100
            assert offset == 0
            if not populated:
                return {"analyses": []}
            return {"analyses": [
                {"id": "new", "overall_score": 82.0, "filename": "latest.pdf", "created_at": "2026-11-10T10:00:00Z", "component_scores": {"semantic_similarity": {"score": 84}, "technical_skills": {"score": 80}, "keywords": {"score": 76}, "experience_relevance": {"score": 88}, "formatting_completeness": {"score": 90}}},
                {"id": "old", "overall_score": 68.0, "filename": "older.pdf", "created_at": "2026-11-01T10:00:00Z", "component_scores": {}},
            ]}

        def get_analysis(self, analysis_id, _token):
            return {"id": analysis_id}

    st.session_state.browser_authenticated = True
    st.session_state.auth_email = "person@example.test"
    if display_name:
        st.session_state.auth_user_metadata = {"full_name": display_name}
    render_dashboard(Client())


def analyze_upload_test_app() -> None:
    from frontend.app import render_analyze
    from frontend.config import FrontendSettings

    settings = FrontendSettings("https://api.test", None, None, 5 * 1024 * 1024)
    render_analyze(settings, object())


def analyze_workflow_test_app(mode: str = "success", busy: bool = False) -> None:
    import streamlit as st
    from frontend.api_client import ApiError
    from frontend.app import render_analyze
    from frontend.config import FrontendSettings

    analysis = {
        "overall_ats_score": 78.5,
        "disclaimer": "This deterministic ATS indicator is not a hiring probability or a guarantee of selection.",
        "components": {
            "semantic_similarity": {"score": 80, "weight": 35, "explanation": "Semantic explanation"},
            "technical_skills": {"score": 75, "weight": 25, "explanation": "Skills explanation"},
            "keywords": {"score": 70, "weight": 15, "explanation": "Keywords explanation"},
            "experience_relevance": {"score": 85, "weight": 15, "explanation": "Experience explanation"},
            "formatting_completeness": {"score": 90, "weight": 10, "explanation": "Formatting explanation"},
        },
        "matched_skills": ["Python"], "missing_skills": ["Kubernetes"],
        "matched_keywords": ["APIs"], "missing_keywords": ["observability"],
        "relevant_resume_evidence": [{"resume_section": "projects", "similarity": 0.82}],
        "areas_needing_improvement": [{"component": "keywords", "missing_requirements": ["observability"]}],
    }
    feedback = {
        "overall_feedback": "Strong foundation with focused improvements available.",
        "strengths": [{"title": "Relevant projects", "explanation": "Projects align with the role.", "supporting_evidence": ["API project"]}],
        "missing_skills_feedback": [], "resume_improvements": [], "project_recommendations": [],
        "action_plan": {"high_priority": ["Add observability evidence"], "medium_priority": [], "long_term": []},
    }

    class Client:
        def _respond(self, method, content, filename, description, token):
            st.session_state.api_call = {"method": method, "content": content, "filename": filename, "description": description, "token": token}
            if mode == "error":
                raise ApiError("Analysis failed safely", status_code=422)
            result = {"analysis": analysis}
            if method == "feedback":
                result["feedback"] = feedback
            return result

        def analyze(self, content, filename, description, token):
            return self._respond("analyze", content, filename, description, token)

        def feedback(self, content, filename, description, token):
            return self._respond("feedback", content, filename, description, token)

    if busy:
        st.session_state.analysis_busy = True
    render_analyze(FrontendSettings("https://api.test", None, None, 5 * 1024 * 1024), Client())


def incomplete_analysis_result_test_app() -> None:
    from frontend.app import _render_analyze_result
    _render_analyze_result({"analysis": {"overall_ats_score": 42, "components": {}}})


def shared_css_test_app() -> None:
    from frontend.app import _css
    _css()


def sidebar_test_app(current: str = "Dashboard") -> None:
    import streamlit as st
    from frontend.app import PAGES, _render_sidebar
    from frontend.config import FrontendSettings

    st.session_state.guest_mode = True
    selected = st.query_params.get("page")
    effective_current = selected if selected in PAGES else current
    with st.sidebar:
        _render_sidebar(FrontendSettings("https://api.test", None, None, 5 * 1024 * 1024), effective_current)


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


def test_api_client_forwards_only_opaque_session_cookie_when_no_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/session"
        assert request.headers["cookie"] == "ats_session_dev=opaque-session-id"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"authenticated": True, "email": "user@example.test"})

    client = ApiClient(
        "https://api.test",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"),
        session_id="opaque-session-id",
    )
    assert client.session()["authenticated"] is True


def test_google_oauth_link_uses_same_tab_without_changing_start_endpoint():
    markup = _oauth_link_html("http://localhost:8000/auth/google/start?return_to=http%3A%2F%2Flocalhost%3A8501")
    assert 'target="_self"' in markup
    assert 'target="_blank"' not in markup
    assert "/auth/google/start?return_to=" in markup


def test_browser_logout_script_preserves_secure_post_and_top_level_navigation():
    settings = FrontendSettings(
        api_base_url="https://ats.example.test/api",
        supabase_url=None,
        supabase_publishable_key=None,
        max_upload_bytes=1024,
        oauth_redirect_url="https://ats.example.test",
        auth_public_url="https://ats.example.test",
    )
    script = _browser_logout_html(settings, "csrf-value")
    assert 'fetch("https://ats.example.test/auth/logout"' in script
    assert "method: 'POST'" in script
    assert "credentials: 'include'" in script
    assert "'X-CSRF-Token': \"csrf-value\"" in script
    assert "if (!response.ok) throw new Error('logout failed')" in script
    assert 'if (success) window.location.replace("https://ats.example.test")' in script
    assert "window.parent.location" not in script


def test_browser_logout_script_handles_http_and_network_failures_before_navigation():
    settings = FrontendSettings("https://api.test", None, None, 1024, auth_public_url="https://auth.test")
    script = _browser_logout_html(settings, "csrf-value")
    failure = "Sign-out could not be completed. Reload and try again."
    assert ".catch(function()" in script
    assert failure in script
    assert "return false;" in script
    assert script.index(".catch(function()") < script.index(".then(function(success)")
    assert script.index(failure) < script.index("if (success) window.location.replace")


def test_browser_logout_script_json_encodes_dynamic_values_for_html_script_context():
    settings = FrontendSettings(
        "https://api.test",
        None,
        None,
        1024,
        oauth_redirect_url="https://app.test/</script><script>redirect()",
        auth_public_url="https://auth.test/</script><script>logout()",
    )
    script = _browser_logout_html(settings, "csrf</script><script>value")
    assert "<script>redirect()" not in script
    assert "<script>logout()" not in script
    assert "csrf</script>" not in script
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in script


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


def test_dashboard_helpers_use_only_persisted_history_values():
    rows = [
        {"overall_score": 82.0, "filename": "latest.pdf", "created_at": "2026-11-10T10:00:00Z", "component_scores": {"semantic_similarity": {"score": 84}, "technical_skills": {"score": 80}, "keywords": {"score": 76}, "experience_relevance": {"score": 88}, "formatting_completeness": {"score": 90}}},
        {"overall_score": 68.0, "filename": "older.pdf", "created_at": "2026-11-01T10:00:00Z", "component_scores": {}},
    ]
    assert _dashboard_metrics(rows) == {"count": 2, "average": 75.0, "latest": 82.0, "best": 82.0}
    chart = _score_chart_data(rows)
    assert [point["overall_score"] for point in chart] == [68.0, 82.0]
    assert all(isinstance(point["created_at"], datetime) for point in chart)
    assert [point["filename"] for point in chart] == ["older.pdf", "latest.pdf"]
    assert _latest_breakdown(rows) == [("Semantic Similarity", 84.0), ("Technical Skills", 80.0), ("Keywords", 76.0), ("Experience Relevance", 88.0), ("Formatting", 90.0)]


def test_dashboard_history_pages_existing_endpoint_for_complete_statistics():
    class Client:
        def __init__(self): self.offsets = []
        def history(self, _token, limit, offset):
            self.offsets.append(offset)
            return {"analyses": [{"overall_score": 70}] * (100 if offset == 0 else 1)}

    client = Client()
    assert len(_dashboard_history(client)) == 101
    assert client.offsets == [0, 100]


def test_upload_control_and_visible_instructions_use_authoritative_five_mib_limit():
    assert _upload_limit_mib(5 * 1024 * 1024) == 5
    app = AppTest.from_function(analyze_upload_test_app).run()
    assert len(app.exception) == 0
    assert app.file_uploader[0].help == "PDF or DOCX, up to 5 MiB"
    assert any(item.value == "PDF or DOCX · Maximum 5 MiB per file" for item in app.caption)


def test_analysis_actions_stay_disabled_until_file_and_job_description_exist():
    app = AppTest.from_function(analyze_workflow_test_app).run()
    assert [(button.label, button.disabled) for button in app.button] == [
        ("Analyze Resume", True), ("Generate AI feedback", True),
    ]
    app.file_uploader[0].upload("resume.pdf", b"%PDF-test", PDF_MIME).run()
    assert next(button for button in app.button if button.label == "Analyze Resume").disabled is True
    app.text_area[0].input("Python API role").run()
    assert next(button for button in app.button if button.label == "Analyze Resume").disabled is False
    assert any("resume.pdf" in item.value and "Ready to analyze" in item.value for item in app.markdown)


@pytest.mark.parametrize(("filename", "mime_type"), [("resume.pdf", PDF_MIME), ("resume.docx", DOCX_MIME)])
def test_pdf_and_docx_analysis_submission_preserves_existing_api_contract(filename, mime_type):
    app = AppTest.from_function(analyze_workflow_test_app).run()
    app.file_uploader[0].upload(filename, b"document-bytes", mime_type).run()
    app.text_area[0].input("Python API role").run()
    next(button for button in app.button if button.label == "Analyze Resume").click().run()
    assert len(app.exception) == 0
    assert app.session_state["api_call"] == {
        "method": "analyze", "content": b"document-bytes", "filename": filename,
        "description": "Python API role", "token": None,
    }
    rendered = " ".join(item.value for item in app.markdown)
    assert "Your resume results" in rendered
    assert "Component score breakdown" in rendered
    assert "Python" in rendered and "Kubernetes" in rendered
    assert "Projects" in rendered and "Keywords" in rendered
    assert "AI feedback not generated" in rendered


def test_generate_feedback_uses_feedback_endpoint_and_renders_only_returned_feedback():
    app = AppTest.from_function(analyze_workflow_test_app).run()
    app.file_uploader[0].upload("resume.pdf", b"pdf", PDF_MIME).run()
    app.text_area[0].input("Python role").run()
    next(button for button in app.button if button.label == "Generate AI feedback").click().run()
    assert app.session_state["api_call"]["method"] == "feedback"
    rendered = " ".join(item.value for item in app.markdown)
    assert "Strong foundation with focused improvements available." in rendered
    assert "Relevant projects" in rendered
    assert "Add observability evidence" in rendered


def test_analysis_error_and_loading_states_are_clear_and_do_not_duplicate_requests():
    busy = AppTest.from_function(analyze_workflow_test_app, args=("success", True)).run()
    assert all(button.disabled for button in busy.button)
    assert busy.info[0].value == "Analysis is in progress. Please keep this page open."

    failed = AppTest.from_function(analyze_workflow_test_app, args=("error",)).run()
    failed.file_uploader[0].upload("resume.pdf", b"pdf", PDF_MIME).run()
    failed.text_area[0].input("Python role").run()
    next(button for button in failed.button if button.label == "Analyze Resume").click().run()
    assert len(failed.exception) == 0
    assert failed.error[0].value == "Analysis failed safely"
    assert "latest_result" not in failed.session_state


def test_analysis_result_handles_missing_optional_sections_without_fabrication():
    app = AppTest.from_function(incomplete_analysis_result_test_app).run()
    assert len(app.exception) == 0
    rendered = " ".join(item.value for item in app.markdown)
    assert "Component score details were not included" in app.info[0].value
    assert "No matched skills were identified" in " ".join(item.value for item in app.caption)
    assert "AI feedback not generated" in rendered


def test_shared_css_preserves_streamlit_icon_fonts_and_labels_uploader_once():
    app = AppTest.from_function(shared_css_test_app).run()
    stylesheet = " ".join(item.value for item in app.markdown)
    assert "[class*='st-']" not in stylesheet
    assert 'content: "Browse files"' in stylesheet
    assert '[data-testid="stFileUploaderDropzone"] button > *' in stylesheet
    assert '[data-testid="stSidebar"] [data-testid="stSidebarContent"]' in stylesheet


def test_heading_anchor_css_targets_only_streamlit_heading_links():
    app = AppTest.from_function(shared_css_test_app).run()
    stylesheet = " ".join(item.value for item in app.markdown)
    assert '[data-testid="stHeaderActionElements"] a[aria-label="Link to heading"]' in stylesheet
    assert ".oauth-button" in stylesheet
    assert "a{display:none" not in stylesheet


def test_browse_action_state_tracks_upload_and_remove_nonce_workflow():
    app = AppTest.from_function(analyze_workflow_test_app).run()
    assert app.file_uploader[0].value is None
    assert not any("upload-selected-marker" in item.value for item in app.markdown)

    app.file_uploader[0].upload("resume.pdf", b"pdf", PDF_MIME).run()
    assert app.file_uploader[0].value.name == "resume.pdf"
    assert any("upload-selected-marker" in item.value for item in app.markdown)
    next(button for button in app.button if button.label == "Remove").click().run()

    assert app.file_uploader[0].value is None
    assert not any("upload-selected-marker" in item.value for item in app.markdown)
    assert next(button for button in app.button if button.label == "Analyze Resume").disabled is True


def test_analysis_spacing_centered_actions_and_typography_rules_are_present():
    app = AppTest.from_function(shared_css_test_app).run()
    stylesheet = " ".join(item.value for item in app.markdown)
    assert 'stVerticalBlockBorderWrapper"]:has(.upload-selected-marker)' in stylesheet
    assert ".selected-file" in stylesheet and "margin-top: 18px" in stylesheet
    assert ':has(.analysis-action-marker)' in stylesheet and "align-items: stretch" in stylesheet
    assert "flex-direction: column" in stylesheet and "align-items: center" in stylesheet
    assert ".stApp" in stylesheet and "font-size: 16px" in stylesheet
    assert "font-size: 2rem !important" in stylesheet
    assert ".stButton > button" in stylesheet and "font-size: 0.9rem" in stylesheet


def test_light_dashboard_stylesheet_is_external_complete_and_cwd_independent(tmp_path, monkeypatch):
    assert STYLESHEET_PATH.is_absolute()
    assert STYLESHEET_PATH.name == "main.css"
    monkeypatch.chdir(tmp_path)
    stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")
    assert "--canvas: #f7f8fa" in stylesheet
    assert "--brand: #2563eb" in stylesheet
    assert "#6d5dfc" not in stylesheet.lower()
    for section in (
        "Design tokens", "Global typography", "Sidebar and branding", "Navigation",
        "Page headers", "Cards and containers", "Buttons", "Forms and inputs",
        "File uploader", "Tables and history", "Dashboard charts", "Status messages",
        "Authentication pages", "Responsive layouts",
    ):
        assert section in stylesheet


def test_app_loads_one_external_stylesheet_before_all_page_rendering():
    source = (STYLESHEET_PATH.parent.parent / "app.py").read_text(encoding="utf-8")
    assert 'STYLESHEET_PATH = Path(__file__).resolve().parent / "styles" / "main.css"' in source
    assert "STYLESHEET_PATH.read_text(encoding='utf-8')" in source
    assert 'st.markdown("""<style>' not in source
    assert source.count("def _css()") == 1
    assert source.index("_css(); settings = load_frontend_settings()") < source.index("render_sign_in(settings)")


def test_dashboard_populated_and_empty_states_render_actual_metrics():
    populated = AppTest.from_function(dashboard_test_app, args=(True,)).run()
    assert len(populated.exception) == 0
    assert [metric.value for metric in populated.metric] == ["2", "75.0", "82.0", "82.0"]
    assert any("latest.pdf" in item.value for item in populated.markdown)
    assert any("Score performance" in item.value for item in populated.markdown)
    assert any("Score breakdown" in item.value for item in populated.markdown)

    empty = AppTest.from_function(dashboard_test_app, args=(False,)).run()
    assert len(empty.exception) == 0
    assert [metric.value for metric in empty.metric] == ["0", "—", "—", "—"]
    rendered = " ".join(item.value for item in empty.markdown)
    assert "No score history yet" in rendered
    assert "No analyses yet" in rendered


def test_dashboard_uses_profile_display_name_and_never_greets_with_email():
    assert _display_name({"profile": {"full_name": "  Abhishek   Kumar  "}, "user_metadata": {"name": "Other"}}) == "Abhishek Kumar"
    assert _display_name({"user_metadata": {"name": "Metadata Name"}}) == "Metadata Name"
    assert _display_name({"email": "person@example.test"}) is None

    named = AppTest.from_function(dashboard_test_app, args=(False, "Abhishek Kumar")).run()
    named_markup = " ".join(item.value for item in named.markdown)
    assert "Welcome back, Abhishek Kumar" in named_markup
    assert "Welcome back, person@example.test" not in named_markup

    unnamed = AppTest.from_function(dashboard_test_app, args=(False, None)).run()
    unnamed_markup = " ".join(item.value for item in unnamed.markdown)
    assert "Welcome back</h1>" in unnamed_markup
    assert "Welcome back, person@example.test" not in unnamed_markup


def test_sidebar_omits_status_panel_and_loads_original_svg_logo():
    app = AppTest.from_function(sidebar_test_app).run()
    rendered = " ".join(item.value for item in app.sidebar.markdown)
    assert "AI Resume ATS" in rendered and "Resume Intelligence" in rendered
    assert "data:image/svg+xml;base64," in rendered
    assert "Backend" not in rendered and "Model" not in rendered
    assert all(button.label != "Check status" for button in app.sidebar.button)

    assert LOGO_PATH.is_file()
    svg = LOGO_PATH.read_text(encoding="utf-8")
    assert "AI Resume ATS logo" in svg
    assert "folded corner" in svg
    assert "#2563EB" in svg
    assert _asset_data_uri(LOGO_PATH, "image/svg+xml").startswith("data:image/svg+xml;base64,")


@pytest.mark.parametrize("target", PAGES)
def test_icon_sidebar_preserves_all_navigation_actions(target):
    current = "Analyze Resume" if target == "Dashboard" else "Dashboard"
    app = AppTest.from_function(sidebar_test_app, args=(current,)).run()
    assert app.sidebar.radio[0].options == list(PAGES)
    app.sidebar.radio[0].set_value(target).run()
    assert len(app.exception) == 0
    assert app.query_params["page"] == target


def test_sidebar_navigation_uses_four_valid_distinct_svg_assets_without_squares():
    from xml.etree import ElementTree

    stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")
    assert '[data-testid="stRadioOption"] > div > div:first-child' in stylesheet
    assert stylesheet.count('background-image: url("data:image/svg+xml;base64,') == 4
    assert "mask-image" not in stylesheet
    for index in range(1, 5):
        assert f"label:nth-child({index})" in stylesheet
    assert "stRadioOption\"]::before" in stylesheet
    assets = [LOGO_PATH.parent / name for name in ("nav_dashboard.svg", "nav_analyze.svg", "nav_history.svg", "nav_profile.svg")]
    contents = []
    for asset in assets:
        assert asset.is_file()
        root = ElementTree.fromstring(asset.read_text(encoding="utf-8"))
        assert root.attrib["viewBox"] == "0 0 24 24"
        assert root.attrib["fill"] == "none"
        assert root.attrib["stroke"] == "#2563EB"
        assert not root.findall("{http://www.w3.org/2000/svg}rect")
        contents.append(asset.read_bytes())
    assert len(set(contents)) == 4


@pytest.mark.parametrize(("label", "page"), [
    ("＋ Analyze New Resume", "Analyze Resume"),
    ("＋  Analyze a Resume", "Analyze Resume"),
    ("↗  View History", "Analysis History"),
    ("○  Update Profile", "Profile"),
])
def test_dashboard_actions_preserve_existing_page_navigation(label, page):
    app = AppTest.from_function(dashboard_test_app, args=(False,)).run()
    button = next(item for item in app.button if item.label == label)
    button.click().run()
    assert len(app.exception) == 0
    assert app.query_params["page"] == page


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
    session = {"access_token": "secret", "auth_email": "a@example.test", "auth_user_metadata": {"full_name": "Test User"}, "latest_result": {}, "latest_upload": b"x", "other": True}
    clear_session(session)
    assert session == {"other": True}


def test_forgot_password_click_displays_persistent_form_without_widget_error():
    app = AppTest.from_function(reset_form_app).run()
    assert len(app.text_input) == 0
    app.button[0].click().run()
    assert len(app.exception) == 0
    assert app.text_input[0].label == "Email for password reset"
    app.run()
    assert len(app.text_input) == 1
    assert app.session_state["reset_form_visible"] is True


def test_reset_email_submission_calls_existing_reset_method():
    app = AppTest.from_function(reset_form_app).run()
    app.button[0].click().run()
    app.text_input[0].input(" person@example.test ").run()
    app.button[1].click().run()
    assert len(app.exception) == 0
    assert app.session_state["reset_calls"] == ["person@example.test"]
    assert app.success[0].value.startswith("If an account exists")


def test_invalid_reset_email_does_not_call_reset_method():
    app = AppTest.from_function(reset_form_app).run()
    app.button[0].click().run()
    app.text_input[0].input("invalid email").run()
    app.button[1].click().run()
    assert len(app.exception) == 0
    assert "reset_calls" not in app.session_state
    assert app.error[0].value == "Enter a valid email address."


@pytest.mark.parametrize("page", PAGES)
def test_navigation_restores_each_valid_page_from_query_parameter(page):
    app = AppTest.from_function(navigation_query_app)
    app.query_params["page"] = page
    app.run()
    assert len(app.exception) == 0
    assert app.radio[0].value == page
    assert app.markdown[0].value == f"Selected: {page}"


@pytest.mark.parametrize("query_params", [{}, {"page": "Not a real page"}, {"page": "dashboard"}])
def test_navigation_defaults_to_dashboard_for_missing_or_invalid_page(query_params):
    app = AppTest.from_function(navigation_query_app)
    app.query_params.update(query_params)
    app.run()
    assert len(app.exception) == 0
    assert app.radio[0].value == "Dashboard"
    assert app.markdown[0].value == "Selected: Dashboard"


def test_sidebar_navigation_updates_url_without_rerun_loop():
    app = AppTest.from_function(navigation_query_app).run()
    app.radio[0].set_value("Analysis History").run()
    assert len(app.exception) == 0
    assert app.query_params["page"] == "Analysis History"
    assert app.radio[0].value == "Analysis History"
    app.run()
    assert len(app.exception) == 0
    assert app.radio[0].value == "Analysis History"


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


def test_email_password_login_reads_verified_user_metadata_name(monkeypatch):
    class PasswordAuth:
        def sign_in_with_password(self, _):
            user = type("User", (), {"user_metadata": {"full_name": "Abhishek Kumar"}, "profile": None})()
            session = type("Session", (), {"access_token": "password-access", "user": user})()
            return type("Response", (), {"session": session, "user": user})()
    class PasswordClient: auth = PasswordAuth()
    monkeypatch.setattr("frontend.auth.create_client", lambda *_: PasswordClient())
    auth = SupabaseAuth("https://supabase.test", "sb_publishable_safe")
    assert auth.login("user@example.test", "password") == "password-access"
    assert auth.display_name == "Abhishek Kumar"


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
            return type("UserResponse", (), {"user": type("User", (), {"email": "google.user@example.test", "user_metadata": {"name": "Abhishek Kumar"}, "profile": None})()})()
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
    assert auth.display_name == "Abhishek Kumar"
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
