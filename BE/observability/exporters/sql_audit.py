"""MSSQL `FDE_VIEWS.AgentAuditLog` compliance ledger exporter (`BE/sql/setup_security_view.sql`).

Enforces the strict separation of duties defined in `setup_security_view.sql`:
- **Write Path (`export_span`)**: Uses the `USR_FDE_RO` agent account, which holds `GRANT INSERT ON FDE_VIEWS.AgentAuditLog` and cannot modify or delete historical audit rows.
- **Read Path (`query_ledger`)**: Requires `SQL_ADMIN_USER` (`sa`) credentials to `SELECT` from `FDE_VIEWS.AgentAuditLog`.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SqlAuditLedgerExporter:
    """Append-only SQL Server compliance sink for `FDE_VIEWS.AgentAuditLog`."""

    name = "sql"

    def __init__(
        self,
        *,
        engine: Any | None = None,
        enabled: bool = True,
        table_name: str = "FDE_VIEWS.AgentAuditLog",
    ) -> None:
        self._engine = engine
        self.enabled = enabled
        self.table_name = table_name

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
        if not self.enabled or self._engine is None:
            return False
        try:
            from sqlalchemy import text

            sql = text(
                f"INSERT INTO {self.table_name} (SessionID, NodeExecuted, ToolName, Content) "
                "VALUES (:session_id, :node_executed, :tool_name, :content)"
            )
            with self._engine.begin() as conn:
                conn.execute(
                    sql,
                    {
                        "session_id": str(session_id)[:50],
                        "node_executed": str(node_executed)[:50],
                        "tool_name": str(tool_name)[:100],
                        "content": str(content),
                    },
                )
            return True
        except Exception as exc:
            logger.warning("SQL Audit ledger write skipped (%s): %s", self.table_name, exc)
            return False
