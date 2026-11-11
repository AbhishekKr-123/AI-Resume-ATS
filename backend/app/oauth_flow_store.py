"""FastAPI-owned, process-local PKCE and OAuth state storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from secrets import token_urlsafe
from secrets import compare_digest
from threading import Lock
from time import monotonic
from typing import Callable

from supabase_auth._sync.storage import SyncSupportedStorage


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _FlowRecord:
    state_digest: str
    transaction_digest: str
    return_to: str
    expires_at: float
    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConsumedOAuthFlow:
    code_verifier: str
    return_to: str


class OAuthFlowStore:
    """Single-use OAuth transactions scoped to one FastAPI process."""

    def __init__(self, ttl_seconds: float = 600, clock: Callable[[], float] = monotonic) -> None:
        if ttl_seconds <= 0:
            raise ValueError("OAuth flow lifetime must be positive.")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._flows: dict[str, _FlowRecord] = {}

    def begin(self, return_to: str) -> tuple[str, str, str, SyncSupportedStorage]:
        """Create a flow ID, state, transaction cookie, and PKCE SDK storage."""
        flow_id = token_urlsafe(32)
        state = token_urlsafe(32)
        transaction = token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            self._flows[_digest(flow_id)] = _FlowRecord(
                state_digest=_digest(state),
                transaction_digest=_digest(transaction),
                return_to=return_to,
                expires_at=now + self._ttl_seconds,
            )
        return flow_id, state, transaction, _FlowStorage(self, flow_id)

    def consume(self, flow_id: str, state: str, transaction: str) -> ConsumedOAuthFlow | None:
        """Atomically validate and consume one callback transaction."""
        now = self._clock()
        flow_digest = _digest(flow_id)
        with self._lock:
            self._purge_locked(now)
            record = self._flows.get(flow_digest)
            if record is None:
                return None
            if not compare_digest(record.state_digest, _digest(state)) or not compare_digest(record.transaction_digest, _digest(transaction)):
                return None
            self._flows.pop(flow_digest, None)
            verifier = next(
                (value.partition("/")[0] for key, value in record.values.items() if key.endswith("-code-verifier")),
                None,
            )
            if not verifier:
                return None
            return ConsumedOAuthFlow(code_verifier=verifier, return_to=record.return_to)

    def _set_item(self, flow_id: str, key: str, value: str) -> None:
        if not key.endswith("-code-verifier"):
            return
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            record = self._flows.get(_digest(flow_id))
            if record is not None:
                record.values[key] = value

    def _get_item(self, flow_id: str, key: str) -> str | None:
        if not key.endswith("-code-verifier"):
            return None
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            record = self._flows.get(_digest(flow_id))
            return record.values.get(key) if record else None

    def _remove_item(self, flow_id: str, key: str) -> None:
        with self._lock:
            record = self._flows.get(_digest(flow_id))
            if record is not None:
                record.values.pop(key, None)

    def _purge_locked(self, now: float) -> None:
        for flow_digest, record in list(self._flows.items()):
            if record.expires_at <= now:
                self._flows.pop(flow_digest, None)


class _FlowStorage(SyncSupportedStorage):
    """Supabase SDK adapter that accepts only PKCE verifier values."""

    def __init__(self, store: OAuthFlowStore, flow_id: str) -> None:
        self._store = store
        self._flow_id = flow_id

    def get_item(self, key: str) -> str | None:
        return self._store._get_item(self._flow_id, key)

    def set_item(self, key: str, value: str) -> None:
        self._store._set_item(self._flow_id, key, value)

    def remove_item(self, key: str) -> None:
        self._store._remove_item(self._flow_id, key)
