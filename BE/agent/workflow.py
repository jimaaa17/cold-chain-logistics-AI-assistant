"""Facade for the agentic dispatch workflow, which lives in `agent.dispatch`.

Kept so `api/server.py`, `agent/cli.py` and the tests (which patch
`agent.workflow.build_dispatch_workflow`) keep their import path. Add new code to `agent.dispatch`.
"""
from agent.dispatch import (
    DispatchContext,
    DispatchInput,
    DispatchOutput,
    DispatchResources,
    DispatchState,
    build_dispatch_workflow,
    collect_final,
    iter_dispatch_events,
    pending_approval,
    render_final,
)

__all__ = [
    "DispatchContext",
    "DispatchInput",
    "DispatchOutput",
    "DispatchResources",
    "DispatchState",
    "build_dispatch_workflow",
    "collect_final",
    "iter_dispatch_events",
    "pending_approval",
    "render_final",
]
