"use client";

import type { PendingApproval } from "../tasks/api";
import type { ApprovalDecision } from "../tasks/approval-payload";
import type { SendBackChoice } from "../tasks/approval-copy";
import { ApprovalCard } from "./turn";

/**
 * 待你处理的卡，挂在输入框上方。
 *
 * 为什么不在聊天流里：卡片长在正文里时，长对话要往上翻才看得见；用户看到的
 * 是输入框突然不能用了，而"为什么不能用"在视口之外。输入框上方是眼睛和手
 * 都在的地方，卡放这里，「要你动手」和「动手的地方」就在同一处。
 *
 * 三层兜底（抄的是同类产品的主路径做法）：
 *   1. 有完整请求 → 渲染完整卡（几个授权档、驳回可写理由）
 *   2. 说是等审批但没有卡（刷新过、事件丢了）→ 降级卡，只给"停止"
 *   3. 都没有 → 不产出任何节点
 * 关键是**永远不让"等你批准"这四个字配一块空白**：最坏情况也要有东西可点。
 */
export function PendingPanel({
  approvals,
  waiting,
  busy,
  steps = [],
  onDecide,
  onRetry,
  onStop,
}: {
  approvals: PendingApproval[];
  /** 任务状态是 waiting_approval。用来判断"该有卡却一张都没有"。 */
  waiting: boolean;
  busy: boolean;
  /**
   * 能打回到哪几步。由页面从当前任务的 planSteps 算好传进来——面板自己不该去猜
   * 计划长什么样，它只负责摆卡。
   */
  steps?: SendBackChoice[];
  /**
   * 载荷还是对象，卡的身份由这里补上：卡片手里只有"我这次决定了什么"，
   * 它不知道自己是哪张卡（同一屏可能有两张）。补在这里而不是让卡片记住 approval，
   * 是为了让卡片能继续只依赖纯展示字段。
   */
  onDecide: (approval: PendingApproval, request: ApprovalDecision) => void;
  onRetry: () => void;
  onStop: () => void;
}) {
  if (approvals.length > 0) {
    // 同屏最多两张：三张以上会顶掉输入框，用户反而找不到地方说话。
    // 剩下的只报数——点开的那张处理完，下一张立刻补上来。
    const shown = approvals.slice(0, 2);
    const rest = approvals.length - shown.length;
    return (
      <div className="bg-canvas px-4 pt-1">
       <div className="mx-auto w-full max-w-3xl">
        {shown.map((approval) => (
          <ApprovalCard
            key={approval.approvalId || approval.toolCallId || approval.tool}
            approval={approval}
            busy={busy}
            steps={steps}
            onRetry={onRetry}
            onDecide={(request) => onDecide(approval, request)}
          />
        ))}
        {rest > 0 && (
          <p className="mt-1 font-mono text-[11px] text-faint">还有 {rest} 个请求在排队等着处理</p>
        )}
       </div>
      </div>
    );
  }

  if (!waiting) return null;

  // 状态说在等审批，却拿不到请求详情。不说"等你批准"就完了——那等于把用户
  // 关在门外还不告诉他门在哪。
  return (
    <div className="bg-canvas px-4 pt-1">
      <div className="mx-auto w-full max-w-3xl">
        <div className="my-2 rounded-xl border border-warn/35 bg-warn/[0.04] p-3">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <p className="min-w-0 flex-1 text-[13px] font-medium text-text">有一个操作在等你确认</p>
          <span className="shrink-0 font-mono text-[11px] text-faint">详情缺失</span>
        </div>
        <p className="mt-1 text-[12px] leading-relaxed text-muted">
          这次请求的细节没拿到（可能刷新过，或者事件丢了）。点停止再重新运行这一轮就能重新发起。
        </p>
        <div className="mt-2.5 flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onStop}
            className="rounded-md border border-line bg-surface px-2.5 py-1 text-[12px] text-text transition-colors hover:border-line-strong disabled:opacity-50"
          >
            停止当前任务
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onRetry}
            className="rounded-md px-1.5 py-1 text-[12px] text-muted transition-colors hover:text-text disabled:opacity-50"
          >
            重新运行这一轮
          </button>
        </div>
       </div>
      </div>
    </div>
  );
}
