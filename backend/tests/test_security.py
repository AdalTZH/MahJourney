import pytest

from mahjourney.audit import AuditChain
from mahjourney.security import ApprovalService, EnrollmentService


def test_audit_chain_detects_tampering() -> None:
    chain = AuditChain("test-key")
    chain.append("ONE", "tester", {"value": 1})
    chain.append("TWO", "tester", {"value": 2})
    assert chain.verify()
    chain._events[0] = chain._events[0].model_copy(update={"payload": {"value": 99}})
    assert not chain.verify()


def test_enrollment_is_private_single_use_and_scoped() -> None:
    service = EnrollmentService("pepper")
    token, digest, expires_at = service.issue("DRV-01")
    assert digest and expires_at
    with pytest.raises(PermissionError):
        service.enroll(token, 42, "group")
    assert service.enroll(token, 42, "private") == "DRV-01"
    assert service.driver_for(42) == "DRV-01"
    service.suspend("DRV-01")
    assert service.driver_for(42) is None
    with pytest.raises(PermissionError):
        service.enroll(token, 43, "private")


def test_enrollment_telegram_user_for_is_reverse_lookup() -> None:
    service = EnrollmentService("pepper")
    token, _digest, _expires_at = service.issue("DRV-01")
    service.enroll(token, 42, "private")
    assert service.telegram_user_for("DRV-01") == 42
    assert service.telegram_user_for("DRV-02") is None
    service.suspend("DRV-01")
    assert service.telegram_user_for("DRV-01") is None


def test_enrollment_restore_rehydrates_bindings_and_suspensions() -> None:
    service = EnrollmentService("pepper")
    service.restore(
        (
            {
                "driver_id": "DRV-01",
                "telegram_user_id": 42,
                "enrollment_digest": None,
                "enrollment_expires_at": None,
                "enrollment_used_at": "2026-01-01T00:00:00Z",
                "suspended_at": None,
            },
            {
                "driver_id": "DRV-02",
                "telegram_user_id": None,
                "enrollment_digest": None,
                "enrollment_expires_at": None,
                "enrollment_used_at": None,
                "suspended_at": "2026-01-01T00:00:00Z",
            },
        )
    )
    assert service.driver_for(42) == "DRV-01"
    assert "DRV-02" in service._suspended


def test_x401_proof_is_exact_and_not_replayable() -> None:
    service = ApprovalService("action-key", "issuer", "audience")
    approval = service.create("plan-1", 3, {"type": "ACTIVATE"})
    proof = service.demo_proof(approval.approval_id)
    assert service.verify_and_execute(approval.approval_id, proof).status == "EXECUTED"
    with pytest.raises(PermissionError):
        service.verify_and_execute(approval.approval_id, proof)


def test_x401_rejects_modified_action_binding() -> None:
    service = ApprovalService("action-key", "issuer", "audience")
    approval = service.create("plan-1", 3, {"type": "ACTIVATE"})
    proof = service.demo_proof(approval.approval_id)
    proof["action_digest"] = "tampered"
    with pytest.raises(PermissionError):
        service.verify_and_execute(approval.approval_id, proof)
