/**
 * Generic SSE client using fetch + ReadableStream.
 * Supports POST requests with JSON bodies (unlike EventSource which is GET-only).
 */

export interface SSEClientOptions {
  url: string;
  body: Record<string, unknown>;
  onEvent: (eventType: string, data: unknown) => void;
  onDone?: () => void;
  signal?: AbortSignal;
  timeoutMs?: number;
}

const DEFAULT_SSE_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes

export async function callSSE(opts: SSEClientOptions): Promise<void> {
  const timeout = AbortSignal.timeout(opts.timeoutMs ?? DEFAULT_SSE_TIMEOUT_MS);
  const combinedSignal = opts.signal
    ? AbortSignal.any([opts.signal, timeout])
    : timeout;

  const response = await fetch(opts.url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts.body),
    signal: combinedSignal,
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";

    for (const part of parts) {
      let eventType = "message";
      let eventData = "{}";
      for (const line of part.split("\n")) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7);
        } else if (line.startsWith("data: ")) {
          eventData = line.slice(6);
        }
      }
      try {
        opts.onEvent(eventType, JSON.parse(eventData));
      } catch {
        // Ignore malformed JSON
      }
    }
  }

  // Stream ended normally — notify caller so it can clean up streaming state
  opts.onDone?.();
}

export async function cancelExecution(sessionId: number): Promise<{ status: string }> {
  const response = await fetch(`/api/v1/ai-planning/sessions/${sessionId}/cancel`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  return response.json();
}
