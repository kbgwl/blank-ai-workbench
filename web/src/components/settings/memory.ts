/**
 * 记忆面板的纯逻辑：类型文案、容量提示、按段落分组。
 *
 * 放在 .ts 里是为了能单测——"写满了会丢最旧的"这件事，界面必须说清楚，
 * 说错了比不说更糟（用户以为记着，其实早被裁掉了）。
 */
import type { MemoryEntry, ProjectMemory } from "./api";

const KIND_LABELS: Record<string, string> = {
  user: "你是谁",
  feedback: "做事方式",
  project: "项目事实",
  reference: "外部资源",
};

/**
 * 条目上那个小标签写什么。
 *
 * 优先用后端给的 `label`：老条目没有 `[类型]` 标记，但它们开头有「结果：」
 * 「验证命令：」这类前缀，那套前缀的解释权在存储层，前端不该各写一份正则。
 * 两样都没有才落到"未分类"——那是真的没线索，不是"分类未知"。
 */
export function memoryKindLabel(entry: Pick<MemoryEntry, "kind" | "label">): string {
  const kind = (entry.kind ?? "").toLowerCase();
  const named = KIND_LABELS[kind];
  if (named) return named;
  return (entry.label ?? "").trim() || "未分类";
}

/** 类型标签的固定顺序：让同一类永远挨在一起，扫读时不用找。 */
export const MEMORY_KIND_ORDER = ["user", "feedback", "project", "reference"] as const;

export type MemoryGroup = { section: string; entries: MemoryEntry[] };

/**
 * 按段落（`## 日期 · 标题`）分组。
 *
 * 分组而不是摊平：同一个日期同一件事记下的几条要能看出是一伙的，
 * 摊平之后「用户偏好简洁回答」和「后端跑在 8000」看起来同样重要。
 * 段落的先后按文件里的顺序倒过来——最新记的在最上面。
 */
export function groupMemory(entries: readonly MemoryEntry[]): MemoryGroup[] {
  const order: string[] = [];
  const bucket = new Map<string, MemoryEntry[]>();
  for (const entry of entries) {
    const key = entry.section || "";
    if (!bucket.has(key)) {
      bucket.set(key, []);
      order.push(key);
    }
    bucket.get(key)!.push(entry);
  }
  return order
    .reverse()
    .map((section) => ({ section, entries: bucket.get(section) ?? [] }));
}

/**
 * 容量那句怎么说。
 *
 * 记忆写满会按最旧的段落开始丢。这件事用户必须知道，否则他会以为三个月前
 * 交代过的事还在——而实际早就被裁掉了，于是助手"忘了"，他还以为是谁的错。
 */
export function memoryUsage(memory: ProjectMemory | null): { text: string; tone: "plain" | "warn" } {
  if (!memory || !memory.exists) return { text: "这个工作区还没有记忆。", tone: "plain" };
  const limit = memory.limit > 0 ? memory.limit : 0;
  const chars = Math.max(0, memory.chars);
  if (!limit) return { text: `${memory.entries.length} 条 · ${chars} 字`, tone: "plain" };
  const percent = Math.min(100, Math.round((chars / limit) * 100));
  const head = `${memory.entries.length} 条 · ${chars} / ${limit} 字`;
  if (percent >= 90) return { text: `${head}（快满了，写新的会把最旧的挤掉）`, tone: "warn" };
  if (percent >= 70) return { text: `${head}（${percent}%）`, tone: "plain" };
  return { text: head, tone: "plain" };
}
