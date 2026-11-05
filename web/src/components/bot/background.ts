import type { Task } from "../tasks/api";

/**
 * 后台任务横幅的取舍（v164 §2.3）。
 *
 * 关窗后重开要能找到"刚才那条去哪了"。新任务一定带 threadId，但库里可能还留着
 * 早期没有 conversation_id 的孤儿任务——把它们过滤掉，恰好把最容易丢的那批藏起来。
 * 所以这里只排除"正在看的同一条会话"，孤儿一律留下。
 */
export function visibleBackgroundTasks(
  tasks: Task[],
  currentThreadId: string | null | undefined,
): Task[] {
  return tasks.filter((task) => task.threadId !== currentThreadId);
}

export type BackgroundTarget =
  | { kind: "thread"; threadId: string }
  | { kind: "detail"; href: string };

/** 点横幅去哪儿：有会话就回会话，没有就退到按 task_id 打开的任务详情。 */
export function backgroundTaskTarget(task: Task): BackgroundTarget {
  if (task.threadId) return { kind: "thread", threadId: task.threadId };
  return { kind: "detail", href: `/tasks/detail?id=${encodeURIComponent(task.id)}` };
}
