"""Grounding verifier: checks the reasoner's draft against the evidence ledger, in plain code.

An LLM checking its own answer shares its blind spots, so every check here is deterministic:
1. Every cited evidence id ([T1], [W1], [S1]) exists in the ledger.
2. Every number in the draft matches a ledger value, an SOP threshold, a count, or a number from
   the question or an SOP excerpt (at the precision the draft wrote it).
3. Every proposed action (``ACTION[id] on [T3]``) is permitted by a trigger on fresh evidence.
4. Historical readings are not described as active or live.
5. Every trigger on fresh evidence is reported.
"""
from __future__ import annotations

import re
from typing import Any

from agent.evidence import AGGREGATED, FRESH
from agent.rules import ACTION_LABELS, HIGH_STAKES_ACTIONS, THRESHOLDS

ACTION_TAG = re.compile(r"ACTION\[(\w+)\]\s*(?:on|for)?\s*\[([TWS]\d+)\]")
_ALL_CLEAR = re.compile(r"\bno\s+(?:SOP\s+)?actions?\s+(?:is|are)\s+(?:currently\s+)?(?:required|needed)", re.I)
EVIDENCE_ID = re.compile(r"\[([TWS]\d+)\]")

_STRIP_PATTERNS = (
    ACTION_TAG,
    EVIDENCE_ID,
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:\d{2})?"),
    re.compile(r"\b\d{1,2}:\d{2}\b"),
    re.compile(r"§\s*\d+(?:\.\d+)?"),
    re.compile(r"\b(?:section|tier|step|phase)\s+\d+\b", re.I),
    re.compile(r"^\s*\d+[.)]\s", re.M),  # ordered-list markers
)
_NUMBER = re.compile(r"(?<![\w.])([-−]?\d+(?:,\d{3})*(?:\.\d+)?)(\s*%)?")
_ACTIVE_CLAIM = re.compile(
    r"\b(active shipments?|live (?:readings?|shipments?|data)|currently (?:in transit|active)|in transit now)\b", re.I
)
_NEGATION = re.compile(r"\b(no|not|none|never|without)\b[^.\n]{0,40}$", re.I)

MAX_ATTEMPTS = 2  # first draft + exactly one retry


def _numbers_in(text: str) -> list[float]:
    found = []
    for raw, _pct in _NUMBER.findall(text or ""):
        try:
            found.append(float(raw.replace(",", "").replace("−", "-")))
        except ValueError:
            continue
    return found


def allowed_numbers(ledger: list[dict], triggers: list[dict], historical: list[dict], question: str, window_hours: float) -> set[float]:
    allowed: set[float] = set(THRESHOLDS) | {window_hours}
    by_source: dict[str, int] = {}
    for rec in ledger:
        by_source[rec["source"]] = by_source.get(rec["source"], 0) + 1
        for value in (rec.get("fields") or {}).values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                allowed.add(float(value))
        allowed.update(_numbers_in(rec.get("text", "")))
        allowed.update(_numbers_in(rec.get("origin", "")))  # e.g. the weather point's coordinates
        if rec.get("as_of"):  # bare years/months/days of a timestamp ("historical, 2022")
            date_part = rec["as_of"][:10].split("-")
            allowed.update(float(x) for x in date_part if x.isdigit())
    allowed.update(float(n) for n in by_source.values())
    allowed.update({float(len(ledger)), float(len(triggers)), float(len(historical)), float(len(triggers) + len(historical))})
    allowed.update(float(len({t["evidence"] for t in group})) for group in (triggers, historical))
    allowed.update(_numbers_in(question))
    return allowed


def _matches(token: str, is_percent: bool, allowed: set[float]) -> bool:
    value = float(token.replace(",", "").replace("−", "-"))
    decimals = len(token.split(".", 1)[1]) if "." in token else 0
    if is_percent:
        value, decimals = value / 100.0, decimals + 2
    return any(round(a, decimals) == round(value, decimals) for a in allowed)


def verify_draft(
    draft: str,
    *,
    ledger: list[dict],
    triggers: list[dict],
    historical: list[dict],
    question: str,
    window_hours: float,
) -> dict[str, Any]:
    problems: list[str] = []
    ids = {rec["id"] for rec in ledger}

    unknown_ids = sorted(set(EVIDENCE_ID.findall(draft)) - ids)
    if unknown_ids:
        problems.append(f"Cites evidence that does not exist: {', '.join(unknown_ids)}.")

    # Numbers
    stripped = draft
    for pattern in _STRIP_PATTERNS:
        stripped = pattern.sub(" ", stripped)
    allowed = allowed_numbers(ledger, triggers, historical, question, window_hours)
    ungrounded: list[str] = []
    checked = 0
    for token, pct in _NUMBER.findall(stripped):
        checked += 1
        if not _matches(token, bool(pct.strip()), allowed):
            ungrounded.append(token + ("%" if pct.strip() else ""))
    if ungrounded:
        shown = ", ".join(dict.fromkeys(ungrounded[:8]))
        problems.append(f"Numbers not found in the evidence ledger: {shown}. Use only ledger values.")

    # Actions
    permitted = {(t["evidence"], a) for t in triggers for a in t["actions"]}
    proposed: list[dict] = []
    for action, evidence in ACTION_TAG.findall(draft):
        if action not in ACTION_LABELS:
            problems.append(f"Unknown action ACTION[{action}].")
            continue
        if (evidence, action) not in permitted:
            problems.append(
                f"ACTION[{action}] on [{evidence}] is not permitted: no SOP trigger on fresh evidence allows it."
            )
            continue
        proposed.append({"action": action, "evidence": evidence, "high_stakes": action in HIGH_STAKES_ACTIONS})

    # Freshness wording
    telemetry = [r for r in ledger if r["source"] == "telemetry"]
    individual = [r for r in telemetry if r["freshness"] != AGGREGATED]
    if not individual and _ALL_CLEAR.search(draft):
        problems.append(
            "Says no SOP action is required, but no individual telemetry reading was checked. Say that SOP "
            "thresholds were not checked against current readings for this question."
        )
    if individual and not any(r["freshness"] == FRESH for r in individual):
        for match in _ACTIVE_CLAIM.finditer(draft):
            if not _NEGATION.search(draft[max(0, match.start() - 60): match.start()]):
                problems.append(
                    f'Describes historical readings as "{match.group(1)}"; every telemetry reading here is historical.'
                )
                break

    # Coverage of fresh triggers
    cited = set(EVIDENCE_ID.findall(draft))
    missing = sorted({t["evidence"] for t in triggers} - cited)
    if missing:
        problems.append(f"Fresh SOP triggers not reported for: {', '.join(missing)}.")

    return {
        "passed": not problems,
        "problems": problems,
        "numbers_checked": checked,
        "proposed_actions": proposed,
    }
