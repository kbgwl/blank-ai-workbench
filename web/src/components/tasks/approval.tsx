"use client";

import { useState, type KeyboardEvent } from "react";
import type { ApprovalGrant, ApprovalScope } from "./api";
import type { SendBackChoice } from "./approval-copy";

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
  /**
   * 驳回。带上用户写的那句话（可选）——它会被原样交给模型。
   * 键盘路径（Esc / 数字键）永远不带：那时用户在按快捷键，不是在写理由。
   *
   * 第二个参数是「打回到第几步」：给了就把计划从该步起重开，不给就是普通驳回。
   */
  onDeny: (feedback?: string, sendBackStepId?: string) => void;
  /** 能打回到哪几步。空数组表示没有可退回的步骤（新任务、纯问答）。 */
  steps?: SendBackChoice[];
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
export function ApprovalOptions({ grants, busy, decided = false, onApprove, onDeny, steps = [] }: ApprovalOptionsProps) {
  // 驳回理由。放在组件内而不是上层：换一张卡就该清空，上一次的理由不该跟着跑到下一张。
  const [note, setNote] = useState("");
  // 理由输入是否展开。理由属于「拒绝」这个分支，不是列表里的一个选项：
  // 把它插在 grants 和「不允许」之间，序号就被切成 1 / 2 / [输入框] / 3，
  // 读到第三行的人对不上号。点「不允许」才展开——这也是 Claude Code 那样的做法
  // （"No, and tell me what to do differently"），先选拒绝，再说理由。
  const [explaining, setExplaining] = useState(false);
  // 打回到哪一步。空串 = 普通驳回（只否掉这次调用，计划不动）。
  // 默认空：用户点「不允许」时想表达的多半就是"这次不行"，退回重做是更重的动作，
  // 不该是默认值——误触一下就把已完成的三步推回 pending 了。
  const [sendBackTo, setSendBackTo] = useState("");
  if (decided) {
    return <p className="font-mono text-[12px] text-faint">该请求已处理，不会重复执行。</p>;
  }
  const total = grants.length + 1;
  const submitDeny = () => onDeny(note.trim() || undefined, sendBackTo || undefined);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (busy) return;
    // 展开理由时，键盘归那个输入框：这里再抢一次 Esc 会把"在打字"判成"驳回"。
    if (explaining) return;
    if (event.key === "Escape") {
      event.preventDefault();
      onDeny();
      return;
    }
    const picked = Number(event.key);
    if (!Number.isInteger(picked) || picked < 1 || picked > total) return;
    event.preventDefault();
    if (picked === total) setExplaining(true);
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
                  ? // 默认项只给一层浅底 + 左侧 ›，不加 ring：加了边框它看起来就是个
                    // 输入框，用户会去点它想打字。默认的含义靠符号和底色就够。
                    "bg-surface-2 text-text hover:bg-surface-3"
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
            aria-expanded={explaining}
            onClick={() => setExplaining((open) => !open)}
            className={`flex w-full items-baseline gap-2 rounded-md px-2 py-1 text-left text-[13px] transition-colors disabled:opacity-50 ${
              explaining
                ? // 展开时不能再套一圈边框：那一圈会让「3. 不允许」看起来像个输入框，
                  // 和下面真正的理由输入框并排，用户不知道该往哪个里打字。
                  "bg-warn/[0.06] text-text"
                : "text-muted hover:bg-surface-2 hover:text-text"
            }`}
          >
            <span className="w-2 shrink-0" />
            <span className="shrink-0 font-mono text-[11px] text-faint">{total}.</span>
            <span>不允许</span>
          </button>
        </li>
      </ol>
      {/* 理由输入挂在列表外面：它属于上一条「不允许」，不是下一个选项。
          默认收起，写理由这一步不占位置，也不打断编号。 */}
      {explaining && (
        // 缩进到选项文字那一列：理由属于上一条「不允许」，不是并列的第四行。
        <div className="mt-1 ml-6 rounded-md border border-line bg-surface p-2">
          <input
            type="text"
            autoFocus
            value={note}
            onChange={(event) => setNote(event.target.value)}
            onKeyDown={(event) => {
              event.stopPropagation();
              if (event.key === "Enter") {
                event.preventDefault();
                submitDeny();
              } else if (event.key === "Escape") {
                event.preventDefault();
                setExplaining(false);
              }
            }}
            placeholder="告诉它该怎么改（可不填）"
            maxLength={600}
            className="w-full rounded-md border border-line bg-surface-2 px-2 py-1 text-[12px] text-text placeholder:text-faint focus:border-brand/40 focus:outline-none"
          />
          {/* 打回到哪一步：只在有可退回的步骤时出现。选空就是普通驳回——
              退回重做会把该步之后的已完成步骤全部重开，代价比"这次不行"大得多，
              所以它必须是用户主动选的，不能是默认。 */}
          {steps.length > 0 && (
            <label className="mt-1.5 flex items-center gap-1.5 text-[12px] text-muted">
              <span className="shrink-0">退回重做：</span>
              <select
                value={sendBackTo}
                disabled={busy}
                onChange={(event) => setSendBackTo(event.target.value)}
                onKeyDown={(event) => event.stopPropagation()}
                className="min-w-0 flex-1 rounded-md border border-line bg-surface-2 px-1.5 py-1 text-[12px] text-text focus:border-brand/40 focus:outline-none"
              >
                <option value="">不退回，只否掉这一步</option>
                {steps.map((item) => (
                  <option key={item.id} value={item.id}>
                    从{item.label}起重做
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="mt-1.5 flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={submitDeny}
              className="rounded-md bg-surface-2 px-2 py-1 text-[12px] text-text transition-colors hover:bg-line disabled:opacity-50"
            >
              {sendBackTo ? "退回重做" : "确认不允许"}
            </button>
            <button
              type="button"
              onClick={() => setExplaining(false)}
              className="rounded-md px-1.5 py-1 text-[12px] text-faint transition-colors hover:text-text"
            >
              取消
            </button>
            <span className="font-mono text-[11px] text-faint">回车提交 · Esc 返回</span>
          </div>
        </div>
      )}
      {/* 展开时不再补一句提示：框里那行「回车提交 · Esc 返回」已经是同一件事。 */}
      {!explaining && (
        <p className="mt-1.5 pl-2 font-mono text-[11px] text-faint">
          点一下这里就能按 1–{total} 直接选 · Esc 直接不允许
        </p>
      )}
    </div>
  );
}
