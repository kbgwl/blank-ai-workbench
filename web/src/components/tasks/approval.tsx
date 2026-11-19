"use client";

import type { KeyboardEvent } from "react";
import type { ApprovalGrant, ApprovalScope } from "./api";

/**
 * 审批的两件事：顶上问什么、下面怎么选。
 *
 * 全站有两处审批卡（对话页 `bot/turn.tsx`、任务时间线 `tasks/timeline-blocks.tsx`），
 * 同一件事在两处长得不一样，用户会以为它们的效力也不同。文案与选项只有这一份实现。
 */
export { approvalQuestion } from "./approval-copy";

type ApprovalOptionsProps = {
  grants: ApprovalGrant[];
  busy: boolean;
  /** 已经批过 / 拒过的卡：不再给选项，只留一句状态。 */
  decided?: boolean;
  onApprove: (scope: ApprovalScope) => void;
  onDeny: () => void;
};

/**
 * 选项做成有序列表而不是一排按钮：一排按钮看不出「哪个是默认、哪个范围最大」，
 * 而审批的难点恰恰是分清「就这一次」和「这个会话一直允许」。
 *
 * 「不允许」也不再是红色按钮——红色在别处代表危险动作，而拒绝才是安全的那一个。
 *
 * 键盘只在列表自己拿到焦点时生效：全局监听会让「在输入框里打 1」变成「批准执行」。
 * 点了这里（或 Tab 到它）就是要动手了，这时 1–N 才有意义。
 */
export function ApprovalOptions({ grants, busy, decided = false, onApprove, onDeny }: ApprovalOptionsProps) {
  if (decided) {
    return <p className="font-mono text-[12px] text-faint">该请求已处理，不会重复执行。</p>;
  }
  const total = grants.length + 1;
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (busy) return;
    if (event.key === "Escape") {
      event.preventDefault();
      onDeny();
      return;
    }
    const picked = Number(event.key);
    if (!Number.isInteger(picked) || picked < 1 || picked > total) return;
    event.preventDefault();
    if (picked === total) onDeny();
    else onApprove(grants[picked - 1].scope);
  };
  return (
    <div
      tabIndex={0}
      onKeyDown={onKeyDown}
      role="group"
      aria-label="审批选项"
      className="rounded-md outline-none focus-visible:ring-2 focus-visible:ring-warn/30"
    >
      <ol className="flex flex-col gap-0.5">
        {grants.map((option, index) => (
          <li key={option.scope}>
            <button
              type="button"
              disabled={busy}
              onClick={() => onApprove(option.scope)}
              className={`flex w-full items-baseline gap-2 rounded-md px-2 py-1 text-left text-[13px] transition-colors disabled:opacity-50 ${
                index === 0
                  ? "bg-surface text-text ring-1 ring-line-strong hover:ring-brand/40"
                  : "text-muted hover:bg-surface-2 hover:text-text"
              }`}
            >
              {/* 两列定宽：默认项标记和编号各自一列，选项文字才不会因为
                  有没有 › 而左右错位。 */}
              <span className={`w-2 shrink-0 text-center font-mono text-[11px] ${index === 0 ? "text-brand" : ""}`}>
                {index === 0 ? "›" : ""}
              </span>
              <span className={`shrink-0 font-mono text-[11px] ${index === 0 ? "text-brand" : "text-faint"}`}>
                {index + 1}.
              </span>
              <span className="min-w-0">{option.label}</span>
            </button>
          </li>
        ))}
        <li>
          <button
            type="button"
            disabled={busy}
            onClick={onDeny}
            className="flex w-full items-baseline gap-2 rounded-md px-2 py-1 text-left text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-50"
          >
            <span className="w-2 shrink-0" />
            <span className="shrink-0 font-mono text-[11px] text-faint">{total}.</span>
            <span>不允许</span>
          </button>
        </li>
      </ol>
      <p className="mt-1.5 pl-2 font-mono text-[11px] text-faint">点一下这里就能按 1–{total} 直接选 · Esc 不允许</p>
    </div>
  );
}
