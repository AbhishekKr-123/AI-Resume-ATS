import json
from pathlib import Path
from uuid import UUID

import httpx

from backend.app.database_service import AuthenticatedUser, SupabaseService


USER_ID = UUID("11111111-1111-1111-1111-111111111111")
MIGRATION = Path(__file__).resolve().parents[1] / "backend" / "sql" / "002_create_profiles_table.sql"


def test_profile_database_requests_are_user_jwt_scoped_and_owner_is_server_derived():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["apikey"] == "publishable-key"
        assert request.headers["authorization"] == "Bearer user-jwt"
        if request.method == "GET":
            assert request.url.params["user_id"] == f"eq.{USER_ID}"
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        assert body["user_id"] == str(USER_ID)
        assert request.url.params["on_conflict"] == "user_id"
        assert "resolution=merge-duplicates" in request.headers["prefer"]
        return httpx.Response(201, json=[body | {"created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}])

    service = SupabaseService("https://supabase.test", "publishable-key", 5)
    service._client.close()
    service._client = httpx.Client(base_url="https://supabase.test", transport=httpx.MockTransport(handler))
    user = AuthenticatedUser(USER_ID, "user@example.test")
    try:
        assert service.get_profile(user, "user-jwt") is None
        saved = service.upsert_profile(
            user,
            "user-jwt",
            {
                "full_name": "User Name",
                "linkedin_url": None,
                "github_url": "https://github.com/user",
                "leetcode_url": None,
                "portfolio_url": None,
                "user_id": "attacker-controlled-value",
            },
        )
    finally:
        service.close()
    assert saved["user_id"] == str(USER_ID)
    assert [request.method for request in requests] == ["GET", "POST"]


def test_profile_migration_has_private_owner_scoped_rls_and_no_duplicated_email():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    table_definition = sql.split("create table if not exists public.profiles", 1)[1].split(");", 1)[0]
    assert "user_id uuid primary key references auth.users(id) on delete cascade" in table_definition
    assert "full_name text not null" in table_definition
    assert "email" not in table_definition
    assert "created_at timestamptz" in table_definition and "updated_at timestamptz" in table_definition
    assert "alter table public.profiles enable row level security" in sql
    for operation in ("select", "insert", "update"):
        assert f'create policy "profiles_{operation}_own"' in sql
    assert sql.count("auth.uid()") >= 4
    assert "to authenticated" in sql
    assert "revoke all on table public.profiles from anon" in sql
    assert "service_role" not in sql
