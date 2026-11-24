"""planner (an LLM picks tool calls for open questions) and the router that fans them out.

If the model fails, a code plan runs instead: the incident-scan query when the question names an area
or a temperature filter, plus an SOP search. Calls to tools we don't have become gaps.
"""
from __future__ import annotations

from langgraph.runtime import Runtime
from langgraph.types import Send

from agent.dispatch.context import DispatchContext
from agent.dispatch.prompts import intent_variables
from agent.dispatch.resilience import run_model_step
from agent.dispatch.state import DispatchState
from agent.dispatch.text import history
from agent.intent import Intent, build_scan_sql

# planner tool name -> (gather node, payload builder)
_TOOL_TO_NODE = {
    "query_telemetry_db": ("gather_telemetry", lambda a: {"sql": str(a.get("sql_query", ""))}),
    "fetch_corridor_conditions": ("gather_weather", lambda a: a),
    "search_compliance_sop": ("gather_sop", lambda a: {"query": str(a.get("query", ""))}),
}

FALLBACK_GAP = (
    "planner: model unavailable, used the standard scan; counts, averages or comparisons were not computed"
)


def code_plan(intent: Intent, question: str, *, half_width_deg: float, max_rows: int) -> list[dict]:
    calls: list[dict] = []
    if intent.has_area or intent.min_temp_c is not None or intent.max_temp_c is not None:
        sql = build_scan_sql(intent, half_width_deg=half_width_deg, max_rows=max_rows)
        calls.append({"name": "query_telemetry_db", "args": {"sql_query": sql}})
    calls.append({"name": "search_compliance_sop", "args": {"query": question}})
    return calls


def planner(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    ctx, cfg = runtime.context, runtime.context.settings
    intent = Intent(**state["intent"])
    llm = ctx.resources.planner_llm()
    msgs = ctx.planner_prompt.format_messages(
        history=history(state),
        question=state["question"],
        max_rows=cfg.sql_max_rows,
        **intent_variables(intent, half_width_deg=cfg.area_half_width_deg),
    )

    def call() -> list[dict]:
        ai = llm.invoke(msgs)
        return [{"name": tc["name"], "args": dict(tc.get("args") or {})} for tc in (getattr(ai, "tool_calls", None) or [])]

    result = run_model_step(
        "planner",
        call,
        lambda: code_plan(intent, state["question"], half_width_deg=cfg.area_half_width_deg, max_rows=cfg.sql_max_rows),
        emit=runtime.stream_writer,
        fallback_name="standard scan",
        gap=FALLBACK_GAP,
    )
    # Routers can't write state, so unknown tools are dropped (and recorded) here.
    unknown = [c["name"] for c in result.value if c["name"] not in _TOOL_TO_NODE]
    return {
        "planned_calls": [c for c in result.value if c["name"] in _TOOL_TO_NODE],
        "gaps": [*result.gaps, *(f"planner: unknown tool {name}" for name in unknown)],
    }


def route_after_planner(state: DispatchState):
    sends = []
    for call in state.get("planned_calls", []):
        node, payload = _TOOL_TO_NODE[call["name"]]
        sends.append(Send(node, payload(call["args"])))
    return sends or "ledger"
