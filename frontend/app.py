"""Professional Streamlit interface for the existing AI Resume ATS API."""

from __future__ import annotations

import re
import sys
import logging
import json
import html
import base64
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import streamlit as st

# `streamlit run frontend/app.py` executes this file as a script and can place
# frontend/ (rather than the project root) first on sys.path. Ensure the one
# canonical `frontend` package is importable in both script and module modes.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLESHEET_PATH = Path(__file__).resolve().parent / "styles" / "main.css"
LOGO_PATH = Path(__file__).resolve().parent / "static" / "assets" / "ai_resume_ats_logo.svg"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend.api_client import ApiClient, ApiError
from frontend.auth import AuthError, SupabaseAuth, clear_session
from frontend.components.feedback_display import render_feedback
from frontend.components.score_display import score_band
from frontend.config import FrontendSettings, load_frontend_settings

logger = logging.getLogger(__name__)

PAGES = ("Dashboard", "Analyze Resume", "Analysis History", "Profile")
NAVIGATION_ICONS = {
    "Dashboard": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10M9 20v-6h6v6"/></svg>',
    "Analyze Resume": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><circle cx="11.5" cy="15" r="2.75"/><path d="m16.5 20-3-3"/></svg>',
    "Analysis History": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg>',
    "Profile": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
}
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


def _display_name(identity: Any) -> str | None:
    """Resolve presentation-only identity fields without deriving a name from email."""
    if not isinstance(identity, dict):
        return None
    nested = [identity.get("profile"), identity.get("user_metadata"), identity.get("auth_profile"), identity.get("auth_user_metadata")]
    for source in [*nested, identity]:
        if not isinstance(source, dict):
            continue
        for field in ("full_name", "name", "display_name", "auth_display_name"):
            value = source.get(field)
            if isinstance(value, str):
                normalized = " ".join(value.split())
                if normalized and "@" not in normalized:
                    return normalized
    return None


def _token_display_name(access_token: str | None) -> str | None:
    """Read Supabase user metadata for display only; never use it for authorization."""
    if not access_token:
        return None
    try:
        payload = access_token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded.decode("utf-8"))
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _display_name(claims)


def _session_display_name() -> str | None:
    identity = {
        key: st.session_state.get(key)
        for key in ("profile", "user_metadata", "auth_profile", "auth_user_metadata", "full_name", "name", "display_name", "auth_display_name")
    }
    return _display_name(identity) or _token_display_name(_token())


