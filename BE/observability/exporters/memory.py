"""Thread-safe in-memory ring-buffer exporter for sub-millisecond UI trace waterfall queries."""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

from observability.models import TraceSpanRecord


class InMemorySpanExporter:
    """Stores recent spans in a bounded thread-safe ring buffer (`deque(maxlen=...)`)."""

    name = "memory"

    def __init__(self, max_spans: int = 1000) -> None:
        self._spans: deque[TraceSpanRecord] = deque(maxlen=max_spans)
        self._counter = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._spans)

    def append_record(
        self,
        *,
        session_id: str,
        node_executed: str,
        tool_name: str,
        content: str,
        trace_id: str,
        span_id: str,
        elapsed_ms: int = 0,
        sink_status: str = "memory",
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpanRecord:
        with self._lock:
            self._counter += 1
            record = TraceSpanRecord.create(
                log_id=self._counter,
                session_id=session_id,
                node_executed=node_executed,
                tool_name=tool_name,
                content=content,
                trace_id=trace_id,
                span_id=span_id,
                elapsed_ms=elapsed_ms,
                sink_status=sink_status,
                metadata=metadata,
            )
            self._spans.append(record)
            return record

    def query(
        self,
        *,
        session_id_filter: str | None = None,
        node_filter: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(reversed(self._spans))
        if session_id_filter:
            items = [s for s in items if s.SessionID == session_id_filter]
        if node_filter and node_filter != "ALL":
            items = [s for s in items if s.NodeExecuted == node_filter]
        return [s.to_dict() for s in items[:limit]]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._counter = 0
