"""Deterministic intent routing: decides, without an LLM call, how a question is answered.

- ``incident_scan``: the standard "what is going on with shipments (near X)?" question. Fixed fan-out:
  telemetry + weather + SOP, no planning call.
- ``sop_lookup``: a policy question with no data request. SOP search only.
- ``open``: in scope but not a standard shape (aggregates, comparisons). An LLM plans the tool calls.
- ``clarify``: out of scope or too vague. Answered directly, no tools.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Named places operators use, with a representative point (lat, lon).
GAZETTEER: dict[str, tuple[float, float]] = {
    "port of long beach": (33.75, -118.21),
    "port of los angeles": (33.74, -118.27),
    "port of la": (33.74, -118.27),
    "long beach": (33.77, -118.19),
    "san pedro": (33.74, -118.29),
    "los angeles": (34.05, -118.24),
    "inland empire": (34.06, -117.39),
    "san bernardino": (34.11, -117.29),
    "riverside": (33.98, -117.38),
    "san diego": (32.72, -117.16),
}
_LA_ABBREVIATION = re.compile(r"\bLA\b")  # case-sensitive: "LA", not "la"

_COORDS = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")
_TEMP_ABOVE = re.compile(r"(?:above|over|greater than|exceeding|hotter than|>)\s*(-?\d+(?:\.\d+)?)\s*°?\s*c\b", re.I)
_TEMP_BELOW = re.compile(r"(?:below|under|less than|colder than|<)\s*(-?\d+(?:\.\d+)?)\s*°?\s*c\b", re.I)

_DOMAIN = re.compile(
    r"shipment|cargo|reefer|temperat|\btemp\b|°\s*c|excursion|breach|congestion|\bport\b|delay|risk|fleet|"
    r"truck|vehicle|\bsop\b|procedure|policy|protocol|threshold|weather|wind|corridor|divert|diversion|"
    r"escalat|cold|spoil|perishable|telemetry|driver|dispatch",
    re.I,
)
_DATA = re.compile(
    r"shipment|cargo|fleet|truck|vehicle|reading|temperat|\btemp\b|°\s*c|excursion|breach|congestion|"
    r"delay|risk|status|incident|anomal",
    re.I,
)
_ANALYTIC = re.compile(
    r"\baverage\b|\bavg\b|\bmean\b|how many|\bcount\b|\btrend|compare|comparison|distribution|"
    r"\bper\b|group by|breakdown|correlat|\btotal\b|\bmedian\b|percent",
    re.I,
)
_POLICY = re.compile(
    r"\bsop\b|procedure|policy|protocol|what (?:should|must|do) (?:we|i|dispatchers?)|what does the|"
    r"rule|threshold|required|requirement",
    re.I,
)
_LISTING = re.compile(r"\bwhich\b|\bshow\b|\blist\b|\bany\b|\bcurrent|\bnow\b|\blatest\b|\bnear\b|\baround\b", re.I)


@dataclass(frozen=True)
class Intent:
    kind: str  # incident_scan | sop_lookup | open | clarify
    reason: str
    place: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    min_temp_c: float | None = None
    max_temp_c: float | None = None

    def to_state(self) -> dict:
        return asdict(self)

    @property
    def has_area(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def _find_place(text: str) -> tuple[str | None, tuple[float, float] | None]:
    lowered = text.lower()
    for name in sorted(GAZETTEER, key=len, reverse=True):  # longest name first ("port of long beach")
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return name, GAZETTEER[name]
    if _LA_ABBREVIATION.search(text):
        return "los angeles", GAZETTEER["los angeles"]
    match = _COORDS.search(text)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return f"{lat:g}, {lon:g}", (lat, lon)
    return None, None


def classify_intent(question: str) -> Intent:
    text = " ".join((question or "").split())
    if len(text.split()) < 2 or not _DOMAIN.search(text):
        return Intent("clarify", "not a cold-chain operations question")

    place, point = _find_place(text)
    above = _TEMP_ABOVE.search(text)
    below = _TEMP_BELOW.search(text)
    common = {
        "place": place,
        "latitude": point[0] if point else None,
        "longitude": point[1] if point else None,
        "min_temp_c": float(above.group(1)) if above else None,
        "max_temp_c": float(below.group(1)) if below else None,
    }

    if _ANALYTIC.search(text):
        return Intent("open", "aggregate or comparison question", **common)
    wants_data = bool(_DATA.search(text)) and (point is not None or above or below or _LISTING.search(text))
    if wants_data:
        return Intent("incident_scan", "standard shipment status question", **common)
    if _POLICY.search(text):
        return Intent("sop_lookup", "policy question without a data request", **common)
    return Intent("open", "in scope, non-standard question", **common)


def build_scan_sql(intent: Intent, *, half_width_deg: float, max_rows: int) -> str:
    """Fixed, validated-shape query for an incident scan (latest readings first)."""
    where = []
    if intent.has_area:
        lat, lon, w = intent.latitude, intent.longitude, half_width_deg
        where.append(f"Latitude BETWEEN {lat - w:.4f} AND {lat + w:.4f}")
        where.append(f"Longitude BETWEEN {lon - w:.4f} AND {lon + w:.4f}")
    if intent.min_temp_c is not None:
        where.append(f"Current_Temperature_C > {intent.min_temp_c:g}")
    if intent.max_temp_c is not None:
        where.append(f"Current_Temperature_C < {intent.max_temp_c:g}")
    sql = (
        f"SELECT TOP {int(max_rows)} Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code, "
        "Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index "
        "FROM FDE_VIEWS.VW_ACTIVE_FLEET"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql + " ORDER BY Timestamp DESC"


SCAN_SOP_QUERY = (
    "cold-chain temperature breach mitigation, port congestion diversion, "
    "high risk delay probability escalation"
)
