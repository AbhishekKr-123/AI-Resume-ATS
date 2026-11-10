"""Professional Streamlit interface for the existing AI Resume ATS API."""

from __future__ import annotations

import re
from typing import Any, Callable

import streamlit as st

from frontend.api_client import ApiClient, ApiError
from frontend.auth import AuthError, SupabaseAuth, clear_session
from frontend.components.feedback_display import render_feedback
from frontend.components.score_display import render_analysis
from frontend.config import FrontendSettings, load_frontend_settings

PAGES = ("Dashboard", "Analyze Resume", "Analysis History", "Profile")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    return ApiClient(settings.api_base_url)


def _token() -> str | None:
    return st.session_state.get("access_token")


def _auth(settings: FrontendSettings) -> SupabaseAuth | None:
    if not settings.supabase_url or not settings.supabase_publishable_key:
        return None
    return SupabaseAuth(settings.supabase_url, settings.supabase_publishable_key)


def _set_page(page: str) -> None:
    st.session_state.current_page = page
    st.rerun()


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
    return {
        "overall_ats_score": record["overall_score"], "maximum_score": 100.0,
        "methodology_version": "deterministic-ats-v1",
        "disclaimer": "This deterministic ATS indicator is not a hiring probability or a guarantee of selection.",
        "components": record.get("component_scores", {}), "matched_skills": record.get("matched_skills", []),
        "missing_skills": record.get("missing_skills", []), "matched_keywords": record.get("matched_keywords", []),
        "missing_keywords": record.get("missing_keywords", []), "relevant_resume_evidence": [], "areas_needing_improvement": [],
    }


def _validate_upload(uploaded_file: Any, job_description: str, max_bytes: int) -> str | None:
    if uploaded_file is None: return "Please upload a valid PDF or DOCX file."
    if not uploaded_file.name.lower().endswith((".pdf", ".docx")): return "Please upload a valid PDF or DOCX file."
    if uploaded_file.size > max_bytes: return f"The selected file exceeds the {max_bytes // (1024 * 1024)} MiB upload limit."
    if not job_description.strip(): return "Enter a job description before continuing."
    return None


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


def render_sign_in(settings: FrontendSettings) -> None:
    st.markdown("<div class='auth-brand'><span>RESUME INTELLIGENCE</span><h1>AI Resume ATS</h1><p>Understand how your resume aligns with the role before you apply.</p></div>", unsafe_allow_html=True)
    auth = _auth(settings)
    if auth is None:
        st.error("Authentication is not configured. Add the Supabase URL and publishable key to the local environment.")
        return
    login_tab, signup_tab = st.tabs(["Sign in", "Create account"])
    with login_tab:
        with st.form("login_form", clear_on_submit=False):
            email = st.text_input("Email address", key="login_email", placeholder="you@example.com")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True, disabled=st.session_state.get("auth_busy", False))
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
        if st.button("Forgot password?", key="show_reset", use_container_width=True):
            st.session_state.show_reset = True
        if st.session_state.get("show_reset"):
            reset_email = st.text_input("Email for password reset", key="reset_email")
            if st.button("Send reset email", key="reset_password"):
                if not _valid_email(reset_email): st.error("Enter a valid email address.")
                else:
                    if _run_auth(lambda: auth.reset_password(reset_email.strip())):
                        st.success("If an account exists for this email, a password reset message has been sent.")
    with signup_tab:
        st.caption("Use at least 8 characters with a mix of letters, numbers, and symbols where your Supabase policy requires it.")
        with st.form("signup_form", clear_on_submit=False):
            email = st.text_input("Email address", key="signup_email", placeholder="you@example.com")
            password = st.text_input("Password", type="password", key="signup_password")
            submitted = st.form_submit_button("Create account", type="primary", use_container_width=True, disabled=st.session_state.get("auth_busy", False))
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
    if st.button("Continue without an account", use_container_width=True):
        st.session_state.guest_mode = True
        _set_page("Analyze Resume")


def render_dashboard(client: ApiClient) -> None:
    st.title("Good to see you")
    st.caption("Your resume alignment workspace")
    if not _token():
        st.info("Guest mode supports stateless analysis. Sign in to save your work and access history.")
        if st.button("Analyze a resume", type="primary"): _set_page("Analyze Resume")
        return
    try: rows = client.history(_token(), limit=100).get("analyses", [])
    except ApiError as exc: st.error(_friendly_api_error(exc)); return
    scores = [float(row["overall_score"]) for row in rows]
    latest = scores[0] if scores else None
    a, b, c = st.columns(3)
    a.metric("Resumes analyzed", len(rows)); b.metric("Average ATS score", f"{sum(scores) / len(scores):.1f}/100" if scores else "—"); c.metric("Latest ATS score", f"{latest:.1f}/100" if latest is not None else "—")
    left, right = st.columns((2, 1))
    with left:
        st.subheader("Recent analyses")
        if rows:
            for row in rows[:5]: st.markdown(f"**{row.get('filename') or 'Untitled resume'}**  \\n+{row['overall_score']:.1f}/100 · {row.get('created_at', '')}")
        else: st.info("No saved analyses yet. Start with your first resume.")
    with right:
        st.subheader("Quick actions")
        if st.button("Analyze new resume", type="primary", use_container_width=True): _set_page("Analyze Resume")
        if st.button("View history", use_container_width=True): _set_page("Analysis History")


