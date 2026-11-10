from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_health_endpoints_report_liveness_and_verified_readiness(
    settings, loaded_model_service
):
    app = create_app(settings=settings, model_service=loaded_model_service)

    with TestClient(app) as client:
        liveness = client.get("/health/live")
        readiness = client.get("/health/ready")

    assert liveness.status_code == 200
    assert liveness.json() == {"status": "alive"}
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "model": "local-finetuned",
        "embedding_dimension": 768,
    }
