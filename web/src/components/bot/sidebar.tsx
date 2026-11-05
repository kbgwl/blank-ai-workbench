"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo, useState } from "react";
import type { Thread } from "../tasks/api";
import { isNavActive, NAV_ITEMS, TURN_TONE_LABEL, turnTone, type NavId } from "./nav";
import { dayBucket } from "./turns";

const ICON = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "size-3.5 shrink-0 text-faint",
};

/** 每个入口一个图标：光有文字时，收起的侧栏就什么都认不出来了。 */
function NavIcon({ id }: { id: NavId }) {
  switch (id) {
    case "workspace":
      return (
        <svg {...ICON} aria-hidden>
          <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l1.5 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z" />
        </svg>
      );
    case "evidence":
      return (
        <svg {...ICON} aria-hidden>
          <path d="M12 3 5 6v5c0 4.2 2.8 7.4 7 9 4.2-1.6 7-4.8 7-9V6l-7-3Z" />
          <path d="m9 12 2 2 4-4" />
        </svg>
      );
    case "skills":
      return (
        <svg {...ICON} aria-hidden>
          <path d="M12 3v3M12 18v3M4.2 7.5l2.6 1.5M17.2 15l2.6 1.5M4.2 16.5l2.6-1.5M17.2 9l2.6-1.5" />
          <circle cx="12" cy="12" r="3.2" />
        </svg>
      );
    case "connectors":
      return (
        <svg {...ICON} aria-hidden>
          <path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0V8ZM12 16v5" />
        </svg>
      );
  }
}

/**
 * 会话行的状态点。
 *
 * 用 title/aria-label 把颜色翻成字：只给颜色的话，色觉差异的用户读不出"跑失败了"。
 */
function StatusDot({ status }: { status: string }) {
  const tone = turnTone(status);
  if (tone === "idle") return null;
  const toneClass =
    tone === "running"
      ? "bg-brand animate-pulse"
      : tone === "failed"
        ? "bg-danger"
        : "bg-accent";
  return (
    <span
      title={TURN_TONE_LABEL[tone]}
      aria-label={TURN_TONE_LABEL[tone]}
      className={`size-1.5 shrink-0 rounded-full ${toneClass}`}
    />
  );
}

const GROUP_LABEL: Record<ReturnType<typeof dayBucket>, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "最近 7 天",
  earlier: "更早",
};

const GROUP_ORDER: ReturnType<typeof dayBucket>[] = ["today", "yesterday", "week", "earlier"];

