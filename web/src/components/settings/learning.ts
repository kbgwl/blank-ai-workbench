export function statusLabel(status: string): string {
  if (status === "active") return "生效中";
  if (status === "candidate") return "候选";
  if (status === "retired") return "已退役";
  if (status === "conflict") return "冲突停用";
  return status || "未知状态";
}

export function kindLabel(kind: string): string {
  if (kind === "pitfall") return "坑";
  if (kind === "procedure") return "做法";
  if (kind === "fact") return "事实";
  return kind || "其它";
}

export function originLabel(origin: string): string {
  if (origin === "explicit") return "你明说的";
  if (origin === "harvested") return "从任务描述摘的";
  return origin || "未知来源";
}

export function activeCount(stats: Record<string, number>): number {
  return Number(stats.active ?? 0);
}

export function quoteText(quotes: { text?: string }[]): string {
  return quotes
    .map((quote) => String(quote?.text ?? "").trim())
    .filter(Boolean)
    .join(" / ");
}

/** 体检总数 = 经验体检 + 偏好体检；两个库分开算，界面上合成一行总数。 */
export function lintCount(experience: unknown[], persona: unknown[]): number {
  return experience.length + persona.length;
}
