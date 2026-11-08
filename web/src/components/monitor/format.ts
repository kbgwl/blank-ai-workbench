/**
 * 监控页的读法：把后端的机读字段翻成「人一眼能懂的一句话」。
 *
 * 这里只放纯函数，不碰请求也不碰 DOM——因为最容易出错的就是**文案映射**：
 * 少一个意图就会让某类消息显示成空白，而空白比写错更难发现。
 */

/** v165 §2.2 的十六种意图。少一个都会让那一类消息在界面上显示成空白。 */
export const INTENT_LABEL: Record<string, string> = {
  answer: "直接回答",
  inspect: "只读查看",
  execute: "动手执行",
  continue: "接着上一轮",
  retry: "重试",
  resume: "恢复",
  pause: "暂停",
  cancel: "取消",
  approve: "批准",
  reject: "拒绝",
  brainstorm: "头脑风暴",
  schedule: "定时",
  watch: "持续监控",
  remember: "记住",
  forget: "忘掉",
  ask_user: "先问清楚",
};

export type IntentTone = "act" | "reply" | "control" | "defer" | "unknown";

const ACT = new Set(["execute", "inspect", "continue", "retry", "resume"]);
const REPLY = new Set(["answer", "brainstorm", "ask_user"]);
const CONTROL = new Set(["pause", "cancel", "approve", "reject"]);
const DEFER = new Set(["schedule", "watch"]);
const REMEMBER = new Set(["remember", "forget"]);

/** 认不出的意图返回 `unknown`，界面据此显示原值——不猜、也不吞掉。 */
export function intentTone(intent: string): IntentTone {
  const value = (intent ?? "").trim().toLowerCase();
  if (ACT.has(value) || REMEMBER.has(value)) return "act";
  if (REPLY.has(value)) return "reply";
  if (CONTROL.has(value)) return "control";
  if (DEFER.has(value)) return "defer";
  return "unknown";
}

export function intentLabel(intent: string): string {
  const value = (intent ?? "").trim().toLowerCase();
  return INTENT_LABEL[value] ?? (value || "未判定");
}

/**
 * 一句判定后面跟的那半句。
 *
 * 顺序有意义：控制词（暂停/取消）优先于「要不要动手」，因为一条「暂停」
 * 如果显示成「已建任务」，用户会以为它又开了一轮。
 */
export function judgementOutcome(input: {
  intent: string;
  control_action?: string;
  needs_execution?: boolean;
  continues_goal?: boolean;
  status?: string;
}): string {
  const control = (input.control_action ?? "").trim();
  if (control) return `作用于已有的那一轮（${intentLabel(control)}）`;
  if (input.continues_goal) return "接着上一轮的目标，另开一轮";
  if (input.needs_execution) return "建了任务，交给执行侧";
  const intent = (input.intent ?? "").trim().toLowerCase();
  if (intent === "answer") return "没碰工具，直接答";
  if (intent === "ask_user") return "先问你，不动手";
  if (intent === "brainstorm") return "只发散，不改工作区";
  if (intent === "schedule" || intent === "watch") return "登记成待触发，先不跑";
  return "没建任务";
}

/** 「现在还在跑吗」——只用来给状态上色，不改写状态本身。 */
export function statusTone(status: string): "running" | "waiting" | "failed" | "idle" {
  const value = (status ?? "").trim().toLowerCase();
  if (value === "running" || value === "queued" || value === "draft") return "running";
  if (value === "waiting_approval" || value === "paused") return "waiting";
  if (value === "failed" || value === "cancelled") return "failed";
  return "idle";
}

export const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  queued: "排队中",
  running: "运行中",
  waiting_approval: "等你审批",
  paused: "已暂停",
  succeeded: "完成",
  failed: "失败",
  cancelled: "已取消",
};

export function statusLabel(status: string): string {
  const value = (status ?? "").trim().toLowerCase();
  return STATUS_LABEL[value] ?? (value || "未知");
}

/**
 * 触发源一句话。
 *
 * `connected=false` 的必须说「未接通」而不是「暂不支持」：前者是事实（现在读不到），
 * 后者听起来像以后自然会好。
 */
export function sourceLine(source: { label: string; connected: boolean; note: string }): string {
  return source.connected ? `${source.label} · 已接通` : `${source.label} · 未接通`;
}

/**
 * 后端存的是 UTC（SQLite 的 `CURRENT_TIMESTAMP` 就是 UTC），而序列化出来的是
 * 不带时区的 `2026-11-08T03:30:00`。直接 `Date.parse` 会按**本地时间**解释，
 * 于是每一行都差 8 小时（实测显示 03:30，而真实是 11:30）。这里统一补 `Z`。
 */
export function parseStamp(iso: string): number {
  const value = (iso ?? "").trim();
  if (!value) return Number.NaN;
  return Date.parse(value.endsWith("Z") ? value : `${value}Z`);
}

/** 本地时钟 HH:MM；解析不出来给破折号，不留空。 */
export function clockTime(iso: string): string {
  const stamp = parseStamp(iso);
  if (!Number.isFinite(stamp)) return "—";
  const date = new Date(stamp);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 相对时间：只给「刚刚 / N 分钟前 / N 小时前 / 日期」，超过一天就不再算相对值。 */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const stamp = parseStamp(iso);
  if (!Number.isFinite(stamp)) return "";
  const diff = Math.max(0, now - stamp);
  const minute = 60_000;
  if (diff < minute) return "刚刚";
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
  const date = new Date(stamp);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

/** 待触发项的一句话结论。永远等不到的那种要单独说，不能让用户以为它在盯。 */
export function pendingLine(item: { waiting_for: string; never_fires: boolean }): string {
  if (item.never_fires) return `永远等不到：${item.waiting_for}`;
  return item.waiting_for;
}
