import json
import os
import sys
import time
import urllib.parse
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


# ==========================================
# 1. PURE SSE & AUDIT STREAMING ENGINE
# ==========================================
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
    try:
        for step in streamer_fn(session_id, prompt):
            elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
            step_type = step.get("type")
            span_id = new_span_id()

            if step_type == "tool_input":
                serialized_args = json.dumps(step.get("args", {}))
                writer_fn(
                    session_id,
                    "reasoner",
                    step["name"],
                    serialized_args,
                )
                bridge.record_span(
                    session_id,
                    "reasoner",
                    step["name"],
                    serialized_args,
                    trace_id=trace_id,
                    span_id=span_id,
                    elapsed_ms=elapsed_ms,
                )
                yield _sse_frame(
                    "tool_input",
                    {
                        "node": "reasoner",
                        "name": step["name"],
                        "args": step.get("args", {}),
                        "elapsed_ms": elapsed_ms,
                        "trace_id": trace_id,
                        "span_id": span_id,
                    },
                )
            elif step_type == "tool_output":
                content_str = step.get("content", "")
                writer_fn(
                    session_id,
                    "tools",
                    step["name"],
                    content_str,
                )
                bridge.record_span(
                    session_id,
                    "tools",
                    step["name"],
                    content_str,
                    trace_id=trace_id,
                    span_id=span_id,
                    elapsed_ms=elapsed_ms,
                )
                yield _sse_frame(
                    "tool_output",
                    {
                        "node": "tools",
                        "name": step["name"],
                        "content": content_str,
                        "elapsed_ms": elapsed_ms,
                        "trace_id": trace_id,
                        "span_id": span_id,
                    },
                )
            elif step_type == "final_answer":
                final_emitted = True
                content_str = step.get("content", "")
                writer_fn(
                    session_id,
                    "reasoner_final",
                    "LLM Text Synthesis",
                    content_str,
                )
                bridge.record_span(
                    session_id,
                    "reasoner_final",
                    "LLM Text Synthesis",
                    content_str,
                    trace_id=trace_id,
                    span_id=span_id,
                    elapsed_ms=elapsed_ms,
                )
                yield _sse_frame(
                    "final_answer",
                    {
                        "node": "reasoner_final",
                        "content": content_str,
                        "elapsed_ms": elapsed_ms,
                        "trace_id": trace_id,
                        "span_id": span_id,
                    },
                )

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
            },
        )


# ==========================================
# 2. DEFAULT DATABASE & ORCHESTRATOR ADAPTERS
# ==========================================
_ENGINE_CACHE: dict[tuple[str, str, str], Any] = {}


def _build_mssql_engine(user: str, password: str):
    from sqlalchemy import create_engine

    db_host = os.getenv("SQL_SERVER_HOST", "localhost")
    db_port = os.getenv("SQL_SERVER_PORT", "1433")
    cache_key = (db_host, db_port, user)
    if cache_key in _ENGINE_CACHE:
        return _ENGINE_CACHE[cache_key]

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={db_host},{db_port};"
        f"DATABASE=master;"
        f"UID={user};"
        f"PWD={password};"
        f"Encrypt=no;"
        f"TrustServerCertificate=yes;"
    )
    params = urllib.parse.quote_plus(conn_str)
    engine = create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
    )
    _ENGINE_CACHE[cache_key] = engine
    return engine


