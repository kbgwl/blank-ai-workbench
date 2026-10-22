/** 团队页的纯展示逻辑：状态判定、提及插入、用量进度。 */

export const ACTIVE_ROUND_STATUSES = ["running", "waiting_approval", "cancelling"] as const;

export function roundActive(status: string | null | undefined): boolean {
  return ACTIVE_ROUND_STATUSES.includes((status ?? "") as (typeof ACTIVE_ROUND_STATUSES)[number]);
}

export function roundStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case "running":
      return "讨论中";
    case "waiting_approval":
      return "等待确认";
    case "cancelling":
      return "正在停止";
    case "cancelled":
      return "已停止";
    case "failed":
      return "执行失败";
    case "completed":
      return "已完成";
    default:
      return "";
  }
}

export function messageStatusLabel(status: string): string {
  switch (status) {
    case "running":
      return "思考中";
    case "waiting_approval":
      return "等待确认";
    case "pass":
      return "没有补充";
    case "paused":
      return "已暂停";
    case "failed":
      return "失败";
    case "cancelled":
      return "已停止";
    default:
      return "";
  }
}

/** 在草稿末尾插入一个 @名字，前后补空格；已存在就不重复插入。 */
export function insertMention(draft: string, name: string): string {
  const token = `@${name}`;
  if (draft.includes(token)) return draft;
  if (!draft.trim()) return `${token} `;
  const head = /\s$/.test(draft) ? draft : `${draft} `;
  return `${head}${token} `;
}

export function usagePercent(tokens: number, budget: number): number {
  if (!budget || budget <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((tokens / budget) * 100)));
}

export function shortTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(value));
}

/** 头像只放一个字：中文取首字，英文取前两个字母。 */
export function initials(name: string): string {
  const cleaned = name.trim();
  if (!cleaned) return "?";
  if (/[\u4e00-\u9fff]/.test(cleaned[0])) return cleaned[0];
  return cleaned.slice(0, 2).toUpperCase();
}

export function messageTokens(message: { meta?: { usage?: { total_tokens?: number } } }): number {
  return message.meta?.usage?.total_tokens ?? 0;
}

export function argumentPreview(args: Record<string, unknown>): string {
  const command = args.command;
  if (typeof command === "string" && command.trim()) return command.trim();
  try {
    const text = JSON.stringify(args);
    return text === "{}" ? "" : text;
  } catch {
    return "";
  }
}

export type HighlightPart = { text: string; hit: boolean };

/** 把检索命中的词从摘要里切出来，交给渲染层加底色；长词优先，避免被短词切碎。 */
export function splitHighlight(text: string, tokens: string[]): HighlightPart[] {
  if (!text) return [];
  const words = Array.from(new Set((tokens ?? []).map((token) => token.trim()).filter(Boolean))).sort(
    (a, b) => b.length - a.length,
  );
  if (words.length === 0) return [{ text, hit: false }];
  const pattern = new RegExp(`(${words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  const parts: HighlightPart[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > last) parts.push({ text: text.slice(last, index), hit: false });
    parts.push({ text: match[0], hit: true });
    last = index + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), hit: false });
  return parts;
}
