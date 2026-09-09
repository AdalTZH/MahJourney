from mahjourney.evaluation import run_evaluation


def test_evaluation_uses_real_scenario_mix_and_fails_closed() -> None:
    evaluation = run_evaluation(samples=50)
    assert evaluation["scenario_count"] == 24
    assert evaluation["scenario_mix"] == {"golden": 8, "disruption": 8, "adversarial": 8}
    assert evaluation["policy_compliance"] == 1.0
    assert evaluation["zero_infeasible_automatic_executions"] is True
    assert evaluation["traffic_free_ortools_comparison"] in {"PASS", "NEEDS_IMPROVEMENT"}
    assert {item["event"] for item in evaluation["results"]} >= {
        "HEAVY_RAIN",
        "ROAD_CLOSURE",
        "TRUCK_BREAKDOWN",
        "URGENT_ORDER",
        "STALE_DATA",
    }
