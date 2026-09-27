"""In-app notification hub for pushing live events to connected dispatcher UIs.

A tiny in-memory pub/sub. WebSocket handlers (``/ws/events``) subscribe to get
an ``asyncio.Queue`` they read from; anywhere in the app can call ``publish()``
to fan a JSON-serialisable event out to every connected client.

Deliberately in-memory and best-effort: notifications are transient UI signals
(a breakdown just happened — show a toast), not durable records. The durable
record is the audit chain. If no dispatcher UI is connected, the event is simply
dropped, which is the correct behaviour for a "pop up on screen" alert.
"""

from __future__ import annotations

import asyncio
from typing import Any


class NotificationHub:
    """Fan-out of live events to all connected WebSocket subscribers."""

    def __init__(self, max_queue: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its queue.

        The caller (a WebSocket handler) reads events off this queue and must
        call :meth:`unsubscribe` when the connection closes.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        """Fan an event out to every connected subscriber.

        Never raises and never blocks: if a subscriber's queue is full (a slow
        or stuck client), that event is skipped for that client rather than
        stalling the publisher or every other client.
        """
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
