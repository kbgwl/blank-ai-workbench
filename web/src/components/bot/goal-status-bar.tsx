"use client";

/**
 * v165 §9.1 顶部状态条。
 *
 * 纯展示：数据全部由 bot-app 传进来，这里不自己拉接口、不持有轮询。
 * 口径与文案规则在 ./goal-status.ts（可单测），这里只负责渲染。
 */

import { statusBarChips, type GoalStatusBarData, type StatusChip } from "./goal-status";

const TONE_CLASS: Record<StatusChip["tone"], string> = {
  plain: "bg-surface-2 text-muted",
  brand: "bg-brand/[0.08] text-brand",
  warn: "bg-warn/[0.12] text-warn",
  danger: "bg-danger/[0.08] text-danger",
  muted: "bg-surface-2 text-faint",
};

export function GoalStatusBar({ data }: { data: GoalStatusBarData }) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-4 py-1.5 text-[10.5px]">
      {statusBarChips(data).map((chip) => (
        <span
          key={chip.key}
          title={chip.key === "goal" ? data.goal : `${chip.label}：${chip.value}`}
          className={`inline-flex max-w-[min(42vw,320px)] items-center gap-1 rounded-full px-2 py-0.5 ${TONE_CLASS[chip.tone]}`}
        >
          <span className="shrink-0 opacity-70">{chip.label}</span>
          <span className="truncate font-medium">{chip.value}</span>
        </span>
      ))}
      <span
        title={data.serviceOnline ? "后端服务在线" : "后端服务未连接"}
        className="ml-auto inline-flex shrink-0 items-center gap-1 text-faint"
      >
        <span className={`size-1.5 rounded-full ${data.serviceOnline ? "bg-ok" : "bg-danger"}`} />
        {data.serviceOnline ? "服务在线" : "服务未连接"}
      </span>
    </div>
  );
}
