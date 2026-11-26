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

/** 断线重连退避：1s、2s、4s、8s、16s 封顶；最多 5 次后改轮询。 */
export function reconnectDelay(attempt: number): number {
  return Math.min(16_000, 1_000 * 2 ** Math.max(0, attempt));
}

const TERMINAL_EVENT_HINTS = [
  "task.completed",
  "task.failed",
  "task.cancelled",
  "task.canceled",
  // 取消请求本身就是终态：再轮询下去也不会等来新东西，只会每 1.5 秒问一次
  // 一个已经停下的任务。真正的 task.cancelled 由后端在取消时补写。
  "task.cancel_requested",
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
 * 先尝试 SSE（GET /tasks/{id}/events/stream）；断线按 1s→2s→4s→8s→16s 退避重连，
 * 用 `Last-Event-ID` 从最后一个 seq 之后续传；30s 没有任何帧由看门狗主动断开重连。
 * 连续 5 次重连失败才退回 1.5 秒轮询 GET /tasks/{id}/events，任务进入终态后停止。
 * 挂载时总会拉一次完整事件快照，所以刷新页面后时间线可以完整恢复。
 */
export function useTaskEvents(taskId: string | null, status: string | null): TaskEventsState {
  const [store, setStore] = useState<Store | null>(null);
  const [live, setLive] = useState<{ taskId: string; kind: LiveKind } | null>(null);
  // 实时文字单独放一个 state：它每秒变好几次，混进 events 会把整条时间线重排。
  const [streaming, setStreaming] = useState<{ taskId: string; text: string } | null>(null);
  const [nonce, setNonce] = useState(0);
  const seenRef = useRef<{ taskId: string; keys: Set<string> }>({ taskId: "", keys: new Set() });
  // 断线重连游标：已经收到的最大 seq。切换任务时归零，避免把上一条会话的序号带过去。
  const lastSeqRef = useRef<{ taskId: string; seq: number }>({ taskId: "", seq: 0 });

  const terminal = isTerminalStatus(status);

  const patch = useCallback((target: string, updater: (current: Store) => Store) => {
    setStore((current) => (current && current.taskId === target ? updater(current) : current));
  }, []);

  const merge = useCallback(
    (target: string, incoming: AgentEvent[]): number => {
      if (incoming.length === 0) return 0;
      if (seenRef.current.taskId !== target) seenRef.current = { taskId: target, keys: new Set() };
      if (lastSeqRef.current.taskId !== target) lastSeqRef.current = { taskId: target, seq: 0 };
      const keys = seenRef.current.keys;
      const added = incoming.filter((event) => !keys.has(eventKey(event)));
      if (added.length === 0) return 0;
      added.forEach((event) => keys.add(eventKey(event)));
      lastSeqRef.current.seq = Math.max(lastSeqRef.current.seq, ...added.map((event) => event.seq));
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
    let finished = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let watchdog: ReturnType<typeof setInterval> | null = null;
    let active: AbortController | null = null;
    let attempts = 0;
    let lastActivity = Date.now();
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const applySnapshot = (rows: AgentEvent[]) => {
      if (ignore) return;
      seenRef.current = { taskId: target, keys: new Set(rows.map(eventKey)) };
      lastSeqRef.current = {
        taskId: target,
        seq: rows.reduce((max, event) => Math.max(max, event.seq), 0),
      };
      finished = finished || hasTerminalEvent(rows);
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

    const stopLive = () => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (watchdog) {
        clearInterval(watchdog);
        watchdog = null;
      }
      active?.abort();
      active = null;
    };

    const poll = () => {
      fetchEvents(target)
        .then((rows) => {
          if (stopped) return;
          const added = merge(target, rows);
          if (added > 0) patch(target, (current) => ({ ...current, notice: null }));
          if (hasTerminalEvent(rows)) {
            finished = true;
            if (pollTimer) {
              clearInterval(pollTimer);
              pollTimer = null;
            }
          }
        })
        .catch((error: unknown) => {
          if (!stopped) patch(target, (current) => ({ ...current, notice: `事件轮询失败：${errorMessage(error)}` }));
        });
    };

    const startPolling = (reason: string | null) => {
      if (stopped || pollTimer) return;
      stopLive();
      setLive({ taskId: target, kind: "polling" });
      if (reason) patch(target, (current) => ({ ...current, notice: reason }));
      poll();
      pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    };

    const scheduleReconnect = (reason: string | null) => {
      if (stopped || finished || pollTimer) return;
      if (reason) patch(target, (current) => ({ ...current, notice: reason }));
      if (attempts >= 5) {
        startPolling("实时事件流多次重连失败，已改用 1.5 秒轮询");
        return;
      }
      const delay = reconnectDelay(attempts);
      attempts += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (stopped || finished) return;
      active?.abort();
      const controller = new AbortController();
      active = controller;
      lastActivity = Date.now();
      streamTaskEvents(target, {
        signal: controller.signal,
        lastSeq: lastSeqRef.current.taskId === target ? lastSeqRef.current.seq : 0,
        onActivity: () => {
          lastActivity = Date.now();
          // 收到过数据就重置退避：断线是偶发，不是"这条流一直是坏的"。
          attempts = 0;
        },
        onEvent: (event) => {
          if (stopped) return;
          setLive({ taskId: target, kind: "sse" });
          merge(target, [event]);
          if (hasTerminalEvent([event])) {
            finished = true;
            stopLive();
          }
        },
        onDelta: (delta) => {
          if (stopped) return;
          // 每帧都是"到目前为止的尾巴"，直接替换即可——不拼接，所以丢帧也不会拼错。
          // done 表示这一屏说完了：清空，让位给紧随其后的持久事件。
          setStreaming({ taskId: target, text: delta.done ? "" : delta.text });
        },
      })
        .then(() => {
          if (!stopped && !finished) scheduleReconnect("实时事件流中断，正在重连");
        })
        .catch((error: unknown) => {
          if (stopped || ignore) return;
          if ((error as Error).name === "AbortError") {
            scheduleReconnect(null);
            return;
          }
          scheduleReconnect(`实时事件流不可用（${errorMessage(error)}），正在重连`);
        });
    };

    // 看门狗：服务端每 15s 至少发一帧（事件 / delta / ping）。30s 没有任何帧
    // 说明连接可能被中间层静默掐断，主动断开走重连，而不是让界面永远“连接中”。
    watchdog = setInterval(() => {
      if (stopped || finished) return;
      if (Date.now() - lastActivity > 30_000) {
        lastActivity = Date.now();
        active?.abort();
      }
    }, 5_000);

    connect();

    return () => {
      ignore = true;
      stopped = true;
      stopLive();
      if (pollTimer) clearInterval(pollTimer);
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
