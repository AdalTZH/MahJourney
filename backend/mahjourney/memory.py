from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .domain import AgentResult, MemoryItem, utc_now

if TYPE_CHECKING:
    pass


class MasterMemory:
    """Curated in-process memory store for the master agent.

    Beyond the original propose/curate/supersede/search lifecycle, this class
    now supports two explicit state-management capabilities:

    ``recall_for_turn``
        A richer mid-turn recall that returns ranked hits for a set of query
        terms collected at different points in the turn (e.g. the original
        message PLUS the task type determined by the supervisor PLUS any
        worker-specific context terms). Items are de-duplicated and ordered by
        relevance (hit count descending) so the most pertinent snippets surface
        first.

    ``write_back_from_result``
        After a worker produces an ``AgentResult``, any warnings it raised may
        be worth remembering as incident lessons.  This method proposes (not
        curates — a human must still approve) a ``PROPOSED / UNTRUSTED_EXTERNAL``
        ``INCIDENT_LESSON`` item for each unique warning so the next similar turn
        can recall them.  Callers control whether write-back is enabled via the
        ``enabled`` flag, keeping the behaviour opt-in and test-safe.
    """

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}
        # Guards every read AND write of ``self._items``. Needed because this
        # dict is mutated from more than one execution context: the async
        # event loop thread (the /memory/* endpoints, directly) and a
        # ThreadPoolExecutor worker thread (the LangGraph turn, run via
        # ``asyncio.to_thread`` — see api.py's streaming ``_drive`` and the
        # Telegram driver-message path). Two concurrent dispatcher turns can
        # also land on two different pool threads at once. A plain dict has no
        # atomicity guarantee across the multi-step operations here (e.g.
        # curate's read-then-write, or an iteration in ``search`` racing a
        # concurrent ``del`` from ``prune_stale``), so every public method
        # below acquires this lock for its entire body. An RLock (not a plain
        # Lock) because some methods call other locked methods on ``self``
        # from the same thread (e.g. ``write_back_from_result`` reads
        # ``self._items`` under the same acquisition it uses to build its
        # result — see that method for why it does NOT call ``propose``).
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------

    def propose(
        self, kind: str, content: str, trust_label: str = "UNTRUSTED_EXTERNAL"
    ) -> MemoryItem:
        item = MemoryItem(kind=kind, content=content, trust_label=trust_label)
        with self._lock:
            self._items[item.memory_id] = item
        return item

    def curate(self, memory_id: str) -> MemoryItem:
        with self._lock:
            item = self._items[memory_id]
            curated = item.model_copy(
                update={"status": "CURATED", "trust_label": "HUMAN_APPROVED"}
            )
            self._items[memory_id] = curated
            return curated

    def supersede(self, memory_id: str, replacement: str) -> MemoryItem:
        with self._lock:
            old = self._items[memory_id]
            self._items[memory_id] = old.model_copy(update={"status": "SUPERSEDED"})
            new = MemoryItem(
                kind=old.kind,
                content=replacement,
                status="CURATED",
                trust_label="HUMAN_APPROVED",
                supersedes_id=old.memory_id,
            )
            self._items[new.memory_id] = new
            return new

    def search(self, query: str, include_proposed: bool = False) -> tuple[MemoryItem, ...]:
        """Simple single-query keyword search used by the pre-turn recall path."""
        words = set(query.casefold().split())
        with self._lock:
            return tuple(
                item
                for item in self._items.values()
                if (include_proposed or item.status == "CURATED")
                and any(word in item.content.casefold() for word in words)
            )

    def proposed(self) -> tuple[MemoryItem, ...]:
        with self._lock:
            return tuple(item for item in self._items.values() if item.status == "PROPOSED")

    def by_kind(self, kind: str, status: str = "CURATED") -> tuple[MemoryItem, ...]:
        """Exact lookup of every item of a given kind/status — no keyword match.

        Some memory (notably ``POLICY``) should always be visible to the router
        regardless of whether the turn's message happens to contain a matching
        word: a policy like "never route through the CBD after 6pm" is relevant
        to a replanning turn even if the dispatcher's message says nothing that
        would trigger a keyword or semantic hit. :meth:`search` and
        :meth:`recall_for_turn` are fuzzy/ranked recall, appropriate for
        ``PREFERENCE``/``INCIDENT_LESSON`` snippets that only matter when
        relevant to the current message; ``by_kind`` is the structured,
        always-return-everything counterpart for memory that should be loaded
        unconditionally.

        Args:
            kind:   One of ``MemoryItem.kind`` ("POLICY", "PREFERENCE",
                    "INCIDENT_LESSON").
            status: One of ``MemoryItem.status``; defaults to "CURATED" so
                    un-vetted proposals are never surfaced through this path.

        Returns:
            Every matching item, in insertion (``created_at``) order.
        """
        with self._lock:
            return tuple(
                item
                for item in self._items.values()
                if item.kind == kind and item.status == status
            )

    def get(self, memory_id: str) -> MemoryItem:
        with self._lock:
            return self._items[memory_id]

    def restore(self, items: tuple[MemoryItem, ...]) -> None:
        with self._lock:
            self._items = {item.memory_id: item for item in items}

    def add_items(self, items: tuple[MemoryItem, ...]) -> None:
        """Insert pre-built ``MemoryItem`` instances directly.

        The single-writer counterpart to :meth:`write_back_from_result`: that
        method runs inside a LangGraph node (a worker-pool thread) and only
        *constructs* candidate items, deliberately without inserting them —
        see its docstring. The async layer (api.py's ``_finalize_dispatch``,
        already the sole caller draining ``GraphState.proposed_lessons``) is
        the one place that should ever call this, so every write to
        ``self._items`` funnels through a small, easy-to-audit set of methods
        (``propose``, ``curate``, ``supersede``, ``restore``, ``prune_stale``,
        and this one) rather than the graph thread reaching into the dict
        indirectly through ``propose``.

        Items that already exist (same ``memory_id``) are overwritten, same as
        ``propose`` would via dict assignment — callers are expected to pass
        freshly-constructed items with fresh ids, as ``write_back_from_result``
        does, so collisions are not expected in practice.
        """
        if not items:
            return
        with self._lock:
            for item in items:
                self._items[item.memory_id] = item

    def prune_stale(
        self,
        proposed_incident_lesson_max_age: timedelta,
        superseded_max_age: timedelta,
        now: datetime | None = None,
    ) -> tuple[MemoryItem, ...]:
        """Remove stale, inert items from the in-process store.

        Bounds the growth MasterMemory would otherwise never limit (it only
        ever gained items before this method existed — see ``propose`` /
        ``curate`` / ``supersede``, none of which delete). Two categories are
        pruned, both chosen because deleting them loses nothing anything
        currently reads:

        * ``PROPOSED`` ``INCIDENT_LESSON`` items older than
          ``proposed_incident_lesson_max_age``. These are auto-generated
          worker-warning write-backs (see ``write_back_from_result``) that no
          human has curated. If nobody has acted on one in that window, it is
          stale noise, not a pending decision — human-submitted ``POLICY``/
          ``PREFERENCE`` proposals are NOT touched by this method regardless of
          age, since those genuinely await a curation decision.
        * ``SUPERSEDED`` items (either kind) older than ``superseded_max_age``.
          Every recall path (``search``, ``recall_for_turn``,
          ``recall_incident_lessons``, ``by_kind``) already excludes
          ``SUPERSEDED`` items, so they are dead to the agent the moment
          ``supersede()`` creates them. They are kept for a while in case their
          ``supersedes_id`` provenance is ever inspected, then pruned.

        Does not touch: ``CURATED`` items (regardless of age — a standing
        policy does not go stale just because time passed), or ``PROPOSED``
        ``POLICY``/``PREFERENCE`` items (a human decision is still pending on
        those).

        Args:
            proposed_incident_lesson_max_age: Age cutoff for uncurated
                INCIDENT_LESSON proposals.
            superseded_max_age: Age cutoff for SUPERSEDED items of any kind.
            now: Injection point for deterministic testing; defaults to the
                current UTC time.

        Returns:
            The tuple of items that were removed, so callers can log what was
            pruned.
        """
        current_time = now if now is not None else utc_now()
        removed: list[MemoryItem] = []
        with self._lock:
            for memory_id, item in list(self._items.items()):
                age = current_time - item.created_at
                is_stale_proposed_lesson = (
                    item.kind == "INCIDENT_LESSON"
                    and item.status == "PROPOSED"
                    and age > proposed_incident_lesson_max_age
                )
                is_stale_superseded = item.status == "SUPERSEDED" and age > superseded_max_age
                if is_stale_proposed_lesson or is_stale_superseded:
                    del self._items[memory_id]
                    removed.append(item)
        return tuple(removed)

    # ------------------------------------------------------------------
    # Mid-turn contextual recall
    # ------------------------------------------------------------------

    def recall_for_turn(
        self,
        queries: tuple[str, ...],
        include_proposed: bool = False,
        max_items: int = 8,
    ) -> tuple[MemoryItem, ...]:
        """Return the most relevant memory items for a multi-term turn context.

        Unlike :meth:`search`, which evaluates a single query string, this
        method accepts several distinct query terms collected at different
        points in the turn (raw message, task type, worker focus area, etc.)
        and scores each candidate item by how many of those terms it matches.
        Items that hit more terms rank higher. De-duplication is exact (by
        ``memory_id``). Only CURATED items are considered by default.

        This is intended for mid-turn recall inside the supervisor node, where
        the task type and any worker-specific context are already known and can
        be combined with the original message to produce a richer recall set
        than the single upfront ``search()`` call provides.

        Args:
            queries:         Ordered sequence of query strings; each is
                             word-tokenised independently.
            include_proposed: If True, PROPOSED items are also considered.
            max_items:       Cap on returned items (highest-scoring first).

        Returns:
            Tuple of at most ``max_items`` MemoryItems, ordered by hit count
            descending (ties preserve insertion order).
        """
        if not queries:
            return ()

        # Build one set of normalised words per query.
        word_sets: list[set[str]] = [
            set(q.casefold().split()) for q in queries if q.strip()
        ]
        if not word_sets:
            return ()

        scored: dict[str, tuple[int, MemoryItem]] = {}
        with self._lock:
            for item in self._items.values():
                if not (include_proposed or item.status == "CURATED"):
                    continue
                lower_content = item.content.casefold()
                hits = sum(
                    1
                    for words in word_sets
                    if any(word in lower_content for word in words)
                )
                if hits > 0:
                    scored[item.memory_id] = (hits, item)

        ranked = sorted(scored.values(), key=lambda t: t[0], reverse=True)
        return tuple(item for _, item in ranked[:max_items])

    def recall_incident_lessons(
        self,
        queries: tuple[str, ...],
        max_items: int = 5,
    ) -> tuple[MemoryItem, ...]:
        """Recall auto-generated INCIDENT_LESSON proposals for a turn.

        This is the deliberately-scoped counterpart to :meth:`recall_for_turn`.
        It surfaces the write-back lessons produced by
        :meth:`write_back_from_result` so they can feed back into the router
        WITHOUT human curation — but it is narrowed on two axes to keep that
        safe:

        * **kind** — only ``INCIDENT_LESSON`` items are considered, so enabling
          proposal recall here never exposes arbitrary human-submitted
          ``POLICY``/``PREFERENCE`` proposals to the model.
        * **status** — only ``PROPOSED`` items (the auto-lessons), since CURATED
          lessons already flow through :meth:`search` / :meth:`recall_for_turn`.

        These items stay ``UNTRUSTED_EXTERNAL``; the caller is responsible for
        presenting them to the model as an explicitly-unvetted bucket, distinct
        from curated hints.

        Ranking favours RECENCY, not hit-count: among items matching at least
        one query term, the most recent ``max_items`` (by ``created_at``) are
        returned, newest first. A lesson that never matches any query term is
        excluded, so an empty/irrelevant turn recalls nothing.

        Args:
            queries:   Query strings (raw message, task type, etc.); each is
                       word-tokenised independently.
            max_items: Cap on returned items (most recent first).

        Returns:
            Tuple of at most ``max_items`` PROPOSED INCIDENT_LESSON items.
        """
        if not queries or max_items <= 0:
            return ()

        word_sets: list[set[str]] = [
            set(q.casefold().split()) for q in queries if q.strip()
        ]
        if not word_sets:
            return ()

        matches: list[MemoryItem] = []
        with self._lock:
            for item in self._items.values():
                if item.kind != "INCIDENT_LESSON" or item.status != "PROPOSED":
                    continue
                lower_content = item.content.casefold()
                if any(
                    any(word in lower_content for word in words) for words in word_sets
                ):
                    matches.append(item)

        # Most recent first; ties fall back to insertion order via created_at.
        matches.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(matches[:max_items])

    # ------------------------------------------------------------------
    # Post-worker memory write-back
    # ------------------------------------------------------------------

    def write_back_from_result(
        self,
        result: AgentResult,
        worker_node: str,
        enabled: bool = True,
    ) -> tuple[MemoryItem, ...]:
        """Build candidate INCIDENT_LESSON items from a worker result's warnings.

        DOES NOT insert anything into ``self._items``. This method runs inside
        a LangGraph worker node — executed on a thread-pool thread via
        ``asyncio.to_thread`` (see api.py's streaming ``_drive`` and the
        Telegram driver-message path), concurrently with other requests that
        may be reading or writing this same ``MasterMemory`` on the event-loop
        thread (the ``/memory/*`` endpoints) or another pool thread (a second
        in-flight dispatcher turn). To keep ``MasterMemory`` single-writer —
        every insert funnels through :meth:`add_items`, called only from the
        async layer after the graph has finished — this method only reads
        (under the lock, for the dedup check below) and constructs
        ``MemoryItem`` instances; it never calls :meth:`propose`. The caller
        (``_worker_update`` in agents.py) returns these candidates on
        ``GraphState.proposed_lessons``, and api.py's ``_finalize_dispatch``
        is the sole place that turns them into durable memory, via
        :meth:`add_items` (in-process) and ``PostgresRepository.save_memory``
        (persisted).

        Each unique warning text that has not already been captured as an
        active (non-SUPERSEDED) memory item becomes a candidate
        ``PROPOSED``/``UNTRUSTED_EXTERNAL`` ``INCIDENT_LESSON``. Proposed items
        still require human curation before they influence *curated* recall —
        this method never produces CURATED items.

        Args:
            result:      The AgentResult returned by a worker node.
            worker_node: Node name (e.g. ``"disruption"``) — prepended to the
                         lesson content for traceability.
            enabled:     When False the method is a no-op; callers can gate
                         write-back on a feature flag or test mode.

        Returns:
            Tuple of newly constructed, NOT-YET-STORED MemoryItems (may be
            empty). Callers must pass these to :meth:`add_items` (and persist
            them) for them to actually become part of memory.
        """
        if not enabled or not result.warnings:
            return ()

        with self._lock:
            # Index existing active content so we don't duplicate. Read under
            # the same lock acquisition used by the rest of this method so the
            # dedup check can't race a concurrent insert from another thread.
            existing_contents = {
                item.content.casefold()
                for item in self._items.values()
                if item.status != "SUPERSEDED"
            }

            candidates: list[MemoryItem] = []
            for warning in result.warnings:
                lesson = f"[{worker_node}] {warning}"
                if lesson.casefold() not in existing_contents:
                    candidates.append(
                        MemoryItem(
                            kind="INCIDENT_LESSON",
                            content=lesson,
                            trust_label="UNTRUSTED_EXTERNAL",
                        )
                    )
                    existing_contents.add(lesson.casefold())

        return tuple(candidates)
