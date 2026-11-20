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

/** 还在动的那几条。failed / interrupted 不算——它们是"去找"的兜底入口，不是"在等"。 */
export function liveBackgroundTasks(tasks: Task[]): Task[] {
  return tasks.filter((task) => ["queued", "running", "waiting_approval", "paused"].includes(task.status));
}

export type BackgroundRow = {
  id: string;
  title: string;
  status: string;
  /** 「跑了 12 分」/「等了 3 分钟」：两种状态下这句话的意思完全不同。 */
  elapsed: string;
  /** 卡在等人点东西上。这一格同时决定行首用什么颜色。 */
  waiting: boolean;
  /** 能不能点停止。排队中和在跑能停；等审批的也用同一个取消，所以一并给。 */
  stoppable: boolean;
  target: BackgroundTarget;
};

function minutes(ms: number): string {
  const total = Math.max(0, Math.round(ms / 60_000));
  if (total < 1) return "不到 1 分钟";
  if (total < 60) return `${total} 分钟`;
  const hours = Math.floor(total / 60);
  return `${hours} 小时${total % 60 ? ` ${total % 60} 分` : ""}`;
}

/**
 * 后台任务清单的每一行。
 *
 * 原来顶部只有一格「后台 3 个」，点它跳到其中一条——另外两条既看不见也点不到。
 * 三条同时在跑的时候，用户只能挨个翻任务列表；而"哪条在跑、跑了多久、卡在哪"
 * 恰恰是他要判断"要不要等"的全部依据。
 *
 * 只收还在动的（见 liveBackgroundTasks）：这份清单回答的是"现在有什么在跑"。
 * 已经失败/结束的不进来——它们不是"在等"，混进来会让数字变成噪音，
 * 要找它们走任务列表。
 *
 * 排序：在跑的排前面，然后按开始时间倒序。等审批的夹在中间——
 * 它不动，但它需要人动手，排在"已经在跑"的后面比埋在失败里好。
 */
export function backgroundRows(tasks: Task[], now: number = Date.now()): BackgroundRow[] {
  const order: Record<string, number> = { running: 0, waiting_approval: 1, queued: 2, paused: 3 };
  return liveBackgroundTasks(tasks)
    .sort((a, b) => {
      const rank = (order[a.status] ?? 9) - (order[b.status] ?? 9);
      if (rank !== 0) return rank;
      return Date.parse(b.createdAt ?? "") - Date.parse(a.createdAt ?? "");
    })
    .map((task) => {
      const started = Date.parse(String(task.createdAt ?? "").replace(" ", "T"));
      const span = Number.isFinite(started) ? now - started : null;
      const waiting = task.status === "waiting_approval";
      const running = task.status === "running";
      const elapsed =
        span === null
          ? ""
          : waiting
            ? `已等 ${minutes(span)}`
            : running
              ? `跑了 ${minutes(span)}`
              : task.status === "queued"
                ? `排了 ${minutes(span)}`
                : `停了 ${minutes(span)}`;
      return {
        id: task.id,
        title: task.title || "未命名任务",
        status: task.status,
        elapsed,
        waiting,
        stoppable: ["queued", "running", "waiting_approval", "paused"].includes(task.status),
        target: backgroundTaskTarget(task),
      };
    });
}

/**
 * 一条后台任务该不该在清单里出现。
 *
 * 正在看的那条不重复报——它就在眼前，再看一次只会让人以为有两份。
 * 但没有 threadId 的孤儿任务**必须**留下：它们恰恰是最容易"关窗后找不到"的一批。
 */
export function backgroundRowsFor(
  tasks: Task[],
  currentThreadId: string | null | undefined,
  now: number = Date.now(),
): BackgroundRow[] {
  return backgroundRows(visibleBackgroundTasks(tasks, currentThreadId), now);
}

