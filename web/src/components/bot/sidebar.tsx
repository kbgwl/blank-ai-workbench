"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { Thread } from "../tasks/api";
import { dayBucket } from "./turns";

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
  collapsed: boolean;
  onToggle: () => void;
};

export function Sidebar(props: SidebarProps) {
  const [query, setQuery] = useState("");

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
        <Link
          href="/settings"
          title="设置"
          className="mt-auto grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-text"
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
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-[#1b2a4a]">
          <span className="size-2 rounded-[2px] bg-[#5eead4]" />
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
                      <span className="min-w-0 flex-1 truncate text-[12.5px]">{thread.title}</span>
                      {thread.turnCount > 1 && (
                        <span className="shrink-0 font-mono text-[9.5px] text-faint">{thread.turnCount}</span>
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        props.onArchive(thread);
                      }}
                      title="归档这条对话"
                      className="absolute top-1/2 right-1 hidden -translate-y-1/2 rounded p-1 text-faint transition-colors hover:bg-surface-3 hover:text-danger group-hover/item:block"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3" aria-hidden>
                        <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
                      </svg>
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-line p-2">
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
