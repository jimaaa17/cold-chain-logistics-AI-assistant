"""assess: which readings breach the SOP.

The coded rules (`agent/rules.py`) always run and are a floor: a model can never remove a breach they
found, nor permit an action. In `model` mode it can add findings for thresholds the SOP excerpts state
but the rules don't cover. The model supplies the threshold and its quote; code compares the ledger
value, and added findings carry no actions. A (field, direction) the rules cover stays theirs, because
they also apply the SOP's conditions (port area, risk class).
"""
from __future__ import annotations

import operator
import re
from datetime import datetime

from langgraph.runtime import Runtime

from agent.dispatch.consult import consult, emit_decision
from agent.dispatch.context import DispatchContext
from agent.dispatch.schemas import Assessment, Finding
from agent.dispatch.state import DispatchState
from agent.evidence import FRESH, fmt_value, render_ledger
from agent.llm import LLMTask
from agent.rules import PORTS_LB_LA_BOX, RULES, evaluate_rules

FALLBACK_GAP = "assessment: model unavailable, used coded SOP thresholds only"

# Numeric telemetry fields: view alias -> legacy name used in the SOP
LEGACY_NAMES = {
    "Current_Temperature_C": "IOT_TEMP_VAL_C",
    "Cargo_Condition_Code": "CGO_COND_CD",
    "Delay_Probability": "DELAY_PROB_DEC",
    "Port_Congestion_Level": "PRT_CNG_LVL",
    "Route_Risk_Index": "RT_RSK_IDX",
}
_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
_CODED = {(r.view_field, r.direction) for r in RULES}


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[*`]", "", text).lower().split())


def check_finding(f: Finding, by_id: dict[str, dict], coded: set[tuple]) -> tuple[dict | None, str | None]:
    """(trigger to add, None), (None, reason it was rejected), or (None, None) for one the rules already found."""
    rec, sop = by_id.get(f.evidence), by_id.get(f.sop_evidence)
    direction = f.operator[0]
    if rec is None or rec["source"] != "telemetry" or sop is None or sop["source"] != "sop":
        return None, f"{f.evidence}/{f.sop_evidence} is not a telemetry record with an SOP excerpt"
    if (f.evidence, f.field, direction) in coded:
        return None, None
    if (f.field, direction) in _CODED:
        return None, f"{f.evidence} {f.field}: the coded SOP rule does not apply"
    value = rec["fields"].get(f.field)
    if f.field not in LEGACY_NAMES or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{f.evidence} has no numeric {f.field[:40]}"
    quote = _normalise(f.sop_quote)
    names = (LEGACY_NAMES[f.field].lower(), f.field.lower())
    if quote not in _normalise(sop.get("text") or "") or not any(n in quote for n in names):
        return None, f"{f.evidence} {f.field}: the quoted threshold is not in {f.sop_evidence}"
    if f.threshold not in {float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", quote)}:
        return None, f"{f.evidence} {f.field}: threshold {f.threshold:g} is not in the quote"
    if not _OPS[f.operator](value, f.threshold):
        return None, f"{f.evidence} {f.field} {fmt_value(value)} is not {f.operator} {f.threshold:g}"
    return {
        "rule": "SOP_MODEL_FINDING",
        "evidence": f.evidence,
        "summary": f"{f.field} {fmt_value(value)} {f.operator} {f.threshold:g} (threshold read from the SOP by the model)",
        "sop_ref": f"{sop['origin']} [{f.sop_evidence}]",
        "field": f.field,
        "direction": direction,
        "freshness": rec["freshness"],
        "actions": [],
        "high_stakes": False,
    }, None


def assess(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    ctx, records = runtime.context, state.get("ledger", [])
    mode = ctx.settings.thresholds_mode
    triggers, historical = evaluate_rules(records)
    gaps: list[str] = []
    if mode != "code" and {"telemetry", "sop"} <= {r["source"] for r in records}:
        gaps = _model_assessment(state, runtime, mode, triggers, historical)
    runtime.stream_writer(
        {
            "type": "evidence",
            "records": len(records),
            "fresh": sum(1 for r in records if r["freshness"] == FRESH),
            "historical": sum(1 for r in records if r["source"] == "telemetry" and r["freshness"] != FRESH),
            "triggers": [f"{t['rule']} [{t['evidence']}]" for t in triggers],
            "historical_breaches": len(historical),
            "gaps": [*(state.get("gaps") or []), *gaps],
            "notes": state.get("notes") or [],
        }
    )
    return {"triggers": triggers, "historical": historical, "gaps": gaps}


def _model_assessment(state, runtime, mode: str, triggers: list[dict], historical: list[dict]) -> list[str]:
    """Consults the model; in `model` mode appends its valid extra findings in place. Returns gaps."""
    ctx, records = runtime.context, state["ledger"]
    lat_min, lat_max, lon_min, lon_max = PORTS_LB_LA_BOX
    now = datetime.fromisoformat(state["as_of"]) if state.get("as_of") else ctx.clock()
    result, gaps = consult(
        runtime, decision="thresholds", mode=mode, task=LLMTask.ASSESS, schema=Assessment,
        messages=ctx.assessor_prompt.format_messages(
            ledger=render_ledger(
                records, [], [], now=now, window_hours=ctx.settings.evidence_freshness_hours, include_assessment=False,
            ),
            field_names=", ".join(f"{legacy} = {view}" for view, legacy in LEGACY_NAMES.items()),
            port_lat=f"{lat_min:g} to {lat_max:g}", port_lon=f"{lon_min:g} to {lon_max:g}",
        ),
        fallback_name="coded SOP thresholds", gap=FALLBACK_GAP,
    )
    if result is None:
        return gaps
    by_id = {r["id"]: r for r in records}
    coded = {(t["evidence"], t["field"], t["direction"]) for t in (*triggers, *historical)}
    reported = {(f.evidence, f.field, f.operator[0]) for f in result.findings}
    added: dict[tuple, dict] = {}
    rejected: list[str] = []
    for f in result.findings:
        item, reason = check_finding(f, by_id, coded)
        if item:
            added.setdefault((f.evidence, f.field, f.operator[0]), item)
        elif reason:
            rejected.append(reason)
    emit_decision(
        runtime, decision="thresholds", mode=mode, agreed=reported == coded,
        code=sorted(map(list, coded)),
        model={"reported": sorted(map(list, reported)), "missed": sorted(map(list, coded - reported)),
               "added": sorted(map(list, added)), "rejected": rejected},
        reasoning=result.reasoning,
    )
    if mode == "shadow":
        return []
    for item in added.values():
        (triggers if item["freshness"] == FRESH else historical).append(item)
    if rejected:
        gaps.append(f"assessment: ignored {len(rejected)} model finding(s) that failed checks ({'; '.join(rejected[:3])})")
    return gaps
