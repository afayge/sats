from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sats.config import Settings
from sats.rag.knowledge import (
    KnowledgeSearchResult,
    KnowledgeStore,
    build_knowledge_context,
    infer_stock_collections,
)


@dataclass(frozen=True, slots=True)
class StockResearchContext:
    results: tuple[KnowledgeSearchResult, ...]
    system_message: str
    collections: tuple[str, ...]

    @property
    def sources(self) -> tuple[dict, ...]:
        return tuple(result.to_source() for result in self.results)


def build_stock_research_context(
    message: str,
    *,
    settings: Settings,
    knowledge: str | None = None,
    collections: Iterable[str] = (),
    store: KnowledgeStore | None = None,
    limit: int = 6,
) -> StockResearchContext | None:
    resolved_store = store or KnowledgeStore(getattr(settings, "db_path", None))
    resolved_store.ensure_default_knowledge(settings=settings)
    selected = infer_stock_collections(message, explicit=collections)
    if knowledge:
        results = resolved_store.search(message, knowledge=knowledge, limit=limit)
        selected = (knowledge,)
    elif selected:
        results = resolved_store.search(message, collections=selected, limit=limit)
    else:
        results = resolved_store.search(message, limit=limit)
    system_message = build_knowledge_context(results)
    if not system_message:
        return None
    return StockResearchContext(
        results=tuple(results),
        system_message=system_message,
        collections=tuple(selected),
    )
