"""Small process-local limiter for expensive document-analysis endpoints."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic


class RequestRateLimiter:
    """Fixed-window-per-client limiter; deployment-wide limiting belongs at the proxy."""

    def __init__(self, requests: int, window_seconds: float) -> None:
        self._requests = requests
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, client: str) -> bool:
        now = monotonic()
        with self._lock:
            events = self._events[client]
            while events and events[0] <= now - self._window:
                events.popleft()
            if len(events) >= self._requests:
                return False
            events.append(now)
            return True
