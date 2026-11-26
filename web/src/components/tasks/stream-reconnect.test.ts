import test from "node:test";
import assert from "node:assert/strict";

import { streamTaskEvents } from "./api";
import { reconnectDelay } from "./use-task-events";

function eventFrame(seq: number): string {
  const body = {
    id: `e${seq}`,
    task_id: "t1",
    type: "task.started",
    node: "receive_task",
    payload: {},
    seq,
    created_at: "2026-11-26T00:00:00Z",
  };
  return `id: ${seq}\ndata: ${JSON.stringify(body)}\n\n`;
}

test("SSE 重连带游标：after_seq 与 Last-Event-ID 都从 lastSeq 续传", async () => {
  const captured: { url: string; headers: Record<string, string> }[] = [];
  const frames = [eventFrame(7), "event: ping\ndata: \n\n"];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    captured.push({
      url: String(input),
      headers: Object.fromEntries(new Headers(init?.headers ?? {}).entries()),
    });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const frame of frames) controller.enqueue(new TextEncoder().encode(frame));
        controller.close();
      },
    });
    return new Response(body, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as typeof fetch;
  try {
    let activity = 0;
    const events: unknown[] = [];
    await streamTaskEvents("t1", {
      signal: new AbortController().signal,
      lastSeq: 5,
      onActivity: () => {
        activity += 1;
      },
      onEvent: (event) => events.push(event),
    });

    assert.match(captured[0].url, /after_seq=5/);
    assert.equal(captured[0].headers["last-event-id"], "5");
    // 持久事件 + ping 都算“连接还活着”，看门狗据此不误杀安静的任务。
    assert.equal(activity, 2);
    assert.equal(events.length, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("SSE 退避是 1/2/4/8/16 秒封顶", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map(reconnectDelay), [1000, 2000, 4000, 8000, 16000, 16000, 16000]);
});