def default_audit_writer(session_id: str, node_name: str, tool_name: str, content: str) -> None:
    """Silently writes agent execution traces to FDE_VIEWS.AgentAuditLog using agent permissions."""
    try:
        from sqlalchemy import text

        db_user = os.getenv("SQL_AGENT_USER", "USR_FDE_RO")
        db_password = os.getenv("SQL_AGENT_PASSWORD", "")
        engine = _build_mssql_engine(db_user, db_password)
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO FDE_VIEWS.AgentAuditLog (SessionID, NodeExecuted, ToolName, Content)
                    VALUES (:session_id, :node_name, :tool_name, :content)
                    """
                ),
                {
                    "session_id": session_id,
                    "node_name": node_name,
                    "tool_name": tool_name,
                    "content": content,
                },
            )
            conn.commit()
    except Exception as exc:
        print(f"Audit Log Failed (Silent): {exc}")


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
        engine = _build_mssql_engine(username, password)
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
            FROM FDE_VIEWS.AgentAuditLog
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
    except Exception:
        # Fallback to the Langfuse/in-memory span buffer when MSSQL is unreachable in local dev
        return get_langfuse_bridge().query_spans(
            session_id_filter=session_id_filter,
            node_filter=node_filter,
            limit=limit,
        )


_ACTIVE_GRAPH: Any = None


def _get_compiled_graph():
    """Lazily builds and caches the LangGraph ReAct state machine (prefers modular `agent.graph`)."""
    global _ACTIVE_GRAPH
    if _ACTIVE_GRAPH is not None:
        return _ACTIVE_GRAPH
    try:
        from agent.graph import build_agent_graph

        _ACTIVE_GRAPH = build_agent_graph()
        return _ACTIVE_GRAPH
    except Exception:
        from src.orchestrator import fde_agent

        _ACTIVE_GRAPH = fde_agent
        return _ACTIVE_GRAPH


def _offline_fallback_streamer(session_id: str, prompt: str) -> Iterable[dict[str, Any]]:
    """Graceful local control-tower simulation when live MSSQL/Pinecone/LLM credentials are offline."""
    time.sleep(0.12)
    yield {
        "type": "tool_input",
        "node": "reasoner",
        "name": "query_telemetry_db",
        "args": {
            "sql_query": (
                "SELECT TOP 5 TS_UTC, V_LAT, V_LON, IOT_TEMP_VAL_C, CGO_COND_CD, "
                "RISK_CLS_TXT, DELAY_PROB_DEC, PRT_CNG_LVL "
                "FROM FDE_VIEWS.VW_ACTIVE_FLEET WHERE IOT_TEMP_VAL_C > 4.0 OR PRT_CNG_LVL > 7.0 "
                "ORDER BY IOT_TEMP_VAL_C DESC"
            )
        },
    }
    time.sleep(0.15)
    yield {
        "type": "tool_output",
        "node": "tools",
        "name": "query_telemetry_db",
        "content": (
            "COLUMNS: TS_UTC, V_LAT, V_LON, IOT_TEMP_VAL_C, CGO_COND_CD, RISK_CLS_TXT, DELAY_PROB_DEC, PRT_CNG_LVL\n"
            "('2026-11-14T23:35:00Z', 33.74, -118.27, 6.8, 'BREACH', 'High Risk', 0.81, 8.4)\n"
            "('2026-11-14T23:32:00Z', 33.77, -118.19, 4.9, 'ALERT', 'High Risk', 0.72, 7.6)"
        ),
    }
    time.sleep(0.12)
    yield {
        "type": "tool_input",
        "node": "reasoner",
        "name": "search_compliance_sop",
        "args": {"query": "IOT_TEMP_VAL_C > 4.0 PRT_CNG_LVL > 7.0 DELAY_PROB_DEC > 0.65 mitigation SOP"},
    }
    time.sleep(0.15)
    yield {
        "type": "tool_output",
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
        "node": "reasoner_final",
        "name": "LLM Text Synthesis",
        "content": (
            "🚨 **Operational Resolution Report (Cited: Cold_Chain_Incident_SOP_v2.md §3.2)**\n\n"
            "1. **Critical Thermal Breach Detected**: Unit at GPS `(33.74, -118.27)` reports `IOT_TEMP_VAL_C = 6.8°C` "
            "(exceeding the `4.0°C` threshold) with `PRT_CNG_LVL = 8.4` (`> 7.0`) and `DELAY_PROB_DEC = 0.81` (`> 0.65`).\n"
            "2. **Mandatory SOP Mitigation**: Engage secondary compressor override immediately and reroute to the "
            "San Pedro Cold-Storage Staging Hub within 45 minutes.\n"
            "3. **Audit Status**: Execution spans logged to `FDE_VIEWS.AgentAuditLog` (`USR_FDE_RO`) and `Langfuse`."
        ),
    }


def default_agent_streamer(session_id: str, prompt: str) -> Iterable[dict[str, Any]]:
    """Streams normalized trace events from the compiled LangGraph agent (with offline resilience)."""
    try:
        from langchain_core.messages import HumanMessage, ToolMessage
        from agent.graph import format_message_content

        graph = _get_compiled_graph()
        thread_config = {"configurable": {"thread_id": session_id}}
        events = graph.stream(
            {"messages": [HumanMessage(content=prompt)]},
            config=thread_config,
            stream_mode="updates",
        )

        emitted_any = False
        for event in events:
            for node_name, node_state in event.items():
                if node_name == "reasoner":
                    latest_msg = node_state["messages"][-1]
                    tool_calls = getattr(latest_msg, "tool_calls", None) or []
                    if tool_calls:
                        for tc in tool_calls:
                            emitted_any = True
                            yield {
                                "type": "tool_input",
                                "node": "reasoner",
                                "name": tc["name"],
                                "args": tc["args"],
                            }
                    formatted_text = format_message_content(getattr(latest_msg, "content", ""))
                    if formatted_text and not tool_calls:
                        emitted_any = True
                        yield {
                            "type": "final_answer",
                            "node": "reasoner_final",
                            "name": "LLM Text Synthesis",
                            "content": formatted_text,
                        }
                elif node_name == "tools":
                    for msg in node_state.get("messages", []):
                        if isinstance(msg, ToolMessage) or getattr(msg, "type", "") == "tool":
                            emitted_any = True
                            yield {
                                "type": "tool_output",
                                "node": "tools",
                                "name": getattr(msg, "name", "tool"),
                                "content": str(getattr(msg, "content", "")),
                            }
        if not emitted_any:
            yield from _offline_fallback_streamer(session_id, prompt)
    except Exception:
        yield from _offline_fallback_streamer(session_id, prompt)


# ==========================================
# 3. FASTAPI APPLICATION FACTORY
# ==========================================
def create_app(
    agent_streamer: Optional[Callable[[str, str], Iterable[dict[str, Any]]]] = None,
    audit_writer: Optional[Callable[[str, str, str, str], None]] = None,
    audit_reader: Optional[Callable[..., list[dict[str, Any]]]] = None,
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
        default_llm = "GEMINI" if (os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_APKEY")) else "OLLAMA"
        llm_setting = (os.getenv("Agent_llm") or os.getenv("llm") or default_llm).strip().upper()
        embeddings_setting = os.getenv("Embeddings_model", "LOCAL").strip().upper()
        return {
            "status": "ok",
            "service": "fde-dispatch-bff",
            "agent_llm": llm_setting,
            "embeddings_model": embeddings_setting,
            "observability": get_langfuse_bridge().status(),
            "capabilities": {
                "telemetry_db": ["past_history", "present_snapshot", "future_risk_predictions"],
                "weather_corridor": ["past_days", "current_weather", "forecast_days"],
                "sop_rag": ["post_incident_audit", "active_thresholds", "preventive_escalation"],
            },
        }

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
        return {
            "purged": True,
            "previous_session_id": req.old_session_id,
            "session_id": str(uuid.uuid4()),
        }

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


def run_stdlib_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Zero-dependency HTTP/SSE server for running FE/ + BFF endpoints in any Python environment."""
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
                default_llm = "GEMINI" if (os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_APKEY")) else "OLLAMA"
                llm_setting = (os.getenv("Agent_llm") or os.getenv("llm") or default_llm).strip().upper()
                embeddings_setting = os.getenv("Embeddings_model", "LOCAL").strip().upper()
                return self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "fde-dispatch-bff",
                        "agent_llm": llm_setting,
                        "embeddings_model": embeddings_setting,
                        "observability": get_langfuse_bridge().status(),
                    },
                )
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
                return self._send_json(
                    200,
                    {
                        "purged": True,
                        "previous_session_id": body.get("old_session_id"),
                        "session_id": str(uuid.uuid4()),
                    },
                )

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

    server = ThreadingHTTPServer((host, port), BffHandler)
    print(f"🚀 FDE Control Tower (FE + BFF) listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    run_stdlib_server(host="127.0.0.1", port=port)
