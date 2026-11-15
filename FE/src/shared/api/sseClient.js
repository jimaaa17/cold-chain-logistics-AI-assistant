/**
 * Resilient Server-Sent Events (SSE) reader over POST fetch with AbortController support.
 * Parses standard `event: <type>\ndata: <json>\n\n` frames emitted by src/api/server.py.
 *
 * @param {string} url
 * @param {object} body
 * @param {(event: { type: string, data: any }) => void} onEvent
 * @param {AbortSignal} [signal]
 */
export async function streamPostSSE(url, body, onEvent, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    const errText = await response.text().catch(() => response.statusText);
    throw new Error(`SSE HTTP ${response.status}: ${errText}`);
  }

  if (!response.body) {
    throw new Error("ReadableStream not supported by browser response.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    for (const rawFrame of frames) {
      if (!rawFrame.trim()) continue;
      let eventType = "message";
      const dataLines = [];

      for (const line of rawFrame.split("\n")) {
        if (line.startsWith("event:")) {
          eventType = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }

      if (dataLines.length > 0) {
        const rawData = dataLines.join("\n");
        try {
          onEvent({ type: eventType, data: JSON.parse(rawData) });
        } catch {
          onEvent({ type: eventType, data: { raw: rawData } });
        }
      }
    }
  }
}
