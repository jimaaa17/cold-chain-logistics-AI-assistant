"""finalize: renders permitted/blocked actions, the verification footer and the dispatcher decision."""
from __future__ import annotations

from langchain_core.messages import AIMessage

from agent.dispatch.events import emit
from agent.dispatch.state import DispatchState
from agent.rules import ACTION_LABELS
from agent.verifier import ACTION_TAG


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
    if verification.get("skipped"):
        return body
    footer = ["", "---"]
    if verification.get("passed"):
        footer.append(
            f"**Verification:** passed. {verification.get('numbers_checked', 0)} numbers and every citation "
            "were checked against the evidence ledger."
        )
    else:
        footer.append("**Unverified:** the automatic check still failed after one retry. Treat this answer with caution.")
        footer.extend(f"- {p}" for p in verification.get("problems", []))
    if approval:
        line = f"**Dispatcher decision:** {approval['decision']} by {approval['by']} at {approval['at'][:16].replace('T', ' ')} UTC"
        if approval.get("note"):
            line += f" ({approval['note']})"
        footer.append(line)
        if approval["decision"] == "rejected":
            footer.append("The proposed high-stakes actions must not be executed.")
    return body + "\n" + "\n".join(footer)


def finalize(state: DispatchState) -> dict:
    text = render_final(state)
    v = state.get("verification") or {}
    emit(
        {
            "type": "final_answer",
            "content": text,
            "verified": bool(v.get("passed")) and not v.get("skipped"),
            "approval": state.get("approval"),
        }
    )
    return {"final": text, "messages": [AIMessage(content=text)]}
