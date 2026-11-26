/**
 * 批量归档的纯逻辑。
 *
 * 放在组件外面有两个理由：侧栏的选择规则要能被单测直接钉住（仓库没有 React
 * 测试库），而"哪些能选、选了几条、提示怎么说"本身就是判定，不该埋在 JSX 里。
 */

import type { BatchArchiveResult, Thread } from "../tasks/api";

/** 尚未结束的轮次状态。归档它们不停止任务，但确认框必须把这件事说清楚。 */
export const UNFINISHED_TURN_STATUSES = ["draft", "queued", "running", "waiting_approval", "paused"] as const;

/** 已归档列表一次取多少条；与 bot-api.ts 的取数上限保持一致。达到上限时提示只显示最近这些。 */
export const ARCHIVED_FETCH_LIMIT = 200;

/** 只有活跃会话能被归档：已归档的不出现在选择列表里。 */
export function isArchivable(thread: Thread): boolean {
  return thread.status === "active";
}

/** 按状态分成「最近」与「已归档」两段。未知状态留在活跃段，但 isArchivable 会拒绝选择。 */
export function partitionThreads(threads: Thread[]): { active: Thread[]; archived: Thread[] } {
  const active: Thread[] = [];
  const archived: Thread[] = [];
  for (const thread of threads) {
    if (thread.status === "archived") archived.push(thread);
    else active.push(thread);
  }
  return { active, archived };
}

/** 切换一条的选中状态。返回新集合，不修改传入的那个。 */
export function toggleSelected(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/**
 * 全选当前可见的活跃会话。
 *
 * 已选里的其他 id 保留：列表刷新可能让某些行暂时不可见，但用户没取消选择，
 * 就不该悄悄丢掉。已归档的行即使被误传进来也不会被选中。
 */
export function selectAllVisible(
  visible: Thread[],
  selected: ReadonlySet<string> = new Set(),
): Set<string> {
  const next = new Set(selected);
  for (const thread of visible) {
    if (isArchivable(thread)) next.add(thread.id);
  }
  return next;
}

/** 选中项里还有多少条尚未结束（确认框据此插入"任务会在后台继续"）。 */
export function countUnfinished(threads: Thread[]): number {
  const unfinished = new Set<string>(UNFINISHED_TURN_STATUSES);
  return threads.filter((thread) => unfinished.has(thread.lastTurnStatus)).length;
}

/** 整批确认的文案。一次操作影响多条，只弹一次，但必须把不可逆感与边界说清楚。 */
export function archiveConfirmMessage(count: number, unfinished: number): string {
  const lines = [
    `归档所选 ${count} 条会话？`,
    "",
    "归档后它们会从「最近」移到「已归档」，不再接受新的一轮；",
    "已有轮次全部保留，可随时取消归档。",
  ];
  if (unfinished > 0) {
    lines.push(
      "",
      `其中 ${unfinished} 条尚未结束（草稿/排队/执行中/等待审批/暂停）：`,
      "归档不会停止它们，任务会在后台继续。",
    );
  }
  return lines.join("\n");
}

/**
 * 批量归档后的提示。
 *
 * unchanged 也算成功：那条会话本来就已经归档，用户的目标已经达成；
 * missing 单独说出来，不能把"已经不存在"混进"已归档"里。
 */
export function summarizeBatchResult(result: BatchArchiveResult): string {
  const done = result.archived.length + result.unchanged.length;
  if (done === 0) {
    return result.missing.length > 0
      ? `所选 ${result.missing.length} 条会话已不存在，没有可归档的会话`
      : "没有可归档的会话";
  }
  const base = `已归档 ${done} 条会话，可在「已归档」中找回`;
  return result.missing.length > 0 ? `${base}；${result.missing.length} 条已不存在` : base;
}
