"""Refactored Cold-Chain Logistics Backend Agent Orchestration Package.

Decouples the LangGraph ReAct reasoning engine, read-only SQL telemetry tool, live corridor
weather API tool, in-process RAG SOP retrieval tool, and immutable SQL audit logger from the
prototype UI in ``src/``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.config import AgentSettings, get_agent_settings
    from agent.graph import AgentOrchestrator, AgentState, build_agent_graph

__all__ = [
    "AgentOrchestrator",
    "AgentSettings",
    "AgentState",
    "build_agent_graph",
    "get_agent_settings",
]


def __getattr__(name: str) -> Any:
    if name in ("AgentSettings", "get_agent_settings"):
        from agent import config

        return getattr(config, name)
    if name in ("AgentOrchestrator", "AgentState", "build_agent_graph"):
        from agent import graph

        return getattr(graph, name)
    raise AttributeError(f"module 'agent' has no attribute {name!r}")
