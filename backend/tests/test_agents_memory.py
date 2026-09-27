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
    # Heavy rain is a user-drawn zone, not fleet-wide, so the zone must cover
    # every synthetic order's location for "every leg slows down" to hold.
    fleet_wide_zone = [
        {"lat": 1.15, "lon": 103.55},
        {"lat": 1.15, "lon": 104.10},
        {"lat": 1.50, "lon": 104.10},
        {"lat": 1.50, "lon": 103.55},
    ]
    rain = DisruptionEvent(
        scenario_id="test", event_type="HEAVY_RAIN", effective_minute=480,
        payload={"severity": "HEAVY", "polygon": fleet_wide_zone},
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


# --- Task 3: Pydantic GraphState validation ----------------------------------

import operator  # noqa: E402

import pytest  # noqa: E402

from mahjourney.agents import GraphState  # noqa: E402


def test_graphstate_rejects_blank_task_type() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GraphState(task=AgentTask(task_type="   ", requester="t", conversation_id="c"))


def test_graphstate_rejects_unknown_delegation_target() -> None:
    from pydantic import ValidationError

    good = AgentTask(task_type="ROUTE_PLAN", requester="t", conversation_id="c")
    with pytest.raises(ValidationError):
        GraphState(task=good, delegated_to="not_a_real_worker")


def test_graphstate_results_reducer_is_additive() -> None:
    # The results channel must accumulate, not overwrite, across worker updates.
    from mahjourney.agents import GraphState as GS

    field = GS.model_fields["results"]
    # The Annotated metadata carries the operator.add reducer.
    assert operator.add in getattr(field.metadata[0], "__metadata__", ()) or any(
        m is operator.add for m in field.metadata
    )


def test_graphstate_step_count_defaults_and_validates() -> None:
    from pydantic import ValidationError

    task = AgentTask(task_type="ROUTE_PLAN", requester="t", conversation_id="c")
    assert GraphState(task=task).step_count == 0
    with pytest.raises(ValidationError):
        GraphState(task=task, step_count=-1)


# --- Capability-contract enforcement wired into the worker return path -------

from mahjourney.agents import _worker_update  # noqa: E402
from mahjourney.contracts import load_contract_registry  # noqa: E402
from mahjourney.domain import AgentResult  # noqa: E402


def _state(task_type: str = "ROUTE_PLAN") -> GraphState:
    return GraphState(task=AgentTask(task_type=task_type, requester="t", conversation_id="c"))


def test_worker_update_keeps_allowed_action_and_records_no_violation() -> None:
    reg = load_contract_registry()
    state = _state()
    result = AgentResult(
        task_id="x",
        status="COMPLETED",
        proposed_actions=({"type": "GENERATE_CANDIDATE_PLAN"},),
    )
    update = _worker_update(state, "route_planning", result, reg)
    assert update["result"].status == "COMPLETED"
    assert update["result"].proposed_actions == ({"type": "GENERATE_CANDIDATE_PLAN"},)
    assert update["contract_violations"] == []
    assert update["step_count"] == 1


def test_worker_update_escalates_on_forbidden_action() -> None:
    reg = load_contract_registry()
    state = _state()
    # A forged attempt to activate a plan from the route-planning worker.
    result = AgentResult(
        task_id="x",
        status="COMPLETED",
        proposed_actions=({"type": "ACTIVATE_PLAN"},),
    )
    update = _worker_update(state, "route_planning", result, reg)
    assert update["result"].status == "ESCALATED"
    assert update["result"].proposed_actions == ()  # fatal action never survives
    assert update["result"].escalation_reason
    assert any(v.severity == "FATAL" for v in update["contract_violations"])


def test_worker_update_drops_unsanctioned_action_but_continues() -> None:
    reg = load_contract_registry()
    state = _state()
    result = AgentResult(
        task_id="x",
        status="COMPLETED",
        proposed_actions=(
            {"type": "GENERATE_CANDIDATE_PLAN"},
            {"type": "SOMETHING_WEIRD"},
        ),
    )
    update = _worker_update(state, "route_planning", result, reg)
    # Turn continues (COMPLETED), unknown action stripped, allowed one kept.
    assert update["result"].status == "COMPLETED"
    assert update["result"].proposed_actions == ({"type": "GENERATE_CANDIDATE_PLAN"},)
    assert any(v.severity == "DROPPED" for v in update["contract_violations"])


def test_forbidden_action_escalates_through_the_full_graph(monkeypatch) -> None:
    # A worker corrupted to emit a forbidden action must escalate end to end,
    # and the graph's returned result must never carry the forbidden action.
    from mahjourney.agents import AgentSystem

    def rogue(self, state):  # noqa: ANN001
        result = AgentResult(
            task_id=state.task.task_id,
            status="COMPLETED",
            proposed_actions=({"type": "ACTIVATE_PLAN"},),
        )
        return _worker_update(state, "route_planning", result, self._contracts)

    # Patch the class method BEFORE constructing so the graph binds the rogue
    # worker when it wires nodes in __init__.
    monkeypatch.setattr(AgentSystem, "_route_planning", rogue)
    agents = AgentSystem(MasterMemory())
    result = agents.invoke(task("ROUTE_PLAN"))
    assert result.status == "ESCALATED"
    assert result.proposed_actions == ()


# --- Task 5 + 6: LLM supervisor + bounded multi-worker loop ------------------

from mahjourney.agents import MAX_WORKER_STEPS  # noqa: E402


class _StubGateway:
    """Stand-in for OpenAIGateway.route used to drive the loop + guard.

    ``plan`` is what the (fake) LLM proposes as the ordered worker list: a tuple
    of worker names, or None to simulate no client / parse failure (the
    supervisor then falls back to deterministic keyword routing).
    """

    def __init__(self, plan):  # noqa: ANN001
        self._plan = plan
        self.calls = []

    def route(self, message, conversation_history=(), memory_snippets=(), incident_lessons=()):  # noqa: ANN001
        self.calls.append(
            (message, tuple(conversation_history), tuple(memory_snippets), tuple(incident_lessons))
        )
        if self._plan is None:
            # Simulate no client / unusable output: supervisor uses keywords.
            return None
        return {
            "action": "dispatch",
            "reply": "",
            "workers": tuple(self._plan),
            "rationale": "",
        }


def _routed_worker(result) -> str | None:  # noqa: ANN001
    return result.proposed_actions[0]["type"] if result.proposed_actions else None


def test_supervisor_falls_back_to_keyword_without_gateway() -> None:
    # No gateway: routing is purely keyword-based (baseline behavior preserved).
    agents = AgentSystem(MasterMemory())
    result = agents.invoke(task("ROUTE_PLAN"), message="please replan the route")
    assert _routed_worker(result) == "GENERATE_CANDIDATE_PLAN"


def test_supervisor_honors_valid_llm_plan() -> None:
    # A paraphrase the keyword classifier would miss; the LLM plans disruption
    # and the guard accepts the valid enum value.
    stub = _StubGateway(("disruption",))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    t = AgentTask(task_type="CLARIFICATION_NEEDED", requester="t", conversation_id="c")
    result = agents.invoke(t, message="the roads are a mess near Jurong today")
    assert _routed_worker(result) == "BOUNDED_REPLAN"
    assert stub.calls  # the gateway was consulted


def test_guard_drops_offenum_workers_from_plan() -> None:
    # Unknown workers in the plan are stripped; the keyword baseline still runs.
    stub = _StubGateway(("totally_not_a_worker",))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    result = agents.invoke(task("ROUTE_PLAN"), message="replan the route")
    assert _routed_worker(result) == "GENERATE_CANDIDATE_PLAN"


def test_keyword_baseline_survives_when_llm_omits_it() -> None:
    # A clear keyword match (ROUTE_PLAN) is merged into the plan even if the LLM
    # only proposed a different worker, so it is never silently dropped.
    stub = _StubGateway(("driver_comms",))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    final = agents.run(task("ROUTE_PLAN"), message="optimize the plan and check driver 3")
    ran = set(final.dispatched_workers)
    assert "route_planning" in ran  # keyword baseline preserved
    assert "driver_comms" in ran  # LLM addition honored


def test_empty_plan_and_no_keyword_asks_to_clarify() -> None:
    # Keyword classifier had nothing and the LLM proposed nothing usable.
    stub = _StubGateway(())
    agents = AgentSystem(MasterMemory(), gateway=stub)
    t = AgentTask(task_type="CLARIFICATION_NEEDED", requester="t", conversation_id="c")
    result = agents.invoke(t, message="hello there")
    assert result.status == "NEEDS_INPUT"


def test_none_llm_plan_falls_back_to_keyword() -> None:
    # Gateway returning None (no client / parse failure) => keyword routing.
    stub = _StubGateway(None)
    agents = AgentSystem(MasterMemory(), gateway=stub)
    result = agents.invoke(task("DRIVER_ENQUIRY"), message="where is driver 3")
    assert _routed_worker(result) == "DRAFT_DRIVER_REPLY"


def test_memory_snippets_reach_supervisor_only() -> None:
    stub = _StubGateway(("route_planning",))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    agents.invoke(
        task("ROUTE_PLAN"),
        message="replan",
        memory_snippets=("prefer depot A after 6pm",),
    )
    # The supervisor received the curated hint via the planner.
    assert stub.calls[0][2] == ("prefer depot A after 6pm",)


def test_bounded_loop_dispatches_multiple_workers_then_synthesizes() -> None:
    # A compound request should run two workers and produce a synthesized reply.
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    stub = _StubGateway(("disruption", "driver_comms"))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    context = AgentContext(plan=plan, fleet=fleet, orders=orders, driver_id=fleet[0].driver_id)
    t = AgentTask(task_type="DISRUPTION_ANALYSIS", requester="t", conversation_id="c")
    final = agents.run(t, context=context, message="replan around the closure and check driver")
    ran = set(final.dispatched_workers)
    assert {"disruption", "driver_comms"} <= ran
    assert len(final.results) >= 2
    assert final.final_reply  # synthesize produced a combined reply


def test_loop_is_bounded_by_step_cap() -> None:
    # Even if a plan somehow lists more than the cap, no more than
    # MAX_WORKER_STEPS workers run and the turn still terminates.
    stub = _StubGateway(("route_planning", "disruption", "driver_comms"))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    final = agents.run(task("ROUTE_PLAN"), message="do everything")
    assert len(final.dispatched_workers) <= MAX_WORKER_STEPS
    assert final.final_reply is not None


def test_each_worker_runs_at_most_once() -> None:
    # A plan that repeats a worker must not run it twice.
    stub = _StubGateway(("route_planning", "route_planning"))
    agents = AgentSystem(MasterMemory(), gateway=stub)
    final = agents.run(task("ROUTE_PLAN"), message="replan replan")
    assert final.dispatched_workers.count("route_planning") == 1


class _ConversationalGateway:
    """Stand-in for OpenAIGateway.route that answers conversationally.

    Simulates the LLM master deciding a message needs no worker (a greeting,
    small talk, a capability question, or a clarifying question) and returning a
    ``direct_reply`` with the reply text.
    """

    def __init__(self, reply: str):
        self._reply = reply
        self.calls = []

    def route(self, message, conversation_history=(), memory_snippets=(), incident_lessons=()):  # noqa: ANN001
        self.calls.append(
            (message, tuple(conversation_history), tuple(memory_snippets), tuple(incident_lessons))
        )
        return {"action": "direct_reply", "reply": self._reply, "workers": (), "rationale": ""}


def test_greeting_gets_conversational_reply_not_clarification() -> None:
    # The whole point of Phase 1: "hi" must get a friendly reply, never the
    # canned "I could not tell whether this is a route/disruption/driver" refusal.
    stub = _ConversationalGateway("Hi! I'm the MahJourney dispatcher. How can I help?")
    agents = AgentSystem(MasterMemory(), gateway=stub)
    t = AgentTask(task_type=CLARIFICATION_NEEDED, requester="t", conversation_id="c")
    final = agents.run(t, message="hi")
    assert final.direct_reply == "Hi! I'm the MahJourney dispatcher. How can I help?"
    # No worker ran and nothing was proposed on a conversational turn.
    assert final.dispatched_workers == ()
    assert final.results == []


def test_direct_reply_receives_conversation_history() -> None:
    stub = _ConversationalGateway("Sure, what would you like to know?")
    agents = AgentSystem(MasterMemory(), gateway=stub)
    t = AgentTask(task_type=CLARIFICATION_NEEDED, requester="t", conversation_id="c")
    history = (("user", "hello"), ("assistant", "Hi there!"))
    agents.run(t, message="can you help me", conversation_history=history)
    # The router saw the prior turns.
    assert stub.calls[0][1] == history


def test_empty_direct_reply_falls_back_to_clarify() -> None:
    # A direct_reply decision with no text must not become an empty message; it
    # falls through to the clarify path so the user always gets something.
    stub = _ConversationalGateway("")
    agents = AgentSystem(MasterMemory(), gateway=stub)
    t = AgentTask(task_type=CLARIFICATION_NEEDED, requester="t", conversation_id="c")
    final = agents.run(t, message="???")
    assert final.direct_reply is None
    assert final.result is not None and final.result.status == "NEEDS_INPUT"
