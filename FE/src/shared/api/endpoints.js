import { streamPostSSE } from "./sseClient.js";

const API_BASE = import.meta?.env?.VITE_API_BASE_URL || "";

/**
 * Fetches orchestrator metadata (active LLM & embeddings config).
 */
export async function fetchSystemMeta() {
  const res = await fetch(`${API_BASE}/api/v1/meta`);
  if (!res.ok) throw new Error(`Meta check failed: ${res.status}`);
  return res.json();
}

/**
 * Requests a clean session rotation from the BFF.
 * @param {string} oldSessionId
 */
export async function purgeWorkspaceSession(oldSessionId) {
  const res = await fetch(`${API_BASE}/api/v1/session/purge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_session_id: oldSessionId }),
  });
  if (!res.ok) throw new Error(`Purge failed: ${res.status}`);
  return res.json();
}

/**
 * Streams LangGraph execution steps (`status`, `tool_input`, `tool_output`, `final_answer`, `error`, `done`).
 */
export async function streamDispatchPrompt({ sessionId, prompt, onEvent, signal }) {
  return streamPostSSE(
    `${API_BASE}/api/v1/dispatch/stream`,
    { session_id: sessionId, prompt },
    onEvent,
    signal
  );
}

/**
 * Sends the dispatcher's decision on a paused high-stakes action and streams the resumed run.
 * @param {{ sessionId: string, decision: "approve" | "reject", note?: string, by?: string }} args
 */
export async function streamApprovalDecision({ sessionId, decision, note = "", by = "dispatcher", onEvent, signal }) {
  return streamPostSSE(
    `${API_BASE}/api/v1/dispatch/approve`,
    { session_id: sessionId, decision, note, by },
    onEvent,
    signal
  );
}

/**
 * Authenticates admin credentials server-side and retrieves FDE_VIEWS.AgentAuditLog records.
 */
export async function fetchAuditTrail({
  username,
  password,
  sessionIdFilter = null,
  nodeFilter = "ALL",
  limit = 200,
}) {
  const res = await fetch(`${API_BASE}/api/v1/audit/logs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username,
      password,
      session_id_filter: sessionIdFilter || null,
      node_filter: nodeFilter,
      limit,
    }),
  });

  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.detail || `Audit query failed (${res.status})`);
  }
  return res.json();
}

/**
 * Fetches live Langfuse-style hierarchical trace spans from the dual-sink traceability buffer.
 */
export async function fetchLiveTraces({
  sessionId = null,
  nodeFilter = "ALL",
  limit = 200,
} = {}) {
  const params = new URLSearchParams();
  if (sessionId) params.set("session_id", sessionId);
  if (nodeFilter && nodeFilter !== "ALL") params.set("node", nodeFilter);
  params.set("limit", String(limit));
  const query = params.toString();
  const res = await fetch(`${API_BASE}/api/v1/traces${query ? `?${query}` : ""}`);
  if (!res.ok) throw new Error(`Trace fetch failed (${res.status})`);
  return res.json();
}
