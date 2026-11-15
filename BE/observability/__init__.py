"""Enterprise AI Observability & Compliance Subsystem (`BE/observability/`).

Provides a scalable, modular, fault-isolated telemetry architecture based on OpenTelemetry
and Langfuse conventions, integrating:
- **Langfuse Distributed Tracing** (`LangfuseSpanExporter`): Hierarchical traces, LLM generations,
  tool spans, and LangChain `CallbackHandler` integration.
- **MSSQL Compliance Audit Ledger** (`SqlAuditLedgerExporter`): Append-only writes to
  `FDE_VIEWS.AgentAuditLog` under the `USR_FDE_RO` role (`BE/sql/setup_security_view.sql`).
- **In-Memory Ring Buffer** (`InMemorySpanExporter`): Sub-millisecond queryable span buffer
  powering the Control Tower UI waterfall (`FE/src/features/audit/`).
"""
from __future__ import annotations

from observability.config import ObservabilitySettings
from observability.context import TraceContext, new_span_id, new_trace_id
from observability.exporters import SpanExporter
from observability.exporters.langfuse import LangfuseSpanExporter
from observability.exporters.memory import InMemorySpanExporter
from observability.exporters.sql_audit import SqlAuditLedgerExporter
from observability.models import SpanKind, TraceSpanRecord, infer_span_kind
from observability.pipeline import (
    MAX_RING_BUFFER_SPANS,
    LangfuseBridge,
    ObservabilityPipeline,
    get_langfuse_bridge,
    get_observability_pipeline,
)

__all__ = [
    "InMemorySpanExporter",
    "LangfuseBridge",
    "LangfuseSpanExporter",
    "MAX_RING_BUFFER_SPANS",
    "ObservabilityPipeline",
    "ObservabilitySettings",
    "SpanExporter",
    "SpanKind",
    "SqlAuditLedgerExporter",
    "TraceContext",
    "TraceSpanRecord",
    "get_langfuse_bridge",
    "get_observability_pipeline",
    "infer_span_kind",
    "new_span_id",
    "new_trace_id",
]
