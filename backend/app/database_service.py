"""Supabase Auth verification and RLS-scoped analysis persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx


class AuthenticationError(RuntimeError):
    """Safe authentication failure for bearer-token verification."""


class DatabaseError(RuntimeError):
    """Safe persistence failure that never exposes Supabase internals."""


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID


class SupabaseService:
    """Reusable publishable-key HTTP client; every CRUD call carries the user JWT."""

    def __init__(self, url: str, publishable_key: str, timeout_seconds: float) -> None:
        self._publishable_key = publishable_key
        self._client = httpx.Client(base_url=url.rstrip("/"), timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def _headers(self, token: str) -> dict[str, str]:
        return {"apikey": self._publishable_key, "Authorization": f"Bearer {token}"}

    def verify_user(self, token: str) -> AuthenticatedUser:
        try:
            response = self._client.get("/auth/v1/user", headers=self._headers(token))
        except httpx.HTTPError as exc:
            raise AuthenticationError("Authentication is currently unavailable.") from exc
        if response.status_code != 200:
            raise AuthenticationError("Invalid or expired access token.")
        try:
            user_id = UUID(response.json()["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError("Invalid access-token identity.") from exc
        return AuthenticatedUser(id=user_id)

    def _request(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        headers = self._headers(token) | kwargs.pop("headers", {})
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise DatabaseError("Analysis storage is currently unavailable.") from exc
        if response.status_code >= 400:
            raise DatabaseError("Analysis storage operation failed.")
        try:
            return response.json() if response.content else None
        except ValueError as exc:
            raise DatabaseError("Analysis storage returned an invalid response.") from exc

    @staticmethod
    def _record(user_id: UUID, analysis: dict[str, Any], filename: str | None, feedback: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "user_id": str(user_id),
            "overall_score": analysis["overall_ats_score"],
            "component_scores": analysis["components"],
            "matched_skills": analysis["matched_skills"],
            "missing_skills": analysis["missing_skills"],
            "matched_keywords": analysis["matched_keywords"],
            "missing_keywords": analysis["missing_keywords"],
            "feedback": feedback,
            "filename": filename,
        }

    def insert_analysis(self, user: AuthenticatedUser, token: str, analysis: dict[str, Any], filename: str | None, feedback: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = self._request("POST", "/rest/v1/analyses", token, headers={"Prefer": "return=representation"}, json=self._record(user.id, analysis, filename, feedback))
        if not isinstance(rows, list) or not rows:
            raise DatabaseError("Analysis storage did not return a record.")
        return rows[0]

    def list_analyses(self, user: AuthenticatedUser, token: str, limit: int, offset: int) -> list[dict[str, Any]]:
        result = self._request("GET", "/rest/v1/analyses", token, params={"select": "id,overall_score,component_scores,matched_skills,missing_skills,matched_keywords,missing_keywords,feedback,filename,created_at", "user_id": f"eq.{user.id}", "order": "created_at.desc", "limit": str(limit), "offset": str(offset)})
        return result if isinstance(result, list) else []

    def get_analysis(self, user: AuthenticatedUser, token: str, analysis_id: UUID) -> dict[str, Any] | None:
        result = self._request("GET", "/rest/v1/analyses", token, params={"select": "id,overall_score,component_scores,matched_skills,missing_skills,matched_keywords,missing_keywords,feedback,filename,created_at", "id": f"eq.{analysis_id}", "user_id": f"eq.{user.id}", "limit": "1"})
        return result[0] if isinstance(result, list) and result else None

    def delete_analysis(self, user: AuthenticatedUser, token: str, analysis_id: UUID) -> bool:
        result = self._request("DELETE", "/rest/v1/analyses", token, headers={"Prefer": "return=representation"}, params={"id": f"eq.{analysis_id}", "user_id": f"eq.{user.id}"})
        return bool(result)
