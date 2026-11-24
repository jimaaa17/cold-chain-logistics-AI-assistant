"""intake (resets the turn and routes the question in code) and clarify (no tools, no LLM)."""
from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.dispatch.text import text_of
from agent.intent import classify_intent


def intake(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    human = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
    question = text_of(human[-1].content) if human else ""
    intent = classify_intent(question, runtime.context.clock())
    runtime.stream_writer({"type": "status", "phase": "routing", "label": f"Route: {intent.kind}", "intent": intent.kind})
    return {  # resets every per-turn field; `gathered: None` clears the fan-in reducer
        "question": question,
        "intent": intent.to_state(),
        "planned_calls": [],
        "gathered": None,
        "ledger": [],
        "gaps": None,
        "notes": [],
        "answer_degraded": False,
        "triggers": [],
        "historical": [],
        "draft": "",
        "attempts": 0,
        "verification": None,
        "approval": None,
    }


def clarify(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    return {
        "draft": runtime.context.clarify_text,
        "verification": {"passed": True, "skipped": True, "problems": [], "proposed_actions": []},
    }
