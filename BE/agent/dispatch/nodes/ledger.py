"""ledger: gathered results -> numbered evidence records with freshness, gaps and notes."""
from __future__ import annotations

from types import SimpleNamespace

from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.evidence import AGGREGATED, REAL, UNKNOWN_SOURCE, sop_records, telemetry_records, weather_records
from agent.seasonality import seasonality_record

_SOURCE_ORDER = {"telemetry": 0, "weather": 1, "sop": 2}
MIXED_SOURCES_NOTE = (
    "telemetry: the query neither returned nor filtered on Data_Source, so these rows may mix real and synthetic readings"
)
SYNTHETIC_LEFT_OUT_NOTE = (
    "telemetry: the period also has synthetic readings; they were left out because real readings exist for it"
)
PARTIAL_LIST_NOTE = "telemetry: only the newest matching readings of the period are listed; more may match"
_EMPTY_NOTES = {
    "telemetry": "telemetry: no rows matched the query",
    "weather": "weather: no observations returned",
    "sop": "sop: no matching SOP section",
}


def ledger(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    now = runtime.context.clock()
    window = runtime.context.settings.evidence_freshness_hours
    records: list[dict] = []
    gaps: list[str] = []
    notes: list[str] = []
    month_rows: list[dict] | None = None
    counts = {"telemetry": 0, "weather": 0, "sop": 0}
    for item in sorted(state.get("gathered") or [], key=lambda g: _SOURCE_ORDER.get(g["source"], 9)):
        status = item.get("status") or ("error" if item.get("error") else "ok")
        if status == "error":
            gaps.append(f"{item['source']}: {item['error']}")
            continue
        if status == "empty":
            note = _EMPTY_NOTES.get(item["source"], f"{item['source']}: nothing returned")
            if note not in notes:
                notes.append(note)
            continue
        if item["source"] == "telemetry":
            rows = item["rows"]
            if item.get("rollup"):  # real history answers the question; synthetic only where there is none
                at = item["columns"].index("Data_Source")
                real = [r for r in rows if r[at] == REAL]
                if real and len(real) < len(rows):
                    rows = real
                    if SYNTHETIC_LEFT_OUT_NOTE not in notes:
                        notes.append(SYNTHETIC_LEFT_OUT_NOTE)
            new = telemetry_records(
                item["columns"], rows, now=now, window_hours=window, start=counts["telemetry"] + 1,
                aggregated=bool(item.get("aggregated")), sql=item.get("sql") or "",
            )
            if any(r["data_source"] == UNKNOWN_SOURCE for r in new) and MIXED_SOURCES_NOTE not in notes:
                notes.append(MIXED_SOURCES_NOTE)
            if item.get("rollup") == "month":
                month_rows = [dict(zip(item["columns"], row)) for row in rows]
        elif item["source"] == "weather":
            new = weather_records(item["observations"], now=now, window_hours=window, start=counts["weather"] + 1)
        else:
            new = sop_records([SimpleNamespace(**c) for c in item["chunks"]], start=counts["sop"] + 1)
        counts[item["source"]] += len(new)
        records.extend(new)
    if month_rows is not None:
        records.append(seasonality_record(month_rows))
    intent = state.get("intent") or {}
    if intent.get("period"):
        individual = sum(1 for r in records if r["source"] == "telemetry" and r["freshness"] != AGGREGATED)
        if individual >= runtime.context.settings.sql_max_rows:
            notes.append(PARTIAL_LIST_NOTE)
        bounds = f"{intent.get('period_start') or 'first record'} to {intent.get('period_end') or 'newest record'}"
        notes.append(f"period asked about: {intent['period']} ({bounds}, end not included)")
    # `gaps` appends (reducer): planner/router gaps recorded earlier this turn are kept
    return {"ledger": records, "gaps": gaps, "notes": notes, "as_of": now.isoformat()}
