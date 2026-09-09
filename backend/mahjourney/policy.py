from .domain import PolicyDecision, PolicyInput, PolicyTier


def decide_policy(value: PolicyInput) -> PolicyDecision:
    if value.data_stale or not value.feasible or value.hard_violation:
        reasons = []
        if value.data_stale:
            reasons.append("required input data is stale")
        if not value.feasible:
            reasons.append("candidate is infeasible")
        if value.hard_violation:
            reasons.append("candidate has a hard constraint violation")
        return PolicyDecision(tier=PolicyTier.DENY, reasons=tuple(reasons))

    approval_reasons = []
    if not value.same_driver:
        approval_reasons.append("cross-driver delegation")
    if not value.same_vehicle:
        approval_reasons.append("vehicle reassignment")
    if value.overtime:
        approval_reasons.append("overtime")
    if value.protected_cargo:
        approval_reasons.append("protected cargo")
    if value.final_stop_removed:
        approval_reasons.append("final-stop removal")
    if value.hard_window_relaxed:
        approval_reasons.append("hard-window relaxation")
    if approval_reasons:
        return PolicyDecision(tier=PolicyTier.APPROVAL_REQUIRED, reasons=tuple(approval_reasons))

    if value.eta_degradation_minutes <= 5:
        return PolicyDecision(
            tier=PolicyTier.AUTO_EXECUTE, reasons=("bounded same-assignment change",)
        )
    if value.eta_degradation_minutes <= 15:
        return PolicyDecision(
            tier=PolicyTier.NOTIFY_THEN_EXECUTE,
            reasons=("soft ETA degradation between 5 and 15 minutes",),
            veto_seconds=60,
        )
    return PolicyDecision(
        tier=PolicyTier.APPROVAL_REQUIRED, reasons=("ETA degradation exceeds 15 minutes",)
    )
