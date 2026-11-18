"""Topology of the dispatch workflow. Node bodies live in `dispatch/nodes/`.

    intake ─┬─ clarify ────────────────────────────────────────────────┐
            ├─ (fixed fan-out, Send) gather_telemetry ┐                  │
            │                         gather_weather   ├─ ledger ─ rules ─ reasoner ─ verifier ─┬─ action_gate ─ finalize
            │                         gather_sop       ┘                    ▲                   │   (interrupt for
            └─ planner (LLM picks tools, Send) ───────┘                    └── retry once ─────┘    dispatcher approval)

Who decides what: tools report facts, code applies SOP thresholds/freshness/verification, the LLM
judges and explains, and a dispatcher approves high-stakes actions.

The compiled graph carries its `DispatchContext` as `graph.dispatch_context`; run it through
`streaming.iter_dispatch_events`, which passes that context in. Calling `graph.stream`/`invoke`
directly requires `context=graph.dispatch_context`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agent.config import AgentSettings, get_agent_settings
from agent.dispatch.context import DispatchContext, DispatchResources
from agent.dispatch.nodes.approval import action_gate
from agent.dispatch.nodes.finalize import finalize
from agent.dispatch.nodes.gather import gather_sop, gather_telemetry, gather_weather
from agent.dispatch.nodes.intake import clarify, intake, route_after_intake
from agent.dispatch.nodes.ledger import ledger, rules
from agent.dispatch.nodes.planner import planner, route_after_planner
from agent.dispatch.nodes.reasoning import reasoner, route_after_verifier, verifier
from agent.dispatch.state import DispatchInput, DispatchOutput, DispatchState

GATHERS = ["gather_telemetry", "gather_weather", "gather_sop"]


def build_dispatch_workflow(
    settings: AgentSettings | None = None,
    *,
    resources: DispatchResources | None = None,
    llm: Any | None = None,
    planner_llm: Any | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    clock: Callable[[], datetime] | None = None,
):
    cfg = settings or get_agent_settings()
    res = resources or DispatchResources(cfg, llm=llm, planner_llm=planner_llm)

    builder = StateGraph(
        DispatchState,
        context_schema=DispatchContext,
        input_schema=DispatchInput,
        output_schema=DispatchOutput,
    )
    builder.add_node("intake", intake)
    builder.add_node("clarify", clarify)
    builder.add_node("planner", planner)
    builder.add_node("gather_telemetry", gather_telemetry)
    builder.add_node("gather_weather", gather_weather)
    builder.add_node("gather_sop", gather_sop)
    builder.add_node("ledger", ledger)
    builder.add_node("rules", rules)
    builder.add_node("reasoner", reasoner)
    builder.add_node("verifier", verifier)
    builder.add_node("action_gate", action_gate)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake, ["clarify", "planner", *GATHERS])
    builder.add_conditional_edges("planner", route_after_planner, [*GATHERS, "ledger"])
    for g in GATHERS:
        builder.add_edge(g, "ledger")
    builder.add_edge("ledger", "rules")
    builder.add_edge("rules", "reasoner")
    builder.add_edge("reasoner", "verifier")
    builder.add_conditional_edges("verifier", route_after_verifier, ["reasoner", "action_gate"])
    builder.add_edge("action_gate", "finalize")
    builder.add_edge("clarify", "finalize")
    builder.add_edge("finalize", END)

    graph = builder.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())
    graph.dispatch_context = DispatchContext.build(cfg, res, clock)  # type: ignore[attr-defined]
    graph.dispatch_resources = res  # type: ignore[attr-defined]  # for pre-warming
    return graph
