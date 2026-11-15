import React, { useState } from "react";
import { CodeBlock, StatusBadge } from "../../../shared/ui/Primitives.jsx";

export function ToolTraceAccordion({ trace }) {
  const [expanded, setExpanded] = useState(false);
  const isInput = trace.type === "tool_input";

  return (
    <div className="rounded-lg border border-slate-800 bg-[#0B0E14]/90 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="w-full px-3 py-2 flex items-center justify-between text-left hover:bg-slate-800/40 transition"
      >
        <div className="flex items-center gap-2 text-xs">
          <span>{isInput ? "⚡" : "📤"}</span>
          <span className="font-medium text-slate-200">
            {isInput ? "Intent Recognized:" : "Raw Subsystem Output:"}
          </span>
          <code className="px-1.5 py-0.5 rounded bg-slate-800 text-sky-400 font-mono">
            {trace.name}
          </code>
        </div>
        <div className="flex items-center gap-2">
          {trace.elapsedMs != null && (
            <span className="text-[11px] font-mono text-slate-400">
              {trace.elapsedMs}ms
            </span>
          )}
          <StatusBadge variant={isInput ? "info" : "success"}>
            {trace.node || (isInput ? "reasoner" : "tools")}
          </StatusBadge>
          <span className="text-slate-400 text-xs">{expanded ? "▴" : "▾"}</span>
        </div>
      </button>

      {expanded && (
        <div className="p-3 border-t border-slate-800 bg-[#0B0E14]">
          <CodeBlock
            title={
              isInput
                ? `📥 View Generated Input (${trace.name})`
                : `📤 View Raw Output (${trace.name})`
            }
            content={isInput ? trace.args : trace.content}
            language={isInput ? "json" : "text"}
          />
        </div>
      )}
    </div>
  );
}
