"""Durable ingestion state (which file version is in the index, under which pipeline config).

* Keyed per index+namespace (see ``Settings.manifest_path``).
* Each entry stores the pipeline fingerprint, so changing the embedding model or chunking
  re-embeds every document - even if a previous run was interrupted half-way.
* Writes are atomic (temp file + ``os.replace``) and happen after every document, so a crash
  never corrupts state or loses progress.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass(slots=True)
class DocumentState:
    sha256: str
    doc_key: str
    chunk_count: int
    fingerprint: str
    ingested_at: str


class Manifest:
    def __init__(self, path: Path, fingerprint: str) -> None:
        self.path = path
        self.fingerprint = fingerprint
        self.documents: dict[str, DocumentState] = {}
        self.legacy_migrated = False
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != SCHEMA_VERSION:
                logger.warning("Manifest schema changed; all documents will be re-ingested")
                return
            self.documents = {name: DocumentState(**state) for name, state in raw.get("documents", {}).items()}
            self.legacy_migrated = bool(raw.get("legacy_migrated", False))
        except (OSError, ValueError, TypeError) as exc:
            # Safe fallback: an unreadable manifest only costs a full (idempotent) re-ingest.
            logger.warning("Could not read manifest %s (%s); starting fresh", self.path, exc)

    def is_current(self, name: str, sha256: str) -> bool:
        state = self.documents.get(name)
        return state is not None and state.sha256 == sha256 and state.fingerprint == self.fingerprint

    def record(self, name: str, state: DocumentState) -> None:
        with self._lock:
            self.documents[name] = state
            self._save_locked()

    def remove(self, name: str) -> None:
        with self._lock:
            self.documents.pop(name, None)
            self._save_locked()

    def mark_legacy_migrated(self) -> None:
        with self._lock:
            self.legacy_migrated = True
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "legacy_migrated": self.legacy_migrated,
            "documents": {name: asdict(state) for name, state in sorted(self.documents.items())},
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

