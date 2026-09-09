from mahjourney.domain import PolicyInput, PolicyTier
from mahjourney.policy import decide_policy


def base(**updates):
    values = {"same_driver": True, "same_vehicle": True}
    values.update(updates)
    return PolicyInput(**values)


def test_auto_execute_is_narrow() -> None:
    assert decide_policy(base(eta_degradation_minutes=5)).tier == PolicyTier.AUTO_EXECUTE


def test_notify_has_sixty_second_veto() -> None:
    decision = decide_policy(base(eta_degradation_minutes=10))
    assert decision.tier == PolicyTier.NOTIFY_THEN_EXECUTE
    assert decision.veto_seconds == 60


def test_protected_or_cross_driver_needs_approval() -> None:
    assert decide_policy(base(protected_cargo=True)).tier == PolicyTier.APPROVAL_REQUIRED
    assert decide_policy(base(same_driver=False)).tier == PolicyTier.APPROVAL_REQUIRED


def test_unknown_risk_fails_closed() -> None:
    assert decide_policy(base(data_stale=True)).tier == PolicyTier.DENY
    assert decide_policy(base(feasible=False)).tier == PolicyTier.DENY
    assert decide_policy(base(hard_violation=True)).tier == PolicyTier.DENY
