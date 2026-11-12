"""Professional Streamlit interface for the existing AI Resume ATS API."""

from __future__ import annotations

import re
import sys
import logging
import json
import html
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import streamlit as st

# `streamlit run frontend/app.py` executes this file as a script and can place
# frontend/ (rather than the project root) first on sys.path. Ensure the one
# canonical `frontend` package is importable in both script and module modes.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend.api_client import ApiClient, ApiError
from frontend.auth import AuthError, SupabaseAuth, clear_session
from frontend.components.feedback_display import render_feedback
from frontend.components.score_display import render_analysis, score_band
from frontend.config import FrontendSettings, load_frontend_settings

logger = logging.getLogger(__name__)

PAGES = ("Dashboard", "Analyze Resume", "Analysis History", "Profile")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCORE_COMPONENTS = (
    ("semantic_similarity", "Semantic Similarity"),
    ("technical_skills", "Technical Skills"),
    ("keywords", "Keywords"),
    ("experience_relevance", "Experience Relevance"),
    ("formatting_completeness", "Formatting"),
)


def check_backend(client: ApiClient) -> dict[str, Any]:
    """Probe the backend once: liveness via /health/live, model readiness via /health/ready."""
    status: dict[str, Any] = {"live": False, "ready": False, "model": None}
    try:
        client.health_live()
        status["live"] = True
    except ApiError:
        return status
    try:
        ready = client.health_ready()
        status["ready"] = ready.get("status") == "ready"
        status["model"] = ready.get("model")
    except ApiError:
        status["ready"] = False
    return status


@st.cache_data(ttl=60, show_spinner=False)
def backend_status(api_base_url: str, cache_bust: int = 0) -> dict[str, Any]:
    """Cached so Streamlit reruns never re-request health more than once per minute."""
    return check_backend(ApiClient(api_base_url))


def _client(settings: FrontendSettings) -> ApiClient:
    return ApiClient(
        settings.api_base_url,
        session_id=_browser_session_id(settings),
        session_cookie_name=settings.auth_cookie_name,
    )


def _token() -> str | None:
    return st.session_state.get("access_token")


def _browser_session_id(settings: FrontendSettings) -> str | None:
    return st.context.cookies.get(settings.auth_cookie_name)


def _is_authenticated() -> bool:
    return bool(_token() or st.session_state.get("browser_authenticated"))


def _restore_browser_session(settings: FrontendSettings) -> None:
    """Restore an opaque-cookie session before deciding whether to show sign-in."""
    if _token() or not _browser_session_id(settings):
        return
    try:
        status = _client(settings).session()
    except ApiError:
        clear_session(st.session_state)
        return
    st.session_state.browser_authenticated = True
    st.session_state.auth_email = status.get("email", "Authenticated user")
    st.session_state.auth_csrf_token = status.get("csrf_token")


