"""Custom stream events emitted by the dispatch nodes.

The shapes below are the contract with `api/server.py` (SSE framing and audit) and
`FE/src/features/dispatch/hooks/dispatchEvents.js`; change all three together.
`token` and `approval_required` are produced by `streaming.iter_dispatch_events`, not by nodes.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.config import get_stream_writer


class StatusEvent(TypedDict):
    type: Literal["status"]
    phase: str
    label: str
    intent: str


class ToolInputEvent(TypedDict):
    type: Literal["tool_input"]
    name: str
    args: dict[str, Any]


class ToolOutputEvent(TypedDict):
    type: Literal["tool_output"]
    name: str
    content: str


class EvidenceEvent(TypedDict):
    type: Literal["evidence"]
    records: int
    fresh: int
    historical: int
    triggers: list[str]
    historical_breaches: int
    gaps: list[str]


class DraftStartEvent(TypedDict):
    type: Literal["draft_start"]
    attempt: int


class VerificationEvent(TypedDict, total=False):
    """`verify_draft`'s result plus `attempt` and `retrying`."""

    type: Literal["verification"]
    passed: bool
    problems: list[str]
    proposed_actions: list[dict]
    attempt: int
    retrying: bool


class FinalAnswerEvent(TypedDict):
    type: Literal["final_answer"]
    content: str
    verified: bool
    approval: dict | None


def emit(event: dict) -> None:
    """Sends an event to the run's custom stream; a no-op outside a streaming run."""
    try:
        get_stream_writer()(event)
    except Exception:  # outside a streaming run (e.g. graph.invoke) there is no writer
        pass
