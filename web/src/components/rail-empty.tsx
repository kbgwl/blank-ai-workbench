import type { ReactNode } from "react";

/**
 * 右栏空态：交代「这一栏会出现什么」，必要时给一个可点的下一步。
 *
 * 之前这里要么是一句 12px 灰字、要么是几块空卡片，用户分不清是「没有数据」
 * 还是「没加载完」。统一成「图标 + 一句标题 + 一句解释 + 可选动作」：
 * 不编造数据、不拿假图表填高度，但空得有交代。
 *
 * 两条约束来自实测：
 * - 解释用 --muted 而不是 --faint：右栏底色是 sidebar/canvas，faint 在浅色下
 *   只有约 3:1，12px 的正文读起来发虚；
 * - 标题与解释之间只差一档字重和一级灰度，不靠字号拉层次——右栏窄，字号一跳
 *   就像标题党。
 */
export function RailEmpty({
  icon,
  title,
  body,
  children,
  compact = false,
  fill = false,
}: {
  icon: ReactNode;
  title: string;
  body: string;
  /** 下一步动作。没有可做的事就别传，别摆一个点了没反应的按钮。 */
  children?: ReactNode;
  /** 窄栏 / 已经有分组标题时用紧凑档：不抢上方信息的注意力。 */
  compact?: boolean;
  /** 撑满剩余高度并垂直居中：整栏都空的时候，贴在顶部会像只加载了一半。 */
  fill?: boolean;
}) {
  return (
    <div
      className={`flex flex-col items-center text-center ${compact ? "px-1 py-5" : "px-2 py-8"} ${
        fill ? "flex-1 justify-center" : ""
      }`}
    >
      <span
        className={`grid shrink-0 place-items-center rounded-lg border border-line bg-surface text-muted ${
          compact ? "size-8" : "size-10"
        }`}
      >
        {icon}
      </span>
      <p className="mt-2.5 text-[13px] font-medium text-text">{title}</p>
      <p className="mt-1 max-w-[248px] text-[12px] leading-relaxed text-muted">{body}</p>
      {children && <div className="mt-3 flex flex-wrap items-center justify-center gap-2">{children}</div>}
    </div>
  );
}
