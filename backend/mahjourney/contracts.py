"""Capability-contract loading, validation, and enforcement.

The ``backend/contracts/*.json`` files declare, per agent, the tools a worker is
allowed to use and the capabilities it is forbidden from exercising. These are
authorization guardrails — NOT agent "skills" in the prompt-instruction sense —
so they are named contracts. This module makes them load-bearing:

* :class:`CapabilityContract` is a strict Pydantic model; a malformed contract
  fails fast at load time rather than silently disabling a guardrail.
* :class:`ContractRegistry` loads every contract once and indexes it by
  ``agent`` id so the graph can look up a worker's contract by the node it ran.
* :func:`enforce_contract` applies a two-tier check to a worker's proposed
  actions.

The registry is deliberately small and dependency-free so it can be constructed
once at application startup and shared read-only across requests.

Enforcement runs in the worker return path before proposed actions reach the
supervisor or API layer.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Directory holding the versioned capability contracts. Resolved relative to the
# backend package root (…/backend/contracts) so it works regardless of the
# process working directory.
CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

# Canonical mapping from a graph worker node name to the ``agent`` id used
# inside the capability contracts. The three worker nodes each own one contract;
# the supervisor/master owns the remaining master-scoped contracts but is not a
# worker subject to per-action enforcement, so it is intentionally absent here.
NODE_TO_AGENT: dict[str, str] = {
    "route_planning": "route-planning-agent",
    "disruption": "disruption-analyst-agent",
    "driver_comms": "driver-communications-agent",
}


class CapabilityContract(BaseModel):
    """A single versioned capability contract from ``contracts/<name>.vN.json``.

    Frozen because a contract is a fixed policy artifact for the life of the
    process; it must not be mutated after load. Extra keys are rejected so a
    typo in a contract file (e.g. ``tool`` instead of ``tools``) surfaces as a
    load error rather than a silently missing guardrail.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()

    @field_validator("tools", "forbidden")
    @classmethod
    def _no_blank_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entry.strip() for entry in value):
            raise ValueError("tool/forbidden entries must be non-empty strings")
        return value


class ContractRegistry:
    """Read-only index of capability contracts keyed by agent id.

    A single agent id (e.g. ``master-dispatcher-agent``) may map to several
    contracts (explain, approval, memory), so the registry stores a tuple of
    contracts per agent and exposes helpers to resolve the allowed/forbidden
    capability sets for a worker node.
    """

    def __init__(self, contracts: tuple[CapabilityContract, ...]) -> None:
        by_agent: dict[str, list[CapabilityContract]] = {}
        for contract in contracts:
            by_agent.setdefault(contract.agent, []).append(contract)
        self._by_agent: dict[str, tuple[CapabilityContract, ...]] = {
            agent: tuple(items) for agent, items in by_agent.items()
        }
        self._contracts = contracts

    @property
    def contracts(self) -> tuple[CapabilityContract, ...]:
        return self._contracts

    def for_agent(self, agent: str) -> tuple[CapabilityContract, ...]:
        return self._by_agent.get(agent, ())

    def for_node(self, node: str) -> tuple[CapabilityContract, ...]:
        """Contracts governing a graph worker node (empty if the node is unbound)."""
        agent = NODE_TO_AGENT.get(node)
        return self.for_agent(agent) if agent else ()

    def allowed_tools(self, node: str) -> frozenset[str]:
        """Union of every tool the node's contract(s) permit."""
        return frozenset(
            tool for contract in self.for_node(node) for tool in contract.tools
        )

    def forbidden_capabilities(self, node: str) -> frozenset[str]:
        """Union of every capability the node's contract(s) forbid."""
        return frozenset(
            cap for contract in self.for_node(node) for cap in contract.forbidden
        )


