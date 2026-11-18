"""Agentic dispatch workflow (LangGraph). `graph.py` is the topology, `nodes/` the steps,
`context.py` the injected dependencies and `streaming.py` the adapter callers use to run it."""
from agent.dispatch.context import DispatchContext, DispatchResources
from agent.dispatch.graph import build_dispatch_workflow
from agent.dispatch.nodes.finalize import render_final
from agent.dispatch.state import DispatchState
from agent.dispatch.streaming import collect_final, iter_dispatch_events, pending_approval

__all__ = [
    "DispatchContext",
    "DispatchResources",
    "DispatchState",
    "build_dispatch_workflow",
    "collect_final",
    "iter_dispatch_events",
    "pending_approval",
    "render_final",
]
