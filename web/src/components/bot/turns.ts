/**
 * 把一条任务的事件流折叠成「一轮对话里助手做过什么」。
 *
 * 后端的事件是给审计用的：一条工具调用会拆成 requested / completed 两条，
 * 中间还夹着 context.measured、guardian.reviewed、node.started 这类内部观测。
 * 直接铺到界面上，用户看到的是几十行机器话，看不出"它到底改了什么"。
 *
 * 所以这里做三件事，而且只做这三件：
 * 1. 只保留人看得懂的动作（计划、工具、文件变更、提问、审批）；
 * 2. 把同一次工具调用的开始与结束配成一条——配对按顺序 + 同名，
 *    因为 requested 里没有 tool_call_id，只有 completed 才有；
 * 3. 配不上的 completed 仍然保留。宁可多显示一条"执行完了"，也不能因为
 *    配对失败就把一次真实发生的操作从视野里抹掉。
 *
 * 纯函数，不 import React 也不 import 别名路径——这样 node --test 能直接跑。
 */

export type RawEvent = {
  seq: number;
  type: string;
  node?: string;
  payload: Record<string, unknown>;
};

export type ToolStep = {
  kind: "tool";
  key: string;
  name: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "failed";
  durationMs: number | null;
  output: string;
  risk: string;
};

export type BotStep =
  | { kind: "plan"; key: string; items: string[] }
  | { kind: "change"; key: string; path: string; changeType: string }
  | { kind: "note"; key: string; text: string; tone: "muted" | "warn" | "danger" }
  | { kind: "question"; key: string; text: string; answered: string }
  | { kind: "approval"; key: string; text: string; granted: boolean | null }
  | ToolStep;

/** 只做展示，判断不了真假的观测类事件。留在这里是为了让"为什么这条不显示"有据可查。 */
const INTERNAL = /^(node\.|context\.|guardian\.|memory\.|shell\.|snapshot\.|model\.|intent\.|task\.(created|started|resumed)|delivery\.|validation\.)/;

function str(source: unknown, ...keys: string[]): string {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return "";
}

function num(source: unknown, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) return Number(value);
  }
  return null;
}

function record(source: unknown, ...keys: string[]): Record<string, unknown> {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  }
  return {};
}

function list(source: unknown, ...keys: string[]): unknown[] {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (Array.isArray(value)) return value;
  }
  return [];
}

export function toolNameOf(event: RawEvent): string {
  return str(event.payload, "tool", "name", "tool_name", "action") || event.node || "tool";
}

function toolOutputOf(payload: Record<string, unknown>): string {
  const text = str(payload, "summary", "output", "result", "message", "detail", "stdout", "stderr");
  if (text) return text;
  const nested = record(payload, "result", "output");
  return nested ? JSON.stringify(nested).slice(0, 4000) : "";
}

function planItems(payload: Record<string, unknown>): string[] {
  const direct = list(payload, "plan", "steps")
    .map((item) => (typeof item === "string" ? item : str(item, "step", "title", "text")))
    .filter(Boolean);
  if (direct.length > 0) return direct;
  // planner 有时把整段 JSON 塞在 text 里，取不出来就当作没有计划——不要显示一段生 JSON。
  const raw = str(payload, "text");
  if (!raw.trim().startsWith("{")) return [];
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return list(parsed, "steps", "plan")
      .map((item) => (typeof item === "string" ? item : str(item, "step", "title")))
      .filter(Boolean);
  } catch {
    return [];
  }
}

/** 折叠后的步骤序列。顺序就是发生顺序——不要按 kind 重排，那会让读者以为计划在最后才出现。 */
export function buildSteps(events: readonly RawEvent[]): BotStep[] {
  const steps: BotStep[] = [];
  const running = new Map<string, number[]>();

  for (const event of events) {
    const type = event.type.toLowerCase();
    if (INTERNAL.test(type)) continue;

    if (type === "plan.created") {
      const items = planItems(event.payload);
      if (items.length > 0) steps.push({ kind: "plan", key: `plan-${event.seq}`, items });
      continue;
    }

    if (type === "tool.requested" || type === "tool.started") {
      const name = toolNameOf(event);
      steps.push({
        kind: "tool",
        key: `tool-${event.seq}`,
        name,
        args: record(event.payload, "arguments", "args", "input", "params"),
        status: "running",
        durationMs: null,
        output: "",
        risk: str(event.payload, "risk_level", "risk"),
      });
      running.set(name, [...(running.get(name) ?? []), steps.length - 1]);
      continue;
    }

    if (type === "tool.completed" || type === "tool.failed" || type === "tool.result") {
      const name = toolNameOf(event);
      const queue = running.get(name);
      // 先配最早那条未结束的。这个顺序是有保证的：后端 `graph.py` 的收尾段
      // "全部按模型给出的顺序写事件，前端看到的次序与串行时一致"——即使只读工具
      // 是并发执行的，落盘顺序仍是调用顺序。所以同名调用按下标一一对应，
      // 不按结束先后配（那会把并发的两次读文件张冠李戴）。
      const index = queue?.length ? queue[0] : undefined;
      const ok = event.payload.ok === true || str(event.payload, "status").toLowerCase() === "ok";
      const failed = type === "tool.failed" || event.payload.ok === false;
      const patch = {
        status: (failed ? "failed" : ok ? "ok" : "ok") as ToolStep["status"],
        durationMs: num(event.payload, "duration_ms", "latency_ms", "elapsed_ms"),
        output: toolOutputOf(event.payload),
        risk: str(event.payload, "risk_level", "risk"),
      };
      if (index !== undefined && steps[index]?.kind === "tool") {
        queue!.shift();
        const existing = steps[index] as ToolStep;
        // requested 没带参数时，completed 里的也不一定有；两边都空就保持空，
        // 不要用一个空对象把"参数未知"伪装成"没有参数"。
        steps[index] = { ...existing, ...patch, args: Object.keys(existing.args).length ? existing.args : record(event.payload, "arguments", "args") };
      } else {
        steps.push({
          kind: "tool",
          key: `tool-${event.seq}`,
          name,
          args: record(event.payload, "arguments", "args", "input"),
          ...patch,
        });
      }
      continue;
    }

    if (type === "file.changed" || type === "file.created" || type === "file.deleted") {
      const path = str(event.payload, "path", "file", "file_path");
      if (!path) continue;
      steps.push({
        kind: "change",
        key: `change-${event.seq}`,
        path,
        changeType: str(event.payload, "change_type", "type") || (type === "file.created" ? "created" : type === "file.deleted" ? "deleted" : "modified"),
      });
      continue;
    }

    if (type === "question.asked" || type === "question.required") {
      const text = str(event.payload, "question", "text", "prompt");
      if (!text) continue;
      steps.push({ kind: "question", key: `q-${event.seq}`, text, answered: "" });
      continue;
    }

    if (type === "question.answered") {
      const answer = str(event.payload, "answer", "answers", "text");
      const pending = [...steps].reverse().find((step) => step.kind === "question" && !step.answered);
      if (pending?.kind === "question") pending.answered = answer || "已回答";
      continue;
    }

    if (type === "approval.decided") {
      const granted = event.payload.approved === true || str(event.payload, "decision").toLowerCase().startsWith("approve");
      steps.push({
        kind: "approval",
        key: `ap-${event.seq}`,
        text: str(event.payload, "message", "reason", "tool") || (granted ? "已批准一次操作" : "驳回过一次操作"),
        granted,
      });
      continue;
    }

    if (type === "steer.queued") {
      const text = str(event.payload, "message", "text");
      if (text) steps.push({ kind: "note", key: `note-${event.seq}`, text, tone: "muted" });
    }
  }

  return steps;
}

