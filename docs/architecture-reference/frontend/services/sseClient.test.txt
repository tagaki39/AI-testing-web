import { describe, expect, test, vi, beforeEach, afterEach } from "vitest";
import { callSSE, cancelExecution } from "./sseClient";

function createMockStream(chunks: string[]) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        body: createMockStream([]),
      } as Response),
    ),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("callSSE", () => {
  test("sends POST request and parses SSE events", async () => {
    const events: Array<{ type: string; data: unknown }> = [];
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: createMockStream([
        'event: status\ndata: {"phase":"thinking"}\n\n',
        'event: text_chunk\ndata: {"text":"hello"}\n\n',
        "event: done\ndata: {}\n\n",
      ]),
    } as unknown as Response);

    await callSSE({
      url: "/api/v1/ai-planning/sessions/1/chat",
      body: { content: "test" },
      onEvent: (type, data) => events.push({ type, data }),
    });

    expect(fetch).toHaveBeenCalledWith("/api/v1/ai-planning/sessions/1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: "test" }),
      signal: expect.any(AbortSignal),
    });
    expect(events).toEqual([
      { type: "status", data: { phase: "thinking" } },
      { type: "text_chunk", data: { text: "hello" } },
      { type: "done", data: {} },
    ]);
  });

  test("handles split chunks across reads", async () => {
    const events: Array<{ type: string; data: unknown }> = [];
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: createMockStream([
        'event: text_chunk\ndata: {"text":"hel',
        'lo"}\n\nevent: done\ndata: {}\n\n',
      ]),
    } as unknown as Response);

    await callSSE({
      url: "/api/test",
      body: {},
      onEvent: (type, data) => events.push({ type, data }),
    });

    expect(events).toEqual([
      { type: "text_chunk", data: { text: "hello" } },
      { type: "done", data: {} },
    ]);
  });

  test("throws on non-2xx response", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
    } as Response);

    await expect(
      callSSE({ url: "/api/test", body: {}, onEvent: vi.fn() }),
    ).rejects.toThrow("HTTP 500");
  });

  test("ignores malformed JSON in data", async () => {
    const events: Array<{ type: string; data: unknown }> = [];
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: createMockStream(["event: test\ndata: not-json\n\n"]),
    } as unknown as Response);

    await callSSE({
      url: "/api/test",
      body: {},
      onEvent: (type, data) => events.push({ type, data }),
    });

    expect(events).toEqual([]);
  });
});

describe("cancelExecution", () => {
  test("POSTs to cancel endpoint and returns result", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: "cancelled" }),
    } as Response);

    const result = await cancelExecution(5);
    expect(fetch).toHaveBeenCalledWith("/api/v1/ai-planning/sessions/5/cancel", {
      method: "POST",
    });
    expect(result).toEqual({ status: "cancelled" });
  });

  test("throws on non-2xx response", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
    } as Response);

    await expect(cancelExecution(999)).rejects.toThrow("HTTP 404");
  });
});
