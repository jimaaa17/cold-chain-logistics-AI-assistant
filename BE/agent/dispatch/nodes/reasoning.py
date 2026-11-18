"""reasoner (LLM writes from the ledger), verifier (plain-code checks) and the one-retry router."""
from __future__ import annotations

from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.dispatch.text import history, text_of
from agent.evidence import render_ledger
from agent.verifier import MAX_ATTEMPTS, verify_draft


def reasoner(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    ctx = runtime.context
    attempt = int(state.get("attempts", 0)) + 1
    runtime.stream_writer({"type": "draft_start", "attempt": attempt})
    now = datetime.fromisoformat(state["as_of"]) if state.get("as_of") else ctx.clock()
    ledger_text = render_ledger(
        state.get("ledger", []), state.get("triggers", []), state.get("historical", []),
        now=now, window_hours=ctx.settings.evidence_freshness_hours,
    )
    if state.get("gaps"):
        ledger_text += "\n\nDATA GAPS (say these sources were unavailable; do not guess):\n" + "\n".join(f"- {g}" for g in state["gaps"])
    content = f"QUESTION:\n{state['question']}\n\n{ledger_text}"
    previous = state.get("verification") or {}
    if attempt > 1 and previous.get("problems"):
        content += (
            "\n\nYOUR PREVIOUS DRAFT WAS REJECTED BY THE AUTOMATIC CHECK. Rewrite it and fix exactly these problems:\n"
            + "\n".join(f"- {p}" for p in previous["problems"])
        )
    msgs = [SystemMessage(ctx.reasoner_system), *history(state), HumanMessage(content)]
    ai = ctx.resources.llm().invoke(msgs)
    return {"draft": text_of(ai.content), "attempts": attempt}


def verifier(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    result = verify_draft(
        state.get("draft", ""),
        ledger=state.get("ledger", []),
        triggers=state.get("triggers", []),
        historical=state.get("historical", []),
        question=state.get("question", ""),
        window_hours=runtime.context.settings.evidence_freshness_hours,
    )
    result["attempt"] = state.get("attempts", 1)
    retrying = not result["passed"] and result["attempt"] < MAX_ATTEMPTS
    runtime.stream_writer({"type": "verification", **result, "retrying": retrying})
    return {"verification": result}


def route_after_verifier(state: DispatchState) -> str:
    v = state.get("verification") or {}
    if not v.get("passed") and state.get("attempts", 0) < MAX_ATTEMPTS:
        return "reasoner"
    return "action_gate"
