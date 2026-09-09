from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from .domain import ApprovalRequest


def secure_digest(value: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


class EnrollmentService:
    def __init__(self, pepper: str) -> None:
        self.pepper = pepper
        self._pending: dict[str, dict[str, Any]] = {}
        self._bindings: dict[int, str] = {}
        self._suspended: set[str] = set()

    def issue(self, driver_id: str, ttl_minutes: int = 10) -> str:
        token = secrets.token_urlsafe(32)
        self._pending[secure_digest(token, self.pepper)] = {
            "driver_id": driver_id,
            "expires_at": datetime.now(UTC) + timedelta(minutes=ttl_minutes),
            "used": False,
        }
        return token

    def enroll(self, token: str, telegram_user_id: int, chat_type: str) -> str:
        if chat_type != "private":
            raise PermissionError("enrollment is limited to private Telegram chats")
        record = self._pending.get(secure_digest(token, self.pepper))
        if record is None or record["used"] or record["expires_at"] < datetime.now(UTC):
            raise PermissionError("invalid, expired, or replayed enrollment token")
        record["used"] = True
        self._bindings[telegram_user_id] = record["driver_id"]
        return record["driver_id"]

    def driver_for(self, telegram_user_id: int) -> str | None:
        driver_id = self._bindings.get(telegram_user_id)
        return None if driver_id in self._suspended else driver_id

    def suspend(self, driver_id: str) -> None:
        self._suspended.add(driver_id)


class ApprovalService:
    def __init__(self, action_key: str, issuer: str, audience: str) -> None:
        self.action_key = action_key
        self.issuer = issuer
        self.audience = audience
        self.requests: dict[str, ApprovalRequest] = {}
        self.used_nonces: set[str] = set()

    def _action_digest(self, plan_id: str, plan_version: int, action: dict[str, Any]) -> str:
        message = json.dumps(
            {"plan_id": plan_id, "plan_version": plan_version, "action": action},
            sort_keys=True,
            separators=(",", ":"),
        )
        return secure_digest(message, self.action_key)

    def create(self, plan_id: str, plan_version: int, action: dict[str, Any]) -> ApprovalRequest:
        request = ApprovalRequest(
            plan_id=plan_id,
            plan_version=plan_version,
            action_digest=self._action_digest(plan_id, plan_version, action),
        )
        self.requests[request.approval_id] = request
        return request

    def demo_proof(self, approval_id: str) -> dict[str, Any]:
        request = self.requests[approval_id]
        payload = {
            "protocol": "x401/0.1.0",
            "issuer": self.issuer,
            "audience": self.audience,
            "approval_id": approval_id,
            "action_digest": request.action_digest,
            "nonce": secrets.token_urlsafe(18),
        }
        payload["signature"] = secure_digest(
            json.dumps(payload, sort_keys=True, separators=(",", ":")), self.action_key
        )
        return payload

    def verify_and_execute(self, approval_id: str, proof: dict[str, Any]) -> ApprovalRequest:
        request = self.requests[approval_id]
        if request.status == "EXECUTED":
            raise PermissionError("approval already executed")
        signature = proof.get("signature", "")
        unsigned = {key: value for key, value in proof.items() if key != "signature"}
        expected = secure_digest(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")), self.action_key
        )
        nonce = str(proof.get("nonce", ""))
        valid = (
            proof.get("protocol") == "x401/0.1.0"
            and proof.get("issuer") == self.issuer
            and proof.get("audience") == self.audience
            and proof.get("approval_id") == approval_id
            and proof.get("action_digest") == request.action_digest
            and nonce not in self.used_nonces
            and hmac.compare_digest(signature, expected)
        )
        if not valid:
            raise PermissionError("invalid or replayed approval proof")
        self.used_nonces.add(nonce)
        executed = request.model_copy(update={"status": "EXECUTED"})
        self.requests[approval_id] = executed
        return executed

    def restore(self, requests: tuple[ApprovalRequest, ...], used_nonces: set[str]) -> None:
        self.requests = {request.approval_id: request for request in requests}
        self.used_nonces = set(used_nonces)
