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
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Callable, Iterable, Iterator, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, Send, interrupt

from agent.config import AgentSettings, get_agent_settings
from agent.evidence import render_ledger, sop_records, telemetry_records, utc_now, weather_records
from agent.intent import SCAN_SOP_QUERY, Intent, build_scan_sql, classify_intent
from agent.rules import ACTION_LABELS, evaluate_rules
from agent.verifier import ACTION_TAG, MAX_ATTEMPTS, verify_draft

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
CLARIFY_TEXT = (
    "I can answer questions about refrigerated shipments, port congestion and SOP compliance. "
    "For example: \"Which shipments are above 4 °C near Long Beach?\" or "
    "\"What does the SOP require when port congestion is high?\""
)
PLANNER_PROMPT = (
    "You plan data retrieval for a cold-chain control tower. Call the tools needed to answer the "
    "question, in parallel where possible. Do not answer the question yourself. query_telemetry_db "
    "takes one T-SQL SELECT against FDE_VIEWS.VW_ACTIVE_FLEET (columns: Timestamp, Latitude, Longitude, "
    "Current_Temperature_C, Cargo_Condition_Code, Risk_Classification, Delay_Probability, "
    "Port_Congestion_Level, Route_Risk_Index); always include Timestamp."
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _collect(old: list | None, new: list | None) -> list:
    """Fan-in reducer for parallel gather nodes; `None` resets it at the start of each turn."""
    if new is None:
        return []
    return (old or []) + list(new)


class DispatchState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    question: str
    intent: dict
    planned_calls: list[dict]
    gathered: Annotated[list[dict], _collect]
    ledger: list[dict]
    gaps: list[str]
    triggers: list[dict]
    historical: list[dict]
    as_of: str
    draft: str
    attempts: int
    verification: dict | None
    approval: dict | None
    final: str


# ---------------------------------------------------------------------------
# Shared resources (built once, pre-warmed at server startup)
# ---------------------------------------------------------------------------
class DispatchResources:
    """Lazily built, thread-safe handles shared by every run of the workflow."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        llm: Any | None = None,
        planner_llm: Any | None = None,
        engine: Any | None = None,
        http_client: Any | None = None,
        retrieval_service: Any | None = None,
    ) -> None:
        self.settings = settings
        self._llm = llm
        self._planner_llm = planner_llm
        self._engine = engine
        self._http = http_client
        self._retrieval = retrieval_service
        self._lock = threading.Lock()

    def llm(self):
        with self._lock:
            if self._llm is None:
                from agent.graph import build_reasoner_llm

                self._llm = build_reasoner_llm(self.settings)
            return self._llm

    def planner_llm(self):
        llm = self.llm()
        with self._lock:
            if self._planner_llm is None:
                from agent.graph import build_default_tools

                self._planner_llm = llm.bind_tools(build_default_tools(self.settings))
            return self._planner_llm

    def engine(self):
        with self._lock:
            if self._engine is None:
                from agent.tools.telemetry_sql import create_telemetry_engine

                self._engine = create_telemetry_engine(self.settings)
            return self._engine

    def http(self):
        with self._lock:
            if self._http is None:
                from agent.tools.corridor_api import build_corridor_client

                self._http = build_corridor_client(self.settings)
            return self._http

    def retrieval(self):
        with self._lock:
            if self._retrieval is None:
                from rag.config import get_settings
                from rag.retrieval.service import RetrievalService

                self._retrieval = RetrievalService.from_settings(get_settings())
            return self._retrieval

    def warm(self) -> dict[str, str]:
        """Loads everything the first request would otherwise pay for. Never raises."""
        report: dict[str, str] = {}
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("llm_client", self.llm),
            ("sql_engine", self.engine),
            ("http_client", self.http),
            ("sop_retrieval", lambda: self.retrieval().warm_up()),
        ]
        for name, step in steps:
            try:
                step()
                report[name] = "ready"
            except Exception as exc:  # degraded, not fatal: the tool reports the gap at request time
                report[name] = f"failed: {type(exc).__name__}"
                logger.warning("Pre-warm step %s failed: %s", name, type(exc).__name__)
        return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except Exception:  # outside a streaming run (e.g. graph.invoke) there is no writer
        pass


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _text(content: Any, *, strip: bool = True) -> str:
    if isinstance(content, str):
        return content.strip() if strip else content
    if isinstance(content, list):
        parts = [
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, str) or (isinstance(item, dict) and item.get("type") == "text")
        ]
        joined = "".join(parts)
        return joined.strip() if strip else joined
    return str(content or "")


def _history(state: DispatchState, limit: int = 4) -> list[BaseMessage]:
    msgs = [m for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage))]
    return msgs[:-1][-limit:]  # earlier turns only; the current question is passed with the ledger


def _load_prompt(name: str, fallback: str) -> str:
    try:
        return (PROMPT_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return fallback


def render_final(state: DispatchState) -> str:
    draft = state.get("draft", "")
    verification = state.get("verification") or {}
    approval = state.get("approval")
    permitted = {(a["action"], a["evidence"]): a for a in verification.get("proposed_actions", [])}

    def _action(match) -> str:
        action, evidence = match.group(1), match.group(2)
        label = ACTION_LABELS.get(action, action)
        if (action, evidence) not in permitted:
            return f"**Blocked action** (no SOP trigger on fresh data): {label} [{evidence}]"
        if permitted[(action, evidence)]["high_stakes"]:
            status = "awaiting dispatcher approval"
            if approval:
                status = f"{approval['decision']} by {approval['by']}"
            return f"**Proposed action ({status}):** {label} [{evidence}]"
        return f"**Proposed action:** {label} [{evidence}]"

    body = ACTION_TAG.sub(_action, draft).strip()
    if verification.get("skipped"):
        return body
    footer = ["", "---"]
    if verification.get("passed"):
        footer.append(
            f"**Verification:** passed. {verification.get('numbers_checked', 0)} numbers and every citation "
            "were checked against the evidence ledger."
        )
    else:
        footer.append("**Unverified:** the automatic check still failed after one retry. Treat this answer with caution.")
        footer.extend(f"- {p}" for p in verification.get("problems", []))
    if approval:
        line = f"**Dispatcher decision:** {approval['decision']} by {approval['by']} at {approval['at'][:16].replace('T', ' ')} UTC"
        if approval.get("note"):
            line += f" ({approval['note']})"
        footer.append(line)
        if approval["decision"] == "rejected":
            footer.append("The proposed high-stakes actions must not be executed.")
    return body + "\n" + "\n".join(footer)


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
    reasoner_system = _load_prompt("reasoner_prompt.txt", "Answer only from the evidence ledger and cite ids.")

    # -- intake & routing --------------------------------------------------
    def intake(state: DispatchState) -> dict:
        human = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        question = _text(human[-1].content) if human else ""
        intent = classify_intent(question)
        _emit({"type": "status", "phase": "routing", "label": f"Route: {intent.kind}", "intent": intent.kind})
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
        return {"draft": CLARIFY_TEXT, "verification": {"passed": True, "skipped": True, "problems": [], "proposed_actions": []}}

    def planner(state: DispatchState) -> dict:
        msgs = [SystemMessage(PLANNER_PROMPT), *_history(state), HumanMessage(state["question"])]
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
        _emit({"type": "tool_input", "name": "query_telemetry_db", "args": {"sql_query": sql}})
        result: dict = {"source": "telemetry", "columns": [], "rows": [], "error": None}
        try:
            columns, rows = fetch_telemetry_rows(res.engine(), sql, max_rows=cfg.sql_max_rows)
            result["columns"] = columns
            result["rows"] = [[_plain(v) for v in row] for row in rows]
            text = f"COLUMNS: {', '.join(columns)}\n" + "\n".join(str(tuple(r)) for r in result["rows"]) if rows else "No records matched the query criteria."
        except UnsafeSqlError as exc:
            result["error"] = str(exc)
            text = str(exc)
        except Exception as exc:
            logger.warning("Telemetry gather failed: %s", type(exc).__name__)
            result["error"] = f"telemetry database unavailable ({type(exc).__name__})"
            text = f"Database Error: {result['error']}"
        _emit({"type": "tool_output", "name": "query_telemetry_db", "content": text})
        return {"gathered": [result]}

    def gather_weather(payload: dict) -> dict:
        from agent.tools.corridor_api import CorridorRequestError, fetch_corridor_observations, format_corridor_observations

        args = {
            "latitude": float(payload.get("latitude", 0.0)),
            "longitude": float(payload.get("longitude", 0.0)),
            "past_days": int(payload.get("past_days", 0) or 0),
            "forecast_days": int(payload.get("forecast_days", 0) or 0),
        }
        _emit({"type": "tool_input", "name": "fetch_corridor_conditions", "args": args})
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
        _emit({"type": "tool_output", "name": "fetch_corridor_conditions", "content": text})
        return {"gathered": [result]}

    def gather_sop(payload: dict) -> dict:
        from agent.tools.sop_rag import format_inprocess_results
        from rag.retrieval.client import NO_MATCH_MESSAGE, QUERY_REJECTED_MESSAGE, UNAVAILABLE_MESSAGE

        query = payload.get("query", "")
        _emit({"type": "tool_input", "name": "search_compliance_sop", "args": {"query": query}})
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
        _emit({"type": "tool_output", "name": "search_compliance_sop", "content": text})
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
        _emit(
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
        _emit({"type": "draft_start", "attempt": attempt})
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
        msgs = [SystemMessage(reasoner_system), *_history(state), HumanMessage(content)]
        ai = res.llm().invoke(msgs)
        return {"draft": _text(ai.content), "attempts": attempt}

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
        _emit({"type": "verification", **result, "retrying": retrying})
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

    def finalize(state: DispatchState) -> dict:
        text = render_final(state)
        v = state.get("verification") or {}
        _emit(
            {
                "type": "final_answer",
                "content": text,
                "verified": bool(v.get("passed")) and not v.get("skipped"),
                "approval": state.get("approval"),
            }
        )
        return {"final": text, "messages": [AIMessage(content=text)]}

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


# ---------------------------------------------------------------------------
# Streaming adapter (graph output -> normalised events for the BFF)
# ---------------------------------------------------------------------------
def pending_approval(graph, session_id: str) -> dict | None:
    """The approval request the session is paused on, if any."""
    snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return dict(intr.value)
    return None


def iter_dispatch_events(
    graph,
    *,
    session_id: str,
    prompt: str | None = None,
    resume: dict | None = None,
    callbacks: list | None = None,
) -> Iterator[dict]:
    """Runs (or resumes) one turn and yields normalised events:
    status, tool_input, tool_output, evidence, draft_start, token, verification,
    approval_required, final_answer."""
    config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
    if callbacks:
        config["callbacks"] = callbacks
    payload: Any = Command(resume=resume) if resume is not None else {"messages": [HumanMessage(content=prompt or "")]}
    for mode, chunk in graph.stream(payload, config=config, stream_mode=["custom", "messages", "updates"]):
        if mode == "custom" and isinstance(chunk, dict):
            yield chunk
        elif mode == "messages":
            message, meta = chunk
            if (meta or {}).get("langgraph_node") == "reasoner":
                text = _text(getattr(message, "content", ""), strip=False)
                if text:
                    yield {"type": "token", "text": text}
        elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
            for intr in chunk["__interrupt__"]:
                yield {"type": "approval_required", **dict(intr.value)}


def collect_final(events: Iterable[dict]) -> tuple[str, dict | None]:
    """Drains events; returns (final answer text, pending approval request or None)."""
    final, pending = "", None
    for event in events:
        if event.get("type") == "final_answer":
            final = event.get("content", "")
        elif event.get("type") == "approval_required":
            pending = event
    return final, pending
