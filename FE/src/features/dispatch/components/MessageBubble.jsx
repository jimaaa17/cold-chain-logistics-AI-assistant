import React from "react";
import { ReasoningTimeline } from "./ReasoningTimeline.jsx";

export function MessageBubble({ entry }) {
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
        ) : (
          entry.state === "running" && (
            <div className="text-xs font-mono text-slate-400 animate-pulse">
              Synthesizing telemetry and SOP compliance citations...
            </div>
          )
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