/** 这一轮助手最后说的话。取最后一条 task.completed 的 summary；失败时取原因。 */
export function turnReply(events: readonly RawEvent[]): { text: string; status: string; reason: string } {
  let text = "";
  let status = "";
  let reason = "";
  for (const event of events) {
    const type = event.type.toLowerCase();
    if (type === "task.completed" || type === "task.finished") {
      text = str(event.payload, "summary", "result", "message") || text;
      status = str(event.payload, "status") || status;
    }
    if (type === "task.failed" || type === "task.cancelled" || type === "task.canceled") {
      status = type.replace("task.", "");
      reason = str(event.payload, "reason", "error", "message") || reason;
    }
  }
  return { text, status, reason };
}

/** 一轮是否还在跑：有终态事件或任务状态是终态都算结束。 */
export function isTurnRunning(status: string, events: readonly RawEvent[]): boolean {
  const normalized = status.toLowerCase();
  if (["succeeded", "failed", "cancelled", "canceled", "error", "completed"].includes(normalized)) return false;
  return !events.some((event) => {
    const type = event.type.toLowerCase();
    return type === "task.completed" || type === "task.failed" || type === "task.cancelled" || type === "task.canceled";
  });
}

/** 一轮里改过哪些文件（用于对话里的"改了 N 个文件"摘要，去重保序）。 */
export function touchedFiles(steps: readonly BotStep[]): { path: string; changeType: string }[] {
  const seen = new Map<string, string>();
  for (const step of steps) {
    if (step.kind === "change" && !seen.has(step.path)) seen.set(step.path, step.changeType);
  }
  return [...seen].map(([path, changeType]) => ({ path, changeType }));
}

/**
 * 拿首条消息当会话标题：取第一行、去掉 markdown 前缀、按**字符**截断。
 *
 * 按字符而不是按字节：中文一个字三字节，按字节切会把最后一个字砍成半个乱码。
 * 去掉前缀是因为用户常先写 `# 修一下登录` 或 `- 帮我看看`，标题里不该出现这些符号。
 * 但只削真正的 markdown 前缀："2026 年那次重构"不能因为以数字开头就被削成"年那次重构"。
 */
export function threadTitleFrom(text: string, limit = 28): string {
  const first = String(text ?? "")
    .split("\n")
    .map((line) => line.replace(/^(?:#{1,6}\s*|[-*+]\s+|\d+[.)、]\s*|>\s*)/, "").trim())
    .find((line) => line.length > 0);
  if (!first) return "新对话";
  const chars = [...first];
  return chars.length <= limit ? first : `${chars.slice(0, limit).join("")}…`;
}

/**
 * 会话列表按「今天 / 昨天 / 本周 / 更早」分组。
 *
 * 用本地日历日切分，不是"距今 24 小时"：早上九点看昨天傍晚的会话，
 * 按小时算会归到"今天"，而用户心里那是昨天。
 * 时间戳解析不出来（空、坏值）时归到"更早"，不抛错——列表少一个分组标题，
 * 好过整块列表渲染不出来。
 */
export function dayBucket(value: string, now: number = Date.now()): "today" | "yesterday" | "week" | "earlier" {
  const parsed = Date.parse(String(value ?? "").includes("T") ? String(value) : String(value ?? "").replace(" ", "T"));
  if (!Number.isFinite(parsed)) return "earlier";
  const startOfDay = (ms: number) => {
    const date = new Date(ms);
    date.setHours(0, 0, 0, 0);
    return date.getTime();
  };
  const today = startOfDay(now);
  const day = startOfDay(parsed);
  if (day >= today) return "today";
  if (day >= today - 86_400_000) return "yesterday";
  if (day >= today - 7 * 86_400_000) return "week";
  return "earlier";
}

