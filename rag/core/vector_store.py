"""Pinecone gateway: one index + namespace, with batching and retries.

Provisioning (creating indexes) is only allowed from the ingestion path. The retrieval path
connects read-only and fails fast if the index is missing or has the wrong dimension.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from rag.core.models import RetrievedChunk, VectorRecord
from rag.core.resilience import with_retry

if TYPE_CHECKING:
    from rag.config import Settings

logger = logging.getLogger(__name__)

PINECONE_MAX_DELETE_BATCH = 1000
TEXT_KEY = "text"  # same key LangChain's PineconeVectorStore uses, so either client can read the index


class IndexDimensionMismatch(RuntimeError):
    pass


class IndexNotFound(RuntimeError):
    pass


class VectorStore:
    def __init__(self, index: Any, namespace: str = "", *, upsert_batch_size: int = 100) -> None:
        self._index = index
        self.namespace = namespace
        self.upsert_batch_size = upsert_batch_size

    def with_namespace(self, namespace: str) -> "VectorStore":
        return VectorStore(self._index, namespace, upsert_batch_size=self.upsert_batch_size)

    # ---- writes -------------------------------------------------------------------
    def upsert(self, records: Sequence[VectorRecord]) -> int:
        for start in range(0, len(records), self.upsert_batch_size):
            self._upsert_batch(records[start : start + self.upsert_batch_size])
        return len(records)

    @with_retry()
    def _upsert_batch(self, batch: Sequence[VectorRecord]) -> None:
        self._index.upsert(
            vectors=[{"id": r.id, "values": r.values, "metadata": r.metadata} for r in batch],
            namespace=self.namespace,
        )

    def delete_ids(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        for start in range(0, len(ids), PINECONE_MAX_DELETE_BATCH):
            self._delete_batch(ids[start : start + PINECONE_MAX_DELETE_BATCH])
        return len(ids)

    @with_retry()
    def _delete_batch(self, ids: list[str]) -> None:
        self._index.delete(ids=ids, namespace=self.namespace)

    @with_retry()
    def list_ids(self, prefix: str) -> list[str]:
        """List IDs by prefix (supported on serverless, unlike delete-by-metadata-filter)."""
        return [vid for page in self._index.list(prefix=prefix, namespace=self.namespace) for vid in page]

    def delete_prefix(self, prefix: str) -> int:
        return self.delete_ids(self.list_ids(prefix))

    # ---- reads --------------------------------------------------------------------
    @with_retry(attempts=3, max_wait=2.0)
    def query(
        self, vector: list[float], top_k: int, metadata_filter: dict[str, Any] | None = None
    ) -> list[RetrievedChunk]:
        response = self._index.query(
            vector=vector,
            top_k=top_k,
            namespace=self.namespace,
            filter=metadata_filter or None,
            include_metadata=True,
            include_values=False,
        )
        results = []
        for match in getattr(response, "matches", None) or []:
            metadata = dict(getattr(match, "metadata", None) or {})
            text = metadata.pop(TEXT_KEY, "")
            results.append(RetrievedChunk(id=match.id, score=float(match.score), text=text, metadata=metadata))
        return results

    @with_retry(attempts=3, max_wait=2.0)
    def vector_count(self) -> int | None:
        stats = self._index.describe_index_stats()
        namespaces = getattr(stats, "namespaces", None) or {}
        summary = namespaces.get(self.namespace)
        return getattr(summary, "vector_count", 0) if summary is not None else 0


def connect_vector_store(settings: "Settings", dimension: int, *, provision: bool = False) -> VectorStore:
    """Connect to the configured index. ``provision=True`` (ingestion only) may create it."""
    from pinecone import Pinecone, ServerlessSpec

    pc = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
    name = settings.index_name
    description = pc.describe_index(name) if name in pc.list_indexes().names() else None

    if description is not None and description.dimension != dimension:
        if not (provision and settings.allow_index_recreate):
            raise IndexDimensionMismatch(
                f"Index '{name}' has dimension {description.dimension} but model "
                f"'{settings.embedding_model_name}' produces {dimension}. Set ALLOW_INDEX_RECREATE=true "
                "(or use --allow-index-recreate) to drop and rebuild it, or configure a different index."
            )
        logger.warning("Dropping index '%s' (dimension %s != %s)", name, description.dimension, dimension)
        pc.delete_index(name)
        description = None

    if description is None:
        if not provision:
            raise IndexNotFound(f"Index '{name}' does not exist. Run the ingestion service first.")
        logger.info("Creating serverless index '%s' (%d dim, cosine)", name, dimension)
        pc.create_index(
            name=name,
            dimension=dimension,
            metric="cosine",
            spec=ServerlessSpec(cloud=settings.pinecone_cloud, region=settings.pinecone_region),
        )  # blocks until the index is ready
        description = pc.describe_index(name)

    index = pc.Index(host=description.host)  # host-pinned: skips a control-plane lookup per client
    logger.info("Connected to Pinecone index '%s' namespace '%s'", name, settings.pinecone_namespace)
    return VectorStore(index, settings.pinecone_namespace, upsert_batch_size=settings.upsert_batch_size)

