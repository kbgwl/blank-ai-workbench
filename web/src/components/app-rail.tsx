"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { fetchAllThreads, fetchProjects, type Project, type Thread } from "@/components/tasks/api";
import { HealthPill } from "@/components/health-pill";
import { ThemeToggle } from "@/components/theme-toggle";
import { isNavActive } from "@/components/bot/nav";
import { PALETTE_SHORTCUT, shortcutHint } from "@/lib/shortcuts";
import { dayBucket } from "@/components/bot/turns";
import { StatusDot } from "@/components/bot/status-dot";

/**
 * 全站唯一的左栏。默认展开（图标 + 文字，200px），可一键收窄成 52px 图标栏。
 *
 * 之前是固定 52px 纯图标栏——用户反馈"设置页左侧还是收窄的、看不出是什么"。
 * 收窄可以是功能，但不该是默认值：常驻栏要让人一眼看懂有哪些地方可去。
 * 展开态把工作区/最近对话的动态内容内联列出，收窄态才退化成点图标开浮层。
 * 状态存 localStorage，全站一致；首页会话栏自带 Sidebar，不走本栏。
 */
type Flyout = "workspaces" | "tasks" | null;
const RAIL_KEY = "blank.rail.collapsed";

/** 与首页会话栏同一套分组文案：两边结构一致，用户不会觉得"换了个列表"。 */
const DAY_LABEL: Record<ReturnType<typeof dayBucket>, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "最近 7 天",
  earlier: "更早",
};
const DAY_ORDER: ReturnType<typeof dayBucket>[] = ["today", "yesterday", "week", "earlier"];

