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


def test_evaluation_includes_agent_security_block() -> None:
    evaluation = run_evaluation(samples=50)
    security = evaluation["agent_security"]
    # The agent-security adversarial block runs alongside the 24 planning cases
    # without changing the scenario count.
    assert evaluation["scenario_count"] == 24
    assert security["case_count"] >= 4
    assert security["all_cases_passed"] is True
    assert security["no_unsafe_agent_actions"] is True
    names = {c["name"] for c in security["cases"]}
    assert {
        "prompt_injection_activation",
        "forged_forbidden_action",
        "hostile_llm_plan",
        "step_cap_exhaustion",
    } <= names
    # The forged forbidden action must have escalated and recorded a fatal.
    forged = next(c for c in security["cases"] if c["name"] == "forged_forbidden_action")
    assert forged["escalated"] is True
    assert forged["fatal_violation_recorded"] is True
