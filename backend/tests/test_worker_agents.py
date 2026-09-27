"""Phase 3 tests: workers run as LLM agents via the tool-calling loop.

These use a fake gateway whose ``run_agent`` mimics the real contract — it
invokes the supplied ``run_tool`` (simulating the model choosing a tool) and
then returns findings — so we exercise the worker's LLM path and prove the
stored metrics come from REAL tool executions, not model free-text.
"""

from mahjourney.agents import AgentContext, AgentSystem
from mahjourney.domain import AgentTask, DisruptionEvent
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.memory import MasterMemory
from mahjourney.planning import build_plan


class _AgentGateway:
    """Fake gateway with a client and a working run_agent tool loop.

    ``dispatch_workers`` is what route() proposes. ``tool_plan`` maps a worker's
    system-prompt keyword to the tool name the "model" decides to call, so each
    worker exercises its own tool.
    """

    def __init__(self, dispatch_workers):
        self.client = object()  # non-None: signals the LLM path is available
        self._workers = tuple(dispatch_workers)
        self.agent_calls = []

    def route(self, message, conversation_history=(), memory_snippets=(), incident_lessons=()):  # noqa: ANN001
        return {
            "action": "dispatch",
            "reply": "",
            "workers": self._workers,
            "rationale": "",
        }

    def run_agent(self, system_prompt, task_summary, tools, run_tool, finalize_schema):  # noqa: ANN001
        # Simulate the model: call every offered domain tool once (skipping the
        # implicit submit_findings, which isn't in `tools`), then submit.
        self.agent_calls.append(task_summary)
        for tool in tools:
            name = tool["name"]
            args = {}
            if name == "lookup_driver_assignment":
                args = {"driver_id": task_summary.get("enquiring_driver_id")}
            run_tool(name, args)
        return {"summary": "done"}


def _task(task_type: str) -> AgentTask:
    return AgentTask(task_type=task_type, requester="t", conversation_id="c")


def test_route_planning_llm_path_uses_real_tool_metrics() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    gw = _AgentGateway(("route_planning",))
    agents = AgentSystem(MasterMemory(), gateway=gw)
    ctx = AgentContext(plan=plan, fleet=fleet, orders=orders)
    result = agents.invoke(_task("ROUTE_PLAN"), ctx, message="check the plan")
    # Metrics came from the real validate_current_plan tool run.
    assert result.status == "COMPLETED"
    assert result.computed_metrics["assigned_stops"] == sum(len(r.stops) for r in plan.routes)
    assert result.computed_metrics["vehicles"] == len(plan.routes)
    assert result.proposed_actions[0]["type"] == "GENERATE_CANDIDATE_PLAN"
    assert gw.agent_calls  # the LLM agent loop actually ran


def test_disruption_llm_path_reports_added_minutes() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    disruption = DisruptionEvent(
        scenario_id="demo", event_type="HEAVY_RAIN", effective_minute=480, payload={}
    )
    gw = _AgentGateway(("disruption",))
    agents = AgentSystem(MasterMemory(), gateway=gw)
    ctx = AgentContext(
        plan=plan, fleet=fleet, orders=orders, active_disruptions=(disruption,)
    )
    result = agents.invoke(_task("DISRUPTION_ANALYSIS"), ctx, message="rain impact?")
    assert result.status == "COMPLETED"
    assert "added_minutes_total" in result.computed_metrics
    assert "HEAVY_RAIN" in result.computed_metrics["disruption_types"]
    assert result.proposed_actions[0]["type"] == "BOUNDED_REPLAN"


def test_driver_comms_llm_path_scopes_to_driver() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    driver = fleet[0].driver_id
    gw = _AgentGateway(("driver_comms",))
    agents = AgentSystem(MasterMemory(), gateway=gw)
    ctx = AgentContext(plan=plan, fleet=fleet, orders=orders, driver_id=driver)
    result = agents.invoke(_task("DRIVER_ENQUIRY"), ctx, message="where am I going?")
    assert result.status == "COMPLETED"
    assert result.computed_metrics["driver_id"] == driver
    assert result.proposed_actions[0]["type"] == "DRAFT_DRIVER_REPLY"
    assert result.proposed_actions[0]["send"] is False


def test_llm_path_still_only_proposes_never_executes() -> None:
    # Even on the LLM path, actions remain propose-only: no activation/send flag.
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    gw = _AgentGateway(("route_planning",))
    agents = AgentSystem(MasterMemory(), gateway=gw)
    ctx = AgentContext(plan=plan, fleet=fleet, orders=orders)
    result = agents.invoke(_task("ROUTE_PLAN"), ctx, message="do it")
    for action in result.proposed_actions:
        assert action.get("activate") is not True
        assert action.get("send") is not True


