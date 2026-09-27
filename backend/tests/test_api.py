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
        # No plan is built at startup, so the live map starts with no active
        # plan and no trucks until the dispatcher generates AND activates one.
        initial = client.get("/api/v1/map/state").json()
        assert initial["plan"]["status"] == "NO_ACTIVE_PLAN"
        assert initial["trucks"] == []
        # Drive the real flow: generate a candidate, then activate it. Only then
        # should the map reflect the fleet's routes.
        app.state.services.settings.allow_plan_execution_in_tests = True
        generated = client.post("/api/v1/plans/generate", json={}).json()
        assert generated["status"] in {"CANDIDATE", "VALIDATED"}
        activated = client.post(
            "/api/v1/plans/activate",
            json={"plan_id": generated["plan_id"], "version": generated["version"]},
        )
        assert activated.status_code == 200
        map_state = client.get("/api/v1/map/state").json()
        assert len(map_state["trucks"]) == len(app.state.services.fleet)
        assert map_state["plan"]["status"] == "ACTIVE"
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


# --- Task 7: dispatcher_message integrated with the multi-worker loop --------


def test_dispatcher_route_message_generates_candidate_not_active() -> None:
    with TestClient(app) as client:
        _login(client)
        active_before = app.state.services.active_plan_id
        response = client.post(
            "/api/v1/dispatcher/messages",
            json={"conversation_id": "c1", "message": "please replan the route"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reply"]
        assert body["result"]["status"] == "COMPLETED"
        # A candidate plan was generated for review, never activated.
        assert body["generated_plan"] is not None
        assert body["generated_plan"]["status"] in {"CANDIDATE", "VALIDATED"}
        # The agent turn never activates a plan.
        assert app.state.services.active_plan_id == active_before


def test_dispatcher_unclassifiable_message_asks_to_clarify() -> None:
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/v1/dispatcher/messages",
            json={"conversation_id": "c2", "message": "hello there"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["status"] == "NEEDS_INPUT"
        assert body["generated_plan"] is None


def test_dispatcher_message_keeps_audit_chain_verifiable() -> None:
    with TestClient(app) as client:
        _login(client)
        client.post(
            "/api/v1/dispatcher/messages",
            json={"conversation_id": "c3", "message": "replan the route please"},
        )
        # The hash-linked audit chain must still verify after agent + plan events.
        assert app.state.services.audit.verify() is True
