"""Composite Observability Pipeline fanning out traces to Memory, Langfuse, and MSSQL (`setup_security_view.sql`)."""
from __future__ import annotations

import json
from typing import Any

from observability.config import ObservabilitySettings
from observability.context import new_span_id, new_trace_id
from observability.exporters.langfuse import LangfuseSpanExporter
from observability.exporters.memory import InMemorySpanExporter
from observability.exporters.sql_audit import SqlAuditLedgerExporter
from observability.models import TraceSpanRecord

MAX_RING_BUFFER_SPANS = 1000


class ObservabilityPipeline:
    """
    Fault-isolated multi-sink observability facade (Composite Pattern).

    Coordinates:
    1. `InMemorySpanExporter` (ring buffer for live Control Tower UI waterfalls)
    2. `LangfuseSpanExporter` (hierarchical LLM/Tool tracing & LangChain callbacks)
    3. `SqlAuditLedgerExporter` (tamper-evident `FDE_VIEWS.AgentAuditLog` insert-only sink)
    """

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
        max_spans: int = MAX_RING_BUFFER_SPANS,
        sql_engine: Any | None = None,
        sql_enabled: bool = True,
        sql_table: str = "FDE_VIEWS.AgentAuditLog",
    ) -> None:
        self.settings = ObservabilitySettings.from_env()
        self.memory_exporter = InMemorySpanExporter(max_spans=max_spans)
        self.langfuse_exporter = LangfuseSpanExporter(
            public_key=public_key or self.settings.langfuse_public_key,
            secret_key=secret_key or self.settings.langfuse_secret_key,
            host=host or self.settings.langfuse_host,
        )
        self.sql_exporter = SqlAuditLedgerExporter(
            engine=sql_engine,
            enabled=sql_enabled,
            table_name=sql_table,
        )

    @property
    def public_key(self) -> str | None:
        return self.langfuse_exporter.public_key

    @property
    def secret_key(self) -> str | None:
        return self.langfuse_exporter.secret_key

    @property
    def host(self) -> str:
        return self.langfuse_exporter.host

    @property
    def is_configured(self) -> bool:
        return self.langfuse_exporter.is_configured

    @property
    def is_active(self) -> bool:
        return self.langfuse_exporter.is_active

    def status(self) -> dict[str, Any]:
        return {
            "langfuse_configured": self.langfuse_exporter.is_configured,
            "langfuse_sdk_available": self.langfuse_exporter._sdk_available,
            "langfuse_active": self.langfuse_exporter.is_active,
            "langfuse_host": self.langfuse_exporter.host,
            "sql_audit_table": self.sql_exporter.table_name,
            "sql_rbac_role": self.settings.sql_rbac_role,
            "buffered_spans": len(self.memory_exporter),
        }

    def get_langchain_callback(self, session_id: str, *, trace_id: str | None = None) -> Any | None:
        return self.langfuse_exporter.get_langchain_callback(session_id, trace_id=trace_id)

    def record_span(
        self,
        session_id: str,
        node_executed: str,
        tool_name: str,
        content: Any,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        elapsed_ms: int = 0,
        sql_persisted: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpanRecord:
        effective_trace_id = trace_id or new_trace_id(session_id)
        effective_span_id = span_id or new_span_id()
        text_content = content if isinstance(content, str) else json.dumps(content, default=str)

        sinks = ["memory"]
        if sql_persisted or self.sql_exporter.export_span(
            session_id=session_id,
            trace_id=effective_trace_id,
            span_id=effective_span_id,
            node_executed=node_executed,
            tool_name=tool_name,
            content=text_content,
            elapsed_ms=elapsed_ms,
            metadata=metadata,
        ):
            sinks.append("sql")

        if self.langfuse_exporter.export_span(
            session_id=session_id,
            trace_id=effective_trace_id,
            span_id=effective_span_id,
            node_executed=node_executed,
            tool_name=tool_name,
            content=text_content,
            elapsed_ms=elapsed_ms,
            metadata=metadata,
        ):
            sinks.append("langfuse")

        return self.memory_exporter.append_record(
            session_id=session_id,
            node_executed=node_executed,
            tool_name=tool_name,
            content=text_content,
            trace_id=effective_trace_id,
            span_id=effective_span_id,
            elapsed_ms=elapsed_ms,
            sink_status="+".join(sinks),
            metadata=metadata,
        )

    def query_spans(
        self,
        *,
        session_id_filter: str | None = None,
        node_filter: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.memory_exporter.query(
            session_id_filter=session_id_filter,
            node_filter=node_filter,
            limit=limit,
        )

    def clear(self) -> None:
        self.memory_exporter.clear()


# Alias for backward compatibility with existing callers
LangfuseBridge = ObservabilityPipeline

_GLOBAL_PIPELINE: ObservabilityPipeline | None = None


def get_observability_pipeline() -> ObservabilityPipeline:
    global _GLOBAL_PIPELINE
    if _GLOBAL_PIPELINE is None:
        _GLOBAL_PIPELINE = ObservabilityPipeline()
    return _GLOBAL_PIPELINE


get_langfuse_bridge = get_observability_pipeline
