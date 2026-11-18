"""Graph state for the dispatch workflow.

`DispatchState` is the internal state every node sees. `DispatchInput` and `DispatchOutput` are the
graph's public contract: callers send messages in and get the final answer, the verification result
and the dispatcher's decision out."""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def _collect(old: list | None, new: list | None) -> list:
    """Fan-in reducer for parallel gather nodes; `None` resets it at the start of each turn."""
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
    gaps: list[str]
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
