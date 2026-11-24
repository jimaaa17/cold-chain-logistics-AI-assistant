"""Evidence ledger: tool and database results normalised into compact, citable records.

Every value the reasoner may state comes from a ledger record with a source, an `as_of` time and a
freshness tag. The verifier later checks the reasoner's draft against these same records.
Pure Python: no I/O, no LLM, unit-testable offline.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

FRESH = "fresh"
HISTORICAL = "historical"
FORECAST = "forecast"
UNDATED = "undated"
AGGREGATED = "aggregated"  # a COUNT/AVG/... row: covers many readings, so it is never "fresh"

# Where a telemetry row comes from. UNKNOWN: the query didn't return or filter on Data_Source.
REAL = "historical"
SYNTHETIC = "synthetic"
UNKNOWN_SOURCE = "unknown"

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


_LATEST_READING_FILTER = re.compile(r"[=<>]\s*\(\s*SELECT\s+MAX\s*\(\s*\[?Timestamp\]?\s*\)[^()]*\)", re.I)
_AGGREGATE_SQL = re.compile(r"\bGROUP\s+BY\b|\b(?:COUNT|COUNT_BIG|AVG|SUM|MIN|MAX|STDEVP?|VARP?|STRING_AGG)\s*\(", re.I)


def is_aggregate_sql(sql: str) -> bool:
    """Whether a query's rows summarise several readings. `WHERE Timestamp = (SELECT MAX(Timestamp) ...)`
    only picks individual readings, so it doesn't count."""
    return bool(_AGGREGATE_SQL.search(_LATEST_READING_FILTER.sub(" ", sql or "")))


_SOURCE_FILTER = re.compile(r"\bData_Source\]?\s*=\s*N?'(historical|synthetic)'", re.I)
_SOURCE_ALIAS = re.compile(r"\bAS\s+\[?Data_Source\b", re.I)


def source_filter(sql: str) -> str | None:
    """The single data source a query restricts itself to, if that is certain."""
    found = {m.lower() for m in _SOURCE_FILTER.findall(sql or "")}
    if len(found) != 1 or re.search(r"\b(?:OR|NOT|UNION)\b", sql, re.I):
        return None
    return found.pop()


def data_source_of(value: Any, sql_source: str | None, sql: str = "") -> str:
    """The row's Data_Source column when the query returned it unchanged, else the query's filter."""
    name = str(value or "").strip().lower()
    if name in (REAL, SYNTHETIC) and not _SOURCE_ALIAS.search(sql):
        return name
    return sql_source or UNKNOWN_SOURCE


def telemetry_records(
    columns: list[str], rows: Iterable[Iterable[Any]], *, now: datetime, window_hours: float, start: int = 1,
    aggregated: bool = False, sql: str = "",
) -> list[dict]:
    """With `aggregated`, the timestamp is only the newest reading behind the row, so the row is labelled
    AGGREGATED instead of taking that reading's freshness (and can't trigger or permit an SOP action)."""
    records = []
    sql_source = source_filter(sql)
    for offset, row in enumerate(rows):
        fields = {col: _plain(val) for col, val in zip(columns, row) if val is not None or not aggregated}
        data_source = data_source_of(fields.pop("Data_Source", None), sql_source, sql)
        as_of = to_utc(fields.pop("Timestamp", None))
        records.append(
            {
                "id": f"T{start + offset}",
                "source": "telemetry",
                "origin": "FDE_VIEWS.VW_ACTIVE_FLEET"
                + {REAL: " (real data)", SYNTHETIC: " (synthetic data)", UNKNOWN_SOURCE: " (data source not stated)"}.get(data_source, ""),
                "data_source": data_source,
                "as_of": as_of.isoformat() if as_of else None,
                "freshness": AGGREGATED if aggregated else classify_freshness(as_of, now, window_hours),
                "fields": fields,
            }
        )
    return records


def source_counts(ledger: Iterable[dict]) -> dict[str, int]:
    """Telemetry records per data source."""
    counts: dict[str, int] = {}
    for rec in ledger:
        if rec["source"] == "telemetry":
            name = rec.get("data_source") or UNKNOWN_SOURCE
            counts[name] = counts.get(name, 0) + 1
    return counts


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


def would_have_required(item: dict, action_labels: dict[str, str]) -> str:
    return "; ".join(action_labels.get(a, a) for a in item["actions"]) or "no action"


def render_ledger(
    ledger: list[dict],
    triggers: list[dict],
    historical: list[dict],
    *,
    now: datetime,
    window_hours: float,
    notes: list[str] | None = None,
    include_assessment: bool = True,
    retrospective: bool = False,
    action_labels: dict[str, str] | None = None,
) -> str:
    """Compact text form handed to the models (one line per record).

    `notes` lists valid-but-empty results ("telemetry: no rows matched the query"). With
    `include_assessment=False` the SOP trigger sections are left out, so a model assessing the
    thresholds itself isn't anchored on the coded result. With `retrospective` (the question names a past
    period) each past breach also says what the SOP would have required; that permits no action."""
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
    lines.append(
        "HISTORICAL OR UNDATED BREACHES (report as history; never propose actions for these"
        + ("; you may say what the SOP would have required):" if retrospective else "):")
    )
    if not historical:
        lines.append("none")
    for trig in historical:
        line = f"- {trig['rule']} on [{trig['evidence']}] ({trig['freshness']}): {trig['summary']} ({trig['sop_ref']})"
        if retrospective:
            line += f" | SOP would have required: {would_have_required(trig, action_labels or {})}"
        lines.append(line)
    return "\n".join(lines)