def load_contracts(contracts_dir: Path | None = None) -> tuple[CapabilityContract, ...]:
    """Load and validate every ``*.json`` contract in ``contracts_dir``.

    Fails fast: a missing directory, unreadable file, invalid JSON, or a file
    that does not satisfy :class:`CapabilityContract` raises immediately, so a
    broken guardrail can never be silently skipped at startup.
    """
    directory = contracts_dir or CONTRACTS_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"contracts directory not found: {directory}")
    contracts: list[CapabilityContract] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in capability contract {path.name}: {exc}") from exc
        try:
            contracts.append(CapabilityContract.model_validate(raw))
        except Exception as exc:  # pydantic ValidationError and friends
            raise ValueError(f"invalid capability contract {path.name}: {exc}") from exc
    if not contracts:
        raise ValueError(f"no capability contracts found in {directory}")
    return tuple(contracts)


def load_contract_registry(contracts_dir: Path | None = None) -> ContractRegistry:
    """Convenience constructor: load contracts and index them."""
    return ContractRegistry(load_contracts(contracts_dir))


# --- Two-tier enforcement (Task 2) -------------------------------------------
#
# Workers describe what they want to do as ``proposed_actions`` — dicts with a
# ``type`` key (e.g. {"type": "GENERATE_CANDIDATE_PLAN"}). To check those
# proposals against a capability contract we translate each action type into:
#   * the tool that legitimately produces it (checked against ``tools``), and
#   * the capabilities it would exercise (checked against ``forbidden``).
#
# The forbidden check is the security-critical one: it catches an action that
# would activate a plan, message a driver, or touch master memory even if some
# contract happened to list the producing tool. An action type not present in
# this map is treated as UNKNOWN — it has no known legitimate tool, so it is
# dropped (tier 2) rather than trusted.

# type -> the single tool that legitimately emits this action.
_ACTION_TOOL: dict[str, str] = {
    "GENERATE_CANDIDATE_PLAN": "ortools",
    "BOUNDED_REPLAN": "bounded_replan_request",
    "DRAFT_DRIVER_REPLY": "draft_reply",
    # A propose-only request to send ONE driver their route. Emitted by the
    # driver_comms worker on a DRIVER_DISPATCH turn; sanctioned only by the
    # driver_enquiry contract, so no other worker can propose it. The send
    # itself stays with the API layer behind the dispatcher's confirmation —
    # the `send: True` flag check below is what keeps that boundary.
    "SEND_DRIVER_ROUTE": "request_route_dispatch",
}

# type -> capabilities the action exercises. Empty for the benign propose-only
# actions today; populated for anything that would cross an authority boundary
# so a forged/hallucinated action is caught by the forbidden denylist.
_ACTION_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "GENERATE_CANDIDATE_PLAN": (),
    "BOUNDED_REPLAN": (),
    "DRAFT_DRIVER_REPLY": (),
    # Propose-only: asking the dispatcher to confirm a send crosses no boundary.
    # Flipping it to {"send": true} does, and is caught by _SEND_FLAG_CAPABILITY
    # below regardless of this entry — so the no-self-dispatch rule still holds.
    "SEND_DRIVER_ROUTE": (),
    # Boundary-crossing action types a worker must NEVER successfully propose.
    # These are not emitted by the honest workers; they exist so that a forged
    # or prompt-injected action naming one of them maps onto a forbidden
    # capability and fails the turn.
    "ACTIVATE_PLAN": ("activate_plan",),
    "APPROVE_ACTION": ("approve_action",),
    "SEND_DRIVER_MESSAGE": ("send_driver_message",),
    "EDIT_PLAN": ("edit_plan",),
    "CROSS_DRIVER_ASSIGNMENT": ("cross_driver_assignment",),
    "FORGE_PROOF": ("forge_proof",),
    "BYPASS_POLICY": ("bypass_policy",),
    "READ_MASTER_MEMORY": ("master_memory",),
}

