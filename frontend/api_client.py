"""Typed, raw-body client for the existing FastAPI backend."""

from __future__ import annotations

from typing import Any

import httpx

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ApiClient:
    def __init__(self, base_url: str, timeout_seconds: float = 90, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    @staticmethod
    def content_type(filename: str) -> str:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix == "pdf": return PDF_MIME
        if suffix == "docx": return DOCX_MIME
        raise ApiError("Only PDF and DOCX files are supported.")

    @staticmethod
    def _headers(filename: str | None = None, job_description: str | None = None, token: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if filename: headers["X-Upload-Filename"] = filename
        if job_description is not None: headers["X-Job-Description"] = job_description
        if token: headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError("The backend is unavailable. Start FastAPI and try again.") from exc
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise ApiError(error.get("message") or detail or "The request could not be completed.", response.status_code, error.get("code"))
        return payload

    def analyze(self, file_bytes: bytes, filename: str, job_description: str, token: str | None = None) -> dict:
        headers = self._headers(filename, job_description, token)
        headers["Content-Type"] = self.content_type(filename)
        return self._request("POST", "/resumes/analyze", content=file_bytes, headers=headers)

    def feedback(self, file_bytes: bytes, filename: str, job_description: str, token: str | None = None) -> dict:
        headers = self._headers(filename, job_description, token)
        headers["Content-Type"] = self.content_type(filename)
        return self._request("POST", "/resumes/feedback", content=file_bytes, headers=headers)

    def health_live(self) -> dict:
        return self._request("GET", "/health/live")

    def health_ready(self) -> dict:
        return self._request("GET", "/health/ready")

    def history(self, token: str, limit: int = 20, offset: int = 0) -> dict:
        return self._request("GET", "/analyses", headers=self._headers(token=token), params={"limit": limit, "offset": offset})

    def get_analysis(self, analysis_id: str, token: str) -> dict:
        return self._request("GET", f"/analyses/{analysis_id}", headers=self._headers(token=token))

    def delete_analysis(self, analysis_id: str, token: str) -> dict:
        return self._request("DELETE", f"/analyses/{analysis_id}", headers=self._headers(token=token))
