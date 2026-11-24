"""reasoner (LLM writes from the ledger), verifier (checks the draft) and the routers around them.

If the reasoner's model fails, the answer falls back to the evidence only: the ledger, the SOP
triggers and the gaps, with no ACTION lines (so nothing goes to approval), marked unverified, and
routed straight to the action gate without a verifier pass or retry."""
from __future__ import annotations

from datetime import datetime

from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.prompts import gaps_block, retry_block
from agent.dispatch.resilience import EmptyModelReply, run_model_step
from agent.dispatch.state import DispatchState
from agent.dispatch.text import history, text_of
from agent.evidence import fmt_value, render_ledger
from agent.rules import ACTION_LABELS
from agent.verifier import MAX_ATTEMPTS, verify_draft


FALLBACK_GAP = "answer: model unavailable, showing evidence only"


def reasoner(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    ctx = runtime.context
    attempt = int(state.get("attempts", 0)) + 1
    llm = ctx.resources.llm()
    now = datetime.fromisoformat(state["as_of"]) if state.get("as_of") else ctx.clock()
    ledger_text = render_ledger(
        state.get("ledger", []), state.get("triggers", []), state.get("historical", []),
        now=now, window_hours=ctx.settings.evidence_freshness_hours, notes=state.get("notes") or None,
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

    def call() -> str:
        # per call, not per node: a transient retry must clear tokens already streamed
        runtime.stream_writer({"type": "draft_start", "attempt": attempt})
        text = text_of(llm.invoke(msgs).content)
        if not text:
            raise EmptyModelReply("empty reply")
        return text

    result = run_model_step(
        "reasoner",
        call,
        lambda: evidence_only_answer(state),
        emit=runtime.stream_writer,
        fallback_name="evidence-only answer",
        gap=FALLBACK_GAP,
    )
    if not result.degraded:
        return {"draft": result.value, "attempts": attempt}
    # Clear any tokens streamed before the failure, then show the evidence-only answer.
    runtime.stream_writer({"type": "draft_start", "attempt": attempt, "fallback": True})
    verification = {
        "passed": False,
        "skipped": False,
        "degraded": True,
        "problems": ["The language model was unavailable; this answer lists the evidence only and was not checked."],
        "numbers_checked": 0,
        "proposed_actions": [],
        "attempt": attempt,
    }
    runtime.stream_writer({"type": "verification", **verification, "retrying": False})
    return {
        "draft": result.value,
        "attempts": attempt,
        "answer_degraded": True,
        "verification": verification,
        "gaps": result.gaps,
    }


def evidence_only_answer(state: DispatchState) -> str:
    """Deterministic answer from the ledger. No ACTION tags: proposing actions needs the model's judgement."""
    records = state.get("ledger", [])
    lines = [
        "## Summary",
        "The language model is unavailable, so this answer lists the evidence only. "
        "Nothing below has been interpreted, and no action is proposed.",
    ]

    def record_line(rec: dict) -> str:
        when = rec["as_of"][:16].replace("T", " ") if rec.get("as_of") else "no timestamp"
        fields = " · ".join(f"{k}={fmt_value(v)}" for k, v in (rec.get("fields") or {}).items())
        return f"- [{rec['id']}] {when} UTC, {rec['freshness']}" + (f": {fields}" if fields else "")

    for title, source in (("Readings", "telemetry"), ("Weather", "weather")):
        group = [r for r in records if r["source"] == source]
        if group:
            lines += ["", f"## {title}", *[record_line(r) for r in group]]
    triggers = state.get("triggers") or []
    lines += ["", "## SOP triggers on fresh data"]
    lines += [
        f"- {t['rule']} on [{t['evidence']}]: {t['summary']} ({t['sop_ref']}). "
        f"The SOP allows: {'; '.join(ACTION_LABELS.get(a, a) for a in t['actions']) or 'no action'}. "
        "A dispatcher must decide."
        for t in triggers
    ] or ["None."]
    historical = state.get("historical") or []
    if historical:
        lines += ["", "## Historical or undated breaches (not current)"]
        lines += [f"- {t['rule']} on [{t['evidence']}] ({t['freshness']}): {t['summary']}" for t in historical]
    sop = [r for r in records if r["source"] == "sop"]
    if sop:
        lines += ["", "## SOP excerpts"]
        lines += [f"- [{r['id']}] {r['origin']}: {(r.get('text') or '')[:240]}" for r in sop]
    return "\n".join(lines)


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


def route_after_reasoner(state: DispatchState) -> str:
    """An evidence-only fallback answer isn't verified or retried."""
    return "action_gate" if state.get("answer_degraded") else "verifier"


def route_after_verifier(state: DispatchState) -> str:
    v = state.get("verification") or {}
    if not v.get("passed") and state.get("attempts", 0) < MAX_ATTEMPTS:
        return "reasoner"
    return "action_gate"
