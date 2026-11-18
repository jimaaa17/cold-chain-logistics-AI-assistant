"""Parallel data-gathering nodes. Each reports its tool call, and every failure becomes a ledger gap
(`result["error"]`) instead of an exception, so the reasoner says what's missing rather than guessing."""
from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.text import plain

logger = logging.getLogger(__name__)


def gather_telemetry(payload: dict, runtime: Runtime[DispatchContext]) -> dict:
    from agent.tools.telemetry_sql import UnsafeSqlError, fetch_telemetry_rows

    ctx, emit = runtime.context, runtime.stream_writer
    sql = payload.get("sql", "")
    emit({"type": "tool_input", "name": "query_telemetry_db", "args": {"sql_query": sql}})
    result: dict = {"source": "telemetry", "columns": [], "rows": [], "error": None}
    try:
        columns, rows = fetch_telemetry_rows(ctx.resources.engine(), sql, max_rows=ctx.settings.sql_max_rows)
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


def gather_weather(payload: dict, runtime: Runtime[DispatchContext]) -> dict:
    from agent.tools.corridor_api import CorridorRequestError, fetch_corridor_observations, format_corridor_observations

    ctx, emit = runtime.context, runtime.stream_writer
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
            args["latitude"], args["longitude"], client=ctx.resources.http(), api_url=ctx.settings.corridor_api_url,
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


def gather_sop(payload: dict, runtime: Runtime[DispatchContext]) -> dict:
    from agent.tools.sop_rag import format_inprocess_results
    from rag.retrieval.client import NO_MATCH_MESSAGE, QUERY_REJECTED_MESSAGE, UNAVAILABLE_MESSAGE

    ctx, emit = runtime.context, runtime.stream_writer
    query = payload.get("query", "")
    emit({"type": "tool_input", "name": "search_compliance_sop", "args": {"query": query}})
    result: dict = {"source": "sop", "chunks": [], "error": None}
    try:
        chunks = ctx.resources.retrieval().retrieve(query, top_k=ctx.settings.rag_top_k)
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
