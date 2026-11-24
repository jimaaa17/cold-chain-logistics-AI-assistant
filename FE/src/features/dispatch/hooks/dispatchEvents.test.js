import { describe, expect, it } from "vitest";
import { applyDispatchEvent } from "./dispatchEvents.js";

const fresh = () => ({ id: "m1", role: "assistant", content: "", draft: "", traces: [], state: "running" });
const play = (events, start = fresh()) =>
  events.reduce((msg, [type, data]) => applyDispatchEvent(msg, { type, data }), start);

describe("applyDispatchEvent", () => {
  it("streams draft tokens, then replaces them with the final answer", () => {
    const msg = play([
      ["draft_start", { attempt: 1 }],
      ["token", { text: "Reading " }],
      ["token", { text: "[T1] is 6.8 °C" }],
    ]);
    expect(msg.draft).toBe("Reading [T1] is 6.8 °C");
    expect(msg.content).toBe("");

    const done = play(
      [
        ["verification", { passed: true, numbers_checked: 3 }],
        ["final_answer", { content: "Final text", verified: true }],
        ["done", { outcome: "complete", label: "Incident Matrix Evaluation Complete" }],
      ],
      msg
    );
    expect(done.content).toBe("Final text");
    expect(done.draft).toBe("");
    expect(done.verified).toBe(true);
    expect(done.state).toBe("complete");
  });

  it("resets the draft when the verifier sends it back for a retry", () => {
    const msg = play([
      ["draft_start", { attempt: 1 }],
      ["token", { text: "bad 8.5" }],
      ["verification", { passed: false, retrying: true, problems: ["8.5"] }],
      ["draft_start", { attempt: 2 }],
      ["token", { text: "good" }],
    ]);
    expect(msg.draft).toBe("good");
    expect(msg.attempt).toBe(2);
  });

  it("pauses for approval, keeps the draft visible, then records the decision", () => {
    const paused = play([
      ["draft_start", { attempt: 1 }],
      ["token", { text: "Divert [T1]" }],
      ["approval_required", { actions: [{ action: "divert_inland_empire", evidence: "T1", label: "Divert" }], verified: true }],
      ["done", { outcome: "awaiting_approval", label: "Awaiting dispatcher approval" }],
    ]);
    expect(paused.state).toBe("awaiting");
    expect(paused.approval.state).toBe("pending");
    expect(paused.content).toBe("Divert [T1]");

    const decided = play(
      [
        ["approval_recorded", { decision: "approve", by: "dana" }],
        ["final_answer", { content: "Approved text", verified: true, approval: { decision: "approved", by: "dana" } }],
        ["done", { outcome: "complete" }],
      ],
      { ...paused, content: "", state: "running" }
    );
    expect(decided.approval.state).toBe("decided");
    expect(decided.approval.decision).toBe("approved");
    expect(decided.content).toBe("Approved text");
  });

  it("marks simulated demo output and keeps tool traces", () => {
    const msg = play([["tool_input", { name: "query_telemetry_db", args: { sql_query: "SELECT 1" }, simulated: true }]]);
    expect(msg.simulated).toBe(true);
    expect(msg.traces).toHaveLength(1);
    expect(msg.traces[0].simulated).toBe(true);
  });

  it("an error ends the run in the error state", () => {
    const msg = play([
      ["error", { message: "Orchestrator Runtime Error: boom" }],
      ["done", { outcome: "error" }],
    ]);
    expect(msg.state).toBe("error");
    expect(msg.content).toContain("boom");
  });

  it("records degraded steps and clears a draft replaced by the evidence-only fallback", () => {
    const step = { step: "reasoner", fallback: "evidence-only answer", reason: "AuthenticationError" };
    const msg = play([
      ["draft_start", { attempt: 1 }],
      ["token", { text: "partial" }],
      ["degraded", step],
      ["draft_start", { attempt: 1, fallback: true }],
    ]);
    expect(msg.degraded).toEqual([step]);
    expect(msg.draft).toBe("");
    expect(msg.statusLabel).toBe("Model unavailable; showing the evidence only");
  });
});
