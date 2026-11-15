"""Langfuse distributed LLM & tool tracing exporter (Cloud or Self-Hosted)."""
from __future__ import annotations

import logging
import os
from typing import Any

from observability.context import new_trace_id
from observability.models import SpanKind, infer_span_kind

logger = logging.getLogger(__name__)


class LangfuseSpanExporter:
    """Exports hierarchical traces, generations, and tool spans to Langfuse."""

    name = "langfuse"

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ) -> None:
        self.public_key = public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
        self.secret_key = secret_key or os.getenv("LANGFUSE_SECRET_KEY")
        self.host = (host or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com").strip()
        self._client: Any | None = None
        self._sdk_available = self._probe_sdk()

    @staticmethod
    def _probe_sdk() -> bool:
        try:
            import langfuse  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def is_configured(self) -> bool:
        return bool(self.public_key and self.secret_key)

    @property
    def is_active(self) -> bool:
        return self.is_configured and self._sdk_available

    def get_langchain_callback(self, session_id: str, *, trace_id: str | None = None) -> Any | None:
        """Return a Langfuse LangChain CallbackHandler when configured & installed."""
        if not self.is_active:
            return None
        try:
            try:
                from langfuse.callback import CallbackHandler  # type: ignore[import-not-found]
            except ImportError:
                from langfuse.langchain import CallbackHandler  # type: ignore[import-not-found]

            return CallbackHandler(
                public_key=self.public_key,
                secret_key=self.secret_key,
                host=self.host,
                session_id=session_id,
                trace_name="fde-cold-chain-dispatch",
                metadata={"trace_id": trace_id or new_trace_id(session_id)},
            )
        except Exception as exc:
            logger.warning("Could not initialize Langfuse CallbackHandler: %s", exc)
            return None

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
        if not self.is_active:
            return False
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]

            if self._client is None:
                self._client = Langfuse(
                    public_key=self.public_key,
                    secret_key=self.secret_key,
                    host=self.host,
                )
            trace = self._client.trace(
                id=trace_id,
                session_id=session_id,
                name="fde-cold-chain-dispatch",
            )
            kind = infer_span_kind(node_executed, tool_name)
            span_meta = {
                "node": node_executed,
                "tool": tool_name,
                "span_kind": kind.value,
                "elapsed_ms": elapsed_ms,
                **(metadata or {}),
            }
            if kind == SpanKind.GENERATION and hasattr(trace, "generation"):
                trace.generation(
                    id=span_id,
                    name=f"{node_executed}:{tool_name}",
                    output=content,
                    metadata=span_meta,
                )
            else:
                trace.span(
                    id=span_id,
                    name=f"{node_executed}:{tool_name}",
                    output=content,
                    metadata=span_meta,
                )
            return True
        except Exception as exc:
            logger.debug("Langfuse span export skipped: %s", exc)
            return False
