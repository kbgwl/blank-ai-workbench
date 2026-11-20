/**
 * 一次审批决定的载荷，以及它到 API 入参的唯一一层映射。
 *
 * 为什么单独一个模块：全站有两处审批卡（对话页 `bot/pending-panel.tsx` 与任务时间线
 * `tasks/timeline-blocks.tsx`），它们手里的「这是哪张卡」长得不一样——时间线有完整
 * `Block`，对话页只有 `PendingApproval`。之前两处各自拼参数，一路都是可选的位置参数，
 * 少传一个 `tsc` 不会报：用户写的驳回理由就这样被静默丢了整整一版（2026-11-21 修）。
 *
 * 所以这里只认「两处都拿得到的那几个字段」，其余归类由调用方自己解。
 */

import type { ApprovalScope } from "./api";

/**
 * 审批卡的身份。时间线的 `Block` 结构上满足它，对话页的 `PendingApproval` 也满足；
 * 缺字段一律当"后端按 approval_id 对号"，不要在这里编一个空串顶替。
 */
export type ApprovalTarget = {
  /** 时间线里这张卡的 key（用来做"正在提交哪一张"的高亮）。对话页没有这个概念。 */
  key?: string;
  approvalId?: string;
  toolCallId?: string;
  argumentsHash?: string;
  toolName?: string;
};

/**
 * 一次审批决定的完整载荷。
 *
 * 为什么是一个对象而不是若干位置参数：这条链上有四层转发（卡片 → 面板 → 页面 →
 * API）。对象按引用透传，漏字段编译期就炸；位置参数少传一个是运行期静默降级。
 */
export type ApprovalDecision = {
  decision: "approve" | "deny";
  scope: ApprovalScope;
  /** 驳回时用户写的那句话，会被原样交给模型。 */
  feedback?: string;
  /** 打回到哪一步；空表示普通驳回（只否掉这次调用，计划不动）。 */
  stepId?: string;
  /**
   * 卡的身份。对话页的卡来自 `PendingApproval`、时间线来自 `Block`，两者共有的是
   * 这几个字段，所以类型收窄到这里——绑死 `Block` 会让对话页为了传载荷先伪造一个块。
   */
  target?: ApprovalTarget;
};

/** `decideApproval` 的入参。字段名与它一一对应，避免在这里二次改名。 */
export type DecideInput = {
  decision: "approve" | "deny";
  scope: ApprovalScope;
  feedback?: string;
  sendBackStepId?: string;
  approvalId?: string;
  toolCallId?: string;
  argumentsHash?: string;
  toolName?: string;
};

/**
 * 载荷 → 接口入参。
 *
 * 两条取舍写死在这里，而不是留给每个调用方自己想：
 * 1. **批准时不带理由、不带打回**：后端只读驳回这一档，带了也是白占协议；
 *    更重要的是，"批准 + 退回第 3 步"本身自相矛盾，能发出去就说明有人拼错了。
 * 2. **打回只在驳回时生效**：这是「整段退回重做」与「这次不行」的唯一区别，
 *    混进批准里会让已完成的三步被静默推回 pending。
 *
 * 空串按"没填"处理：输入框里敲几格空格等于没写，发一个空 feedback 过去，
 * 模型会收到一条空的"用户意见"。
 */
export function decisionInput(decision: ApprovalDecision): DecideInput {
  const target = decision.target ?? {};
  const deny = decision.decision === "deny";
  const note = String(decision.feedback ?? "").trim();
  const stepId = String(decision.stepId ?? "").trim();
  return {
    decision: decision.decision,
    scope: decision.scope,
    feedback: deny && note ? note : undefined,
    sendBackStepId: deny && stepId ? stepId : undefined,
    approvalId: target.approvalId || undefined,
    toolCallId: target.toolCallId || undefined,
    argumentsHash: target.argumentsHash || undefined,
    toolName: target.toolName || undefined,
  };
}
