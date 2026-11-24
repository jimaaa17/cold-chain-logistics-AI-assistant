"""intake (routes the question in code), its router, and clarify (no tools, no LLM)."""
from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Send

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.dispatch.text import text_of
from agent.intent import SCAN_SOP_QUERY, Intent, build_scan_sql, classify_intent


def intake(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    human = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
    question = text_of(human[-1].content) if human else ""
    intent = classify_intent(question)
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


def route_after_intake(state: DispatchState, runtime: Runtime[DispatchContext]):
    intent = Intent(**state["intent"])
    if intent.kind == "clarify":
        return "clarify"
    if intent.kind == "open":
        return "planner"
    cfg = runtime.context.settings
    sends: list[Send] = []
    if intent.kind == "incident_scan":
        sql = build_scan_sql(intent, half_width_deg=cfg.area_half_width_deg, max_rows=cfg.sql_max_rows)
        sends.append(Send("gather_telemetry", {"sql": sql}))
        if intent.has_area:
            sends.append(
                Send(
                    "gather_weather",
                    {"latitude": intent.latitude, "longitude": intent.longitude, "past_days": 0, "forecast_days": 1},
                )
            )
        sends.append(Send("gather_sop", {"query": SCAN_SOP_QUERY}))
    else:  # sop_lookup
        sends.append(Send("gather_sop", {"query": state["question"]}))
    return sends


def clarify(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    return {
        "draft": runtime.context.clarify_text,
        "verification": {"passed": True, "skipped": True, "problems": [], "proposed_actions": []},
    }
