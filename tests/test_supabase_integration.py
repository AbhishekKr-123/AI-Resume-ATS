from io import BytesIO
from uuid import UUID, uuid4

from docx import Document
from fastapi.testclient import TestClient
import pytest

from backend.app.database_service import AuthenticatedUser, AuthenticationError, DatabaseError
from backend.app.main import create_app
from backend.app.session_service import SessionStore


USER_A = UUID("11111111-1111-1111-1111-111111111111")
USER_B = UUID("22222222-2222-2222-2222-222222222222")


def make_docx() -> bytes:
    document = Document()
    for line in ("Synthetic Candidate", "candidate@example.test", "Summary", "Python engineer", "Skills", "Python SQL", "Work Experience", "3 years Python SQL API work", "Education", "Synthetic University"):
        document.add_paragraph(line)
    output = BytesIO(); document.save(output)
    return output.getvalue()


class FakeSupabase:
    def __init__(self, fail_insert: bool = False) -> None:
        self.users = {
            "token-a": AuthenticatedUser(USER_A, "abhishek.kumar@example.test"),
            "token-b": AuthenticatedUser(USER_B, "other.user@example.test"),
        }
        self.rows: dict[str, dict] = {}
        self.profiles: dict[str, dict] = {}
        self.fail_insert = fail_insert
        self.calls: list[tuple[str, UUID]] = []

    def close(self) -> None: pass

    def verify_user(self, token: str) -> AuthenticatedUser:
        if token not in self.users: raise AuthenticationError("invalid")
        return self.users[token]

    def insert_analysis(self, user, token, analysis, filename, feedback=None):
        if self.fail_insert: raise DatabaseError("failure")
        assert user.id == self.users[token].id
        record_id = str(uuid4())
        row = {"id": record_id, "user_id": str(user.id), "overall_score": analysis["overall_ats_score"], "component_scores": analysis["components"], "matched_skills": analysis["matched_skills"], "missing_skills": analysis["missing_skills"], "matched_keywords": analysis["matched_keywords"], "missing_keywords": analysis["missing_keywords"], "feedback": feedback, "filename": filename, "created_at": "2026-01-01T00:00:00+00:00"}
        self.rows[record_id] = row; self.calls.append(("insert", user.id)); return row

    def list_analyses(self, user, token, limit, offset):
        assert user.id == self.users[token].id
        self.calls.append(("list", user.id))
        return [row for row in self.rows.values() if row["user_id"] == str(user.id)][offset:offset + limit]

    def get_analysis(self, user, token, analysis_id):
        assert user.id == self.users[token].id
        row = self.rows.get(str(analysis_id)); self.calls.append(("get", user.id))
        return row if row and row["user_id"] == str(user.id) else None

    def delete_analysis(self, user, token, analysis_id):
        row = self.get_analysis(user, token, analysis_id)
        if not row: return False
        self.rows.pop(str(analysis_id)); self.calls.append(("delete", user.id)); return True

    def get_profile(self, user, token):
        assert user.id == self.users[token].id
        self.calls.append(("profile_get", user.id))
        return self.profiles.get(str(user.id))

    def upsert_profile(self, user, token, profile):
        assert user.id == self.users[token].id
        existing = self.profiles.get(str(user.id), {})
        row = {
            **existing,
            "user_id": str(user.id),
            **profile,
            "created_at": existing.get("created_at", "2026-01-01T00:00:00+00:00"),
            "updated_at": "2026-01-02T00:00:00+00:00",
        }
        self.profiles[str(user.id)] = row
        self.calls.append(("profile_upsert", user.id))
        return row


class FakeFeedback:
    def generate(self, _analysis): return {"overall_feedback": "Synthetic feedback"}


def headers(token: str | None = None) -> dict[str, str]:
    values = {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "x-upload-filename": "synthetic.docx", "x-job-description": "Python SQL required."}
    if token: values["authorization"] = f"Bearer {token}"
    return values


def test_protected_history_requires_verified_bearer_token(settings, loaded_model_service):
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=FakeSupabase())
    with TestClient(app) as client:
        missing = client.get("/analyses")
        invalid = client.get("/analyses", headers={"authorization": "Bearer forged-user-id"})
    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_authenticated_analysis_persists_without_client_user_id(settings, loaded_model_service):
    database = FakeSupabase()
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=database)
    with TestClient(app) as client:
        response = client.post("/resumes/analyze", content=make_docx(), headers=headers("token-a"))
    assert response.status_code == 200
    analysis_id = response.json()["analysis_id"]
    assert database.rows[analysis_id]["user_id"] == str(USER_A)
    assert "candidate@example.test" not in str(database.rows[analysis_id])


def test_history_get_delete_are_user_scoped(settings, loaded_model_service):
    database = FakeSupabase()
    row_a = database.insert_analysis(AuthenticatedUser(USER_A), "token-a", {"overall_ats_score": 80, "components": {}, "matched_skills": [], "missing_skills": [], "matched_keywords": [], "missing_keywords": []}, "a.docx")
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=database)
    with TestClient(app) as client:
        own_history = client.get("/analyses?limit=1&offset=0", headers={"authorization": "Bearer token-a"})
        other_get = client.get(f"/analyses/{row_a['id']}", headers={"authorization": "Bearer token-b"})
        other_delete = client.delete(f"/analyses/{row_a['id']}", headers={"authorization": "Bearer token-b"})
        own_get = client.get(f"/analyses/{row_a['id']}", headers={"authorization": "Bearer token-a"})
        own_delete = client.delete(f"/analyses/{row_a['id']}", headers={"authorization": "Bearer token-a"})
    assert len(own_history.json()["analyses"]) == 1
    assert other_get.status_code == other_delete.status_code == 404
    assert own_get.status_code == own_delete.status_code == 200


