"""route: lets a model place the questions the coded router can't, then fans out.

`intake` has already routed in code. Where code is confident (`incident_scan`, `sop_lookup`) no model
is called, so the standard question keeps its latency. Guardrails: coordinates come from the gazetteer
or the question's own text, and temperature limits must appear in the question.
"""
from __future__ import annotations

import re
from dataclasses import replace

from langgraph.runtime import Runtime
from langgraph.types import Send

from agent.dispatch.consult import consult, emit_decision
from agent.dispatch.context import DispatchContext
from agent.dispatch.schemas import RouteDecision
from agent.dispatch.state import DispatchState
from agent.dispatch.text import history
from agent.intent import GAZETTEER, SCAN_SOP_QUERY, Intent, build_scan_sql
from agent.llm import LLMTask
from agent.rollup import MAX_ROLLUP_ROWS, month_sql, year_sql

FALLBACK_GAP = "router: model unavailable, routed by keyword rules"


def guard_route(decision: RouteDecision, question: str, code: Intent) -> tuple[Intent, list[str]]:
    """The model's route with every extracted value checked; returns (intent, gaps)."""
    numbers = {float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", question)}
    gaps: list[str] = []
    place, lat, lon = code.place, code.latitude, code.longitude
    if not code.has_area:
        name = (decision.place or "").strip().lower()
        typed = (
            decision.latitude in numbers and decision.longitude in numbers
            and -90 <= decision.latitude <= 90 and -180 <= decision.longitude <= 180
        )
        if name in GAZETTEER:
            place, (lat, lon) = name, GAZETTEER[name]
        elif typed:
            place, lat, lon = f"{decision.latitude:g}, {decision.longitude:g}", decision.latitude, decision.longitude
        elif name:
            gaps.append(f"router: unknown place '{name[:40]}', searched without an area")

    def limit(coded: float | None, extracted: float | None) -> float | None:
        return coded if coded is not None else (extracted if extracted in numbers else None)

    intent = replace(
        code, kind=decision.kind, place=place, latitude=lat, longitude=lon,
        min_temp_c=limit(code.min_temp_c, decision.min_temp_c), max_temp_c=limit(code.max_temp_c, decision.max_temp_c),
        reason=code.reason if decision.kind == code.kind else "routed by the model",
    )
    return intent, gaps


def route(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    mode = runtime.context.settings.routing_mode
    code, question = Intent(**state["intent"]), state["question"]
    if mode == "code" or code.kind not in {"open", "clarify"} or not question.strip():
        return {}
    decision, gaps = consult(
        runtime, decision="routing", mode=mode, task=LLMTask.ROUTER, schema=RouteDecision,
        messages=runtime.context.router_prompt.format_messages(
            history=history(state), question=question, places=", ".join(sorted(GAZETTEER)),
        ),
        fallback_name="keyword routing", gap=FALLBACK_GAP,
    )
    if decision is None:
        return {"gaps": gaps}
    intent, guard_gaps = guard_route(decision, question, code)
    emit_decision(
        runtime, decision="routing", mode=mode, agreed=replace(intent, reason="") == replace(code, reason=""),
        code=code.to_state(), model=intent.to_state(), reasoning=decision.reasoning,
    )
    if mode == "shadow":
        return {}
    runtime.stream_writer({"type": "status", "phase": "routing", "label": f"Route: {intent.kind}", "intent": intent.kind})
    return {"intent": intent.to_state(), "gaps": guard_gaps}


def route_after_route(state: DispatchState, runtime: Runtime[DispatchContext]):
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
        if intent.has_area and intent.period is None:  # today's weather says nothing about a past period
            sends.append(
                Send(
                    "gather_weather",
                    {"latitude": intent.latitude, "longitude": intent.longitude, "past_days": 0, "forecast_days": 1},
                )
            )
        sends.append(Send("gather_sop", {"query": SCAN_SOP_QUERY}))
    elif intent.kind == "sop_history":
        rollups = [("year", year_sql)] + ([("month", month_sql)] if intent.seasonal else [])
        for name, build in rollups:
            sql = build(intent, half_width_deg=cfg.area_half_width_deg)
            sends.append(Send("gather_telemetry", {"sql": sql, "max_rows": MAX_ROLLUP_ROWS, "rollup": name}))
        sends.append(Send("gather_sop", {"query": SCAN_SOP_QUERY}))
    else:  # sop_lookup
        sends.append(Send("gather_sop", {"query": state["question"]}))
    return sends
