import { useState, useRef, useCallback } from "react";
import { streamDispatchPrompt } from "../../../shared/api/endpoints.js";

/**
 * Custom hook managing multi-turn conversation history and real-time LangGraph SSE execution traces.
 */
export function useDispatchStream(sessionId) {
  const [messages, setMessages] = useState([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [activeStatus, setActiveStatus] = useState(null);
  const abortControllerRef = useRef(null);

  const sendPrompt = useCallback(
    async (promptText) => {
      const trimmed = (promptText || "").trim();
      if (!trimmed || isStreaming) return;

      const assistantMsgId = `msg-${Date.now()}`;
      const userEntry = {
        id: `user-${Date.now()}`,
        role: "user",
        content: trimmed,
        timestamp: new Date().toISOString(),
      };

      const placeholderAssistant = {
        id: assistantMsgId,
        role: "assistant",
        content: "",
        traces: [],
        statusLabel: "🧠 Initializing Core Reasoner Node...",
        state: "running",
        elapsedMs: 0,
        timestamp: new Date().toISOString(),
      };

      setMessages((prev) => [...prev, userEntry, placeholderAssistant]);
      setIsStreaming(true);
      setActiveStatus("🧠 Initializing Core Reasoner Node...");

      const controller = new AbortController();
      abortControllerRef.current = controller;

      try {
        await streamDispatchPrompt({
          sessionId,
          prompt: trimmed,
          signal: controller.signal,
          onEvent: ({ type, data }) => {
            setMessages((prev) =>
              prev.map((current) => {
                if (current.id !== assistantMsgId) return current;
                // Sticky: once any frame is simulated (AGENT_OFFLINE_DEMO), the whole reply is.
                const m = data?.simulated ? { ...current, simulated: true } : current;

                if (type === "status") {
                  setActiveStatus(data.label);
                  return {
                    ...m,
                    statusLabel: data.label,
                    traceId: data.trace_id || m.traceId,
                  };
                }

                if (type === "tool_input") {
                  const nextLabel = `🧠 Agent generated tool parameters (${data.name})...`;
                  setActiveStatus(nextLabel);
                  return {
                    ...m,
                    statusLabel: nextLabel,
                    traceId: data.trace_id || m.traceId,
                    elapsedMs: data.elapsed_ms ?? m.elapsedMs,
                    traces: [
                      ...m.traces,
                      {
                        type: "tool_input",
                        simulated: Boolean(data.simulated),
                        node: data.node || "reasoner",
                        name: data.name,
                        args: data.args,
                        elapsedMs: data.elapsed_ms,
                        traceId: data.trace_id,
                        spanId: data.span_id,
                      },
                    ],
                  };
                }

                if (type === "tool_output") {
                  const nextLabel = `🔧 Executed subsystem tool (${data.name})...`;
                  setActiveStatus(nextLabel);
                  return {
                    ...m,
                    statusLabel: nextLabel,
                    traceId: data.trace_id || m.traceId,
                    elapsedMs: data.elapsed_ms ?? m.elapsedMs,
                    traces: [
                      ...m.traces,
                      {
                        type: "tool_output",
                        simulated: Boolean(data.simulated),
                        node: data.node || "tools",
                        name: data.name,
                        content: data.content,
                        elapsedMs: data.elapsed_ms,
                        traceId: data.trace_id,
                        spanId: data.span_id,
                      },
                    ],
                  };
                }

                if (type === "final_answer") {
                  const nextLabel = "📝 Generating Operational Resolution Report...";
                  setActiveStatus(nextLabel);
                  return {
                    ...m,
                    statusLabel: nextLabel,
                    content: data.content,
                    traceId: data.trace_id || m.traceId,
                    elapsedMs: data.elapsed_ms ?? m.elapsedMs,
                  };
                }

                if (type === "error") {
                  return {
                    ...m,
                    state: "error",
                    statusLabel: "Execution Error",
                    content: m.content || data.message,
                  };
                }

                if (type === "done") {
                  setActiveStatus(null);
                  return {
                    ...m,
                    state: m.state === "error" ? "error" : "complete",
                    statusLabel: data.label || "Incident Matrix Evaluation Complete",
                    elapsedMs: data.elapsed_ms ?? m.elapsedMs,
                  };
                }

                return m;
              })
            );
          },
        });
      } catch (err) {
        if (err.name !== "AbortError") {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId
                ? {
                    ...m,
                    state: "error",
                    statusLabel: "Connection Interrupted",
                    content:
                      m.content ||
                      `⚠️ Stream Error: ${err.message}. Verify FastAPI BFF is running on port 8000.`,
                  }
                : m
            )
          );
        }
      } finally {
        setIsStreaming(false);
        setActiveStatus(null);
        abortControllerRef.current = null;
      }
    },
    [sessionId, isStreaming]
  );

  const stopStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      setIsStreaming(false);
      setActiveStatus(null);
    }
  }, []);

  const clearMessages = useCallback(() => {
    stopStream();
    setMessages([]);
  }, [stopStream]);

  return {
    messages,
    isStreaming,
    activeStatus,
    sendPrompt,
    stopStream,
    clearMessages,
  };
}
