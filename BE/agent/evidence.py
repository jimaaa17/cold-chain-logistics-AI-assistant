"""Evidence ledger: tool and database results normalised into compact, citable records.

Every value the reasoner may state comes from a ledger record with a source, an `as_of` time and a
freshness tag. The verifier later checks the reasoner's draft against these same records.
Pure Python: no I/O, no LLM, unit-testable offline.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

FRESH = "fresh"
HISTORICAL = "historical"
FORECAST = "forecast"
UNDATED = "undated"

SOP_EXCERPT_CHARS = 700


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: Any) -> datetime | None:
    """Parse datetimes, dates and ISO strings; naive values are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify_freshness(as_of: datetime | None, now: datetime, window_hours: float) -> str:
    if as_of is None:
        return UNDATED
    if as_of > now + timedelta(hours=1):
        return FORECAST
    return FRESH if now - as_of <= timedelta(hours=window_hours) else HISTORICAL


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def telemetry_records(
    columns: list[str], rows: Iterable[Iterable[Any]], *, now: datetime, window_hours: float, start: int = 1
) -> list[dict]:
    records = []
    for offset, row in enumerate(rows):
        fields = {col: _plain(val) for col, val in zip(columns, row)}
        as_of = to_utc(fields.pop("Timestamp", None))
        records.append(
            {
                "id": f"T{start + offset}",
                "source": "telemetry",
                "origin": "FDE_VIEWS.VW_ACTIVE_FLEET",
                "as_of": as_of.isoformat() if as_of else None,
                "freshness": classify_freshness(as_of, now, window_hours),
                "fields": fields,
            }
        )
    return records


def weather_records(obs: dict, *, now: datetime, window_hours: float, start: int = 1) -> list[dict]:
    records = []
    current = obs.get("current") or {}
    as_of = to_utc(current.get("time"))
    records.append(
        {
            "id": f"W{start}",
            "source": "weather",
            "origin": f"open-meteo @ {obs.get('latitude')}, {obs.get('longitude')}",
            "as_of": as_of.isoformat() if as_of else None,
            "freshness": classify_freshness(as_of, now, window_hours),
            "fields": {
                k: v
                for k, v in {
                    "temperature_c": current.get("temperature_c"),
                    "wind_kmh": current.get("wind_kmh"),
                    "wind_direction_deg": current.get("wind_direction_deg"),
                }.items()
                if v is not None
            },
        }
    )
    for offset, day in enumerate(obs.get("daily") or [], start=1):
        day_as_of = to_utc(day.get("date"))
        records.append(
            {
                "id": f"W{start + offset}",
                "source": "weather",
                "origin": f"open-meteo daily @ {obs.get('latitude')}, {obs.get('longitude')}",
                "as_of": day_as_of.isoformat() if day_as_of else None,
                "freshness": classify_freshness(day_as_of, now, max(window_hours, 24.0)),
                "fields": {k: v for k, v in day.items() if k != "date" and v is not None},
            }
        )
    return records


def sop_records(chunks: Iterable[Any], *, start: int = 1) -> list[dict]:
    records = []
    for offset, chunk in enumerate(chunks):
        meta = getattr(chunk, "metadata", None) or {}
        section = meta.get("section")
        source_file = getattr(chunk, "source_file", None) or "SOP"
        citation = f"{source_file} > {section}" if section else source_file
        if "page_number" in meta:
            citation += f" (page {meta['page_number']})"
        text = " ".join(str(getattr(chunk, "text", "")).split())
        records.append(
            {
                "id": f"S{start + offset}",
                "source": "sop",
                "origin": citation,
                "as_of": None,
                "freshness": UNDATED,
                "fields": {"relevance": round(float(getattr(chunk, "score", 0.0)), 2)},
                "text": text[:SOP_EXCERPT_CHARS],
            }
        )
    return records


def fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{round(value, 3):g}"
    return str(value)


def render_ledger(
    ledger: list[dict],
    triggers: list[dict],
    historical: list[dict],
    *,
    now: datetime,
    window_hours: float,
    notes: list[str] | None = None,
    include_assessment: bool = True,
) -> str:
    """Compact text form handed to the models (one line per record).

    `notes` lists valid-but-empty results ("telemetry: no rows matched the query"). With
    `include_assessment=False` the SOP trigger sections are left out, so a model assessing the
    thresholds itself isn't anchored on the coded result."""
    lines = [f"EVIDENCE LEDGER (now {now.strftime('%Y-%m-%d %H:%M')} UTC; fresh = newer than {window_hours:g} h)"]
    if not ledger:
        lines.append("(no evidence gathered)")
    for rec in ledger:
        when = rec["as_of"][:16].replace("T", " ") if rec.get("as_of") else "no timestamp"
        fields = " · ".join(f"{k}={fmt_value(v)}" for k, v in rec["fields"].items())
        line = f"[{rec['id']}] {rec['source']} · {rec['origin']} · {when} · {rec['freshness']}"
        if fields:
            line += f" · {fields}"
        if rec.get("text"):
            line += f' · "{rec["text"]}"'
        lines.append(line)
    if notes:
        lines.append("")
        lines.append("NOTES (sources that answered with nothing; not failures):")
        lines.extend(f"- {n}" for n in notes)
    if not include_assessment:
        return "\n".join(lines)
    lines.append("")
    lines.append("SOP TRIGGERS (fresh evidence; the only actions you may propose):")
    if not triggers:
        lines.append("none")
    for trig in triggers:
        lines.append(
            f"- {trig['rule']} on [{trig['evidence']}]: {trig['summary']} ({trig['sop_ref']}) "
            f"-> allowed actions: {', '.join(trig['actions'])}"
        )
    lines.append("")
    lines.append("HISTORICAL OR UNDATED BREACHES (report as history; never propose actions for these):")
    if not historical:
        lines.append("none")
    for trig in historical:
        lines.append(f"- {trig['rule']} on [{trig['evidence']}] ({trig['freshness']}): {trig['summary']} ({trig['sop_ref']})")
    return "\n".join(lines)
