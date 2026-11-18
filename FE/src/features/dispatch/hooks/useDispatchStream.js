import { useState, useRef, useCallback } from "react";
import { streamDispatchPrompt, streamApprovalDecision } from "../../../shared/api/endpoints.js";
import { applyDispatchEvent } from "./dispatchEvents.js";

/**
 * Manages conversation history and the dispatch workflow's SSE streams: the question stream and,
 * when a high-stakes action is proposed, the dispatcher's approval stream on the same message.
 */
export function useDispatchStream(sessionId) {
  const [messages, setMessages] = useState([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [activeStatus, setActiveStatus] = useState(null);
  const abortControllerRef = useRef(null);

  const runStream = useCallback(async (messageId, start) => {
    const controller = new AbortController();
    abortControllerRef.current = controller;
    setIsStreaming(true);
    try {
      await start({
        signal: controller.signal,
        onEvent: (event) => {
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== messageId) return m;
              const next = applyDispatchEvent(m, event);
              setActiveStatus(event.type === "done" ? null : next.statusLabel);
              return next;
            })
          );
        },
      });
    } catch (err) {
      if (err.name !== "AbortError") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId
              ? {
                  ...m,
                  state: "error",
                  statusLabel: "Connection Interrupted",
                  content:
                    m.content ||
                    `Stream error: ${err.message}. Check that the API is running on port 8000.`,
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
  }, []);

  const sendPrompt = useCallback(
    async (promptText) => {
      const trimmed = (promptText || "").trim();
      if (!trimmed || isStreaming) return;

      const assistantMsgId = `msg-${Date.now()}`;
      setMessages((prev) => [
        ...prev,
        { id: `user-${Date.now()}`, role: "user", content: trimmed, timestamp: new Date().toISOString() },
        {
          id: assistantMsgId,
          role: "assistant",
          content: "",
          draft: "",
          traces: [],
          statusLabel: "Routing the question...",
          state: "running",
          elapsedMs: 0,
          timestamp: new Date().toISOString(),
        },
      ]);
      setActiveStatus("Routing the question...");
      await runStream(assistantMsgId, ({ signal, onEvent }) =>
        streamDispatchPrompt({ sessionId, prompt: trimmed, onEvent, signal })
      );
    },
    [sessionId, isStreaming, runStream]
  );

  const decideApproval = useCallback(
    async (messageId, decision, note = "") => {
      if (isStreaming) return;
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? { ...m, state: "running", content: "", statusLabel: "Sending decision..." }
            : m
        )
      );
      await runStream(messageId, ({ signal, onEvent }) =>
        streamApprovalDecision({ sessionId, decision, note, onEvent, signal })
      );
    },
    [sessionId, isStreaming, runStream]
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
    decideApproval,
    stopStream,
    clearMessages,
  };
}
