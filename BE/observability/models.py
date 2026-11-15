"""Domain models for AI Observability traces, spans, and compliance ledger entries.

Follows OpenTelemetry & Langfuse semantic conventions (`TRACE`, `GENERATION`, `TOOL`, `RETRIEVAL`)
while maintaining 100% column compatibility with `FDE_VIEWS.AgentAuditLog` in `setup_security_view.sql`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SpanKind(str, Enum):
    """Industry-standard AI observability observation types (Langfuse / OpenTelemetry GenAI)."""

    CHAIN = "CHAIN"
    GENERATION = "GENERATION"
    TOOL = "TOOL"
    RETRIEVAL = "RETRIEVAL"
    GUARDRAIL = "GUARDRAIL"


def infer_span_kind(node_executed: str, tool_name: str) -> SpanKind:
    node_lower = (node_executed or "").lower()
    tool_lower = (tool_name or "").lower()
    if "sop" in tool_lower or "retrieval" in tool_lower:
        return SpanKind.RETRIEVAL
    if node_lower == "tools":
        return SpanKind.TOOL
    if "reasoner" in node_lower or "synthesis" in tool_lower or "llm" in tool_lower:
        return SpanKind.GENERATION
    return SpanKind.CHAIN


@dataclass(slots=True, frozen=True)
class TraceSpanRecord:
    """Immutable span record compatible with both Langfuse waterfalls and `FDE_VIEWS.AgentAuditLog`."""

    LogID: int
    Timestamp: str
    SessionID: str
    NodeExecuted: str
    ToolName: str
    Content: str
    trace_id: str
    span_id: str
    elapsed_ms: int = 0
    sink_status: str = "memory+sql"
    span_kind: str = SpanKind.CHAIN.value
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        log_id: int,
        session_id: str,
        node_executed: str,
        tool_name: str,
        content: str,
        trace_id: str,
        span_id: str,
        elapsed_ms: int = 0,
        sink_status: str = "memory",
        metadata: dict[str, Any] | None = None,
    ) -> "TraceSpanRecord":
        kind = infer_span_kind(node_executed, tool_name)
        return cls(
            LogID=log_id,
            Timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            SessionID=str(session_id)[:50],
            NodeExecuted=str(node_executed)[:50],
            ToolName=str(tool_name)[:100],
            Content=content,
            trace_id=trace_id,
            span_id=span_id,
            elapsed_ms=int(elapsed_ms),
            sink_status=sink_status,
            span_kind=kind.value,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
