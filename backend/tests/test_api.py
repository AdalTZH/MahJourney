from fastapi.testclient import TestClient

from mahjourney.main import app


def test_health_map_and_evaluation_contracts() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
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
