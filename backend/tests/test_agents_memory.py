from mahjourney.agents import AgentSystem
from mahjourney.domain import AgentTask
from mahjourney.memory import MasterMemory


def task(task_type: str) -> AgentTask:
    return AgentTask(task_type=task_type, requester="test", conversation_id="test")


def test_agent_sequences_are_bounded_and_non_authoritative() -> None:
    agents = AgentSystem(MasterMemory())
    result = agents.invoke(task("ROUTE_PLAN"))
    assert result.status == "COMPLETED"
    assert result.computed_metrics["tool_calls"] <= 3
    assert result.proposed_actions[0]["type"] == "GENERATE_CANDIDATE_PLAN"
    assert all(action.get("activate") is not True for action in result.proposed_actions)


def test_external_memory_requires_human_curation() -> None:
    memory = MasterMemory()
    item = memory.propose("INCIDENT_LESSON", "Avoid Pioneer Road after flooding")
    assert memory.search("Pioneer") == ()
    curated = memory.curate(item.memory_id)
    assert curated.trust_label == "HUMAN_APPROVED"
    assert memory.search("Pioneer")[0].status == "CURATED"