def render_analyze(settings: FrontendSettings, client: ApiClient) -> None:
    st.title("Analyze resume")
    st.caption("A deterministic alignment score—not a hiring probability. Files are sent as bounded raw bytes and are not retained by the frontend.")
    nonce = st.session_state.get("upload_nonce", 0)
    upload = st.file_uploader("Upload your resume", type=["pdf", "docx"], key=f"resume_upload_{nonce}", help=f"PDF or DOCX, up to {settings.max_upload_bytes // (1024 * 1024)} MiB")
    if upload:
        file_col, remove_col = st.columns((4, 1))
        file_col.success(f"{upload.name} · {upload.size / 1024:.1f} KB")
        if remove_col.button("Remove file", use_container_width=True):
            st.session_state.upload_nonce = nonce + 1; st.rerun()
    job_description = st.text_area("Job description", key="job_description", height=230, max_chars=20_000, placeholder="Paste the role requirements, responsibilities, and preferred qualifications.")
    st.caption(f"{len(job_description):,} / 20,000 characters")
    analyze_col, feedback_col = st.columns(2)
    busy = st.session_state.get("analysis_busy", False)
    analyze_clicked = analyze_col.button("Analyze resume", type="primary", use_container_width=True, disabled=busy)
    feedback_clicked = feedback_col.button("Generate AI feedback", use_container_width=True, disabled=busy, help="Runs the same ATS analysis plus optional AI feedback.")
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
        st.divider(); render_analysis(result["analysis"])
        if result.get("feedback"): render_feedback(result["feedback"])


def render_history(client: ApiClient) -> None:
    st.title("Analysis history")
    if not _token():
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
    if not _token(): st.info("You are using guest mode. Sign in to manage a profile and saved analyses."); return
    st.subheader("Account")
    st.write(f"**Email:** {st.session_state.get('auth_email', 'Authenticated user')}")
    st.caption("Your access is scoped by Supabase Auth and database row-level security.")
    auth = _auth(settings)
    if st.button("Send password reset email") and auth:
        try: auth.reset_password(st.session_state.get("auth_email", "")); st.success("If the account is available, a reset email has been sent.")
        except AuthError as exc: st.error(str(exc))
    if st.button("Sign out", type="primary"):
        if auth: auth.logout()
        clear_session(st.session_state); st.session_state.pop("guest_mode", None); st.rerun()


def _css() -> None:
    st.markdown("""<style>
    .stApp{background:#f7f8fc;color:#152238}.block-container{max-width:1180px;padding-top:2rem;padding-bottom:3rem}
    [data-testid='stSidebar']{background:#111c30}.auth-brand{max-width:620px;margin:7vh auto 1rem;padding:2.2rem;border:1px solid #e2e6ef;border-radius:18px;background:#fff;box-shadow:0 10px 28px rgba(18,32,56,.08)}
    .auth-brand span{color:#be3443;font-size:.75rem;font-weight:700;letter-spacing:.15em}.auth-brand h1{color:#111c30;margin:.5rem 0}.auth-brand p{color:#516074}.stButton>button{border-radius:8px;font-weight:600}.stMetric{background:#fff;border:1px solid #e2e6ef;border-radius:12px;padding:1rem}
    </style>""", unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="AI Resume ATS", page_icon="📄", layout="wide")
    _css(); settings = load_frontend_settings()
    if not _token() and not st.session_state.get("guest_mode"):
        render_sign_in(settings); return
    client = _client(settings); current = st.session_state.get("current_page", "Dashboard")
    with st.sidebar:
        st.title("AI Resume ATS"); st.caption("Resume intelligence")
        status = backend_status(settings.api_base_url, st.session_state.get("status_bust", 0))
        st.caption(f"Backend: {'Online' if status['live'] else 'Offline'} · Model ready: {'Yes' if status['ready'] else 'Not ready'}")
        if st.button("Check status", use_container_width=True):
            st.session_state.status_bust = st.session_state.get("status_bust", 0) + 1; st.rerun()
        page = st.radio("Navigation", PAGES, index=PAGES.index(current), label_visibility="collapsed")
        if page != current: st.session_state.current_page = page
        st.divider(); st.caption(st.session_state.get("auth_email", "Guest mode"))
        if _token() and st.button("Sign out", use_container_width=True):
            auth = _auth(settings)
            if auth: auth.logout()
            clear_session(st.session_state); st.session_state.pop("guest_mode", None); st.rerun()
    {"Dashboard": lambda: render_dashboard(client), "Analyze Resume": lambda: render_analyze(settings, client), "Analysis History": lambda: render_history(client), "Profile": lambda: render_profile(settings)}[page]()


if __name__ == "__main__": main()
