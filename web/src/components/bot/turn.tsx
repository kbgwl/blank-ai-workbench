"use client";

import { useState } from "react";
import { Button } from "@/components/projects/ui";
import { formatDuration } from "../tasks/format";
import type { AttachmentRef, PendingApproval, PendingQuestion } from "../tasks/api";
import { MarkdownView } from "./markdown-view";
import { StepView } from "./steps";
import type { BotStep, StepFilter } from "./turns";
import { filterSteps, turnEvidence } from "./turns";
import { uploadUrl } from "./uploads";

function timeLabel(value: string): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return "";
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function StatusChip({ status, running }: { status: string; running: boolean }) {
  if (running) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-brand/[0.08] px-2 py-0.5 text-[10.5px] font-medium text-brand">
        <span className="relative flex size-1.5">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-brand/60" />
          <span className="relative inline-flex size-1.5 rounded-full bg-brand" />
        </span>
        进行中
      </span>
    );
  }
  if (status === "waiting_approval") {
    return <span className="rounded-full bg-warn/[0.1] px-2 py-0.5 text-[10.5px] font-medium text-warn">等你批准</span>;
  }
  if (status === "failed" || status === "error") {
    return <span className="rounded-full bg-danger/[0.08] px-2 py-0.5 text-[10.5px] font-medium text-danger">失败</span>;
  }
  if (status === "cancelled" || status === "canceled") {
    return <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[10.5px] font-medium text-muted">已停止</span>;
  }
  return null;
}

function ApprovalCard({
  approval,
  onDecide,
  busy,
}: {
  approval: PendingApproval;
  onDecide: (decision: "approve" | "deny", scope: "once" | "prefix" | "tool") => void;
  busy: boolean;
}) {
  const command = String(approval.args.command ?? approval.args.path ?? "");
  return (
    <div className="my-2 rounded-xl border border-warn/35 bg-warn/[0.04] p-3">
      <p className="text-[12.5px] font-medium text-warn">
        需要你批准：{approval.tool}
        {approval.risk && <span className="ml-1.5 font-normal text-warn/80">（风险 {approval.risk}）</span>}
      </p>
      {command && (
        <pre className="mt-1.5 overflow-auto rounded-md bg-surface px-2.5 py-1.5 font-mono text-[11.5px] text-text">
          {command}
        </pre>
      )}
      {approval.message && <p className="mt-1 text-[11.5px] text-muted">{approval.message}</p>}
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        <Button variant="primary" disabled={busy} onClick={() => onDecide("approve", "once")}>
          允许这一次
        </Button>
        {approval.prefix && (
          <Button disabled={busy} onClick={() => onDecide("approve", "prefix")}>
            本会话允许 {approval.prefix}
          </Button>
        )}
        <Button disabled={busy} onClick={() => onDecide("approve", "tool")}>
          本会话允许该工具
        </Button>
        <Button variant="danger" disabled={busy} onClick={() => onDecide("deny", "once")}>
          拒绝
        </Button>
      </div>
    </div>
  );
}

