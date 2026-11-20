/**
 * 定时与触发列表的纯文案。
 *
 * 这里的纪律：后端只给了一次性的 `fire_at`，没给 RRULE。所以界面只说
 * 「什么时候触发过 / 什么时候要触发」，绝不能编出「每天」「每周」——
 * 那会让用户以为明天它还会来，而实际上它只来这一次。
 */
import type { Trigger } from "./api";

const STATUS: Record<string, { label: string; tone: "brand" | "warn" | "danger" | "muted" }> = {
  pending: { label: "待触发", tone: "brand" },
  fired: { label: "已触发", tone: "brand" },
  skipped: { label: "已跳过", tone: "muted" },
  expired: { label: "已过期", tone: "muted" },
  paused: { label: "已暂停", tone: "warn" },
  cancelled: { label: "已取消", tone: "danger" },
};

export function triggerStatus(status: string): { label: string; tone: "brand" | "warn" | "danger" | "muted" } {
  return STATUS[(status ?? "").toLowerCase()] ?? { label: status || "状态未知", tone: "muted" };
}

/** 「下次触发」只对还没触发的成立；已经跑过的要说"跑于"，不能继续叫"下次"。 */
export function triggerWhen(row: Trigger, now: number = Date.now()): string {
  const at = Date.parse(String(row.fired_at || row.fire_at || "").replace(" ", "T"));
  if (!Number.isFinite(at)) return "";
  const stamp = new Date(at);
  const pad = (value: number) => String(value).padStart(2, "0");
  const clock = `${pad(stamp.getHours())}:${pad(stamp.getMinutes())}`;
  if (row.fired_at) return `跑于 ${clock}`;
  if (row.status === "pending" && at < now) return `已过点（${clock}），等放行`;
  return `${clock} 触发`;
}

/** 哪些操作现在按得下去。给一张已经跑完的卡挂「立即触发」只会得到 409。 */
export function triggerActions(row: Trigger): ("fire" | "pause" | "resume" | "cancel")[] {
  const status = (row.status ?? "").toLowerCase();
  if (status === "pending") return ["fire", "pause", "cancel"];
  if (status === "paused") return ["resume", "cancel"];
  return [];
}

export const TRIGGER_ACTION_LABELS: Record<string, string> = {
  fire: "现在触发",
  pause: "暂停",
  resume: "恢复",
  cancel: "取消",
};

/** 已触发的条目给出任务的去处。没有 task_id 就说清楚"只登记了事件，没建任务"。 */
export function triggerTaskHref(row: Trigger): string {
  return row.task_id ? `/tasks/detail?id=${encodeURIComponent(row.task_id)}` : "";
}
