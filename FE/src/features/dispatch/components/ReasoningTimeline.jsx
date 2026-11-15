import React, { useState } from "react";
import { ToolTraceAccordion } from "./ToolTraceAccordion.jsx";
import { StatusBadge } from "../../../shared/ui/Primitives.jsx";

export function ReasoningTimeline({ traces = [], statusLabel, state, elapsedMs, traceId }) {
  const [open, setOpen] = useState(true);

  if (!traces.length && state !== "running") return null;

  const badgeVariant =
    state === "running" ? "warning" : state === "error" ? "critical" : "success";

  return (
    <div className="rounded-xl border border-slate-800 bg-[#111622]/90 overflow-hidden mb-3">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="w-full px-3.5 py-2.5 flex items-center justify-between bg-slate-900/60 hover:bg-slate-900 transition"
      >
        <div className="flex items-center gap-2.5 text-xs">
          <span
            className={`w-2 h-2 rounded-full ${
              state === "running"
                ? "bg-amber-400 animate-pulse"
                : state === "error"
                ? "bg-rose-400"
                : "bg-emerald-400"
            }`}
          />
          <span className="font-semibold text-slate-200">
            {statusLabel || "Incident Matrix Evaluation Complete"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {traceId && (
            <span className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-sky-300">
              {traceId}
            </span>
          )}
          <StatusBadge variant={badgeVariant}>
            {traces.length} {traces.length === 1 ? "trace" : "traces"}
          </StatusBadge>
          {elapsedMs > 0 && (
            <span className="font-mono text-[11px] text-slate-400">
              {elapsedMs}ms
            </span>
          )}
          <span className="text-xs text-slate-400">{open ? "▴" : "▾"}</span>
        </div>
      </button>

      {open && traces.length > 0 && (
        <div className="p-3 space-y-2 border-t border-slate-800/80">
          {traces.map((trace, index) => (
            <ToolTraceAccordion key={`${trace.name}-${index}`} trace={trace} />
          ))}
        </div>
      )}
    </div>
  );
}
