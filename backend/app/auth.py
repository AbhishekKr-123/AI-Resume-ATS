"""Bearer-token dependencies backed by Supabase Auth's verified user endpoint."""

from __future__ import annotations

import anyio
from fastapi import HTTPException, Request

from .database_service import AuthenticatedUser, AuthenticationError, SupabaseService


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
    if token is None:
        return None
    service: SupabaseService | None = getattr(request.app.state, "supabase_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    try:
        user = await anyio.to_thread.run_sync(service.verify_user, token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials.") from exc
    return user, token


async def require_authenticated_user(request: Request) -> tuple[AuthenticatedUser, str]:
    authenticated = await optional_authenticated_user(request)
    if authenticated is None:
        raise HTTPException(status_code=401, detail="Authentication credentials are required.")
    return authenticated
