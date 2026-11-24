"""ledger (gathered results -> numbered evidence records with freshness) and rules (SOP §1-§3 as code)."""
from __future__ import annotations

from types import SimpleNamespace

from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.evidence import sop_records, telemetry_records, weather_records
from agent.rules import evaluate_rules

_SOURCE_ORDER = {"telemetry": 0, "weather": 1, "sop": 2}
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
            new = telemetry_records(item["columns"], item["rows"], now=now, window_hours=window, start=counts["telemetry"] + 1)
        elif item["source"] == "weather":
            new = weather_records(item["observations"], now=now, window_hours=window, start=counts["weather"] + 1)
        else:
            new = sop_records([SimpleNamespace(**c) for c in item["chunks"]], start=counts["sop"] + 1)
        counts[item["source"]] += len(new)
        records.extend(new)
    # `gaps` appends (reducer): planner/router gaps recorded earlier this turn are kept
    return {"ledger": records, "gaps": gaps, "notes": notes, "as_of": now.isoformat()}


def rules(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    ledger_records = state.get("ledger", [])
    triggers, historical = evaluate_rules(ledger_records)
    runtime.stream_writer(
        {
            "type": "evidence",
            "records": len(ledger_records),
            "fresh": sum(1 for r in ledger_records if r["freshness"] == "fresh"),
            "historical": sum(1 for r in ledger_records if r["source"] == "telemetry" and r["freshness"] != "fresh"),
            "triggers": [f"{t['rule']} [{t['evidence']}]" for t in triggers],
            "historical_breaches": len(historical),
            "gaps": state.get("gaps") or [],
            "notes": state.get("notes") or [],
        }
    )
    return {"triggers": triggers, "historical": historical}
