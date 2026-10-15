"use client";

import type { ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { statusMeta } from "./status";

export function Textarea({ className = "", ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 font-sans text-[13px] leading-relaxed text-text placeholder:text-faint focus:border-brand/60 focus:outline-none ${className}`}
      {...props}
    />
  );
}

export function Select({ className = "", children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className="relative">
      <select
        className={`w-full appearance-none rounded-lg border border-line bg-surface-2 py-2 pr-8 pl-3 text-[13px] text-text focus:border-brand/60 focus:outline-none disabled:text-faint ${className}`}
        {...props}
      >
        {children}
      </select>
      <svg
        viewBox="0 0 24 24"
        className="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-faint"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
      >
        <path d="m6 9 6 6 6-6" />
      </svg>
    </div>
  );
}

export function StatusBadge({ status, className = "" }: { status: string; className?: string }) {
  const meta = statusMeta(status);
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] leading-4 font-medium whitespace-nowrap ${meta.chip} ${className}`}
    >
      <span className={`size-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

export function LiveBadge({ mode }: { mode: "off" | "snapshot" | "connecting" | "sse" | "polling" }) {
  const map = {
    off: { text: "未选择任务", tone: "text-faint" },
    snapshot: { text: "已恢复历史事件", tone: "text-faint" },
    connecting: { text: "连接事件流…", tone: "text-warn" },
    sse: { text: "SSE 实时推送", tone: "text-accent" },
    polling: { text: "轮询 1.5s", tone: "text-brand" },
  } as const;
  const item = map[mode];
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 font-mono text-[10px] ${item.tone}`}>
      {(mode === "sse" || mode === "polling" || mode === "connecting") && (
        <span className="size-1.5 animate-pulse rounded-full bg-current" />
      )}
      {item.text}
    </span>
  );
}

export function Meta({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="font-mono text-[10px] tracking-wide text-faint uppercase">{label}</span>
      <span className="min-w-0 truncate text-[12px] text-muted">{value}</span>
    </div>
  );
}

export function KeyVal({ k, v, tone }: { k: string; v: string; tone?: "muted" | "danger" | "accent" }) {
  const valueClass =
    tone === "danger" ? "text-danger" : tone === "accent" ? "text-accent" : "text-muted";
  return (
    <div className="flex min-w-0 items-baseline gap-2 text-[12px]">
      <span className="shrink-0 text-faint">{k}</span>
      <span className={`min-w-0 flex-1 truncate font-mono ${valueClass}`} title={v}>
        {v}
      </span>
    </div>
  );
}

const blockIcons: Record<string, ReactNode> = {
  plan: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <path d="M4 5h16M4 12h10M4 19h7" />
    </svg>
  ),
  tool: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <path d="M14.7 6.3a4 4 0 0 1-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 0 1 5.4-5.4l-2.5 2.5-1.4-1.4z" />
    </svg>
  ),
  terminal: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="m7 9 3 3-3 3M13 15h4" />
    </svg>
  ),
  approval: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <path d="M12 3 5 6v6c0 4 3 7 7 8 4-1 7-4 7-8V6z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  ),
  result: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <circle cx="12" cy="12" r="8" />
      <path d="m8.5 12 2.5 2.5 4.5-5" />
    </svg>
  ),
  change: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <path d="M6 4v10a2 2 0 0 0 2 2h8" />
      <path d="m4 6 2-2 2 2M14 14l2 2-2 2" />
    </svg>
  ),
  system: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5">
      <circle cx="12" cy="12" r="8" />
      <path d="M12 8v4l2.5 2.5" />
    </svg>
  ),
};

export function BlockIcon({ kind }: { kind: string }) {
  return blockIcons[kind] ?? blockIcons.system;
}
