"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  deleteTask,
  errorMessage,
  fetchProjects,
  fetchProviders,
  fetchTasks,
  type Project,
  type Provider,
  type Task,
} from "@/components/tasks/api";
import { statusMeta, taskMatchesFilter, TASK_STATUS_FILTERS, type TaskStatusFilter } from "@/components/tasks/status";
import { diffStats, formatDateTime } from "@/components/tasks/format";
import { HealthPill } from "@/components/health-pill";

/**
 * 任务列表页：只负责"找任务"。
 *
 * 会话（消息、步骤、审批卡、输入框、右栏）只由首页画，这里点一行就带着
 * `?thread=` 回首页打开那条会话——同一个决定不写第二遍。
 */

/** 历史任务里可能存着早先版本拼的规则前缀，列表摘要只展示用户真正写的那句。 */
function userText(prompt: string): string {
  if (!prompt) return "";
  const marker = prompt.lastIndexOf("用户任务：");
  const tail = marker >= 0 ? prompt.slice(marker + "用户任务：".length) : prompt;
  return tail
    .split("\n")
    .filter((line) => !line.startsWith("执行规则：") && !line.trim().startsWith("你处于"))
    .join("\n")
    .trim();
}

/**
 * 任务行的状态点。
 *
 * 不复用 `bot/status-dot.tsx`：那个回答的是「这一轮跑成没有」，只有跑着/失败/完成
 * 三档；任务多两档（待审批、已暂停），把「待审批」画成蓝点就把"要你动手"这件事弄丢了。
 * 成功不画点：一屏十几行都是「已完成」时，排出一列绿点等于没有信息。
 */
function Dot({ status }: { status: string }) {
  const meta = statusMeta(status);
  const key = (status ?? "").toLowerCase();
  if (key === "succeeded" || key === "completed") return <span className="size-1.5 shrink-0" aria-hidden />;
  return (
    <span
      title={meta.label}
      aria-label={meta.label}
      className={`size-1.5 shrink-0 rounded-full ${meta.dot.replace("animate-pulse", "")}`}
    />
  );
}

/** 一句话结论：失败先看原因，其次看交付摘要，最后退回用户原话。 */
function summaryLine(task: Task): string {
  const text = (task.status === "failed" ? task.failureReason : "") || task.resultSummary || userText(task.prompt);
  return text.replace(/\s+/g, " ").trim();
}

/** 计划进度优先（几/几步），没有计划时退回已用步数。 */
function progressLabel(task: Task): string {
  if (task.planProgress.total > 0) return `计划 ${task.planProgress.completed}/${task.planProgress.total}`;
  if (task.stepsUsed !== null) return `${task.stepsUsed} 步`;
  return "";
}

/**
 * 列表里的改动行数。
 *
 * 列表接口目前不带改动明细，所以只有当后端把 `changes` / `diff` 直接塞进任务里时
 * 才算得出来；算不出来就返回 null，不去为每一行补一次证据请求（那是 N+1）。
 */
function diffOf(task: Task): { added: number; removed: number } | null {
  const raw = task.raw ?? {};
  const changes = Array.isArray(raw.changes) ? raw.changes : [];
  let added = 0;
  let removed = 0;
  let found = false;
  for (const change of changes) {
    if (!change || typeof change !== "object") continue;
    const diff = (change as Record<string, unknown>).diff;
    if (typeof diff !== "string" || !diff) continue;
    const stat = diffStats(diff);
    added += stat.added;
    removed += stat.removed;
    found = true;
  }
  if (found) return { added, removed };
  const merged = typeof raw.diff === "string" ? raw.diff : typeof raw.full_diff === "string" ? raw.full_diff : "";
  return merged ? diffStats(merged) : null;
}

