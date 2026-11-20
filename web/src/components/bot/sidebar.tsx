"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Thread } from "../tasks/api";
import { ThemeToggle } from "../theme-toggle";
import { isNavActive, NAV_ITEMS, type NavId } from "./nav";
import { StatusDot } from "./status-dot";
import { dayBucket, splitTitleStamp } from "./turns";

const ICON = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "size-3.5 shrink-0 text-faint",
};

/** 每个入口一个图标：常驻只剩工作区，其余入口收进设置页。 */
function NavIcon({ id }: { id: NavId }) {
  switch (id) {
    case "workspace":
      return (
        <svg {...ICON} aria-hidden>
          <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l1.5 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z" />
        </svg>
      );
  }
}

const GROUP_LABEL: Record<ReturnType<typeof dayBucket>, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "最近 7 天",
  earlier: "更早",
};

const GROUP_ORDER: ReturnType<typeof dayBucket>[] = ["today", "yesterday", "week", "earlier"];

function clockOf(value: string): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return "";
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 同一分组里重名的会话，补一份「id → 时间」的补位表。
 *
 * 自动起的轮次（定时、夜班、批量压测）标题天然相同：一列四个「L11 性能与正确性」，
 * 用户既分不出哪个是刚跑的、也点不进想看的那个。标题里自带时间时用标题的，
 * 没有就用更新时间顶上——只给重名的那几条补，不重名的不加噪音。
 */
function disambiguate(items: Thread[]): Map<string, string> {
  const baseOf = (thread: Thread) => splitTitleStamp(thread.title).base;
  const counts = new Map<string, number>();
  for (const thread of items) {
    const base = baseOf(thread);
    counts.set(base, (counts.get(base) ?? 0) + 1);
  }
  const out = new Map<string, string>();
  for (const thread of items) {
    if ((counts.get(baseOf(thread)) ?? 0) < 2) continue;
    const clock = clockOf(thread.updatedAt);
    if (clock) out.set(thread.id, clock);
  }
  return out;
}

export type SidebarProps = {
  threads: Thread[] | null;
  /** 项目分组：Codex 桌面版左栏就是「New chat / Projects / Recents」三段。 */
  projects: { id: string; name: string; pinned?: boolean }[];
  activeProjectId: string;
  onSelectProject: (id: string) => void;
  onToggleProjectPin: (id: string, pinned: boolean) => void;
  activeId: string | null;
  error: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onArchive: (thread: Thread) => void;
  /** 分叉：带着上下文另开一条线（Codex 的 thread/fork）。 */
  onFork: (thread: Thread) => void;
  onRename: (thread: Thread) => void;
  onUnarchive: (thread: Thread) => void;
  onDelete: (thread: Thread) => void;
  collapsed: boolean;
  onToggle: () => void;
};

