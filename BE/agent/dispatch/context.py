"""Shared resources for the dispatch workflow (built once, pre-warmed at server startup)."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from agent.config import AgentSettings

logger = logging.getLogger(__name__)


class DispatchResources:
    """Lazily built, thread-safe handles shared by every run of the workflow."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        llm: Any | None = None,
        planner_llm: Any | None = None,
        engine: Any | None = None,
        http_client: Any | None = None,
        retrieval_service: Any | None = None,
    ) -> None:
        self.settings = settings
        self._llm = llm
        self._planner_llm = planner_llm
        self._engine = engine
        self._http = http_client
        self._retrieval = retrieval_service
        self._lock = threading.Lock()

    def llm(self):
        with self._lock:
            if self._llm is None:
                from agent.graph import build_reasoner_llm

                self._llm = build_reasoner_llm(self.settings)
            return self._llm

    def planner_llm(self):
        llm = self.llm()
        with self._lock:
            if self._planner_llm is None:
                from agent.graph import build_default_tools

                self._planner_llm = llm.bind_tools(build_default_tools(self.settings))
            return self._planner_llm

    def engine(self):
        with self._lock:
            if self._engine is None:
                from agent.tools.telemetry_sql import create_telemetry_engine

                self._engine = create_telemetry_engine(self.settings)
            return self._engine

    def http(self):
        with self._lock:
            if self._http is None:
                from agent.tools.corridor_api import build_corridor_client

                self._http = build_corridor_client(self.settings)
            return self._http

    def retrieval(self):
        with self._lock:
            if self._retrieval is None:
                from rag.config import get_settings
                from rag.retrieval.service import RetrievalService

                self._retrieval = RetrievalService.from_settings(get_settings())
            return self._retrieval

    def warm(self) -> dict[str, str]:
        """Loads everything the first request would otherwise pay for. Never raises."""
        report: dict[str, str] = {}
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("llm_client", self.llm),
            ("sql_engine", self.engine),
            ("http_client", self.http),
            ("sop_retrieval", lambda: self.retrieval().warm_up()),
        ]
        for name, step in steps:
            try:
                step()
                report[name] = "ready"
            except Exception as exc:  # degraded, not fatal: the tool reports the gap at request time
                report[name] = f"failed: {type(exc).__name__}"
                logger.warning("Pre-warm step %s failed: %s", name, type(exc).__name__)
        return report
