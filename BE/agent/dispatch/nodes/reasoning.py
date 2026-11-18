"""reasoner (LLM writes from the ledger), verifier (plain-code checks) and the one-retry router."""
from __future__ import annotations

from datetime import datetime

from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.prompts import gaps_block, retry_block
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
    previous = state.get("verification") or {}
    msgs = ctx.reasoner_prompt.format_messages(
        freshness_hours=f"{ctx.settings.evidence_freshness_hours:g}",
        max_rows=ctx.settings.sql_max_rows,
        history=history(state),
        question=state["question"],
        ledger=ledger_text,
        gaps_block=gaps_block(state.get("gaps") or []),
        retry_block=retry_block(previous.get("problems") or []) if attempt > 1 else "",
    )
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
