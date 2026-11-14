"""Typed, centralised configuration shared by every RAG service.

All values come from environment variables (or the project ``.env``). Field names map to
env vars case-insensitively, e.g. ``pinecone_namespace`` <- ``PINECONE_NAMESPACE``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Bump whenever parsing/chunking/metadata logic changes in a way that must force a re-embed.
PIPELINE_VERSION = "2"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_by_name=True,
        validate_by_alias=True,
    )

    # ---- Pinecone -------------------------------------------------------------------
    pinecone_api_key: SecretStr
    pinecone_index_name: str | None = None  # default derived from provider (see index_name)
    pinecone_namespace: str = "sop"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    # Safety latch: never drop an index with a mismatched dimension unless explicitly allowed.
    allow_index_recreate: bool = False

    # ---- Embeddings -----------------------------------------------------------------
    embeddings_provider: Literal["LOCAL", "OPENAI"] = Field(
        default="LOCAL",
        # ``Embeddings_model`` kept for backwards compatibility with the original script.
        validation_alias=AliasChoices("EMBEDDINGS_PROVIDER", "Embeddings_model"),
    )
    local_embedding_model: str = "BAAI/bge-m3"
    local_embedding_device: str = "auto"  # auto | cpu | cuda | mps
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int | None = None  # override only for models not in KNOWN_DIMENSIONS

    # ---- Ingestion (write path) -----------------------------------------------------
    sop_source_dir: Path = PROJECT_ROOT / "data" / "policy"
    rag_cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    chunk_size: int = Field(default=600, gt=50)
    chunk_overlap: int = Field(default=60, ge=0)
    embed_batch_size: int = Field(default=64, gt=0)
    upsert_batch_size: int = Field(default=100, gt=0, le=1000)
    ingest_workers: int = Field(default=4, gt=0, le=32)
    document_type: str = "Compliance Asset"

    # ---- Retrieval (read path) ------------------------------------------------------
    retrieval_top_k: int = Field(default=5, gt=0, le=50)
    retrieval_min_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    query_cache_size: int = Field(default=1024, ge=0)

    rag_log_level: str = "INFO"

    # ---- validators -----------------------------------------------------------------
    @field_validator("embeddings_provider", mode="before")
    @classmethod
    def _normalise_provider(cls, value: object) -> str:
        return str(value).strip().upper()

    @field_validator("sop_source_dir", "rag_cache_dir", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @model_validator(mode="after")
    def _check_chunking(self) -> "Settings":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self

    # ---- derived values -------------------------------------------------------------
    @property
    def embedding_model_name(self) -> str:
        if self.embeddings_provider == "OPENAI":
            return self.openai_embedding_model
        return self.local_embedding_model

    @property
    def index_name(self) -> str:
        return self.pinecone_index_name or f"fde-sop-index-{self.embeddings_provider.lower()}"

    @property
    def manifest_path(self) -> Path:
        # One manifest per index+namespace: switching providers can never reuse stale state.
        namespace = self.pinecone_namespace or "default"
        return self.rag_cache_dir / f"manifest__{self.index_name}__{namespace}.json"

    @property
    def legacy_cache_path(self) -> Path:
        return self.rag_cache_dir / "ingestion_hash_cache.json"

    def pipeline_fingerprint(self) -> str:
        """Hash of everything that changes the produced vectors. A change forces re-embedding."""
        payload = {
            "provider": self.embeddings_provider,
            "model": self.embedding_model_name,
            "dimensions": self.embedding_dimensions,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "document_type": self.document_type,
            "pipeline_version": PIPELINE_VERSION,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from env


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )
    # Third-party libraries are noisy at INFO.
    for noisy in ("httpx", "urllib3", "sentence_transformers", "pinecone"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

