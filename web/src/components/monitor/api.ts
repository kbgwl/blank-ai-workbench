/**
 * 监控页的数据读取。
 *
 * 只打两个接口：`GET /monitor`（一屏读数）和 `POST /triggers/{id}/fire`
 * （把一条待触发项现在放行）。刻意不做轮询聚合——后端一次给全，前端只负责轮询
 * 这一个地址，免得首屏对着四五个接口各超时一次。
 */

import { API_V1, jsonInit, request } from "@/lib/http";

const api = API_V1;

export type MonitorJudgement = {
  task_id: string;
  thread_id: string;
  goal: string;
  title: string;
  status: string;
  intent: string;
  reason: string;
  control_action: string;
  needs_execution: boolean;
  continues_goal: boolean;
  scenario: string;
  automation_mode: string;
  permission_mode: string;
  created_at: string;
};

export type MonitorTask = {
  task_id: string;
  thread_id: string;
  title: string;
  goal: string;
  status: string;
  updated_at: string;
};

export type MonitorPending = {
  trigger_id: string;
  project_id: string;
  task_id: string;
  event_type: string;
  source: string;
  goal: string;
  fire_at: string;
  created_at: string;
  waiting_for: string;
  never_fires: boolean;
  can_release: boolean;
  // v0.11.2 §8：暂停过的触发器也留在这张表里，前端据此显示「恢复」而不是「放行」。
  // 老后端没有这个字段，缺省按 pending 处理。
  status?: string;
};

export type MonitorSource = {
  id: string;
  label: string;
  kind: "builtin" | "im";
  connected: boolean;
  note: string;
};

export type MonitorCounts = {
  watching: number;
  running: number;
  waiting_approval: number;
  pending: number;
  never_fires: number;
};

export type MonitorOverview = {
  now: string;
  watching: MonitorJudgement[];
  running: MonitorTask[];
  pending: MonitorPending[];
  sources: MonitorSource[];
  counts: MonitorCounts;
  background_execution: boolean;
};

const EMPTY_COUNTS: MonitorCounts = {
  watching: 0,
  running: 0,
  waiting_approval: 0,
  pending: 0,
  never_fires: 0,
};

export function emptyOverview(): MonitorOverview {
  return {
    now: "",
    watching: [],
    running: [],
    pending: [],
    sources: [],
    counts: { ...EMPTY_COUNTS },
    background_execution: false,
  };
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asCounts(value: unknown): MonitorCounts {
  const record = (value ?? {}) as Record<string, unknown>;
  const num = (key: keyof MonitorCounts): number => {
    const raw = record[key];
    return typeof raw === "number" && Number.isFinite(raw) ? raw : 0;
  };
  return {
    watching: num("watching"),
    running: num("running"),
    waiting_approval: num("waiting_approval"),
    pending: num("pending"),
    never_fires: num("never_fires"),
  };
}

/** 后端字段缺一个就让整页崩是最差的结果：这里逐段兜底，缺的当空数组。 */
export function normalizeOverview(raw: unknown): MonitorOverview {
  const body = (raw ?? {}) as Record<string, unknown>;
  return {
    now: typeof body.now === "string" ? body.now : "",
    watching: asArray<MonitorJudgement>(body.watching),
    running: asArray<MonitorTask>(body.running),
    pending: asArray<MonitorPending>(body.pending),
    sources: asArray<MonitorSource>(body.sources),
    counts: asCounts(body.counts),
    background_execution: body.background_execution === true,
  };
}

export function fetchMonitor(limit = 30): Promise<MonitorOverview> {
  return request<unknown>(`${api}/monitor?limit=${limit}`, { cache: "no-store" }).then(normalizeOverview);
}

export type ReleaseResult = { task_id?: string; status?: string };

/** 现在放行一条待触发项。走的就是 worker 到点放行的同一段逻辑。 */
export function releaseTrigger(triggerId: string): Promise<ReleaseResult> {
  return request<ReleaseResult>(`${api}/triggers/${triggerId}/fire`, {
    method: "POST",
    ...jsonInit,
  });
}

export type TriggerAction = "pause" | "resume" | "cancel";
export type TriggerControlResult = {
  trigger_id?: string;
  status?: string;
  previous_status?: string;
  action?: string;
};

/** 暂停 / 恢复 / 取消：停掉的触发器不再到点、也不再被 watch 唤醒。 */
export function controlTrigger(triggerId: string, action: TriggerAction): Promise<TriggerControlResult> {
  return request<TriggerControlResult>(`${api}/triggers/${triggerId}/${action}`, {
    method: "POST",
    ...jsonInit,
  });
}
