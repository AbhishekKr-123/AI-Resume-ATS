"""Browser-facing OAuth endpoints backed by process-local secure sessions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

from .config import Settings
from .oauth_flow_store import OAuthFlowStore
from .session_service import InvalidSession, RefreshedTokens, ResolvedSession, SessionStore

logger = logging.getLogger(__name__)


def session_cookie_name(settings: Settings) -> str:
    return "__Host-ats_session" if settings.auth_cookie_secure else "ats_session_dev"


@dataclass(frozen=True)
class ProviderSession:
    user_id: UUID
    email: str
    access_token: str
    refresh_token: str
    expires_at: float
    display_name: str | None = None


def _verified_display_name(user: Any) -> str | None:
    """Resolve safe presentation metadata without deriving identity from email."""
    sources = (getattr(user, "user_metadata", None), getattr(user, "profile", None))
    for source in sources:
        if not isinstance(source, dict):
            continue
        for field in ("full_name", "name"):
            value = source.get(field)
            if isinstance(value, str):
                normalized = " ".join(value.split())
                if normalized and "@" not in normalized:
                    return normalized
    return None


class OAuthProvider(Protocol):
    def authorization_url(self, callback_url: str, storage: Any) -> str: ...
    def exchange(self, code: str, verifier: str) -> ProviderSession: ...
    def refresh(self, refresh_token: str) -> RefreshedTokens: ...
    def sign_out(self, access_token: str, refresh_token: str) -> None: ...


class SupabaseOAuthProvider:
    """Fresh Supabase clients prevent user sessions from being shared globally."""

    def __init__(self, url: str, publishable_key: str) -> None:
        self._url = url
        self._key = publishable_key

    def authorization_url(self, callback_url: str, storage: Any) -> str:
        client = create_client(
            self._url,
            self._key,
            SyncClientOptions(storage=storage, flow_type="pkce"),
        )
        response = client.auth.sign_in_with_oauth(
            {"provider": "google", "options": {"redirect_to": callback_url}}
        )
        if not response.url:
            raise RuntimeError("Supabase did not return an OAuth authorization URL.")
        return response.url

    def exchange(self, code: str, verifier: str) -> ProviderSession:
        client = create_client(self._url, self._key)
        response = client.auth.exchange_code_for_session(
            {"auth_code": code, "code_verifier": verifier}
        )
        session = response.session
        if session is None or not session.access_token or not session.refresh_token:
            raise RuntimeError("Supabase did not return a complete session.")
        verified = client.auth.get_user(session.access_token)
        user = getattr(verified, "user", None)
        return self._provider_session(user, session)

    def refresh(self, refresh_token: str) -> RefreshedTokens:
        client = create_client(self._url, self._key)
        response = client.auth.refresh_session(refresh_token)
        session = response.session
        user = getattr(response, "user", None) or getattr(session, "user", None)
        provider = self._provider_session(user, session)
        return RefreshedTokens(
            provider.user_id,
            provider.email,
            provider.access_token,
            provider.refresh_token,
            provider.expires_at,
            provider.display_name,
        )

    def sign_out(self, access_token: str, refresh_token: str) -> None:
        client = create_client(self._url, self._key)
        client.auth.set_session(access_token, refresh_token)
        client.auth.sign_out()

    @staticmethod
    def _provider_session(user: Any, session: Any) -> ProviderSession:
        try:
            user_id = UUID(str(user.id))
            email = str(user.email)
            access_token = str(session.access_token)
            refresh_token = str(session.refresh_token)
            expires_at = float(session.expires_at)
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("Supabase returned an invalid authenticated session.") from exc
        if not email or not access_token or not refresh_token:
            raise RuntimeError("Supabase returned an incomplete authenticated session.")
        return ProviderSession(user_id, email, access_token, refresh_token, expires_at, _verified_display_name(user))


def _append_query(url: str, values: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(values.items())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def create_auth_router(
    settings: Settings,
    flow_store: OAuthFlowStore,
    session_store: SessionStore,
    provider: OAuthProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/auth")
    allowed_redirects = set(settings.auth_allowed_redirect_urls)
    allowed_origins = {
        f"{parts.scheme}://{parts.netloc}" for parts in map(urlsplit, allowed_redirects)
    }
    oauth_provider = provider
    if oauth_provider is None and settings.supabase_url and settings.supabase_publishable_key:
        oauth_provider = SupabaseOAuthProvider(settings.supabase_url, settings.supabase_publishable_key)
    session_cookie = session_cookie_name(settings)
    transaction_cookie = "__Host-ats_oauth" if settings.auth_cookie_secure else "ats_oauth_dev"
    cookie_max_age = int(settings.auth_session_absolute_seconds)

    def require_provider() -> OAuthProvider:
        if oauth_provider is None:
            raise HTTPException(status_code=503, detail="Google authentication is not configured.")
        return oauth_provider

    def set_cookie(response: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=True,
            secure=settings.auth_cookie_secure,
            samesite="lax",
            path=path,
        )

    def delete_cookie(response: Response, name: str, path: str = "/") -> None:
        response.delete_cookie(
            name,
            httponly=True,
            secure=settings.auth_cookie_secure,
            samesite="lax",
            path=path,
        )

    def safe_return_to(value: str | None) -> str:
        candidate = (value or settings.auth_allowed_redirect_urls[0]).rstrip("/")
        if candidate not in allowed_redirects:
            raise HTTPException(status_code=400, detail="The authentication return destination is not allowed.")
        return candidate

    def resolve_session(request: Request) -> tuple[str, ResolvedSession]:
        session_id = request.cookies.get(session_cookie)
        if not session_id:
            raise HTTPException(status_code=401, detail="Authentication credentials are required.")
        try:
            resolved = session_store.resolve(session_id, require_provider().refresh)
        except InvalidSession as exc:
            raise HTTPException(status_code=401, detail="The session is invalid or expired.") from exc
        return session_id, resolved

    @router.get("/google/start")
    def google_start(return_to: str | None = None) -> RedirectResponse:
        destination = safe_return_to(return_to)
        flow_id, state, transaction, storage = flow_store.begin(destination)
        callback = _append_query(
            f"{settings.auth_public_url}/auth/google/callback",
            {"flow": flow_id, "state": state},
        )
        try:
            authorization_url = require_provider().authorization_url(callback, storage)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Google OAuth authorization could not be started (%s).", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Google sign-in could not be started.") from exc
        response = RedirectResponse(authorization_url, status_code=302)
        set_cookie(response, transaction_cookie, transaction, 600)
        return response

    @router.get("/google/callback")
    def google_callback(
        request: Request,
        code: str | None = None,
        flow: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        transaction = request.cookies.get(transaction_cookie)
        if not flow or not state or not transaction:
            raise HTTPException(status_code=400, detail="The OAuth transaction is invalid or expired.")
        consumed = flow_store.consume(flow, state, transaction)
        if consumed is None:
            raise HTTPException(status_code=400, detail="The OAuth transaction is invalid or expired.")
        if error or not code:
            response = RedirectResponse(_append_query(consumed.return_to, {"auth": "error"}), status_code=302)
            delete_cookie(response, transaction_cookie)
            return response
        try:
            authenticated = require_provider().exchange(code, consumed.code_verifier)
            previous = request.cookies.get(session_cookie)
            if previous:
                session_store.revoke(previous)
            session_id = session_store.create(
                authenticated.user_id,
                authenticated.email,
                authenticated.access_token,
                authenticated.refresh_token,
                authenticated.expires_at,
                authenticated.display_name,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Google OAuth exchange failed (%s).", type(exc).__name__)
            response = RedirectResponse(_append_query(consumed.return_to, {"auth": "error"}), status_code=302)
            delete_cookie(response, transaction_cookie)
            return response
        response = RedirectResponse(_append_query(consumed.return_to, {"auth": "success"}), status_code=302)
        set_cookie(response, session_cookie, session_id, cookie_max_age)
        delete_cookie(response, transaction_cookie)
        return response

    @router.get("/session")
    def session_status(request: Request) -> dict[str, Any]:
        _, resolved = resolve_session(request)
        return {
            "authenticated": True,
            "email": resolved.email,
            "display_name": resolved.display_name,
            "csrf_token": resolved.csrf_token,
            "expires_at": resolved.absolute_expires_at,
        }

    @router.post("/logout", status_code=204)
    def logout(
        request: Request,
        origin: str | None = Header(default=None),
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> Response:
        if origin not in allowed_origins:
            raise HTTPException(status_code=403, detail="The request origin is not allowed.")
        session_id = request.cookies.get(session_cookie)
        if not session_id or not csrf_token or not session_store.validate_csrf(session_id, csrf_token):
            raise HTTPException(status_code=403, detail="The logout request is invalid.")
        revoked = session_store.revoke(session_id)
        if revoked is not None:
            try:
                require_provider().sign_out(revoked.access_token, revoked.refresh_token)
            except Exception as exc:
                logger.warning("Supabase sign-out failed after local revocation (%s).", type(exc).__name__)
        response = Response(status_code=204)
        delete_cookie(response, session_cookie)
        return response

    return router


class RedactOAuthAccessLog(logging.Filter):
    """Remove callback query data from Uvicorn's standard access-log tuple."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = str(record.args[2])
            if path.startswith("/auth/google/callback?"):
                values = list(record.args)
                values[2] = "/auth/google/callback?<redacted>"
                record.args = tuple(values)
        return True
