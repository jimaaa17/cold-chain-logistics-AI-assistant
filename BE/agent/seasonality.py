"""Does an SOP breach rate depend on the calendar month? Decided in code, from real history only.

Chi-square test of independence (breach or not, by calendar month) at the 1% level. A model shown twelve
slightly different percentages will usually find a "summer peak" in them; this says whether the
differences are larger than chance. Synthetic readings are excluded: their seasons were generated.
Pure Python: no I/O, no LLM.
"""
from __future__ import annotations

from typing import Any, Iterable

from agent.evidence import AGGREGATED, REAL
from agent.rules import RULES

SEASONAL = "seasonal"
NOT_SEASONAL = "no seasonal pattern"
INSUFFICIENT = "insufficient data"

CRITICAL_1PCT_DF11 = 24.725
MIN_EXPECTED = 5.0
MIN_YEARS = 2  # one year can't separate a season from a one-off
ORIGIN = "seasonality test: chi-square over 12 calendar months, 1% level, real history only"


def chi_square(months: list[tuple[float, float]]) -> tuple[float, float]:
    """(statistic, smallest expected count) for (readings, breaches) pairs."""
    total = sum(n for n, _ in months)
    share = sum(b for _, b in months) / total if total else 0.0
    if not total or share in (0.0, 1.0):
        return 0.0, 0.0
    statistic, smallest = 0.0, float("inf")
    for n, breaches in months:
        for observed, expected in ((breaches, n * share), (n - breaches, n * (1 - share))):
            smallest = min(smallest, expected)
            if expected:
                statistic += (observed - expected) ** 2 / expected
    return statistic, smallest


def assess_seasonality(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Verdict per SOP rule from calendar-month rollup rows (`rollup.month_sql`)."""
    real = {int(r["Month"]): r for r in rows if str(r.get("Data_Source", "")).lower() == REAL}
    enough = len(real) == 12 and all(int(r["Years_Covered"]) >= MIN_YEARS for r in real.values())
    out: dict[str, dict[str, Any]] = {}
    for rule in RULES:
        base = f"{rule.id}_Base" if rule.sql_scope else "Reading_Count"
        months = {m: (float(r[base] or 0), float(r[f"{rule.id}_Count"] or 0)) for m, r in real.items()}
        if not enough:
            out[rule.id] = {"verdict": INSUFFICIENT}
            continue
        statistic, smallest = chi_square(list(months.values()))
        if smallest < MIN_EXPECTED:
            out[rule.id] = {"verdict": INSUFFICIENT}
            continue
        rates = {m: round(100 * b / n, 1) for m, (n, b) in months.items()}
        low, high = min(rates, key=rates.get), max(rates, key=rates.get)
        out[rule.id] = {
            "verdict": SEASONAL if statistic > CRITICAL_1PCT_DF11 else NOT_SEASONAL,
            "chi_square": round(statistic, 1), "critical_value": CRITICAL_1PCT_DF11,
            "lowest_month": low, "lowest_pct": rates[low], "highest_month": high, "highest_pct": rates[high],
        }
    return out


def seasonality_record(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Ledger record [H1]. Always produced for a seasonality question, even when nothing could be tested."""
    fields = {f"{rule}_{key}": value for rule, v in assess_seasonality(rows).items() for key, value in v.items()}
    return {
        "id": "H1", "source": "analysis", "origin": ORIGIN, "as_of": None, "freshness": AGGREGATED, "fields": fields,
    }


def any_seasonal(record: dict[str, Any]) -> bool:
    return any(v == SEASONAL for k, v in record["fields"].items() if k.endswith("_verdict"))
