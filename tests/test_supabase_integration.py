from io import BytesIO
from uuid import UUID, uuid4

from docx import Document
from fastapi.testclient import TestClient

from backend.app.database_service import AuthenticatedUser, AuthenticationError, DatabaseError
from backend.app.main import create_app


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
        self.users = {"token-a": AuthenticatedUser(USER_A), "token-b": AuthenticatedUser(USER_B)}
        self.rows: dict[str, dict] = {}
        self.fail_insert = fail_insert
        self.calls: list[tuple[str, UUID]] = []

    def close(self) -> None: pass

    def verify_user(self, token: str) -> AuthenticatedUser:
        if token not in self.users: raise AuthenticationError("invalid")
        return self.users[token]

    def insert_analysis(self, user, token, analysis, filename, feedback=None):
        if self.fail_insert: raise DatabaseError("failure")
        assert user == self.users[token]
        record_id = str(uuid4())
        row = {"id": record_id, "user_id": str(user.id), "overall_score": analysis["overall_ats_score"], "component_scores": analysis["components"], "matched_skills": analysis["matched_skills"], "missing_skills": analysis["missing_skills"], "matched_keywords": analysis["matched_keywords"], "missing_keywords": analysis["missing_keywords"], "feedback": feedback, "filename": filename, "created_at": "2026-01-01T00:00:00+00:00"}
        self.rows[record_id] = row; self.calls.append(("insert", user.id)); return row

    def list_analyses(self, user, token, limit, offset):
        assert user == self.users[token]
        self.calls.append(("list", user.id))
        return [row for row in self.rows.values() if row["user_id"] == str(user.id)][offset:offset + limit]

    def get_analysis(self, user, token, analysis_id):
        assert user == self.users[token]
        row = self.rows.get(str(analysis_id)); self.calls.append(("get", user.id))
        return row if row and row["user_id"] == str(user.id) else None

    def delete_analysis(self, user, token, analysis_id):
        row = self.get_analysis(user, token, analysis_id)
        if not row: return False
        self.rows.pop(str(analysis_id)); self.calls.append(("delete", user.id)); return True


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


def test_feedback_persistence_failure_is_not_silent(settings, loaded_model_service):
    app = create_app(settings=settings, model_service=loaded_model_service, feedback_service=FakeFeedback(), supabase_service=FakeSupabase(fail_insert=True))
    with TestClient(app) as client:
        response = client.post("/resumes/feedback", content=make_docx(), headers=headers("token-a"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"


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
