"""Short-lived server-side storage for Supabase PKCE verifiers."""

from __future__ import annotations

from secrets import token_urlsafe
from threading import Lock
from time import monotonic

from supabase_auth._sync.storage import SyncSupportedStorage

_TTL_SECONDS = 10 * 60
_lock = Lock()
_flows: dict[str, tuple[float, dict[str, str]]] = {}


def new_flow_id() -> str:
    return token_urlsafe(32)


def _purge(now: float) -> None:
    for flow_id, (expires, _) in list(_flows.items()):
        if expires <= now:
            _flows.pop(flow_id, None)


class OAuthFlowStorage(SyncSupportedStorage):
    """SDK storage that retains only PKCE verifiers, never OAuth sessions."""

    def __init__(self, flow_id: str) -> None:
        self._flow_id = flow_id

    def get_item(self, key: str) -> str | None:
        if not key.endswith("-code-verifier"):
            return None
        now = monotonic()
        with _lock:
            _purge(now)
            entry = _flows.get(self._flow_id)
            return entry[1].get(key) if entry else None

    def set_item(self, key: str, value: str) -> None:
        if not key.endswith("-code-verifier"):
            return
        now = monotonic()
        with _lock:
            _purge(now)
            _, values = _flows.get(self._flow_id, (0, {}))
            values[key] = value
            _flows[self._flow_id] = (now + _TTL_SECONDS, values)

    def remove_item(self, key: str) -> None:
        with _lock:
            entry = _flows.get(self._flow_id)
            if not entry:
                return
            entry[1].pop(key, None)
            if not entry[1]:
                _flows.pop(self._flow_id, None)


def consume_verifier(flow_id: str) -> str | None:
    """Atomically consume one verifier so callbacks cannot be replayed."""
    now = monotonic()
    with _lock:
        _purge(now)
        entry = _flows.pop(flow_id, None)
        if not entry:
            return None
        for key, value in entry[1].items():
            if key.endswith("-code-verifier"):
                return value.partition("/")[0]
        return None