export function AppRail() {
  const pathname = usePathname();
  const [projects, setProjects] = useState<Project[]>([]);
  const [threads, setThreads] = useState<Thread[] | null>(null);
  const [projectsError, setProjectsError] = useState(false);
  const [threadsError, setThreadsError] = useState(false);
  const [flyout, setFlyout] = useState<Flyout>(null);
  // 默认展开；只有用户显式收窄过才记住 collapsed=true。
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    setCollapsed(window.localStorage.getItem(RAIL_KEY) === "1");
    fetchProjects()
      .then((rows) => {
        setProjects(rows);
        setProjectsError(false);
      })
      .catch(() => {
        setProjects([]);
        setProjectsError(true);
      });
    // 会话列表与首页同源：同一个 /threads 接口，切页面条数不变。
    fetchAllThreads()
      .then((rows) => setThreads(rows.slice(0, 12)))
      .catch(() => {
        setThreads([]);
        setThreadsError(true);
      });
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      window.localStorage.setItem(RAIL_KEY, current ? "0" : "1");
      return !current;
    });
  };

  // 切页后收起浮层，避免它盖在新页面上。
  useEffect(() => {
    setFlyout(null);
  }, [pathname]);

  const openPalette = () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
  };

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

  const groupedThreads = useMemo(() => {
    const buckets = new Map<ReturnType<typeof dayBucket>, Thread[]>();
    for (const thread of threads ?? []) {
      const bucket = dayBucket(thread.updatedAt || thread.createdAt);
      buckets.set(bucket, [...(buckets.get(bucket) ?? []), thread]);
    }
    return DAY_ORDER.filter((bucket) => (buckets.get(bucket) ?? []).length > 0).map((bucket) => ({
      bucket,
      label: DAY_LABEL[bucket],
      items: buckets.get(bucket) ?? [],
    }));
  }, [threads]);

  const toggle = (next: Exclude<Flyout, null>) => setFlyout((current) => (current === next ? null : next));

  if (collapsed) {
    return (
      <aside className="relative z-20 flex w-[52px] shrink-0 flex-col items-center gap-1 border-r border-line bg-sidebar py-3">
        <RailButton title="展开侧栏" onClick={toggleCollapsed} active={false}>
          <ExpandIcon />
        </RailButton>
        <Link
          href="/"
          title="空白格 · 回到工作台"
          className="grid size-8 place-items-center rounded-lg transition-colors hover:bg-surface-2"
        >
          <span className="grid size-6 place-items-center rounded-md bg-logo-bg">
            <span className="size-2 rounded-[2px] bg-logo-accent" />
          </span>
        </Link>
        <RailButton title="新的任务" onClick={() => (window.location.href = "/")} active={false}>
          <PlusIcon />
        </RailButton>
        <RailButton title={`搜索（${shortcutHint(PALETTE_SHORTCUT)}）`} onClick={openPalette} active={false}>
          <SearchIcon />
        </RailButton>
        <span className="my-1 h-px w-6 bg-line" />
        <div className="relative">
          <RailButton title="工作区" onClick={() => toggle("workspaces")} active={flyout === "workspaces"}>
            <FolderIcon />
          </RailButton>
          {flyout === "workspaces" && (
            <FlyoutPanel title="工作区" onClose={() => setFlyout(null)}>
              {projectsError && <FlyoutLine tone="danger">工作区读取失败，去项目页重试</FlyoutLine>}
              {!projectsError && workspaces.length === 0 && <FlyoutLine>还没有工作区</FlyoutLine>}
              {workspaces.slice(0, 8).map((workspace) => (
                <FlyoutRow key={workspace.key} href="/projects" title={workspace.key} label={workspace.label} badge={workspace.count > 1 ? String(workspace.count) : undefined} />
              ))}
              <Link href="/projects" onClick={() => setFlyout(null)} className="mt-1 block px-2.5 py-1.5 text-[12px] text-faint hover:text-text">管理工作区 →</Link>
            </FlyoutPanel>
          )}
        </div>
        <div className="relative">
          <RailButton title="最近对话" onClick={() => toggle("tasks")} active={flyout === "tasks"}>
            <ClockIcon />
          </RailButton>
          {flyout === "tasks" && (
            <FlyoutPanel title="最近对话" onClose={() => setFlyout(null)}>
              {threads === null && <FlyoutLine>读取中…</FlyoutLine>}
              {threads !== null && threadsError && <FlyoutLine tone="danger">会话读取失败</FlyoutLine>}
              {threads !== null && !threadsError && threads.length === 0 && <FlyoutLine>还没有对话</FlyoutLine>}
              {(threads ?? []).map((thread) => (
                <FlyoutRow key={thread.id} href={`/?thread=${encodeURIComponent(thread.id)}`} title={thread.title} label={thread.title || "未命名对话"} dot={thread.lastTurnStatus === "failed" ? "danger" : thread.lastTurnStatus === "running" ? "brand" : thread.lastTurnStatus ? "done" : "muted"} />
              ))}
              <Link href="/tasks" onClick={() => setFlyout(null)} className="mt-1 block px-2.5 py-1.5 text-[12px] text-faint hover:text-text">全部对话 →</Link>
            </FlyoutPanel>
          )}
        </div>
        <div className="mt-auto flex flex-col items-center gap-1">
          <span className="my-1 h-px w-6 bg-line" />
          <RailLink href="/settings" title="设置" icon={<GearIcon />} active={isNavActive(pathname, "/settings")} />
          <ThemeToggle compact />
          <HealthPill compact />
        </div>
      </aside>
    );
  }

  return (
    // 240px 是全站统一的左栏宽度：首页会话栏、任务列表都对齐这一档，
    // 切换页面时左边缘不动。之前 200/308/308 三套宽度，一切页就跳。
    <aside className="relative z-20 flex w-[240px] shrink-0 flex-col gap-1 border-r border-line bg-sidebar px-2 py-3">
      {/* 品牌 + 收窄按钮 */}
      <div className="flex items-center gap-2 px-1 pb-1">
        <Link href="/" title="空白格 · 回到工作台" className="grid size-7 shrink-0 place-items-center rounded-lg transition-colors hover:bg-surface-2">
          <span className="grid size-6 place-items-center rounded-md bg-logo-bg">
            <span className="size-2 rounded-[2px] bg-logo-accent" />
          </span>
        </Link>
        <Link href="/" className="min-w-0 flex-1 truncate text-[13px] font-semibold tracking-tight hover:text-brand">空白格</Link>
        <button type="button" onClick={toggleCollapsed} title="收窄侧栏" aria-label="收窄侧栏" className="grid size-7 shrink-0 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 hover:text-text">
          <CollapseIcon />
        </button>
      </div>

      <WideButton icon={<PlusIcon />} label="新的任务" onClick={() => (window.location.href = "/")} />
      <WideButton icon={<SearchIcon />} label={`搜索（${shortcutHint(PALETTE_SHORTCUT)}）`} onClick={openPalette} />

      <span className="my-1 h-px bg-line" />

      <WideLink icon={<FolderIcon />} label="工作区" href="/projects" active={isNavActive(pathname, "/projects")} />

      {/* 会话列表：按天分组，与首页会话栏同一套标题（今天 / 昨天 / 最近 7 天 / 更早）。
          之前这里是平铺一串，而首页是分组的——同一条记录两边看着"不一样"，其实是结构不同。 */}
      <div className="flex min-h-0 flex-col overflow-y-auto">
        {threads === null && <p className="px-2 py-1 text-[12px] text-faint">读取中…</p>}
        {threads !== null && threadsError && <p className="px-2 py-1 text-[12px] text-danger">会话读取失败</p>}
        {threads !== null && !threadsError && threads.length === 0 && <p className="px-2 py-1 text-[12px] text-faint">还没有对话</p>}
        {groupedThreads.map((group) => (
          <div key={group.bucket} className="flex flex-col gap-0.5 pb-1">
            <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-faint">{group.label}</div>
            {group.items.map((thread) => (
              <Link
                key={thread.id}
                href={`/?thread=${encodeURIComponent(thread.id)}`}
                title={thread.title}
                className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
              >
                <StatusDot status={thread.lastTurnStatus} />
                <span className="min-w-0 flex-1 truncate">{thread.title || "未命名对话"}</span>
              </Link>
            ))}
          </div>
        ))}
        <Link href="/tasks" className="px-2 py-1.5 text-[12px] text-faint hover:text-text">全部对话 →</Link>
      </div>

      {/* 底部三项与首页会话栏同一套：Bot 团队 / 设置 / 皮肤。
          以前这里只有「设置」，一进设置页左下角就少了一项，看着像换了套导航。 */}
      <div className="mt-auto flex flex-col gap-1 border-t border-line pt-2">
        <WideLink icon={<TeamIcon />} label="Bot 团队" href="/team" active={isNavActive(pathname, "/team")} />
        <WideLink icon={<GearIcon />} label="设置" href="/settings" active={isNavActive(pathname, "/settings")} />
        <div className="flex items-center justify-between px-1 pt-0.5">
          <ThemeToggle compact />
          <HealthPill compact />
        </div>
      </div>
    </aside>
  );
}

