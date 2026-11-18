import React, { useState } from "react";
import { ReasoningTimeline } from "./ReasoningTimeline.jsx";

function ApprovalPanel({ approval, disabled, onDecide }) {
  const [note, setNote] = useState("");
  if (!approval || !approval.actions?.length) return null;
  const pending = approval.state === "pending";

  return (
    <section
      aria-label="Dispatcher approval"
      data-testid="approval-panel"
      className="mt-3 rounded-lg border border-slate-700 px-3.5 py-3 space-y-2.5"
    >
      <div className="text-xs font-semibold text-slate-100">
        {pending ? "Dispatcher approval required" : "Dispatcher decision"}
      </div>
      <ul className="space-y-1.5">
        {approval.actions.map((a) => (
          <li key={`${a.action}-${a.evidence}`} className="text-xs text-slate-300">
            <span className="text-slate-100">{a.label}</span>{" "}
            <span className="font-mono text-slate-400">[{a.evidence}]</span>
            {a.trigger && <div className="text-slate-400">Because: {a.trigger}</div>}
          </li>
        ))}
      </ul>
      {pending && approval.verified === false && (
        <p className="text-xs text-amber-300">
          The answer failed the automatic check. Review the evidence before approving.
        </p>
      )}
      {pending ? (
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="approval-note" className="sr-only">
            Note
          </label>
          <input
            id="approval-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Note (optional)"
            className="flex-1 min-w-[10rem] rounded-md bg-slate-900 border border-slate-700 px-2.5 py-1.5 text-xs text-slate-200"
          />
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecide("approve", note)}
            className="rounded-md bg-sky-600 hover:bg-sky-500 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
          >
            Approve
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecide("reject", note)}
            className="rounded-md border border-slate-600 hover:border-slate-400 px-3 py-1.5 text-xs font-semibold text-slate-200 disabled:opacity-50"
          >
            Reject
          </button>
        </div>
      ) : (
        <p className="text-xs text-slate-300">
          {approval.state === "submitted"
            ? "Decision sent..."
            : `${approval.decision === "approved" ? "Approved" : "Rejected"} by ${approval.by}`}
        </p>
      )}
    </section>
  );
}

function VerificationChip({ entry }) {
  if (entry.role === "user" || entry.state === "running" || !entry.verification) return null;
  const passed = entry.verified ?? entry.verification.passed;
  return (
    <div className="mt-3 flex items-center gap-2 text-[11px] text-slate-400">
      <span className={`w-1.5 h-1.5 rounded-full ${passed ? "bg-emerald-400" : "bg-amber-400"}`} />
      {passed
        ? `Checked against the evidence ledger (${entry.verification.numbers_checked ?? 0} numbers)`
        : "Unverified: the automatic check failed after one retry"}
    </div>
  );
}

export function MessageBubble({ entry, isStreaming = false, onDecideApproval }) {
  const isUser = entry.role === "user";

  return (
    <div
      className={`flex gap-3.5 ${
        isUser ? "justify-end" : "justify-start"
      }`}
    >
      {!isUser && (
        <div className="w-8 h-8 rounded-lg bg-sky-500/15 border border-sky-400/30 flex items-center justify-center text-sm shrink-0">
          🤖
        </div>
      )}

      <div
        className={`max-w-3xl w-full rounded-2xl p-4 border ${
          isUser
            ? "bg-sky-500/15 border-sky-500/30 text-slate-100 max-w-xl"
            : "bg-[#111622] border-slate-800 text-slate-200"
        }`}
      >
        {!isUser && entry.simulated && (
          <div
            role="alert"
            data-testid="simulated-banner"
            className="mb-3 rounded-lg border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-xs font-mono text-amber-300"
          >
            ⚠️ OFFLINE DEMO: canned sample data (AGENT_OFFLINE_DEMO). Not live telemetry or SOP retrieval.
          </div>
        )}

        {!isUser && (
          <ReasoningTimeline
            traces={entry.traces || []}
            statusLabel={entry.statusLabel}
            state={entry.state}
            elapsedMs={entry.elapsedMs}
            traceId={entry.traceId}
          />
        )}

        {entry.content ? (
          <div className="text-sm leading-relaxed whitespace-pre-wrap">
            {entry.content}
          </div>
        ) : entry.draft ? (
          <div data-testid="draft" className="text-sm leading-relaxed whitespace-pre-wrap text-slate-400">
            <div className="mb-1 text-[11px] font-mono uppercase tracking-wide text-slate-500">
              Draft, being checked against the data
            </div>
            {entry.draft}
          </div>
        ) : (
          entry.state === "running" && (
            <div className="text-xs font-mono text-slate-400 animate-pulse">
              Gathering telemetry, weather and SOP evidence...
            </div>
          )
        )}

        {!isUser && <VerificationChip entry={entry} />}

        {!isUser && (
          <ApprovalPanel
            approval={entry.approval}
            disabled={isStreaming}
            onDecide={(decision, note) => onDecideApproval?.(entry.id, decision, note)}
          />
        )}
      </div>

      {isUser && (
        <div className="w-8 h-8 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center text-sm shrink-0">
          👤
        </div>
      )}
    </div>
  );
}
