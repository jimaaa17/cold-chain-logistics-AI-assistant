"""planner (an LLM picks tool calls for open questions) and the router that fans them out."""
from __future__ import annotations

from langgraph.runtime import Runtime
from langgraph.types import Send

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.dispatch.text import history

# planner tool name -> (gather node, payload builder)
_TOOL_TO_NODE = {
    "query_telemetry_db": ("gather_telemetry", lambda a: {"sql": str(a.get("sql_query", ""))}),
    "fetch_corridor_conditions": ("gather_weather", lambda a: a),
    "search_compliance_sop": ("gather_sop", lambda a: {"query": str(a.get("query", ""))}),
}


def planner(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    ctx = runtime.context
    msgs = ctx.planner_prompt.format_messages(history=history(state), question=state["question"])
    ai = ctx.resources.planner_llm().invoke(msgs)
    calls = [{"name": tc["name"], "args": dict(tc.get("args") or {})} for tc in (getattr(ai, "tool_calls", None) or [])]
    return {"planned_calls": calls}


def route_after_planner(state: DispatchState):
    sends = []
    for call in state.get("planned_calls", []):
        target = _TOOL_TO_NODE.get(call["name"])
        if target:  # unknown tool names are dropped
            node, payload = target
            sends.append(Send(node, payload(call["args"])))
    return sends or "ledger"