def _json_for_script(value: str) -> str:
    """JSON-encode dynamic JavaScript values and neutralize HTML delimiters."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _browser_logout_html(settings: FrontendSettings, csrf: str) -> str:
    logout_url = _json_for_script(settings.auth_public_url + "/auth/logout")
    csrf_value = _json_for_script(csrf)
    redirect_url = _json_for_script(settings.oauth_redirect_url)
    return f"""
    <div style="font-family:sans-serif;color:#516074">Signing out…</div>
    <script>
    fetch({logout_url}, {{
      method: 'POST', credentials: 'include',
      headers: {{'X-CSRF-Token': {csrf_value}}}
    }}).then(function(response) {{
      if (!response.ok) throw new Error('logout failed');
      return true;
    }}).catch(function() {{
      document.body.textContent = 'Sign-out could not be completed. Reload and try again.';
      return false;
    }}).then(function(success) {{
      if (success) window.location.replace({redirect_url});
    }});
    </script>
    """


def _render_browser_logout(settings: FrontendSettings, key: str, width: str = "content") -> None:
    if not st.button("Sign out", key=key, type="primary", width=width):
        return
    csrf = st.session_state.get("auth_csrf_token")
    if not csrf:
        clear_session(st.session_state)
        st.rerun()
    st.html(_browser_logout_html(settings, csrf), unsafe_allow_javascript=True)


def _auth(settings: FrontendSettings) -> SupabaseAuth | None:
    if not settings.supabase_url or not settings.supabase_publishable_key:
        return None
    return SupabaseAuth(settings.supabase_url, settings.supabase_publishable_key)


def _consume_google_callback(auth: SupabaseAuth | None) -> None:
    """Exchange an OAuth code once, then remove it from the browser URL."""
    code = st.query_params.get("code")
    flow_id = st.query_params.get("oauth_flow")
    provider_error = st.query_params.get("error")
    if code or provider_error:
        logger.info("Google OAuth callback received query parameter names: %s", sorted(st.query_params.keys()))
    if provider_error:
        st.query_params.clear()
        st.session_state.oauth_error = "Google sign-in was cancelled or could not be completed. Please try again."
        return
    if not code:
        return
    if not flow_id:
        st.query_params.clear()
        st.session_state.oauth_error = "Google sign-in session information is missing. Start Google sign-in again."
        return
    if auth is None:
        st.query_params.clear()
        st.session_state.oauth_error = "Google sign-in is unavailable because authentication is not configured."
        return
    try:
        logger.info("Google OAuth callback received an authorization code; beginning Supabase exchange.")
        with st.spinner("Completing Google sign-in..."):
            token, email = auth.complete_google_callback(str(code), str(flow_id))
        st.session_state.access_token = token
        st.session_state.auth_email = email
        st.session_state.pop("guest_mode", None)
        st.session_state.oauth_success = "Signed in with Google."
    except AuthError as exc:
        st.session_state.oauth_error = str(exc)
    finally:
        # Never retain one-time authorization codes in browser-visible URLs.
        st.query_params.clear()


def _set_page(page: str) -> None:
    selected = page if page in PAGES else "Dashboard"
    st.session_state.current_page = selected
    if st.query_params.get("page") != selected:
        st.query_params["page"] = selected
    st.rerun()


def _page_from_query() -> str:
    page = st.query_params.get("page")
    return page if page in PAGES else "Dashboard"


def _dashboard_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    scores = [float(row["overall_score"]) for row in rows if isinstance(row.get("overall_score"), (int, float))]
    return {
        "count": len(rows),
        "average": sum(scores) / len(scores) if scores else None,
        "latest": float(rows[0]["overall_score"]) if rows and isinstance(rows[0].get("overall_score"), (int, float)) else None,
        "best": max(scores) if scores else None,
    }


def _dashboard_history(client: ApiClient, token: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.history(token, limit=100, offset=offset).get("analyses", [])
        if not isinstance(batch, list):
            break
        rows.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
        offset += 100
    return rows


def _analysis_date(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "Date unavailable"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%b %d, %Y")


def _score_chart_data(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row in rows:
        score = row.get("overall_score")
        created_at = row.get("created_at")
        if not isinstance(score, (int, float)) or not isinstance(created_at, str):
            continue
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        points.append({"created_at": parsed, "overall_score": float(score), "filename": row.get("filename") or "Untitled resume"})
    return sorted(points, key=lambda point: point["created_at"])


def _latest_breakdown(rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    if not rows or not isinstance(rows[0].get("component_scores"), dict):
        return []
    components = rows[0]["component_scores"]
    breakdown: list[tuple[str, float]] = []
    for key, label in SCORE_COMPONENTS:
        item = components.get(key)
        score = item.get("score") if isinstance(item, dict) else None
        if isinstance(score, (int, float)):
            breakdown.append((label, max(0.0, min(100.0, float(score)))))
    return breakdown


def _friendly_api_error(error: ApiError) -> str:
    if error.status_code == 401:
        clear_session(st.session_state)
        return "Your session has expired. Please sign in again."
    if error.code in {"groq_not_configured", "groq_authentication_failed", "groq_rate_limited", "groq_timeout", "groq_unavailable", "invalid_feedback", "unsupported_feedback"}:
        return "AI feedback is temporarily unavailable. Your ATS analysis remains available."
    if error.status_code in {502, 503, 504}:
        return "The analysis service is temporarily unavailable. Please try again."
    if error.code in {"invalid_document", "unsupported_document", "invalid_content_type"}:
        return "Please upload a valid PDF or DOCX file."
    return str(error)


def _history_analysis(record: dict[str, Any]) -> dict[str, Any]:
    components = record.get("component_scores", {})
    if not isinstance(components, dict):
        components = {}

    semantic = components.get("semantic_similarity", {})
    semantic_evidence = semantic.get("evidence", {}) if isinstance(semantic, dict) else {}
    matches = semantic_evidence.get("matches", []) if isinstance(semantic_evidence, dict) else []
    relevant_evidence = matches if isinstance(matches, list) else []

    improvement_areas: list[dict[str, Any]] = []
    for name, item in components.items():
        if not isinstance(item, dict):
            continue
        score = item.get("score")
        if not isinstance(score, (int, float)) or score >= 70:
            continue
        missing = item.get("missing_requirements", [])
        improvement_areas.append({
            "component": name,
            "missing_requirements": missing if isinstance(missing, list) else [],
        })

    return {
        "overall_ats_score": record.get("overall_score", 0), "maximum_score": 100.0,
        "methodology_version": "deterministic-ats-v1",
        "disclaimer": "This deterministic ATS indicator is not a hiring probability or a guarantee of selection.",
        "components": components, "matched_skills": record.get("matched_skills", []),
        "missing_skills": record.get("missing_skills", []), "matched_keywords": record.get("matched_keywords", []),
        "missing_keywords": record.get("missing_keywords", []),
        "relevant_resume_evidence": relevant_evidence,
        "areas_needing_improvement": improvement_areas,
    }


def _validate_upload(uploaded_file: Any, job_description: str, max_bytes: int) -> str | None:
    if uploaded_file is None: return "Please upload a valid PDF or DOCX file."
    if not uploaded_file.name.lower().endswith((".pdf", ".docx")): return "Please upload a valid PDF or DOCX file."
    if uploaded_file.size > max_bytes: return f"The selected file exceeds the {max_bytes // (1024 * 1024)} MiB upload limit."
    if not job_description.strip(): return "Enter a job description before continuing."
    return None


def _upload_limit_mib(max_bytes: int) -> int:
    """Use the largest whole-MiB uploader limit that never exceeds the backend limit."""
    return max(1, max_bytes // (1024 * 1024))


def _oauth_link_html(url: str) -> str:
    return f'<a class="oauth-button" href="{html.escape(url, quote=True)}" target="_self">Continue with Google</a>'


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _render_requirement_group(title: str, values: Any, empty_message: str, tone: str = "matched") -> None:
    items = _list_value(values)
    st.markdown(f"**{title}**")
    if not items:
        st.caption(empty_message)
        return
    chips = "".join(
        f'<span class="requirement-chip {tone}">{html.escape(str(item))}</span>'
        for item in items
    )
    st.markdown(f'<div class="requirement-list">{chips}</div>', unsafe_allow_html=True)


def _render_analysis_feedback(feedback: dict[str, Any] | None) -> None:
    with st.container(border=True):
        st.markdown("<div class='card-heading'><h3>AI feedback</h3><p>Optional guidance generated from the completed ATS analysis.</p></div>", unsafe_allow_html=True)
        if not isinstance(feedback, dict) or not feedback:
            st.markdown("<div class='analysis-empty compact'><b>AI feedback not generated</b><span>Use Generate AI feedback to request optional recommendations.</span></div>", unsafe_allow_html=True)
            return

        if overall := feedback.get("overall_feedback"):
            st.write(overall)
        sections = (
            ("Strengths", "strengths", ("title", "explanation")),
            ("Missing skills", "missing_skills_feedback", ("skill", "importance", "recommendation")),
            ("Resume improvements", "resume_improvements", ("section", "current_issue", "suggestion")),
            ("Suggested future projects", "project_recommendations", ("project_name", "description")),
        )
        for heading, key, fields in sections:
            entries = _list_value(feedback.get(key))
            if not entries:
                continue
            st.markdown(f"#### {heading}")
            for entry in entries:
                if not isinstance(entry, dict):
                    st.write(entry)
                    continue
                primary = entry.get(fields[0])
                if primary:
                    st.markdown(f"**{primary}**")
                for field in fields[1:]:
                    if entry.get(field):
                        st.write(entry[field])
                for evidence_key in ("supporting_evidence", "skills_demonstrated"):
                    for evidence in _list_value(entry.get(evidence_key)):
                        st.write(f"- {evidence}")
        plan = feedback.get("action_plan")
        if isinstance(plan, dict) and plan:
            st.markdown("#### Action plan")
            for priority in ("high_priority", "medium_priority", "long_term"):
                actions = _list_value(plan.get(priority))
                if actions:
                    st.markdown(f"**{priority.replace('_', ' ').title()}**")
                    for action in actions:
                        st.write(f"- {action}")


def _render_analyze_result(result: dict[str, Any]) -> None:
    analysis = result.get("analysis")
    if not isinstance(analysis, dict):
        st.warning("The analysis completed, but no result details were returned.")
        return

    raw_score = analysis.get("overall_ats_score", 0)
    score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
    bounded_score = max(0.0, min(100.0, score))
    st.markdown("<div class='analysis-results-heading'><span>ANALYSIS COMPLETE</span><h2>Your resume results</h2><p>Review the score, matched requirements, and evidence returned by the ATS analysis.</p></div>", unsafe_allow_html=True)

    with st.container(border=True):
        score_column, summary_column = st.columns((0.34, 0.66), vertical_alignment="center")
        with score_column:
            st.markdown(
                f'<div class="ats-score" style="--score:{bounded_score:.1f}"><div><b>{score:.1f}</b><span>/ 100</span></div></div>',
                unsafe_allow_html=True,
            )
        with summary_column:
            band = score_band(score)
            band_class = "strong" if band == "Strong" else "needs-work" if band == "Needs work" else ""
            st.markdown(f'<span class="score-badge {band_class}">{html.escape(band)}</span>', unsafe_allow_html=True)
            st.markdown("### Overall ATS score")
            st.write("This score summarizes alignment across the five configured ATS components.")
            st.caption(analysis.get("disclaimer", "This score is an indicator, not a hiring probability."))

    components = analysis.get("components") if isinstance(analysis.get("components"), dict) else {}
    with st.container(border=True):
        st.markdown("<div class='card-heading'><h3>Component score breakdown</h3><p>The existing 35 / 25 / 15 / 15 / 10 weighted scoring components.</p></div>", unsafe_allow_html=True)
        rendered_components = 0
        for key, label in SCORE_COMPONENTS:
            item = components.get(key)
            if not isinstance(item, dict):
                continue
            component_score = item.get("score", 0)
            numeric_score = float(component_score) if isinstance(component_score, (int, float)) else 0.0
            weight = item.get("weight", 0)
            st.markdown(
                f'<div class="analysis-score-row"><div><span>{html.escape(label)}</span><small>Weight {html.escape(str(weight))}%</small><b>{numeric_score:.1f}</b></div><div class="score-track"><i style="width:{max(0, min(100, numeric_score)):.1f}%"></i></div></div>',
                unsafe_allow_html=True,
            )
            if explanation := item.get("explanation"):
                st.caption(explanation)
            rendered_components += 1
        if not rendered_components:
            st.info("Component score details were not included in this result.")

    requirements_left, requirements_right = st.columns(2)
    with requirements_left:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Matched requirements</h3><p>Skills and keywords found in the resume.</p></div>", unsafe_allow_html=True)
            _render_requirement_group("Matched skills", analysis.get("matched_skills"), "No matched skills were identified.")
            _render_requirement_group("Matched keywords", analysis.get("matched_keywords"), "No matched keywords were identified.")
    with requirements_right:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Missing requirements</h3><p>Existing gaps identified against the job description.</p></div>", unsafe_allow_html=True)
            _render_requirement_group("Missing skills", analysis.get("missing_skills"), "No missing skills were identified.", "missing")
            _render_requirement_group("Missing keywords", analysis.get("missing_keywords"), "No missing keywords were identified.", "missing")

    evidence_column, improvement_column = st.columns(2)
    with evidence_column:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Resume strengths</h3><p>Relevant resume evidence returned by semantic analysis.</p></div>", unsafe_allow_html=True)
            evidence = _list_value(analysis.get("relevant_resume_evidence"))
            if not evidence:
                st.caption("No relevant resume evidence was included in this result.")
            for item in evidence:
                if isinstance(item, dict):
                    section = str(item.get("resume_section", "Resume section")).replace("_", " ").title()
                    similarity = item.get("similarity")
                    suffix = f" · {float(similarity) * 100:.1f}% similarity" if isinstance(similarity, (int, float)) else ""
                    st.markdown(f"- **{section}**{suffix}")
                else:
                    st.write(f"- {item}")
    with improvement_column:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Areas for improvement</h3><p>Existing component gaps and missing requirements.</p></div>", unsafe_allow_html=True)
            areas = _list_value(analysis.get("areas_needing_improvement"))
            if not areas:
                st.caption("No improvement areas were included in this result.")
            for area in areas:
                if not isinstance(area, dict):
                    st.write(f"- {area}")
                    continue
                component = str(area.get("component", "Component")).replace("_", " ").title()
                missing = _list_value(area.get("missing_requirements"))
                st.markdown(f"**{component}**")
                if missing:
                    for requirement in missing:
                        st.write(f"- {requirement}")
                else:
                    st.caption("Review this component for additional alignment opportunities.")

    _render_analysis_feedback(result.get("feedback"))


def _valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(email.strip()))


def _run_auth(action: Callable[[], None]) -> bool:
    try:
        with st.spinner("Contacting the authentication service..."):
            action()
        return True
    except AuthError as exc:
        st.error(str(exc))
        return False


def _show_reset_form() -> None:
    st.session_state.reset_form_visible = True


def _render_password_reset(auth: SupabaseAuth) -> None:
    st.button(
        "Forgot password?",
        key="forgot_password_button",
        type="tertiary",
        width="content",
        on_click=_show_reset_form,
    )
    if st.session_state.get("reset_form_visible", False):
        reset_email = st.text_input("Email for password reset", key="reset_email")
        if st.button("Send reset email", key="reset_password"):
            if not _valid_email(reset_email):
                st.error("Enter a valid email address.")
            elif _run_auth(lambda: auth.reset_password(reset_email.strip())):
                st.success("If an account exists for this email, a password reset message has been sent.")


def render_sign_in(settings: FrontendSettings) -> None:
    _, auth_column, _ = st.columns((1, 1.15, 1))
    with auth_column:
        with st.container(border=True):
            st.markdown("<div class='auth-brand'><div class='auth-logo'>AI</div><span>RESUME INTELLIGENCE</span><h1>Welcome to AI Resume ATS</h1><p>Understand how your resume aligns with each role before you apply.</p></div>", unsafe_allow_html=True)
            auth = _auth(settings)
            if auth is None:
                st.error(settings.auth_configuration_error or "Authentication is not configured. Add the Supabase URL and publishable key to the local environment.")
                return
            if error := st.session_state.pop("oauth_error", None):
                st.error(error)
            google_url = f"{settings.auth_public_url}/auth/google/start?{urlencode({'return_to': settings.oauth_redirect_url})}"
            st.markdown(_oauth_link_html(google_url), unsafe_allow_html=True)
            st.caption("Secure sign-in through Google and Supabase. Your Google credentials are never sent to this application.")
            st.divider()
            login_tab, signup_tab = st.tabs(["Sign in", "Create account"])
            with login_tab:
                with st.form("login_form", clear_on_submit=False):
                    email = st.text_input("Email address", key="login_email", placeholder="you@example.com")
                    password = st.text_input("Password", type="password", key="login_password")
                    submitted = st.form_submit_button("Sign in", type="primary", width="stretch", disabled=st.session_state.get("auth_busy", False))
                if submitted:
                    if not _valid_email(email): st.error("Enter a valid email address.")
                    elif not password: st.error("Enter your password.")
                    else:
                        def login() -> None:
                            st.session_state.auth_busy = True
                            st.session_state.access_token = auth.login(email.strip(), password)
                            st.session_state.auth_email = email.strip()
                            st.session_state.auth_busy = False
                            st.rerun()
                        _run_auth(login); st.session_state.auth_busy = False
                _render_password_reset(auth)
            with signup_tab:
                st.caption("Use at least 8 characters and follow your project's password policy.")
                with st.form("signup_form", clear_on_submit=False):
                    email = st.text_input("Email address", key="signup_email", placeholder="you@example.com")
                    password = st.text_input("Password", type="password", key="signup_password")
                    submitted = st.form_submit_button("Create account", type="primary", width="stretch", disabled=st.session_state.get("auth_busy", False))
                if submitted:
                    if not _valid_email(email): st.error("Enter a valid email address.")
                    elif len(password) < 8: st.error("Use a password with at least 8 characters.")
                    else:
                        def signup() -> None:
                            st.session_state.auth_busy = True
                            signed_in = auth.signup(email.strip(), password)
                            st.session_state.auth_busy = False
                            st.success("Account created. Sign in now." if signed_in else "Account created. Check your email to confirm it, then sign in.")
                        _run_auth(signup); st.session_state.auth_busy = False
            st.divider()
            if st.button("Continue without an account", width="stretch"):
                st.session_state.guest_mode = True
                _set_page("Analyze Resume")


def render_dashboard(client: ApiClient) -> None:
    identity = st.session_state.get("auth_email") if _is_authenticated() else None
    greeting = f"Welcome back, {html.escape(str(identity))}" if identity else "Welcome to your workspace"
    header_copy, header_action = st.columns((4, 1), vertical_alignment="center")
    with header_copy:
        st.markdown(
            f"<div class='dashboard-header'><span>RESUME INTELLIGENCE</span><h1>{greeting}</h1>"
            "<p>Track your resume performance and improve every application.</p></div>",
            unsafe_allow_html=True,
        )
    with header_action:
        if st.button("＋ Analyze New Resume", type="primary", width="stretch", key="dashboard_analyze_header"):
            _set_page("Analyze Resume")
    if not _is_authenticated():
        with st.container(border=True):
            st.markdown("### Your dashboard is ready")
            st.caption("Guest mode supports stateless analysis. Sign in to save your work and access score history.")
            if st.button("Analyze a Resume", type="primary", key="guest_dashboard_analyze"): _set_page("Analyze Resume")
        return
    try: rows = _dashboard_history(client, _token())
    except ApiError as exc: st.error(_friendly_api_error(exc)); return
    metrics = _dashboard_metrics(rows)
    cards = st.columns(4)
    cards[0].metric("Resumes analyzed", str(metrics["count"]), help="Saved analyses in your history")
    cards[1].metric("Average ATS score", f"{metrics['average']:.1f}" if metrics["average"] is not None else "—")
    cards[2].metric("Latest ATS score", f"{metrics['latest']:.1f}" if metrics["latest"] is not None else "—")
    cards[3].metric("Best score", f"{metrics['best']:.1f}" if metrics["best"] is not None else "—")

    performance, breakdown_card = st.columns((1.65, 1), gap="large")
    with performance:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><div><h3>Score performance</h3><p>Your ATS score trend over time</p></div></div>", unsafe_allow_html=True)
            chart_data = _score_chart_data(rows)
            if chart_data:
                st.vega_lite_chart(
                    chart_data,
                    {"height": 265, "mark": {"type": "line", "color": "#6d5dfc", "strokeWidth": 3, "point": {"filled": True, "fill": "#ffffff", "stroke": "#6d5dfc", "strokeWidth": 2, "size": 80}}, "encoding": {"x": {"field": "created_at", "type": "temporal", "title": None, "axis": {"format": "%b %d", "labelAngle": 0, "labelColor": "#6b7280", "domain": False, "ticks": False}}, "y": {"field": "overall_score", "type": "quantitative", "title": None, "scale": {"domain": [0, 100]}, "axis": {"gridColor": "#eef0f5", "labelColor": "#6b7280", "domain": False, "ticks": False}}, "tooltip": [{"field": "filename", "title": "Resume"}, {"field": "overall_score", "title": "ATS score", "format": ".1f"}, {"field": "created_at", "type": "temporal", "title": "Date", "format": "%b %d, %Y"}]}, "config": {"view": {"stroke": None}, "background": "transparent"}},
                    width="stretch",
                )
                if len(chart_data) == 1:
                    st.caption("Add another analysis to see a connected performance trend.")
            else:
                st.markdown("<div class='dashboard-empty'><b>No score history yet</b><span>Analyze a resume to start tracking performance.</span></div>", unsafe_allow_html=True)

    with breakdown_card:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><div><h3>Score breakdown</h3><p>Your latest analysis</p></div></div>", unsafe_allow_html=True)
            breakdown = _latest_breakdown(rows)
            if breakdown:
                for label, score in breakdown:
                    st.markdown(f"<div class='score-row'><div><span>{label}</span><b>{score:.0f}</b></div><div class='score-track'><i style='width:{score:.2f}%'></i></div></div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='dashboard-empty compact'><b>No breakdown available</b><span>Your latest component scores will appear here.</span></div>", unsafe_allow_html=True)

    recent_card, actions_card = st.columns((1.65, 1), gap="large")
    with recent_card:
        with st.container(border=True):
            recent_heading, recent_link = st.columns((4, 1), vertical_alignment="center")
            recent_heading.markdown("<div class='card-heading'><div><h3>Recent analyses</h3><p>Your latest resume evaluations</p></div></div>", unsafe_allow_html=True)
            if recent_link.button("View all →", key="dashboard_view_all", width="stretch"): _set_page("Analysis History")
            if not rows:
                st.markdown("<div class='dashboard-empty compact'><b>No analyses yet</b><span>Your saved resume analyses will appear here.</span></div>", unsafe_allow_html=True)
            for index, row in enumerate(rows[:5]):
                name, score_col, status_col, date_col, action_col = st.columns((3.2, 1, 1.35, 1.6, .7), vertical_alignment="center")
                name.write(row.get("filename") or "Untitled resume")
                score = float(row.get("overall_score", 0))
                score_col.markdown(f"**{score:.1f}**")
                band = score_band(score)
                status_col.markdown(f"<span class='score-badge {band.lower().replace(' ', '-')}'>{band}</span>", unsafe_allow_html=True)
                date_col.caption(_analysis_date(row.get("created_at")))
                if action_col.button("View", key=f"dashboard-view-{row.get('id', index)}", disabled=not bool(row.get("id"))):
                    try:
                        st.session_state.selected_analysis = client.get_analysis(row["id"], _token())
                        _set_page("Analysis History")
                    except ApiError as exc:
                        st.error(_friendly_api_error(exc))
                if index < min(len(rows), 5) - 1: st.markdown("<div class='row-divider'></div>", unsafe_allow_html=True)

    with actions_card:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><div><h3>Quick actions</h3><p>Continue your workflow</p></div></div>", unsafe_allow_html=True)
            if st.button("＋  Analyze a Resume", type="primary", width="stretch", key="dashboard_quick_analyze"): _set_page("Analyze Resume")
            if st.button("↗  View History", width="stretch", key="dashboard_quick_history"): _set_page("Analysis History")
            if st.button("○  Update Profile", width="stretch", key="dashboard_quick_profile"): _set_page("Profile")


def render_analyze(settings: FrontendSettings, client: ApiClient) -> None:
    st.markdown("<div class='dashboard-header'><span>RESUME ANALYSIS</span><h1>Analyze Resume</h1><p>Understand how your resume aligns with a job description and identify areas for improvement.</p></div>", unsafe_allow_html=True)
    nonce = st.session_state.get("upload_nonce", 0)
    upload_limit_mib = _upload_limit_mib(settings.max_upload_bytes)
    input_left, input_right = st.columns(2, gap="large")
    with input_left:
        with st.container(border=True):
            st.markdown("<div class='analysis-input-marker card-heading'><h3>Upload your resume</h3><p>Select the document you want to compare with this role.</p></div>", unsafe_allow_html=True)
            upload = st.file_uploader("Resume document", type=["pdf", "docx"], key=f"resume_upload_{nonce}", help=f"PDF or DOCX, up to {upload_limit_mib} MiB", max_upload_size=upload_limit_mib, label_visibility="collapsed")
            st.caption(f"PDF or DOCX · Maximum {upload_limit_mib} MiB per file")
            if upload is None:
                st.markdown("<div class='upload-empty'><b>No resume selected</b><span>Drag and drop a file above or use Browse files.</span></div>", unsafe_allow_html=True)
            else:
                st.markdown("<span class='upload-selected-marker'></span>", unsafe_allow_html=True)
                file_col, remove_col = st.columns((3, 1), vertical_alignment="center")
                file_col.markdown(f'<div class="selected-file"><b>{html.escape(upload.name)}</b><span>{upload.size / 1024:.1f} KiB · Ready to analyze</span></div>', unsafe_allow_html=True)
                if remove_col.button("Remove", width="stretch"):
                    st.session_state.upload_nonce = nonce + 1
                    st.rerun()
    with input_right:
        with st.container(border=True):
            st.markdown("<div class='analysis-input-marker card-heading'><h3>Job description</h3><p>Paste the responsibilities, skills, and experience expected for the role.</p></div>", unsafe_allow_html=True)
            job_description = st.text_area("Job description", key="job_description", height=255, max_chars=20_000, placeholder="Paste the role requirements, responsibilities, and preferred qualifications.", label_visibility="collapsed")
            st.caption(f"Required · {len(job_description):,} / 20,000 characters")

    action_marker, analyze_col, feedback_col, action_end = st.columns((1, 1.6, 1.6, 1))
    action_marker.markdown("<span class='analysis-action-marker'></span>", unsafe_allow_html=True)
    action_end.markdown("<span class='analysis-action-end-marker'></span>", unsafe_allow_html=True)
    busy = st.session_state.get("analysis_busy", False)
    has_required_inputs = upload is not None and bool(job_description.strip())
    analyze_clicked = analyze_col.button("Analyze Resume", type="primary", width="stretch", disabled=busy or not has_required_inputs)
    feedback_clicked = feedback_col.button("Generate AI feedback", width="stretch", disabled=busy or not has_required_inputs, help="Runs the same ATS analysis plus optional AI feedback.")
    if busy:
        st.info("Analysis is in progress. Please keep this page open.")
    elif not has_required_inputs:
        st.caption("Select a resume and enter a job description to enable analysis.")
    if analyze_clicked or feedback_clicked:
        validation = _validate_upload(upload, job_description, settings.max_upload_bytes)
        if validation: st.error(validation)
        else:
            st.session_state.analysis_busy = True
            try:
                with st.spinner("Analyzing resume alignment..."):
                    result = client.feedback(upload.getvalue(), upload.name, job_description, _token()) if feedback_clicked else client.analyze(upload.getvalue(), upload.name, job_description, _token())
                st.session_state.latest_result = result
                st.success("Analysis complete and saved to your history." if result.get("analysis_id") else "Analysis complete.")
            except ApiError as exc:
                st.error(_friendly_api_error(exc))
            finally: st.session_state.analysis_busy = False
    result = st.session_state.get("latest_result")
    if result:
        _render_analyze_result(result)


def render_history(client: ApiClient) -> None:
    st.title("Analysis history")
    if not _is_authenticated():
        st.info("Sign in to access your saved analysis history."); return
    offset = st.session_state.get("history_offset", 0)
    try: rows = client.history(_token(), limit=10, offset=offset).get("analyses", [])
    except ApiError as exc: st.error(_friendly_api_error(exc)); return
    if not rows: st.info("No analyses on this page. Analyze a resume to create your history.")
    for row in rows:
        with st.expander(f"{row.get('filename') or 'Untitled resume'} · {row.get('overall_score', 0):.1f}/100 · {row.get('created_at', '')}"):
            left, right = st.columns(2)
            if left.button("View details", key=f"open-{row['id']}"):
                try: st.session_state.selected_analysis = client.get_analysis(row["id"], _token())
                except ApiError as exc: st.error(_friendly_api_error(exc))
            if right.button("Delete", key=f"delete-{row['id']}"):
                st.session_state.confirm_delete = row["id"]
            if st.session_state.get("confirm_delete") == row["id"]:
                st.warning("Delete this saved analysis? This cannot be undone.")
                yes, no = st.columns(2)
                if yes.button("Confirm delete", key=f"confirm-{row['id']}"):
                    try: client.delete_analysis(row["id"], _token()); st.session_state.pop("confirm_delete", None); st.session_state.pop("selected_analysis", None); st.rerun()
                    except ApiError as exc: st.error(_friendly_api_error(exc))
                if no.button("Cancel", key=f"cancel-{row['id']}"): st.session_state.pop("confirm_delete", None); st.rerun()
    if selected := st.session_state.get("selected_analysis"):
        st.divider(); st.subheader("Analysis details"); render_analysis(_history_analysis(selected))
        if selected.get("feedback"): render_feedback(selected["feedback"])
    previous, next_page = st.columns(2)
    if previous.button("Previous", disabled=offset == 0): st.session_state.history_offset = max(0, offset - 10); st.rerun()
    if next_page.button("Next", disabled=len(rows) < 10): st.session_state.history_offset = offset + 10; st.rerun()


def render_profile(settings: FrontendSettings) -> None:
    st.title("Profile")
    if not _is_authenticated(): st.info("You are using guest mode. Sign in to manage a profile and saved analyses."); return
    st.subheader("Account")
    st.write(f"**Email:** {st.session_state.get('auth_email', 'Authenticated user')}")
    st.caption("Your access is scoped by Supabase Auth and database row-level security.")
    auth = _auth(settings)
    if st.button("Send password reset email") and auth:
        try: auth.reset_password(st.session_state.get("auth_email", "")); st.success("If the account is available, a reset email has been sent.")
        except AuthError as exc: st.error(str(exc))
    if st.session_state.get("browser_authenticated"):
        _render_browser_logout(settings, "profile_browser_logout")
    elif st.button("Sign out", type="primary"):
        if auth: auth.logout()
        clear_session(st.session_state); st.session_state.pop("guest_mode", None); st.rerun()


def _css() -> None:
    st.markdown("""<style>
    :root{--brand:#6d5dfc;--brand-dark:#5748e5;--brand-soft:#f0efff;--navy:#172033;--muted:#6b7280;--border:#e7e9f0;--surface:#fff;--canvas:#f5f6fa;--success:#1f9d68}
    html,body,.stApp,button,input,textarea,select{font-family:Inter,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
    html,body,.stApp{max-width:100%;overflow-x:clip}.stApp{background:var(--canvas);color:var(--navy);font-size:16px;line-height:1.58}
    h1{font-size:1.9rem!important;line-height:1.2!important;font-weight:750!important;letter-spacing:-.025em!important;color:var(--navy)!important}h2{font-size:1.35rem!important;line-height:1.3!important;font-weight:730!important;color:var(--navy)!important}h3{font-size:1.12rem!important;line-height:1.4!important;font-weight:700!important;color:var(--navy)!important}
    p,[data-testid='stMarkdownContainer'] p{font-size:.97rem;line-height:1.58}[data-testid='stCaptionContainer']{color:var(--muted);font-size:.84rem;line-height:1.5}label,[data-testid='stWidgetLabel']{font-size:.9rem!important;color:#4d5669!important;font-weight:600!important}
    [data-testid='stHeaderActionElements'] a[aria-label='Link to heading']{display:none!important}
    .block-container{max-width:1440px;padding:2.2rem 2.5rem 3.5rem}
    [data-testid='stSidebar']{background:#fff;border-right:1px solid var(--border);overflow-x:hidden}
    [data-testid='stSidebar'] [data-testid='stSidebarContent']{padding-top:1.25rem;overflow-x:hidden}
    [data-testid='stSidebar'] [data-testid='stSidebarUserContent']{max-width:100%;overflow-x:hidden}
    [data-testid='stSidebar'] *{box-sizing:border-box;min-width:0}
    [data-testid='stSidebar'] hr{border-color:#edf0f5;margin:.9rem 0}
    .sidebar-brand{display:flex;align-items:center;gap:.75rem;max-width:100%;padding:.25rem .45rem 1.8rem}.sidebar-brand>div:last-child{min-width:0}
    .sidebar-logo{width:38px;height:38px;border-radius:11px;background:var(--brand);color:#fff;display:grid;place-items:center;font-weight:800;font-size:1.05rem;box-shadow:0 7px 18px rgba(109,93,252,.24)}
    .sidebar-brand h2{font-size:1.05rem!important;line-height:1.15!important;margin:0;color:var(--navy);font-weight:750}.sidebar-brand p{margin:.24rem 0 0;font-size:.82rem;color:#8a91a1}
    .sidebar-label{padding:0 .55rem .55rem;color:#858d9d;font-size:.76rem;font-weight:750;letter-spacing:.07em}
    [data-testid='stSidebar'] div[role='radiogroup']{gap:.28rem}
    [data-testid='stSidebar'] div[role='radiogroup'] label{max-width:100%;padding:.63rem .75rem;border-radius:9px;color:#626a7b;transition:none;position:relative;overflow-wrap:anywhere}
    [data-testid='stSidebar'] div[role='radiogroup'] label:has(input:checked){background:var(--brand-soft);color:var(--brand);font-weight:700}
    [data-testid='stSidebar'] div[role='radiogroup'] label:has(input:checked):before{content:'';position:absolute;left:-1rem;top:20%;bottom:20%;width:3px;border-radius:0 4px 4px 0;background:var(--brand)}
    [data-testid='stSidebar'] div[role='radiogroup'] [data-testid='stWidgetLabel']{display:none}
    .sidebar-spacer{height:clamp(1.5rem,10vh,7rem)}
    .sidebar-status{width:100%;max-width:100%;border:1px solid var(--border);border-radius:11px;padding:.7rem .8rem;background:#fafbfc;display:grid;gap:.45rem}
    .status-line{display:flex;align-items:center;justify-content:space-between;color:#697184;font-size:.82rem}.status-value{color:#354052;font-weight:650}.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:.35rem;background:#b8bec9}.status-dot.online{background:var(--success);box-shadow:0 0 0 3px #ddf5eb}.status-dot.ready{background:var(--brand);box-shadow:0 0 0 3px var(--brand-soft)}
    .sidebar-profile{display:flex;align-items:center;gap:.65rem;max-width:100%;padding:.25rem .15rem}.sidebar-profile>div:last-child{min-width:0}.profile-avatar{width:34px;height:34px;min-width:34px!important;border-radius:50%;display:grid;place-items:center;background:var(--brand-soft);color:var(--brand);font-size:.86rem;font-weight:800}.sidebar-profile b{display:block;color:#30384a;font-size:.88rem}.sidebar-profile span{display:block;max-width:100%;overflow-wrap:anywhere;color:#8a91a1;font-size:.8rem;white-space:normal}
    .auth-brand{margin:0 auto 1.1rem;padding:.5rem .35rem .35rem;text-align:center}.auth-logo{width:46px;height:46px;border-radius:13px;background:var(--brand);color:#fff;display:grid;place-items:center;margin:0 auto .9rem;font-weight:800;box-shadow:0 8px 20px rgba(109,93,252,.22)}
    .auth-brand span,.dashboard-header span{color:var(--brand);font-size:.78rem;font-weight:800;letter-spacing:.08em}.auth-brand h1{font-size:1.75rem!important;color:var(--navy);margin:.5rem 0 .4rem}.auth-brand p{color:var(--muted);font-size:.92rem;max-width:390px;margin:0 auto}
    .oauth-button{display:flex;align-items:center;justify-content:center;width:100%;min-height:2.75rem;border-radius:9px;background:var(--brand);color:#fff!important;text-decoration:none!important;font-size:.92rem;font-weight:700;box-shadow:0 5px 12px rgba(109,93,252,.18);margin:.35rem 0 .35rem}.oauth-button:hover{background:var(--brand-dark)}
    [data-baseweb='tab-list']{gap:1rem}[data-baseweb='tab']{font-size:.9rem;font-weight:650;padding:.7rem .2rem}
    [data-testid='stTextInput'] input{border-radius:9px;min-height:2.6rem}
    .dashboard-header{padding:.2rem 0 1.2rem}.dashboard-header h1{color:var(--navy);font-size:1.9rem;line-height:1.2;margin:.42rem 0 .32rem;font-weight:750;letter-spacing:-.025em}.dashboard-header p{color:var(--muted);font-size:.95rem;margin:0}
    .stButton>button,.stLinkButton>a{border-radius:9px;font-size:.9rem;font-weight:650;line-height:1.3;min-height:2.65rem;border-color:#dfe2ea}
    .stButton>button[kind='primary'],.stLinkButton>a[kind='primary']{background:var(--brand);border-color:var(--brand);box-shadow:0 5px 12px rgba(109,93,252,.18)}.stButton>button[kind='primary']:hover{background:var(--brand-dark);border-color:var(--brand-dark)}
    [data-testid='stMetric']{background:#fff;border:1px solid var(--border);border-radius:14px;padding:1.08rem 1.15rem;box-shadow:0 2px 9px rgba(31,38,55,.035);min-height:108px}
    [data-testid='stMetricLabel']{color:#7a8190;font-size:.82rem}[data-testid='stMetricValue']{color:var(--navy);font-size:1.65rem;font-weight:750}
    [data-testid='stVerticalBlockBorderWrapper']{background:#fff;border-color:var(--border)!important;border-radius:15px!important;box-shadow:0 2px 10px rgba(31,38,55,.035);padding:.28rem}
    .card-heading{padding:.05rem 0 .55rem}.card-heading h3{font-size:1.12rem!important;color:var(--navy);margin:0;font-weight:750}.card-heading p{font-size:.88rem;color:#7c8494;margin:.22rem 0 0}
    .dashboard-empty{min-height:265px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#8a91a1;background:#fafbfc;border:1px dashed #dfe2ea;border-radius:11px}.dashboard-empty b{color:#4a5365;font-size:.9rem}.dashboard-empty span{font-size:.8rem;margin-top:.28rem}.dashboard-empty.compact{min-height:130px}
    .score-row{margin:.68rem 0 1rem}.score-row>div:first-child{display:flex;justify-content:space-between;align-items:center;margin-bottom:.42rem}.score-row span{font-size:.82rem;color:#596174}.score-row b{font-size:.8rem;color:#343d50}.score-track{height:7px;background:#edeef4;border-radius:8px;overflow:hidden}.score-track i{display:block;height:100%;border-radius:8px;background:var(--brand)}
    .score-badge{display:inline-block;border-radius:999px;padding:.24rem .55rem;font-size:.75rem;font-weight:750;background:#fff2db;color:#9a6715;white-space:nowrap}.score-badge.strong{background:#e5f7ef;color:#16815a}.score-badge.needs-work{background:#fdeced;color:#b54852}
    .row-divider{height:1px;background:#edf0f4;margin:.15rem 0 .3rem}
    [data-testid='stHorizontalBlock']:has(.analysis-input-marker)>[data-testid='stColumn']{display:flex;min-width:0}[data-testid='stColumn']:has(.analysis-input-marker)>[data-testid='stVerticalBlock']{width:100%}[data-testid='stColumn']:has(.analysis-input-marker) [data-testid='stVerticalBlockBorderWrapper']{width:100%;height:100%;min-height:390px}
    [data-testid='stFileUploaderDropzone']{min-height:158px;max-width:100%;border:1.5px dashed #cfd3e3;border-radius:12px;background:#fafaff;padding:1.25rem}[data-testid='stFileUploaderDropzone']:hover{border-color:var(--brand);background:#f8f7ff}[data-testid='stFileUploaderDropzone'] button{border-color:#d9d5ff;color:var(--brand);font-size:0!important;font-weight:700;white-space:nowrap}[data-testid='stFileUploaderDropzone'] button>*{display:none!important}[data-testid='stFileUploaderDropzone'] button:after{content:'Browse files';display:inline;font-family:Inter,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:.9rem;line-height:1.2}[data-testid='stVerticalBlockBorderWrapper']:has(.upload-selected-marker) [data-testid='stFileUploaderDropzone'] button{display:none!important}
    [data-testid='stTextArea'] textarea{max-width:100%;border-radius:11px;border-color:#dfe2ea;background:#fcfcfe;font-size:.95rem;line-height:1.58;padding:.85rem}.upload-selected-marker{display:none}.upload-empty{min-height:62px;display:flex;flex-direction:column;justify-content:center;padding:.65rem .8rem;border-radius:9px;background:#fafbfc;color:#7c8494}.upload-empty b{font-size:.88rem;color:#4c5568}.upload-empty span{font-size:.82rem}.selected-file{display:flex;min-width:0;max-width:100%;flex-direction:column;margin-top:18px;padding:.7rem .85rem;border:1px solid #e1defd;border-radius:10px;background:var(--brand-soft)}.selected-file b{display:block;max-width:100%;font-size:.9rem;color:#34305f;overflow-wrap:anywhere;word-break:break-word}.selected-file span{font-size:.82rem;color:#77718f;margin-top:.12rem}
    .analysis-action-marker,.analysis-action-end-marker{display:none}[data-testid='stHorizontalBlock']:has(.analysis-action-marker){align-items:stretch;gap:1rem}[data-testid='stHorizontalBlock']:has(.analysis-action-marker) button{min-height:2.75rem}[data-testid='stHorizontalBlock']:has(.analysis-action-marker) button:disabled{background:#eceef3!important;border-color:#dfe2e8!important;color:#9ba1ad!important;box-shadow:none!important;opacity:1!important;cursor:not-allowed}
    .analysis-results-heading{padding:2rem 0 .8rem}.analysis-results-heading>span{color:var(--success);font-size:.78rem;font-weight:800;letter-spacing:.08em}.analysis-results-heading h2{margin:.35rem 0 .2rem}.analysis-results-heading p{margin:0;color:var(--muted);font-size:.92rem}
    .ats-score{--score:0;width:154px;height:154px;margin:.5rem auto;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--brand) calc(var(--score)*1%),#ececf4 0);position:relative}.ats-score:before{content:'';position:absolute;inset:11px;border-radius:50%;background:#fff}.ats-score>div{position:relative;text-align:center;display:flex;flex-direction:column}.ats-score b{font-size:2.15rem;line-height:1;color:var(--navy)}.ats-score span{font-size:.82rem;color:var(--muted);margin-top:.3rem}
    .analysis-score-row{margin:.85rem 0 .25rem}.analysis-score-row>div:first-child{display:grid;grid-template-columns:minmax(150px,1fr) auto 45px;align-items:center;gap:.65rem;margin-bottom:.4rem}.analysis-score-row span{font-size:.9rem;font-weight:650;color:#414a5e}.analysis-score-row small{font-size:.78rem;color:#9097a5}.analysis-score-row b{text-align:right;font-size:.86rem;color:#343d50}.requirement-list{display:flex;flex-wrap:wrap;gap:.42rem;margin:.45rem 0 1rem}.requirement-chip{display:inline-block;border-radius:999px;padding:.32rem .65rem;background:#e8f7f0;color:#157657;font-size:.8rem;font-weight:650}.requirement-chip.missing{background:#fff0f1;color:#aa4651}.analysis-empty{min-height:145px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#858c9b;background:#fafbfc;border:1px dashed #dfe2ea;border-radius:11px}.analysis-empty.compact{min-height:100px}.analysis-empty b{font-size:.9rem;color:#4c5568}.analysis-empty span{font-size:.82rem;margin-top:.25rem}
    @media(max-width:900px){.block-container{max-width:100%;padding:1.3rem 1rem 2.5rem}.dashboard-header h1{font-size:1.75rem!important}[data-testid='stHorizontalBlock']{gap:.75rem}[data-testid='stHorizontalBlock']:has(.analysis-input-marker){flex-direction:column}[data-testid='stHorizontalBlock']:has(.analysis-input-marker)>[data-testid='stColumn']{width:100%!important;flex:1 1 100%!important}[data-testid='stColumn']:has(.analysis-input-marker) [data-testid='stVerticalBlockBorderWrapper']{min-height:0}[data-testid='stHorizontalBlock']:has(.analysis-action-marker){flex-direction:column;align-items:center}[data-testid='stHorizontalBlock']:has(.analysis-action-marker)>[data-testid='stColumn']{width:min(100%,420px)!important;flex:1 1 auto!important}[data-testid='stHorizontalBlock']:has(.analysis-action-marker)>[data-testid='stColumn']:has(.analysis-action-marker),[data-testid='stHorizontalBlock']:has(.analysis-action-marker)>[data-testid='stColumn']:has(.analysis-action-end-marker){display:none}.sidebar-spacer{height:1rem}.auth-brand h1{font-size:1.65rem!important}}
    </style>""", unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="AI Resume ATS", page_icon="📄", layout="wide")
    _css(); settings = load_frontend_settings()
    auth_result = st.query_params.get("auth")
    if auth_result:
        st.query_params.clear()
        if auth_result == "success": st.session_state.oauth_success = "Signed in with Google."
        else: st.session_state.oauth_error = "Google sign-in was cancelled or could not be completed. Please try again."
    _restore_browser_session(settings)
    if success := st.session_state.pop("oauth_success", None):
        st.success(success)
    if not _is_authenticated() and not st.session_state.get("guest_mode"):
        render_sign_in(settings); return
    client = _client(settings); current = _page_from_query(); st.session_state.current_page = current
    with st.sidebar:
        st.markdown("<div class='sidebar-brand'><div class='sidebar-logo'>AI</div><div><h2>AI Resume ATS</h2><p>Resume intelligence</p></div></div>", unsafe_allow_html=True)
        st.markdown("<div class='sidebar-label'>WORKSPACE</div>", unsafe_allow_html=True)
        page = st.radio("Navigation", PAGES, index=PAGES.index(current), label_visibility="collapsed")
        if page != current: _set_page(page)
        st.markdown("<div class='sidebar-spacer'></div>", unsafe_allow_html=True)
        status = backend_status(settings.api_base_url, st.session_state.get("status_bust", 0))
        st.markdown(
            f"<div class='sidebar-status'><div class='status-line'><span><i class='status-dot {'online' if status['live'] else ''}'></i>Backend</span><b class='status-value'>{'Online' if status['live'] else 'Offline'}</b></div>"
            f"<div class='status-line'><span><i class='status-dot {'ready' if status['ready'] else ''}'></i>Model</span><b class='status-value'>{'Ready' if status['ready'] else 'Not ready'}</b></div></div>",
            unsafe_allow_html=True,
        )
        if st.button("Check status", width="stretch"):
            st.session_state.status_bust = st.session_state.get("status_bust", 0) + 1; st.rerun()
        st.divider()
        sidebar_identity = str(st.session_state.get("auth_email", "Guest mode"))
        initial = html.escape(sidebar_identity[:1].upper() if sidebar_identity else "U")
        st.markdown(f"<div class='sidebar-profile'><div class='profile-avatar'>{initial}</div><div><b>{'Signed in' if _is_authenticated() else 'Guest mode'}</b><span>{html.escape(sidebar_identity)}</span></div></div>", unsafe_allow_html=True)
        if st.session_state.get("browser_authenticated"):
            _render_browser_logout(settings, "sidebar_browser_logout", width="stretch")
        elif _token() and st.button("Sign out", width="stretch"):
            auth = _auth(settings)
            if auth: auth.logout()
            clear_session(st.session_state); st.session_state.pop("guest_mode", None); st.rerun()
    {"Dashboard": lambda: render_dashboard(client), "Analyze Resume": lambda: render_analyze(settings, client), "Analysis History": lambda: render_history(client), "Profile": lambda: render_profile(settings)}[page]()


if __name__ == "__main__": main()
