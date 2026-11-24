"""Coded rollups of SOP breaches for questions about past periods. The SQL comes from `rules.py`, so a
rollup counts exactly what the rules would flag; no model writes or computes any of it.

Every rollup groups by Data_Source, so real and synthetic readings are never added together.
"""
from __future__ import annotations

from agent.intent import Intent, area_sql, period_sql
from agent.rules import RULES

MIN_RATE_BASE = 30  # a rate over fewer readings is left out (2 of 2 is not "100%"); the counts remain
MAX_ROLLUP_ROWS = 24  # 12 calendar months x 2 data sources


def _rollup(bucket_sql: str, bucket: str, intent: Intent, *, half_width_deg: float, extra: str = "") -> str:
    parts = ["Data_Source", f"{bucket_sql} AS {bucket}", "COUNT(*) AS Reading_Count"]
    for rule in RULES:
        hits = f"SUM(CASE WHEN {rule.sql_predicate} THEN 1 ELSE 0 END)"
        base = "COUNT(*)"
        if rule.sql_scope:
            base = f"SUM(CASE WHEN {rule.sql_scope} THEN 1 ELSE 0 END)"
            parts.append(f"{base} AS {rule.id}_Base")
        parts.append(f"{hits} AS {rule.id}_Count")
        parts.append(f"CASE WHEN {base} >= {MIN_RATE_BASE} THEN CAST(ROUND(100.0 * {hits} / NULLIF({base}, 0), 1) AS FLOAT) END AS {rule.id}_Pct")
    parts.append(f"{extra}MAX(Timestamp) AS Timestamp")
    where = area_sql(intent, half_width_deg=half_width_deg) + period_sql(intent)
    sql = f"SELECT TOP {MAX_ROLLUP_ROWS} " + ", ".join(parts) + " FROM FDE_VIEWS.VW_ACTIVE_FLEET"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql + f" GROUP BY Data_Source, {bucket_sql} ORDER BY Data_Source, {bucket_sql}"


def year_sql(intent: Intent, *, half_width_deg: float) -> str:
    return _rollup("YEAR(Timestamp)", "Year", intent, half_width_deg=half_width_deg)


def month_sql(intent: Intent, *, half_width_deg: float) -> str:
    """By calendar month across the years in the period."""
    return _rollup("MONTH(Timestamp)", "Month", intent, half_width_deg=half_width_deg,
                   extra="COUNT(DISTINCT YEAR(Timestamp)) AS Years_Covered, ")
