"""Supabase Auth wrapper; tokens remain only in Streamlit session state."""

from __future__ import annotations

from typing import Any

from supabase import create_client


class AuthError(RuntimeError):
    """A safe, user-facing Supabase authentication failure."""


def _safe_auth_message(exc: Exception, action: str) -> str:
    """Classify provider failures without surfacing provider payloads or secrets."""
    detail = str(exc).casefold()
    if "email not confirmed" in detail or "not confirmed" in detail:
        return "Confirm your email address, then sign in."
    if "invalid login" in detail or "invalid credentials" in detail:
        return "The email or password is incorrect."
    if "already registered" in detail or "already exists" in detail:
        return "An account already exists for this email. Try signing in."
    if "password" in detail and ("weak" in detail or "least" in detail or "characters" in detail):
        return "Choose a stronger password that meets the displayed requirements."
    if "timeout" in detail or "connection" in detail or "network" in detail:
        return "The authentication service is temporarily unavailable. Please try again."
    return f"Unable to {action}. Please try again."


class SupabaseAuth:
    def __init__(self, url: str, publishable_key: str) -> None:
        self._client = create_client(url, publishable_key)

    def login(self, email: str, password: str) -> str:
        try:
            response = self._client.auth.sign_in_with_password({"email": email, "password": password})
            if response.session is None:
                raise AuthError("Sign-in did not return a session. Confirm your email and try again.")
            return response.session.access_token
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            raise AuthError(_safe_auth_message(exc, "sign in")) from exc

    def signup(self, email: str, password: str) -> bool:
        try:
            response = self._client.auth.sign_up({"email": email, "password": password})
            return response.session is not None
        except Exception as exc:
            raise AuthError(_safe_auth_message(exc, "create the account")) from exc

    def reset_password(self, email: str) -> None:
        try:
            self._client.auth.reset_password_for_email(email)
        except Exception as exc:
            raise AuthError(_safe_auth_message(exc, "send the password reset email")) from exc

    def logout(self) -> None:
        try: self._client.auth.sign_out()
        except Exception: pass


def clear_session(session_state: Any) -> None:
    for key in ("access_token", "auth_email", "latest_result", "latest_upload"):
        session_state.pop(key, None)
