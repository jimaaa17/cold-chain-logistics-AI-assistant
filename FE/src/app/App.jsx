import React, { useState, useEffect, useCallback } from "react";
import { Sidebar } from "./Sidebar.jsx";
import { DispatchConsole } from "../features/dispatch/components/DispatchConsole.jsx";
import { AuditLogView } from "../features/audit/components/AuditLogView.jsx";
import { useDispatchStream } from "../features/dispatch/hooks/useDispatchStream.js";
import { fetchSystemMeta, purgeWorkspaceSession } from "../shared/api/endpoints.js";

function getInitialSessionId() {
  const saved = window.sessionStorage?.getItem("fde_thread_id");
  if (saved) return saved;
  const generated =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `session-${Date.now()}`;
  window.sessionStorage?.setItem("fde_thread_id", generated);
  return generated;
}

export default function App() {
  const [activeMode, setActiveMode] = useState("dispatch");
  const [sessionId, setSessionId] = useState(getInitialSessionId);
  const [agentLlm, setAgentLlm] = useState("GEMINI");

  const {
    messages,
    isStreaming,
    sendPrompt,
    stopStream,
    clearMessages,
  } = useDispatchStream(sessionId);

  useEffect(() => {
    fetchSystemMeta()
      .then((meta) => {
        if (meta?.agent_llm) setAgentLlm(meta.agent_llm);
      })
      .catch(() => {
        // Fallback when running UI standalone without BFF server active
      });
  }, []);

  const handlePurgeSession = useCallback(async () => {
    clearMessages();
    try {
      const res = await purgeWorkspaceSession(sessionId);
      if (res?.session_id) {
        setSessionId(res.session_id);
        window.sessionStorage?.setItem("fde_thread_id", res.session_id);
        return;
      }
    } catch {
      // Offline fallback rotation
    }
    const nextId =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `session-${Date.now()}`;
    setSessionId(nextId);
    window.sessionStorage?.setItem("fde_thread_id", nextId);
  }, [sessionId, clearMessages]);

  return (
    <div className="min-h-screen flex bg-[#0B0E14] text-[#E2E8F0]">
      <Sidebar
        activeMode={activeMode}
        onSelectMode={setActiveMode}
        sessionId={sessionId}
        agentLlm={agentLlm}
        onPurgeSession={handlePurgeSession}
      />
      <main className="flex-1 p-6 md:p-8 overflow-y-auto">
        {activeMode === "dispatch" ? (
          <DispatchConsole
            messages={messages}
            isStreaming={isStreaming}
            onSendPrompt={sendPrompt}
            onStopStream={stopStream}
          />
        ) : (
          <AuditLogView currentSessionId={sessionId} />
        )}
      </main>
    </div>
  );
}
