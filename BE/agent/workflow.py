"""Agentic dispatch workflow: a LangGraph state machine replacing the single ReAct loop.

    intake ─┬─ clarify ────────────────────────────────────────────────┐
            ├─ (fixed fan-out, Send) gather_telemetry ┐                  │
            │                         gather_weather   ├─ ledger ─ rules ─ reasoner ─ verifier ─┬─ action_gate ─ finalize
            │                         gather_sop       ┘                    ▲                   │   (interrupt for
            └─ planner (LLM picks tools, Send) ───────┘                    └── retry once ─────┘    dispatcher approval)

Who decides what: tools report facts, code applies SOP thresholds/freshness/verification, the LLM
judges and explains, and a dispatcher approves high-stakes actions. Nodes emit normalised events
through LangGraph's stream writer; `iter_dispatch_events` turns graph output into the BFF's SSE events.
No import-time side effects: models, engines and clients are built lazily (or injected in tests).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from agent.config import AgentSettings, get_agent_settings
from agent.dispatch.context import DispatchResources
from agent.dispatch.events import emit
from agent.dispatch.nodes.finalize import finalize, render_final
from agent.dispatch.prompts import clarify_text, planner_prompt, reasoner_prompt
from agent.dispatch.state import DispatchState
from agent.dispatch.streaming import collect_final, iter_dispatch_events, pending_approval
from agent.dispatch.text import history, plain, text_of
from agent.evidence import render_ledger, sop_records, telemetry_records, utc_now, weather_records
from agent.intent import SCAN_SOP_QUERY, Intent, build_scan_sql, classify_intent
from agent.rules import ACTION_LABELS, evaluate_rules
from agent.verifier import MAX_ATTEMPTS, verify_draft

__all__ = [
    "DispatchResources",
    "DispatchState",
    "build_dispatch_workflow",
    "collect_final",
    "iter_dispatch_events",
    "pending_approval",
    "render_final",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
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
    now_fn = clock or utc_now
    reasoner_system = reasoner_prompt()

    # -- intake & routing --------------------------------------------------
    def intake(state: DispatchState) -> dict:
        human = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        question = text_of(human[-1].content) if human else ""
        intent = classify_intent(question)
        emit({"type": "status", "phase": "routing", "label": f"Route: {intent.kind}", "intent": intent.kind})
        return {
            "question": question,
            "intent": intent.to_state(),
            "planned_calls": [],
            "gathered": None,
            "ledger": [],
            "gaps": [],
            "triggers": [],
            "historical": [],
            "draft": "",
            "attempts": 0,
            "verification": None,
            "approval": None,
        }

    def route_after_intake(state: DispatchState):
        intent = Intent(**state["intent"])
        if intent.kind == "clarify":
            return "clarify"
        if intent.kind == "open":
            return "planner"
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

    def clarify(state: DispatchState) -> dict:
        return {"draft": clarify_text(), "verification": {"passed": True, "skipped": True, "problems": [], "proposed_actions": []}}

    def planner(state: DispatchState) -> dict:
        msgs = [SystemMessage(planner_prompt()), *history(state), HumanMessage(state["question"])]
        ai = res.planner_llm().invoke(msgs)
        calls = [{"name": tc["name"], "args": dict(tc.get("args") or {})} for tc in (getattr(ai, "tool_calls", None) or [])]
        return {"planned_calls": calls}

    def route_after_planner(state: DispatchState):
        sends = []
        for call in state.get("planned_calls", []):
            args = call["args"]
            if call["name"] == "query_telemetry_db":
                sends.append(Send("gather_telemetry", {"sql": str(args.get("sql_query", ""))}))
            elif call["name"] == "fetch_corridor_conditions":
                sends.append(Send("gather_weather", args))
            elif call["name"] == "search_compliance_sop":
                sends.append(Send("gather_sop", {"query": str(args.get("query", ""))}))
        return sends or "ledger"

    # -- gather (run in parallel) ---------------------------------------------
    def gather_telemetry(payload: dict) -> dict:
        from agent.tools.telemetry_sql import UnsafeSqlError, fetch_telemetry_rows

        sql = payload.get("sql", "")
        emit({"type": "tool_input", "name": "query_telemetry_db", "args": {"sql_query": sql}})
        result: dict = {"source": "telemetry", "columns": [], "rows": [], "error": None}
        try:
            columns, rows = fetch_telemetry_rows(res.engine(), sql, max_rows=cfg.sql_max_rows)
            result["columns"] = columns
            result["rows"] = [[plain(v) for v in row] for row in rows]
            text = f"COLUMNS: {', '.join(columns)}\n" + "\n".join(str(tuple(r)) for r in result["rows"]) if rows else "No records matched the query criteria."
        except UnsafeSqlError as exc:
            result["error"] = str(exc)
            text = str(exc)
        except Exception as exc:
            logger.warning("Telemetry gather failed: %s", type(exc).__name__)
            result["error"] = f"telemetry database unavailable ({type(exc).__name__})"
            text = f"Database Error: {result['error']}"
        emit({"type": "tool_output", "name": "query_telemetry_db", "content": text})
        return {"gathered": [result]}

    def gather_weather(payload: dict) -> dict:
        from agent.tools.corridor_api import CorridorRequestError, fetch_corridor_observations, format_corridor_observations

        args = {
            "latitude": float(payload.get("latitude", 0.0)),
            "longitude": float(payload.get("longitude", 0.0)),
            "past_days": int(payload.get("past_days", 0) or 0),
            "forecast_days": int(payload.get("forecast_days", 0) or 0),
        }
        emit({"type": "tool_input", "name": "fetch_corridor_conditions", "args": args})
        result: dict = {"source": "weather", "observations": None, "error": None}
        try:
            obs = fetch_corridor_observations(
                args["latitude"], args["longitude"], client=res.http(), api_url=cfg.corridor_api_url,
                past_days=args["past_days"], forecast_days=args["forecast_days"],
            )
            result["observations"] = obs
            text = format_corridor_observations(obs, past_days=args["past_days"], forecast_days=args["forecast_days"])
        except CorridorRequestError as exc:
            result["error"] = f"weather request invalid: {exc}"
            text = f"Corridor API Validation Error: {exc}"
        except Exception as exc:
            logger.warning("Weather gather failed: %s", type(exc).__name__)
            result["error"] = f"weather service unavailable ({type(exc).__name__})"
            text = f"Corridor API Communication Failure: {type(exc).__name__}"
        emit({"type": "tool_output", "name": "fetch_corridor_conditions", "content": text})
        return {"gathered": [result]}

    def gather_sop(payload: dict) -> dict:
        from agent.tools.sop_rag import format_inprocess_results
        from rag.retrieval.client import NO_MATCH_MESSAGE, QUERY_REJECTED_MESSAGE, UNAVAILABLE_MESSAGE

        query = payload.get("query", "")
        emit({"type": "tool_input", "name": "search_compliance_sop", "args": {"query": query}})
        result: dict = {"source": "sop", "chunks": [], "error": None}
        try:
            chunks = res.retrieval().retrieve(query, top_k=cfg.rag_top_k)
            result["chunks"] = [
                {"source_file": c.source_file, "text": c.text, "score": float(c.score), "metadata": dict(c.metadata or {})}
                for c in chunks
            ]
            text = format_inprocess_results(chunks) if chunks else NO_MATCH_MESSAGE
            if not chunks:
                result["error"] = "no matching SOP section"
        except ValueError:
            result["error"] = "SOP query rejected"
            text = QUERY_REJECTED_MESSAGE
        except Exception as exc:
            logger.warning("SOP gather failed: %s", type(exc).__name__)
            result["error"] = "SOP knowledge base unavailable"
            text = UNAVAILABLE_MESSAGE
        emit({"type": "tool_output", "name": "search_compliance_sop", "content": text})
        return {"gathered": [result]}

    # -- ledger, rules -----------------------------------------------------
    def ledger(state: DispatchState) -> dict:
        now = now_fn()
        window = cfg.evidence_freshness_hours
        records: list[dict] = []
        gaps: list[str] = []
        order = {"telemetry": 0, "weather": 1, "sop": 2}
        counts = {"telemetry": 0, "weather": 0, "sop": 0}
        for item in sorted(state.get("gathered") or [], key=lambda g: order.get(g["source"], 9)):
            if item.get("error"):
                gaps.append(f"{item['source']}: {item['error']}")
                continue
            if item["source"] == "telemetry":
                new = telemetry_records(item["columns"], item["rows"], now=now, window_hours=window, start=counts["telemetry"] + 1)
            elif item["source"] == "weather":
                new = weather_records(item["observations"], now=now, window_hours=window, start=counts["weather"] + 1)
            else:
                new = sop_records([SimpleNamespace(**c) for c in item["chunks"]], start=counts["sop"] + 1)
            counts[item["source"]] += len(new)
            records.extend(new)
        return {"ledger": records, "gaps": gaps, "as_of": now.isoformat()}

    def rules(state: DispatchState) -> dict:
        triggers, historical = evaluate_rules(state.get("ledger", []))
        ledger_records = state.get("ledger", [])
        emit(
            {
                "type": "evidence",
                "records": len(ledger_records),
                "fresh": sum(1 for r in ledger_records if r["freshness"] == "fresh"),
                "historical": sum(1 for r in ledger_records if r["source"] == "telemetry" and r["freshness"] != "fresh"),
                "triggers": [f"{t['rule']} [{t['evidence']}]" for t in triggers],
                "historical_breaches": len(historical),
                "gaps": state.get("gaps", []),
            }
        )
        return {"triggers": triggers, "historical": historical}

    # -- reasoner, verifier ------------------------------------------------
    def reasoner(state: DispatchState) -> dict:
        attempt = int(state.get("attempts", 0)) + 1
        emit({"type": "draft_start", "attempt": attempt})
        now = datetime.fromisoformat(state["as_of"]) if state.get("as_of") else now_fn()
        ledger_text = render_ledger(
            state.get("ledger", []), state.get("triggers", []), state.get("historical", []),
            now=now, window_hours=cfg.evidence_freshness_hours,
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
        msgs = [SystemMessage(reasoner_system), *history(state), HumanMessage(content)]
        ai = res.llm().invoke(msgs)
        return {"draft": text_of(ai.content), "attempts": attempt}

    def verifier(state: DispatchState) -> dict:
        result = verify_draft(
            state.get("draft", ""),
            ledger=state.get("ledger", []),
            triggers=state.get("triggers", []),
            historical=state.get("historical", []),
            question=state.get("question", ""),
            window_hours=cfg.evidence_freshness_hours,
        )
        result["attempt"] = state.get("attempts", 1)
        retrying = not result["passed"] and result["attempt"] < MAX_ATTEMPTS
        emit({"type": "verification", **result, "retrying": retrying})
        return {"verification": result}

    def route_after_verifier(state: DispatchState) -> str:
        v = state.get("verification") or {}
        if not v.get("passed") and state.get("attempts", 0) < MAX_ATTEMPTS:
            return "reasoner"
        return "action_gate"

    # -- human gate, finalize ------------------------------------------------
    def action_gate(state: DispatchState) -> dict:
        v = state.get("verification") or {}
        high = [a for a in v.get("proposed_actions", []) if a["high_stakes"]]
        if not high:
            return {"approval": None}
        reasons = {
            (t["evidence"], action): f"{t['summary']} ({t['sop_ref']})"
            for t in state.get("triggers", [])
            for action in t["actions"]
        }
        request = {
            "actions": [
                {**a, "label": ACTION_LABELS[a["action"]], "trigger": reasons.get((a["evidence"], a["action"]), "")}
                for a in high
            ],
            "verified": bool(v.get("passed")),
        }
        decision = interrupt(request) or {}
        verdict = str(decision.get("decision", "")).lower()
        return {
            "approval": {
                "decision": "approved" if verdict in {"approve", "approved", "yes"} else "rejected",
                "by": str(decision.get("by") or "dispatcher")[:80],
                "note": str(decision.get("note") or "")[:500],
                "at": datetime.now(timezone.utc).isoformat(),
                "actions": high,
            }
        }

    builder = StateGraph(DispatchState)
    for name, fn in (
        ("intake", intake), ("clarify", clarify), ("planner", planner),
        ("gather_telemetry", gather_telemetry), ("gather_weather", gather_weather), ("gather_sop", gather_sop),
        ("ledger", ledger), ("rules", rules), ("reasoner", reasoner), ("verifier", verifier),
        ("action_gate", action_gate), ("finalize", finalize),
    ):
        builder.add_node(name, fn)
    gathers = ["gather_telemetry", "gather_weather", "gather_sop"]
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake, ["clarify", "planner", *gathers])
    builder.add_conditional_edges("planner", route_after_planner, [*gathers, "ledger"])
    for g in gathers:
        builder.add_edge(g, "ledger")
    builder.add_edge("ledger", "rules")
    builder.add_edge("rules", "reasoner")
    builder.add_edge("reasoner", "verifier")
    builder.add_conditional_edges("verifier", route_after_verifier, ["reasoner", "action_gate"])
    builder.add_edge("action_gate", "finalize")
    builder.add_edge("clarify", "finalize")
    builder.add_edge("finalize", END)
    graph = builder.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())
    graph.dispatch_resources = res  # type: ignore[attr-defined]  # for pre-warming
    return graph

