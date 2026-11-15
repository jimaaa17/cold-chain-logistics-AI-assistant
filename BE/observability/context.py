"""Distributed trace and span ID generation & contextvar propagation."""
from __future__ import annotations

import contextvars
import hashlib
import uuid
from dataclasses import dataclass

_ACTIVE_TRACE_CTX: contextvars.ContextVar["TraceContext | None"] = contextvars.ContextVar(
    "fde_active_trace_ctx", default=None
)


def new_trace_id(session_id: str = "") -> str:
    """Generate a compact deterministic-prefix trace ID (`tr-<12 hex chars>`)."""
    seed = f"{session_id}:{uuid.uuid4().hex}"
    return f"tr-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def new_span_id() -> str:
    """Generate a unique span ID (`sp-<8 hex chars>`)."""
    return f"sp-{uuid.uuid4().hex[:8]}"


@dataclass(slots=True, frozen=True)
class TraceContext:
    session_id: str
    trace_id: str
    parent_span_id: str | None = None

    @classmethod
    def start(cls, session_id: str, trace_id: str | None = None) -> "TraceContext":
        ctx = cls(session_id=session_id, trace_id=trace_id or new_trace_id(session_id))
        _ACTIVE_TRACE_CTX.set(ctx)
        return ctx

    @staticmethod
    def current() -> "TraceContext | None":
        return _ACTIVE_TRACE_CTX.get()
