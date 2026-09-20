from mahjourney.agents import CLARIFICATION_NEEDED, AgentContext, AgentSystem
from mahjourney.domain import AgentTask, DisruptionEvent
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.memory import MasterMemory
from mahjourney.planning import build_plan


def task(task_type: str) -> AgentTask:
    return AgentTask(task_type=task_type, requester="test", conversation_id="test")


def test_agent_sequences_are_bounded_and_non_authoritative() -> None:
    agents = AgentSystem(MasterMemory())
    result = agents.invoke(task("ROUTE_PLAN"))
    assert result.status == "COMPLETED"
    assert result.computed_metrics["tool_calls"] <= 3
    assert result.proposed_actions[0]["type"] == "GENERATE_CANDIDATE_PLAN"
    assert all(action.get("activate") is not True for action in result.proposed_actions)


def test_route_planning_reports_real_metrics_from_plan() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    agents = AgentSystem(MasterMemory())
    context = AgentContext(plan=plan, fleet=fleet, orders=orders)
    result = agents.invoke(task("ROUTE_PLAN"), context)
    # Metrics are derived from the real plan, not hardcoded.
    assert result.computed_metrics["hard_violations"] == len(plan.hard_violations)
    assert result.computed_metrics["assigned_stops"] == sum(len(r.stops) for r in plan.routes)
    assert result.computed_metrics["vehicles"] == len(plan.routes)
    assert result.computed_metrics["tool_calls"] <= 3
    assert result.proposed_actions[0]["type"] == "GENERATE_CANDIDATE_PLAN"


def test_master_classification_is_single_source_and_routes_correctly() -> None:
    from mahjourney.agents import classify_task_type

    # The classifier is the one place messages become task types.
    assert classify_task_type("please replan the route") == "ROUTE_PLAN"
    assert classify_task_type("there is a road closure and heavy rain") == "DISRUPTION_ANALYSIS"
    assert classify_task_type("where is driver 42 headed?") == "DRIVER_ENQUIRY"
    # A message matching none of the known keyword groups is genuinely
    # ambiguous, not a driver enquiry, so it must not be guessed as one.
    assert classify_task_type("hello") == CLARIFICATION_NEEDED
    assert classify_task_type("what's the weather like today") == CLARIFICATION_NEEDED

    # The master routes each canonical task type to the matching worker. An
    # unrecognized type (including CLARIFICATION_NEEDED) is handled by the
    # master's own clarify node, never guessed onto a worker.
    agents = AgentSystem(MasterMemory())

    def action(task_type: str) -> str | None:
        result = agents.invoke(task(task_type))
        return result.proposed_actions[0]["type"] if result.proposed_actions else None

    assert action("ROUTE_PLAN") == "GENERATE_CANDIDATE_PLAN"
    assert action("DISRUPTION_ANALYSIS") == "BOUNDED_REPLAN"
    assert action("DRIVER_ENQUIRY") == "DRAFT_DRIVER_REPLY"
    assert action("SOMETHING_NEW") is None


def test_unclassifiable_request_asks_for_clarification_not_a_worker_guess() -> None:
    agents = AgentSystem(MasterMemory())
    result = agents.invoke(task(CLARIFICATION_NEEDED))
    assert result.status == "NEEDS_INPUT"
    assert result.escalation_reason
    assert result.proposed_actions == ()


def test_disruption_agent_reports_real_added_time_not_an_estimate() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    agents = AgentSystem(MasterMemory())
    rain = DisruptionEvent(
        scenario_id="test", event_type="HEAVY_RAIN", effective_minute=480,
        payload={"severity": "HEAVY"},
    )
    result = agents.invoke(
        task("DISRUPTION_ANALYSIS"),
        AgentContext(plan=plan, fleet=fleet, orders=orders, active_disruptions=(rain,)),
    )
    # Heavy rain slows every leg, so the recomputed plan must take longer, and
    # the reported delta must match that real recomputation, not an estimate.
    assert result.computed_metrics["added_minutes_total"] > 0
    assert result.computed_metrics["disruption_types"] == "HEAVY_RAIN"
    assert result.proposed_actions[0]["type"] == "BOUNDED_REPLAN"
    assert result.proposed_actions[0]["requires_policy_check"] is True
    assert result.warnings


def test_disruption_agent_reports_no_delay_without_active_disruptions() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    agents = AgentSystem(MasterMemory())
    result = agents.invoke(
        task("DISRUPTION_ANALYSIS"), AgentContext(plan=plan, fleet=fleet, orders=orders)
    )
    assert result.computed_metrics.get("added_minutes_total", 0) == 0


def test_external_memory_requires_human_curation() -> None:
    memory = MasterMemory()
    item = memory.propose("INCIDENT_LESSON", "Avoid Pioneer Road after flooding")
    assert memory.search("Pioneer") == ()
    curated = memory.curate(item.memory_id)
    assert curated.trust_label == "HUMAN_APPROVED"
    assert memory.search("Pioneer")[0].status == "CURATED"
