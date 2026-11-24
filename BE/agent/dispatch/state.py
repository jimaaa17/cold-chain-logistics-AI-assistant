"""Graph state for the dispatch workflow.

`DispatchState` is the internal state every node sees. `DispatchInput` and `DispatchOutput` are the
graph's public contract: callers send messages in and get the final answer, the verification result
and the dispatcher's decision out."""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def _collect(old: list | None, new: list | None) -> list:
    """Append reducer for lists several nodes write to; `intake` sends `None` to reset it each turn."""
    if new is None:
        return []
    return (old or []) + list(new)


class DispatchState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    question: str
    intent: dict
    planned_calls: list[dict]
    gathered: Annotated[list[dict], _collect]
    ledger: list[dict]
    # "<step>: <reason>" for each source or model step that failed this turn
    gaps: Annotated[list[str], _collect]
    notes: list[str]  # valid-but-empty results; shown in the ledger, not failures
    answer_degraded: bool  # the evidence-only fallback answer, which skips the verifier
    triggers: list[dict]
    historical: list[dict]
    as_of: str
    draft: str
    attempts: int
    verification: dict | None
    approval: dict | None
    final: str


class DispatchInput(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


class DispatchOutput(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    final: str
    verification: dict | None
    approval: dict | None
