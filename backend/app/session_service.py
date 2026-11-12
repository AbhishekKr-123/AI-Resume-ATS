"""Process-local authenticated sessions with serialized token refresh."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from threading import Lock
from time import time
from typing import Callable
from uuid import UUID


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class InvalidSession(RuntimeError):
    """Raised for missing, expired, revoked, or unrefreshable sessions."""


@dataclass(frozen=True)
class RefreshedTokens:
    user_id: UUID
    email: str
    access_token: str
    refresh_token: str
    expires_at: float
    display_name: str | None = None


@dataclass(frozen=True)
class ResolvedSession:
    user_id: UUID
    email: str
    access_token: str
    csrf_token: str
    absolute_expires_at: float
    display_name: str | None = None


@dataclass
class _SessionRecord:
    user_id: UUID
    email: str
    access_token: str
    refresh_token: str
    access_expires_at: float
    idle_expires_at: float
    absolute_expires_at: float
    csrf_token: str
    display_name: str | None = None
    lock: Lock = field(default_factory=Lock)
    revoked: bool = False


class SessionStore:
    """One-process store; browser identifiers are never retained in plaintext."""

    def __init__(
        self,
        idle_ttl_seconds: float = 24 * 60 * 60,
        absolute_ttl_seconds: float = 7 * 24 * 60 * 60,
        refresh_skew_seconds: float = 60,
        clock: Callable[[], float] = time,
    ) -> None:
        if idle_ttl_seconds <= 0 or absolute_ttl_seconds <= 0 or refresh_skew_seconds < 0:
            raise ValueError("Session lifetimes must be positive and refresh skew non-negative.")
        if idle_ttl_seconds > absolute_ttl_seconds:
            raise ValueError("Idle session lifetime cannot exceed absolute lifetime.")
        self._idle_ttl_seconds = idle_ttl_seconds
        self._absolute_ttl_seconds = absolute_ttl_seconds
        self._refresh_skew_seconds = refresh_skew_seconds
        self._clock = clock
        self._lock = Lock()
        self._sessions: dict[str, _SessionRecord] = {}

    def create(
        self,
        user_id: UUID,
        email: str,
        access_token: str,
        refresh_token: str,
        access_expires_at: float,
        display_name: str | None = None,
    ) -> str:
        if not email or not access_token or not refresh_token:
            raise ValueError("A verified user and both Supabase tokens are required.")
        now = self._clock()
        session_id = token_urlsafe(32)
        record = _SessionRecord(
            user_id=user_id,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            idle_expires_at=min(now + self._idle_ttl_seconds, now + self._absolute_ttl_seconds),
            absolute_expires_at=now + self._absolute_ttl_seconds,
            csrf_token=token_urlsafe(32),
            display_name=display_name,
        )
        with self._lock:
            self._purge_locked(now)
            self._sessions[_digest(session_id)] = record
        return session_id

    def resolve(
        self,
        session_id: str,
        refresh: Callable[[str], RefreshedTokens],
    ) -> ResolvedSession:
        session_digest = _digest(session_id)
        record = self._record(session_digest)
        with record.lock:
            now = self._clock()
            if not self._is_current(session_digest, record) or self._expired(record, now):
                self._revoke_record(session_digest, record)
                raise InvalidSession("The session is invalid or expired.")
            if record.access_expires_at <= now + self._refresh_skew_seconds:
                try:
                    updated = refresh(record.refresh_token)
                    if updated.user_id != record.user_id or not updated.access_token or not updated.refresh_token:
                        raise InvalidSession("The refreshed session identity is invalid.")
                    record.email = updated.email or record.email
                    record.display_name = updated.display_name or record.display_name
                    record.access_token = updated.access_token
                    record.refresh_token = updated.refresh_token
                    record.access_expires_at = updated.expires_at
                except Exception as exc:
                    self._revoke_record(session_digest, record)
                    if isinstance(exc, InvalidSession):
                        raise
                    raise InvalidSession("The session could not be refreshed.") from exc
            record.idle_expires_at = min(now + self._idle_ttl_seconds, record.absolute_expires_at)
            return ResolvedSession(
                user_id=record.user_id,
                email=record.email,
                access_token=record.access_token,
                csrf_token=record.csrf_token,
                absolute_expires_at=record.absolute_expires_at,
                display_name=record.display_name,
            )

    def revoke(self, session_id: str) -> _SessionRecord | None:
        session_digest = _digest(session_id)
        with self._lock:
            record = self._sessions.get(session_digest)
        if record is None:
            return None
        with record.lock:
            with self._lock:
                if self._sessions.get(session_digest) is not record:
                    return None
                self._sessions.pop(session_digest, None)
                record.revoked = True
                record.display_name = None
            return record

    def validate_csrf(self, session_id: str, csrf_token: str) -> bool:
        try:
            record = self._record(_digest(session_id))
        except InvalidSession:
            return False
        with record.lock:
            return not record.revoked and compare_digest(record.csrf_token, csrf_token) and not self._expired(record, self._clock())

    def _record(self, session_digest: str) -> _SessionRecord:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            record = self._sessions.get(session_digest)
        if record is None:
            raise InvalidSession("The session is invalid or expired.")
        return record

    def _is_current(self, session_digest: str, record: _SessionRecord) -> bool:
        with self._lock:
            return self._sessions.get(session_digest) is record and not record.revoked

    @staticmethod
    def _expired(record: _SessionRecord, now: float) -> bool:
        return record.revoked or record.idle_expires_at <= now or record.absolute_expires_at <= now

    def _revoke_record(self, session_digest: str, record: _SessionRecord) -> None:
        with self._lock:
            if self._sessions.get(session_digest) is record:
                self._sessions.pop(session_digest, None)
            record.revoked = True
            record.display_name = None

    def _purge_locked(self, now: float) -> None:
        for session_digest, record in list(self._sessions.items()):
            if self._expired(record, now):
                self._sessions.pop(session_digest, None)
                record.revoked = True
                record.display_name = None
