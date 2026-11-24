/**
 * Pure reducer: applies one SSE event from the dispatch workflow to an assistant message.
 * Shared by the question stream and the approval stream; unit-tested without a browser.
 *
 * Events: status, tool_input, tool_output, evidence, draft_start, token, verification,
 * degraded, model_decision (audit only, ignored here), approval_required, approval_recorded, final_answer, error, done.
 */
export function applyDispatchEvent(message, { type, data = {} }) {
  const m = data.simulated ? { ...message, simulated: true } : message;
  const base = {
    ...m,
    traceId: data.trace_id || m.traceId,
    elapsedMs: data.elapsed_ms ?? m.elapsedMs,
  };

  switch (type) {
    case "status":
      return { ...base, statusLabel: data.label || base.statusLabel };

    case "tool_input":
    case "tool_output":
      return {
        ...base,
        statusLabel:
          type === "tool_input" ? `Fetching data (${data.name})...` : `Received data (${data.name})`,
        traces: [
          ...(base.traces || []),
          {
            type,
            node: data.node || (type === "tool_input" ? "reasoner" : "tools"),
            name: data.name,
            args: data.args,
            content: data.content,
            elapsedMs: data.elapsed_ms,
            traceId: data.trace_id,
            spanId: data.span_id,
            simulated: Boolean(data.simulated),
          },
        ],
      };

    case "evidence":
      return {
        ...base,
        evidence: data,
        statusLabel: `Evidence ledger: ${data.records ?? 0} records, ${(data.triggers || []).length} SOP triggers`,
      };

    case "draft_start":
      return {
        ...base,
        draft: "",
        attempt: data.attempt || 1,
        statusLabel: data.fallback
          ? "Model unavailable; showing the evidence only"
          : data.attempt > 1
          ? "Rewriting the answer after a failed check..."
          : "Writing the answer...",
      };

    case "token":
      return { ...base, draft: (base.draft || "") + (data.text || "") };

    case "verification":
      return {
        ...base,
        verification: data,
        statusLabel: data.passed
          ? "Checked against the evidence ledger"
          : data.retrying
          ? "Check failed; retrying once"
          : "Check failed; answer will be marked unverified",
      };

    case "degraded":
      // A model step fell back to code; the final answer's footer lists it too.
      return {
        ...base,
        degraded: [...(base.degraded || []), { step: data.step, fallback: data.fallback, reason: data.reason }],
        statusLabel: `${data.step} unavailable; using ${data.fallback}`,
      };

    case "approval_required":
      return {
        ...base,
        approval: { state: "pending", actions: data.actions || [], verified: data.verified },
        statusLabel: "Awaiting dispatcher approval",
      };

    case "approval_recorded":
      return {
        ...base,
        approval: { ...(base.approval || {}), state: "submitted", decision: data.decision, by: data.by },
        statusLabel: data.decision === "approve" ? "Approval recorded" : "Rejection recorded",
      };

    case "final_answer":
      return {
        ...base,
        content: data.content,
        draft: "",
        verified: Boolean(data.verified),
        approval: data.approval
          ? { ...(base.approval || {}), state: "decided", ...data.approval }
          : base.approval,
        statusLabel: "Answer ready",
      };

    case "error":
      return {
        ...base,
        state: "error",
        statusLabel: "Execution Error",
        content: base.content || data.message,
      };

    case "done": {
      const state =
        base.state === "error" || data.outcome === "error"
          ? "error"
          : data.outcome === "awaiting_approval"
          ? "awaiting"
          : "complete";
      return {
        ...base,
        state,
        statusLabel: data.label || base.statusLabel,
        // A paused run has no final answer yet: keep the checked draft visible.
        content: base.content || (state === "awaiting" ? base.draft : base.content),
      };
    }

    default:
      return base;
  }
}
