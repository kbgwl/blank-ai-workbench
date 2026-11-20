"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/projects/ui";
import { formatDuration } from "../tasks/format";
import { guardianReviewLabel } from "../tasks/status";
import { approvalGrantOptions } from "../tasks/api";
import { ApprovalOptions, approvalQuestion } from "../tasks/approval";
import { approvalApproveLabel, isPlanGate } from "../tasks/approval-copy";
import { explainFailure, failureParts } from "../tasks/error-copy";
import type { FailureCopy } from "../tasks/error-copy";
import type { AnswerValues, AttachmentRef, PendingApproval, PendingQuestion } from "../tasks/api";
import { MarkdownView } from "./markdown-view";
import { StepView } from "./steps";
import type { BotStep, PlanView, StepFilter } from "./turns";
import { filterSteps, turnEvidence, collapseRepeatedNotes, thinkVerb } from "./turns";
import { uploadUrl } from "./uploads";
import type { SkillSuggestion } from "@/lib/skill-suggest";


/** 附件是不是图片。老数据没有 mime 就没有 kind，按图片处理（那时只支持图片）。 */
function isImage(item: AttachmentRef): boolean {
  if (item.kind) return item.kind === "image";
  return !item.mime || item.mime.startsWith("image/");
}

function fileLabel(item: AttachmentRef): string {
  if (item.kind === "pdf" || item.mime === "application/pdf") return "PDF";
  const dot = item.name.lastIndexOf(".");
  return dot > 0 ? item.name.slice(dot + 1).toLowerCase() : "文件";
}

function timeLabel(value: string): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return "";
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseMoment(value: string): number | null {
  if (!value) return null;
  const ms = Date.parse(value.includes("T") ? value : value.replace(" ", "T"));
  return Number.isNaN(ms) ? null : ms;
}

/**
 * 「在想怎么做…」跑 30 秒和跑 3 分钟原本长得一模一样，用户分不清它是在想、
 * 还是已经卡死——这时候人只会做一件事：按停止。给它两个可验证的量（换了哪个
 * 词、已经花了多久），等不等就变成用户自己的判断，而不是靠猜。
 */
