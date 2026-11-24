"""SOP thresholds as deterministic code (data/policy/Cold_Chain_Incident_SOP_v2.md).

The LLM never decides whether a threshold is breached. Rules read named telemetry fields from the
evidence ledger; a breach on fresh evidence becomes a *trigger* that permits specific actions, and a
breach on historical or undated evidence is reported as history only. Weather never triggers a rule:
the SOP has no weather threshold (add one to the SOP first, then here).

The SOP names raw legacy columns (IOT_TEMP_VAL_C, PRT_CNG_LVL, ...); the agent reads the view's
aliases. Each rule records both so citations stay traceable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.evidence import AGGREGATED, FRESH

SOP_DOC = "Cold_Chain_Incident_SOP_v2.md"

# Actions that move or destroy freight: proposing one pauses the workflow for dispatcher approval.
HIGH_STAKES_ACTIONS = frozenset({"divert_emergency_cold_storage", "divert_inland_empire", "write_off"})

ACTION_LABELS = {
    "restart_cooling": "Contact the driver to restart the auxiliary cooling unit",
    "divert_emergency_cold_storage": "Divert to the nearest emergency cold-storage facility (SOP: only if ETA delay > 1 h)",
    "divert_inland_empire": "Divert to the Inland Empire Overflow Depot (San Bernardino) for cross-docking",
    "escalate_tier2": "Escalate to the Tier 2 Logistics Manager",
    "write_off": "Write off the cargo",
}

# Port of Long Beach / Los Angeles area (SOP section 2 applies only here).
PORTS_LB_LA_BOX = (33.60, 34.00, -118.45, -118.00)  # lat_min, lat_max, lon_min, lon_max


def _num(fields: dict, name: str) -> float | None:
    value = fields.get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _in_ports_area(fields: dict) -> bool:
    lat, lon = _num(fields, "Latitude"), _num(fields, "Longitude")
    if lat is None or lon is None:
        return False
    lat_min, lat_max, lon_min, lon_max = PORTS_LB_LA_BOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


@dataclass(frozen=True)
class SopRule:
    id: str
    sop_ref: str
    sop_field: str
    threshold: float
    actions: tuple[str, ...]
    check: Callable[[dict], str | None]  # returns a human summary when breached, else None
    view_field: str  # the semantic view's name for sop_field
    direction: str  # ">" or "<"
    sql_predicate: str  # the same condition over the view's columns, for rollups (tests compare the two)
    sql_scope: str | None = None  # readings the rule can apply to at all; None = every reading


def _ports_area_sql() -> str:
    lat_min, lat_max, lon_min, lon_max = PORTS_LB_LA_BOX
    return f"Latitude BETWEEN {lat_min} AND {lat_max} AND Longitude BETWEEN {lon_min} AND {lon_max}"


def _temp_breach(f: dict) -> str | None:
    t = _num(f, "Current_Temperature_C")
    return f"Current_Temperature_C {t:g} > 4.0" if t is not None and t > 4.0 else None


def _temp_below_range(f: dict) -> str | None:
    t = _num(f, "Current_Temperature_C")
    return f"Current_Temperature_C {t:g} < 0.0" if t is not None and t < 0.0 else None


def _port_congestion(f: dict) -> str | None:
    c = _num(f, "Port_Congestion_Level")
    if c is not None and c > 7.0 and _in_ports_area(f):
        return f"Port_Congestion_Level {c:g} > 7.0 (Port of Long Beach / LA)"
    return None


def _high_risk_delay(f: dict) -> str | None:
    risk = str(f.get("Risk_Classification") or "")
    p = _num(f, "Delay_Probability")
    if risk.lower().startswith("high") and p is not None and p > 0.65:
        return f"Risk_Classification {risk} and Delay_Probability {p:g} > 0.65"
    return None


RULES: tuple[SopRule, ...] = (
    # The SOP's emergency cold-storage diversion applies only "if ETA delay is greater than 1 hour".
    # Telemetry has no ETA field, so code cannot confirm that condition and does not permit it;
    # add it here once an ETA delay column exists in the view.
    SopRule("TEMP_BREACH", f"{SOP_DOC} §1 Critical Breach", "IOT_TEMP_VAL_C", 4.0,
            ("restart_cooling",), _temp_breach, "Current_Temperature_C", ">",
            "Current_Temperature_C > 4.0"),
    SopRule("TEMP_BELOW_RANGE", f"{SOP_DOC} §1 Fresh Perishables range", "IOT_TEMP_VAL_C", 0.0,
            (), _temp_below_range, "Current_Temperature_C", "<",
            "Current_Temperature_C < 0.0"),
    SopRule("PORT_CONGESTION", f"{SOP_DOC} §2 Port of Long Beach / LA", "PRT_CNG_LVL", 7.0,
            ("divert_inland_empire",), _port_congestion, "Port_Congestion_Level", ">",
            f"Port_Congestion_Level > 7.0 AND {_ports_area_sql()}", _ports_area_sql()),
    SopRule("HIGH_RISK_DELAY", f"{SOP_DOC} §3 Risk Classification Triggers", "DELAY_PROB_DEC", 0.65,
            ("escalate_tier2",), _high_risk_delay, "Delay_Probability", ">",
            "Risk_Classification LIKE 'High%' AND Delay_Probability > 0.65"),
)

THRESHOLDS = tuple(sorted({r.threshold for r in RULES}))


def evaluate_rules(ledger: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Returns (triggers on fresh evidence, breaches on historical/undated evidence)."""
    triggers: list[dict] = []
    historical: list[dict] = []
    for rec in ledger:
        if rec.get("source") != "telemetry":
            continue  # only telemetry fields drive SOP rules
        if rec.get("freshness") == AGGREGATED:
            continue  # an average or maximum is not one shipment's reading
        for rule in RULES:
            summary = rule.check(rec.get("fields") or {})
            if summary is None:
                continue
            item = {
                "rule": rule.id,
                "evidence": rec["id"],
                "summary": summary,
                "sop_ref": rule.sop_ref,
                "field": rule.view_field,
                "direction": rule.direction,
                "freshness": rec.get("freshness"),
                "data_source": rec.get("data_source"),
                "actions": list(rule.actions),
                "high_stakes": any(a in HIGH_STAKES_ACTIONS for a in rule.actions),
            }
            (triggers if rec.get("freshness") == FRESH else historical).append(item)
    return triggers, historical