export type SidebarProps = {
  threads: Thread[] | null;
  activeId: string | null;
  error: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onArchive: (thread: Thread) => void;
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
      </aside>
    );
  }

  return (
    <aside className="flex w-[260px] shrink-0 flex-col border-r border-line bg-sidebar">
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
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
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5" aria-hidden>
            <path d="M15 6l-6 6 6 6" />
          </svg>
        </button>
      </div>

      <div className="px-3 pb-2">
        <button
          type="button"
          onClick={props.onNew}
          className="flex w-full items-center gap-2 rounded-lg border border-line-strong bg-surface px-2.5 py-2 text-[12.5px] font-medium text-text transition-colors hover:border-brand/40 hover:bg-surface-2"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} className="size-3.5" aria-hidden>
            <path d="M12 5v14M5 12h14" />
          </svg>
          新对话
        </button>
      </div>

      <div className="px-3 pb-2">
        <div className="flex items-center gap-1.5 rounded-lg bg-surface-2 px-2 py-1.5">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5 shrink-0 text-faint" aria-hidden>
            <path d="M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16.5 16.5 21 21" />
          </svg>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索对话"
            className="w-full bg-transparent text-[12px] text-text placeholder:text-faint focus:outline-none"
          />
        </div>
      </div>

      {/* 工作区紧挨着搜索：它决定"这次在哪个目录里干"，是开口之前的事。 */}
      <div className="px-2 pb-2">
        <Link
          href="/projects"
          title="项目与目录"
          className={`flex items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] transition-colors ${
            isNavActive(pathname, "/projects")
              ? "bg-surface-3 text-text"
              : "text-muted hover:bg-surface-2 hover:text-text"
          }`}
        >
          <NavIcon id="workspace" />
          工作区
        </Link>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {props.error && (
          <p className="mx-1 mt-2 rounded-md border border-danger/25 bg-danger/[0.04] px-2 py-1.5 text-[11px] text-danger">
            {props.error}
          </p>
        )}

        {props.threads === null && !props.error && (
          <div className="space-y-1.5 px-1 pt-2">
            {[0, 1, 2].map((row) => (
              <div key={row} className="h-7 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        )}

        {props.threads !== null && props.threads.length === 0 && (
          <p className="px-2 py-6 text-center text-[11.5px] leading-relaxed text-faint">
            还没有对话。
            <br />
            说一句就开始。
          </p>
        )}

        {groups.map((group) => (
          <div key={group.bucket} className="mb-2">
            <p className="px-2 py-1 font-mono text-[10px] tracking-wide text-faint uppercase">{group.label}</p>
            <div className="space-y-0.5">
              {group.items.map((thread) => {
                const active = thread.id === props.activeId;
                return (
                  <div key={thread.id} className="group/item relative">
                    <button
                      type="button"
                      onClick={() => props.onSelect(thread.id)}
                      className={`flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 pr-7 text-left transition-colors ${
                        active ? "bg-surface-3 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                      }`}
                    >
                      <StatusDot status={thread.lastTurnStatus} />
                      <span className="min-w-0 flex-1 truncate text-[12.5px]">{thread.title}</span>
                      {thread.turnCount > 1 && (
                        <span className="shrink-0 font-mono text-[9.5px] text-faint">{thread.turnCount}</span>
                      )}
                    </button>
                    {/* 悬停操作条：以前是三个图标直接浮在标题上，标题从图标缝里透出来像乱码。
                        现在垫一层与行底色一致的遮罩：右 68% 实色盖住图标区，左边一段渐变淡出，
                        被盖住的半个字顺着渐变消失，读起来是"标题在这里截断"而不是"字被压住"。
                        渐变用行内样式写死色值和停点——Tailwind 的 from-55% 停点落在图标中间，
                        会让最左边那个图标压在文字上。 */}
                    <div
                      className="absolute inset-y-0 right-0 hidden items-center gap-0.5 rounded-r-lg pr-1 pl-9 group-hover/item:flex"
                      style={{
                        backgroundImage: `linear-gradient(to left, var(${
                          active ? "--surface-3" : "--surface-2"
                        }) 68%, transparent 100%)`,
                      }}
                    >
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          props.onRename(thread);
                        }}
                        title="重命名"
                        aria-label={`重命名「${thread.title}」`}
                        className="rounded p-1 text-faint transition-colors hover:bg-surface-3 hover:text-text"
                      >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3" aria-hidden>
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                        </svg>
                      </button>
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          props.onArchive(thread);
                        }}
                        title="归档这条对话"
                        aria-label={`归档「${thread.title}」`}
                        className="rounded p-1 text-faint transition-colors hover:bg-surface-3 hover:text-text"
                      >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3" aria-hidden>
                          <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
                        </svg>
                      </button>
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          props.onDelete(thread);
                        }}
                        title="删除对话（不可恢复）"
                        aria-label={`删除「${thread.title}」`}
                        className="rounded p-1 text-faint transition-colors hover:bg-surface-3 hover:text-danger"
                      >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3" aria-hidden>
                          <path d="M3 6h18M8 6V4h8v3M6 6l1 14h10l1-14M10 11v6M14 11v6" />
                        </svg>
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-line p-2">
        {NAV_ITEMS.filter((item) => item.id !== "workspace").map((item) => (
          <Link
            key={item.id}
            href={item.href}
            title={item.hint}
            className={`flex items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] transition-colors ${
              isNavActive(pathname, item.href)
                ? "bg-surface-3 text-text"
                : "text-muted hover:bg-surface-2 hover:text-text"
            }`}
          >
            <NavIcon id={item.id} />
            {item.label}
          </Link>
        ))}
        <div className="my-1 border-t border-line" />
        <Link
          href="/team"
          className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-3.5 text-faint" aria-hidden>
            <circle cx="9" cy="8" r="3" />
            <path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S13.9 16 14.5 19M15.5 5.5a3 3 0 0 1 0 6M17 14.8c2.1.3 3.3 1.7 3.7 4.2" />
          </svg>
          Bot 团队
        </Link>
        <Link
          href="/settings"
          className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-3.5 text-faint" aria-hidden>
            <circle cx="12" cy="12" r="3" />
            <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
          </svg>
          设置
        </Link>
        <p className="px-2 pt-1 font-mono text-[9.5px] text-faint">空白格 bot</p>
      </div>
    </aside>
  );
}
