import React from "react";

export function Sidebar({
  activeMode,
  onSelectMode,
  sessionId,
  agentLlm,
  onPurgeSession,
}) {
  return (
    <aside className="w-72 bg-[#111622] border-r border-[#1E293B] p-5 flex flex-col justify-between shrink-0">
      <div className="space-y-6">
        {/* Brand Header */}
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-xl bg-sky-500/15 border border-sky-400/30 flex items-center justify-center text-2xl">
            🧊
          </div>
          <div>
            <h2 className="text-base font-bold text-slate-100">
              FDE Command Center
            </h2>
            <span className="text-[11px] font-mono text-sky-400">
              Enterprise Control Tower
            </span>
          </div>
        </div>

        {/* System Mode Navigation */}
        <div className="space-y-1.5">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 px-1">
            System Mode
          </span>
          <button
            type="button"
            onClick={() => onSelectMode("dispatch")}
            className={`w-full text-left px-3.5 py-2.5 rounded-xl text-xs font-medium transition flex items-center gap-2.5 ${
              activeMode === "dispatch"
                ? "bg-sky-500/15 text-sky-300 border border-sky-500/40"
                : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 border border-transparent"
            }`}
          >
            <span>🧊</span>
            <span>Dispatch Console</span>
          </button>

          <button
            type="button"
            onClick={() => onSelectMode("audit")}
            className={`w-full text-left px-3.5 py-2.5 rounded-xl text-xs font-medium transition flex items-center gap-2.5 ${
              activeMode === "audit"
                ? "bg-sky-500/15 text-sky-300 border border-sky-500/40"
                : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 border border-transparent"
            }`}
          >
            <span>🛡️</span>
            <span>Security &amp; Audit Logs</span>
          </button>
        </div>

        {/* Session & Reasoning Metadata */}
        <div className="pt-4 border-t border-slate-800 space-y-2.5 text-xs">
          <div className="flex items-center justify-between text-slate-400">
            <span>Session Token:</span>
            <code className="px-2 py-0.5 rounded bg-[#1E293B] text-[#38BDF8] font-mono">
              {sessionId.slice(0, 8)}...
            </code>
          </div>
          <div className="flex items-center justify-between text-slate-400">
            <span>Reasoning LLM:</span>
            <code className="px-2 py-0.5 rounded bg-[#1E293B] text-[#38BDF8] font-mono">
              {agentLlm}
            </code>
          </div>
        </div>
      </div>

      {/* Workspace Purge Action */}
      <div className="pt-4 border-t border-slate-800">
        <button
          type="button"
          onClick={onPurgeSession}
          className="w-full py-2.5 px-3 rounded-xl border border-rose-500/30 bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 text-xs font-medium transition"
        >
          🗑️ Purge Dispatch Workspace Session
        </button>
      </div>
    </aside>
  );
}
