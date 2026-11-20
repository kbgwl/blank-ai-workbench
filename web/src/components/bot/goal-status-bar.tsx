"use client";

/**
 * 对话顶部状态条。
 *
 * 纯展示：数据全部由 bot-app 传进来，这里不自己拉接口、不持有轮询。
 * 口径与文案规则在 ./goal-status.ts（可单测），这里只负责渲染。
 *
 * 新对话、没有目标、服务正常 → 整条不渲染。多一行常驻状态栏，
 * 换来的只是「这行是什么」的疑问。
 *
 * 一行放完「这一轮在做什么」和「别的任务卡在哪」：原来后台任务是条独立的
 * 橙色横幅，挂在状态条下面，两条都在说审批、都是整行——一屏里两条告示互相
 * 抢注意力，看着就乱。后台任务现在是这一行右边的一格，点它就过去。
 */

import { statusBarChips, statusBarVisible, type GoalStatusBarData, type StatusChip } from "./goal-status";

const TONE_CLASS: Record<StatusChip["tone"], string> = {
  plain: "bg-surface-2 text-muted",
  brand: "bg-brand/[0.08] text-brand",
  warn: "bg-warn/[0.12] text-warn",
  danger: "bg-danger/[0.08] text-danger",
  muted: "bg-surface-2 text-faint",
};

const DOT_CLASS: Record<NonNullable<StatusChip["dot"]>, string> = {
  warn: "bg-warn",
  danger: "bg-danger",
  brand: "bg-brand",
};

type Props = {
  data: GoalStatusBarData;
  /** 后台任务有会话时：点了回那条会话。 */
  onBackground?: () => void;
  /** 后台任务没有会话时：直接给任务详情的地址。 */
  backgroundHref?: string;
  backgroundTitle?: string;
  /**
   * 不止一条后台任务。此时这一格不再是"跳过去"，而是"展开清单"——
   * 跳只能跳到其中一条，另外几条既看不见也点不到，用户会以为它们不存在。
   */
  backgroundExpandable?: boolean;
  backgroundOpen?: boolean;
};

export function GoalStatusBar({
  data,
  onBackground,
  backgroundHref,
  backgroundTitle,
  backgroundExpandable = false,
  backgroundOpen = false,
}: Props) {
  if (!statusBarVisible(data)) return null;
  return (
    <div className="flex shrink-0 items-center gap-1.5 border-b border-line px-3 py-1.5 text-[11px]">
      {statusBarChips(data).map((chip) => {
        const className = `inline-flex min-w-0 items-center gap-1 rounded-full px-2 py-0.5 ${TONE_CLASS[chip.tone]}`;
        const body = (
          <>
            {chip.dot && <span className={`size-1.5 shrink-0 rounded-full ${DOT_CLASS[chip.dot]}`} />}
            <span className="shrink-0 opacity-70">{chip.label}</span>
            <span className="truncate font-medium">{chip.value}</span>
          </>
        );

        if (chip.key === "goal") {
          return (
            // 上限 58%：目标往往是一整句，不设限它会把整行占满、把后台那一格
            // 顶到最右边，中间再留一截空白——像是两件不相关的事被硬塞进一行。
            <span key={chip.key} title={data.goal} className={`${className} max-w-[58%] flex-1`}>
              {body}
            </span>
          );
        }

        // 后台这一格是入口，不是装饰：状态色 + 可点。宽度压在上限里，
        // 不让一条长会话名把目标挤成半句。
        const backgroundClass = `${className} max-w-[42%] shrink-0`;
        if (chip.key === "background" && backgroundExpandable && onBackground) {
          return (
            <button
              key={chip.key}
              type="button"
              onClick={onBackground}
              aria-expanded={backgroundOpen}
              title={backgroundOpen ? "收起后台任务清单" : "展开后台任务清单（都在这里）"}
              className={`${backgroundClass} transition-opacity hover:opacity-80`}
            >
              {body}
              {/* 有 ▽ 才知道这一格是能展开的；没有它就只是个数字，点了会跳走。 */}
              <span className="shrink-0 opacity-60">{backgroundOpen ? "▴" : "▾"}</span>
            </button>
          );
        }
        if (backgroundHref) {
          return (
            <a
              key={chip.key}
              href={backgroundHref}
              title={backgroundTitle ? `${backgroundTitle}（没有关联会话，打开任务详情）` : "打开任务详情"}
              className={`${backgroundClass} transition-opacity hover:opacity-80`}
            >
              {body}
            </a>
          );
        }
        if (onBackground) {
          return (
            <button
              key={chip.key}
              type="button"
              onClick={onBackground}
              title={backgroundTitle ?? "打开那条会话"}
              className={`${backgroundClass} transition-opacity hover:opacity-80`}
            >
              {body}
            </button>
          );
        }
        return (
          <span key={chip.key} className={backgroundClass}>
            {body}
          </span>
        );
      })}
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