function RailButton({ title, onClick, active, children }: { title: string; onClick: () => void; active: boolean; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} title={title} aria-label={title}
      className={`grid size-8 place-items-center rounded-lg transition-colors hover:bg-surface-2 hover:text-text ${active ? "bg-surface-3 text-text" : "text-muted"}`}>
      {children}
    </button>
  );
}

function RailLink({ href, title, icon, active }: { href: string; title: string; icon: ReactNode; active: boolean }) {
  return (
    <Link href={href} title={title} aria-label={title}
      className={`grid size-8 place-items-center rounded-lg transition-colors hover:bg-surface-2 hover:text-text ${active ? "bg-surface-3 text-text" : "text-muted"}`}>
      {icon}
    </Link>
  );
}

function WideButton({ icon, label, onClick }: { icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} title={label}
      className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text">
      <span className="text-faint">{icon}</span>
      <span className="min-w-0 flex-1 truncate text-left">{label}</span>
    </button>
  );
}

function WideLink({ icon, label, href, active }: { icon: ReactNode; label: string; href: string; active: boolean }) {
  return (
    <Link href={href} title={label}
      className={`flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-[13px] transition-colors hover:bg-surface-2 hover:text-text ${active ? "bg-surface-3 font-medium text-text" : "text-muted"}`}>
      <span className={active ? "text-text" : "text-faint"}>{icon}</span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
    </Link>
  );
}

function FlyoutPanel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="absolute top-0 left-full z-30 ml-2 w-60 rounded-xl border border-line bg-surface p-2 shadow-[var(--shadow-lg)]">
      <div className="flex items-center justify-between px-2.5 pb-1.5">
        <span className="text-[13px] font-medium">{title}</span>
        <button type="button" onClick={onClose} aria-label="关闭" className="grid size-5 place-items-center rounded text-faint hover:bg-surface-2 hover:text-text">
          <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden>
            <path d="M6 6l12 12M18 6 6 18" />
          </svg>
        </button>
      </div>
      <div className="max-h-[60vh] overflow-y-auto">{children}</div>
    </div>
  );
}

function FlyoutRow({ href, title, label, badge, dot }: { href: string; title?: string; label: string; badge?: string; dot?: "danger" | "brand" | "done" | "muted" }) {
  return (
    <Link href={href} title={title} className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text">
      {dot && <span className={`size-1.5 shrink-0 rounded-full ${dot === "danger" ? "bg-danger" : dot === "brand" ? "bg-brand animate-pulse" : dot === "done" ? "bg-accent" : "bg-line-strong"}`} />}
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {badge && <span className="shrink-0 font-mono text-[11px] text-faint">{badge}</span>}
    </Link>
  );
}

function FlyoutLine({ children, tone = "faint" }: { children: ReactNode; tone?: "faint" | "danger" }) {
  return <p className={`px-2.5 py-1.5 text-[12px] ${tone === "danger" ? "text-danger" : "text-faint"}`}>{children}</p>;
}

const ICON = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, className: "size-4 shrink-0", "aria-hidden": true };

function CollapseIcon() { return <svg {...ICON}><path d="M14 6 8 12l6 6" /></svg>; }
function TeamIcon() { return <svg {...ICON}><circle cx="9" cy="8" r="3" /><path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S13.9 16 14.5 19M15.5 5.5a3 3 0 0 1 0 6M17 14.8c2.1.3 3.3 1.7 3.7 4.2" /></svg>; }
function ExpandIcon() { return <svg {...ICON}><path d="m10 6 6 6-6 6" /></svg>; }
function PlusIcon() { return <svg {...ICON} strokeWidth={2}><path d="M12 5v14M5 12h14" /></svg>; }
function SearchIcon() { return <svg {...ICON}><path d="M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16.5 16.5 21 21" /></svg>; }
function FolderIcon() { return <svg {...ICON}><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l1.5 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z" /></svg>; }
function ClockIcon() { return <svg {...ICON}><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></svg>; }
function GearIcon() { return <svg {...ICON}><circle cx="12" cy="12" r="3" /><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" /></svg>; }
