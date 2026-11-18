"""Prompt text for the dispatch workflow, read from `agent/prompts/*.txt` (no import-time I/O)."""
from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

REASONER_FALLBACK = "Answer only from the evidence ledger and cite ids."
PLANNER_FALLBACK = (
    "You plan data retrieval for a cold-chain control tower. Call the tools needed to answer the "
    "question, in parallel where possible. Do not answer the question yourself."
)
CLARIFY_FALLBACK = "I can answer questions about refrigerated shipments, port congestion and SOP compliance."


def load_prompt(name: str, fallback: str, *, strip: bool = False) -> str:
    try:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return fallback
    return text.strip() if strip else text


def reasoner_prompt() -> str:
    return load_prompt("reasoner_prompt.txt", REASONER_FALLBACK)


def planner_prompt() -> str:
    return load_prompt("planner_prompt.txt", PLANNER_FALLBACK, strip=True)


def clarify_text() -> str:
    return load_prompt("clarify_text.txt", CLARIFY_FALLBACK, strip=True)