# An explicit flag on the action itself that would flip a draft into a real
# send is treated as exercising the send capability, regardless of type, and is
# a hard structural violation for any worker (see enforce_contract). This
# defends against {"type": "DRAFT_DRIVER_REPLY", "send": true} even for a
# contract whose forbidden list omits the send verb.
_SEND_FLAG_CAPABILITY = "send_driver_message"


class ContractViolation(BaseModel):
    """A single enforcement finding against one proposed action."""

    model_config = ConfigDict(frozen=True)

    node: str
    action_type: str
    reason: str
    # FATAL => a forbidden capability was requested; the turn must escalate.
    # DROPPED => the action was not permitted by the contract's tools (or is
    # unknown) but crossed no forbidden boundary; it is stripped and the turn
    # continues.
    severity: str  # "FATAL" | "DROPPED"


def _action_type(action: dict) -> str:
    return str(action.get("type", "")).upper()


def _capabilities_for(action: dict) -> tuple[str, ...]:
    caps = list(_ACTION_CAPABILITIES.get(_action_type(action), ()))
    # A draft that is actually flagged to send exercises the send capability.
    if action.get("send") is True:
        caps.append(_SEND_FLAG_CAPABILITY)
    return tuple(caps)


def enforce_contract(
    node: str,
    registry: ContractRegistry,
    proposed_actions: tuple[dict, ...] | list[dict],
) -> tuple[tuple[dict, ...], tuple[ContractViolation, ...], bool]:
    """Apply the two-tier contract check to a worker's proposed actions.

    Returns ``(kept_actions, violations, fatal)``:

    * **Tier 1 (fatal).** If any action would exercise a capability the node's
      contract lists as ``forbidden``, ``fatal`` is True. The whole turn must be
      escalated; no proposed action from this worker should be acted on.
    * **Tier 2 (drop).** An action whose producing tool is not in the
      contract's ``tools`` allowlist (including unknown action types) crosses no
      forbidden boundary but is not sanctioned, so it is dropped from
      ``kept_actions`` and recorded as a DROPPED violation. The turn continues.
    * Actions whose tool is allowed and which exercise no forbidden capability
      are kept.

    A node with no governing contract (e.g. the supervisor) keeps its actions
    unchanged — enforcement only applies to contract-bound worker nodes.
    """
    contracts = registry.for_node(node)
    if not contracts:
        return tuple(proposed_actions), (), False

    allowed = registry.allowed_tools(node)
    forbidden = registry.forbidden_capabilities(node)

    kept: list[dict] = []
    violations: list[ContractViolation] = []
    fatal = False

    for action in proposed_actions:
        action_type = _action_type(action)
        capabilities = _capabilities_for(action)
        breached = sorted(set(capabilities) & forbidden)
        # A worker flagging an action to actually send is a hard structural
        # violation regardless of whether its own contract happens to list the
        # send capability: no worker may self-dispatch. This keeps the
        # propose-only invariant even for a contract that omits the send verb.
        if _SEND_FLAG_CAPABILITY in capabilities and _SEND_FLAG_CAPABILITY not in breached:
            breached = sorted({*breached, _SEND_FLAG_CAPABILITY})
        if breached:
            # Tier 1: hard stop. Record and do not keep the action.
            fatal = True
            violations.append(
                ContractViolation(
                    node=node,
                    action_type=action_type or "<missing>",
                    reason=f"forbidden capability requested: {', '.join(breached)}",
                    severity="FATAL",
                )
            )
            continue

        tool = _ACTION_TOOL.get(action_type)
        if tool is None or tool not in allowed:
            # Tier 2: not sanctioned by the contract's tools. Drop, keep going.
            detail = (
                f"unknown action type '{action_type or '<missing>'}'"
                if tool is None
                else f"tool '{tool}' not permitted by contract"
            )
            violations.append(
                ContractViolation(
                    node=node,
                    action_type=action_type or "<missing>",
                    reason=detail,
                    severity="DROPPED",
                )
            )
            continue

        kept.append(action)

    return tuple(kept), tuple(violations), fatal
