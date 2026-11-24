import React, { useState } from "react";
import { DESIGN_TOKENS } from "../../../shared/config/designTokens.js";
import { TelemetryKpiBar } from "../../telemetry/TelemetryKpiBar.jsx";
import { MessageBubble } from "./MessageBubble.jsx";

export function DispatchConsole({
  messages,
  isStreaming,
  onSendPrompt,
  onDecideApproval,
  onStopStream,
}) {
  const [input, setInput] = useState("");

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;
    onSendPrompt(input);
    setInput("");
  };

  return (
    <div className="flex flex-col h-full max-w-6xl mx-auto space-y-5">
      {/* Header & Cold-Chain Threshold Gates */}
      <header className="space-y-4 border-b border-slate-800 pb-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-2xl font-bold text-slate-100">
              Cold-Chain Incident Control Dashboard
            </h1>
            <p className="text-xs text-slate-400">
              Production Data Engineering Pipeline • Real-Time Decision Optimization Platform
            </p>
          </div>
        </div>
        <TelemetryKpiBar />
      </header>

      {/* Quick Operator Prompt Chips */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-mono text-slate-400">Quick Dispatch:</span>
        {DESIGN_TOKENS.quickPrompts.map((chip) => (
          <button
            key={chip.id}
            type="button"
            disabled={isStreaming}
            onClick={() => onSendPrompt(chip.prompt)}
            className="px-3 py-1.5 rounded-lg bg-[#111622] hover:bg-slate-800/80 border border-slate-800 hover:border-sky-500/40 text-xs text-slate-200 transition disabled:opacity-50"
          >
            {chip.icon} {chip.label}
          </button>
        ))}
      </div>

      {/* Conversation & Trace Stream Feed */}
      <div className="flex-1 overflow-y-auto space-y-4 pr-1 min-h-[340px]">
        {messages.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-800 bg-[#111622]/50 p-8 text-center space-y-2">
            <div className="text-2xl">🧊</div>
            <h3 className="text-sm font-semibold text-slate-200">
              Control Tower Dispatch Ready
            </h3>
            <p className="text-xs text-slate-400 max-w-md mx-auto">
              Query active fleet telemetry (<code className="text-sky-400">VW_ACTIVE_FLEET</code>),
              live corridor weather, or Pinecone compliance SOPs. Every reasoning node and
              tool trace streams live and logs to <code className="text-sky-400">FDE_VIEWS.AgentAuditLog</code>.
            </p>
          </div>
        ) : (
          messages.map((entry) => (
            <MessageBubble
              key={entry.id}
              entry={entry}
              isStreaming={isStreaming}
              onDecideApproval={onDecideApproval}
            />
          ))
        )}
      </div>

      {/* Operator Input Bar */}
      <form
        onSubmit={handleSubmit}
        className="pt-2 border-t border-slate-800 flex items-center gap-2.5"
      >
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Query fleet telemetry, corridor updates, or compliance thresholds..."
          className="flex-1 px-4 py-3 rounded-xl bg-[#111622] border border-slate-800 focus:border-sky-400 focus:outline-none text-sm text-slate-100 placeholder-slate-500"
        />
        {isStreaming ? (
          <button
            type="button"
            onClick={onStopStream}
            className="px-4 py-3 rounded-xl bg-rose-500/20 border border-rose-500/40 text-rose-300 text-xs font-semibold hover:bg-rose-500/30 transition"
          >
            ⏹ Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!input.trim()}
            className="px-5 py-3 rounded-xl bg-sky-500 hover:bg-sky-400 disabled:opacity-40 text-slate-950 font-semibold text-xs transition"
          >
            Dispatch ↵
          </button>
        )}
      </form>
    </div>
  );
}
