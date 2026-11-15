import React, { useState, useEffect } from "react";
import { useAuditLogs } from "../hooks/useAuditLogs.js";
import { CodeBlock, StatusBadge } from "../../../shared/ui/Primitives.jsx";

export function AuditLogView({ currentSessionId }) {
  const {
    rows,
    liveSpans,
    observability,
    authenticated,
    loading,
    error,
    credentials,
    loadLogs,
    loadLiveTraces,
  } = useAuditLogs();

  const [viewTab, setViewTab] = useState("waterfall"); // "waterfall" | "sql"
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [nodeFilter, setNodeFilter] = useState("ALL");
  const [onlyCurrentSession, setOnlyCurrentSession] = useState(false);
  const [selectedRow, setSelectedRow] = useState(null);

  useEffect(() => {
    loadLiveTraces({
      sessionId: onlyCurrentSession ? currentSessionId : null,
      nodeFilter,
    });
  }, [loadLiveTraces, currentSessionId, onlyCurrentSession, nodeFilter]);

  const handleAuthSubmit = (e) => {
    e.preventDefault();
    loadLogs({
      username,
      password,
      sessionIdFilter: onlyCurrentSession ? currentSessionId : null,
      nodeFilter,
    });
  };

  const handleRefresh = (nextNode = nodeFilter, nextSessionOnly = onlyCurrentSession) => {
    loadLiveTraces({
      sessionId: nextSessionOnly ? currentSessionId : null,
      nodeFilter: nextNode,
    });
    if (!authenticated) return;
    loadLogs({
      username: credentials.username,
      password: credentials.password,
      sessionIdFilter: nextSessionOnly ? currentSessionId : null,
      nodeFilter: nextNode,
    });
  };

  const activeItems = viewTab === "waterfall" ? liveSpans : rows;
  const maxLatency = Math.max(
    1,
    ...activeItems.map((item) => Number(item.elapsed_ms || 0))
  );

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <header className="border-b border-slate-800 pb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">
            🛡️ Dual-Sink Agent Traceability &amp; Compliance Ledger
          </h1>
          <p className="text-xs text-slate-400 mt-1">
            Combines <code className="text-sky-400">Langfuse</code> hierarchical LLM/Tool spans with the append-only MSSQL compliance ledger{" "}
            <code className="text-emerald-400">FDE_VIEWS.AgentAuditLog</code> (
            <code className="text-slate-300">GRANT INSERT TO USR_FDE_RO</code>)
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge variant="info">
            RBAC: {observability?.sql_rbac_role || "USR_FDE_RO (INSERT-ONLY)"}
          </StatusBadge>
          <StatusBadge variant={observability?.langfuse_active ? "success" : "warning"}>
            {observability?.langfuse_active
              ? "🔭 Langfuse Cloud Active"
              : "🔭 Langfuse Local Buffer Active"}
          </StatusBadge>
          {authenticated && (
            <StatusBadge variant="success">
              ✅ SQL Admin: {credentials.username}
            </StatusBadge>
          )}
        </div>
      </header>

      {/* View Mode Switcher + Session/Node Filter Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex rounded-xl bg-[#111622] border border-slate-800 p-1 text-xs">
          <button
            type="button"
            onClick={() => setViewTab("waterfall")}
            className={`px-3.5 py-1.5 rounded-lg font-semibold transition ${
              viewTab === "waterfall"
                ? "bg-sky-500 text-slate-950"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            🔭 Langfuse Trace Waterfall ({liveSpans.length})
          </button>
          <button
            type="button"
            onClick={() => setViewTab("sql")}
            className={`px-3.5 py-1.5 rounded-lg font-semibold transition ${
              viewTab === "sql"
                ? "bg-sky-500 text-slate-950"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            🗄️ SQL Server Ledger (FDE_VIEWS.AgentAuditLog)
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-2 text-slate-300 cursor-pointer">
            <input
              type="checkbox"
              checked={onlyCurrentSession}
              onChange={(e) => {
                const checked = e.target.checked;
                setOnlyCurrentSession(checked);
                handleRefresh(nodeFilter, checked);
              }}
              className="accent-sky-400"
            />
            Filter to active session ({currentSessionId.slice(0, 8)}...)
          </label>

          <select
            value={nodeFilter}
            onChange={(e) => {
              const next = e.target.value;
              setNodeFilter(next);
              handleRefresh(next, onlyCurrentSession);
            }}
            className="px-2.5 py-1.5 rounded-lg bg-[#111622] border border-slate-800 text-xs text-slate-200"
          >
            <option value="ALL">All Graph Nodes</option>
            <option value="reasoner">reasoner</option>
            <option value="tools">tools</option>
            <option value="reasoner_final">reasoner_final</option>
          </select>

          <button
            type="button"
            onClick={() => handleRefresh(nodeFilter, onlyCurrentSession)}
            className="px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800/70 hover:bg-slate-800 text-slate-200"
          >
            ↻ Refresh
          </button>
        </div>
      </div>

      {/* Admin Authorization Gate (for SQL Ledger tab) */}
      {viewTab === "sql" && (
        <form
          onSubmit={handleAuthSubmit}
          className="p-5 rounded-2xl bg-[#111622] border border-slate-800 space-y-4"
        >
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-200">
              Database Authorization Gate (setup_security_view.sql)
            </h3>
            <span className="text-xs text-slate-400">
              <code className="text-emerald-400">USR_FDE_RO</code> has <code className="text-amber-300">INSERT</code> only — reading requires{" "}
              <code className="text-sky-400">SQL_ADMIN_USER</code>
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Admin Username
              </label>
              <input
                type="text"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="e.g. sa or USR_FDE_ADMIN"
                className="w-full px-3.5 py-2 rounded-lg bg-[#0B0E14] border border-slate-800 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Admin Password
              </label>
              <input
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••••••"
                className="w-full px-3.5 py-2 rounded-lg bg-[#0B0E14] border border-slate-800 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
          </div>

          <div className="flex justify-end">
            <button
              type="submit"
              disabled={loading}
              className="px-5 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 disabled:opacity-40 text-slate-950 font-semibold text-xs transition"
            >
              {loading ? "Authenticating..." : "Authenticate & Query FDE_VIEWS.AgentAuditLog"}
            </button>
          </div>
        </form>
      )}

      {error && (
        <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs">
          ❌ {error}
        </div>
      )}

      {/* Main Split View: Waterfall or SQL Table + Inspector */}
      {(viewTab === "waterfall" || authenticated) && (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-start">
          <div className="lg:col-span-8 rounded-2xl border border-slate-800 bg-[#111622] overflow-hidden">
            {activeItems.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-400">
                {viewTab === "waterfall"
                  ? "No live trace spans recorded yet. Run a prompt in the Dispatch Console to stream Langfuse + SQL spans."
                  : "No audit logs found matching the filter criteria."}
              </div>
            ) : viewTab === "waterfall" ? (
              <div className="p-4 space-y-2.5 max-h-[540px] overflow-y-auto font-mono text-xs">
                {activeItems.map((span) => {
                  const widthPct = Math.max(
                    8,
                    Math.min(100, Math.round(((span.elapsed_ms || 50) / maxLatency) * 100))
                  );
                  return (
                    <div
                      key={`${span.LogID}-${span.span_id}`}
                      onClick={() => setSelectedRow(span)}
                      className={`p-3 rounded-xl border cursor-pointer transition space-y-1.5 ${
                        selectedRow?.LogID === span.LogID
                          ? "bg-sky-500/10 border-sky-400/50"
                          : "bg-slate-900/70 border-slate-800 hover:border-slate-700"
                      }`}
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <StatusBadge
                            variant={
                              span.NodeExecuted === "tools"
                                ? "success"
                                : span.NodeExecuted === "reasoner_final"
                                ? "info"
                                : "warning"
                            }
                          >
                            {span.NodeExecuted}
                          </StatusBadge>
                          <span className="text-sky-300 font-semibold">{span.ToolName}</span>
                        </div>
                        <div className="flex items-center gap-2 text-[11px] text-slate-400">
                          {span.trace_id && <span className="text-sky-400">{span.trace_id}</span>}
                          <span>{span.elapsed_ms ?? 0} ms</span>
                        </div>
                      </div>
                      <div className="w-full h-1.5 bg-slate-800 rounded overflow-hidden">
                        <div
                          className={`h-full ${
                            span.NodeExecuted === "tools"
                              ? "bg-emerald-400"
                              : span.NodeExecuted === "reasoner_final"
                              ? "bg-sky-400"
                              : "bg-amber-400"
                          }`}
                          style={{ width: `${widthPct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="overflow-x-auto max-h-[540px]">
                <table className="w-full text-left border-collapse text-xs">
                  <thead className="bg-slate-900/90 border-b border-slate-800 text-slate-400 font-mono sticky top-0">
                    <tr>
                      <th className="py-2.5 px-3">ID</th>
                      <th className="py-2.5 px-3">Execution Time</th>
                      <th className="py-2.5 px-3">Trace / Session</th>
                      <th className="py-2.5 px-3">Graph Node</th>
                      <th className="py-2.5 px-3">Tool Triggered</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/70 font-mono">
                    {activeItems.map((row) => (
                      <tr
                        key={row.LogID}
                        onClick={() => setSelectedRow(row)}
                        className={`cursor-pointer hover:bg-slate-800/50 transition ${
                          selectedRow?.LogID === row.LogID ? "bg-sky-500/10" : ""
                        }`}
                      >
                        <td className="py-2.5 px-3 text-slate-300">#{row.LogID}</td>
                        <td className="py-2.5 px-3 text-slate-400">{row.Timestamp}</td>
                        <td className="py-2.5 px-3 text-slate-400">
                          {row.trace_id || `${String(row.SessionID || "").slice(0, 8)}...`}
                        </td>
                        <td className="py-2.5 px-3">
                          <StatusBadge
                            variant={
                              row.NodeExecuted === "tools"
                                ? "success"
                                : row.NodeExecuted === "reasoner_final"
                                ? "info"
                                : "warning"
                            }
                          >
                            {row.NodeExecuted}
                          </StatusBadge>
                        </td>
                        <td className="py-2.5 px-3 text-sky-300">{row.ToolName}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Side Payload Inspector */}
          <div className="lg:col-span-4 rounded-2xl border border-slate-800 bg-[#111622] p-4 space-y-3">
            <h4 className="text-xs font-semibold text-slate-200 uppercase tracking-wider">
              Span &amp; SQL Payload Inspector
            </h4>
            {selectedRow ? (
              <div className="space-y-2 text-xs">
                <div className="space-y-1 text-slate-400 font-mono border-b border-slate-800 pb-2">
                  <div className="flex justify-between">
                    <span>LogID:</span>
                    <span className="text-slate-200">#{selectedRow.LogID}</span>
                  </div>
                  {selectedRow.trace_id && (
                    <div className="flex justify-between">
                      <span>Trace ID:</span>
                      <span className="text-sky-400">{selectedRow.trace_id}</span>
                    </div>
                  )}
                  {selectedRow.span_id && (
                    <div className="flex justify-between">
                      <span>Span ID:</span>
                      <span className="text-sky-400">{selectedRow.span_id}</span>
                    </div>
                  )}
                  {selectedRow.sink_status && (
                    <div className="flex justify-between">
                      <span>Sinks:</span>
                      <span className="text-emerald-300">{selectedRow.sink_status}</span>
                    </div>
                  )}
                </div>
                <CodeBlock
                  title={selectedRow.ToolName}
                  content={selectedRow.Content}
                  language="text"
                />
              </div>
            ) : (
              <p className="text-xs text-slate-400">
                Select any trace span or SQL audit row to inspect its raw SQL query,
                tool arguments, trace metadata, or LLM synthesis payload.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
