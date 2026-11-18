"use client";

/**
 * 对话顶部状态条。
 *
 * 纯展示：数据全部由 bot-app 传进来，这里不自己拉接口、不持有轮询。
 * 口径与文案规则在 ./goal-status.ts（可单测），这里只负责渲染。
 *
 * 新对话、没有目标、服务正常 → 整条不渲染。多一行常驻状态栏，
 * 换来的只是「这行是什么」的疑问。
 */

import { statusBarChips, statusBarVisible, type GoalStatusBarData, type StatusChip } from "./goal-status";

const TONE_CLASS: Record<StatusChip["tone"], string> = {
  plain: "bg-surface-2 text-muted",
  brand: "bg-brand/[0.08] text-brand",
  warn: "bg-warn/[0.12] text-warn",
  danger: "bg-danger/[0.08] text-danger",
  muted: "bg-surface-2 text-faint",
};

export function GoalStatusBar({ data }: { data: GoalStatusBarData }) {
  if (!statusBarVisible(data)) return null;
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-4 py-1.5 text-[11px]">
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
      {!data.serviceOnline && (
        <span
          title="后端服务未连接：发出去的消息不会被执行"
          className="ml-auto inline-flex shrink-0 items-center gap-1 text-danger"
        >
          <span className="size-1.5 rounded-full bg-danger" />
          服务未连接
        </span>
      )}
    </div>
  );
}
