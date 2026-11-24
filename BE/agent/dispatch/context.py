"""Run context for the dispatch workflow: shared resources (built once, pre-warmed at startup) and
the `DispatchContext` that LangGraph injects into every node as `runtime.context`."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from langchain_core.prompts import ChatPromptTemplate

from agent.config import AgentSettings
from agent.dispatch.prompts import CLARIFY_TEXT
from agent.dispatch.prompts import assessor_prompt as ASSESSOR_PROMPT
from agent.dispatch.prompts import judge_prompt as JUDGE_PROMPT
from agent.dispatch.prompts import planner_prompt as PLANNER_PROMPT
from agent.dispatch.prompts import reasoner_prompt as REASONER_PROMPT
from agent.dispatch.prompts import router_prompt as ROUTER_PROMPT
from agent.evidence import utc_now

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
        task_llms: dict[Any, Any] | None = None,
    ) -> None:
        self.settings = settings
        self._llm = llm
        self._planner_llm = planner_llm
        self._engine = engine
        self._http = http_client
        self._retrieval = retrieval_service
        self._task_llms: dict[Any, Any] = dict(task_llms or {})
        self._lock = threading.Lock()

    def llm(self):
        with self._lock:
            if self._llm is None:
                from agent.llm import build_reasoner_llm

                self._llm = build_reasoner_llm(self.settings)
            return self._llm

    def planner_llm(self):
        llm = self.llm()
        with self._lock:
            if self._planner_llm is None:
                from agent.llm import build_default_tools

                self._planner_llm = llm.bind_tools(build_default_tools(self.settings))
            return self._planner_llm

    def task_llm(self, task):
        """The model for an `agent.llm.LLMTask`: its own if configured, else the reasoner's."""
        shared = None if task in self._task_llms else self.llm()
        with self._lock:
            if task not in self._task_llms:
                from agent.llm import build_task_llm

                self._task_llms[task] = build_task_llm(self.settings, task) or shared
            return self._task_llms[task]

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


@dataclass(frozen=True)
class DispatchContext:
    """Everything a dispatch node needs besides its state; injected per run as `runtime.context`.

    Nodes take clients from `resources` and never build or import them themselves.
    """

    settings: AgentSettings
    resources: DispatchResources
    clock: Callable[[], datetime] = utc_now
    # ChatPromptTemplate is an unhashable pydantic model, which dataclasses reject as a plain default
    reasoner_prompt: ChatPromptTemplate = field(default_factory=lambda: REASONER_PROMPT)
    planner_prompt: ChatPromptTemplate = field(default_factory=lambda: PLANNER_PROMPT)
    router_prompt: ChatPromptTemplate = field(default_factory=lambda: ROUTER_PROMPT)
    assessor_prompt: ChatPromptTemplate = field(default_factory=lambda: ASSESSOR_PROMPT)
    judge_prompt: ChatPromptTemplate = field(default_factory=lambda: JUDGE_PROMPT)
    clarify_text: str = CLARIFY_TEXT

    @classmethod
    def build(
        cls,
        settings: AgentSettings,
        resources: DispatchResources,
        clock: Callable[[], datetime] | None = None,
    ) -> "DispatchContext":
        """The context a built graph runs with; prompts default to the `dispatch/prompts` templates."""
        return cls(settings=settings, resources=resources, clock=clock or utc_now)