def test_disruption_result_allows_none_worst_affected_vehicle() -> None:
    # Regression: a live disruption turn produced worst_affected_vehicle=None,
    # which AgentResult.computed_metrics rejected (only float|int|str|bool).
    # None must be permitted for optional metrics like worst_affected_vehicle.
    from mahjourney.agents import _disruption_metrics_from_tool
    from mahjourney.domain import AgentResult

    tool_result = {
        "available": True,
        "active_disruptions": 1,
        "disruption_types": ["ROAD_CLOSURE"],
        "added_minutes_total": 0,
        "objective_cost_delta_km": 0.0,
        "worst_affected_vehicle": None,
        "worst_route_duration_minutes": 0,
    }
    metrics, _warnings = _disruption_metrics_from_tool(tool_result)
    assert metrics["worst_affected_vehicle"] is None
    # The crashing line: constructing the result must now succeed.
    result = AgentResult(task_id="x", status="COMPLETED", computed_metrics=metrics)
    assert result.computed_metrics["worst_affected_vehicle"] is None


def test_extract_driver_id_matches_known_fleet_only() -> None:
    # The API extracts a driver id from the message ONLY when it matches a real
    # fleet id, so "reroute driver 452907" scopes to that driver and a random
    # number does not produce a false match.
    from mahjourney.api import _extract_driver_id
    from mahjourney.domain import Coordinate, Vehicle

    fleet = (
        Vehicle(vehicle_id="TRK-1", driver_id="452907", start=Coordinate(lat=1.32, lon=103.7)),
        Vehicle(vehicle_id="TRK-2", driver_id="DRV-02", start=Coordinate(lat=1.33, lon=103.7)),
    )
    assert _extract_driver_id("reroute driver 452907 road closure", fleet) == "452907"
    assert _extract_driver_id("where is DRV-02 now?", fleet) == "DRV-02"
    # A number that is not a known driver id must not match.
    assert _extract_driver_id("there are 999999 orders today", fleet) is None
    # No driver named at all.
    assert _extract_driver_id("optimize the whole plan", fleet) is None


def test_task_summary_surfaces_driver_and_disruption_flag() -> None:
    from mahjourney.agents import AgentSystem, GraphState
    from mahjourney.domain import AgentTask, DisruptionEvent

    ctx = AgentContext(driver_id="452907", active_disruptions=(
        DisruptionEvent(scenario_id="demo", event_type="ROAD_CLOSURE", effective_minute=480),
    ))
    state = GraphState(
        task=AgentTask(task_type="DISRUPTION_ANALYSIS", requester="t", conversation_id="c"),
        context=ctx,
        message="reroute driver 452907",
    )
    summary = AgentSystem._task_summary(state)
    assert summary["enquiring_driver_id"] == "452907"
    assert summary["has_active_disruption"] is True
    assert summary["active_disruption_count"] == 1


def test_stream_run_emits_real_step_events_then_final_state() -> None:
    # Option 2 streaming: stream_run yields a ("step", {node,label}) per node the
    # graph actually runs, then a ("final", GraphState). The steps must reflect
    # the REAL nodes (supervisor + the dispatched worker + synthesize), and the
    # final state must match what run() produces for the same input.
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    gw = _AgentGateway(("route_planning",))
    agents = AgentSystem(MasterMemory(), gateway=gw)
    ctx = AgentContext(plan=plan, fleet=fleet, orders=orders)
    task = _task("ROUTE_PLAN")

    events = list(agents.stream_run(task, ctx, message="check the plan"))
    kinds = [k for k, _ in events]
    assert kinds[-1] == "final"
    step_nodes = [p["node"] for k, p in events if k == "step"]
    # The supervisor and the route_planning worker really ran.
    assert "supervisor" in step_nodes
    assert "route_planning" in step_nodes
    # Every step carries a human-readable label.
    assert all(p["label"] for k, p in events if k == "step")

    final_state = events[-1][1]
    # The reconstructed final state carries the worker result, like run().
    assert final_state.results
    assert final_state.results[-1].proposed_actions[0]["type"] == "GENERATE_CANDIDATE_PLAN"


def test_stream_run_final_state_matches_run() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    agents = AgentSystem(MasterMemory())  # no gateway: deterministic path
    ctx = AgentContext(plan=plan, fleet=fleet, orders=orders)
    task = _task("ROUTE_PLAN")

    run_final = agents.run(task, ctx, message="replan the route")
    stream_events = list(agents.stream_run(task, ctx, message="replan the route"))
    stream_final = stream_events[-1][1]
    # Same worker ran and same proposed action type on both paths.
    assert [r.status for r in stream_final.results] == [r.status for r in run_final.results]
    assert stream_final.dispatched_workers == run_final.dispatched_workers
