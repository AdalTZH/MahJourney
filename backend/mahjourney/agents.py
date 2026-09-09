from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .domain import AgentResult, AgentTask
from .memory import MasterMemory


class GraphState(TypedDict, total=False):
    task: AgentTask
    result: AgentResult
    delegated_to: str


class AgentSystem:
    """Bounded dispatcher graph. Worker nodes never receive the master memory object."""

    def __init__(self, memory: MasterMemory) -> None:
        self._master_memory = memory
        graph = StateGraph(GraphState)
        graph.add_node("master", self._master)
        graph.add_node("route_planning", self._route_planning)
        graph.add_node("disruption", self._disruption)
        graph.add_node("driver_comms", self._driver_comms)
        graph.set_entry_point("master")
        graph.add_conditional_edges(
            "master",
            lambda state: state["delegated_to"],
            {
                "route_planning": "route_planning",
                "disruption": "disruption",
                "driver_comms": "driver_comms",
            },
        )
        graph.add_edge("route_planning", END)
        graph.add_edge("disruption", END)
        graph.add_edge("driver_comms", END)
        self.graph = graph.compile()

    def _master(self, state: GraphState) -> GraphState:
        task = state["task"]
        task_type = task.task_type.upper()
        if any(term in task_type for term in ("ROUTE", "PLAN", "OPTIMIZE")):
            delegated_to = "route_planning"
        elif any(
            term in task_type for term in ("CLOSURE", "BREAKDOWN", "RAIN", "DISRUPTION", "URGENT")
        ):
            delegated_to = "disruption"
        else:
            delegated_to = "driver_comms"
        return {"delegated_to": delegated_to}

    @staticmethod
    def _route_planning(state: GraphState) -> GraphState:
        task = state["task"]
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics={"tool_calls": 3, "hard_violations": 0},
                proposed_actions=({"type": "GENERATE_CANDIDATE_PLAN"},),
            )
        }

    @staticmethod
    def _disruption(state: GraphState) -> GraphState:
        task = state["task"]
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics={"tool_calls": 2},
                proposed_actions=({"type": "BOUNDED_REPLAN", "requires_policy_check": True},),
            )
        }

    @staticmethod
    def _driver_comms(state: GraphState) -> GraphState:
        task = state["task"]
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics={"tool_calls": 1},
                proposed_actions=({"type": "DRAFT_DRIVER_REPLY", "send": False},),
            )
        }

    def invoke(self, task: AgentTask) -> AgentResult:
        final: dict[str, Any] = self.graph.invoke({"task": task})
        return final["result"]
