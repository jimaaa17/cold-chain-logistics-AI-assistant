import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

be_root = Path(__file__).resolve().parents[1]
project_root = Path(__file__).resolve().parents[2]
if str(be_root) not in sys.path:
    sys.path.insert(0, str(be_root))

from observability import get_langfuse_bridge, new_span_id, new_trace_id  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(project_root / ".env")
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger("api.server")


# ==========================================
# 1. PURE SSE & AUDIT STREAMING ENGINE
# ==========================================
SIMULATED_AUDIT_PREFIX = "[SIMULATED: AGENT_OFFLINE_DEMO] "
PROMPT_AUDIT_NODE = "user_input"
PROMPT_AUDIT_TOOL = "Dispatcher"


def _sse_frame(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def generate_dispatch_sse_frames(
    session_id: str,
    prompt: str,
    streamer_fn: Callable[[str, str], Iterable[dict[str, Any]]],
    writer_fn: Callable[[str, str, str, str], None],
) -> Iterable[str]:
    """
    Pure generator that transforms LangGraph stream updates into SSE frames
    and writes each execution trace to FDE_VIEWS.AgentAuditLog via writer_fn.
    """
    start_ts = time.perf_counter()
    trace_id = new_trace_id(session_id)
    bridge = get_langfuse_bridge()
    yield _sse_frame(
        "status",
        {
            "phase": "initializing",
            "label": "🧠 Initializing Core Reasoner Node...",
            "session_id": session_id,
            "trace_id": trace_id,
        },
    )
    final_emitted = False
    simulated_announced = False
    try:
        # The operator's prompt is the first audit row of every turn (as in AgentOrchestrator.stream).
        writer_fn(session_id, PROMPT_AUDIT_NODE, PROMPT_AUDIT_TOOL, prompt)
        bridge.record_span(
            session_id,
            PROMPT_AUDIT_NODE,
            PROMPT_AUDIT_TOOL,
            prompt,
            trace_id=trace_id,
            span_id=new_span_id(),
            elapsed_ms=0,
        )
        for step in streamer_fn(session_id, prompt):
            elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
            step_type = step.get("type")
            if step_type not in ("tool_input", "tool_output", "final_answer"):
                continue
            span_id = new_span_id()
            simulated = bool(step.get("simulated"))

            if simulated and not simulated_announced:
                simulated_announced = True
                yield _sse_frame(
                    "status",
                    {
                        "phase": "simulated",
                        "label": "⚠️ OFFLINE DEMO: simulated data, not live telemetry",
                        "simulated": True,
                        "session_id": session_id,
                        "trace_id": trace_id,
                    },
                )

            if step_type == "tool_input":
                node, tool_name = "reasoner", step["name"]
                audit_content = json.dumps(step.get("args", {}))
                payload = {"node": node, "name": tool_name, "args": step.get("args", {})}
            elif step_type == "tool_output":
                node, tool_name = "tools", step["name"]
                audit_content = step.get("content", "")
                payload = {"node": node, "name": tool_name, "content": audit_content}
            else:
                final_emitted = True
                node, tool_name = "reasoner_final", "LLM Text Synthesis"
                audit_content = step.get("content", "")
                payload = {"node": node, "content": audit_content}

            if simulated:
                audit_content = SIMULATED_AUDIT_PREFIX + audit_content
            writer_fn(session_id, node, tool_name, audit_content)
            bridge.record_span(
                session_id,
                node,
                tool_name,
                audit_content,
                trace_id=trace_id,
                span_id=span_id,
                elapsed_ms=elapsed_ms,
            )
            payload.update(
                {
                    "elapsed_ms": elapsed_ms,
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "simulated": simulated,
                }
            )
            yield _sse_frame(step_type, payload)

        if not final_emitted:
            yield _sse_frame(
                "error",
                {
                    "message": "⚠️ Execution Timeout: System engine encountered an unresolved processing edge case.",
                    "trace_id": trace_id,
                },
            )
    except Exception as exc:
        yield _sse_frame(
            "error",
            {"message": f"Orchestrator Runtime Error: {str(exc)}", "trace_id": trace_id},
        )
    finally:
        total_ms = int((time.perf_counter() - start_ts) * 1000)
        yield _sse_frame(
            "done",
            {
                "label": "Incident Matrix Evaluation Complete",
                "elapsed_ms": total_ms,
                "trace_id": trace_id,
                "simulated": simulated_announced,
            },
        )


# ==========================================
# 2. DEFAULT DATABASE & ORCHESTRATOR ADAPTERS
# ==========================================
_AUDIT_LOCK = threading.Lock()
_AUDIT_EXPORTER: Any = None
_ADMIN_ENGINES: dict[str, Any] = {}


def _get_audit_exporter():
    """SQL audit sink built once from AgentSettings: same host/port/database/driver as the
    telemetry tool, written as the agent's INSERT-only role. Not cached on failure."""
    global _AUDIT_EXPORTER
    if _AUDIT_EXPORTER is not None:
        return _AUDIT_EXPORTER
    with _AUDIT_LOCK:
        if _AUDIT_EXPORTER is None:
            from agent.config import get_agent_settings
            from observability import SqlAuditLedgerExporter

            settings = get_agent_settings()
            engine = None
            if settings.audit_enabled:
                from agent.tools.telemetry_sql import create_telemetry_engine

                engine = create_telemetry_engine(settings)
            _AUDIT_EXPORTER = SqlAuditLedgerExporter(
                engine=engine, enabled=settings.audit_enabled, table_name=settings.audit_table
            )
    return _AUDIT_EXPORTER


def default_audit_writer(session_id: str, node_name: str, tool_name: str, content: str) -> None:
    """Writes one row to the audit ledger. Never raises: a failed write is logged, not fatal."""
    try:
        _get_audit_exporter().export_span(  # logs its own warning when the INSERT fails
            session_id=session_id,
            trace_id="",
            span_id="",
            node_executed=node_name,
            tool_name=tool_name,
            content=content,
        )
    except Exception as exc:
        logger.warning("Audit ledger unavailable, row not written: %s", _describe_config_error(exc))


def _admin_engine(username: str, password: str):
    """Engine for the audit reader: configured server/database, admin credentials from the request.
    Only called after the credentials matched SQL_ADMIN_USER / SQL_ADMIN_PASSWORD."""
    if username not in _ADMIN_ENGINES:
        from pydantic import SecretStr

        from agent.config import get_agent_settings
        from agent.tools.telemetry_sql import create_telemetry_engine

        admin_settings = get_agent_settings().model_copy(
            update={"sql_agent_user": username, "sql_agent_password": SecretStr(password)}
        )
        _ADMIN_ENGINES[username] = create_telemetry_engine(admin_settings)
    return _ADMIN_ENGINES[username]


def default_audit_reader(
    username: str,
    password: str,
    session_id_filter: Optional[str] = None,
    node_filter: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Authenticates admin credentials and returns audit rows from FDE_VIEWS.AgentAuditLog."""
    from sqlalchemy import text

    expected_user = os.getenv("SQL_ADMIN_USER")
    expected_pass = os.getenv("SQL_ADMIN_PASSWORD")
    if not expected_user or username != expected_user or password != expected_pass:
        raise PermissionError("Invalid Administrator Credentials.")

    try:
        from agent.config import get_agent_settings

        audit_table = get_agent_settings().audit_table
        engine = _admin_engine(username, password)
        clauses = []
        params: dict[str, Any] = {"limit": int(limit)}
        if session_id_filter:
            clauses.append("SessionID = :session_id")
            params["session_id"] = session_id_filter
        if node_filter and node_filter != "ALL":
            clauses.append("NodeExecuted = :node_name")
            params["node_name"] = node_filter

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = text(
            f"""
            SELECT TOP (:limit) LogID, Timestamp, SessionID, NodeExecuted, ToolName, Content
            FROM {audit_table}
            {where_sql}
            ORDER BY Timestamp DESC
            """
        )
        with engine.connect() as conn:
            result = conn.execute(query, params)
            keys = list(result.keys())
            rows = []
            for row in result.fetchall():
                item = dict(zip(keys, row))
                if item.get("Timestamp") is not None and not isinstance(item["Timestamp"], str):
                    item["Timestamp"] = item["Timestamp"].isoformat()
                rows.append(item)
            return rows
    except Exception as exc:
        # MSSQL unreachable (typically local dev): serve the in-memory span buffer instead.
        logger.warning(
            "Audit reader: SQL ledger unavailable, serving in-memory spans instead: %s",
            _describe_config_error(exc),
        )
        return get_langfuse_bridge().query_spans(
            session_id_filter=session_id_filter,
            node_filter=node_filter,
            limit=limit,
        )


_ACTIVE_GRAPH: Any = None


class AgentConfigError(RuntimeError):
    """The LangGraph agent could not be built from the current configuration."""


_SECRET_ENV_MARKERS = ("KEY", "PASSWORD", "PWD", "SECRET", "TOKEN")
_CONN_STR_SECRET = re.compile(r"(?i)\b(pwd|password)=[^;\s]*")
_MAX_ERROR_DETAIL = 300


def _redact_secrets(message: str) -> str:
    """Masks secret-looking env values and connection-string passwords in an error message."""
    for name, value in os.environ.items():
        if value and len(value) >= 4 and any(m in name.upper() for m in _SECRET_ENV_MARKERS):
            message = message.replace(value, "***")
    return _CONN_STR_SECRET.sub(lambda m: f"{m.group(1)}=***", message)


def _describe_config_error(exc: Exception) -> str:
    """Operator-safe summary of a build failure: field/reason only, secrets redacted, length capped."""
    errors = getattr(exc, "errors", None)
    if callable(errors):  # pydantic ValidationError: report field + reason only, never the input
        try:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', ())) or 'settings'}: {err.get('msg', '')}"
                for err in errors()
            )
            summary = f"Invalid agent settings ({details})"
        except Exception:  # pragma: no cover
            summary = "Invalid agent settings"
    elif isinstance(exc, ImportError):
        summary = f"Missing dependency for the configured provider: {exc.name or exc}"
    else:
        summary = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    summary = _redact_secrets(summary)
    if len(summary) > _MAX_ERROR_DETAIL:
        summary = summary[: _MAX_ERROR_DETAIL - 3] + "..."
    return summary


def _as_config_error(exc: Exception) -> "AgentConfigError":
    if isinstance(exc, AgentConfigError):
        return exc
    return AgentConfigError(f"Agent configuration error: {_describe_config_error(exc)}")


def _reset_settings_cache() -> None:
    """Drops cached AgentSettings so the next build re-reads `.env` (lru_cache would pin stale values)."""
    try:
        from agent.config import get_agent_settings

        get_agent_settings.cache_clear()
    except Exception:  # pragma: no cover - agent.config itself failed to import
        pass


_GRAPH_LOCK = threading.Lock()


def _get_compiled_graph():
    """Lazily builds and caches the LangGraph ReAct state machine (thread-safe, built once).

    On failure nothing is cached and the settings cache is cleared, so the next request retries
    the build. Values added to `.env` are then picked up; values already exported into the process
    environment (by `load_dotenv` at startup or by systemd) win over `.env` and need a restart.
    Raises AgentConfigError with an operator-safe message; full details go to the server log only.
    """
    global _ACTIVE_GRAPH
    if _ACTIVE_GRAPH is not None:
        return _ACTIVE_GRAPH
    with _GRAPH_LOCK:
        if _ACTIVE_GRAPH is not None:  # another thread finished the build while we waited
            return _ACTIVE_GRAPH
        try:
            from agent.graph import build_agent_graph

            graph = build_agent_graph()
        except Exception as exc:
            _reset_settings_cache()
            err = _as_config_error(exc)
            logger.error("Agent graph build failed: %s", err)
            logger.debug("Agent graph build traceback", exc_info=True)
            raise err from exc
        _ACTIVE_GRAPH = graph
        return graph


_PRODUCTION_ENVS = {"prod", "production"}


def _is_production() -> bool:
    return os.getenv("APP_ENV", "").strip().lower() in _PRODUCTION_ENVS


def _offline_demo_enabled() -> bool:
    """AGENT_OFFLINE_DEMO opt-in for UI work without keys. Never honoured when APP_ENV=production."""
    if os.getenv("AGENT_OFFLINE_DEMO", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    if _is_production():
        logger.error("AGENT_OFFLINE_DEMO is set but ignored because APP_ENV=production.")
        return False
    return True


def _offline_fallback_streamer(session_id: str, prompt: str) -> Iterable[dict[str, Any]]:
    """Canned control-tower demo for UI work without credentials (AGENT_OFFLINE_DEMO=1 only).

    Every event carries `simulated: True`; `generate_dispatch_sse_frames` propagates that flag to
    the SSE frames and marks the audit/trace content so fabricated rows never pass as live data.
    """
    time.sleep(0.12)
    yield {
        "type": "tool_input",
        "simulated": True,
        "node": "reasoner",
        "name": "query_telemetry_db",
        "args": {
            "sql_query": (
                "SELECT TOP 5 Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code, "
                "Risk_Classification, Delay_Probability, Port_Congestion_Level "
                "FROM FDE_VIEWS.VW_ACTIVE_FLEET WHERE Current_Temperature_C > 4.0 OR Port_Congestion_Level > 7.0 "
                "ORDER BY Current_Temperature_C DESC"
            )
        },
    }
    time.sleep(0.15)
    yield {
        "type": "tool_output",
        "simulated": True,
        "node": "tools",
        "name": "query_telemetry_db",
        "content": (
            "COLUMNS: Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code, "
            "Risk_Classification, Delay_Probability, Port_Congestion_Level\n"
            "('2026-11-14T23:35:00Z', 33.74, -118.27, 6.8, 'BREACH', 'High Risk', 0.81, 8.4)\n"
            "('2026-11-14T23:32:00Z', 33.77, -118.19, 4.9, 'ALERT', 'High Risk', 0.72, 7.6)"
        ),
    }
    time.sleep(0.12)
    yield {
        "type": "tool_input",
        "simulated": True,
        "node": "reasoner",
        "name": "search_compliance_sop",
        "args": {"query": "IOT_TEMP_VAL_C > 4.0 PRT_CNG_LVL > 7.0 DELAY_PROB_DEC > 0.65 mitigation SOP"},
    }
    time.sleep(0.15)
    yield {
        "type": "tool_output",
        "simulated": True,
        "node": "tools",
        "name": "search_compliance_sop",
        "content": (
            "[Source: Cold_Chain_Incident_SOP_v2.md | Format: MD]\n"
            "Section 3.2 Critical Excursion (IOT_TEMP_VAL_C > 4.0°C): Immediately engage auxiliary reefer cooling "
            "and divert to nearest certified cold-storage staging facility within 45 minutes when PRT_CNG_LVL > 7.0 "
            "and DELAY_PROB_DEC > 0.65."
        ),
    }
    time.sleep(0.12)
    yield {
        "type": "final_answer",
        "simulated": True,
        "node": "reasoner_final",
        "name": "LLM Text Synthesis",
        "content": (
            "🚨 **Operational Resolution Report (Cited: Cold_Chain_Incident_SOP_v2.md §3.2)**\n\n"
            "1. **Critical Thermal Breach Detected**: Unit at GPS `(33.74, -118.27)` reports `Current_Temperature_C = 6.8°C` "
            "(SOP threshold `IOT_TEMP_VAL_C > 4.0`) with `Port_Congestion_Level = 8.4` (`PRT_CNG_LVL > 7.0`) "
            "and `Delay_Probability = 0.81` (`DELAY_PROB_DEC > 0.65`).\n"
            "2. **Mandatory SOP Mitigation**: Engage secondary compressor override immediately and reroute to the "
            "San Pedro Cold-Storage Staging Hub within 45 minutes.\n"
            "3. **Audit Status**: Execution spans logged to `FDE_VIEWS.AgentAuditLog` (`USR_FDE_RO`) and `Langfuse`."
        ),
    }


def default_agent_streamer(session_id: str, prompt: str) -> Iterable[dict[str, Any]]:
    """Streams normalized trace events from the compiled LangGraph agent.

    Config and runtime errors propagate so `generate_dispatch_sse_frames` emits an SSE `error`
    frame. The canned demo stream is only used when the graph can't be built AND AGENT_OFFLINE_DEMO
    is set outside production (UI work without keys).
    """
    try:
        from langchain_core.messages import HumanMessage, ToolMessage
        from agent.graph import format_message_content

        graph = _get_compiled_graph()
    except Exception as exc:
        err = _as_config_error(exc)
        if _offline_demo_enabled():
            logger.warning("Agent unavailable, serving SIMULATED offline demo stream: %s", err)
            yield from _offline_fallback_streamer(session_id, prompt)
            return
        if err is exc:
            raise
        raise err from exc

    thread_config = {"configurable": {"thread_id": session_id}}
    events = graph.stream(
        {"messages": [HumanMessage(content=prompt)]},
        config=thread_config,
        stream_mode="updates",
    )

    for event in events:
        for node_name, node_state in event.items():
            if node_name == "reasoner":
                latest_msg = node_state["messages"][-1]
                tool_calls = getattr(latest_msg, "tool_calls", None) or []
                if tool_calls:
                    for tc in tool_calls:
                        yield {
                            "type": "tool_input",
                            "node": "reasoner",
                            "name": tc["name"],
                            "args": tc["args"],
                        }
                formatted_text = format_message_content(getattr(latest_msg, "content", ""))
                if formatted_text and not tool_calls:
                    yield {
                        "type": "final_answer",
                        "node": "reasoner_final",
                        "name": "LLM Text Synthesis",
                        "content": formatted_text,
                    }
            elif node_name == "tools":
                for msg in node_state.get("messages", []):
                    if isinstance(msg, ToolMessage) or getattr(msg, "type", "") == "tool":
                        yield {
                            "type": "tool_output",
                            "node": "tools",
                            "name": getattr(msg, "name", "tool"),
                            "content": str(getattr(msg, "content", "")),
                        }


def default_session_purger(session_id: str) -> bool:
    """Deletes the conversation memory (LangGraph checkpoint) for `session_id` in this process.

    Returns True when no memory for that session remains afterwards. The checkpointer is the
    in-process MemorySaver, so with several workers each holds its own copy (see onboarding Q4).
    """
    graph = _ACTIVE_GRAPH
    if graph is None:  # graph never built in this process, so it holds no memory to delete
        return True
    checkpointer = getattr(graph, "checkpointer", None)
    if checkpointer is None or not hasattr(checkpointer, "delete_thread"):
        logger.warning("Session purge: checkpointer cannot delete threads; memory for the session is kept.")
        return False
    checkpointer.delete_thread(session_id)
    return True


def build_purge_payload(
    old_session_id: Optional[str], purger: Callable[[str], bool] = default_session_purger
) -> dict[str, Any]:
    """`/api/v1/session/purge` for both servers: clears the old thread's memory, mints a new id."""
    purged = True
    if old_session_id:
        try:
            purged = bool(purger(old_session_id))
        except Exception as exc:
            logger.warning("Session purge failed: %s", _describe_config_error(exc))
            purged = False
    return {"purged": purged, "previous_session_id": old_session_id, "session_id": str(uuid.uuid4())}


META_CAPABILITIES = {
    "telemetry_db": ["past_history", "present_snapshot", "future_risk_predictions"],
    "weather_corridor": ["past_days", "current_weather", "forecast_days"],
    "sop_rag": ["post_incident_audit", "active_thresholds", "preventive_escalation"],
}


def build_meta_payload() -> dict[str, Any]:
    """`/api/v1/meta` for both servers. Reads the same settings objects the agent and RAG use, so
    the reported LLM and embeddings provider can't drift from what actually runs."""
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "fde-dispatch-bff",
        "agent_llm": None,
        "agent_model": None,
        "embeddings_model": None,
        "observability": get_langfuse_bridge().status(),
        "capabilities": META_CAPABILITIES,
    }
    config_errors: list[str] = []
    try:
        from agent.config import get_agent_settings

        agent_cfg = get_agent_settings()
        payload["agent_llm"] = agent_cfg.agent_llm
        payload["agent_model"] = {
            "GEMINI": agent_cfg.gemini_model,
            "OPENAI": agent_cfg.openai_model,
            "OLLAMA": agent_cfg.ollama_model,
        }.get(agent_cfg.agent_llm)
    except Exception as exc:
        config_errors.append(f"agent: {_describe_config_error(exc)}")
    try:
        from rag.config import get_settings as get_rag_settings

        payload["embeddings_model"] = get_rag_settings().embeddings_provider
    except Exception as exc:
        config_errors.append(f"rag: {_describe_config_error(exc)}")
    if config_errors:
        payload["config_errors"] = config_errors
    return payload


# ==========================================
# 3. FASTAPI APPLICATION FACTORY
# ==========================================
def create_app(
    agent_streamer: Optional[Callable[[str, str], Iterable[dict[str, Any]]]] = None,
    audit_writer: Optional[Callable[[str, str, str, str], None]] = None,
    audit_reader: Optional[Callable[..., list[dict[str, Any]]]] = None,
    session_purger: Optional[Callable[[str], bool]] = None,
):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel, Field

    class DispatchStreamRequest(BaseModel):
        session_id: str = Field(..., min_length=1, description="Active operator thread_id")
        prompt: str = Field(..., min_length=1, description="Natural-language operator query")

    class SessionPurgeRequest(BaseModel):
        old_session_id: Optional[str] = None

    class AuditQueryRequest(BaseModel):
        username: str = Field(..., min_length=1)
        password: str = Field(..., min_length=1)
        session_id_filter: Optional[str] = None
        node_filter: Optional[str] = None
        limit: int = Field(default=200, ge=1, le=1000)

    streamer_fn = agent_streamer or default_agent_streamer
    writer_fn = audit_writer or default_audit_writer
    reader_fn = audit_reader or default_audit_reader
    purger_fn = session_purger or default_session_purger

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = time.time()
        yield

    app = FastAPI(
        title="FDE Cold-Chain Dispatch BFF API",
        version="2.0.0",
        description="Streaming SSE & Audit Gateway for the FE/ JavaScript Control Tower.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "fde-dispatch-bff"}

    @app.get("/api/v1/meta")
    def get_meta():
        return build_meta_payload()

    @app.get("/api/v1/traces")
    def get_live_traces(
        session_id: Optional[str] = None,
        node: Optional[str] = None,
        limit: int = 200,
    ):
        """Returns Langfuse-style hierarchical trace spans from the dual-sink traceability buffer."""
        bridge = get_langfuse_bridge()
        spans = bridge.query_spans(
            session_id_filter=session_id,
            node_filter=node,
            limit=max(1, min(limit, 1000)),
        )
        return {
            "status": "ok",
            "observability": bridge.status(),
            "count": len(spans),
            "spans": spans,
        }

    @app.post("/api/v1/session/purge")
    def purge_session(req: SessionPurgeRequest):
        return build_purge_payload(req.old_session_id, purger_fn)

    @app.post("/api/v1/dispatch/stream")
    def stream_dispatch(req: DispatchStreamRequest):
        return StreamingResponse(
            generate_dispatch_sse_frames(req.session_id, req.prompt, streamer_fn, writer_fn),
            media_type="text/event-stream",
        )

    @app.post("/api/v1/audit/logs")
    def query_audit_logs(req: AuditQueryRequest):
        try:
            rows = reader_fn(
                username=req.username,
                password=req.password,
                session_id_filter=req.session_id_filter,
                node_filter=req.node_filter,
                limit=req.limit,
            )
            return {"authenticated": True, "count": len(rows), "rows": rows}
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Database Query Failed: {exc}") from exc

    fe_dist = project_root / "FE" / "dist"
    if (fe_dist / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(fe_dist), html=True), name="fe_static")

    return app


def get_app():
    return create_app()


try:
    app = get_app()
except ImportError:  # pragma: no cover
    app = None


def make_stdlib_server(host: str = "127.0.0.1", port: int = 8000):
    """Builds (does not start) the zero-dependency HTTP/SSE server. Port 0 picks a free port."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    fe_dist = project_root / "FE" / "dist" / "index.html"

    class BffHandler(BaseHTTPRequestHandler):
        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                return self._send_json(200, {"status": "ok", "service": "fde-dispatch-bff"})
            if parsed.path == "/api/v1/meta":
                return self._send_json(200, build_meta_payload())
            if parsed.path == "/api/v1/traces":
                qs = parse_qs(parsed.query)
                session_id = qs.get("session_id", [None])[0]
                node = qs.get("node", [None])[0]
                limit = int(qs.get("limit", ["200"])[0])
                bridge = get_langfuse_bridge()
                spans = bridge.query_spans(
                    session_id_filter=session_id,
                    node_filter=node,
                    limit=max(1, min(limit, 1000)),
                )
                return self._send_json(
                    200,
                    {
                        "status": "ok",
                        "observability": bridge.status(),
                        "count": len(spans),
                        "spans": spans,
                    },
                )
            if parsed.path in ("/", "/index.html") and fe_dist.exists():
                html_bytes = fe_dist.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return
            self._send_json(404, {"detail": "Not Found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            body = self._read_json()

            if parsed.path == "/api/v1/session/purge":
                return self._send_json(200, build_purge_payload(body.get("old_session_id")))

            if parsed.path == "/api/v1/dispatch/stream":
                session_id = str(body.get("session_id") or "default-session")
                prompt = str(body.get("prompt") or "")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for frame in generate_dispatch_sse_frames(
                    session_id, prompt, default_agent_streamer, default_audit_writer
                ):
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                return

            if parsed.path == "/api/v1/audit/logs":
                try:
                    rows = default_audit_reader(
                        username=str(body.get("username") or ""),
                        password=str(body.get("password") or ""),
                        session_id_filter=body.get("session_id_filter"),
                        node_filter=body.get("node_filter"),
                        limit=int(body.get("limit") or 200),
                    )
                    return self._send_json(
                        200, {"authenticated": True, "count": len(rows), "rows": rows}
                    )
                except PermissionError as exc:
                    return self._send_json(401, {"detail": str(exc)})
                except Exception as exc:
                    return self._send_json(500, {"detail": f"Database Query Failed: {exc}"})

            self._send_json(404, {"detail": "Not Found"})

    return ThreadingHTTPServer((host, port), BffHandler)


def run_stdlib_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Zero-dependency HTTP/SSE server for running FE/ + BFF endpoints in any Python environment."""
    server = make_stdlib_server(host, port)
    print(f"🚀 FDE Control Tower (FE + BFF) listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    run_stdlib_server(host="127.0.0.1", port=port)
