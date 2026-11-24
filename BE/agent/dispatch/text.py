"""Small text and value helpers shared by the dispatch nodes and the streaming adapter."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


def plain(value: Any) -> Any:
    """Makes a SQL value JSON-friendly (Decimal -> float, datetime -> ISO string)."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def text_of(content: Any, *, strip: bool = True) -> str:
    """Flattens message content (a string or Gemini-style content blocks) to text."""
    if isinstance(content, str):
        return content.strip() if strip else content
    if isinstance(content, list):
        parts = [
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, str) or (isinstance(item, dict) and item.get("type") == "text")
        ]
        joined = "".join(parts)
        return joined.strip() if strip else joined
    return str(content or "")


def history(state: dict, limit: int = 4) -> list[BaseMessage]:
    msgs = [m for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage))]
    return msgs[:-1][-limit:]  # earlier turns only; the current question is passed with the ledger
