from __future__ import annotations

import operator
from typing import Annotated, Any

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import (
    ContractRegistry,
    ContractViolation,
    enforce_contract,  # noqa: F401  # TODO(guardrails): re-attached in Phase 6 (see _worker_update)
    load_contract_registry,
)
from .disruptions import disruption_speed_kph_by_stop
from .domain import (
    AgentResult,
    AgentTask,
    DisruptionEvent,
    FrozenModel,
    MemoryItem,
    Order,
    PlanVersion,
    StateTransition,
    TurnPhase,
    Vehicle,
)
from .memory import MasterMemory
from .openai_gateway import OpenAIGateway
from .planning import reapply_route_timing, validate_plan
from .prompts import (
    DISRUPTION_PROMPT,
    DRIVER_AGENT_PROMPT,
    DRIVER_COMMS_PROMPT,
    ROUTE_PLANNING_PROMPT,
)
from .tools import (
    DISRUPTION_TOOLS,
    DRIVER_AGENT_TOOLS,
    DRIVER_COMMS_TOOLS,
    ROUTE_PLANNING_TOOLS,
    ToolContext,
    ToolError,
    ToolRegistry,
)


class AgentContext(FrozenModel):
    """Read-only snapshot handed to worker nodes.

    Workers receive this instead of any mutable service or the master-memory
    object, so they can compute real metrics from the current plan and fleet
    without the ability to change state or read curated memory. All fields are
    optional so the graph still runs (with safe defaults) when no snapshot is
    supplied, for example in unit tests.
    """

    plan: PlanVersion | None = None
    fleet: tuple[Vehicle, ...] = ()
    orders: tuple[Order, ...] = ()
    max_stops_per_vehicle: int = 25
    enforce_delivery_windows: bool = True
    active_disruptions: tuple[DisruptionEvent, ...] = ()
    driver_id: str | None = None
    # Pre-fetched, summarised external context (LTA traffic, NEA weather) the API
    # layer computes before the synchronous graph runs. Optional; tools degrade
    # gracefully to "unavailable" when absent.
    traffic_conditions: dict[str, Any] | None = None
    weather_conditions: dict[str, Any] | None = None


