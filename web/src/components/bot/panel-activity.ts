/**
 * 右栏「活动」页的行数据。
 *
 * 对话流里每一步都已经出现过一次，这里再列一遍不是为了重复，而是为了**倒着查**：
 * 用户想问的是"它到底跑了哪些命令、哪一步失败了"，而不是重读整段对话。
 * 所以这里只保留"做过什么 + 结果"，不复制输出正文。
 */

import type { BotStep } from "./turns";

export type ActivityTone = "ok" | "failed" | "running" | "muted" | "warn";

export type ActivityRow = {
  key: string;
  /** 一行标题：工具名、改动类型、计划…… */
  label: string;
  /** 副行：参数要点或说明。空串表示没什么可补的。 */
  detail: string;
  tone: ActivityTone;
};

function argBrief(args: Record<string, unknown>): string {
  for (const key of ["path", "file", "command", "cmd", "query", "pattern", "url", "relative"]) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim().slice(0, 80);
  }
  for (const value of Object.values(args)) {
    if (typeof value === "string" && value.trim()) return value.trim().slice(0, 80);
  }
  return "";
}

const CHANGE_LABEL: Record<string, string> = { created: "新增", modified: "修改", deleted: "删除" };

export function activityRows(steps: BotStep[]): ActivityRow[] {
  return steps.map((step) => {
    switch (step.kind) {
      case "tool":
        return {
          key: step.key,
          label: step.name || "工具调用",
          detail: argBrief(step.args) || step.output.split("\n")[0]?.slice(0, 80) || "",
          tone: step.status === "failed" ? "failed" : step.status === "running" ? "running" : "ok",
        };
      case "change":
        return {
          key: step.key,
          label: `${CHANGE_LABEL[step.changeType] ?? "改动"} ${step.path}`,
          detail: "",
          tone: "muted",
        };
      case "plan":
        return {
          key: step.key,
          label: "计划",
          detail: step.items.slice(0, 3).join(" → ") + (step.items.length > 3 ? ` …共 ${step.items.length} 步` : ""),
          tone: "muted",
        };
      case "note":
        return {
          key: step.key,
          label: step.tone === "danger" ? "出错" : step.tone === "warn" ? "提醒" : "说明",
          detail: step.text,
          tone: step.tone === "danger" ? "failed" : step.tone === "warn" ? "warn" : "muted",
        };
      case "question":
        return {
          key: step.key,
          label: "提问",
          detail: step.answered ? `${step.text} → ${step.answered}` : step.text,
          tone: step.answered ? "muted" : "warn",
        };
      case "approval":
        return {
          key: step.key,
          label: step.granted === null ? "等待批准" : step.granted ? "已批准" : "已拒绝",
          detail: step.text,
          tone: step.granted === null ? "warn" : step.granted ? "ok" : "failed",
        };
    }
  });
}

export const TONE_CLASS: Record<ActivityTone, string> = {
  ok: "bg-accent",
  failed: "bg-danger",
  running: "bg-brand animate-pulse",
  warn: "bg-warn",
  muted: "bg-line-strong",
};
