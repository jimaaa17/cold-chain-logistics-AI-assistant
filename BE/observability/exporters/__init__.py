"""Strategy Pattern interface for pluggable AI Observability span exporters."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SpanExporter(Protocol):
    """Contract implemented by every observability sink (Langfuse, SQL Audit Ledger, In-Memory)."""

    name: str

    def export_span(
        self,
        *,
        session_id: str,
        trace_id: str,
        span_id: str,
        node_executed: str,
        tool_name: str,
        content: str,
        elapsed_ms: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Persist or transmit a single span. Must isolate faults and return True on success."""
        ...
