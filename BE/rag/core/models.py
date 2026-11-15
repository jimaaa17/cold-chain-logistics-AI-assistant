"""Plain data types passed between services (no framework dependencies)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Chunk:
    """A unit of text produced by a parser, ready to be embedded."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class VectorRecord:
    id: str
    values: list[float]
    metadata: dict[str, Any]


@dataclass(slots=True, frozen=True)
class RetrievedChunk:
    id: str
    score: float
    text: str
    metadata: dict[str, Any]

    @property
    def source_file(self) -> str | None:
        return self.metadata.get("source_file")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": self.score,
            "text": self.text,
            "source_file": self.source_file,
            "metadata": self.metadata,
        }

