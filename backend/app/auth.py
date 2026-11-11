"""Bearer-token dependencies backed by Supabase Auth's verified user endpoint."""

from __future__ import annotations

import anyio
from fastapi import HTTPException, Request

from .auth_routes import session_cookie_name
from .database_service import AuthenticatedUser, AuthenticationError, SupabaseService
from .session_service import InvalidSession, SessionStore


def _bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization")
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid authentication credentials.")
    return token.strip()


async def optional_authenticated_user(request: Request) -> tuple[AuthenticatedUser, str] | None:
    token = _bearer_token(request)
    cookie_authenticated = False
    session_store: SessionStore | None = None
    session_id: str | None = None
    resolved = None
    service: SupabaseService | None = getattr(request.app.state, "supabase_service", None)
    if token is None:
        settings = getattr(request.app.state, "settings", None)
        session_store: SessionStore | None = getattr(request.app.state, "session_store", None)
        provider = getattr(request.app.state, "oauth_provider", None)
        if settings is None or session_store is None or provider is None:
            return None
        session_id = request.cookies.get(session_cookie_name(settings))
        if session_id is None:
            return None
        try:
            resolved = await anyio.to_thread.run_sync(session_store.resolve, session_id, provider.refresh)
        except InvalidSession as exc:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials.") from exc
        token = resolved.access_token
        cookie_authenticated = True
    if service is None:
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    try:
        user = await anyio.to_thread.run_sync(service.verify_user, token)
    except AuthenticationError as exc:
        if cookie_authenticated and session_store is not None and session_id is not None:
            session_store.revoke(session_id)
        raise HTTPException(status_code=401, detail="Invalid authentication credentials.") from exc
    if cookie_authenticated and resolved is not None and user.id != resolved.user_id:
        session_store.revoke(session_id)
        raise HTTPException(status_code=401, detail="Invalid authentication credentials.")
    return user, token


async def require_authenticated_user(request: Request) -> tuple[AuthenticatedUser, str]:
    authenticated = await optional_authenticated_user(request)
    if authenticated is None:
        raise HTTPException(status_code=401, detail="Authentication credentials are required.")
    return authenticated
