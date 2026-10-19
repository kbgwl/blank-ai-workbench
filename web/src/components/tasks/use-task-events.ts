"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, fetchEvents, isTerminalStatus, streamTaskEvents, type AgentEvent } from "./api";
export type LiveKind = "sse" | "polling";
export type LiveMode = "off" | "snapshot" | "connecting" | "sse" | "polling";

export type TaskEventsState = {
  events: AgentEvent[];
  mode: LiveMode;
  notice: string | null;
  revision: number;
  /** 模型正在说的最后一屏文字。空串表示当前没有输出在进行。 */
  streaming: string;
  reload: () => void;
};

const POLL_INTERVAL_MS = 1500;

const TERMINAL_EVENT_HINTS = [
  "task.completed",
  "task.failed",
  "task.cancelled",
  "task.canceled",
  "result.succeeded",
  "result.failed",
  "result.completed",
];

function hasTerminalEvent(events: AgentEvent[]): boolean {
  return events.some((event) => {
    const type = event.type.toLowerCase();
    return TERMINAL_EVENT_HINTS.includes(type) || type.startsWith("result.");
  });
}

function eventKey(event: AgentEvent): string {
  return `${event.seq}|${event.type}|${event.node}`;
}

function sortBySeq(events: AgentEvent[]): AgentEvent[] {
  return [...events].sort((a, b) => a.seq - b.seq);
}

type Store = { taskId: string; events: AgentEvent[]; notice: string | null; revision: number };

/**
 * 先尝试 SSE（GET /tasks/{id}/stream）；端点缺失、返回不是 event-stream 或连接中断时，
 * 自动退回 1.5 秒轮询 GET /tasks/{id}/events，任务进入终态后停止。
 * 挂载时总会拉一次完整事件快照，所以刷新页面后时间线可以完整恢复。
 */
export function useTaskEvents(taskId: string | null, status: string | null): TaskEventsState {
  const [store, setStore] = useState<Store | null>(null);
  const [live, setLive] = useState<{ taskId: string; kind: LiveKind } | null>(null);
  // 实时文字单独放一个 state：它每秒变好几次，混进 events 会把整条时间线重排。
  const [streaming, setStreaming] = useState<{ taskId: string; text: string } | null>(null);
  const [nonce, setNonce] = useState(0);
  const seenRef = useRef<{ taskId: string; keys: Set<string> }>({ taskId: "", keys: new Set() });

  const terminal = isTerminalStatus(status);

  const patch = useCallback((target: string, updater: (current: Store) => Store) => {
    setStore((current) => (current && current.taskId === target ? updater(current) : current));
  }, []);

  const merge = useCallback(
    (target: string, incoming: AgentEvent[]): number => {
      if (incoming.length === 0) return 0;
      if (seenRef.current.taskId !== target) seenRef.current = { taskId: target, keys: new Set() };
      const keys = seenRef.current.keys;
      const added = incoming.filter((event) => !keys.has(eventKey(event)));
      if (added.length === 0) return 0;
      added.forEach((event) => keys.add(eventKey(event)));
      patch(target, (current) => ({
        ...current,
        events: sortBySeq([...current.events, ...added]),
        revision: current.revision + 1,
        notice: null,
      }));
      return added.length;
    },
    [patch],
  );

  useEffect(() => {
    if (!taskId) return;
    const target = taskId;
    let ignore = false;
    let stopped = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const controller = new AbortController();

    const applySnapshot = (rows: AgentEvent[]) => {
      if (ignore) return;
      seenRef.current = { taskId: target, keys: new Set(rows.map(eventKey)) };
      setStore({ taskId: target, events: sortBySeq(rows), notice: null, revision: rows.length });
    };

    fetchEvents(target)
      .then((rows) => applySnapshot(rows))
      .catch((error: unknown) => {
        if (ignore) return;
        const notice = `事件时间线读取失败：${errorMessage(error)}`;
        setStore((current) =>
          current && current.taskId === target
            ? { ...current, notice }
            : { taskId: target, events: [], notice, revision: 0 },
        );
      });

    if (terminal) {
      return () => {
        ignore = true;
      };
    }

    const poll = () => {
      fetchEvents(target)
        .then((rows) => {
          if (stopped) return;
          const added = merge(target, rows);
          if (added > 0) patch(target, (current) => ({ ...current, notice: null }));
          if (hasTerminalEvent(rows) && timer) {
            clearInterval(timer);
            timer = null;
          }
        })
        .catch((error: unknown) => {
          if (!stopped) patch(target, (current) => ({ ...current, notice: `事件轮询失败：${errorMessage(error)}` }));
        });
    };

    const startPolling = (reason: string | null) => {
      if (stopped || timer) return;
      setLive({ taskId: target, kind: "polling" });
      if (reason) patch(target, (current) => ({ ...current, notice: reason }));
      poll();
      timer = setInterval(poll, POLL_INTERVAL_MS);
    };

    streamTaskEvents(target, {
      signal: controller.signal,
      onEvent: (event) => {
        if (stopped) return;
        setLive({ taskId: target, kind: "sse" });
        merge(target, [event]);
      },
      onDelta: (delta) => {
        if (stopped) return;
        // 每帧都是"到目前为止的尾巴"，直接替换即可——不拼接，所以丢帧也不会拼错。
        // done 表示这一屏说完了：清空，让位给紧随其后的持久事件。
        setStreaming({ taskId: target, text: delta.done ? "" : delta.text });
      },
    })
      .then(() => {
        if (!stopped) startPolling(null);
      })
      .catch((error: unknown) => {
        if (stopped || ignore) return;
        startPolling(`实时事件流不可用（${errorMessage(error)}），已自动改用 1.5 秒轮询`);
      });

    return () => {
      ignore = true;
      stopped = true;
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [taskId, terminal, nonce, merge, patch]);

  const scoped = store && store.taskId === taskId ? store : null;
  const liveKind = live && live.taskId === taskId ? live.kind : null;
  const finished = terminal || (scoped ? hasTerminalEvent(scoped.events) : false);
  const mode: LiveMode = !taskId ? "off" : finished ? "snapshot" : liveKind ?? "connecting";

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  return {
    events: scoped?.events ?? [],
    mode,
    notice: scoped?.notice ?? null,
    revision: scoped?.revision ?? 0,
    streaming: streaming && streaming.taskId === taskId && !finished ? streaming.text : "",
    reload,
  };
}
