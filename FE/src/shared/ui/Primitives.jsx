import React, { useState } from "react";

export function StatusBadge({ variant = "info", children }) {
  const styles = {
    info: "bg-sky-500/15 text-sky-300 border-sky-500/30",
    success: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
    warning: "bg-amber-500/15 text-amber-300 border-amber-500/30",
    critical: "bg-rose-500/15 text-rose-300 border-rose-500/30",
    neutral: "bg-slate-800 text-slate-300 border-slate-700",
  };

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-mono border ${
        styles[variant] || styles.neutral
      }`}
    >
      {children}
    </span>
  );
}

export function CodeBlock({ title, content, language = "json" }) {
  const [copied, setCopied] = useState(false);
  const formatted =
    typeof content === "string" ? content : JSON.stringify(content, null, 2);

  const handleCopy = () => {
    navigator.clipboard?.writeText(formatted);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-[#0B0E14] overflow-hidden text-xs">
      <div className="flex items-center justify-between px-3 py-1.5 bg-slate-900/80 border-b border-slate-800 text-slate-400 font-mono">
        <span>{title || language.toUpperCase()}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="text-[11px] text-sky-400 hover:text-sky-300 transition"
        >
          {copied ? "✓ Copied" : "Copy"}
        </button>
      </div>
      <pre className="p-3 font-mono text-xs text-sky-300 overflow-x-auto whitespace-pre-wrap leading-relaxed">
        {formatted}
      </pre>
    </div>
  );
}
