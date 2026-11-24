"""Finds the time period a question names. Pure code, so the dates in a query never come from a model.

Returns None for questions about now. `end` is exclusive. Times are UTC.
"last year" is the previous calendar year; "past year" and "recent year" are the 12 months up to now.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

FIRST_YEAR = 2015

_MONTHS = {
    name: i for i, names in enumerate(
        (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
         ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"),
         ("november", "nov"), ("december", "dec")), start=1,
    ) for name in names
}
# season -> (first month, year offset of that month). Winter 2023 = Dec 2022 to Feb 2023.
_SEASONS = {"winter": (12, -1), "spring": (3, 0), "summer": (6, 0), "autumn": (9, 0), "fall": (9, 0)}

_YEAR = r"(20\d{2})"
_TO = r"\s*(?:-|–|—|to|and|through|until|thru)\s*"
_MONTH = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ISO_DAY = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH_YEAR = re.compile(rf"\b{_MONTH}\s+(?:of\s+)?{_YEAR}\b", re.I)
_MONTH_RANGE = re.compile(rf"\b{_MONTH}\s+(?:of\s+)?{_YEAR}{_TO}{_MONTH}\s+(?:of\s+)?{_YEAR}\b", re.I)
_SEASON_YEAR = re.compile(rf"\b(winter|spring|summer|autumn|fall)\s+(?:of\s+|in\s+)?{_YEAR}\b", re.I)
_QUARTER = re.compile(rf"\bQ([1-4])\s*(?:of\s+)?{_YEAR}\b", re.I)
_YEAR_RANGE = re.compile(rf"\b{_YEAR}{_TO}{_YEAR}\b", re.I)
_SINCE = re.compile(rf"\bsince\s+{_YEAR}\b", re.I)
_ONE_YEAR = re.compile(rf"(?<![\d.-]){_YEAR}(?![\d.-]*\d)")
_LAST_YEAR = re.compile(r"\blast\s+(?:calendar\s+)?year\b", re.I)
_PAST_YEAR = re.compile(r"\b(?:past|recent|previous\s+12\s+months|last\s+12\s+months|latest)(?:\s+year)\b|\b(?:past|last|previous)\s+(?:12|twelve)\s+months\b", re.I)
_LAST_N = re.compile(r"\b(?:past|last|previous|recent)\s+(\d{1,2})\s+(day|week|month|year)s\b", re.I)
_ALL_YEARS = re.compile(
    r"\b(?:over|across|through)\s+(?:the\s+|all\s+|several\s+|many\s+)*years\b|\b(?:several|all|multiple)\s+years\b|"
    r"\bhistorical(?:ly)?\b|\bdata\s*warehouse\b|\bever\b", re.I
)
SEASONAL = re.compile(r"\bseason(?:s|al|ality|ally)?\b|\btime of (?:the )?year\b|\b(?:which|what) months?\b|\bmonth of the year\b", re.I)


@dataclass(frozen=True)
class Period:
    start: datetime | None  # None: from the first record
    end: datetime | None  # exclusive; None: up to the newest record
    label: str


def _first(year: int, month: int = 1) -> datetime:
    return datetime(year + (month - 1) // 12, (month - 1) % 12 + 1, 1)


def _months_back(now: datetime, months: int) -> datetime:
    index = now.year * 12 + now.month - 1 - months
    year, month = divmod(index, 12)
    last_day = (_first(year, month + 2) - timedelta(days=1)).day
    return now.replace(year=year, month=month + 1, day=min(now.day, last_day))


def parse_period(text: str, now: datetime) -> Period | None:
    now = now.replace(tzinfo=None, microsecond=0)
    midnight = now.replace(hour=0, minute=0, second=0)

    def valid(*years: int) -> bool:
        return all(FIRST_YEAR <= y <= now.year for y in years)

    if m := _ISO_DAY.search(text):
        try:
            day = datetime(*(int(g) for g in m.groups()))
        except ValueError:
            return None
        return Period(day, day + timedelta(days=1), m.group(0)) if valid(day.year) else None
    if (m := _MONTH_RANGE.search(text)) and valid(int(m[2]), int(m[4])):
        start = _first(int(m[2]), _MONTHS[m[1].lower()])
        end = _first(int(m[4]), _MONTHS[m[3].lower()] + 1)
        if start < end:
            return Period(start, end, m.group(0))
    if (m := _MONTH_YEAR.search(text)) and valid(int(m[2])):
        month = _MONTHS[m[1].lower()]
        return Period(_first(int(m[2]), month), _first(int(m[2]), month + 1), m.group(0))
    if (m := _SEASON_YEAR.search(text)) and valid(int(m[2])):
        month, shift = _SEASONS[m[1].lower()]
        year = int(m[2]) + shift
        return Period(_first(year, month), _first(year, month + 3), m.group(0))
    if (m := _QUARTER.search(text)) and valid(int(m[2])):
        month = 3 * int(m[1]) - 2
        return Period(_first(int(m[2]), month), _first(int(m[2]), month + 3), m.group(0))
    if (m := _YEAR_RANGE.search(text)) and valid(int(m[1]), int(m[2])):
        low, high = sorted((int(m[1]), int(m[2])))
        return Period(_first(low), _first(high + 1), m.group(0))
    if (m := _SINCE.search(text)) and valid(int(m[1])):
        return Period(_first(int(m[1])), None, m.group(0))
    if (m := _ONE_YEAR.search(text)) and valid(int(m[1])):
        return Period(_first(int(m[1])), _first(int(m[1]) + 1), m[1])
    if _PAST_YEAR.search(text):
        return Period(_months_back(midnight, 12), None, "past year")
    if _LAST_YEAR.search(text):
        return Period(_first(now.year - 1), _first(now.year), "last calendar year")
    if m := _LAST_N.search(text):
        n, unit = int(m[1]), m[2].lower()
        if n and unit == "day" and n < 2:
            return None
        if n:
            start = (_months_back(midnight, n * (12 if unit == "year" else 1)) if unit in ("month", "year")
                     else midnight - timedelta(days=n * (7 if unit == "week" else 1)))
            return Period(start, None, m.group(0))
    if _ALL_YEARS.search(text):
        return Period(None, None, "all years on record")
    return None
