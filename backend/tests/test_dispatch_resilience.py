"""Regression tests for two bugs found reviewing the dispatcher memory recall path:

1. Route registration: ``@router.post("/dispatcher/messages")`` must be bound to
   the real endpoint function (``dispatcher_message``), not to the internal
   ``_prepare_dispatch`` helper it calls. The helper takes ``(state, body)``,
   and FastAPI treated the stray decorator's ``state`` as a required query
   parameter, so every call to the endpoint returned 422.

2. Persistence outage tolerance: a database/embedding-service outage during
   ``_prepare_dispatch`` must degrade the turn (empty history, keyword-search
   fallback) rather than raise and fail the whole request.

Both are checked without a live Postgres connection: the route-registration
test introspects the router's route table directly, and the outage test uses a
minimal fake ``state``/``persistence`` whose calls raise the exact exception
classes ``_PERSISTENCE_OUTAGE_ERRORS`` is meant to catch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from mahjourney.api import DispatcherMessage, _prepare_dispatch, dispatcher_message, router
from mahjourney.domain import MasterAgentState
from mahjourney.memory import MasterMemory


def test_dispatcher_messages_route_is_bound_to_dispatcher_message() -> None:
    """POST /dispatcher/messages must route to dispatcher_message, not the
    internal _prepare_dispatch helper it calls.

    Regression guard for the bug where the decorator sat on _prepare_dispatch:
    FastAPI would then require a "state" query parameter and every real
    request returned 422, independent of any database being reachable.
    """
    matches = [
        route
        for route in router.routes
        if getattr(route, "path", None) == "/api/v1/dispatcher/messages"
        and "POST" in getattr(route, "methods", set())
    ]
    assert matches, "no POST route registered for /dispatcher/messages"
    assert len(matches) == 1, f"expected exactly one matching route, found {len(matches)}"
    assert matches[0].endpoint is dispatcher_message, (
        f"/dispatcher/messages POST is bound to {matches[0].endpoint!r}, "
        "expected dispatcher_message"
    )


class _RaisingRepository:
    """Fake persistence layer where every call raises, simulating a DB outage."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def recent_conversation_messages(self, conversation_id: str, limit: int = 50):
        raise self._error

    async def save_conversation_message(self, message) -> None:
        raise self._error

    async def search_curated_memory(self, query: str, embedding, limit: int = 10):
        raise self._error


class _FakeOpenAI:
    """Embedding call also fails, as it would during the same network outage."""

    def embed(self, text: str) -> tuple[float, ...] | None:
        raise ConnectionError("simulated embedding-service outage")


class _FakeSimulation:
    def get(self, name: str):
        return SimpleNamespace(current_minute=0)

    def events(self, scenario_id: str) -> tuple:
        return ()


def _build_fake_state(error: BaseException) -> SimpleNamespace:
    """Minimal AppState stand-in exposing only what _prepare_dispatch reads."""
    return SimpleNamespace(
        persistence=_RaisingRepository(error),
        openai=_FakeOpenAI(),
        memory=MasterMemory(),
        simulation=_FakeSimulation(),
        latest_plan=None,
        fleet=(),
        orders=(),
        settings=SimpleNamespace(
            max_stops_per_vehicle=25,
            enforce_delivery_windows=True,
            conversation_retention_days=30,
        ),
        lta=None,
        nea=None,
        agent_state=MasterAgentState(),
    )


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("simulated connection outage"),
        SQLAlchemyError("simulated database outage"),
        TimeoutError("simulated timeout"),
    ],
)
@pytest.mark.asyncio
async def test_prepare_dispatch_degrades_gracefully_on_persistence_outage(error) -> None:
    """A DB/embedding outage during _prepare_dispatch must not raise.

    Regression guard: before graceful degradation was added, an outage on
    recent_conversation_messages, save_conversation_message, or
    search_curated_memory propagated straight out of _prepare_dispatch and
    failed the whole dispatcher turn instead of falling back.
    """
    state = _build_fake_state(error)
    body = DispatcherMessage(conversation_id="resilience-test", message="please replan the route")

    task, task_type, context, memory_snippets, conversation_history = await _prepare_dispatch(
        state, body
    )

    # No prior turns could be loaded during the outage.
    assert conversation_history == ()
    # Curated recall fell back to the (empty, since MasterMemory() is fresh)
    # in-memory keyword scan rather than raising.
    assert memory_snippets == ()
    # The turn still produced a usable task despite every persistence call
    # failing.
    assert task.conversation_id == "resilience-test"
    assert task_type
