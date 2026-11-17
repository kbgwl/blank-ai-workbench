"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { fetchProjects, fetchTasks, type Project, type Task } from "@/components/tasks/api";
import { HealthPill } from "@/components/health-pill";
import { ThemeToggle } from "@/components/theme-toggle";
import { isNavActive } from "@/components/bot/nav";
import { PALETTE_SHORTCUT, shortcutHint } from "@/lib/shortcuts";

/**
 * 全局左栏。
 *
 * 布局对标 Qoder：品牌 → 新建/搜索 → 工作区 → 最近任务 → 固定入口 → 设置/外观/状态。
 * 首页与任务工作台自带更具体的左栏，故不由这里渲染。
 */
export function AppRail() {
  const pathname = usePathname();
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [projectsError, setProjectsError] = useState(false);
  const [tasksError, setTasksError] = useState(false);

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
    fetchTasks()
      .then((rows) => setTasks(rows.slice(0, 8)))
      .catch(() => {
        setTasks([]);
        setTasksError(true);
      });
  }, []);

  const openPalette = () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
  };

  // 多个项目可能绑定同一个目录，按目录合并，否则左栏会出现一整列同名工作区。
  const workspaces = useMemo(() => {
    const groups = new Map<string, { key: string; label: string; count: number }>();
    for (const project of projects) {
      const first = project.workspaces[0];
      const label = first?.label || project.name;
      const key = first?.path || label;
      const found = groups.get(key);
      if (found) found.count += 1;
      else groups.set(key, { key, label, count: 1 });
    }
    return [...groups.values()];
  }, [projects]);

  return (
    <aside className="flex w-[248px] shrink-0 flex-col bg-sidebar">
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-logo-bg">
          <span className="size-2 rounded-[2px] bg-logo-accent" />
        </span>
        <span className="text-[13px] font-semibold tracking-tight">空白格</span>
        <span className="rounded bg-surface-3 px-1 py-0.5 font-mono text-[9px] tracking-wide text-muted uppercase">
          bot
        </span>
      </div>

      <div className="px-2 pb-1">
        <Link
          href="/"
          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] font-medium text-text transition-colors hover:bg-surface-2"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} className="size-3.5 shrink-0 text-muted" aria-hidden>
            <path d="M12 5v14M5 12h14" />
          </svg>
          新的任务
        </Link>
      </div>

      <div className="px-2 pb-2">
        <button
          type="button"
          onClick={openPalette}
          className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5 shrink-0 text-faint" aria-hidden>
            <path d="M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16.5 16.5 21 21" />
          </svg>
          搜索
          <kbd className="ml-auto font-mono text-[10px] text-faint">{shortcutHint(PALETTE_SHORTCUT)}</kbd>
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <Group
          action={
            <Link
              href="/projects"
              title="管理工作区"
              aria-label="管理工作区"
              className="grid size-4 place-items-center rounded text-faint transition-colors hover:bg-surface-2 hover:text-text"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-3" aria-hidden>
                <circle cx="12" cy="12" r="3" />
                <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
              </svg>
            </Link>
          }
        >
          工作区
        </Group>
        {workspaces.length === 0 && (
          <Link
            href="/projects"
            className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] transition-colors ${
              projectsError
                ? "text-danger hover:bg-danger/10"
                : isNavActive(pathname, "/projects")
                  ? "bg-surface-3 text-text"
                  : "text-muted hover:bg-surface-2 hover:text-text"
            }`}
          >
            <FolderIcon />
            {projectsError ? "工作区读取失败，去项目页重试" : "项目与工作区"}
          </Link>
        )}
        {workspaces.slice(0, 6).map((workspace) => (
          <Link
            key={workspace.key}
            href="/projects"
            title={workspace.key}
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            <FolderIcon />
            <span className="min-w-0 truncate">{workspace.label}</span>
            {workspace.count > 1 && (
              <span className="ml-auto shrink-0 font-mono text-[10px] text-faint">{workspace.count}</span>
            )}
          </Link>
        ))}
        {workspaces.length > 6 && (
          <Link href="/projects" className="flex items-center gap-2 rounded-md px-2 py-1 text-[11.5px] text-faint hover:text-text">
            还有 {workspaces.length - 6} 个工作区…
          </Link>
        )}

        <Group>最近任务</Group>
        {tasks === null && <p className="px-2 py-1 text-[11.5px] text-faint">读取中…</p>}
        {tasks !== null && tasksError && <p className="px-2 py-1 text-[11.5px] text-danger">任务读取失败</p>}
        {tasks !== null && !tasksError && tasks.length === 0 && (
          <p className="px-2 py-1 text-[11.5px] text-faint">还没有任务</p>
        )}
        {(tasks ?? []).map((task) => (
          <Link
            key={task.id}
            href={`/tasks/detail?id=${encodeURIComponent(task.id)}`}
            title={task.title}
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            <span className={`size-1.5 shrink-0 rounded-full ${task.status === "failed" ? "bg-danger" : task.status === "running" ? "bg-brand" : "bg-line-strong"}`} />
            <span className="truncate">{task.title || "未命名任务"}</span>
          </Link>
        ))}

        <div className="my-2 h-px bg-line" />
        <NavRow href="/monitor" label="监控" icon={<PulseIcon />} active={isNavActive(pathname, "/monitor")} />
        <NavRow href="/evidence" label="证据库" icon={<ShieldIcon />} active={isNavActive(pathname, "/evidence")} />
        <NavRow href="/skills" label="技能" icon={<SkillIcon />} active={isNavActive(pathname, "/skills")} />
        <NavRow href="/team" label="Bot 团队" icon={<TeamIcon />} active={isNavActive(pathname, "/team")} />
      </div>

      <div className="border-t border-line px-2 py-2">
        <NavRow href="/settings" label="设置" icon={<GearIcon />} active={isNavActive(pathname, "/settings")} />
        <div className="mt-1 flex items-center gap-1 px-1">
          <ThemeToggle compact />
          <span className="ml-auto">
            <HealthPill compact />
          </span>
        </div>
      </div>
    </aside>
  );
}

function Group({ children, action }: { children: string; action?: ReactNode }) {
  return (
    <div className="flex items-center px-2 pt-3 pb-1 text-[11px] font-medium text-faint">
      <span>{children}</span>
      {action && <span className="ml-auto">{action}</span>}
    </div>
  );
}

function NavRow({ href, label, icon, active }: { href: string; label: string; icon: ReactNode; active: boolean }) {
  return (
    <Link
      href={href}
      className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] transition-colors ${
        active ? "bg-surface-3 font-medium text-text" : "text-muted hover:bg-surface-2 hover:text-text"
      }`}
    >
      {icon}
      {label}
    </Link>
  );
}

