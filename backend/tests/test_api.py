from fastapi.testclient import TestClient

from mahjourney.auth import hash_password
from mahjourney.main import app


def _login(client: TestClient) -> None:
    app.state.services.settings.admin_username = "admin"
    app.state.services.settings.admin_password_hash = hash_password("test-password")
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "test-password"}
    )
    assert response.status_code == 200


def test_health_map_and_evaluation_contracts() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        _login(client)
        map_state = client.get("/api/v1/map/state").json()
        assert len(map_state["trucks"]) == 10
        assert map_state["plan"]["hard_violations"] == []
        evaluation = client.post("/api/v1/evaluations/run").json()
        assert evaluation["scenario_count"] == 24
        assert evaluation["hard_constraint_compliance"] == 1.0
        assert evaluation["median_cost_improvement_percent"] >= 10


def test_public_websocket_contract() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/events") as socket:
        event = socket.receive_json()
        assert event["type"] == "heartbeat"
        assert event["at"]


def test_protected_route_requires_session() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/map/state")
        assert response.status_code == 401


def test_login_rejects_wrong_password_and_locks_out_after_repeated_failures() -> None:
    with TestClient(app) as client:
        app.state.services.settings.admin_username = "admin"
        app.state.services.settings.admin_password_hash = hash_password("correct-password")
        for _ in range(5):
            response = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
            )
            assert response.status_code == 401
        locked = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "correct-password"}
        )
        assert locked.status_code == 429


def test_login_logout_and_session_status_round_trip() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False
        _login(client)
        assert client.get("/api/v1/auth/session").json()["authenticated"] is True
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False


def test_api_docs_require_admin_session() -> None:
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 401
        assert client.get("/redoc").status_code == 401
        assert client.get("/openapi.json").status_code == 401
        _login(client)
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        assert client.get("/openapi.json").status_code == 200