function QuestionCard({
  questions,
  onAnswer,
  busy,
}: {
  questions: PendingQuestion[];
  onAnswer: (answers: Record<string, string>) => void;
  busy: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  return (
    <div className="my-2 rounded-xl border border-brand/30 bg-brand/[0.03] p-3">
      <p className="text-[12.5px] font-medium text-brand">它想先问你一句</p>
      <div className="mt-2 space-y-3">
        {questions.map((question) => (
          <div key={question.id}>
            <p className="text-[12.5px] text-text">{question.question}</p>
            {question.options.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {question.options.map((option) => (
                  <button
                    key={option.label}
                    type="button"
                    title={option.description}
                    onClick={() => setDraft((current) => ({ ...current, [question.id]: option.label }))}
                    className={`rounded-md border px-2 py-1 text-[11.5px] transition-colors ${
                      draft[question.id] === option.label
                        ? "border-brand/60 bg-brand/[0.08] text-brand"
                        : "border-line bg-surface text-muted hover:border-line-strong hover:text-text"
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            )}
            {question.allowFreeText && (
              <input
                value={draft[question.id] ?? ""}
                onChange={(event) => setDraft((current) => ({ ...current, [question.id]: event.target.value }))}
                placeholder="或者直接写你的答案"
                className="mt-1.5 w-full rounded-md border border-line bg-surface px-2.5 py-1.5 text-[12px] text-text placeholder:text-faint focus:border-brand/60 focus:outline-none"
              />
            )}
          </div>
        ))}
      </div>
      <Button
        variant="primary"
        className="mt-2.5"
        disabled={busy || Object.values(draft).every((value) => !value?.trim())}
        onClick={() => onAnswer(draft)}
      >
        回答并继续
      </Button>
    </div>
  );
}

export type TurnViewProps = {
  goal: string;
  /** 这一轮贴的图。回放时要看得见，否则用户不知道自己当时发了什么。 */
  images: AttachmentRef[];
  /** 拼图片读取地址用。会话属于当前项目，所以直接用当前项目 id。 */
  projectId: string;
  steps: BotStep[];
  reply: string;
  reason: string;
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
  onDecide: (approval: PendingApproval, decision: "approve" | "deny", scope: "once" | "prefix" | "tool") => void;
  onAnswer: (questions: PendingQuestion[], answers: Record<string, string>) => void;
  onRetry: () => void;
};

export function TurnView(props: TurnViewProps) {
  const { steps, running } = props;
  // 跑的时候展开过程（用户想看着它干活），跑完收起（只留结论）。
  const [openSteps, setOpenSteps] = useState<boolean | null>(null);
  const expanded = openSteps ?? running;
  const [filter, setFilter] = useState<StepFilter>("all");
  const evidence = turnEvidence(steps);
  const paths = props.files.map((file) => file.path);
  const shown = filterSteps(steps, filter);
  const hasBody = props.streaming !== "" || props.reply !== "" || props.reason !== "";

  // 正文里点文件名：就地定位到那一行，而不是另开一个页面去翻。
  // 收起状态下点也生效——先展开，再切到"改动"，因为用户点的就是改动。
  const jumpToFile = () => {
    setOpenSteps(true);
    setFilter("changes");
  };

  return (
    <div className="group/turn">
      {/* 用户说的话：右侧淡色气泡，与助手的整段正文区分开 */}
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-2 px-3.5 py-2 text-[13.5px] leading-relaxed text-text">
          {props.images.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1.5">
              {props.images.map((image) => (
                <a
                  key={image.id}
                  href={uploadUrl(props.projectId, image.id)}
                  target="_blank"
                  rel="noreferrer"
                  title={image.name}
                  className="block size-20 overflow-hidden rounded-lg border border-line transition-opacity hover:opacity-85"
                >
                  {/* 静态导出没有图片优化器，回放用的就是后端原图，走 img 才对。 */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={uploadUrl(props.projectId, image.id)} alt={image.name} className="size-full object-cover" />
                </a>
              ))}
            </div>
          )}
          <div className="whitespace-pre-wrap">{props.goal}</div>
        </div>
      </div>

      <div className="mt-3.5">
        <div className="mb-1 flex items-center gap-2">
          <span className="grid size-5 shrink-0 place-items-center rounded-md bg-[#1b2a4a]">
            <span className="size-1.5 rounded-[2px] bg-[#5eead4]" />
          </span>
          <span className="text-[12.5px] font-semibold tracking-tight">空白格</span>
          <StatusChip status={props.status} running={running} />
          {props.createdAt && (
            <span className="ml-auto font-mono text-[10px] text-faint">{timeLabel(props.createdAt)}</span>
          )}
        </div>

        {/* 依据条：一行数字回答"它到底做了什么"，正文因此不必再复述过程。
            每个数字都能点开落到具体的行，这就是"回复短但不丢溯源"的地方。 */}
        {props.stepCount > 0 && (
          <div className="ml-7">
            <button
              type="button"
              onClick={() => setOpenSteps(!expanded)}
              title={evidence.commands.length ? `跑过的命令：\n${evidence.commands.join("\n")}` : undefined}
              className="mb-1 flex max-w-full items-center gap-1.5 rounded-md px-1 py-0.5 text-[11px] text-faint transition-colors hover:bg-surface-2 hover:text-muted"
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
              {evidence.durationMs > 0 && (
                <span className="ml-1 shrink-0 font-mono">· {formatDuration(evidence.durationMs)}</span>
              )}
            </button>

            {expanded && (
              <div className="pb-1">
                {/* 筛选只在真有内容时出现：一个点了什么都没有的按钮，比没有按钮更让人怀疑。 */}
                {(props.files.length > 0 || evidence.failed > 0) && (
                  <div className="mb-1 flex items-center gap-1 pl-0.5">
                    {(
                      [
                        ["all", `全部 ${steps.length}`],
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
                          className={`rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                            filter === key ? "bg-surface-3 text-text" : "text-faint hover:bg-surface-2 hover:text-muted"
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                  </div>
                )}
                <div className="space-y-0.5">
                  {shown.length === 0 && <p className="px-1.5 py-1 text-[11.5px] text-faint">这一步没有可显示的内容。</p>}
                  {shown.map((step) => (
                    <StepView key={step.key} step={step} />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <div className="ml-7">
          {props.streaming && (
            <div className="relative">
              <MarkdownView text={props.streaming} files={paths} onFileClick={jumpToFile} />
              <span className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-[2px] animate-pulse bg-brand align-middle" />
            </div>
          )}

          {!props.streaming && props.reply && (
            <MarkdownView text={props.reply} files={paths} onFileClick={jumpToFile} />
          )}

          {!props.streaming && !props.reply && running && props.stepCount === 0 && (
            <p className="flex items-center gap-2 py-1 text-[12.5px] text-muted">
              <span className="size-2 animate-pulse rounded-full bg-brand" />
              在想怎么做…
            </p>
          )}

          {props.reason && (
            <p className="mt-1 rounded-lg border border-danger/25 bg-danger/[0.04] px-2.5 py-1.5 text-[12px] text-danger">
              {props.reason}
            </p>
          )}

          {props.approvals.map((approval) => (
            <ApprovalCard
              key={approval.toolCallId || approval.tool}
              approval={approval}
              busy={props.actionBusy}
              onDecide={(decision, scope) => props.onDecide(approval, decision, scope)}
            />
          ))}

          {props.questions.length > 0 && (
            <QuestionCard
              questions={props.questions}
              busy={props.actionBusy}
              onAnswer={(answers) => props.onAnswer(props.questions, answers)}
            />
          )}

          {!running && !hasBody && props.stepCount === 0 && (
            <p className="text-[12px] text-faint">这一轮没有留下可显示的内容。</p>
          )}

          {!running && (props.status === "failed" || props.status === "cancelled") && (
            <button
              type="button"
              onClick={props.onRetry}
              className="mt-2 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted transition-colors hover:border-line-strong hover:text-text"
            >
              用同样的话再试一次
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
