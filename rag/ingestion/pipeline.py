"""Incremental, failure-isolated ingestion pipeline.

Per changed document:  parse -> embed (bounded windows) -> upsert -> prune stale chunk IDs.

Reliability properties vs. the original script:
* Upsert-then-prune: new vectors are written *before* old ones are removed, so retrieval never
  sees a document disappear mid-update, and a failed update leaves the previous version intact.
* Deterministic, ASCII-safe IDs ``<doc_key>#<chunk_index>`` enable prefix listing/deletion,
  which works on serverless indexes (delete-by-metadata-filter does not, on many plans).
* A failing document never aborts the run, is not marked as ingested, and is retried next run.
* Memory stays bounded for large files (embedding/upserting happens in windows).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rag.config import PIPELINE_VERSION, Settings
from rag.core.embeddings import Embedder
from rag.core.models import Chunk, VectorRecord
from rag.core.vector_store import TEXT_KEY, VectorStore
from rag.ingestion.manifest import DocumentState, Manifest
from rag.ingestion.parsers import parse_document, supported_extensions

logger = logging.getLogger(__name__)

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def make_doc_key(name: str) -> str:
    """Stable, ASCII-only document key; the hash suffix guarantees uniqueness after slugging."""
    slug = _UNSAFE_ID_CHARS.sub("_", name).strip("_")[:80] or "doc"
    return f"{slug}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"


def chunk_id(doc_key: str, index: int) -> str:
    return f"{doc_key}#{index:05d}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def discover_documents(source_dir: Path) -> dict[str, Path]:
    """Recursively find supported files, keyed by POSIX path relative to ``source_dir``."""
    if not source_dir.is_dir():
        raise FileNotFoundError(f"SOP source directory not found: {source_dir}")
    extensions = supported_extensions()
    found = {}
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if path.name.startswith((".", "~$")):  # hidden files, Office lock files
            continue
        found[path.relative_to(source_dir).as_posix()] = path
    return found


@dataclass(slots=True)
class IngestionPlan:
    files: dict[str, Path]
    hashes: dict[str, str]
    to_process: list[str]
    to_delete: list[str]
    unchanged: list[str]


def build_plan(settings: Settings, manifest: Manifest, *, full_rebuild: bool = False) -> IngestionPlan:
    files = discover_documents(settings.sop_source_dir)
    hashes = {name: sha256_file(path) for name, path in files.items()}
    to_process = [n for n in files if full_rebuild or not manifest.is_current(n, hashes[n])]
    return IngestionPlan(
        files=files,
        hashes=hashes,
        to_process=to_process,
        to_delete=sorted(set(manifest.documents) - set(files)),
        unchanged=[n for n in files if n not in set(to_process)],
    )


@dataclass(slots=True)
class IngestionReport:
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    chunks_upserted: int = 0
    stale_chunks_pruned: int = 0
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


class IngestionPipeline:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, manifest: Manifest) -> None:
        self.settings = settings
        self.embedder = embedder
        self.store = store
        self.manifest = manifest

    def run(self, plan: IngestionPlan) -> IngestionReport:
        started = time.perf_counter()
        report = IngestionReport(skipped=list(plan.unchanged))
        self._migrate_legacy_ids()

        for name in plan.to_delete:
            try:
                pruned = self.store.delete_prefix(f"{self.manifest.documents[name].doc_key}#")
                self.manifest.remove(name)  # only forget the file once its vectors are really gone
                report.deleted.append(name)
                logger.info("Purged deleted document %s (%d vectors)", name, pruned)
            except Exception as exc:
                logger.exception("Failed to purge deleted document %s", name)
                report.failed[name] = f"purge failed: {exc}"

        workers = min(self.settings.ingest_workers, max(len(plan.to_process), 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ingest") as pool:
            futures = {
                pool.submit(self._ingest_document, name, plan.files[name], plan.hashes[name]): name
                for name in plan.to_process
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    upserted, pruned = future.result()
                    report.processed.append(name)
                    report.chunks_upserted += upserted
                    report.stale_chunks_pruned += pruned
                except Exception as exc:
                    logger.exception("Ingestion failed for %s; previous version (if any) kept", name)
                    report.failed[name] = f"{type(exc).__name__}: {exc}"

        report.duration_s = round(time.perf_counter() - started, 2)
        return report

    def _ingest_document(self, name: str, path: Path, sha256: str) -> tuple[int, int]:
        started = time.perf_counter()
        doc_key = make_doc_key(name)
        chunks = parse_document(path, chunk_size=self.settings.chunk_size, chunk_overlap=self.settings.chunk_overlap)
        if not chunks:
            logger.warning("%s: no extractable text (scanned PDF? needs OCR). Indexing 0 chunks.", name)

        base_metadata = {
            "source_file": name,
            "doc_key": doc_key,
            "file_format": path.suffix.lstrip(".").upper(),
            "document_type": self.settings.document_type,
            "content_sha256": sha256,
            "embedding_model": self.embedder.model_name,
            "pipeline_version": PIPELINE_VERSION,
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        new_ids: list[str] = []
        window = self.settings.upsert_batch_size
        for start in range(0, len(chunks), window):
            batch = chunks[start : start + window]
            vectors = self.embedder.embed_documents([c.text for c in batch])
            records = [
                self._to_record(doc_key, start + offset, chunk, vector, base_metadata)
                for offset, (chunk, vector) in enumerate(zip(batch, vectors, strict=True))
            ]
            self.store.upsert(records)
            new_ids.extend(r.id for r in records)

        # Prune IDs from the previous version that no longer exist (document shrank / emptied).
        stale = sorted(set(self.store.list_ids(f"{doc_key}#")) - set(new_ids))
        if stale:
            self.store.delete_ids(stale)

        self.manifest.record(
            name,
            DocumentState(
                sha256=sha256,
                doc_key=doc_key,
                chunk_count=len(new_ids),
                fingerprint=self.manifest.fingerprint,
                ingested_at=base_metadata["ingested_at"],
            ),
        )
        logger.info(
            "Ingested %s: %d chunks upserted, %d stale pruned in %.2fs",
            name, len(new_ids), len(stale), time.perf_counter() - started,
        )
        return len(new_ids), len(stale)

    @staticmethod
    def _to_record(doc_key: str, index: int, chunk: Chunk, vector: list[float], base: dict) -> VectorRecord:
        metadata = {**base, **chunk.metadata, "chunk_index": index, TEXT_KEY: chunk.text}
        # Pinecone rejects null metadata values.
        return VectorRecord(
            id=chunk_id(doc_key, index),
            values=vector,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )

    def _migrate_legacy_ids(self) -> None:
        """One-time cleanup of vectors written by the original script (``<file>-chunk-<n>`` IDs
        in the default namespace), which would otherwise show up as duplicate search hits."""
        if self.manifest.legacy_migrated:
            return
        legacy_path = self.settings.legacy_cache_path
        try:
            if legacy_path.exists():
                legacy_files = json.loads(legacy_path.read_text(encoding="utf-8")).keys()
                legacy_store = self.store.with_namespace("")
                removed = sum(legacy_store.delete_prefix(f"{name}-chunk-") for name in legacy_files)
                logger.info("Legacy migration: removed %d vectors written by the old script", removed)
            self.manifest.mark_legacy_migrated()
        except Exception:
            logger.exception("Legacy ID migration failed; will retry on the next run")