function TaskRow({
  task,
  deleting,
  onOpen,
  onDelete,
}: {
  task: Task;
  deleting: boolean;
  onOpen: (task: Task) => void;
  onDelete: (task: Task) => void;
}) {
  const meta = statusMeta(task.status);
  const summary = summaryLine(task);
  const progress = progressLabel(task);
  const diff = diffOf(task);
  // threadId 为空是"这一轮没有关联会话"的老任务；不能凭空造一个能点的链接。
  const canOpen = Boolean(task.threadId);

  const body = (
    <>
      <Dot status={task.status} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className={`min-w-0 truncate text-[14px] ${canOpen ? "text-text" : "text-muted"}`}>
            {task.title}
          </span>
          <span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[11px] font-medium ${meta.chip}`}>
            {meta.label}
          </span>
        </span>
        {summary && (
          <span className="mt-1 block truncate text-[12px] text-muted" title={summary}>
            {summary}
          </span>
        )}
        <span className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-faint">
          {task.updatedAt && <span className="font-mono">{formatDateTime(task.updatedAt)}</span>}
          {progress && <span>{progress}</span>}
          {diff && (diff.added > 0 || diff.removed > 0) && (
            <span className="font-mono text-warn">
              +{diff.added} −{diff.removed}
            </span>
          )}
        </span>
      </span>
      <span className={`hidden shrink-0 self-center text-[12px] sm:inline ${canOpen ? "text-faint group-hover:text-text" : "text-faint/70"}`}>
        {canOpen ? "打开会话 →" : "这一轮没有关联会话"}
      </span>
    </>
  );

  return (
    <div className="group flex items-stretch border-b border-line last:border-0 hover:bg-surface-2">
      {canOpen ? (
        <button
          type="button"
          onClick={() => onOpen(task)}
          title={task.title}
          className="flex min-w-0 flex-1 items-start gap-3 px-4 py-3 text-left"
        >
          {body}
        </button>
      ) : (
        <div title="这一轮没有关联会话（老任务没有会话 id）" className="flex min-w-0 flex-1 items-start gap-3 px-4 py-3">
          {body}
        </div>
      )}
      <button
        type="button"
        onClick={() => onDelete(task)}
        disabled={deleting}
        title="删除这条任务"
        aria-label={`删除 ${task.title}`}
        className="grid w-10 shrink-0 place-items-center text-faint/60 transition-colors hover:text-danger disabled:opacity-40"
      >
        <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6" />
        </svg>
      </button>
    </div>
  );
}

export function Workbench() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsError, setProjectsError] = useState(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [tasksError, setTasksError] = useState(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatusFilter>("all");
  const [deleting, setDeleting] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadTasks = useCallback(() => {
    return fetchTasks()
      .then((rows) => {
        setTasks(rows);
        setTasksError(false);
      })
      .catch((error: unknown) => {
        setTasks((current) => current ?? []);
        setTasksError(true);
        setNotice(errorMessage(error));
      });
  }, []);

  useEffect(() => {
    fetchProjects()
      .then((rows) => {
        setProjects(rows);
        setProjectsError(false);
      })
      .catch(() => {
        setProjects([]);
        setProjectsError(true);
      });
    fetchProviders().then(setProviders).catch(() => setProviders([]));
    void loadTasks();
  }, [loadTasks]);

  // 列表页没有事件流可跟，靠低频轮询让运行中的状态自己走到终态。
  // 页面在后台时不问，省掉无意义的请求。
  useEffect(() => {
    const timer = setInterval(() => {
      if (!document.hidden) void loadTasks();
    }, 15_000);
    return () => clearInterval(timer);
  }, [loadTasks]);

  const openTask = useCallback(
    (item: Task) => {
      if (!item.threadId) return;
      router.push(`/?thread=${encodeURIComponent(item.threadId)}`);
    },
    [router],
  );

  /** 删除任务：事件、改动、检查点一起删，不可恢复，所以先确认一次。 */
  const onDelete = useCallback(async (item: Task) => {
    if (!window.confirm(`删除「${item.title}」？\n这条任务的事件、改动记录和检查点会一起删掉，不能恢复。`)) return;
    setDeleting(item.id);
    setNotice(null);
    try {
      await deleteTask(item.id);
      setTasks((current) => (current ?? []).filter((row) => row.id !== item.id));
    } catch (error: unknown) {
      setNotice(errorMessage(error));
    } finally {
      setDeleting(null);
    }
  }, []);

  const filteredTasks = useMemo(() => {
    const list = tasks ?? [];
    const keyword = query.trim().toLowerCase();
    return list.filter((item) => {
      // 状态筛选与关键词叠加：先看属于哪一组，再按标题/结论过滤。
      if (!taskMatchesFilter(item.status, statusFilter)) return false;
      if (!keyword) return true;
      return (
        item.title.toLowerCase().includes(keyword) ||
        item.resultSummary.toLowerCase().includes(keyword) ||
        item.prompt.toLowerCase().includes(keyword)
      );
    });
  }, [tasks, query, statusFilter]);

  const counts = useMemo(() => {
    const list = tasks ?? [];
    const result: Record<TaskStatusFilter, number> = { all: list.length, active: 0, done: 0, failed: 0 };
    for (const item of list) {
      if (taskMatchesFilter(item.status, "active")) result.active += 1;
      if (taskMatchesFilter(item.status, "done")) result.done += 1;
      if (taskMatchesFilter(item.status, "failed")) result.failed += 1;
    }
    return result;
  }, [tasks]);

  // 按项目分组：同一项目下的任务放在一起，比一条长流水更容易扫。
  const groups = useMemo(() => {
    const names = new Map(projects.map((project) => [project.id, project.name]));
    const map = new Map<string, { key: string; label: string; items: Task[] }>();
    for (const item of filteredTasks) {
      const key = item.projectId || "";
      let group = map.get(key);
      if (!group) {
        group = { key, label: names.get(key) || "未归属项目", items: [] };
        map.set(key, group);
      }
      group.items.push(item);
    }
    return [...map.values()];
  }, [filteredTasks, projects]);

  // 同一目录常被多个项目复用，左栏要按目录合并，否则一屏全是同名工作区。
  const workspaceGroups = (() => {
    const map = new Map<string, { key: string; label: string; count: number }>();
    for (const item of projects) {
      const first = item.workspaces[0];
      const label = first?.label || item.name;
      const key = first?.path || label;
      const found = map.get(key);
      if (found) found.count += 1;
      else map.set(key, { key, label, count: 1 });
    }
    return [...map.values()];
  })();

  const provider = providers.find((item) => item.enabled) ?? providers[0] ?? null;

  return (
    <div className="flex h-full min-h-0 bg-bg text-text">
      {/* ── 左栏：导航与工作区 ── */}
      <aside className="flex w-[240px] shrink-0 flex-col border-r border-line bg-sidebar">
        <div className="flex items-center gap-2 px-3 pt-3 pb-2">
          <span className="grid size-6 place-items-center rounded-[7px] bg-logo-bg">
            <span className="size-2.5 rounded-[2px] bg-logo-accent" />
          </span>
          <span className="text-[14px] font-semibold tracking-tight">空白格</span>
          <Link
            href="/"
            className="ml-auto rounded-md px-1.5 py-0.5 text-[12px] text-faint transition-colors hover:bg-surface-2 hover:text-text"
          >
            工作台
          </Link>
        </div>
        <button
          type="button"
          onClick={() => router.push("/")}
          className="mx-1 mb-1 flex items-center gap-2 rounded-md px-3 py-2 text-left text-[14px] font-medium hover:bg-surface-2"
        >
          <Icon d="M12 5v14M5 12h14" />
          新的任务
        </button>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          <Group>工作区</Group>
          {workspaceGroups.length === 0 && (
            <p className={`px-2 py-1 text-[12px] ${projectsError ? "text-danger" : "text-faint"}`}>
              {projectsError ? "工作区读取失败" : "还没有工作区"}
            </p>
          )}
          {workspaceGroups.slice(0, 8).map((item) => (
            <Link
              key={item.key}
              href="/projects"
              title={item.key}
              className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-[14px] text-muted hover:bg-surface-3 hover:text-text"
            >
              <Icon d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
              <span className="min-w-0 truncate">{item.label}</span>
              {item.count > 1 && <span className="ml-auto shrink-0 font-mono text-[11px] text-faint">{item.count}</span>}
            </Link>
          ))}
          {workspaceGroups.length > 8 && (
            <Link href="/projects" className="flex items-center gap-2 rounded-md px-2 py-1 text-[12px] text-faint hover:text-text">
              还有 {workspaceGroups.length - 8} 个工作区…
            </Link>
          )}
        </div>

        <div className="border-t border-line px-2 py-2">
          <RailLink href="/projects" label="项目与工作区" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
          <RailLink href="/settings" label="设置" d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4 0a1.7 1.7 0 0 0 .3 1.9 2 2 0 1 1-2.8 2.8 1.7 1.7 0 0 0-2.9 1.2 2 2 0 0 1-4 0 1.7 1.7 0 0 0-1.9-1.2 2 2 0 1 1-2.8-2.8A1.7 1.7 0 0 0 4.6 15a2 2 0 0 1 0-4 1.7 1.7 0 0 0 1.2-1.1 1.7 1.7 0 0 0-.3-1.9 2 2 0 1 1 2.8-2.8A1.7 1.7 0 0 0 11 4.6a2 2 0 0 1 4 0 1.7 1.7 0 0 0 2.9 1.2 2 2 0 1 1 2.8 2.8 1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1 2 2 0 0 1 0 4z" />
          <div className="my-1.5 h-px bg-line" />
          <p className="truncate px-2 pt-1 text-[11px] text-faint">{provider?.model || "跟随默认模型"}</p>
          <p className="px-2 pt-0.5">
            <HealthPill compact />
          </p>
        </div>
      </aside>

      {/* ── 主栏：全部任务 ── */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
        <header className="flex h-[47px] shrink-0 items-center gap-2.5 border-b border-line bg-bg px-5">
          <h1 className="text-[14px] font-semibold">全部任务</h1>
          <span className="rounded-full border border-line bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-faint">
            {counts.all}
          </span>
          <button
            type="button"
            onClick={() => router.push("/")}
            className="ml-auto rounded-md bg-brand px-2.5 py-1 text-[13px] font-medium text-on-brand transition-colors hover:brightness-110"
          >
            新建任务
          </button>
        </header>

        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-bg px-4 py-2">
          <div className="flex min-w-[220px] flex-1 items-center gap-2 rounded-md border border-line bg-surface px-2.5 py-1.5 text-[13px] text-faint">
            <Icon d="M11 11a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM20 20l-3.5-3.5" box={24} />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索任务标题与结论"
              className="min-w-0 flex-1 bg-transparent text-text outline-none placeholder:text-faint"
            />
          </div>
          <div className="flex flex-nowrap items-center gap-1 overflow-x-auto" role="group" aria-label="按状态筛选任务">
            {TASK_STATUS_FILTERS.map((item) => {
              const active = statusFilter === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setStatusFilter(item.key)}
                  className={`shrink-0 rounded px-2 py-1 text-[12px] whitespace-nowrap transition-colors ${
                    active ? "bg-brand/10 font-medium text-brand" : "text-faint hover:bg-surface-2 hover:text-text"
                  }`}
                >
                  {item.label}
                  <span className="ml-1 font-mono text-[11px] opacity-70">{counts[item.key]}</span>
                </button>
              );
            })}
          </div>
        </div>

        {notice && (
          <div className="mx-4 mt-3 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-warn">{notice}</div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {tasks === null && <p className="px-5 py-4 text-[12px] text-faint">读取中…</p>}
          {tasks !== null && filteredTasks.length === 0 && (
            <p className={`px-5 py-4 text-[12px] ${tasksError ? "text-danger" : "text-faint"}`}>
              {tasksError
                ? "任务读取失败"
                : statusFilter !== "all" || query
                  ? "这个筛选下没有任务"
                  : "还没有任务，回工作台说一句就开始"}
            </p>
          )}
          {groups.map((group) => (
            <section key={group.key}>
              <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-canvas/95 px-4 py-1.5 backdrop-blur">
                <span className="min-w-0 truncate text-[12px] font-semibold text-muted">{group.label}</span>
                <span className="shrink-0 font-mono text-[11px] text-faint">{group.items.length}</span>
              </div>
              {group.items.map((item) => (
                <TaskRow
                  key={item.id}
                  task={item}
                  deleting={deleting === item.id}
                  onOpen={openTask}
                  onDelete={(task) => void onDelete(task)}
                />
              ))}
            </section>
          ))}
        </div>
      </main>
    </div>
  );
}

function Group({ children }: { children: React.ReactNode }) {
  return <div className="px-2 pt-3 pb-1 text-[12px] font-semibold text-faint">{children}</div>;
}

function RailLink({ href, label, d }: { href: string; label: string; d: string }) {
  return (
    <Link href={href} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted hover:bg-surface-3 hover:text-text">
      <Icon d={d} />
      <span className="truncate">{label}</span>
    </Link>
  );
}

function Icon({ d, box = 16, className = "size-[14px]" }: { d: string; box?: number; className?: string }) {
  return (
    <svg viewBox={`0 0 ${box} ${box}`} className={`${className} shrink-0`} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}
