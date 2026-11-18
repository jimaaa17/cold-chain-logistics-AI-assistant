"""action_gate: pauses with interrupt() for high-stakes actions and records the dispatcher's decision."""
from __future__ import annotations

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agent.dispatch.context import DispatchContext
from agent.dispatch.state import DispatchState
from agent.rules import ACTION_LABELS


def action_gate(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    v = state.get("verification") or {}
    high = [a for a in v.get("proposed_actions", []) if a["high_stakes"]]
    if not high:
        return {"approval": None}
    reasons = {
        (t["evidence"], action): f"{t['summary']} ({t['sop_ref']})"
        for t in state.get("triggers", [])
        for action in t["actions"]
    }
    request = {
        "actions": [
            {**a, "label": ACTION_LABELS[a["action"]], "trigger": reasons.get((a["evidence"], a["action"]), "")}
            for a in high
        ],
        "verified": bool(v.get("passed")),
    }
    decision = interrupt(request) or {}
    verdict = str(decision.get("decision", "")).lower()
    return {
        "approval": {
            "decision": "approved" if verdict in {"approve", "approved", "yes"} else "rejected",
            "by": str(decision.get("by") or "dispatcher")[:80],
            "note": str(decision.get("note") or "")[:500],
            "at": runtime.context.clock().isoformat(),
            "actions": high,
        }
    }
