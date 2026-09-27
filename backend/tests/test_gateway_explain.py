"""Tests for the OpenAIGateway explanation helpers.

Only the no-client (offline) behaviour is asserted here: without an API key the
gateway must return the deterministic fallback verbatim and never raise. The
live LLM phrasing path requires a real key and is exercised manually.
"""

from mahjourney.config import Settings
from mahjourney.domain import AgentResult, AgentTask
from mahjourney.openai_gateway import OpenAIGateway


def _gateway_without_client() -> OpenAIGateway:
    # No OPENAI_API_KEY -> client is None -> all LLM methods short-circuit.
    return OpenAIGateway(Settings(openai_api_key=""))


def _task() -> AgentTask:
    return AgentTask(task_type="DISRUPTION_ANALYSIS", requester="t", conversation_id="c")


def test_explain_multi_returns_fallback_without_client() -> None:
    gw = _gateway_without_client()
    assert gw.client is None
    results = (
        AgentResult(task_id="a", status="COMPLETED", computed_metrics={"added_minutes_total": 0}),
        AgentResult(task_id="b", status="COMPLETED", computed_metrics={"hard_violations": 0}),
    )
    fallback = "deterministic summary"
    assert gw.explain_multi(_task(), results, fallback) == fallback


def test_explain_multi_handles_none_metric_values_without_client() -> None:
    # Regression guard: a None-valued metric must not break the offline path.
    gw = _gateway_without_client()
    results = (
        AgentResult(
            task_id="a",
            status="COMPLETED",
            computed_metrics={"worst_affected_vehicle": None, "added_minutes_total": 0},
        ),
    )
    assert gw.explain_multi(_task(), results, "fallback text") == "fallback text"


def test_explain_candidate_note_appended_to_fallback_without_client() -> None:
    # When a candidate plan note is supplied and there's no client, the offline
    # fallback still surfaces it (so the dispatcher learns a plan was prepared).
    gw = _gateway_without_client()
    results = (AgentResult(task_id="a", status="COMPLETED", computed_metrics={}),)
    note = "A candidate plan was prepared but not activated."
    out = gw.explain_multi(_task(), results, "base reply", note)
    assert note in out and "base reply" in out