def _asset_data_uri(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _navigation_icon_uri(page: str, active: bool) -> str:
    color = "#2563EB" if active else "#64748B"
    svg = NAVIGATION_ICONS[page].replace("currentColor", color)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _sidebar_navigation_html(current: str) -> str:
    items = []
    for page in PAGES:
        active = " active" if page == current else ""
        href = f"?{urlencode({'page': page})}"
        current_attribute = ' aria-current="page"' if page == current else ""
        icon_uri = _navigation_icon_uri(page, page == current)
        items.append(
            f'<a class="sidebar-nav-item{active}" href="{html.escape(href, quote=True)}" target="_self"{current_attribute}>'
            f'<img class="sidebar-nav-icon" src="{icon_uri}" width="21" height="21" alt=""/>'
            f'<span>{html.escape(page)}</span></a>'
        )
    return '<nav class="sidebar-nav" aria-label="Workspace navigation">' + "".join(items) + "</nav>"


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
    if status.get("display_name"):
        st.session_state.auth_display_name = status["display_name"]
    for key in ("profile", "user_metadata", "full_name", "name"):
        if status.get(key) is not None:
            st.session_state[f"auth_{key}" if key in {"profile", "user_metadata"} else key] = status[key]


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
        if auth.display_name:
            st.session_state.auth_display_name = auth.display_name
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
        "overall_ats_score": record.get("overall_score"), "maximum_score": 100.0,
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
                            if auth.display_name:
                                st.session_state.auth_display_name = auth.display_name
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
    display_name = _session_display_name() if _is_authenticated() else None
    greeting = f"Welcome back, {html.escape(display_name)}" if display_name else "Welcome back" if _is_authenticated() else "Welcome to your workspace"
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
                    {"height": 265, "mark": {"type": "line", "color": "#2563eb", "strokeWidth": 3, "point": {"filled": True, "fill": "#ffffff", "stroke": "#2563eb", "strokeWidth": 2, "size": 80}}, "encoding": {"x": {"field": "created_at", "type": "temporal", "title": None, "axis": {"format": "%b %d", "labelAngle": 0, "labelColor": "#64748b", "domain": False, "ticks": False}}, "y": {"field": "overall_score", "type": "quantitative", "title": None, "scale": {"domain": [0, 100]}, "axis": {"gridColor": "#e2e8f0", "labelColor": "#64748b", "domain": False, "ticks": False}}, "tooltip": [{"field": "filename", "title": "Resume"}, {"field": "overall_score", "title": "ATS score", "format": ".1f"}, {"field": "created_at", "type": "temporal", "title": "Date", "format": "%b %d, %Y"}]}, "config": {"view": {"stroke": None}, "background": "transparent"}},
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


def _history_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


def _history_record_html(record: dict[str, Any]) -> str:
    filename_value = record.get("filename")
    filename = str(filename_value).strip() if isinstance(filename_value, str) and filename_value.strip() else "Untitled resume"
    date = _analysis_date(record.get("created_at"))
    score = _history_score(record.get("overall_score"))
    if score is None:
        score_value, score_caption = "—", "Score unavailable"
        status, status_class = "Unavailable", "unavailable"
    else:
        score_value, score_caption = f"{score:.1f}", "ATS score"
        status = score_band(score)
        status_class = status.lower().replace(" ", "-")
    return (
        '<div class="history-record-summary">'
        f'<div class="history-file"><span>RESUME</span><b>{html.escape(filename)}</b></div>'
        f'<div class="history-date"><span>ANALYZED</span><b>{html.escape(date)}</b></div>'
        f'<div class="history-score"><span>{html.escape(score_caption.upper())}</span><b>{html.escape(score_value)}</b></div>'
        f'<div class="history-status"><span>STATUS</span><i class="score-badge {status_class}">{html.escape(status)}</i></div>'
        '</div>'
    )


def _render_history_details(record: dict[str, Any]) -> None:
    analysis = _history_analysis(record)
    score = _history_score(record.get("overall_score"))
    filename = record.get("filename") if isinstance(record.get("filename"), str) and record.get("filename") else "Untitled resume"
    st.markdown("<div class='history-details-heading'><span>SELECTED ANALYSIS</span><h2>Analysis details</h2><p>Review the persisted scores, requirements, and evidence for this resume.</p></div>", unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("<span class='history-details-marker'></span>", unsafe_allow_html=True)
        score_column, context_column = st.columns((1, 3), vertical_alignment="center")
        with score_column:
            if score is None:
                st.markdown("<div class='history-overall-score unavailable'><b>—</b><span>Score unavailable</span></div>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div class='history-overall-score'><b>{score:.1f}</b><span>ATS score / 100</span></div>", unsafe_allow_html=True)
        with context_column:
            st.markdown(f"### {html.escape(str(filename))}")
            st.caption(f"Analyzed {_analysis_date(record.get('created_at'))}")
            st.caption(analysis["disclaimer"])

    components = analysis.get("components") if isinstance(analysis.get("components"), dict) else {}
    with st.container(border=True):
        st.markdown("<div class='card-heading'><h3>Component scores</h3><p>Persisted scoring details from this analysis.</p></div>", unsafe_allow_html=True)
        rendered = 0
        for key, label in SCORE_COMPONENTS:
            item = components.get(key)
            if not isinstance(item, dict):
                continue
            component_score = _history_score(item.get("score"))
            weight = item.get("weight")
            weight_text = f"Weight {weight}%" if isinstance(weight, (int, float)) else "Weight unavailable"
            score_text = f"{component_score:.1f}" if component_score is not None else "—"
            width = max(0.0, min(100.0, component_score)) if component_score is not None else 0.0
            st.markdown(
                f'<div class="analysis-score-row"><div><span>{html.escape(label)}</span><small>{html.escape(weight_text)}</small><b>{score_text}</b></div><div class="score-track"><i style="width:{width:.1f}%"></i></div></div>',
                unsafe_allow_html=True,
            )
            if explanation := item.get("explanation"):
                st.caption(str(explanation))
            rendered += 1
        if not rendered:
            st.caption("Component score details are unavailable for this historical record.")

    matched_column, missing_column = st.columns(2)
    with matched_column:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Matched requirements</h3><p>Skills and keywords recorded as matches.</p></div>", unsafe_allow_html=True)
            _render_requirement_group("Matched skills", analysis.get("matched_skills"), "No matched skills were recorded.")
            _render_requirement_group("Matched keywords", analysis.get("matched_keywords"), "No matched keywords were recorded.")
    with missing_column:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Missing requirements</h3><p>Skills and keywords recorded as gaps.</p></div>", unsafe_allow_html=True)
            _render_requirement_group("Missing skills", analysis.get("missing_skills"), "No missing skills were recorded.", "missing")
            _render_requirement_group("Missing keywords", analysis.get("missing_keywords"), "No missing keywords were recorded.", "missing")

    evidence_column, improvement_column = st.columns(2)
    with evidence_column:
        with st.container(border=True):
            st.markdown("<div class='card-heading'><h3>Resume evidence</h3><p>Persisted semantic evidence from the resume.</p></div>", unsafe_allow_html=True)
            evidence = _list_value(analysis.get("relevant_resume_evidence"))
            if not evidence:
                st.caption("No resume evidence was recorded for this analysis.")
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
            st.markdown("<div class='card-heading'><h3>Improvement areas</h3><p>Persisted component gaps and missing requirements.</p></div>", unsafe_allow_html=True)
            areas = _list_value(analysis.get("areas_needing_improvement"))
            if not areas:
                st.caption("No improvement areas were recorded for this analysis.")
            for area in areas:
                if not isinstance(area, dict):
                    st.write(f"- {area}")
                    continue
                component = str(area.get("component", "Component")).replace("_", " ").title()
                st.markdown(f"**{component}**")
                missing = _list_value(area.get("missing_requirements"))
                if missing:
                    for requirement in missing:
                        st.write(f"- {requirement}")
                else:
                    st.caption("No specific missing requirements were recorded.")

    feedback = record.get("feedback")
    if isinstance(feedback, dict) and feedback:
        with st.container(border=True):
            render_feedback(feedback)


def render_history(client: ApiClient) -> None:
    st.markdown("<div class='dashboard-header'><span>RESUME LIBRARY</span><h1>Analysis History</h1><p>Review saved resume evaluations, scores, and detailed results.</p></div>", unsafe_allow_html=True)
    if not _is_authenticated():
        st.info("Sign in to access your saved analysis history."); return
    offset = st.session_state.get("history_offset", 0)
    try:
        payload = client.history(_token(), limit=10, offset=offset)
        raw_rows = payload.get("analyses", []) if isinstance(payload, dict) else []
        rows = raw_rows if isinstance(raw_rows, list) else []
    except ApiError as exc: st.error(_friendly_api_error(exc)); return
    if not rows:
        with st.container(border=True):
            st.markdown("<div class='history-empty'><b>No saved analyses on this page</b><span>Analyze a resume to create a saved result and start building your history.</span></div>", unsafe_allow_html=True)
            if st.button("Analyze Resume", type="primary", key="history_empty_analyze"):
                _set_page("Analyze Resume")
    else:
        st.caption(f"Showing {len(rows)} saved {'analysis' if len(rows) == 1 else 'analyses'}")
    for index, raw_row in enumerate(rows):
        row = raw_row if isinstance(raw_row, dict) else {}
        analysis_id = row.get("id")
        record_key = str(analysis_id) if analysis_id else f"record-{offset + index}"
        with st.container(border=True):
            st.markdown("<span class='history-card-marker'></span>", unsafe_allow_html=True)
            st.markdown(_history_record_html(row), unsafe_allow_html=True)
            action_spacer, view_action, delete_action = st.columns((3.5, 1.25, 1), vertical_alignment="center")
            action_spacer.markdown("<span class='history-actions-marker'></span>", unsafe_allow_html=True)
            if view_action.button("View details", key=f"open-{record_key}", width="stretch", disabled=not bool(analysis_id)):
                try:
                    selected = client.get_analysis(analysis_id, _token())
                    st.session_state.selected_analysis = selected if isinstance(selected, dict) else {}
                except ApiError as exc:
                    st.error(_friendly_api_error(exc))
            if delete_action.button("Delete", key=f"delete-{record_key}", width="stretch", disabled=not bool(analysis_id)):
                st.session_state.confirm_delete = analysis_id
            if analysis_id and st.session_state.get("confirm_delete") == analysis_id:
                st.warning("Delete this saved analysis? This cannot be undone.")
                confirm_spacer, yes, no = st.columns((3.5, 1.25, 1))
                confirm_spacer.write("")
                if yes.button("Confirm delete", key=f"confirm-{record_key}", width="stretch"):
                    try:
                        client.delete_analysis(analysis_id, _token())
                        st.session_state.pop("confirm_delete", None)
                        st.session_state.pop("selected_analysis", None)
                        st.rerun()
                    except ApiError as exc:
                        st.error(_friendly_api_error(exc))
                if no.button("Cancel", key=f"cancel-{record_key}", width="stretch"):
                    st.session_state.pop("confirm_delete", None)
                    st.rerun()
    if selected := st.session_state.get("selected_analysis"):
        _render_history_details(selected if isinstance(selected, dict) else {})
    pagination_start, previous, next_page, pagination_end = st.columns((2, 1, 1, 2))
    pagination_start.markdown("<span class='history-pagination-marker'></span>", unsafe_allow_html=True)
    pagination_end.write("")
    if previous.button("Previous", width="stretch", disabled=offset == 0): st.session_state.history_offset = max(0, offset - 10); st.rerun()
    if next_page.button("Next", width="stretch", disabled=len(rows) < 10): st.session_state.history_offset = offset + 10; st.rerun()


def render_profile(settings: FrontendSettings) -> None:
    st.markdown("<div class='dashboard-header'><span>ACCOUNT</span><h1>Profile</h1><p>Review your account information and security options.</p></div>", unsafe_allow_html=True)
    if not _is_authenticated(): st.info("You are using guest mode. Sign in to manage a profile and saved analyses."); return
    with st.container(border=True):
        st.markdown("<div class='card-heading'><h3>Account information</h3><p>Your authenticated identity and available security actions.</p></div>", unsafe_allow_html=True)
        st.markdown(f"<div class='profile-account'><b>Email</b><br>{html.escape(str(st.session_state.get('auth_email', 'Authenticated user')))}</div>", unsafe_allow_html=True)
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
    """Load the single shared stylesheet relative to this module."""
    st.markdown(f"<style>{STYLESHEET_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def _render_sidebar(settings: FrontendSettings, current: str) -> str:
    logo_uri = _asset_data_uri(LOGO_PATH, "image/svg+xml")
    st.markdown(f"<div class='sidebar-brand'><img class='sidebar-logo' src='{logo_uri}' alt='AI Resume ATS document and checkmark logo'><div><h2>AI Resume ATS</h2><p>Resume Intelligence</p></div></div>", unsafe_allow_html=True)
    st.markdown("<div class='sidebar-label'>WORKSPACE</div>", unsafe_allow_html=True)
    st.html(_sidebar_navigation_html(current))
    st.divider()
    sidebar_identity = str(st.session_state.get("auth_email", "Guest mode"))
    sidebar_name = _session_display_name() if _is_authenticated() else None
    initial_source = sidebar_name or sidebar_identity
    initial = html.escape(initial_source[:1].upper() if initial_source else "U")
    profile_label = html.escape(sidebar_name) if sidebar_name else "Signed in" if _is_authenticated() else "Guest mode"
    st.markdown(f"<div class='sidebar-profile'><div class='profile-avatar'>{initial}</div><div><b>{profile_label}</b><span>{html.escape(sidebar_identity)}</span></div></div>", unsafe_allow_html=True)
    if st.session_state.get("browser_authenticated"):
        _render_browser_logout(settings, "sidebar_browser_logout", width="stretch")
    elif _token() and st.button("Sign out", width="stretch"):
        auth = _auth(settings)
        if auth:
            auth.logout()
        clear_session(st.session_state)
        st.session_state.pop("guest_mode", None)
        st.rerun()
    return current



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
        page = _render_sidebar(settings, current)
    {"Dashboard": lambda: render_dashboard(client), "Analyze Resume": lambda: render_analyze(settings, client), "Analysis History": lambda: render_history(client), "Profile": lambda: render_profile(settings)}[page]()


if __name__ == "__main__": main()
