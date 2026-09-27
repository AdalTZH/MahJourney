import json

import pytest
from pydantic import ValidationError

from mahjourney.contracts import (
    NODE_TO_AGENT,
    CapabilityContract,
    enforce_contract,
    load_contract_registry,
    load_contracts,
)


def test_all_six_contracts_load_and_validate() -> None:
    contracts = load_contracts()
    # There are exactly six versioned contracts in backend/contracts.
    assert len(contracts) == 6
    names = {c.name for c in contracts}
    assert names == {
        "route_planning",
        "disruption_analysis",
        "driver_enquiry",
        "approval_coordination",
        "explain_tradeoffs",
        "memory_review",
    }
    # Every contract carries the required, well-typed fields.
    for contract in contracts:
        assert isinstance(contract, CapabilityContract)
        assert contract.version
        assert contract.agent
        assert isinstance(contract.tools, tuple)
        assert isinstance(contract.forbidden, tuple)


def test_registry_lookup_by_agent_and_node() -> None:
    registry = load_contract_registry()
    # The route-planning worker node resolves to its contract and tool set.
    contracts = registry.for_node("route_planning")
    assert len(contracts) == 1
    assert contracts[0].name == "route_planning"
    assert "activate_plan" in registry.forbidden_capabilities("route_planning")
    assert "ortools" in registry.allowed_tools("route_planning")

    # The master agent owns several contracts; look them up by agent id.
    master = registry.for_agent("master-dispatcher-agent")
    assert {c.name for c in master} == {
        "approval_coordination",
        "explain_tradeoffs",
        "memory_review",
    }

    # Every worker node in the mapping resolves to at least one contract.
    for node in NODE_TO_AGENT:
        assert registry.for_node(node), node

    # An unbound node (e.g. the supervisor) has no worker contract.
    assert registry.for_node("supervisor") == ()


def test_malformed_contract_raises(tmp_path) -> None:
    # Missing the required "agent" field.
    (tmp_path / "broken.v1.json").write_text(
        json.dumps({"name": "broken", "version": "1.0.0", "tools": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid capability contract broken.v1.json"):
        load_contracts(tmp_path)


def test_unknown_field_in_contract_is_rejected(tmp_path) -> None:
    # A typo ("tool" instead of "tools") must fail rather than silently drop a guardrail.
    (tmp_path / "typo.v1.json").write_text(
        json.dumps(
            {
                "name": "typo",
                "version": "1.0.0",
                "agent": "x",
                "tool": ["a"],
                "forbidden": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid capability contract typo.v1.json"):
        load_contracts(tmp_path)


def test_invalid_json_raises(tmp_path) -> None:
    (tmp_path / "bad.v1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON in capability contract bad.v1.json"):
        load_contracts(tmp_path)


def test_empty_directory_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="no capability contracts found"):
        load_contracts(tmp_path)


def test_missing_directory_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="contracts directory not found"):
        load_contracts(tmp_path / "does-not-exist")


def test_contract_rejects_blank_tool_entries() -> None:
    with pytest.raises(ValidationError):
        CapabilityContract(name="x", version="1", agent="a", tools=("",))


# --- Two-tier enforcement ----------------------------------------------------


def test_allowed_action_is_kept() -> None:
    reg = load_contract_registry()
    kept, violations, fatal = enforce_contract(
        "route_planning", reg, ({"type": "GENERATE_CANDIDATE_PLAN"},)
    )
    assert fatal is False
    assert violations == ()
    assert kept == ({"type": "GENERATE_CANDIDATE_PLAN"},)


def test_disruption_bounded_replan_is_kept() -> None:
    reg = load_contract_registry()
    kept, violations, fatal = enforce_contract(
        "disruption", reg, ({"type": "BOUNDED_REPLAN", "requires_policy_check": True},)
    )
    assert fatal is False
    assert violations == ()
    assert len(kept) == 1


def test_driver_draft_reply_is_kept() -> None:
    reg = load_contract_registry()
    kept, violations, fatal = enforce_contract(
        "driver_comms", reg, ({"type": "DRAFT_DRIVER_REPLY", "send": False},)
    )
    assert fatal is False
    assert violations == ()
    assert len(kept) == 1


def test_forbidden_activate_plan_is_fatal() -> None:
    reg = load_contract_registry()
    # A forged proposal to activate a plan from the route-planning worker.
    kept, violations, fatal = enforce_contract(
        "route_planning", reg, ({"type": "ACTIVATE_PLAN"},)
    )
    assert fatal is True
    assert kept == ()  # the fatal action is never kept
    assert any(v.severity == "FATAL" for v in violations)
    assert "activate_plan" in violations[0].reason


def test_driver_send_flag_is_fatal_for_driver_comms() -> None:
    reg = load_contract_registry()
    # driver_enquiry forbids send_driver_message; a draft flagged send=True trips it.
    kept, violations, fatal = enforce_contract(
        "driver_comms", reg, ({"type": "DRAFT_DRIVER_REPLY", "send": True},)
    )
    assert fatal is True
    assert kept == ()
    assert "send_driver_message" in violations[0].reason


def test_cross_driver_assignment_is_fatal() -> None:
    reg = load_contract_registry()
    kept, violations, fatal = enforce_contract(
        "driver_comms", reg, ({"type": "CROSS_DRIVER_ASSIGNMENT"},)
    )
    assert fatal is True
    assert kept == ()


def test_unknown_action_type_is_dropped_not_fatal() -> None:
    reg = load_contract_registry()
    kept, violations, fatal = enforce_contract(
        "route_planning", reg, ({"type": "SOMETHING_WEIRD"},)
    )
    assert fatal is False
    assert kept == ()
    assert violations[0].severity == "DROPPED"
    assert "unknown action type" in violations[0].reason


def test_wrong_tool_for_node_is_dropped() -> None:
    reg = load_contract_registry()
    # DRAFT_DRIVER_REPLY's tool (draft_reply) is not in route_planning's tools,
    # and it crosses no forbidden boundary, so it is dropped, not fatal.
    kept, violations, fatal = enforce_contract(
        "route_planning", reg, ({"type": "DRAFT_DRIVER_REPLY", "send": False},)
    )
    assert fatal is False
    assert kept == ()
    assert violations[0].severity == "DROPPED"


def test_mixed_actions_keep_allowed_drop_unknown_flag_fatal() -> None:
    reg = load_contract_registry()
    actions = (
        {"type": "GENERATE_CANDIDATE_PLAN"},  # allowed -> kept
        {"type": "SOMETHING_WEIRD"},          # unknown -> dropped
        {"type": "ACTIVATE_PLAN"},            # forbidden -> fatal
    )
    kept, violations, fatal = enforce_contract("route_planning", reg, actions)
    assert fatal is True
    assert kept == ({"type": "GENERATE_CANDIDATE_PLAN"},)
    severities = sorted(v.severity for v in violations)
    assert severities == ["DROPPED", "FATAL"]


def test_unbound_node_keeps_actions_unchanged() -> None:
    reg = load_contract_registry()
    actions = ({"type": "ANYTHING"},)
    kept, violations, fatal = enforce_contract("supervisor", reg, actions)
    assert kept == actions
    assert violations == ()
    assert fatal is False
