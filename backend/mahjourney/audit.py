from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from .domain import AuditEvent


class AuditChain:
    def __init__(self, key: str) -> None:
        self._key = key.encode()
        self._events: list[AuditEvent] = []

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def _digest(self, body: dict[str, Any]) -> str:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(self._key, canonical, hashlib.sha256).hexdigest()

    def append(self, event_type: str, actor: str, payload: dict[str, Any]) -> AuditEvent:
        timestamp = datetime.now(UTC)
        previous_hash = self._events[-1].event_hash if self._events else "GENESIS"
        unsigned = {
            "sequence": len(self._events) + 1,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "timestamp": timestamp.isoformat(),
            "previous_hash": previous_hash,
        }
        event = AuditEvent(**unsigned, event_hash=self._digest(unsigned))
        self._events.append(event)
        return event

    def verify(self) -> bool:
        previous_hash = "GENESIS"
        for event in self._events:
            unsigned = {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "actor": event.actor,
                "payload": event.payload,
                "timestamp": event.timestamp.isoformat(),
                "previous_hash": event.previous_hash,
            }
            if event.previous_hash != previous_hash or not hmac.compare_digest(
                event.event_hash, self._digest(unsigned)
            ):
                return False
            previous_hash = event.event_hash
        return True

    def restore(self, events: tuple[AuditEvent, ...]) -> None:
        self._events = list(events)
        if not self.verify():
            self._events = []
            raise ValueError("persisted audit chain verification failed")