const ICON = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, className: "size-3.5 shrink-0 text-faint", "aria-hidden": true };

function FolderIcon() {
  return (
    <svg {...ICON}>
      <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l1.5 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z" />
    </svg>
  );
}
function PulseIcon() {
  return (
    <svg {...ICON}>
      <path d="M3 12h4l2.5-6 4 12 2.5-6H21" />
    </svg>
  );
}
function ShieldIcon() {
  return (
    <svg {...ICON}>
      <path d="M12 3 5 6v5c0 4.2 2.8 7.4 7 9 4.2-1.6 7-4.8 7-9V6l-7-3Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}
function SkillIcon() {
  return (
    <svg {...ICON}>
      <path d="M12 3v3M12 18v3M4.2 7.5l2.6 1.5M17.2 15l2.6 1.5M4.2 16.5l2.6-1.5M17.2 9l2.6-1.5" />
      <circle cx="12" cy="12" r="3.2" />
    </svg>
  );
}
function TeamIcon() {
  return (
    <svg {...ICON}>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S13.9 16 14.5 19M15.5 5.5a3 3 0 0 1 0 6M17 14.8c2.1.3 3.3 1.7 3.7 4.2" />
    </svg>
  );
}
function GearIcon() {
  return (
    <svg {...ICON}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
    </svg>
  );
}
