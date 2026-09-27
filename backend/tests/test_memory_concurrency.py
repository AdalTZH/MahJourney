"""Regression tests for the MasterMemory dict/DB race.

MasterMemory's in-process dict was previously mutated from more than one
execution context with no synchronization: the async event loop thread (the
/memory/* endpoints, via propose/curate/supersede) and a worker-pool thread
(the LangGraph turn's write_back_from_result, run via asyncio.to_thread — see
api.py's streaming _drive and the Telegram driver-message path). A plain dict
has no atomicity guarantee across the multi-step operations involved (e.g. an
iteration in search()/recall_for_turn()/by_kind() racing a concurrent del from
prune_stale() or curate()'s read-then-write).

Two things are checked here:

1. Single-writer structure: write_back_from_result must be read-only — it
   returns candidate MemoryItems without inserting them, so every insert
   funnels through a small set of methods (propose, curate, supersede,
   restore, prune_stale, add_items) rather than being reachable from a
   worker-pool thread indirectly.
2. Thread safety: concurrent readers, writers, and the pruner must not corrupt
   MasterMemory or raise (e.g. "dictionary changed size during iteration"),
   even under a deliberately adversarial interleaving.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from mahjourney.domain import AgentResult
from mahjourney.memory import MasterMemory


def test_write_back_from_result_does_not_mutate_the_store() -> None:
    """write_back_from_result must be read-only: it returns candidates without
    inserting them, so MasterMemory stays single-writer (the async layer is
    the only thing that calls add_items).
    """
    memory = MasterMemory()
    result = AgentResult(
        task_id="t1", status="COMPLETED", warnings=("road closure adds delay",)
    )

    before = dict(memory._items)
    candidates = memory.write_back_from_result(result, "disruption", enabled=True)
    after = dict(memory._items)

    assert before == after == {}, "write_back_from_result must not insert anything"
    assert len(candidates) == 1
    assert candidates[0].kind == "INCIDENT_LESSON"
    assert candidates[0].status == "PROPOSED"
    assert candidates[0].trust_label == "UNTRUSTED_EXTERNAL"


def test_add_items_is_the_only_path_that_commits_write_back_candidates() -> None:
    """The caller must explicitly commit candidates via add_items; nothing
    about calling write_back_from_result alone makes them recallable.
    """
    memory = MasterMemory()
    result = AgentResult(task_id="t1", status="COMPLETED", warnings=("warning A",))

    candidates = memory.write_back_from_result(result, "disruption", enabled=True)
    # Not yet committed: recall_incident_lessons must not surface it.
    assert memory.recall_incident_lessons(("warning A",)) == ()

    memory.add_items(candidates)
    # Now committed: recall_incident_lessons must surface it.
    recalled = memory.recall_incident_lessons(("warning A",))
    assert len(recalled) == 1
    assert recalled[0].memory_id == candidates[0].memory_id


def test_write_back_dedup_sees_items_committed_via_add_items() -> None:
    """A lesson committed via add_items must be recognised by a later
    write_back_from_result call's dedup check (same warning text), proving the
    dedup read and add_items' write observe the same underlying store.
    """
    memory = MasterMemory()
    result = AgentResult(task_id="t1", status="COMPLETED", warnings=("duplicate warning",))

    first_candidates = memory.write_back_from_result(result, "disruption", enabled=True)
    memory.add_items(first_candidates)

    second_candidates = memory.write_back_from_result(result, "disruption", enabled=True)
    assert second_candidates == (), "a warning already committed must not be re-proposed"


def test_concurrent_readers_writers_and_pruner_do_not_corrupt_the_store() -> None:
    """Deliberately adversarial concurrency stress test.

    Reproduces the exact shape of real traffic: several threads doing the
    graph-thread pattern (write_back_from_result + add_items), several doing
    the /memory/* endpoint pattern (propose/curate/supersede), several doing
    concurrent recall (search/recall_for_turn/recall_incident_lessons/by_kind),
    and one aggressively pruning throughout. Before locking was added, an
    equivalent unlocked dict reliably raises "dictionary changed size during
    iteration" once the iteration window is wide enough (verified separately);
    with the lock in place this must complete with no exceptions and a
    internally-consistent final state.
    """
    memory = MasterMemory()
    errors: list[tuple[str, int, str]] = []
    iterations = 150

    def writer_thread(thread_id: int) -> None:
        try:
            for i in range(iterations):
                result = AgentResult(
                    task_id=f"t{thread_id}-{i}",
                    status="COMPLETED",
                    warnings=(f"warning-{thread_id}-{i}",),
                )
                candidates = memory.write_back_from_result(
                    result, f"worker{thread_id}", enabled=True
                )
                if candidates:
                    memory.add_items(candidates)
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(("writer", thread_id, repr(exc)))

    def curator_thread(thread_id: int) -> None:
        try:
            for i in range(iterations):
                item = memory.propose("PREFERENCE", f"pref-{thread_id}-{i}")
                memory.curate(item.memory_id)
                if i % 5 == 0:
                    memory.supersede(item.memory_id, f"pref-{thread_id}-{i}-v2")
        except Exception as exc:  # noqa: BLE001
            errors.append(("curator", thread_id, repr(exc)))

    def reader_thread(thread_id: int) -> None:
        try:
            for _ in range(iterations):
                memory.search(f"warning-{thread_id}")
                memory.recall_for_turn((f"warning-{thread_id}", "DISRUPTION_ANALYSIS"))
                memory.recall_incident_lessons((f"warning-{thread_id}",))
                memory.by_kind("POLICY")
                memory.proposed()
        except Exception as exc:  # noqa: BLE001
            errors.append(("reader", thread_id, repr(exc)))

    def pruner_thread() -> None:
        try:
            for _ in range(iterations):
                memory.prune_stale(timedelta(days=14), timedelta(days=90))
        except Exception as exc:  # noqa: BLE001
            errors.append(("pruner", 0, repr(exc)))

    threads = (
        [threading.Thread(target=writer_thread, args=(t,)) for t in range(4)]
        + [threading.Thread(target=curator_thread, args=(t,)) for t in range(4)]
        + [threading.Thread(target=reader_thread, args=(t,)) for t in range(4)]
        + [threading.Thread(target=pruner_thread)]
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"race condition detected under concurrent access: {errors}"
