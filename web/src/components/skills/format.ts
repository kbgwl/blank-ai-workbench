/**
 * 技能页的取值口径。
 *
 * 这三件事都必须能单独测：状态该画什么颜色、说明该折成几行、哪些算"已启用"。
 * 把它们埋在组件里，就只能靠肉眼确认，改一次问一次。
 */

export type SkillState = "active" | "draft" | "disabled" | "unknown";

export const SKILL_STATE_LABEL: Record<SkillState, string> = {
  active: "已启用",
  draft: "草案",
  disabled: "已停用",
  unknown: "状态未知",
};

/**
 * 后端只认 ``active | draft | disabled``，多出来的值一律念"状态未知"。
 *
 * 不把未知值兜底成 draft 或 active：前者会让一条其实在生效的偏好看起来没生效，
 * 后者更糟——用户以为关掉了，模型还在照它办。
 */
export function skillState(status: string | null | undefined): SkillState {
  const value = (status ?? "").trim().toLowerCase();
  if (value === "active") return "active";
  if (value === "draft") return "draft";
  if (value === "disabled") return "disabled";
  return "unknown";
}

/** 说明在列表里只占一行：多行会把技能列表顶成一篇文档，反而找不到要启用的那条。 */
export function preview(text: string | null | undefined, max = 120): string {
  const flat = (text ?? "").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  if (flat.length <= max) return flat;
  return `${flat.slice(0, max)}…`;
}

export type SkillSplit = { active: number; total: number };

export function countSkills(statuses: (string | null | undefined)[]): SkillSplit {
  const total = statuses.length;
  const active = statuses.filter((status) => skillState(status) === "active").length;
  return { active, total };
}

/** 文案里"3 条已启用 / 共 5 条"由这里统一拼，避免两处各写一遍再逐渐走样。 */
export function skillSummary(statuses: (string | null | undefined)[]): string {
  const { active, total } = countSkills(statuses);
  if (total === 0) return "还没有技能";
  return `${active} 条已启用 / 共 ${total} 条`;
}
