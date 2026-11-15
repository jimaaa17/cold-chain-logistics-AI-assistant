"""Read-only SQL telemetry tool with strict query validation and connection pooling (TDD 3.1/3.2)."""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from rag.core.resilience import with_retry

if TYPE_CHECKING:
    from agent.config import AgentSettings

logger = logging.getLogger(__name__)

_FORBIDDEN_SQL_PATTERNS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|EXEC|EXECUTE|MERGE|GRANT|REVOKE|DENY|BACKUP|RESTORE|SHUTDOWN|OPENROWSET|OPENDATASOURCE)\b"
    r"|(\bxp_\w+|\bsp_\w+)",
    re.IGNORECASE,
)
_VIEW_PATTERN = re.compile(r"\b(FDE_VIEWS\s*\.\s*VW_ACTIVE_FLEET|VW_ACTIVE_FLEET)\b", re.IGNORECASE)


class UnsafeSqlError(ValueError):
    pass


def validate_readonly_select(sql_query: str) -> str:
    """Validate that a SQL query is a single-statement SELECT against VW_ACTIVE_FLEET."""
    cleaned = sql_query.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        raise UnsafeSqlError("SECURITY BLOCK: Empty SQL query is not allowed.")
    if ";" in cleaned or "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
        raise UnsafeSqlError(
            "SECURITY BLOCK: Multi-statement queries and SQL comments are prohibited."
        )
    if not cleaned.upper().startswith("SELECT"):
        raise UnsafeSqlError("SECURITY BLOCK: Only SELECT operations are authorized on this view.")
    if _FORBIDDEN_SQL_PATTERNS.search(cleaned):
        raise UnsafeSqlError("SECURITY BLOCK: Disallowed DDL/DML or stored procedure keyword in query.")
    if not _VIEW_PATTERN.search(cleaned):
        raise UnsafeSqlError(
            "SECURITY BLOCK: Queries are restricted exclusively to FDE_VIEWS.VW_ACTIVE_FLEET."
        )
    return cleaned


def create_telemetry_engine(settings: "AgentSettings") -> Engine:
    params = urllib.parse.quote_plus(settings.odbc_connection_string())
    return create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
    )


def execute_telemetry_query(engine: Engine, sql_query: str, *, max_rows: int = 10) -> str:
    validated_sql = validate_readonly_select(sql_query)

    @with_retry(attempts=3, max_wait=2.0)
    def _run() -> tuple[list[str], list[Any]]:
        with engine.connect() as conn:
            cursor = conn.execute(text(validated_sql))
            columns = list(cursor.keys())
            rows = cursor.fetchmany(max_rows)
            return columns, list(rows)

    columns, rows = _run()
    if not rows:
        return "No records matched the query criteria."

    lines = [f"COLUMNS: {', '.join(columns)}"]
    for row in rows:
        lines.append(str(tuple(row)))
    return "\n".join(lines) + "\n"


def make_telemetry_sql_tool(
    settings: "AgentSettings | None" = None,
    *,
    engine: Engine | None = None,
):
    """Create the `query_telemetry_db` LangChain tool with lazy engine creation."""
    from agent.config import get_agent_settings

    cfg = settings or get_agent_settings()
    _engine: Engine | None = engine

    def _get_engine() -> Engine:
        nonlocal _engine
        if _engine is None:
            _engine = create_telemetry_engine(cfg)
        return _engine

    @tool("query_telemetry_db")
    def query_telemetry_db(sql_query: str) -> str:
        """Executes a SQL SELECT query against the FDE_VIEWS.VW_ACTIVE_FLEET view.
        Columns available:
        Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code,
        Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index.
        Always write standard T-SQL queries."""
        try:
            validated = validate_readonly_select(sql_query)
        except UnsafeSqlError as exc:
            return str(exc)
        try:
            return execute_telemetry_query(_get_engine(), validated, max_rows=cfg.sql_max_rows)
        except Exception as exc:
            logger.warning("Telemetry DB query failed: %s", exc)
            return f"Database Error: {exc}"

    return query_telemetry_db
