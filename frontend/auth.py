"""Supabase Auth wrapper; tokens remain only in Streamlit session state."""

from __future__ import annotations

import re
import logging
from typing import Any

from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

from frontend.config import is_publishable_key
from frontend.oauth_storage import OAuthFlowStorage, consume_verifier, new_flow_id

logger = logging.getLogger(__name__)

_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(code|token|access_token|refresh_token|authorization|apikey|api_key|secret)\b\s*([=:])\s*[^\s,&]+"
)
_JWT = re.compile(r"\beyJ[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+){2}\b")


class AuthError(RuntimeError):
    """A safe, user-facing Supabase authentication failure."""


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    """Strip accidental surrounding whitespace and reject invalid input locally."""
    normalized = email.strip()
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise AuthError("Enter a valid email address.")
    return normalized


def _safe_auth_message(exc: Exception, action: str) -> str:
    """Classify provider failures without surfacing provider payloads or secrets."""
    detail = f"{getattr(exc, 'code', '')} {str(exc)}".casefold()
    if "over_email_send_rate_limit" in detail or "email send rate limit" in detail:
        return "Too many confirmation or reset emails were requested. Please wait before trying again."
    if "email_address_invalid" in detail or "email address invalid" in detail:
        return "Enter a valid email address."
    if "access_denied" in detail or "oauth" in detail or "google" in detail:
        return "Google sign-in could not be completed. Please try again."
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


def _safe_diagnostic_message(exc: Exception) -> str:
    """Keep terminal OAuth diagnostics useful without logging credential material."""
    message = _SENSITIVE_VALUE.sub(r"\1\2<redacted>", str(exc))
    message = _JWT.sub("<redacted-jwt>", message)
    return message[:500]


class SupabaseAuth:
    def __init__(self, url: str, publishable_key: str) -> None:
        if not is_publishable_key(publishable_key):
            raise AuthError("Supabase authentication is misconfigured. A publishable key is required.")
        self._url = url
        self._publishable_key = publishable_key
        self._client = create_client(url, publishable_key)

    def login(self, email: str, password: str) -> str:
        try:
            response = self._client.auth.sign_in_with_password({"email": normalize_email(email), "password": password})
            if response.session is None:
                raise AuthError("Sign-in did not return a session. Confirm your email and try again.")
            return response.session.access_token
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            raise AuthError(_safe_auth_message(exc, "sign in")) from exc

    def signup(self, email: str, password: str) -> bool:
        try:
            response = self._client.auth.sign_up({"email": normalize_email(email), "password": password})
            return response.session is not None
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            raise AuthError(_safe_auth_message(exc, "create the account")) from exc

    def reset_password(self, email: str) -> None:
        try:
            self._client.auth.reset_password_for_email(normalize_email(email))
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            raise AuthError(_safe_auth_message(exc, "send the password reset email")) from exc

    def google_authorization_url(self, redirect_url: str) -> tuple[str, str]:
        """Start OAuth through Supabase; the Google secret remains in Supabase."""
        try:
            flow_id = new_flow_id()
            separator = "&" if "?" in redirect_url else "?"
            callback_url = f"{redirect_url}{separator}oauth_flow={flow_id}"
            oauth_client = create_client(
                self._url,
                self._publishable_key,
                SyncClientOptions(storage=OAuthFlowStorage(flow_id), flow_type="pkce"),
            )
            response = oauth_client.auth.sign_in_with_oauth(
                {"provider": "google", "options": {"redirect_to": callback_url}}
            )
            if not response.url:
                raise AuthError("Google sign-in could not be started. Please try again.")
            return response.url, flow_id
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            raise AuthError(_safe_auth_message(exc, "start Google sign-in")) from exc

    def complete_google_callback(self, authorization_code: str, flow_id: str) -> tuple[str, str]:
        """Exchange the one-time code and verify the returned Supabase user."""
        try:
            code_verifier = consume_verifier(flow_id)
            if not code_verifier:
                logger.warning("Supabase OAuth exchange stopped: PKCE verifier is missing, expired, or already consumed.")
                raise AuthError("Google sign-in session expired or was already used. Start Google sign-in again.")
            response = self._client.auth.exchange_code_for_session(
                {"auth_code": authorization_code, "code_verifier": code_verifier}
            )
            if response.session is None or not response.session.access_token:
                raise AuthError("Google sign-in did not return a session. Please try again.")
            verified = self._client.auth.get_user(response.session.access_token)
            user = getattr(verified, "user", None)
            email = getattr(user, "email", None)
            if user is None or not email:
                raise AuthError("Google sign-in did not return a verified user. Please try again.")
            return response.session.access_token, email
        except Exception as exc:
            if isinstance(exc, AuthError):
                raise
            logger.warning(
                "Supabase OAuth authorization-code exchange failed (%s): %s",
                type(exc).__name__,
                _safe_diagnostic_message(exc),
            )
            raise AuthError(_safe_auth_message(exc, "complete Google sign-in")) from exc

    def logout(self) -> None:
        try: self._client.auth.sign_out()
        except Exception: pass


def clear_session(session_state: Any) -> None:
    for key in ("access_token", "auth_email", "latest_result", "latest_upload", "oauth_error", "oauth_success"):
        session_state.pop(key, None)
