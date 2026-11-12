from __future__ import annotations

import logging
from dataclasses import replace
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.auth_routes import ProviderSession, RedactOAuthAccessLog, SupabaseOAuthProvider, create_auth_router
from backend.app.oauth_flow_store import OAuthFlowStore
from backend.app.session_service import RefreshedTokens, SessionStore


class FakeProvider:
    def __init__(self, user_id: UUID | None = None, expires_at: float | None = None) -> None:
        self.user_id = user_id or uuid4()
        self.expires_at = expires_at or time() + 3_600
        self.callback_url = ""
        self.exchange_calls = 0
        self.refresh_calls: list[str] = []
        self.sign_out_calls: list[tuple[str, str]] = []

    def authorization_url(self, callback_url, storage):
        self.callback_url = callback_url
        storage.set_item("project-code-verifier", "server-verifier")
        return "https://provider.example.test/authorize"

    def exchange(self, code, verifier):
        self.exchange_calls += 1
        assert code == "one-time-code"
        assert verifier == "server-verifier"
        return ProviderSession(
            self.user_id,
            "user@example.test",
            "access-one",
            "refresh-one",
            self.expires_at,
            "Abhishek Kumar",
        )

    def refresh(self, refresh_token):
        self.refresh_calls.append(refresh_token)
        return RefreshedTokens(
            self.user_id,
            "user@example.test",
            "access-two",
            "refresh-two",
            time() + 3_600,
            "Abhishek Kumar",
        )

    def sign_out(self, access_token, refresh_token):
        self.sign_out_calls.append((access_token, refresh_token))


def auth_client(settings, provider: FakeProvider, session_store: SessionStore | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_auth_router(
            settings,
            OAuthFlowStore(),
            session_store or SessionStore(),
            provider,
        )
    )
    return TestClient(app, base_url=settings.auth_public_url)


def begin_and_callback(client: TestClient, provider: FakeProvider):
    started = client.get(
        "/auth/google/start",
        params={"return_to": "http://localhost:8501"},
        follow_redirects=False,
    )
    assert started.status_code == 302
    assert started.headers["location"] == "https://provider.example.test/authorize"
    parsed = urlsplit(provider.callback_url)
    callback = client.get(
        f"{parsed.path}?{parsed.query}&code=one-time-code",
        follow_redirects=False,
    )
    return started, callback, f"{parsed.path}?{parsed.query}&code=one-time-code"


def test_oauth_callback_sets_secure_cookie_and_rejects_replay(settings):
    secure_settings = replace(
        settings,
        auth_public_url="https://ats.example.test",
        auth_allowed_redirect_urls=("https://ats.example.test",),
        auth_cookie_secure=True,
    )
    provider = FakeProvider()
    client = auth_client(secure_settings, provider)
    started = client.get("/auth/google/start", follow_redirects=False)
    transaction_cookie = client.cookies.get("__Host-ats_oauth")
    parsed = urlsplit(provider.callback_url)
    callback_url = f"{parsed.path}?{parsed.query}&code=one-time-code"
    callback = client.get(callback_url, follow_redirects=False)
    assert callback.status_code == 302
    assert callback.headers["location"] == "https://ats.example.test?auth=success"
    cookies = callback.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if value.startswith("__Host-ats_session="))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    client.cookies.set("__Host-ats_oauth", transaction_cookie, path="/")
    replay = client.get(callback_url, follow_redirects=False)
    assert replay.status_code == 400
    assert provider.exchange_calls == 1
    assert "code=" not in str(replay.json())


def test_oauth_state_transaction_and_redirect_allowlist_are_enforced(settings):
    provider = FakeProvider()
    client = auth_client(settings, provider)
    rejected = client.get(
        "/auth/google/start",
        params={"return_to": "https://attacker.example.test"},
        follow_redirects=False,
    )
    assert rejected.status_code == 400
    client.get("/auth/google/start", follow_redirects=False)
    parsed = urlsplit(provider.callback_url)
    query = parse_qs(parsed.query)
    wrong_state = client.get(
        parsed.path,
        params={"flow": query["flow"][0], "state": "wrong", "code": "one-time-code"},
        follow_redirects=False,
    )
    assert wrong_state.status_code == 400
    assert provider.exchange_calls == 0


def test_session_refresh_rotates_token_once(settings):
    provider = FakeProvider(expires_at=time() + 1)
    store = SessionStore(refresh_skew_seconds=60)
    client = auth_client(settings, provider, store)
    _, callback, _ = begin_and_callback(client, provider)
    assert callback.status_code == 302
    first = client.get("/auth/session")
    second = client.get("/auth/session")
    assert first.status_code == second.status_code == 200
    assert first.json()["email"] == "user@example.test"
    assert first.json()["display_name"] == "Abhishek Kumar"
    assert {"access_token", "refresh_token", "code_verifier"}.isdisjoint(first.json())
    assert provider.refresh_calls == ["refresh-one"]


def test_supabase_google_user_metadata_resolves_verified_display_name():
    user = type("User", (), {
        "id": uuid4(), "email": "user@example.test",
        "user_metadata": {"full_name": "  Abhishek   Kumar  ", "name": "Lower priority"},
        "profile": {"full_name": "Profile Name"},
    })()
    session = type("Session", (), {
        "access_token": "access", "refresh_token": "refresh", "expires_at": time() + 3_600,
    })()
    resolved = SupabaseOAuthProvider._provider_session(user, session)
    assert resolved.display_name == "Abhishek Kumar"


def test_logout_requires_origin_and_csrf_then_invalidates_session(settings):
    provider = FakeProvider()
    client = auth_client(settings, provider)
    _, callback, _ = begin_and_callback(client, provider)
    assert callback.status_code == 302
    status = client.get("/auth/session")
    csrf = status.json()["csrf_token"]
    assert client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 403
    assert client.post(
        "/auth/logout",
        headers={"Origin": "https://attacker.example.test", "X-CSRF-Token": csrf},
    ).status_code == 403
    assert client.post(
        "/auth/logout",
        headers={"Origin": "http://localhost:8501", "X-CSRF-Token": "wrong"},
    ).status_code == 403
    logout = client.post(
        "/auth/logout",
        headers={"Origin": "http://localhost:8501", "X-CSRF-Token": csrf},
    )
    assert logout.status_code == 204
    assert provider.sign_out_calls == [("access-one", "refresh-one")]
    assert client.get("/auth/session").status_code == 401


def test_oauth_access_log_filter_removes_callback_query_values():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "/auth/google/callback?code=sensitive&state=sensitive", "1.1", 302),
        None,
    )
    assert RedactOAuthAccessLog().filter(record)
    assert "sensitive" not in str(record.args)
    assert record.args[2] == "/auth/google/callback?<redacted>"