class GraphState(BaseModel):
    """Mutable graph state carrying frozen payloads between nodes.

    LangGraph merges each node's partial return into this container, so — unlike
    the domain payloads it holds (``AgentTask``/``AgentContext``/``AgentResult``,
    all frozen) — the top-level state must NOT be frozen. Runtime validation on
    this model is the input/output verification layer: a node cannot hand back a
    malformed task or an out-of-range step count without failing the turn here,
    before any effect is produced.

    ``results`` uses an additive reducer so multiple worker dispatches in one
    turn accumulate rather than overwrite. ``result`` is retained as the most
    recent single result for the current single-worker wiring and existing
    callers; the bounded multi-worker loop reads ``results``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: AgentTask
    # Raw dispatcher message, used ONLY by the supervisor node for LLM routing.
    # It is deliberately kept out of the frozen AgentTask (whose input_references
    # carry a hash of the message for audit) so the raw text never changes what
    # is audited. Workers do not read this field.
    message: str = ""
    context: AgentContext = Field(default_factory=lambda: AgentContext())
    result: AgentResult | None = None
    results: Annotated[list[AgentResult], operator.add] = Field(default_factory=list)
    delegated_to: str | None = None
    # Curated-memory snippets injected for the supervisor node ONLY; workers
    # never receive these (they see only ``context``).
    memory_snippets: tuple[str, ...] = ()
    # Recent (role, content) turns for the LLM router's conversational context,
    # oldest first. Supervisor-only, like memory_snippets; workers never see it.
    conversation_history: tuple[tuple[str, str], ...] = ()
    # Bounded-loop guard incremented on each worker dispatch.
    step_count: int = Field(default=0, ge=0)
    # The ordered set of workers the supervisor decided this turn needs, planned
    # once on first entry. Plain last-value-wins channel so it persists across
    # loop iterations. Workers already dispatched are tracked separately.
    planned_workers: tuple[str, ...] = ()
    dispatched_workers: tuple[str, ...] = ()
    # Whether the supervisor has run its one-time planning step yet.
    planned: bool = False
    # Final synthesized reply produced by the synthesize node.
    final_reply: str | None = None
    # Conversational reply produced by the master itself (greeting, small talk,
    # capability question, or a natural clarifying question) when the LLM router
    # decides the message needs no worker. Set by the ``respond`` node; when
    # present the API surfaces it verbatim as the turn's reply.
    direct_reply: str | None = None
    # Destination screen the LLM router inferred the dispatcher wants to be taken
    # to (one of OpenAIGateway._NAV_SCREENS), or "none". The API maps a non-"none"
    # value to a UI navigation directive in its final response. Set on the
    # supervisor node from the router decision; independent of direct_reply vs
    # dispatch, since navigation can accompany either.
    navigation: str = "none"
    # Capability-contract enforcement findings, accumulated across worker
    # dispatches. The API layer emits a SKILL_VIOLATION audit event per finding
    # (serially), keeping the graph itself synchronous and side-effect free.
    contract_violations: Annotated[list[ContractViolation], operator.add] = Field(
        default_factory=list
    )

    # -----------------------------------------------------------------------
    # Explicit state & memory management additions
    # -----------------------------------------------------------------------

    # Current lifecycle phase of the master agent for this turn. Transitions are
    # recorded in state_transitions below so the full path is auditable.
    turn_phase: TurnPhase = TurnPhase.ROUTING

    # Immutable log of every phase transition that occurred during the turn.
    # Uses an additive reducer so nodes append without overwriting prior entries.
    state_transitions: Annotated[list[StateTransition], operator.add] = Field(
        default_factory=list
    )

    # Memory items recalled mid-turn by the supervisor after the task type is
    # known (in addition to the upfront message-level snippets). Workers never
    # see these; they are merged into memory_snippets before the LLM router runs.
    mid_turn_recall: tuple[str, ...] = ()

    # Auto-generated INCIDENT_LESSON proposals recalled for the router this turn.
    # These are UNVETTED (PROPOSED/UNTRUSTED_EXTERNAL) worker warnings fed back
    # without human curation. They are kept in a SEPARATE channel from
    # memory_snippets on purpose: the router receives them as a distinct,
    # explicitly-unvetted bucket so the model never confuses them with curated
    # (human-approved) hints. Supervisor-only; workers never see them.
    incident_lessons: tuple[str, ...] = ()

    # INCIDENT_LESSON items newly PROPOSED by worker write-back this turn. Uses
    # an additive reducer so every worker's proposals accumulate. The API layer
    # drains this after the graph finishes to persist them on the async side —
    # the graph runs in a worker thread and must not touch persistence itself.
    proposed_lessons: Annotated[list[MemoryItem], operator.add] = Field(
        default_factory=list
    )

    @field_validator("task")
    @classmethod
    def _task_is_well_formed(cls, task: AgentTask) -> AgentTask:
        # Reject an empty/whitespace task type: an unclassified message becomes
        # CLARIFICATION_NEEDED upstream, never a blank type, so a blank here is a
        # malformed payload rather than a valid ambiguous request.
        if not task.task_type or not task.task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        return task

    @field_validator("delegated_to")
    @classmethod
    def _delegated_to_is_known(cls, value: str | None) -> str | None:
        if value is not None and value not in _ROUTABLE_TARGETS:
            raise ValueError(f"unknown delegation target: {value!r}")
        return value


# Single source of truth for turning a dispatcher message into a task type.
# Checked in order; the first matching group wins. The master agent owns this
# classification — callers should not re-derive it.
#
# Every real task type requires a POSITIVE keyword match. A message matching
# none of them is genuinely ambiguous and becomes CLARIFICATION_NEEDED rather
# than being guessed as a driver enquiry — defaulting an unrecognized request
# to a specific worker would be a silent misclassification, not a safe choice.
_TASK_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # DRIVER_DISPATCH is checked first: "send the route to driver X" also
    # contains ROUTE_PLAN ("route") and DRIVER_ENQUIRY ("driver") keywords, so a
    # send/notify intent must win before those more general buckets claim it.
    ("DRIVER_DISPATCH", ("send", "notify", "dispatch to", "forward", "text the driver")),
    ("DISRUPTION_ANALYSIS", ("closure", "breakdown", "rain", "urgent", "disruption", "flood")),
    ("ROUTE_PLAN", ("route", "plan", "replan", "optimize", "optimise", "reassign")),
    (
        "DRIVER_ENQUIRY",
        ("driver", "eta", "where is", "assignment", "assigned", "stop", "delivery"),
    ),
)
CLARIFICATION_NEEDED = "CLARIFICATION_NEEDED"

# Words that, on their own, read as the dispatcher approving a pending action.
# Used by the API layer to tell "yes, send it" apart from a fresh request when a
# single-driver send is already awaiting confirmation for this conversation.
_CONFIRMATION_KEYWORDS: tuple[str, ...] = (
    "yes",
    "confirm",
    "confirmed",
    "approve",
    "approved",
    "go ahead",
    "do it",
    "send it",
    "please send",
    "ok",
    "okay",
    "sure",
    "proceed",
)
# Words that read as the dispatcher rejecting/aborting the pending action.
_CANCELLATION_KEYWORDS: tuple[str, ...] = (
    "no",
    "cancel",
    "stop",
    "don't",
    "do not",
    "abort",
    "nevermind",
    "never mind",
    "hold off",
)


def is_confirmation(message: str) -> bool:
    """Whether a message reads as the dispatcher approving a pending action."""
    lowered = message.casefold().strip()
    return any(word in lowered for word in _CONFIRMATION_KEYWORDS)


def is_cancellation(message: str) -> bool:
    """Whether a message reads as the dispatcher rejecting a pending action."""
    lowered = message.casefold().strip()
    return any(word in lowered for word in _CANCELLATION_KEYWORDS)


# Verbs that read as "make this draft the live plan" and the nouns they must be
# paired with, so a plan-approval intent is only matched when BOTH an approving
# verb AND a plan/draft noun are present. This keeps it from firing on plan
# *generation* ("make a new plan") or a bare "yes" (handled by is_confirmation).
_PLAN_APPROVE_VERBS: tuple[str, ...] = (
    "approve", "activate", "go live", "make it live", "make this live",
    "make it active", "put it live", "publish", "confirm the plan", "accept",
    "roll it out", "roll out",
)
_PLAN_NOUNS: tuple[str, ...] = ("plan", "draft", "candidate", "it", "this")


def is_plan_approval_request(message: str) -> bool:
    """Whether a message reads as "approve/activate the draft plan".

    Requires an approving verb AND a plan-ish noun so it doesn't collide with
    plan generation or a plain confirmation. Used by the API layer to offer a
    confirm-gated activation — the agent proposes, the dispatcher's next "yes"
    executes, mirroring the single-driver-send flow.
    """
    lowered = message.casefold()
    has_verb = any(v in lowered for v in _PLAN_APPROVE_VERBS)
    has_noun = any(n in lowered for n in _PLAN_NOUNS)
    return has_verb and has_noun

# Explicit task-type -> worker routing. CLARIFICATION_NEEDED and any other
# unrecognized type route to the master's own clarification node, never to a
# worker, since guessing which worker should handle an unclassifiable request
# is not a safe default.
_TASK_TYPE_TO_WORKER: dict[str, str] = {
    "ROUTE_PLAN": "route_planning",
    "DISRUPTION_ANALYSIS": "disruption",
    "DRIVER_ENQUIRY": "driver_comms",
    # A send/dispatch request is still driver-scoped work, so it reuses the
    # driver_comms worker; the worker proposes a SEND_DRIVER_ROUTE action that
    # the API layer gates behind the dispatcher's confirmation.
    "DRIVER_DISPATCH": "driver_comms",
}

# Every valid value of GraphState.delegated_to: the three workers plus the two
# master-owned terminal nodes. Validated on the state model so a node can never
# route to a target the graph has no edge for.
_ROUTABLE_TARGETS: frozenset[str] = frozenset(
    {"route_planning", "disruption", "driver_comms", "clarify", "synthesize", "respond"}
)

# The three dispatchable workers, and the hard cap on how many worker steps a
# single turn may take. There are only three workers and each runs at most once
# per turn, so the cap doubles as a guarantee the loop always terminates even if
# a plan somehow repeats a worker.
_ALL_WORKERS: tuple[str, ...] = ("route_planning", "disruption", "driver_comms")
MAX_WORKER_STEPS: int = 3


def classify_task_type(message: str) -> str:
    """Classify a raw dispatcher message into a canonical task type.

    This is the one place a message is interpreted into a task type; the API and
    the master node both defer to it so the two can never drift apart. Returns
    :data:`CLARIFICATION_NEEDED` when no task type's keywords match.
    """
    lowered = message.casefold()
    for task_type, keywords in _TASK_TYPE_KEYWORDS:
        if any(word in lowered for word in keywords):
            return task_type
    return CLARIFICATION_NEEDED


def _minute_to_clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


# --- Tool-result -> computed_metrics adapters --------------------------------
# When a worker runs as an LLM agent, the authoritative numbers come from the
# real tool executions (see AgentSystem._run_worker_agent). These helpers shape
# a recorded tool result into the same computed_metrics/warnings the worker's
# deterministic path produces, so downstream consumers (the API explainer, the
# synthesizer) see an identical result shape regardless of which path ran.


def _route_metrics_from_tool(
    tool_result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not tool_result.get("available"):
        return {"tool_calls": 1, "hard_violations": 0}, warnings
    unassigned = tool_result.get("unassigned_orders", 0)
    metrics = {
        "tool_calls": 1,
        "hard_violations": tool_result.get("hard_violation_count", 0),
        "unassigned_orders": unassigned,
        "assigned_stops": tool_result.get("assigned_stops", 0),
        "active_routes": tool_result.get("active_routes", 0),
        "vehicles": tool_result.get("vehicles", 0),
        "objective_cost_km": tool_result.get("objective_cost_km", 0.0),
        "plan_status": tool_result.get("plan_status", ""),
    }
    if unassigned:
        warnings.append(f"{unassigned} order(s) could not be assigned within constraints")
    return metrics, warnings


def _disruption_metrics_from_tool(
    tool_result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not tool_result.get("available"):
        return {"tool_calls": 1}, warnings
    added_minutes = tool_result.get("added_minutes_total", 0)
    metrics: dict[str, Any] = {
        "tool_calls": 1,
        "disruption_types": ",".join(tool_result.get("disruption_types", [])),
        "added_minutes_total": added_minutes,
        "objective_cost_delta_km": tool_result.get("objective_cost_delta_km", 0.0),
        "worst_affected_vehicle": tool_result.get("worst_affected_vehicle"),
        "worst_route_duration_minutes": tool_result.get("worst_route_duration_minutes", 0),
    }
    if added_minutes and added_minutes > 0:
        warnings.append(f"active disruption adds {added_minutes} minute(s) across the fleet")
    return metrics, warnings


def _driver_metrics_from_tool(tool_result: dict[str, Any]) -> dict[str, Any]:
    if not tool_result.get("available"):
        return {"tool_calls": 1}
    metrics: dict[str, Any] = {"tool_calls": 1, "driver_id": tool_result.get("driver_id")}
    if tool_result.get("assigned_stops"):
        metrics.update(
            {
                "vehicle_id": tool_result.get("vehicle_id"),
                "assigned_stops": tool_result.get("assigned_stops"),
                "first_eta": tool_result.get("first_eta"),
                "last_eta": tool_result.get("last_eta"),
                "route_distance_km": tool_result.get("route_distance_km"),
            }
        )
    else:
        metrics["assigned_stops"] = 0
    return metrics


def _worker_update(
    state: GraphState,
    node: str,
    result: AgentResult,
    registry: ContractRegistry,
    master_memory: MasterMemory | None = None,
    master_state=None,  # MasterAgentState | None
) -> dict[str, Any]:
    """Standard partial-state update returned by every worker node, after
    enforcing the node's capability contract on its proposed actions.

    Two-tier enforcement (see :func:`contracts.enforce_contract`):

    * A **fatal** finding (a forbidden capability, e.g. an attempt to activate a
      plan or self-send a driver message) escalates the whole turn: the result
      is rewritten to ``ESCALATED`` with no proposed actions, so nothing
      downstream can act on it.
    * A **dropped** finding strips an unsanctioned action but keeps the turn
      going with the remaining allowed actions.

    Every finding is appended to the ``contract_violations`` channel so the API
    layer can emit a serial ``SKILL_VIOLATION`` audit event for each. Sets the
    most-recent ``result``, appends to the additive ``results`` channel, and
    advances the bounded-loop ``step_count``.

    State management additions:
    - Records a WORKER_RUN → ROUTING StateTransition for the turn audit trail.
    - Calls MasterMemory.write_back_from_result to build candidate PROPOSED
      INCIDENT_LESSON items from worker warnings. This runs on whatever
      thread the graph is executing on (a worker-pool thread for the
      streaming/driver-message paths), so write_back_from_result only reads
      MasterMemory and constructs items — it does NOT insert them. The
      candidates ride on GraphState.proposed_lessons and are committed by
      api.py's _finalize_dispatch (via MasterMemory.add_items) on the async
      layer, keeping MasterMemory single-writer. See write_back_from_result's
      docstring for the full rationale.
    - Updates MasterAgentState.completed_workers and memory_proposed_count.

    """
    kept, violations, fatal = enforce_contract(node, registry, result.proposed_actions)
    if fatal:
        result = result.model_copy(
            update={
                "status": "ESCALATED",
                "proposed_actions": (),
                "escalation_reason": (
                    result.escalation_reason
                    or "a proposed action violated this agent's capability contract"
                ),
            }
        )
    elif kept != tuple(result.proposed_actions):
        result = result.model_copy(update={"proposed_actions": tuple(kept)})

    # Memory write-back: propose INCIDENT_LESSON items for any new warnings.
    proposed_lessons: tuple = ()
    if master_memory is not None:
        proposed_lessons = master_memory.write_back_from_result(result, node, enabled=True)

    # Cross-turn state snapshot: track completed workers and lesson count.
    if master_state is not None:
        master_state.completed_workers = (
            *master_state.completed_workers,
            node,
        )
        master_state.memory_proposed_count += len(proposed_lessons)

    # State transition: the worker just finished; control returns to supervisor.
    transition = StateTransition(
        from_phase=TurnPhase.WORKER_RUN,
        to_phase=TurnPhase.ROUTING,
        node=node,
        reason=f"worker '{node}' completed with status {result.status}",
    )

    return {
        "result": result,
        "results": [result],
        "step_count": state.step_count + 1,
        "turn_phase": TurnPhase.ROUTING,
        "state_transitions": [transition],
        "contract_violations": list(violations),
        "dispatched_workers": (*state.dispatched_workers, node),
        # Surface newly-proposed lessons so the API layer can persist them on the
        # async side (the graph runs in a worker thread; it must not persist).
        "proposed_lessons": list(proposed_lessons),
    }


class AgentSystem:
    """Bounded dispatcher graph.

    Worker nodes never receive the master-memory object; they operate only on
    the task and a read-only :class:`AgentContext`. Workers compute real metrics
    but only ever *propose* actions — activation and execution remain with the
    deterministic policy and approval layers.
    """

    def __init__(
        self,
        memory: MasterMemory,
        contracts: ContractRegistry | None = None,
        gateway: OpenAIGateway | None = None,
        master_state=None,  # MasterAgentState | None — avoids circular import
        incident_lesson_recall_limit: int = 5,
    ) -> None:
        self._master_memory = memory
        # Load capability contracts once; a malformed contract fails fast here
        # rather than silently disabling a guardrail at request time.
        self._contracts = contracts if contracts is not None else load_contract_registry()
        # Optional LLM router. When absent (fixture mode / no API key), the
        # supervisor falls back to deterministic keyword routing.
        self._gateway = gateway
        # Cross-turn state snapshot updated at phase transitions (optional).
        self._master_state = master_state
        # Cap on auto-generated INCIDENT_LESSON proposals recalled per turn.
        self._incident_lesson_recall_limit = incident_lesson_recall_limit
        graph = StateGraph(GraphState)
        graph.add_node("supervisor", self._supervisor)
        graph.add_node("route_planning", self._route_planning)
        graph.add_node("disruption", self._disruption)
        graph.add_node("driver_comms", self._driver_comms)
        graph.add_node("clarify", self._clarify)
        graph.add_node("synthesize", self._synthesize)
        graph.add_node("respond", self._respond)
        graph.set_entry_point("supervisor")
        # The supervisor is the loop hub: it dispatches the next planned worker,
        # answers directly via ``respond``, or routes to clarify/synthesize to
        # end the turn.
        graph.add_conditional_edges(
            "supervisor",
            lambda state: state.delegated_to,
            {
                "route_planning": "route_planning",
                "disruption": "disruption",
                "driver_comms": "driver_comms",
                "clarify": "clarify",
                "synthesize": "synthesize",
                "respond": "respond",
            },
        )
        # Workers return to the supervisor so it can dispatch the next one or
        # finish; this is the bounded multi-worker loop.
        graph.add_edge("route_planning", "supervisor")
        graph.add_edge("disruption", "supervisor")
        graph.add_edge("driver_comms", "supervisor")
        graph.add_edge("clarify", END)
        graph.add_edge("synthesize", END)
        graph.add_edge("respond", END)
        self.graph = graph.compile()

    def _plan_turn(
        self, state: GraphState, router_workers: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """Decide the ordered set of workers for this turn under a strict guard.

        Baseline is the keyword classifier's single worker. When the LLM router
        proposed workers (``router_workers``), they are merged in, but the
        result is always passed through the same strict guard: only known
        workers, de-duplicated, each at most once, capped at
        :data:`MAX_WORKER_STEPS`. The LLM can never invent a target, run a worker
        twice, or exceed the cap. If the keyword classifier found nothing and the
        router proposed nothing usable, the plan is empty and the turn clarifies.
        """
        task = state.task
        keyword_target = _TASK_TYPE_TO_WORKER.get(task.task_type.upper())
        plan: list[str] = [keyword_target] if keyword_target else []

        if router_workers:
            # Guard: keep only known workers, in order, without duplicates.
            guarded: list[str] = []
            for worker in router_workers:
                if worker in _ALL_WORKERS and worker not in guarded:
                    guarded.append(worker)
            # Merge the keyword baseline in (so a clear keyword match is never
            # dropped by the LLM) without duplicating it.
            for worker in plan:
                if worker not in guarded:
                    guarded.append(worker)
            if guarded:
                plan = guarded

        return tuple(plan[:MAX_WORKER_STEPS])

    def _supervisor(self, state: GraphState) -> dict[str, Any]:
        """Loop hub: an LLM router decides, plan once, then dispatch or finish.

        On first entry the supervisor asks the LLM router (:meth:`OpenAIGateway.
        route`) how to handle the message. The router may answer conversationally
        — a greeting, small talk, a capability question, or a natural clarifying
        question — in which case the turn routes to ``respond`` with that reply.
        Otherwise it hands back an ordered worker plan, which is merged with the
        deterministic keyword baseline and passed through the same strict guard
        (known workers only, de-duplicated, capped).

        When no gateway is configured (fixture mode / no API key), the router is
        skipped entirely and the supervisor falls back to keyword planning: a
        non-empty plan dispatches workers, an empty plan routes to ``clarify``.

        Each subsequent entry dispatches the next planned worker that has not run
        yet. When the plan is exhausted — or the hard :data:`MAX_WORKER_STEPS`
        cap is reached — it routes to ``synthesize``.

        State transitions emitted:
          - ROUTING → WORKER_RUN  when dispatching a worker
          - ROUTING → SYNTHESIZING when routing to synthesize/respond
          - ROUTING → ROUTING     when the planning step sets a direct_reply
        """
        update: dict[str, Any] = {}
        planned_workers = state.planned_workers

        if not state.planned:
            update["planned"] = True

            # ---- Mid-turn memory recall ----------------------------------------
            # Now that we know the task_type (determined before the graph ran),
            # combine it with the raw message for a richer, ranked recall pass.
            # The results augment the upfront message-level snippets already on
            # GraphState so the LLM router sees both.
            mid_recall_items = self._master_memory.recall_for_turn(
                queries=(state.message, state.task.task_type),
                max_items=6,
            )
            mid_recall_snippets = tuple(item.content for item in mid_recall_items)
            if mid_recall_snippets:
                update["mid_turn_recall"] = mid_recall_snippets
                # Merge with the upfront snippets so the router has everything.
                combined_snippets = state.memory_snippets + mid_recall_snippets
            else:
                combined_snippets = state.memory_snippets

            # ---- Unvetted incident-lesson recall -------------------------------
            # Auto-generated INCIDENT_LESSON proposals (worker warnings written
            # back on prior turns) are recalled here and fed to the router as a
            # SEPARATE, explicitly-unvetted bucket. They are scoped to
            # PROPOSED/INCIDENT_LESSON only and capped by config so unvetted text
            # can never masquerade as curated, human-approved memory and can't
            # grow the prompt without bound.
            incident_lesson_items = self._master_memory.recall_incident_lessons(
                queries=(state.message, state.task.task_type),
                max_items=self._incident_lesson_recall_limit,
            )
            incident_lessons = tuple(item.content for item in incident_lesson_items)
            if incident_lessons:
                update["incident_lessons"] = incident_lessons

            # ---- LLM router ----------------------------------------------------
            decision = None
            if self._gateway is not None and state.message:
                decision = self._gateway.route(
                    state.message,
                    state.conversation_history,
                    combined_snippets,
                    incident_lessons=incident_lessons,
                )
            # Capture the router's navigation intent (if any) up front so it is
            # recorded whether the turn answers directly, clarifies, or dispatches
            # workers. "none" leaves the default so the API emits no directive.
            if decision is not None:
                nav = decision.get("navigation", "none")
                if nav and nav != "none":
                    update["navigation"] = nav

            if decision is not None and decision["action"] == "direct_reply":
                reply = decision["reply"].strip()
                if reply:
                    update["direct_reply"] = reply
                    update["delegated_to"] = "respond"
                    update["turn_phase"] = TurnPhase.SYNTHESIZING
                    update["state_transitions"] = [
                        StateTransition(
                            from_phase=state.turn_phase,
                            to_phase=TurnPhase.SYNTHESIZING,
                            node="supervisor",
                            reason="LLM router chose direct_reply",
                        )
                    ]
                    if self._master_state is not None:
                        self._master_state.current_phase = TurnPhase.SYNTHESIZING
                    return update
                # Router chose to answer directly but gave no text; clarify.
                update["planned_workers"] = ()
                update["delegated_to"] = "clarify"
                update["turn_phase"] = TurnPhase.SYNTHESIZING
                update["state_transitions"] = [
                    StateTransition(
                        from_phase=state.turn_phase,
                        to_phase=TurnPhase.SYNTHESIZING,
                        node="supervisor",
                        reason="LLM router returned empty direct_reply; clarifying",
                    )
                ]
                if self._master_state is not None:
                    self._master_state.current_phase = TurnPhase.SYNTHESIZING
                return update

            router_workers = decision["workers"] if decision is not None else ()
            planned_workers = self._plan_turn(state, router_workers)
            update["planned_workers"] = planned_workers

            # Reflect the planned worker list on the cross-turn state snapshot.
            if self._master_state is not None:
                self._master_state.planned_workers = planned_workers

        # Nothing to do — route to clarify.
        if not planned_workers:
            update["delegated_to"] = "clarify"
            update["turn_phase"] = TurnPhase.SYNTHESIZING
            update["state_transitions"] = [
                StateTransition(
                    from_phase=state.turn_phase,
                    to_phase=TurnPhase.SYNTHESIZING,
                    node="supervisor",
                    reason="no workers matched; routing to clarify",
                )
            ]
            if self._master_state is not None:
                self._master_state.current_phase = TurnPhase.SYNTHESIZING
            return update

        # Bounded loop: stop once every planned worker has run or the cap hits.
        remaining = [w for w in planned_workers if w not in state.dispatched_workers]
        if not remaining or state.step_count >= MAX_WORKER_STEPS:
            update["delegated_to"] = "synthesize"
            update["turn_phase"] = TurnPhase.SYNTHESIZING
            update["state_transitions"] = [
                StateTransition(
                    from_phase=state.turn_phase,
                    to_phase=TurnPhase.SYNTHESIZING,
                    node="supervisor",
                    reason=(
                        "all planned workers completed"
                        if not remaining
                        else f"step cap ({MAX_WORKER_STEPS}) reached"
                    ),
                )
            ]
            if self._master_state is not None:
                self._master_state.current_phase = TurnPhase.SYNTHESIZING
            return update

        next_worker = remaining[0]
        update["delegated_to"] = next_worker
        update["turn_phase"] = TurnPhase.WORKER_RUN
        update["state_transitions"] = [
            StateTransition(
                from_phase=state.turn_phase,
                to_phase=TurnPhase.WORKER_RUN,
                node="supervisor",
                reason=f"dispatching worker '{next_worker}'",
            )
        ]
        if self._master_state is not None:
            self._master_state.current_phase = TurnPhase.WORKER_RUN
        return update

    @staticmethod
    def _respond(state: GraphState) -> dict[str, Any]:
        """Terminal node for a conversational master reply.

        The LLM router decided the message needs no worker (greeting, small talk,
        capability question, or a clarifying question) and supplied the reply in
        ``direct_reply``. This node simply carries it through to the end of the
        turn; it computes nothing and proposes no actions. A ``NEEDS_INPUT``
        result is attached only when the reply is a clarifying question so the
        API can treat it consistently with the deterministic clarify path.
        """
        # The reply is already in state.direct_reply; nothing to compute here.
        return {}

    @staticmethod
    def _clarify(state: GraphState) -> dict[str, Any]:
        # The request did not match any known task type. Rather than guessing a
        # worker, the master asks the dispatcher to clarify what they need.
        task = state.task
        result = AgentResult(
            task_id=task.task_id,
            status="NEEDS_INPUT",
            evidence_references=task.input_references,
            computed_metrics={"tool_calls": 0},
            escalation_reason=(
                "I could not tell whether this is about a route/plan, a disruption, "
                "or a driver's assignment."
            ),
        )
        return {"result": result, "results": [result]}

    @staticmethod
    def _synthesize(state: GraphState) -> dict[str, Any]:
        """Terminal node: combine all accumulated worker results.

        Produces a deterministic, evidence-locked summary reply from every
        result gathered this turn (the union of their evidence references). The
        API layer may replace this with an LLM-phrased version via the existing
        evidence-locked ``explain`` path, but the graph itself never fabricates
        evidence — it only reports what the workers computed. If any worker
        escalated (e.g. a contract violation), that status is surfaced here.
        """
        results = state.results
        if not results:
            # Defensive: synthesize should only be reached after at least one
            # worker ran, but never crash if it wasn't.
            return {"final_reply": "No worker produced a result."}

        escalated = [r for r in results if r.status == "ESCALATED"]
        parts: list[str] = []
        for r in results:
            metric_bits = ", ".join(
                f"{k}={v}" for k, v in r.computed_metrics.items() if k != "tool_calls"
            )
            summary = metric_bits or r.status.lower()
            parts.append(summary)
        if escalated:
            reason = escalated[0].escalation_reason or "an action was blocked by policy"
            reply = f"One or more steps were escalated: {reason}. No action was taken."
        else:
            reply = "; ".join(parts)
        return {"final_reply": reply}

    @staticmethod
    def _tool_context(context: AgentContext) -> ToolContext:
        """Project the read-only AgentContext onto the worker tool context."""
        return ToolContext(
            plan=context.plan,
            fleet=context.fleet,
            orders=context.orders,
            max_stops_per_vehicle=context.max_stops_per_vehicle,
            enforce_delivery_windows=context.enforce_delivery_windows,
            active_disruptions=context.active_disruptions,
            driver_id=context.driver_id,
            traffic_conditions=context.traffic_conditions,
            weather_conditions=context.weather_conditions,
        )

    @staticmethod
    def _task_summary(state: GraphState) -> dict[str, Any]:
        """Build the shared task summary handed to every worker agent.

        Beyond the raw message and task type, this surfaces the concrete facts a
        worker needs to answer the actual question rather than run a generic
        analysis: the specific driver the dispatcher named (if any), and whether
        a concrete disruption was actually located in the current context. When
        no disruption is present the worker is told so explicitly, so it reports
        the gap honestly instead of silently analysing nothing.
        """
        context = state.context or AgentContext()
        return {
            "task_type": state.task.task_type,
            "message": state.message,
            "enquiring_driver_id": context.driver_id,
            "has_active_disruption": bool(context.active_disruptions),
            "active_disruption_count": len(context.active_disruptions),
        }

    def _run_worker_agent(
        self,
        system_prompt: str,
        task_summary: dict[str, Any],
        registry: ToolRegistry,
        tool_context: ToolContext,
    ) -> dict[str, dict[str, Any]] | None:
        """Drive a worker as an LLM tool-calling loop; return real tool outputs.

        The LLM decides which tools to call and in what order, but the metrics we
        return come from the ACTUAL tool executions recorded here — never from
        model free-text — so the model orchestrates without being able to
        fabricate numbers. Returns a mapping of ``tool_name -> real_result`` for
        every tool the model actually invoked, or ``None`` when the gateway is
        unavailable / the loop produced no usable tool output (the worker then
        falls back to deterministic computation).
        """
        # Skip the LLM path unless a real gateway with an OpenAI client and the
        # tool-calling loop is present. Test stubs that only implement route()
        # (no client / no run_agent) fall straight through to the deterministic
        # path, keeping existing routing tests hermetic.
        if (
            self._gateway is None
            or getattr(self._gateway, "client", None) is None
            or not hasattr(self._gateway, "run_agent")
        ):
            return None

        recorded: dict[str, dict[str, Any]] = {}

        def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            result = registry.run(name, tool_context, args)
            recorded[name] = result
            return result

        # A permissive finalize schema: the model signals completion; the numbers
        # we keep come from ``recorded``, so we don't constrain its fields.
        finalize_schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": True,
        }
        try:
            findings = self._gateway.run_agent(
                system_prompt,
                task_summary,
                registry.openai_tools(),
                run_tool,
                finalize_schema,
            )
        except ToolError:
            return None
        if findings is None or not recorded:
            return None
        return recorded

    def _route_planning(self, state: GraphState) -> dict[str, Any]:
        task = state.task
        context = state.context or AgentContext()
        # LLM path: let the agent orchestrate its tools; keep the real outputs.
        recorded = self._run_worker_agent(
            ROUTE_PLANNING_PROMPT,
            self._task_summary(state),
            ROUTE_PLANNING_TOOLS,
            self._tool_context(context),
        )
        if recorded is not None and "validate_current_plan" in recorded:
            metrics, warnings = _route_metrics_from_tool(recorded["validate_current_plan"])
            result = AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics=metrics,
                proposed_actions=({"type": "GENERATE_CANDIDATE_PLAN"},),
                warnings=tuple(warnings),
            )
            return _worker_update(
                state,
                "route_planning",
                result,
                self._contracts,
                self._master_memory,
                self._master_state,
            )

        # Deterministic fallback (no API key, or the agent produced no tool call).
        metrics: dict[str, Any] = {"tool_calls": 0}
        warnings: list[str] = []
        if context.plan is not None:
            # Tool 1: re-validate the current plan against every hard constraint.
            violations = validate_plan(
                context.plan,
                context.fleet,
                context.orders,
                context.max_stops_per_vehicle,
                enforce_delivery_windows=context.enforce_delivery_windows,
            )
            # Tool 2: summarize assignment coverage from the plan itself.
            assigned = sum(len(route.stops) for route in context.plan.routes)
            unassigned = sum(1 for v in violations if v.startswith("unassigned"))
            active_routes = sum(1 for route in context.plan.routes if route.stops)
            metrics = {
                "tool_calls": 2,
                "hard_violations": len(violations),
                "unassigned_orders": unassigned,
                "assigned_stops": assigned,
                "active_routes": active_routes,
                "vehicles": len(context.plan.routes),
                "objective_cost_km": context.plan.objective_cost,
                "plan_status": context.plan.status,
            }
            if unassigned:
                warnings.append(f"{unassigned} order(s) could not be assigned within constraints")
        else:
            # No plan snapshot (e.g. unit test): stay bounded and non-committal.
            metrics["hard_violations"] = 0
        result = AgentResult(
            task_id=task.task_id,
            status="COMPLETED",
            evidence_references=task.input_references,
            computed_metrics=metrics,
            proposed_actions=({"type": "GENERATE_CANDIDATE_PLAN"},),
            warnings=tuple(warnings),
        )
        return _worker_update(
            state,
            "route_planning",
            result,
            self._contracts,
            self._master_memory,
            self._master_state,
        )

    def _disruption(self, state: GraphState) -> dict[str, Any]:
        task = state.task
        context = state.context or AgentContext()
        # LLM path: let the agent orchestrate its tools; keep the real outputs.
        recorded = self._run_worker_agent(
            DISRUPTION_PROMPT,
            self._task_summary(state),
            DISRUPTION_TOOLS,
            self._tool_context(context),
        )
        if recorded is not None and "assess_disruption_impact" in recorded:
            metrics, warnings = _disruption_metrics_from_tool(
                recorded["assess_disruption_impact"]
            )
            result = AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics=metrics,
                proposed_actions=({"type": "BOUNDED_REPLAN", "requires_policy_check": True},),
                warnings=tuple(warnings),
            )
            return _worker_update(
                state,
                "disruption",
                result,
                self._contracts,
                self._master_memory,
                self._master_state,
            )

        # Deterministic fallback (no API key, or the agent produced no tool call).
        metrics: dict[str, Any] = {"tool_calls": 0}
        warnings: list[str] = []
        if context.plan is not None and context.plan.routes and context.active_disruptions:
            # Tool 1: recompute the plan's timing under the active road-closure
            # and/or heavy-rain disruptions, keeping the same stop assignment.
            # This is a real, deterministic before/after delta, not an estimate.
            speed_context = disruption_speed_kph_by_stop(
                context.orders, context.active_disruptions, None
            )
            stressed = reapply_route_timing(
                context.plan,
                context.fleet,
                context.orders,
                speed_context,
                enforce_delivery_windows=context.enforce_delivery_windows,
            )
            # Tool 2: compare against the plan as it stands today.
            before_duration = sum(route.duration_minutes for route in context.plan.routes)
            after_duration = sum(route.duration_minutes for route in stressed.routes)
            added_minutes = after_duration - before_duration
            worst_route = max(
                stressed.routes, key=lambda route: route.duration_minutes, default=None
            )
            metrics = {
                "tool_calls": 2,
                "disruption_types": ",".join(
                    sorted({str(e.event_type) for e in context.active_disruptions})
                ),
                "added_minutes_total": added_minutes,
                "objective_cost_delta_km": round(
                    stressed.objective_cost - context.plan.objective_cost, 2
                ),
                "worst_affected_vehicle": worst_route.vehicle_id if worst_route else None,
                "worst_route_duration_minutes": (
                    worst_route.duration_minutes if worst_route else 0
                ),
            }
            if added_minutes > 0:
                warnings.append(
                    f"active disruption adds {added_minutes} minute(s) across the fleet"
                )
        elif context.plan is not None and context.plan.routes:
            metrics["tool_calls"] = 1
            metrics["added_minutes_total"] = 0
        else:
            metrics["tool_calls"] = 1
        result = AgentResult(
            task_id=task.task_id,
            status="COMPLETED",
            evidence_references=task.input_references,
            computed_metrics=metrics,
            proposed_actions=({"type": "BOUNDED_REPLAN", "requires_policy_check": True},),
            warnings=tuple(warnings),
        )
        return _worker_update(
            state,
            "disruption",
            result,
            self._contracts,
            self._master_memory,
            self._master_state,
        )

    def _driver_comms(self, state: GraphState) -> dict[str, Any]:
        task = state.task
        context = state.context or AgentContext()
        # A DRIVER_DISPATCH turn is a request to SEND a driver their route, not a
        # read-only enquiry. The worker still only *proposes* the send (with
        # send=False, requires_confirmation=True); the API layer holds it pending
        # until the dispatcher confirms, then performs the actual Telegram send.
        # Anything else stays the classic read-only DRAFT_DRIVER_REPLY.
        is_dispatch = task.task_type.upper() == "DRIVER_DISPATCH"

        def _proposed_action(driver_id: str | None) -> dict[str, Any]:
            if is_dispatch:
                return {
                    "type": "SEND_DRIVER_ROUTE",
                    "driver_id": driver_id,
                    "send": False,
                    "requires_confirmation": True,
                }
            return {"type": "DRAFT_DRIVER_REPLY", "send": False}

        # LLM path: let the agent orchestrate its tools; keep the real outputs.
        recorded = self._run_worker_agent(
            DRIVER_COMMS_PROMPT,
            self._task_summary(state),
            DRIVER_COMMS_TOOLS,
            self._tool_context(context),
        )
        if recorded is not None and "lookup_driver_assignment" in recorded:
            metrics = _driver_metrics_from_tool(recorded["lookup_driver_assignment"])
            result = AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics=metrics,
                proposed_actions=(_proposed_action(metrics.get("driver_id")),),
            )
            return _worker_update(
                state,
                "driver_comms",
                result,
                self._contracts,
                self._master_memory,
                self._master_state,
            )

        # Deterministic fallback (no API key, or the agent produced no tool call).
        metrics: dict[str, Any] = {"tool_calls": 1}
        route = None
        if context.plan is not None and context.driver_id:
            # Tool 1: look up the enquiring driver's own assignment (and nothing
            # about other drivers, preserving the driver-scoped boundary).
            route = next(
                (r for r in context.plan.routes if r.driver_id == context.driver_id), None
            )
        if route is not None and route.stops:
            first, last = route.stops[0], route.stops[-1]
            metrics = {
                "tool_calls": 1,
                "driver_id": context.driver_id,
                "vehicle_id": route.vehicle_id,
                "assigned_stops": len(route.stops),
                "first_eta": _minute_to_clock(first.eta_minute),
                "last_eta": _minute_to_clock(last.eta_minute),
                "route_distance_km": route.distance_km,
            }
        elif context.driver_id:
            metrics = {"tool_calls": 1, "driver_id": context.driver_id, "assigned_stops": 0}
        result = AgentResult(
            task_id=task.task_id,
            status="COMPLETED",
            evidence_references=task.input_references,
            computed_metrics=metrics,
            proposed_actions=(_proposed_action(context.driver_id),),
        )
        return _worker_update(
            state,
            "driver_comms",
            result,
            self._contracts,
            self._master_memory,
            self._master_state,
        )

    def run(
        self,
        task: AgentTask,
        context: AgentContext | None = None,
        message: str = "",
        memory_snippets: tuple[str, ...] = (),
        conversation_history: tuple[tuple[str, str], ...] = (),
    ) -> GraphState:
        """Run the bounded multi-worker loop and return the full final state.

        Callers that need every worker result, the synthesized reply, and the
        accumulated contract violations (e.g. the API layer emitting serial audit
        events) use this. :meth:`invoke` is the thin single-result wrapper.
        """
        state = GraphState(
            task=task,
            context=context or AgentContext(),
            message=message,
            memory_snippets=memory_snippets,
            conversation_history=conversation_history,
        )
        # A single-message turn can dispatch up to MAX_WORKER_STEPS workers plus
        # the supervisor between each; give LangGraph enough steps to finish the
        # loop without its own recursion guard tripping.
        final_dict: dict[str, Any] = self.graph.invoke(
            state, {"recursion_limit": 2 * MAX_WORKER_STEPS + 5}
        )
        return GraphState.model_validate(final_dict)

    # Human-readable label for each graph node, surfaced as a streamed step so a
    # caller can show TRUE per-node progress (not a simulated cycle).
    _NODE_LABELS: dict[str, str] = {
        "supervisor": "Routing your request…",
        "route_planning": "Route-planning agent: checking the plan…",
        "disruption": "Disruption agent: assessing impact…",
        "driver_comms": "Driver-comms agent: looking up the assignment…",
        "clarify": "Preparing a clarifying question…",
        "synthesize": "Composing the reply…",
        "respond": "Composing the reply…",
    }

    def stream_run(
        self,
        task: AgentTask,
        context: AgentContext | None = None,
        message: str = "",
        memory_snippets: tuple[str, ...] = (),
        conversation_history: tuple[tuple[str, str], ...] = (),
    ):
        """Run the graph, yielding a real step event as each node executes.

        Yields ``("step", {"node": <name>, "label": <text>})`` after every node
        the graph runs, in order, then a final ``("final", GraphState)``. Unlike
        the timed UI cycle, these steps are the ACTUAL nodes that ran, so a
        streaming caller (the SSE endpoint) can show truthful per-node progress.
        The post-processing (audit, reply) stays with the caller, exactly as for
        :meth:`run`, so the two paths cannot diverge.
        """
        state = GraphState(
            task=task,
            context=context or AgentContext(),
            message=message,
            memory_snippets=memory_snippets,
            conversation_history=conversation_history,
        )
        config = {"recursion_limit": 2 * MAX_WORKER_STEPS + 5}
        merged: dict[str, Any] = {}
        # stream_mode="updates" yields {node_name: partial_state_update} after
        # each node runs. We accumulate the updates to reconstruct the final
        # state (the supervisor's channels use last-value-wins; the additive
        # reducers we replay explicitly below).
        for chunk in self.graph.stream(state, config, stream_mode="updates"):
            for node, update in chunk.items():
                yield "step", {"node": node, "label": self._NODE_LABELS.get(node, node)}
                if not isinstance(update, dict):
                    continue
                additive_channels = ("results", "contract_violations", "proposed_lessons")
                for key, value in update.items():
                    if key in additive_channels and isinstance(value, list):
                        # Additive-reducer channels: append, don't overwrite.
                        merged.setdefault(key, [])
                        merged[key].extend(value)
                    else:
                        merged[key] = value
        # Reconstruct the full final state from the initial state + accumulated
        # updates so the caller gets the same object shape as run().
        final_state = state.model_copy(update=merged)
        yield "final", final_state

    def invoke(
        self,
        task: AgentTask,
        context: AgentContext | None = None,
        message: str = "",
        memory_snippets: tuple[str, ...] = (),
    ) -> AgentResult:
        """Run the graph and return the most recent single worker result.

        Retained for single-result callers and existing tests. The last worker
        result is representative for a single-worker turn; multi-worker callers
        should use :meth:`run` to see every result.
        """
        final = self.run(task, context, message, memory_snippets)
        if final.result is not None:
            return final.result
        if final.results:
            return final.results[-1]
        # No worker ran (e.g. clarify path with no result set): synthesize a
        # NEEDS_INPUT so callers always get a result object.
        raise RuntimeError("agent graph produced no result")

    def handle_driver_message(
        self,
        message: str,
        driver_id: str,
        plan: PlanVersion | None = None,
        fleet: tuple[Vehicle, ...] | None = None,
        orders: tuple[Order, ...] | None = None,
    ) -> dict[str, Any]:
        """Handle a free-text message sent by a driver over Telegram.

        Runs a single-turn LLM agent with the driver-facing tools and prompt,
        then returns a dict with:
          - ``reply``             (str)       — plain-text confirmation to send back.
          - ``breakdown_report``  (dict|None) — set when a breakdown was logged.
          - ``send_full_route``   (bool)      — True when the driver asked for their
                                  full schedule; the webhook caller should send
                                  the formatted ``format_route_message`` HTML instead
                                  of (or in addition to) the plain reply.

        Falls back to a short deterministic reply when no OpenAI gateway is
        configured or the LLM loop fails, so the driver always gets an answer.
        """
        tool_context = ToolContext(
            plan=plan,
            fleet=tuple(fleet) if fleet else (),
            orders=tuple(orders) if orders else (),
            driver_id=driver_id,
        )

        breakdown_report: dict[str, Any] | None = None
        send_full_route = False
        recorded: dict[str, dict[str, Any]] = {}

        def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            result = DRIVER_AGENT_TOOLS.run(name, tool_context, args)
            recorded[name] = result
            return result

        # finalize_schema: the model submits its short plain-text reply.
        finalize_schema = {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "The plain-text reply to send to the driver.",
                }
            },
            "required": ["reply"],
            "additionalProperties": False,
        }

        findings: dict[str, Any] | None = None
        if (
            self._gateway is not None
            and getattr(self._gateway, "client", None) is not None
            and hasattr(self._gateway, "run_agent")
        ):
            task_summary = {
                "driver_id": driver_id,
                "message": message,
                "has_active_plan": plan is not None,
            }
            try:
                findings = self._gateway.run_agent(
                    DRIVER_AGENT_PROMPT,
                    task_summary,
                    DRIVER_AGENT_TOOLS.openai_tools(),
                    run_tool,
                    finalize_schema,
                )
            except (ToolError, Exception):  # noqa: BLE001
                findings = None

        # --- Interpret tool results -------------------------------------------------

        # Breakdown report: audit payload for the webhook caller.
        if "report_vehicle_breakdown" in recorded:
            br = recorded["report_vehicle_breakdown"]
            if br.get("recorded"):
                breakdown_report = br

        # Route dispatch request: signal the webhook to send the full HTML message.
        if "request_route_dispatch" in recorded:
            rr = recorded["request_route_dispatch"]
            if rr.get("send_route"):
                send_full_route = True

        # --- Build the short confirmation reply ------------------------------------

        # LLM ran and gave a reply — use it directly; it understood intent freely.
        if findings and isinstance(findings.get("reply"), str) and findings["reply"].strip():
            reply = findings["reply"].strip()
        elif send_full_route:
            # LLM triggered a route send but gave no reply text.
            reply = (
                "Sending your full route schedule now, including navigation links "
                "for each stop."
            )
        elif breakdown_report:
            vehicle_label = (
                breakdown_report.get("vehicle_label")
                or breakdown_report.get("license_plate")
                or breakdown_report.get("vehicle_id")
            )
            vehicle_phrase = f" for vehicle {vehicle_label}" if vehicle_label else ""
            reply = (
                f"Breakdown logged{vehicle_phrase}. The dispatcher has been "
                "informed. Please wait for further instructions."
            )
        elif "get_driver_route_summary" in recorded:
            r = recorded["get_driver_route_summary"]
            if r.get("available") and r.get("assigned_stops"):
                reply = (
                    f"You have {r['assigned_stops']} stop(s) assigned. "
                    f"Next ETA: {r.get('next_stop_eta', 'unknown')}. "
                    f"Last stop ETA: {r.get('last_eta', 'unknown')}."
                )
            else:
                reply = (
                    "You have no stops assigned in the current plan. Contact the "
                    "dispatcher if this is unexpected."
                )
        else:
            # No LLM, no tool results — generic fallback. Do NOT keyword-guess
            # intent here; that leads to false positives and missed cases.
            # The LLM is the intent layer; this is purely a "no AI available" notice.
            reply = (
                "I received your message but couldn't process it right now. "
                "Please contact the dispatcher directly."
            )

        return {
            "reply": reply,
            "breakdown_report": breakdown_report,
            "send_full_route": send_full_route,
        }
