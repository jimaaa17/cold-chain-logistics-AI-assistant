"""Typed configuration for the AI Observability & Compliance Telemetry Subsystem (`BE/observability/`)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True, frozen=True)
class ObservabilitySettings:
    """Immutable configuration governing Langfuse tracing and MSSQL compliance auditing."""

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    sql_audit_enabled: bool = True
    sql_audit_table: str = "FDE_VIEWS.AgentAuditLog"
    sql_rbac_role: str = "USR_FDE_RO (INSERT-ONLY per setup_security_view.sql)"
    ring_buffer_max_spans: int = 1000
    service_name: str = "fde-cold-chain-assistant"

    @classmethod
    def from_env(cls) -> "ObservabilitySettings":
        return cls(
            langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY") or None,
            langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY") or None,
            langfuse_host=(os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com").strip(),
            sql_audit_enabled=os.getenv("AUDIT_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
            sql_audit_table=os.getenv("AUDIT_TABLE", "FDE_VIEWS.AgentAuditLog").strip(),
            ring_buffer_max_spans=int(os.getenv("OBSERVABILITY_BUFFER_SIZE", "1000")),
        )
