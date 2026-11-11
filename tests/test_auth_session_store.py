from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import uuid4

import pytest

from backend.app.oauth_flow_store import OAuthFlowStore
from backend.app.session_service import InvalidSession, RefreshedTokens, SessionStore


class Clock:
    def __init__(self, value: float = 1_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_oauth_flow_validates_state_transaction_and_is_single_use():
    clock = Clock()
    store = OAuthFlowStore(ttl_seconds=60, clock=clock)
    flow_id, state, transaction, storage = store.begin("https://app.example.test/")
    storage.set_item("project-code-verifier", "verifier/metadata")
    assert store.consume(flow_id, "wrong", transaction) is None
    assert store.consume(flow_id, state, "wrong") is None
    consumed = store.consume(flow_id, state, transaction)
    assert consumed is not None
    assert consumed.code_verifier == "verifier"
    assert consumed.return_to == "https://app.example.test/"
    assert store.consume(flow_id, state, transaction) is None


def test_oauth_flow_expires_and_ignores_session_storage_values():
    clock = Clock()
    store = OAuthFlowStore(ttl_seconds=10, clock=clock)
    flow_id, state, transaction, storage = store.begin("https://app.example.test/")
    storage.set_item("supabase-session", "must-not-be-stored")
    storage.set_item("project-code-verifier", "verifier")
    assert storage.get_item("supabase-session") is None
    clock.value += 11
    assert store.consume(flow_id, state, transaction) is None


def test_sessions_are_isolated_and_revocation_is_identifier_specific():
    clock = Clock()
    store = SessionStore(clock=clock, refresh_skew_seconds=0)
    user_a, user_b = uuid4(), uuid4()
    sid_a = store.create(user_a, "a@example.test", "access-a", "refresh-a", 2_000)
    sid_b = store.create(user_b, "b@example.test", "access-b", "refresh-b", 2_000)
    no_refresh = lambda _: pytest.fail("valid tokens must not refresh")
    assert store.resolve(sid_a, no_refresh).user_id == user_a
    assert store.resolve(sid_b, no_refresh).user_id == user_b
    store.revoke(sid_a)
    with pytest.raises(InvalidSession):
        store.resolve(sid_a, no_refresh)
    assert store.resolve(sid_b, no_refresh).user_id == user_b


def test_refresh_rotation_is_serialized_and_replaces_refresh_token():
    clock = Clock()
    store = SessionStore(clock=clock, refresh_skew_seconds=60)
    user_id = uuid4()
    sid = store.create(user_id, "user@example.test", "old-access", "old-refresh", 1_001)
    calls: list[str] = []
    calls_lock = Lock()

    def refresh(token: str) -> RefreshedTokens:
        with calls_lock:
            calls.append(token)
        return RefreshedTokens(user_id, "user@example.test", "new-access", "new-refresh", 2_000)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolved = list(executor.map(lambda _: store.resolve(sid, refresh), range(2)))
    assert calls == ["old-refresh"]
    assert [item.access_token for item in resolved] == ["new-access", "new-access"]
    clock.value = 1_950
    store.resolve(sid, refresh)
    assert calls == ["old-refresh", "new-refresh"]


def test_failed_or_cross_user_refresh_revokes_session():
    clock = Clock()
    store = SessionStore(clock=clock)
    user_id = uuid4()
    sid = store.create(user_id, "user@example.test", "access", "refresh", 1_001)
    other_user = uuid4()
    with pytest.raises(InvalidSession):
        store.resolve(sid, lambda _: RefreshedTokens(other_user, "other@example.test", "new", "rotated", 2_000))
    with pytest.raises(InvalidSession):
        store.resolve(sid, lambda _: pytest.fail("revoked sessions must not refresh"))


def test_expired_session_and_csrf_validation_fail_safely():
    clock = Clock()
    store = SessionStore(idle_ttl_seconds=10, absolute_ttl_seconds=20, clock=clock)
    sid = store.create(uuid4(), "user@example.test", "access", "refresh", 2_000)
    resolved = store.resolve(sid, lambda _: pytest.fail("valid token must not refresh"))
    assert store.validate_csrf(sid, resolved.csrf_token)
    assert not store.validate_csrf(sid, "wrong")
    clock.value += 11
    with pytest.raises(InvalidSession):
        store.resolve(sid, lambda _: pytest.fail("expired session must not refresh"))
    assert not store.validate_csrf(sid, resolved.csrf_token)
