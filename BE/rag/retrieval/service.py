"""Retrieval domain logic, independent of the transport (HTTP, in-process, tests)."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from rag.core.models import RetrievedChunk

if TYPE_CHECKING:
    from rag.config import Settings
    from rag.core.embeddings import Embedder
    from rag.core.vector_store import VectorStore

logger = logging.getLogger(__name__)

MAX_TOP_K = 50


class RetrievalService:
    def __init__(
        self,
        embedder: "Embedder",
        store: "VectorStore",
        *,
        default_top_k: int = 5,
        min_score: float = 0.0,
        cache_size: int = 1024,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self.default_top_k = default_top_k
        self.min_score = min_score
        # Operators repeat the same questions ("what's the temp threshold?"); caching the query
        # embedding removes the model/API round-trip, typically the largest latency component.
        self._embed_cached = lru_cache(maxsize=cache_size)(self._embed)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "RetrievalService":
        from rag.core.embeddings import build_embedder
        from rag.core.vector_store import connect_vector_store

        embedder = build_embedder(settings)
        store = connect_vector_store(settings, embedder.dimension, provision=False)  # read-only
        return cls(
            embedder,
            store,
            default_top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score,
            cache_size=settings.query_cache_size,
        )

    def _embed(self, normalised_query: str) -> tuple[float, ...]:
        return tuple(self._embedder.embed_query(normalised_query))

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
        min_score: float | None = None,
    ) -> list[RetrievedChunk]:
        normalised = " ".join(query.split())
        if not normalised:
            raise ValueError("query must not be empty")
        k = max(1, min(top_k or self.default_top_k, MAX_TOP_K))
        threshold = self.min_score if min_score is None else min_score
        vector = list(self._embed_cached(normalised))
        return [m for m in self._store.query(vector, k, metadata_filter) if m.score >= threshold]

    def readiness(self) -> dict[str, Any]:
        """Raises if Pinecone is unreachable; used by the /readyz probe."""
        return {
            "namespace": self._store.namespace,
            "vector_count": self._store.vector_count(),
            "embedding_model": self._embedder.model_name,
        }

    def cache_stats(self) -> dict[str, int]:
        info = self._embed_cached.cache_info()
        return {"hits": info.hits, "misses": info.misses, "size": info.currsize}

