import React from "react";
import { DESIGN_TOKENS } from "../../shared/config/designTokens.js";
import { StatusBadge } from "../../shared/ui/Primitives.jsx";

export function TelemetryKpiBar() {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {DESIGN_TOKENS.thresholds.map((item) => (
        <div
          key={item.id}
          className="p-3 rounded-xl bg-[#111622] border border-slate-800/90 flex items-center justify-between"
        >
          <div>
            <span className="text-xs text-slate-400 block">{item.label}</span>
            <span className="font-mono text-xs text-slate-200 font-semibold">
              {item.column}
            </span>
          </div>
          <StatusBadge variant={item.severity}>{item.rule}</StatusBadge>
        </div>
      ))}
    </div>
  );
}