export function Sidebar(props: SidebarProps) {
  const [query, setQuery] = useState("");
  const pathname = usePathname();

  const groups = useMemo(() => {
    const rows = (props.threads ?? []).filter((thread) =>
      query.trim() ? thread.title.toLowerCase().includes(query.trim().toLowerCase()) : true,
    );
    const buckets = new Map<ReturnType<typeof dayBucket>, Thread[]>();
    for (const thread of rows) {
      const bucket = dayBucket(thread.updatedAt || thread.createdAt);
      buckets.set(bucket, [...(buckets.get(bucket) ?? []), thread]);
    }
    return GROUP_ORDER.filter((bucket) => (buckets.get(bucket) ?? []).length > 0).map((bucket) => ({
      bucket,
      label: GROUP_LABEL[bucket],
      items: buckets.get(bucket) ?? [],
    }));
  }, [props.threads, query]);

  if (props.collapsed) {
    return (
      <aside className="flex w-[52px] shrink-0 flex-col items-center gap-2 border-r border-line bg-sidebar py-3">
        <button
          type="button"
          onClick={props.onToggle}
          title="展开会话列表"
          className="grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
            <path d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <button
          type="button"
          onClick={props.onNew}
          title="新对话"
          className="grid size-8 place-items-center rounded-lg bg-text text-bg"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="size-4" aria-hidden>
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
        <span className="my-1 h-px w-6 bg-line" />
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.id}
            href={item.href}
            title={`${item.label} · ${item.hint}`}
            className={`grid size-8 place-items-center rounded-lg transition-colors hover:bg-surface-2 hover:text-text ${
              isNavActive(pathname, item.href) ? "bg-surface-3 text-text" : "text-muted"
            }`}
          >
            <NavIcon id={item.id} />
          </Link>
        ))}
        <Link
          href="/team"
          title="Bot 团队"
          className="mt-auto grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4" aria-hidden>
            <circle cx="9" cy="8" r="3" />
            <path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S13.9 16 14.5 19M15.5 5.5a3 3 0 0 1 0 6M17 14.8c2.1.3 3.3 1.7 3.7 4.2" />
          </svg>
        </Link>
        <Link
          href="/settings"
          title="设置"
          className="grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4" aria-hidden>
            <circle cx="12" cy="12" r="3" />
            <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
          </svg>
        </Link>
        <ThemeToggle compact />
      </aside>
    );
  }

  return (
    <aside className="flex w-[240px] shrink-0 flex-col border-r border-line bg-sidebar">
      <div className="flex items-center gap-2 px-3 pt-3 pb-2.5">
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-logo-bg">
          <span className="size-2 rounded-[2px] bg-logo-accent" />
        </span>
        <span className="text-[13px] font-semibold tracking-tight">空白格</span>
        <span className="rounded bg-surface-3 px-1 py-0.5 font-mono text-[9px] tracking-wide text-muted uppercase">
          bot
        </span>
        <button
          type="button"
          onClick={props.onToggle}
          title="收起会话列表"
          className="ml-auto grid size-7 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
            <path d="M15 6l-6 6 6 6" />
          </svg>
        </button>
      </div>

      <div className="px-3 pb-2">
        <button
          type="button"
          onClick={props.onNew}
          className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-[13px] font-medium text-text transition-colors hover:bg-surface-2"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} className="size-4" aria-hidden>
            <path d="M12 5v14M5 12h14" />
          </svg>
          新对话
        </button>
      </div>

      <div className="px-3 pb-2">
        <div className="flex items-center gap-1.5 rounded-md border border-transparent bg-surface-2 px-2 py-1.5 transition-colors focus-within:border-line-strong focus-within:bg-surface">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-4 shrink-0 text-faint" aria-hidden>
            <path d="M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16.5 16.5 21 21" />
          </svg>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索对话"
            className="w-full bg-transparent text-[13px] text-text placeholder:text-faint focus:outline-none"
          />
        </div>
      </div>

      {/* 项目分组：与 Codex 桌面版同构——New chat / Projects / Recents。
          点项目 = 切到这个项目并开一条新对话（对应它的 "What should we build in X?"）；
          会话列表在下面「最近」里跨项目显示，所以切项目不会让历史"消失"。 */}
      <div className="px-2 pb-1">
        <div className="flex items-center justify-between px-2 pb-1">
          <span className="text-[11px] font-medium tracking-wide text-faint">项目</span>
          <Link
            href="/projects"
            title="项目与目录管理"
            className="text-[11px] text-faint transition-colors hover:text-text"
          >
            管理
          </Link>
        </div>
        <div className="space-y-0.5">
          {props.projects.length === 0 && (
            <p className="px-2 py-1 text-[12px] text-faint">还没有项目</p>
          )}
          {props.projects.map((project) => {
            const active = project.id === props.activeProjectId;
            return (
              <button
                key={project.id}
                type="button"
                onClick={() => {
                  if (!active) props.onSelectProject(project.id);
                }}
                title={active ? "当前项目" : `切到「${project.name}」并开新对话`}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors ${
                  active ? "bg-surface-3 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                }`}
              >
                <NavIcon id="workspace" />
                <span className="min-w-0 flex-1 truncate">{project.name}</span>
                {active && <span className="shrink-0 text-[11px] text-faint">当前</span>}
                <span
                  role="button"
                  tabIndex={0}
                  title={project.pinned ? "取消置顶" : "置顶这个项目"}
                  onClick={(event) => {
                    event.stopPropagation();
                    props.onToggleProjectPin(project.id, !project.pinned);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      event.stopPropagation();
                      props.onToggleProjectPin(project.id, !project.pinned);
                    }
                  }}
                  className={`shrink-0 rounded px-1 text-[11px] transition-colors ${
                    project.pinned ? "text-brand" : "text-faint hover:text-text"
                  }`}
                >
                  {project.pinned ? "★" : "☆"}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {props.error && (
          <p className="mx-1 mt-2 rounded-md border border-danger/25 bg-danger/[0.04] px-2 py-1.5 text-[13px] text-danger">
            {props.error}
          </p>
        )}

        <div className="px-2 pb-1 pt-2 text-[11px] font-medium tracking-wide text-faint">最近</div>

        {props.threads === null && !props.error && (
          <div className="space-y-1.5 px-1 pt-2">
            {[0, 1, 2].map((row) => (
              <div key={row} className="h-7 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        )}

        {props.threads !== null && props.threads.length === 0 && (
          <p className="px-2 py-6 text-center text-[13px] leading-relaxed text-faint">
            还没有对话。
            <br />
            说一句就开始。
          </p>
        )}

        {groups.map((group) => {
          const stamps = disambiguate(group.items);
          return (
          <div key={group.bucket} className="mb-2">
            <p className="px-2 py-1 text-[11px] font-medium text-faint">{group.label}</p>
            <div className="space-y-0.5">
              {group.items.map((thread) => {
                const active = thread.id === props.activeId;
                // 标题里自带的时间拆到右列：整列 18 行「雷罡 21:51:06」读起来是一堵墙。
                const { base, stamp: titleStamp } = splitTitleStamp(thread.title);
                const stamp = titleStamp || stamps.get(thread.id) || "";
                return (
                  <div key={thread.id} className="group/item relative">
                    <button
                      type="button"
                      onClick={() => props.onSelect(thread.id)}
                      className={`flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 pr-8 text-left transition-colors ${
                        active ? "bg-surface-3 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                      }`}
                    >
                      {/* 留一个固定宽度的槽：点不画的时候标题也不能左右跳。 */}
                      <span className="grid w-1.5 shrink-0 place-items-center">
                        <StatusDot status={thread.lastTurnStatus} quietDone />
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[13px]" title={thread.title}>
                        {base}
                      </span>
                      {/* 时间靠右成一列；轮次数不在这里重复（打开会话后标题栏有「N 轮」）。 */}
                      {stamp && (
                        <span className="shrink-0 font-mono text-[10px] tabular-nums text-faint">{stamp}</span>
                      )}
                    </button>
                    {/* 操作收进一个「⋯」：三个图标并排会吃掉窄侧栏一截标题，而且它们
                        只在悬停时 display:flex，键盘根本 Tab 不到。菜单本身挂到 body 上，
                        侧栏列表是 overflow-y-auto，挂在行里会被裁掉半截。 */}
                    <ThreadMenu
                      thread={thread}
                      onRename={props.onRename}
                      onArchive={props.onArchive}
                      onDelete={props.onDelete}
                      onFork={props.onFork}
                    />
                  </div>
                );
              })}
            </div>
          </div>
          );
        })}
      </div>

      <div className="border-t border-line p-2">
        {/* 监控/证据库/技能/连接器已收进设置页，侧栏底部只留团队与设置。 */}
        <Link
          href="/team"
          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4 text-faint" aria-hidden>
            <circle cx="9" cy="8" r="3" />
            <path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S13.9 16 14.5 19M15.5 5.5a3 3 0 0 1 0 6M17 14.8c2.1.3 3.3 1.7 3.7 4.2" />
          </svg>
          Bot 团队
        </Link>
        <Link
          href="/settings"
          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4 text-faint" aria-hidden>
            <circle cx="12" cy="12" r="3" />
            <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
          </svg>
          设置
        </Link>
        <ThemeToggle />
        <p className="px-2 pt-1 font-mono text-[9px] text-faint">空白格 bot</p>
      </div>
    </aside>
  );
}

/** 菜单里的一项。图标 + 文字，danger 只用在删除上。 */
function ThreadMenuItem(props: {
  onPick: () => void;
  danger?: boolean;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={props.onPick}
      className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors focus:outline-none ${
        props.danger
          ? "text-danger hover:bg-danger/10 focus:bg-danger/10"
          : "text-muted hover:bg-surface-2 hover:text-text focus:bg-surface-2 focus:text-text"
      }`}
    >
      <span className="shrink-0">{props.icon}</span>
      {props.children}
    </button>
  );
}

/**
 * 一条对话的「⋯」菜单。
 *
 * 以前是三个图标并排在行尾：吃掉窄侧栏近 70px 的标题宽度，而且只在悬停时
 * `display:flex`——`display:none` 的元素不能聚焦，键盘用户永远够不着这两个操作。
 * 现在常驻一个可聚焦的「⋯」（平时淡显，悬停或聚焦时满色），操作收进菜单里。
 *
 * 菜单用 portal 挂到 body：侧栏列表是 `overflow-y-auto`，挂在行内的绝对定位菜单
 * 会被容器裁掉，越靠近底部的行越明显。
 */
function ThreadMenu(props: {
  thread: Thread;
  onRename: (thread: Thread) => void;
  onArchive: (thread: Thread) => void;
  onDelete: (thread: Thread) => void;
  onFork: (thread: Thread) => void;
}) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState({ left: 0, top: 0 });
  const trigger = useRef<HTMLButtonElement | null>(null);
  const menu = useRef<HTMLDivElement | null>(null);

  // 三项 + 内边距约 118px：只用来判断向下放得下还是得往上翻，不需要精确。
  const MENU_W = 132;
  const MENU_H = 118;

  function place() {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.min(Math.max(8, rect.right - MENU_W), window.innerWidth - MENU_W - 8);
    const below = rect.bottom + 6;
    const top = below + MENU_H > window.innerHeight - 8 ? Math.max(8, rect.top - MENU_H - 6) : below;
    setAt({ left, top });
  }

  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const items = [...(menu.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
      if (items.length === 0) return;
      event.preventDefault();
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      const step = event.key === "ArrowDown" ? 1 : -1;
      items[(index + step + items.length) % items.length].focus();
    };
    // 菜单是 fixed 定位，列表一滚就挂错位置；直接关掉比跟着跑更不容易出错。
    const onAway = () => setOpen(false);
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onAway);
    window.addEventListener("scroll", onAway, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onAway);
      window.removeEventListener("scroll", onAway, true);
    };
  }, [open]);

  // 打开后把焦点交给第一项：鼠标用户看不到焦点环（程序化聚焦不算 :focus-visible），
  // 键盘用户则可以直接用上下键选，不用再 Tab 一次。
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [open]);

  const pick = (action: (thread: Thread) => void) => {
    setOpen(false);
    action(props.thread);
  };

  return (
    <>
      <button
        ref={trigger}
        type="button"
        title="重命名、归档或删除这条对话"
        aria-label={`「${props.thread.title}」的更多操作`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          if (!open) place();
          setOpen(!open);
        }}
        className={`absolute top-1/2 right-1 grid size-6 -translate-y-1/2 place-items-center rounded text-muted transition-opacity hover:bg-surface-3 hover:text-text focus-visible:opacity-100 focus-visible:ring-1 focus-visible:ring-brand/40 focus-visible:outline-none group-hover/item:opacity-100 group-focus-within/item:opacity-100 ${
          // 常驻半透明：之前默认 opacity-0，用户不把鼠标移到行尾就永远看不见这个入口，
          // 反馈是「改了吗，在哪儿」。现在平时淡显（hover 才满色），一眼能找到。
          open ? "bg-surface-3 text-text opacity-100" : "opacity-55"
        }`}
      >
        <svg viewBox="0 0 24 24" fill="currentColor" className="size-4" aria-hidden>
          <circle cx="5" cy="12" r="1.6" />
          <circle cx="12" cy="12" r="1.6" />
          <circle cx="19" cy="12" r="1.6" />
        </svg>
      </button>

      {open &&
        createPortal(
          <div
            ref={menu}
            role="menu"
            aria-label={`「${props.thread.title}」的操作`}
            style={{ left: at.left, top: at.top, width: MENU_W }}
            className="fixed z-50 rounded-lg border border-line bg-surface p-1 shadow-[0_10px_28px_-12px_rgba(16,24,40,0.35)]"
          >
            <ThreadMenuItem
              onPick={() => pick(props.onFork)}
              icon={
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
                  <circle cx="6" cy="5" r="2.2" />
                  <circle cx="6" cy="19" r="2.2" />
                  <circle cx="18" cy="12" r="2.2" />
                  <path d="M6 7.2v9.6M8.2 5h4.3a3 3 0 0 1 3 3v1.8M8.2 19h4.3a3 3 0 0 0 3-3v-1.8" />
                </svg>
              }
            >
              分叉到新对话
            </ThreadMenuItem>
            <ThreadMenuItem
              onPick={() => pick(props.onRename)}
              icon={
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                  <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                </svg>
              }
            >
              重命名
            </ThreadMenuItem>
            <ThreadMenuItem
              onPick={() => pick(props.onArchive)}
              icon={
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
                  <path d="M3 8h18v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8Z" />
                  <path d="M2 4h20v4H2zM9.5 12h5" />
                </svg>
              }
            >
              归档
            </ThreadMenuItem>
            <ThreadMenuItem
              danger
              onPick={() => pick(props.onDelete)}
              icon={
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
                  <path d="M3 6h18M8 6V4h8v3M6 6l1 14h10l1-14M10 11v6M14 11v6" />
                </svg>
              }
            >
              删除
            </ThreadMenuItem>
          </div>,
          document.body,
        )}
    </>
  );
}
