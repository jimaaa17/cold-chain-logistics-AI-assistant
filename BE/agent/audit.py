"""Immutable dual-sink audit logging for agent reasoning and tool execution (TDD 3.3).

Delegates to `BE/observability/`:
1. `SqlAuditLedgerExporter` -> writes append-only records to `FDE_VIEWS.AgentAuditLog`
   in SQL Server using `USR_FDE_RO`'s `INSERT`-only permission from `BE/sql/setup_security_view.sql`.
2. `ObservabilityPipeline` (`LangfuseSpanExporter` + `InMemorySpanExporter`) -> records
   hierarchical spans with `trace_id`, `span_id`, `span_kind`, and `elapsed_ms`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from observability import (
    LangfuseBridge,
    ObservabilityPipeline,
    SqlAuditLedgerExporter,
    get_observability_pipeline,
    new_span_id,
    new_trace_id,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from agent.config import AgentSettings


@dataclass(slots=True, frozen=True)
class AuditEntry:
    session_id: str
    node_executed: str
    tool_name: str
    content: str
    timestamp_utc: str
    trace_id: str = ""
    span_id: str = ""
    elapsed_ms: int = 0


class AuditLogger:
    def __init__(
        self,
        *,
        engine: "Engine | None" = None,
        enabled: bool = True,
        table_name: str = "FDE_VIEWS.AgentAuditLog",
        langfuse_bridge: ObservabilityPipeline | None = None,
    ) -> None:
        self._engine = engine
        self.enabled = enabled
        self.table_name = table_name
        self.sql_exporter = SqlAuditLedgerExporter(
            engine=engine,
            enabled=enabled,
            table_name=table_name,
        )
        self.bridge = langfuse_bridge or get_observability_pipeline()
        self.entries: list[AuditEntry] = []

    @classmethod
    def from_settings(
        cls,
        settings: "AgentSettings",
        *,
        engine: "Engine | None" = None,
        langfuse_bridge: ObservabilityPipeline | None = None,
    ) -> "AuditLogger":
        bridge = langfuse_bridge or LangfuseBridge(
            public_key=settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else None,
            secret_key=settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else None,
            host=settings.langfuse_host,
        )
        return cls(
            engine=engine,
            enabled=settings.audit_enabled,
            table_name=settings.audit_table,
            langfuse_bridge=bridge,
        )

    def record(
        self,
        session_id: str,
        node_executed: str,
        tool_name: str,
        content: Any,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        elapsed_ms: int = 0,
    ) -> AuditEntry:
        effective_trace_id = trace_id or new_trace_id(session_id)
        effective_span_id = span_id or new_span_id()
        text_content = str(content)

        sql_persisted = self.sql_exporter.export_span(
            session_id=session_id,
            trace_id=effective_trace_id,
            span_id=effective_span_id,
            node_executed=node_executed,
            tool_name=tool_name,
            content=text_content,
            elapsed_ms=elapsed_ms,
        )

        self.bridge.record_span(
            session_id=session_id,
            node_executed=node_executed,
            tool_name=tool_name,
            content=text_content,
            trace_id=effective_trace_id,
            span_id=effective_span_id,
            elapsed_ms=elapsed_ms,
            sql_persisted=sql_persisted,
        )

        entry = AuditEntry(
            session_id=str(session_id)[:50],
            node_executed=str(node_executed)[:50],
            tool_name=str(tool_name)[:100],
            content=text_content,
            timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            trace_id=effective_trace_id,
            span_id=effective_span_id,
            elapsed_ms=elapsed_ms,
        )
        self.entries.append(entry)
        return entry
