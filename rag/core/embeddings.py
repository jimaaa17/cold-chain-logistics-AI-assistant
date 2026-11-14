"""Embedding provider abstraction.

``Embedder`` wraps any LangChain ``Embeddings`` backend and adds what the raw backends lack:
batching, transient-error retries, dimension validation and (for in-process torch models)
serialised access so concurrent ingestion threads don't oversubscribe the CPU/GPU.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import nullcontext
from typing import TYPE_CHECKING, Protocol, Sequence

from rag.core.resilience import with_retry

if TYPE_CHECKING:
    from rag.config import Settings

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

logger = logging.getLogger(__name__)

KNOWN_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "BAAI/bge-m3": 1024,
}


class EmbeddingsBackend(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class Embedder:
    def __init__(
        self,
        backend: EmbeddingsBackend,
        model_name: str,
        *,
        dimension: int | None = None,
        batch_size: int = 64,
        serialize: bool = False,
    ) -> None:
        self._backend = backend
        self.model_name = model_name
        self.batch_size = batch_size
        self._lock = threading.Lock() if serialize else nullcontext()
        self.dimension = dimension or KNOWN_DIMENSIONS.get(model_name) or len(self.embed_query("dimension probe"))

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(list(texts[start : start + self.batch_size])))
        if vectors and len(vectors[0]) != self.dimension:
            raise ValueError(
                f"Model {self.model_name} returned {len(vectors[0])}-dim vectors, expected {self.dimension}"
            )
        return vectors

    @with_retry()
    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        with self._lock:
            return self._backend.embed_documents(batch)

    @with_retry(attempts=3, max_wait=2.0)  # query path is latency-sensitive
    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            return self._backend.embed_query(text)


def _resolve_device(preference: str) -> str:
    if preference != "auto":
        return preference
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except ImportError:  # pragma: no cover - torch is a hard dependency of the local provider
        pass
    return "cpu"


def build_embedder(settings: "Settings") -> Embedder:
    """Create the configured embedder. Heavy imports happen lazily, only for the chosen provider."""
    if settings.embeddings_provider == "OPENAI":
        from langchain_openai import OpenAIEmbeddings

        kwargs: dict = {"model": settings.openai_embedding_model}
        if settings.embedding_dimensions and settings.openai_embedding_model.startswith("text-embedding-3"):
            kwargs["dimensions"] = settings.embedding_dimensions
        backend: EmbeddingsBackend = OpenAIEmbeddings(**kwargs)
        serialize = False  # remote API: concurrency is good
    else:
        from langchain_huggingface import HuggingFaceEmbeddings

        device = _resolve_device(settings.local_embedding_device)
        backend = HuggingFaceEmbeddings(
            model_name=settings.local_embedding_model,
            model_kwargs={"device": device},
            encode_kwargs={"normalize_embeddings": True, "batch_size": settings.embed_batch_size},
        )
        serialize = True  # one in-process model: let it use all cores per batch instead

    logger.info("Embeddings: provider=%s model=%s", settings.embeddings_provider, settings.embedding_model_name)
    return Embedder(
        backend,
        settings.embedding_model_name,
        dimension=settings.embedding_dimensions,
        batch_size=settings.embed_batch_size,
        serialize=serialize,
    )