def test_cookie_sessions_preserve_cross_user_rls_identity(settings, loaded_model_service):
    from time import time

    database = FakeSupabase()
    row_a = database.insert_analysis(AuthenticatedUser(USER_A), "token-a", {"overall_ats_score": 80, "components": {}, "matched_skills": [], "missing_skills": [], "matched_keywords": [], "missing_keywords": []}, "a.docx")
    sessions = SessionStore()
    sid_a = sessions.create(USER_A, "a@example.test", "token-a", "refresh-a", time() + 3600)
    sid_b = sessions.create(USER_B, "b@example.test", "token-b", "refresh-b", time() + 3600)

    class Provider:
        def refresh(self, _): raise AssertionError("valid access tokens must not refresh")

    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=database, oauth_provider=Provider(), session_store=sessions)
    with TestClient(app) as client:
        client.cookies.set("ats_session_dev", sid_a)
        own = client.get(f"/analyses/{row_a['id']}")
        client.cookies.set("ats_session_dev", sid_b)
        other = client.get(f"/analyses/{row_a['id']}")
    assert own.status_code == 200
    assert other.status_code == 404


def test_feedback_persistence_failure_is_not_silent(settings, loaded_model_service):
    app = create_app(settings=settings, model_service=loaded_model_service, feedback_service=FakeFeedback(), supabase_service=FakeSupabase(fail_insert=True))
    with TestClient(app) as client:
        response = client.post("/resumes/feedback", content=make_docx(), headers=headers("token-a"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"


def test_profile_create_update_persistence_and_user_isolation(settings, loaded_model_service):
    database = FakeSupabase()
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=database)
    profile = {
        "full_name": "Abhishek Kumar",
        "linkedin_url": "https://www.linkedin.com/in/abhishek",
        "github_url": "https://github.com/abhishek",
        "leetcode_url": None,
        "portfolio_url": "https://abhishek.example",
    }
    with TestClient(app) as client:
        missing = client.get("/profile")
        initial = client.get("/profile", headers={"authorization": "Bearer token-a"})
        created = client.put("/profile", headers={"authorization": "Bearer token-a"}, json=profile)
        isolated = client.get("/profile", headers={"authorization": "Bearer token-b"})
        updated = client.put("/profile", headers={"authorization": "Bearer token-a"}, json={**profile, "full_name": "New Name", "github_url": None})
        reloaded = client.get("/profile", headers={"authorization": "Bearer token-a"})

    assert missing.status_code == 401
    assert initial.json() == {"email": "abhishek.kumar@example.test", "profile": None}
    assert created.status_code == 200
    assert created.json()["profile"]["user_id"] == str(USER_A)
    assert "email" not in created.json()["profile"]
    assert isolated.json() == {"email": "other.user@example.test", "profile": None}
    assert updated.json()["profile"]["full_name"] == "New Name"
    assert updated.json()["profile"]["github_url"] is None
    assert reloaded.json()["profile"] == updated.json()["profile"]
    assert str(USER_B) not in database.profiles


def test_profile_persists_across_simulated_logout_and_login(settings, loaded_model_service):
    database = FakeSupabase()
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=database)
    payload = {"full_name": "Persistent Name", "github_url": "https://github.com/persistent"}
    with TestClient(app) as first_session:
        saved = first_session.put("/profile", headers={"authorization": "Bearer token-a"}, json=payload)
    assert saved.status_code == 200

    with TestClient(app) as new_session:
        restored = new_session.get("/profile", headers={"authorization": "Bearer token-a"})
    assert restored.status_code == 200
    assert restored.json()["profile"]["full_name"] == "Persistent Name"
    assert restored.json()["profile"]["github_url"] == "https://github.com/persistent"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://example.test/profile", "not-a-url", "https://user:secret@example.test"])
def test_profile_api_rejects_unsafe_or_malformed_urls(settings, loaded_model_service, url):
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=FakeSupabase())
    with TestClient(app) as client:
        response = client.put(
            "/profile",
            headers={"authorization": "Bearer token-a"},
            json={"full_name": "Valid Name", "portfolio_url": url},
        )
    assert response.status_code == 422


def test_authenticated_feedback_is_persisted(settings, loaded_model_service):
    database = FakeSupabase()
    app = create_app(settings=settings, model_service=loaded_model_service, feedback_service=FakeFeedback(), supabase_service=database)
    with TestClient(app) as client:
        response = client.post("/resumes/feedback", content=make_docx(), headers=headers("token-a"))
    assert response.status_code == 200
    assert database.rows[response.json()["analysis_id"]]["feedback"] == {"overall_feedback": "Synthetic feedback"}


def test_unauthenticated_analysis_remains_stateless(settings, loaded_model_service):
    database = FakeSupabase()
    app = create_app(settings=settings, model_service=loaded_model_service, supabase_service=database)
    with TestClient(app) as client:
        response = client.post("/resumes/analyze", content=make_docx(), headers=headers())
    assert response.status_code == 200
    assert response.json()["analysis_id"] is None
    assert database.rows == {}
