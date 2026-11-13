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


class ProfileStorageError(DatabaseError):
    """Safe profile persistence failure."""


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    email: str | None = None


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
            payload = response.json()
            user_id = UUID(payload["id"])
            email_value = payload.get("email")
            email = email_value.strip() if isinstance(email_value, str) and email_value.strip() else None
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError("Invalid access-token identity.") from exc
        return AuthenticatedUser(id=user_id, email=email)

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

    def get_profile(self, user: AuthenticatedUser, token: str) -> dict[str, Any] | None:
        try:
            result = self._request(
                "GET",
                "/rest/v1/profiles",
                token,
                params={
                    "select": "user_id,full_name,linkedin_url,github_url,leetcode_url,portfolio_url,created_at,updated_at",
                    "user_id": f"eq.{user.id}",
                    "limit": "1",
                },
            )
        except DatabaseError as exc:
            raise ProfileStorageError("Profile storage is currently unavailable.") from exc
        return result[0] if isinstance(result, list) and result else None

    def upsert_profile(self, user: AuthenticatedUser, token: str, profile: dict[str, Any]) -> dict[str, Any]:
        record = {
            "user_id": str(user.id),
            "full_name": profile["full_name"],
            "linkedin_url": profile.get("linkedin_url"),
            "github_url": profile.get("github_url"),
            "leetcode_url": profile.get("leetcode_url"),
            "portfolio_url": profile.get("portfolio_url"),
        }
        try:
            rows = self._request(
                "POST",
                "/rest/v1/profiles",
                token,
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                params={"on_conflict": "user_id"},
                json=record,
            )
        except DatabaseError as exc:
            raise ProfileStorageError("Profile storage is currently unavailable.") from exc
        if not isinstance(rows, list) or not rows:
            raise ProfileStorageError("Profile storage did not return a record.")
        return rows[0]
