from __future__ import annotations

from .domain import MemoryItem


class MasterMemory:
    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    def propose(
        self, kind: str, content: str, trust_label: str = "UNTRUSTED_EXTERNAL"
    ) -> MemoryItem:
        item = MemoryItem(kind=kind, content=content, trust_label=trust_label)
        self._items[item.memory_id] = item
        return item

    def curate(self, memory_id: str) -> MemoryItem:
        item = self._items[memory_id]
        curated = item.model_copy(update={"status": "CURATED", "trust_label": "HUMAN_APPROVED"})
        self._items[memory_id] = curated
        return curated

    def supersede(self, memory_id: str, replacement: str) -> MemoryItem:
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
        words = set(query.casefold().split())
        candidates = self._items.values()
        return tuple(
            item
            for item in candidates
            if (include_proposed or item.status == "CURATED")
            and any(word in item.content.casefold() for word in words)
        )

    def proposed(self) -> tuple[MemoryItem, ...]:
        return tuple(item for item in self._items.values() if item.status == "PROPOSED")

    def get(self, memory_id: str) -> MemoryItem:
        return self._items[memory_id]

    def restore(self, items: tuple[MemoryItem, ...]) -> None:
        self._items = {item.memory_id: item for item in items}
