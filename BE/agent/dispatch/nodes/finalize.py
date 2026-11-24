"""finalize: renders permitted/blocked actions, the verification footer and the dispatcher decision."""
from __future__ import annotations

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.nodes.ledger import PARTIAL_LIST_NOTE, SYNTHETIC_LEFT_OUT_NOTE
from agent.dispatch.state import DispatchState
from agent.evidence import SYNTHETIC, UNKNOWN_SOURCE, source_counts
from agent.rules import ACTION_LABELS
from agent.verifier import ACTION_TAG


def source_notice(ledger: list[dict]) -> str:
    """Written by code, so it can't be dropped by the model."""
    counts = source_counts(ledger)
    total = sum(counts.values())
    if counts.get(SYNTHETIC):
        return f"**Synthetic data:** {counts[SYNTHETIC]} of {total} telemetry records are generated, not real shipments."
    if counts.get(UNKNOWN_SOURCE):
        return "**Data source not stated:** some telemetry records may be synthetic."
    return ""


def render_final(state: DispatchState) -> str:
    draft = state.get("draft", "")
    verification = state.get("verification") or {}
    approval = state.get("approval")
    permitted = {(a["action"], a["evidence"]): a for a in verification.get("proposed_actions", [])}

    def _action(match) -> str:
        action, evidence = match.group(1), match.group(2)
        label = ACTION_LABELS.get(action, action)
        if (action, evidence) not in permitted:
            return f"**Blocked action** (no SOP trigger on fresh data): {label} [{evidence}]"
        if permitted[(action, evidence)]["high_stakes"]:
            status = "awaiting dispatcher approval"
            if approval:
                status = f"{approval['decision']} by {approval['by']}"
            return f"**Proposed action ({status}):** {label} [{evidence}]"
        return f"**Proposed action:** {label} [{evidence}]"

    body = ACTION_TAG.sub(_action, draft).strip()
    notices = [source_notice(state.get("ledger") or [])]
    if PARTIAL_LIST_NOTE in (state.get("notes") or []):
        notices.append("**Partial list:** only the newest matching readings of the period are listed; more may match.")
    if SYNTHETIC_LEFT_OUT_NOTE in (state.get("notes") or []):
        notices.append("**Real readings only:** the period also has synthetic readings, which were left out.")
    if shown := "\n\n".join(n for n in notices if n):
        body = f"{shown}\n\n{body}"
    gaps = state.get("gaps") or []
    gaps_line = "**Not available:** " + "; ".join(gaps) + "." if gaps else ""
    if verification.get("skipped"):
        return f"{body}\n\n---\n{gaps_line}" if gaps_line else body
    footer = ["", "---"]
    if verification.get("degraded"):
        footer.append("**Unverified:** the language model was unavailable, so this answer lists the evidence only.")
    elif verification.get("passed"):
        footer.append(
            f"**Verification:** passed. {verification.get('numbers_checked', 0)} numbers and every citation "
            "were checked against the evidence ledger."
        )
    else:
        footer.append("**Unverified:** the automatic check still failed after one retry. Treat this answer with caution.")
        footer.extend(f"- {p}" for p in verification.get("problems", []))
    if gaps_line:
        footer.append(gaps_line)
    if approval:
        line = f"**Dispatcher decision:** {approval['decision']} by {approval['by']} at {approval['at'][:16].replace('T', ' ')} UTC"
        if approval.get("note"):
            line += f" ({approval['note']})"
        footer.append(line)
        if approval["decision"] == "rejected":
            footer.append("The proposed high-stakes actions must not be executed.")
    return body + "\n" + "\n".join(footer)


def finalize(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
    text = render_final(state)
    v = state.get("verification") or {}
    runtime.stream_writer(
        {
            "type": "final_answer",
            "content": text,
            "verified": bool(v.get("passed")) and not v.get("skipped"),
            "approval": state.get("approval"),
        }
    )
    return {"final": text, "messages": [AIMessage(content=text)]}
