/**
 * 助手档案的纯文案与筛选。
 *
 * 判据：用户挑助手去派活时，看的是"它能干什么、被禁止干什么、从哪来的"，
 * 不是它有多少字说明。所以这里把工具清单压成能扫的两行，
 * 并把"没限制"和"字段缺失"分开——前者是真的没写限制，后者是不知道。
 */
import type { AgentProfile } from "./api";

const SOURCE_LABELS: Record<string, string> = {
  builtin: "内置",
  "built-in": "内置",
  user: "我的",
  project: "项目",
  plugin: "插件",
};

export function agentSourceLabel(source: string): string {
  const key = (source ?? "").toLowerCase();
  return SOURCE_LABELS[key] || source || "来源未知";
}

/** 工具清单：有就列前几个并报数，没这个字段就说"没写限制"，不要显示空白。 */
export function agentToolsText(tools: string[] | undefined, disallowed: string[] | undefined): string {
  const allow = Array.isArray(tools) ? tools : [];
  const deny = Array.isArray(disallowed) ? disallowed : [];
  const head = allow.length
    ? `可用 ${allow.slice(0, 5).join("、")}${allow.length > 5 ? ` 等 ${allow.length} 个` : ""}`
    : tools === undefined
      ? "工具范围没写（跟随默认）"
      : "没有可用工具";
  const tail = deny.length ? `；禁止 ${deny.join("、")}` : "";
  return head + tail;
}

/**
 * 默认助手排在前面，其余按名字。
 *
 * 内置助手是绝大多数人真正会用的；按名字排序会把它们散在自定义助手中间，
 * 用户第一眼看到的是一堆自己从没建过的名字。
 */
export function orderAgents(agents: readonly AgentProfile[]): AgentProfile[] {
  const rank = (item: AgentProfile) => {
    const name = (item.name ?? "").toLowerCase();
    if (name === "general-purpose") return 0;
    if (name === "fork") return 1;
    if (name === "plan") return 2;
    return 10;
  };
  return [...agents].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}