function ThinkingLine({ createdAt }: { createdAt: string }) {
  const startedAt = parseMoment(createdAt);
  // 首帧不取 Date.now()：服务端与浏览器的时间不同，取当前时间会造成 hydration 不一致。
  const [now, setNow] = useState(() => startedAt ?? 0);
  useEffect(() => {
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  // 词由开始时间定死：同一轮无论重渲染多少次都是同一个词，不会每帧跳。
  const verb = thinkVerb(startedAt);
  const seconds = startedAt === null ? 0 : Math.max(0, Math.floor((now - startedAt) / 1000));
  return (
    <p className="flex items-center gap-2 py-1 text-[13px]">
      <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-brand" />
      <span className="text-text">{verb}…</span>
      {seconds > 0 && (
        <span className="font-mono text-[11px] tabular-nums text-faint">
          {seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`}
        </span>
      )}
    </p>
  );
}

function StatusChip({ status, running }: { status: string; running: boolean }) {
  // 「等你批准」必须排在「进行中」前面。
  //
  // isTurnRunning 把 waiting_approval 也算"还在跑"（这没错——这一轮确实没结束，
  // 输入框该显示停止按钮）。但徽标先判 running 的话，等审批的任务永远显示
  // "进行中"：关掉窗口再打开的人看到的是一个在动的任务，不知道卡在等他点一下。
  if (status === "approval_expired") {
    // 过期与「等你批准」是两件事：前者点什么都会 409，后者才需要动手。
    // 徽标先说"过期"，别让人对着一个点不动的卡片等下去。
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-medium text-muted">
        审批已过期
      </span>
    );
  }
  if (status === "waiting_approval") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-warn/[0.12] px-2 py-0.5 text-[11px] font-medium text-warn">
        <span className="size-1.5 rounded-full bg-warn" />
        等你批准
      </span>
    );
  }
  if (status === "paused") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-medium text-muted">
        已暂停
      </span>
    );
  }
  if (running) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-brand/[0.08] px-2 py-0.5 text-[11px] font-medium text-brand">
        <span className="relative flex size-1.5">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-brand/60" />
          <span className="relative inline-flex size-1.5 rounded-full bg-brand" />
        </span>
        进行中
      </span>
    );
  }
  if (status === "failed" || status === "error") {
    return <span className="rounded-full bg-danger/[0.08] px-2 py-0.5 text-[11px] font-medium text-danger">失败</span>;
  }
  if (status === "cancelled" || status === "canceled") {
    return <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-medium text-muted">已停止</span>;
  }
  return null;
}

function ArtifactStrip({
  files,
  onFileClick,
}: {
  files: { path: string; changeType: string }[];
  onFileClick?: (path: string) => void;
}) {
  const added = files.filter((file) => /add|new|create|新增/i.test(file.changeType)).length;
  // 最多列 6 个：再多就把这一行撑成第二段正文，反而没人看完。
  const shown = files.slice(0, 6);
  const rest = files.length - shown.length;
  return (
    <div className="mt-2 flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
      <span className="font-mono text-[11px] text-faint">产物</span>
      <span className="font-mono text-[11px] text-muted">
        {files.length} 个文件{added > 0 ? ` · ${added} 新增` : ""}
      </span>
      {shown.map((file) => (
        <button
          key={file.path}
          type="button"
          onClick={() => onFileClick?.(file.path)}
          title={`${file.changeType} · ${file.path}`}
          className="max-w-[14rem] truncate rounded border border-line bg-surface px-1.5 py-0.5 font-mono text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text"
        >
          {file.path.split(/[\\/]/).pop()}
        </button>
      ))}
      {rest > 0 && <span className="font-mono text-[11px] text-faint">+{rest}</span>}
    </div>
  );
}

export function ApprovalCard({
  approval,
  onDecide,
  onRetry,
  busy,
}: {
  approval: PendingApproval;
  onDecide: (decision: "approve" | "deny", scope: "once" | "prefix" | "tool", feedback?: string) => void;
  onRetry: () => void;
  busy: boolean;
}) {
  const command = String(approval.args.command ?? approval.args.path ?? "");
  // 计划门的按钮要改说法：「允许这一次」放在"按这份计划开始改"上说不通。
  const grants = approvalGrantOptions(approval).map((option) => ({
    ...option,
    label: approvalApproveLabel(approval.tool, option.scope, option.label),
  }));
  const planGate = isPlanGate(approval.tool);
  const reviewNote = guardianReviewLabel(approval.guardian);
  // 审查结论压成一个短徽标放在标题行。原来是一整行灰字铺在卡片里，读起来像脚注，
  // 眼睛会直接跳过——而"这条有人先看过"恰恰是用户要的信息。全文进 title。
  const reviewBadge =
    approval.guardian === undefined
      ? ""
      : approval.guardian === null
        ? "未复核"
        : approval.guardian.verdict === "deny"
          ? "审查：否决"
          : approval.guardian.verdict === "allow"
            ? "审查：放行"
            : "审查：未表态";
  const reviewBlocked = Boolean(approval.guardian && approval.guardian.verdict === "deny");
  return (
    <div
      className={`my-2 rounded-xl border p-3 ${
        approval.expired ? "border-line bg-surface-2/40" : "border-warn/35 bg-warn/[0.04]"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <p className="min-w-0 flex-1 text-[13px] font-medium text-text">{approvalQuestion(approval.tool, approval.args)}</p>
        {reviewBadge && (
          <span
            className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] ${
              reviewBlocked ? "bg-danger/10 text-danger" : "bg-surface-3 text-muted"
            }`}
            title={reviewNote || "独立审查员先看过了；它放行不等于安全，它说话也不会替你批准"}
          >
            {reviewBadge}
          </span>
        )}
        <span className="shrink-0 font-mono text-[11px] text-faint">
          {approval.tool}
          {approval.risk && <span className="ml-1.5 text-warn">风险 {approval.risk}</span>}
        </span>
      </div>
      {command && (
        <pre className="mt-1.5 overflow-auto rounded-md bg-surface px-2.5 py-1.5 font-mono text-[12px] text-text">
          {command}
        </pre>
      )}
      {/* 计划门：要批的是计划本身，不是"执行 exit_plan_mode"这个动作。
          不摆出来的后果是用户只能盲批，或者凭这一轮对话里读到的片段猜。 */}
      {planGate &&
        (approval.planPreview ? (
          <div className="mt-1.5 max-h-64 overflow-auto rounded-md border border-line bg-surface px-2.5 py-2">
            <MarkdownView text={approval.planPreview} />
          </div>
        ) : (
          // 空计划也能走到这张卡上（审批发生在工具执行之前）。批了它只会在
          // 下一步原地失败一次，所以先说清楚，而不是让用户批完再看到红字。
          <p className="mt-1.5 rounded-md border border-warn/35 bg-warn/[0.06] px-2.5 py-1.5 text-[12px] leading-relaxed text-warn">
            它还没把计划写出来。这一步批了也交不出东西，会在原地再失败一次——先让它把计划写清楚。
          </p>
        ))}
      {/* 过期卡不写「高风险操作等待人工审批」：它说的是"有人在等你"，而这里
          恰恰是没人来、卡已经作废。留着这句会和下面"已经过期"直接打架。
          同理，"等待人工审批"这类和问句重复的模板话也不再铺一遍——标题已经在问
          "要让它跑这条命令吗"，再补一句只会把卡片撑高。 */}
      {approval.message && !approval.expired && !approval.message.includes("等待人工审批") && (
        <p className="mt-1 text-[12px] text-muted">{approval.message}</p>
      )}
      {/* 过期不是"再点一次就好"：后端一定回 409，任务也不会再往下走。
          这时候给按钮等于给死按钮——直接说清楚，并给唯一还有用的动作（重跑）。 */}
      {approval.expired ? (
        <div className="mt-2.5 rounded-md border border-line bg-surface px-2.5 py-2">
          <p className="text-[12px] leading-relaxed text-muted">
            这张卡已经过期，批准也提交不上去了。任务停在等你确认这一步，不会自己往下走。
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-1.5 rounded-md bg-surface-2 px-2 py-1 text-[12px] text-text transition-colors hover:bg-line"
          >
            重跑这一轮
          </button>
        </div>
      ) : (
        <div className="mt-2.5">
          <ApprovalOptions
            grants={grants}
            busy={busy}
            onApprove={(scope) => onDecide("approve", scope)}
            onDeny={(feedback) => onDecide("deny", "once", feedback)}
          />
        </div>
      )}
      {/* 档位警告只在还能批的时候有意义：过期卡讲"本会话档会免问多远"，
          是一件已经不会发生的事。 */}
      {!approval.expired &&
        grants
          .filter((option) => option.warning)
          .map((option) => (
            <p key={option.scope} className="mt-1 text-[12px] leading-relaxed text-muted">
              {option.scope === "tool" ? "工具档：" : "本会话档："}
              {option.warning}
            </p>
          ))}
    </div>
  );
}

function QuestionCard({
  questions,
  onAnswer,
  busy,
}: {
  questions: PendingQuestion[];
  onAnswer: (answers: AnswerValues) => void;
  busy: boolean;
}) {
  const [draft, setDraft] = useState<AnswerValues>({});
  // 每题都有推荐项才允许「按默认继续」：事实题没有默认值，放空等于让服务器替用户编。
  const canDefault =
    questions.length > 0 &&
    questions.every((question) => question.options.some((option) => option.recommended));
  const hasAnswer = (question: PendingQuestion): boolean => {
    const value = draft[question.id];
    return Array.isArray(value) ? value.length > 0 : Boolean((value ?? "").trim());
  };
  const isSelected = (id: string, label: string): boolean => {
    const value = draft[id];
    return Array.isArray(value) ? value.includes(label) : value === label;
  };
  const preview = questions
    .flatMap((question) =>
      question.options.filter((option) => isSelected(question.id, option.label) && option.preview),
    )
    .map((option) => option.preview)[0];
  return (
    <div className="my-2 rounded-xl border border-brand/30 bg-brand/[0.03] p-3">
      <p className="text-[13px] font-medium text-brand">它想先问你一句</p>
      <div className="mt-2 space-y-3">
        {questions.map((question) => (
          <div key={question.id}>
            <p className="text-[13px] text-text">{question.question}</p>
            {question.options.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {question.options.map((option) => (
                  <button
                    key={option.label}
                    type="button"
                    title={option.description}
                    onClick={() =>
                      setDraft((current) => {
                        if (!question.multiSelect) {
                          return { ...current, [question.id]: option.label };
                        }
                        const picked = Array.isArray(current[question.id])
                          ? (current[question.id] as string[])
                          : [];
                        const next = picked.includes(option.label)
                          ? picked.filter((value) => value !== option.label)
                          : [...picked, option.label];
                        return { ...current, [question.id]: next };
                      })
                    }
                    className={`rounded-md border px-2 py-1 text-[12px] transition-colors ${
                      isSelected(question.id, option.label)
                        ? "border-brand/60 bg-brand/[0.08] text-brand"
                        : "border-line bg-surface text-muted hover:border-line-strong hover:text-text"
                    }`}
                  >
                    {option.label}
                    {option.recommended && (
                      <span className="ml-1 text-[11px] text-brand/70">默认</span>
                    )}
                  </button>
                ))}
              </div>
            )}
            {question.allowFreeText && (
              <input
                value={typeof draft[question.id] === "string" ? (draft[question.id] as string) : ""}
                onChange={(event) => setDraft((current) => ({ ...current, [question.id]: event.target.value }))}
                placeholder="或者直接写你的答案"
                className="mt-1.5 w-full rounded-md border border-line bg-surface px-2.5 py-1.5 text-[13px] text-text placeholder:text-faint focus:border-brand/60 focus:outline-none"
              />
            )}
          </div>
        ))}
      </div>
      {preview && (
        // 模型给的 HTML 片段一律按纯文本展示：不在 innerHTML 里跑它的标签。
        <pre className="mt-2 max-h-40 overflow-auto rounded-md border border-line bg-surface px-2.5 py-2 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-text/85">
          {preview}
        </pre>
      )}
      <div className="mt-2.5 flex items-center gap-2">
        <Button
          variant="primary"
          disabled={busy || questions.every((question) => !hasAnswer(question))}
          onClick={() => onAnswer(draft)}
        >
          回答并继续
        </Button>
        {canDefault && (
          <Button variant="ghost" disabled={busy} onClick={() => onAnswer({})}>
            按默认继续
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * 失败提示。
 *
 * 原来这里只渲染一行红字，内容是 `reason` 原样——「模型请求超时：gpt-4o」
 * 或者干脆是 `all_models_failed`。它说了出了什么事，没说用户该干什么，
 * 于是所有失败最后都收束成同一个动作：再点一次重试。而密钥错了、额度空了、
 * 这个模型不支持工具调用，重试一百次也一样。
 *
 * 三层，顺序就是人遇到故障时的提问顺序：发生了什么 → 我怎么办 → 技术详情。
 * 技术详情默认收起：它是排查依据，不是第一眼该读的东西。
 */
function FailureNotice({
  copy,
  raw,
  code,
  at,
  onRetry,
}: {
  copy: FailureCopy;
  raw: string;
  code: string;
  at: string;
  onRetry: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const { meta, original: detail } = failureParts(raw, code, at);
  const hasDetail = Boolean(detail) || meta.length > 0;
  return (
    <div className="mt-1 rounded-lg border border-danger/25 bg-danger/[0.04] px-2.5 py-2">
      <p className="text-[13px] leading-relaxed text-danger">{copy.title}</p>
      {copy.action && <p className="mt-1 text-[12px] leading-relaxed text-muted">{copy.action}</p>}
      {(copy.next !== "none" || hasDetail) && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          {copy.next === "retry" && (
            <button
              type="button"
              onClick={onRetry}
              className="rounded-md border border-line px-2 py-0.5 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
            >
              用同样的话再试一次
            </button>
          )}
          {copy.next === "settings" && (
            <Link
              href="/settings"
              className="rounded-md border border-line px-2 py-0.5 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
            >
              去改设置
            </Link>
          )}
          {hasDetail && (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              className="font-mono text-[11px] text-faint transition-colors hover:text-text"
            >
              技术详情 {open ? "▴" : "▾"}
            </button>
          )}
        </div>
      )}
      {open && hasDetail && (
        <div className="mt-1.5 rounded-md border border-line bg-surface px-2 py-1.5">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              {meta.length > 0 && (
                <p className="font-mono text-[11px] text-faint">{meta.join(" · ")}</p>
              )}
              {detail && (
                <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted">
                  {detail}
                </pre>
              )}
            </div>
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard?.writeText([meta.join(" "), detail].filter(Boolean).join("\n")).then(() => {
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1200);
                });
              }}
              className="shrink-0 rounded px-1.5 py-0.5 font-mono text-[11px] text-faint transition-colors hover:bg-surface-3 hover:text-text"
            >
              {copied ? "已复制" : "复制"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * 消息级操作：复制这一整段，以及把选中一段带进输入框当引用。
 *
 * 复制做成悬停才出：一轮对话里真正被复制的不到十分之一，常驻一个按钮就是在
 * 每段回复下面钉一行灰字。但选中就要立刻能引用——那是用户已经在做动作了，
 * 让他再多走一步（先滚到某处找按钮）就白选了，所以引用按钮浮在选中的上方。
 */
function ReplyActions({
  text,
  copied,
  onCopy,
}: {
  text: string;
  copied: boolean;
  onCopy: () => void;
}) {
  if (!text.trim()) return null;
  return (
    <div className="mt-1.5 flex items-center gap-2 opacity-0 transition-opacity group-hover/turn:opacity-100 focus-within:opacity-100">
      <button
        type="button"
        onClick={onCopy}
        className="font-mono text-[11px] text-faint transition-colors hover:text-text"
      >
        {copied ? "已复制" : "复制"}
      </button>
    </div>
  );
}

export type TurnViewProps = {
  goal: string;
  /** 这一轮贴的附件。回放时要看得见，否则用户不知道自己当时发了什么。 */
  images: AttachmentRef[];
  /** 拼图片读取地址用。会话属于当前项目，所以直接用当前项目 id。 */
  projectId: string;
  steps: BotStep[];
  /** 这一轮的计划与进度。放在正文上方，不塞进折叠的过程明细。 */
  plan: PlanView | null;
  reply: string;
  reason: string;
  /** 翻译前的原因原话 + 后端错误码。错误分层要用它们认成因，见 tasks/error-copy.ts。 */
  rawReason?: string;
  failureCode?: string;
  status: string;
  running: boolean;
  /** 模型正在输出的文字。非空时优先显示它，位置就是最终回复的位置。 */
  streaming: string;
  createdAt: string;
  files: { path: string; changeType: string }[];
  /** 过程没加载时说明一句，而不是让人以为"它什么都没做"。 */
  stepCount: number;
  approvals: PendingApproval[];
  questions: PendingQuestion[];
  actionBusy: boolean;
  onDecide: (
    approval: PendingApproval,
    decision: "approve" | "deny",
    scope: "once" | "prefix" | "tool",
    /** 驳回时用户写的那句话。以前这里只有三个参数，"写理由"在对话页是白写的。 */
    feedback?: string,
  ) => void;
  onAnswer: (questions: PendingQuestion[], answers: AnswerValues) => void;
  onRetry: () => void;
  /** 把这一段选中的话带进输入框当引用。 */
  onQuote?: (text: string) => void;
  /** 这一轮的活像不像某条技能的覆盖面（纯前端判断，见 lib/skill-suggest）。 */
  skillHint?: SkillSuggestion | null;
  onUseSkill?: (display: string) => void;
};

export function TurnView(props: TurnViewProps) {
  const { steps, running } = props;
  // 跑的时候展开过程（用户想看着它干活），跑完收起（只留结论）。
  const [openSteps, setOpenSteps] = useState<boolean | null>(null);
  const expanded = openSteps ?? running;
  const [filter, setFilter] = useState<StepFilter>("all");
  const evidence = turnEvidence(steps);
  const paths = props.files.map((file) => file.path);
  // 相同的话连说两遍：合并成一条再算筛选与计数，否则「全部 3」里有一半是复读。
  const collapsed = collapseRepeatedNotes(steps);
  const shown = filterSteps(collapsed, filter);
  const hasBody = props.streaming !== "" || props.reply !== "" || props.reason !== "";

  // 正文里点文件名：就地定位到那一行，而不是另开一个页面去翻。
  // 收起状态下点也生效——先展开，再切到"改动"，因为用户点的就是改动。
  const jumpToFile = () => {
    setOpenSteps(true);
    setFilter("changes");
  };

  // 选中一段话就浮出一个「引用」。位置贴着选中区域的左上角：眼睛刚离开选中的
  // 那几个字，按钮就在原处，不用横跨整屏去找。
  const replyRef = useRef<HTMLDivElement>(null);
  const [pick, setPick] = useState<{ text: string; top: number; left: number } | null>(null);
  const [copied, setCopied] = useState(false);

  const captureSelection = () => {
    const host = replyRef.current;
    const selection = window.getSelection();
    if (!host || !selection || selection.isCollapsed || selection.rangeCount === 0) {
      setPick(null);
      return;
    }
    const range = selection.getRangeAt(0);
    if (!host.contains(range.commonAncestorContainer)) {
      setPick(null);
      return;
    }
    const text = selection.toString().trim();
    // 三五个字以内不弹：手滑拖一下也满足"有选中"，弹出来只是挡视线。
    if ([...text].length < 6) {
      setPick(null);
      return;
    }
    const box = host.getBoundingClientRect();
    const rect = range.getBoundingClientRect();
    setPick({
      text,
      top: Math.max(0, rect.top - box.top),
      left: Math.max(0, Math.min(rect.left - box.left, box.width - 52)),
    });
  };

  const copyReply = () => {
    const text = (props.reply || props.streaming).trim();
    if (!text) return;
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };

  return (
    <div className="group/turn">
      {/* 用户说的话：右侧淡色气泡，与助手的整段正文区分开 */}
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-xl rounded-br-md bg-surface-2 px-3.5 py-2 text-[14px] leading-relaxed text-text">
          {props.images.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1.5">
              {props.images.map((item) =>
                isImage(item) ? (
                  <a
                    key={item.id}
                    href={uploadUrl(props.projectId, item.id)}
                    target="_blank"
                    rel="noreferrer"
                    title={item.name}
                    className="block size-20 overflow-hidden rounded-lg border border-line transition-opacity hover:opacity-85"
                  >
                    {/* 静态导出没有图片优化器，回放用的就是后端那份（上传前已降采样）。 */}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={uploadUrl(props.projectId, item.id)} alt={item.name} className="size-full object-cover" />
                  </a>
                ) : (
                  // 文本 / PDF 不渲染缩略图：一个打不开的黑框没有意义，给个能点开的文件名。
                  <a
                    key={item.id}
                    href={uploadUrl(props.projectId, item.id)}
                    target="_blank"
                    rel="noreferrer"
                    title={item.name}
                    className="flex max-w-[220px] items-center gap-1.5 rounded-lg border border-line px-2 py-1.5 text-[12px] text-muted transition-colors hover:bg-surface-3 hover:text-text"
                  >
                    <span className="shrink-0 font-mono text-[11px] uppercase text-faint">
                      {fileLabel(item)}
                    </span>
                    <span className="truncate">{item.name}</span>
                  </a>
                ),
              )}
            </div>
          )}
          <div className="whitespace-pre-wrap">{props.goal}</div>
        </div>
      </div>

      <div className="mt-3.5">
        <div className="mb-1 flex items-center gap-2">
          <span className="grid size-5 shrink-0 place-items-center rounded-md bg-logo-bg">
            <span className="size-1.5 rounded-[2px] bg-logo-accent" />
          </span>
          <span className="text-[13px] font-semibold tracking-tight">空白格</span>
          <StatusChip status={props.status} running={running} />
          {props.createdAt && (
            <span className="ml-auto font-mono text-[11px] text-faint">{timeLabel(props.createdAt)}</span>
          )}
        </div>

        {/* 计划：用户最想先知道"它打算做什么、做到哪了"，所以放在正文之上，
            且不折叠。之前它在折叠的过程明细里，等于没显示。 */}
        {props.plan && <PlanCard plan={props.plan} waiting={props.status === "waiting_approval"} />}

        {/* 依据条：一行数字回答"它到底做了什么"，正文因此不必再复述过程。
            每个数字都能点开落到具体的行，这就是"回复短但不丢溯源"的地方。 */}
        {props.stepCount > 0 && (
          <div className="ml-7">
            <button
              type="button"
              onClick={() => setOpenSteps(!expanded)}
              title={evidence.commands.length ? `跑过的命令：\n${evidence.commands.join("\n")}` : undefined}
              className="mb-1 flex w-full items-center gap-1.5 rounded-md px-1 py-0.5 text-[12px] text-faint transition-colors hover:bg-surface-2 hover:text-muted"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                className={`size-3 shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`}
                aria-hidden
              >
                <path d="m9 6 6 6-6 6" />
              </svg>
              {running && <span className="shrink-0 text-brand">正在做</span>}
              {evidence.looked > 0 && <span className="shrink-0">看了 {evidence.looked}</span>}
              {evidence.files > 0 && <span className="shrink-0">· 改了 {evidence.files} 个文件</span>}
              {evidence.commandCount > 0 && <span className="shrink-0">· 跑了 {evidence.commandCount} 条命令</span>}
              {evidence.calls === 0 && <span className="shrink-0">{props.stepCount} 步</span>}
              {evidence.failed > 0 && <span className="shrink-0 font-medium text-danger">· {evidence.failed} 次失败</span>}
              {/* 耗时单独成列并对齐：它原来跟在数字串尾巴上，位数一变
                  （9 秒 → 1 分 20 秒）整行就跟着左右跳，多轮排在一起扫不出规律。 */}
              {evidence.durationMs > 0 && (
                <span className="ml-auto shrink-0 pl-2 text-right font-mono text-[11px] tabular-nums">
                  {formatDuration(evidence.durationMs)}
                </span>
              )}
            </button>

            {expanded && (
              <div className="pb-1">
                {/* 筛选只在真有内容时出现：一个点了什么都没有的按钮，比没有按钮更让人怀疑。 */}
                {(props.files.length > 0 || evidence.failed > 0) && (
                  <div className="mb-1 flex items-center gap-1 pl-0.5">
                    {(
                      [
                        ["all", `全部 ${collapsed.length}`],
                        ["changes", `改动 ${props.files.length}`],
                        ["failures", `失败 ${evidence.failed}`],
                      ] as [StepFilter, string][]
                    )
                      .filter(([key]) => key === "all" || (key === "changes" ? props.files.length > 0 : evidence.failed > 0))
                      .map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          onClick={() => setFilter(key)}
                          className={`rounded px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
                            filter === key ? "bg-surface-3 text-text" : "text-faint hover:bg-surface-2 hover:text-muted"
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                  </div>
                )}
                <div className="space-y-0.5">
                  {shown.length === 0 && <p className="px-1.5 py-1 text-[12px] text-faint">这一步没有可显示的内容。</p>}
                  {shown.map((step) => (
                    <StepView key={step.key} step={step} />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <div className="ml-7">
          <div ref={replyRef} className="relative" onMouseUp={captureSelection}>
            {props.streaming && (
              <div className="relative">
                <MarkdownView text={props.streaming} files={paths} onFileClick={jumpToFile} />
                <span className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-[2px] animate-pulse bg-brand align-middle" />
              </div>
            )}

            {!props.streaming && props.reply && (
              <MarkdownView text={props.reply} files={paths} onFileClick={jumpToFile} />
            )}

            {pick && props.onQuote && (
              <button
                type="button"
                // 用 mousedown 兜住"按钮自己也会触发一次选择变化"：在 mouseup 之前
                // 就把选中文本带走了，之后浏览器把选区收起来也影响不到这次引用。
                onMouseDown={(event) => {
                  event.preventDefault();
                  props.onQuote?.(pick.text);
                  setPick(null);
                  window.getSelection()?.removeAllRanges();
                }}
                style={{ top: pick.top, left: pick.left, transform: "translateY(-100%)" }}
                className="absolute z-10 rounded-md border border-line-strong bg-surface px-2 py-0.5 text-[12px] text-text shadow-sm transition-colors hover:bg-surface-2"
              >
                引用
              </button>
            )}
          </div>

          <ReplyActions text={props.reply || props.streaming} copied={copied} onCopy={copyReply} />

          {/* 产物条：一轮说"改好了"，用户第一反应是"东西在哪"。
              原来只有展开"过程"或开右侧面板才看得见，等于让他自己找。
              这里给一行：几个文件、几个新增、可点的文件名。 */}
          {!running && props.files.length > 0 && (
            <ArtifactStrip files={props.files} onFileClick={jumpToFile} />
          )}

          {/* 等审批时不算"在思考"。running 把 waiting_approval 也算在跑（输入框该显示
              停止按钮），但这里不能跟着显示动词和计时：那会让人以为它还在干活，
              实际是它在等用户点一下。计时器一路涨到"翻代码 44 分"，页面上却没说是谁在等谁。 */}
          {!props.streaming && !props.reply && running && props.stepCount === 0 && props.approvals.length === 0 && (
            <ThinkingLine createdAt={props.createdAt} />
          )}

          {props.reason && (
            <FailureNotice
              copy={explainFailure(props.rawReason || props.reason, props.failureCode)}
              raw={props.rawReason || props.reason}
              code={props.failureCode ?? ""}
              at={timeLabel(props.createdAt)}
              onRetry={props.onRetry}
            />
          )}

          {/* 技能提示只在一轮还在跑时出现：失败/结束后再推荐"这次适合某个技能"，
              是在用户已经看到红字之后补一句广告，不解决他眼前的问题。 */}
          {props.skillHint && props.onUseSkill && running && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[12px] text-faint">
              <span>这次可能适合</span>
              <button
                type="button"
                onClick={() => props.onUseSkill?.(props.skillHint!.display)}
                title={`命中：${props.skillHint.reason}`}
                className="rounded-md border border-line bg-surface-2 px-1.5 py-0.5 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
              >
                {props.skillHint.display}
              </button>
            </div>
          )}

          {/* 审批卡不在这里渲染：它要出现在输入框上方（用户眼睛和手所在的地方），
              而不是在一屏聊天记录底部等着被滚到。原来放这里时，长对话里它经常
              在视口外，用户只会看到输入框被禁用、不知道为什么。见 pending-panel.tsx。 */}

          {props.questions.length > 0 && (
            <QuestionCard
              questions={props.questions}
              busy={props.actionBusy}
              onAnswer={(answers) => props.onAnswer(props.questions, answers)}
            />
          )}

          {!running && !hasBody && props.stepCount === 0 && (
            <p className="text-[13px] text-faint">这一轮没有留下可显示的内容。</p>
          )}

          {/* 有 reason 时重试按钮由 FailureNotice 给：那里还多了一句"为什么"和
              "这条能不能靠重试解决"。两处都画，用户会以为有两个不同的重试。 */}
          {!running && !props.reason && (props.status === "failed" || props.status === "cancelled") && (
            <button
              type="button"
              onClick={props.onRetry}
              className="mt-2 rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
            >
              用同样的话再试一次
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * 计划卡片。三态用字符而不是彩色圆点：字符在色觉差异下也能读，
 * 且和正文的字重/字号同一档，不抢正文。
 */
/** 计划超过这个条数就折尾。短计划（3–5 步，绝大多数）完全不受影响。 */
const PLAN_LIMIT = 6;

function PlanCard({ plan, waiting = false }: { plan: PlanView; waiting?: boolean }) {
  const percent = plan.total > 0 ? Math.round((plan.completed / plan.total) * 100) : 0;
  // 保持原顺序，只折尾部：计划是有先后次序的，把"进行中"排到前面会让
  // 第 3 步看起来像第 1 步。折叠掉的永远是最后几条，用户往下读的顺序不变。
  const shown = plan.items.length > PLAN_LIMIT ? plan.items.slice(0, PLAN_LIMIT) : plan.items;
  const hidden = plan.items.length - shown.length;
  // 等审批时事件里没有 in_progress：整张计划会显示成一行「0 / 2」加两个空心圈，
  // 看不出它其实已经站在第一步上、只是那一步要人点一下才能走。
  const firstOpen = plan.items.findIndex((item) => item.status !== "completed");
  return (
    // 不套边框：这一屏里已经有审批卡（唯一的"要你动手"的框）和用户气泡，
    // 计划再画一个框就变成三个同等重量的盒子。用一条左边线把它挂在正文旁边，
    // 读得出"这是旁注"，也不抢正文。
    <div className="mb-2 ml-7 border-l-2 border-line-strong pl-3">
      <div className="flex items-center gap-2">
        <span className="text-[12px] font-medium text-muted">计划</span>
        <span className="font-mono text-[11px] text-faint">
          {plan.completed} / {plan.total}
        </span>
        {/* 有一步坏了就写出来。只在标题行说"3/5"会让人以为剩下两步在路上。 */}
        {plan.failed > 0 && (
          <span className="font-mono text-[11px] text-danger">{plan.failed} 步失败</span>
        )}
        {waiting && firstOpen >= 0 && (
          <span className="font-mono text-[11px] text-warn">停在第 {firstOpen + 1} 步等你批准</span>
        )}
        <div className="ml-auto h-1 w-20 overflow-hidden rounded-full bg-surface-3" aria-hidden>
          <div className="h-full rounded-full bg-brand transition-[width] duration-300" style={{ width: `${percent}%` }} />
        </div>
      </div>
      <ol className="mt-1.5 flex flex-col gap-1">
        {shown.map((item, index) => {
          const done = item.status === "completed";
          // 失败与取消都不该画成"还在做"。failed 用 ▲ + 红：它需要有人看一眼，
          // 而不是混在灰字里被当成"迟早会完成"。
          const dead = item.status === "cancelled";
          const broken = item.status === "failed";
          const active = !broken && !dead && (item.status === "in_progress" || item.status === "running" || (waiting && index === firstOpen));
          return (
            <li key={`${index}-${item.text}`} className="flex items-start gap-1.5 text-[13px] leading-relaxed">
              <span
                className={`mt-[1px] shrink-0 font-mono text-[11px] ${
                  broken ? "text-danger" : done ? "text-accent" : active ? "text-brand" : "text-faint"
                }`}
                aria-hidden
              >
                {broken ? "▲" : done ? "✓" : dead ? "–" : active ? "▸" : "○"}
              </span>
              <span
                className={
                  broken
                    ? "text-danger"
                    : done
                      ? "text-faint line-through decoration-line-strong"
                      : dead
                        ? "text-faint line-through decoration-line"
                        : active
                          ? "text-text"
                          : "text-muted"
                }
              >
                {item.text}
              </span>
            </li>
          );
        })}
        {hidden > 0 && (
          <li className="pl-[14px] font-mono text-[11px] text-faint">… 还有 {hidden} 项</li>
        )}
      </ol>
    </div>
  );
}
