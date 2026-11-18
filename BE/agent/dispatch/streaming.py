"""Streaming adapter: graph output -> normalised events for the BFF and CLI."""
from __future__ import annotations

from typing import Any, Iterable, Iterator

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.dispatch.text import text_of


def pending_approval(graph, session_id: str) -> dict | None:
    """The approval request the session is paused on, if any."""
    snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return dict(intr.value)
    return None


def iter_dispatch_events(
    graph,
    *,
    session_id: str,
    prompt: str | None = None,
    resume: dict | None = None,
    callbacks: list | None = None,
) -> Iterator[dict]:
    """Runs (or resumes) one turn and yields normalised events:
    status, tool_input, tool_output, evidence, draft_start, token, verification,
    approval_required, final_answer.

    This is the single place the dispatch graph is run: it passes `graph.dispatch_context`, which
    every node reads as `runtime.context`."""
    config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
    if callbacks:
        config["callbacks"] = callbacks
    payload: Any = Command(resume=resume) if resume is not None else {"messages": [HumanMessage(content=prompt or "")]}
    stream = graph.stream(
        payload,
        config=config,
        context=graph.dispatch_context,
        stream_mode=["custom", "messages", "updates"],
    )
    for mode, chunk in stream:
        if mode == "custom" and isinstance(chunk, dict):
            yield chunk
        elif mode == "messages":
            message, meta = chunk
            if (meta or {}).get("langgraph_node") == "reasoner":
                text = text_of(getattr(message, "content", ""), strip=False)
                if text:
                    yield {"type": "token", "text": text}
        elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
            for intr in chunk["__interrupt__"]:
                yield {"type": "approval_required", **dict(intr.value)}


def collect_final(events: Iterable[dict]) -> tuple[str, dict | None]:
    """Drains events; returns (final answer text, pending approval request or None)."""
    final, pending = "", None
    for event in events:
        if event.get("type") == "final_answer":
            final = event.get("content", "")
        elif event.get("type") == "approval_required":
            pending = event
    return final, pending
