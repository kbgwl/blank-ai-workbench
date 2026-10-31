/**
 * 计划 / 里程碑 / 目标的前端形状与解析。
 *
 * 后端已经把它们归一到固定结构，这里只做两件事：
 * 1. 容错解析（老事件里 `steps` 是纯字符串，新的是对象）；
 * 2. 给出"这一步能不能点"的判断依据。
 *
 * 不依赖 `api.ts`：那边要 import 这里的解析函数，反过来再依赖就成环了。
 * 三个读字段的小工具在本文件内自带一份，比在任何一处加 `// eslint-disable` 都干净。
 */

export type PlanStatus = "pending" | "in_progress" | "completed";

function readText(source: unknown, ...keys: string[]): string {
  if (!source || typeof source !== "object") return "";
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return "";
}

function readNumber(source: unknown, ...keys: string[]): number | null {
  if (!source || typeof source !== "object") return null;
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

function readArray(source: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(source)) return source;
  if (!source || typeof source !== "object") return [];
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value)) return value;
  }
  return [];
}

export type PlanStepView = {
  id: string;
  text: string;
  status: PlanStatus;
  milestoneId: string;
  evidence: string;
};

export type MilestoneView = {
  id: string;
  title: string;
  status: PlanStatus;
  stepIds: string[];
  /** 由步骤推出来的完成度。空里程碑是 0，不是"已完成"。 */
  total: number;
  completed: number;
  percent: number;
};

export type PlanProgressView = {
  total: number;
  completed: number;
  inProgress: number;
  pending: number;
  percent: number;
};

export function normalizePlanStatus(value: string): PlanStatus {
  const text = value.trim().toLowerCase();
  if (text === "completed" || text === "done" || text === "completed ") return "completed";
  if (text === "in_progress" || text === "running" || text === "active") return "in_progress";
  return "pending";
}

/** 解析步骤。`source` 可以是 task.plan_steps，也可以是事件载荷里的 steps。 */
export function parsePlanSteps(source: unknown): PlanStepView[] {
  const raw = Array.isArray(source)
    ? source
    : readArray(source, "plan_steps", "steps", "items", "tasks");
  const steps: PlanStepView[] = [];
  raw.forEach((item, index) => {
    if (typeof item === "string") {
      const text = item.trim();
      if (!text) return;
      steps.push({
        id: `legacy-${index}`,
        text,
        status: "pending",
        milestoneId: "",
        evidence: "",
      });
      return;
    }
    const text =
      readText(item, "step", "text", "title", "name", "description") ||
      readArray(item, "items", "steps")
        .map((leaf) => readText(leaf, "text", "title", "name", "step"))
        .filter(Boolean)
        .join("；");
    const trimmed = text.trim();
    if (!trimmed) return;
    steps.push({
      id: readText(item, "id") || `idx-${index}`,
      text: trimmed,
      status: normalizePlanStatus(readText(item, "status")),
      milestoneId: readText(item, "milestone_id", "milestoneId", "milestone"),
      evidence: readText(item, "evidence", "result", "note"),
    });
  });
  return steps;
}

export function parseMilestones(source: unknown): MilestoneView[] {
  return readArray(source, "milestones", "phases").map((item, index) => {
    const total = readNumber(item, "total") ?? 0;
    const completed = readNumber(item, "completed") ?? 0;
    return {
      id: readText(item, "id") || `m-${index}`,
      title: readText(item, "title", "name", "milestone") || `阶段 ${index + 1}`,
      status: normalizePlanStatus(readText(item, "status")),
      stepIds: readArray(item, "step_ids", "stepIds").map((id) => String(id)),
      total,
      completed,
      percent: readNumber(item, "percent") ?? (total > 0 ? Math.round((completed / total) * 100) : 0),
    };
  });
}

/** 本地算进度。后端给了 `plan_progress` 就用后端的——两边算不一致时以后端为准。 */
export function computeProgress(steps: PlanStepView[]): PlanProgressView {
  const total = steps.length;
  const completed = steps.filter((step) => step.status === "completed").length;
  const inProgress = steps.filter((step) => step.status === "in_progress").length;
  return {
    total,
    completed,
    inProgress,
    pending: total - completed - inProgress,
    percent: total > 0 ? Math.round((completed / total) * 100) : 0,
  };
}

export function parseProgress(source: unknown, steps: PlanStepView[]): PlanProgressView {
  const fallback = computeProgress(steps);
  if (!source || typeof source !== "object") return fallback;
  return {
    total: readNumber(source, "total") ?? fallback.total,
    completed: readNumber(source, "completed") ?? fallback.completed,
    inProgress: readNumber(source, "in_progress", "inProgress") ?? fallback.inProgress,
    pending: readNumber(source, "pending") ?? fallback.pending,
    percent: readNumber(source, "percent") ?? fallback.percent,
  };
}

/**
 * 把步骤按里程碑分组。没归组的排最后，标题为"未分组"。
 *
 * 空里程碑也要出现：阶段划分本身就是信息，"这个阶段还没有步骤"比"这个阶段不存在"
 * 更常见，也更容易被误读成丢了数据。
 */
export function groupByMilestone(
  steps: PlanStepView[],
  milestones: MilestoneView[],
): { milestone: MilestoneView | null; steps: PlanStepView[] }[] {
  const known = new Set(milestones.map((item) => item.id));
  const groups: { milestone: MilestoneView | null; steps: PlanStepView[] }[] = milestones.map(
    (milestone) => ({
      milestone,
      steps: steps.filter((step) => step.milestoneId === milestone.id),
    }),
  );
  const loose = steps.filter((step) => !step.milestoneId || !known.has(step.milestoneId));
  if (loose.length > 0) groups.push({ milestone: null, steps: loose });
  return groups;
}

export function statusMark(status: PlanStatus): string {
  if (status === "completed") return "✓";
  if (status === "in_progress") return "·";
  return "";
}

export function statusLabel(status: PlanStatus): string {
  if (status === "completed") return "已完成";
  if (status === "in_progress") return "进行中";
  return "待开始";
}
