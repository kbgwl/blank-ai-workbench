"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { Button, Notice } from "@/components/projects/ui";
import { ApprovalOptions, approvalQuestion, type ApprovalDecision } from "./approval";
import type { SendBackChoice } from "./approval-copy";
import { clip, formatClock, formatDuration } from "./format";
import {
  boundaryLabel,
  boundaryNeedsWarn,
  compactionChainLabel,
  compactionHeadline,
  compactionReplacementLabel,
  compactionTriggerLabel,
  compactionWaterLabel,
  guardianReviewLabel,
  historyRefLabel,
  nodeLabel,
  riskLabel,
  toolStatusClass,
} from "./status";
import type { HistoryRef } from "./status";
import { ReplyView } from "./reply";
import { describeTool, type ToolFact } from "./tool-display";
import type { Block, TermLine } from "./timeline";
import { hopCopy, hopLine, hopRepeat } from "./transition-copy";
import { approvalGrantOptions, type AnswerValues } from "./api";
import { groupByMilestone, statusLabel, statusMark } from "./plan";
import { BlockIcon } from "./bits";

const toneClass: Record<TermLine["tone"], string> = {
  cmd: "text-text",
  out: "text-muted",
  err: "text-danger",
  warn: "text-warn",
};

function JsonBlock({ value }: { value: Record<string, unknown> }) {
  const keys = Object.keys(value ?? {});
  if (keys.length === 0) {
    return <p className="font-mono text-[12px] text-faint">无参数</p>;
  }
  const text = JSON.stringify(value, null, 2);
  return (
    <pre className="max-h-40 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[12px] leading-relaxed whitespace-pre text-text/80">
      {text}
    </pre>
  );
}

function OutputBlock({ text }: { text: string }) {
  if (!text) return <p className="font-mono text-[12px] text-faint">暂无输出</p>;
  return (
    <pre className="max-h-44 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[12px] leading-relaxed whitespace-pre-wrap break-words text-text/80">
      {text}
    </pre>
  );
}

function CardShell({
  accent,
  icon,
  title,
  trailing,
  children,
  flat = false,
}: {
  block: Block;
  accent: string;
  icon: React.ReactNode;
  title: string;
  trailing?: React.ReactNode;
  children: React.ReactNode;
  /**
   * 一次任务里十几次工具调用，每条都套一个描边卡片，一屏就成了四个盒子叠在一起。
   * flat 版本只留图标 + 一行标题，靠间距分块——需要你动手的卡片（审批/提问）才保留外框。
   */
  flat?: boolean;
}) {
  return (
    <article className={flat ? "px-0.5 py-1" : `rounded-xl border px-3.5 py-3 ${accent}`}>
      <header className="flex flex-wrap items-center gap-2">
        <span className="shrink-0">{icon}</span>
        <h3
          className={
            flat
              ? "min-w-0 flex-1 text-[13px] font-medium tracking-tight text-text/85"
              : "min-w-0 flex-1 text-[14px] font-medium tracking-tight"
          }
        >
          {title}
        </h3>
        {trailing}
      </header>
      <div className={flat ? "mt-1 flex flex-col gap-1.5" : "mt-2 flex flex-col gap-2"}>{children}</div>
    </article>
  );
}

function FactList({ facts }: { facts: ToolFact[] }) {
  if (facts.length === 0) return null;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px] leading-relaxed">
      {facts.map((fact, index) => (
        <Fragment key={`${fact.label}-${index}`}>
          <dt className="whitespace-nowrap text-faint">{fact.label}</dt>
          <dd className="min-w-0 break-words text-text/85">{fact.value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

function PlanCard({ block }: { block: Extract<Block, { kind: "plan" }> }) {
  const progress = block.progress;
  // 老事件只有文本条目：照旧渲染成编号列表，不假装有进度。
  const structured = block.steps.length > 0;
  const groups = structured ? groupByMilestone(block.steps, block.milestones) : [];
  const showBar = structured && progress.total > 0;

  return (
    <CardShell
      block={block}
      accent="border-brand/35 bg-brand/[0.06]"
      icon={
        <span className="text-brand">
          <BlockIcon kind="plan" />
        </span>
      }
      title="执行计划"
      trailing={
        showBar ? (
          <span className="shrink-0 rounded-md border border-brand/30 bg-brand/10 px-1.5 py-0.5 font-mono text-[11px] text-brand">
            {progress.completed}/{progress.total}
            {progress.percent === 100 ? " · 已完成" : ""}
          </span>
        ) : (
          <span className="shrink-0 rounded-md border border-brand/30 bg-brand/10 px-1.5 py-0.5 font-mono text-[11px] text-brand">
            {block.items.length || block.steps.length} 步
          </span>
        )
      }
    >
      {showBar && (
        <div className="mb-2.5 flex items-center gap-2">
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-brand/15">
            <div
              className="h-full rounded-full bg-brand transition-[width] duration-300"
              style={{ width: `${Math.min(100, Math.max(0, progress.percent))}%` }}
            />
          </div>
          <span className="shrink-0 font-mono text-[11px] text-faint">{progress.percent}%</span>
        </div>
      )}

      {structured ? (
        <div className="flex flex-col gap-2.5">
          {groups.map((group) => (
            <div key={group.milestone?.id ?? "loose"} className="flex flex-col gap-1.5">
              {group.milestone && (
                <div className="flex items-center gap-1.5">
                  <span
                    className={[
                      "size-1.5 shrink-0 rounded-full",
                      group.milestone.status === "completed"
                        ? "bg-brand"
                        : group.milestone.status === "in_progress"
                          ? "bg-brand/70 ring-2 ring-brand/25"
                          : "bg-faint/40",
                    ].join(" ")}
                  />
                  <span className="min-w-0 truncate text-[12px] font-medium text-text/75">
                    {group.milestone.title}
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-faint">
                    {group.milestone.total > 0
                      ? `${group.milestone.completed}/${group.milestone.total}`
                      : "—"}
                  </span>
                </div>
              )}
              <ol className="flex flex-col gap-1.5">
                {group.steps.map((step) => (
                  <li
                    key={step.id}
                    className="flex items-start gap-2 text-[14px] leading-relaxed"
                  >
                    <span
                      className={[
                        "mt-0.5 grid size-4 shrink-0 place-items-center rounded-full border font-mono text-[9px]",
                        step.status === "completed"
                          ? "border-brand/60 bg-brand text-on-brand"
                          : step.status === "in_progress"
                            ? "border-brand/60 bg-brand/15 text-brand"
                            : step.status === "cancelled"
                              ? "border-dashed border-line bg-transparent text-faint"
                              : "border-line bg-surface-3 text-faint",
                      ].join(" ")}
                      title={statusLabel(step.status)}
                    >
                      {statusMark(step.status) || ""}
                    </span>
                    <span
                      className={[
                        "min-w-0",
                        step.status === "completed"
                          ? "text-text/55 line-through decoration-line"
                          : step.status === "cancelled"
                            ? "text-faint line-through decoration-line"
                            : "text-text/90",
                      ].join(" ")}
                    >
                      {step.text}
                    </span>
                  </li>
                ))}
              </ol>
              {group.steps.some((step) => step.evidence) && (
                <ul className="ml-6 flex flex-col gap-0.5">
                  {group.steps
                    .filter((step) => step.evidence)
                    .map((step) => (
                      <li key={`${step.id}-ev`} className="truncate font-mono text-[11px] text-faint">
                        {step.text.slice(0, 12)}：{step.evidence}
                      </li>
                    ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      ) : block.items.length === 0 ? (
        <p className="text-[13px] text-faint">暂无计划条目</p>
      ) : (
        <ol className="flex flex-col gap-1.5">
          {block.items.map((item, index) => (
            <li key={`${item}-${index}`} className="flex items-start gap-2 text-[14px] leading-relaxed">
              <span className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-brand/15 font-mono text-[9px] text-brand">
                {index + 1}
              </span>
              <span className="min-w-0 text-text/90">{item}</span>
            </li>
          ))}
        </ol>
      )}
    </CardShell>
  );
}

function RefList({ title, refs }: { title: string; refs: HistoryRef[] }) {
  if (refs.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      <span className="font-mono text-[11px] tracking-wide text-faint uppercase">
        {title}（{refs.length} 条）
      </span>
      <ul className="flex max-h-40 flex-col gap-1 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2">
        {refs.map((ref, index) => (
          <li key={`${ref.index}-${ref.sha}-${index}`} className="font-mono text-[12px] leading-relaxed text-text/80">
            {historyRefLabel(ref)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function BoundaryCard({ block }: { block: Extract<Block, { kind: "boundary" }> }) {
  // 窗口链与替换记录是 #42 才有的字段。老任务不是"第 0 窗、保留 0 条"，是没说这件事，
  // 所以这两组都按存在性判断，缺了就整段不渲染。
  const hasChain = block.windowChain.windowNumber !== null || !!block.windowChain.windowId;
  const hasReplacement = block.replacement.retained !== null;
  const facts: ToolFact[] = [
    { label: "边界", value: `${block.covered} 条之前已不是原文` },
    { label: "本次折入", value: block.folded ? `${block.folded} 条` : "" },
    { label: "压缩前后", value: block.tokensBefore && block.tokensAfter ? `${block.tokensBefore} → ${block.tokensAfter} tokens` : "" },
    { label: "触发依据", value: compactionTriggerLabel(block.triggeredBy) },
    { label: "水位", value: compactionWaterLabel(block.waterRatio, block.window) },
    { label: "同轮裁剪", value: block.microClipped ? `${block.microClipped} 条 · 省 ${block.microCharsSaved} 字符` : "" },
    { label: "历史窗口", value: compactionChainLabel(block.windowChain) },
    { label: "替换", value: compactionReplacementLabel(block.replacement) },
    { label: "节点", value: nodeLabel(block.node) },
  ].filter((item) => item.value);
  const chainFacts: ToolFact[] = [
    { label: "本窗", value: block.windowChain.windowId },
    { label: "上一窗", value: block.windowChain.previousWindowId },
    { label: "出生窗", value: block.windowChain.firstWindowId },
    {
      label: "替换前后",
      value:
        block.replacement.beforeItems !== null && block.replacement.afterItems !== null
          ? `${block.replacement.beforeItems} → ${block.replacement.afterItems} 条`
          : "",
    },
    { label: "压缩调用", value: block.windowChain.responseId },
  ].filter((item) => item.value);
  return (
    <CardShell
      block={block}
      accent="border-brand/25 bg-brand/[0.04]"
      icon={
        <span className="text-brand">
          <BlockIcon kind="system" />
        </span>
      }
      title="上下文已压缩"
      trailing={
        <span className="font-mono text-[11px] text-faint">{formatClock(block.at)}</span>
      }
    >
      <p className="text-[13px] leading-relaxed text-muted">
        {compactionHeadline(block.covered, block.folded, block.tokensSaved)}
      </p>
      <FactList facts={facts} />
      {(hasChain || hasReplacement) && (
        <details className="group">
          <summary className="cursor-pointer list-none font-mono text-[11px] tracking-wide text-faint hover:text-muted">
            这一次替换换掉了哪些历史（只有位置、字数与指纹，不含正文）
          </summary>
          <div className="mt-2 flex flex-col gap-2">
            <FactList facts={chainFacts} />
            <RefList title="丢弃" refs={block.replacement.dropped} />
            <RefList title="新进" refs={block.replacement.added} />
            <RefList title="替换后的历史" refs={block.replacement.items} />
            {!hasReplacement && (
              <p className="text-[12px] leading-relaxed text-faint">
                这条事件带了窗口链，但没有逐条替换记录。
              </p>
            )}
          </div>
        </details>
      )}
    </CardShell>
  );
}

function ToolCard({ block }: { block: Extract<Block, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const status = toolStatusClass(block.status);
  const running = block.status === "running";
  const failed = block.status === "error" || block.status === "failed";
  // 卡片正面只讲人话：这一步在做什么、对谁做、做出来什么。
  // 工具名、参数 JSON、原始输出都属于工程细节，收进展开里。
  const view = describeTool(block.name, block.args, block.output, failed);
  return (
    <CardShell
      block={block}
      accent={failed ? "border-danger/30 bg-danger/[0.05]" : ""}
      flat={!failed}
      icon={
        <span className={running ? "text-brand" : "text-muted"}>
          <BlockIcon kind="tool" />
        </span>
      }
      title={view.label}
      trailing={
        <span className="flex shrink-0 items-center gap-2">
          {block.boundary && (
            <span
              className={`rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${
                boundaryNeedsWarn(block.boundary, block.boundaryFacts) ? "border-warn/40 text-warn" : "border-line text-muted"
              }`}
            >
              {boundaryLabel(block.boundary, block.boundaryFacts)}
            </span>
          )}
          {block.risk && (
            <span className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[11px] text-text/80">
              {riskLabel(block.risk)}
            </span>
          )}
          <span className={`font-mono text-[11px] ${status.className}`}>
            {running && <span className="mr-1 inline-block size-1.5 animate-pulse rounded-full bg-brand align-middle" />}
            {status.label}
          </span>
          <span className="font-mono text-[11px] text-faint">
            {running ? "…" : formatDuration(block.durationMs)}
          </span>
        </span>
      }
    >
      {/* 默认折叠：一次任务十几次工具调用，每条都摊开输出会把对话流冲垮。 */}
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-start gap-2 text-left"
      >
        <span className="min-w-0 flex-1">
          <span className="block break-words text-[13px] leading-relaxed text-text/85">
            {view.target || "无参数"}
          </span>
          {view.result && (
            <span
              className={`mt-0.5 block break-words text-[12px] leading-relaxed ${
                failed ? "text-danger/90" : "text-muted"
              }`}
            >
              {view.result}
            </span>
          )}
        </span>
        <span className="mt-0.5 shrink-0 font-mono text-[11px] text-brand/80 hover:text-brand">
          {open ? "收起" : "详情"}
        </span>
      </button>
      {open && (
        <>
          <FactList facts={view.facts} />
          {view.items.length > 0 && (
            <div className="max-h-56 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2">
              <pre className="font-mono text-[12px] leading-relaxed whitespace-pre text-text/80">
                {view.items.join("\n")}
              </pre>
            </div>
          )}
          <details className="group">
            <summary className="cursor-pointer list-none font-mono text-[11px] tracking-wide text-faint hover:text-muted">
              原始数据（{block.name}）
            </summary>
            <div className="mt-2 flex flex-col gap-2">
              <JsonBlock value={block.args} />
              <span className="font-mono text-[11px] tracking-wide text-faint uppercase">输出</span>
              <OutputBlock text={block.output} />
            </div>
          </details>
        </>
      )}
    </CardShell>
  );
}

function TerminalBlockView({ block }: { block: Extract<Block, { kind: "terminal" }> }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  useEffect(() => {
    const node = scrollRef.current;
    if (node && pinned) node.scrollTop = node.scrollHeight;
  }, [block.lines.length, pinned]);

  return (
    <CardShell
      block={block}
      accent="border-line-strong bg-surface-2"
      icon={
        <span className="text-accent">
          <BlockIcon kind="terminal" />
        </span>
      }
      title={`终端输出 · ${block.title || "shell"}`}
      trailing={
        <button
          type="button"
          onClick={() => setPinned((value) => !value)}
          className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
            pinned ? "border-accent/30 bg-accent/10 text-accent" : "border-line bg-surface-2 text-faint hover:text-muted"
          }`}
        >
          {pinned ? "自动到底" : "已暂停跟随"}
        </button>
      }
    >
      <div
        ref={scrollRef}
        tabIndex={0}
        onScroll={(event) => {
          const node = event.currentTarget;
          setPinned(node.scrollHeight - node.scrollTop - node.clientHeight < 24);
        }}
        className="max-h-60 overflow-y-auto pr-1 font-mono text-[12px] leading-relaxed outline-none focus-visible:ring-1 focus-visible:ring-brand/40"
      >
        {block.lines.map((line, index) => (
          <div key={index} className="flex gap-2">
            <span className="w-4 shrink-0 text-right text-faint select-none">
              {line.tone === "cmd" ? "$" : " "}
            </span>
            <span className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${toneClass[line.tone]}`}>{line.text}</span>
          </div>
        ))}
      </div>
    </CardShell>
  );
}

function QuestionCard({
  block,
  busy,
  onAnswer,
}: {
  block: Extract<Block, { kind: "question" }>;
  busy: boolean;
  onAnswer: (
    answers: AnswerValues,
    block: Extract<Block, { kind: "question" }>,
  ) => void;
}) {
  const [draft, setDraft] = useState<AnswerValues>({});
  const answered = block.state === "answered";
  // 多选题的答案是数组；单选是字符串。判"答完没有"必须按形状分开看，
  // 否则 [] 会被当成非空、空串会被数组分支放过。
  const filled = (id: string): boolean => {
    const value = draft[id];
    return Array.isArray(value) ? value.length > 0 : Boolean((value ?? "").trim());
  };
  const complete = block.questions.every((item) => filled(item.id));
  const selected = (id: string, label: string): boolean => {
    const value = draft[id];
    return Array.isArray(value) ? value.includes(label) : value === label;
  };
  const activePreview = block.questions
    .flatMap((item) =>
      item.options.filter((option) => selected(item.id, option.label) && option.preview),
    )
    .map((option) => option.preview)[0];

  return (
    <CardShell
      block={block}
      accent={answered ? "border-line bg-surface" : "border-brand/35 bg-brand/[0.05]"}
      icon={
        <span className={answered ? "text-faint" : "text-brand"}>
          <BlockIcon kind="question" />
        </span>
      }
      title={block.questions.length > 1 ? `需要你确认 ${block.questions.length} 件事` : "需要你确认"}
      trailing={
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[11px] text-text/80">
          {answered ? "已回答" : "等待回答"}
        </span>
      }
    >
      {block.questions.map((item) => (
        <div key={item.id} className="space-y-2">
          {item.header && <p className="font-mono text-[11px] uppercase tracking-wide text-faint">{item.header}</p>}
          <p className="text-[14px] leading-relaxed text-text">{item.question}</p>
          {!answered && item.options.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {item.options.map((option) => {
                const active = selected(item.id, option.label);
                return (
                  <button
                    key={option.label}
                    type="button"
                    title={option.description || undefined}
                    disabled={busy}
                    onClick={() =>
                      setDraft((prev) => {
                        if (!item.multiSelect) return { ...prev, [item.id]: option.label };
                        const current = Array.isArray(prev[item.id]) ? (prev[item.id] as string[]) : [];
                        const next = current.includes(option.label)
                          ? current.filter((value) => value !== option.label)
                          : [...current, option.label];
                        return { ...prev, [item.id]: next };
                      })
                    }
                    className={`rounded-md border px-2 py-1 text-[13px] transition-colors ${
                      active
                        ? "border-brand/50 bg-brand/15 text-text"
                        : "border-line text-muted hover:border-line hover:text-text"
                    } disabled:cursor-not-allowed disabled:opacity-50`}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          )}
          {!answered && item.allowFreeText && (
            <input
              value={typeof draft[item.id] === "string" ? (draft[item.id] as string) : ""}
              onChange={(event) => setDraft((prev) => ({ ...prev, [item.id]: event.target.value }))}
              placeholder={item.options.length ? "或者直接写你的答案" : "写下你的答案"}
              className="w-full rounded border border-line bg-transparent px-2 py-1.5 text-[13px] text-text outline-none focus:border-brand/60 placeholder:text-faint"
            />
          )}
        </div>
      ))}
      {!answered && activePreview && (
        // preview 是模型给的 HTML 片段：这里只按纯文本展示，绝不塞进 innerHTML。
        <pre className="max-h-40 overflow-auto rounded-md border border-line bg-surface p-2 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-text/85">
          {activePreview}
        </pre>
      )}
      {answered ? (
        <p className="text-[13px] text-text/80">已按你的回答继续执行{block.answers ? `：${clip(block.answers, 120)}` : ""}</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            disabled={busy || !complete}
            onClick={() => onAnswer(draft, block)}
          >
            {busy ? "提交中" : "提交并继续"}
          </Button>
          <span className="text-[12px] text-faint">回答后任务从断点继续，不需要重新运行。</span>
        </div>
      )}
    </CardShell>
  );
}

function ApprovalCard({
  block,
  busy,
  steps,
  onDecide,
}: {
  block: Extract<Block, { kind: "approval" }>;
  busy: boolean;
  /** 可打回的步骤（做过或正在做的）。空表示这次只能普通驳回。 */
  steps?: SendBackChoice[];
  onDecide: (decision: ApprovalDecision) => void;
}) {
  const decided = block.state !== "pending";
  const grants = approvalGrantOptions(block);
  const reviewNote = guardianReviewLabel(block.guardian);
  return (
    <CardShell
      block={block}
      accent={
        block.state === "granted"
          ? "border-accent/30 bg-accent/[0.06]"
          : block.state === "denied"
            ? "border-danger/30 bg-danger/[0.06]"
            : "border-warn/35 bg-warn/[0.06]"
      }
      icon={
        <span className={block.state === "pending" ? "text-warn" : block.state === "granted" ? "text-accent" : "text-danger"}>
          <BlockIcon kind="approval" />
        </span>
      }
      title={approvalQuestion(block.tool, block.args)}
      trailing={
        <span className="flex shrink-0 items-center gap-1.5">
          <span className="font-mono text-[11px] text-faint">{block.tool}</span>
          {/* 风险档挪到标题行：它原来自己占一行写「高风险操作，需人工确认后才继续执行」，
              而这张卡本身就是确认动作，后半句是废话。 */}
          {block.risk && (
            <span className="rounded bg-warn/[0.14] px-1.5 py-0.5 font-mono text-[10px] text-warn">
              {riskLabel(block.risk)}
            </span>
          )}
          <span className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[11px] text-text/80">
            {block.state === "pending" ? "待审批" : block.state === "granted" ? "已批准" : "已拒绝"}
          </span>
        </span>
      }
    >
      {block.message && <p className="text-[13px] leading-relaxed text-text/80">{block.message}</p>}
      {reviewNote && (
        <p
          className="text-[12px] leading-relaxed text-muted"
          title="独立审查员先看过了；它放行不等于安全，它说话也不会替你批准"
        >
          {reviewNote}
        </p>
      )}
      {/* 计划门：把要批的那份计划摆出来。args 里通常没有它（模型写在计划文件里），
          所以只读 planPreview；空的时候不占位，任务详情页不必重复对话页那句警告。 */}
      {block.planPreview && (
        <pre className="max-h-64 overflow-auto rounded-md border border-line bg-surface px-2.5 py-2 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-text/85">
          {block.planPreview}
        </pre>
      )}
      <JsonBlock value={block.args} />
      <ApprovalOptions
        grants={grants}
        busy={busy}
        decided={decided}
        onApprove={(scope) => onDecide({ decision: "approve", target: block, scope })}
        steps={steps}
        onDeny={(feedback, stepId) =>
          onDecide({ decision: "deny", target: block, scope: "once", feedback, stepId })
        }
      />
      {grants
        .filter((option) => option.warning)
        .map((option) => (
          <p key={option.scope} className="mt-1.5 text-[12px] leading-relaxed text-muted">
            {option.scope === "tool" ? "工具档：" : option.scope === "session" ? "会话档：" : "命令档："}
            {option.warning}
          </p>
        ))}
    </CardShell>
  );
}

function ResultCard({ block }: { block: Extract<Block, { kind: "result" }> }) {
  const tone =
    block.status === "succeeded"
      ? { accent: "border-accent/35 bg-accent/[0.06]", text: "text-accent", label: "结果", tag: "成功" }
      : block.status === "failed"
        ? { accent: "border-danger/35 bg-danger/[0.06]", text: "text-danger", label: "执行失败", tag: "失败" }
        : { accent: "border-line bg-surface", text: "text-faint", label: "已取消", tag: "取消" };
  return (
    <CardShell
      block={block}
      accent={tone.accent}
      icon={
        <span className={tone.text}>
          <BlockIcon kind="result" />
        </span>
      }
      title={tone.label}
      trailing={<span className={`font-mono text-[11px] ${tone.text}`}>{tone.tag}</span>}
    >
      {block.summary ? (
        <ReplyView text={block.summary} />
      ) : (
        <p className="text-[13px] text-faint">没有返回内容</p>
      )}
      {block.reason && (
        <Notice tone={block.status === "failed" ? "error" : "muted"}>{block.reason}</Notice>
      )}
    </CardShell>
  );
}

function ChangeCard({ block, onHighlight }: { block: Extract<Block, { kind: "change" }>; onHighlight: (path: string) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2">
      <span className="text-text/80">
        <BlockIcon kind="change" />
      </span>
      <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-text/90" title={block.path}>
        {block.path}
      </span>
      {block.changeType && (
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[11px] text-text/80">
          {block.changeType}
        </span>
      )}
      <span className="shrink-0 font-mono text-[11px] text-faint">{formatClock(block.at)}</span>
      <button
        type="button"
        onClick={() => onHighlight(block.path)}
        className="shrink-0 rounded-md bg-surface-2 px-2 py-1 font-mono text-[11px] text-brand ring-1 ring-line hover:ring-brand/40"
      >
        在 Diff 面板查看
      </button>
    </div>
  );
}

function ChangesCard({ block }: { block: Extract<Block, { kind: "changes" }> }) {
  const shown = block.paths.slice(0, 8);
  const more = block.paths.length - shown.length;
  const covered = block.covers.length;
  const tone = block.missing
    ? "border-warn/40 text-warn"
    : covered > 0
      ? "border-line text-muted"
      : "border-line text-faint";
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-text/80">
          <BlockIcon kind="change" />
        </span>
        <span className="min-w-0 flex-1 truncate text-[12px] text-text/90">
          本轮改动 {block.paths.length} 个文件
          <span className="ml-1 text-[11px] text-faint">（来自交付事实，不是总结）</span>
        </span>
        {block.missing ? (
          <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${tone}`}>
            未跑校验
          </span>
        ) : covered > 0 ? (
          <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${tone}`}>
            校验覆盖 {covered}/{block.paths.length}
          </span>
        ) : null}
        <span className="shrink-0 font-mono text-[11px] text-faint">{formatClock(block.at)}</span>
      </div>
      <ul className="mt-1.5 space-y-0.5">
        {shown.map((path) => (
          <li key={path} className="truncate font-mono text-[11px] text-muted" title={path}>
            {path}
          </li>
        ))}
        {more > 0 && <li className="font-mono text-[11px] text-faint">…还有 {more} 个</li>}
      </ul>
    </div>
  );
}

function SystemRow({ block }: { block: Extract<Block, { kind: "system" }> }) {
  const tone =
    block.tone === "danger"
      ? "text-danger"
      : block.tone === "warn"
        ? "text-warn"
        : block.tone === "accent"
          ? "text-accent"
          : "text-faint";
  return (
    <div className="flex items-center gap-2 px-1 py-0.5">
      <span className={`size-1.5 shrink-0 rounded-full ${tone === "text-faint" ? "bg-line-strong" : "bg-current"} ${tone}`} />
      <span className={`min-w-0 truncate font-mono text-[12px] ${tone}`}>{block.title}</span>
      {block.detail && <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint">{block.detail}</span>}
      <span className="shrink-0 font-mono text-[11px] text-faint">{formatClock(block.at)}</span>
    </div>
  );
}

/**
 * 一次节点跳转：一行，回答"为什么又跑一轮"。
 *
 * 左边那句短话来自 `rule`（机读码翻出来的），右边那半句是后端由事实推出的原句
 * （"校验没通过（failed），回执行修（已用 2/3 轮修复）"）——数字不在这里重算，
 * 免得界面和事件里的账对不上。
 *
 * 认不出的 `rule` 不静默吞掉：标黄、把原始码和 from→to 一起放出来。时间线上
 * 出现一行"看起来合理"的错解释，比没有解释更糟。
 */
function HopRow({ block }: { block: Extract<Block, { kind: "hop" }> }) {
  const copy = hopCopy(block.rule);
  const line = hopLine(block.rule, block.why);
  const repeat = hopRepeat(block);
  const tone =
    line.tone === "warn" ? "text-warn" : line.tone === "accent" ? "text-accent" : "text-muted";
  return (
    <div
      className="flex items-center gap-2 px-1 py-0.5"
      title={`${copy.label} · ${nodeLabel(block.from)} → ${nodeLabel(block.to)}（${block.from} → ${block.to}）`}
    >
      <span className={`size-1.5 shrink-0 rounded-full ${line.tone === "muted" ? "bg-line-strong" : "bg-current"} ${tone}`} />
      <span className={`min-w-0 flex-1 truncate font-mono text-[12px] ${tone}`}>{line.text}</span>
      {line.code && <span className="shrink-0 font-mono text-[10px] text-warn">{line.code} · {block.from}→{block.to}</span>}
      {repeat && <span className="shrink-0 rounded border border-line px-1 font-mono text-[10px] text-faint">{repeat}</span>}
      <span className="shrink-0 font-mono text-[11px] text-faint">{formatClock(block.at)}</span>
    </div>
  );
}

export function TimelineBlock({
  block,
  busyApprovalKey,
  onHighlight,
  onDecide,
  onAnswer,
  steps,
}: {
  block: Block;
  busyApprovalKey: string | null;
  onHighlight: (path: string) => void;
  onDecide: (decision: ApprovalDecision) => void;
  /** 当前计划里能打回的步骤，交给审批卡做「退回重做」下拉。 */
  steps?: SendBackChoice[];
  onAnswer: (
    answers: AnswerValues,
    block: Extract<Block, { kind: "question" }>,
  ) => void;
}) {
  switch (block.kind) {
    case "plan":
      return <PlanCard block={block} />;
    case "tool":
      return <ToolCard block={block} />;
    case "terminal":
      return <TerminalBlockView block={block} />;
    case "question":
      return (
        <QuestionCard
          block={block}
          busy={busyApprovalKey === block.key}
          onAnswer={(answers, target) => onAnswer(answers, target)}
        />
      );
    case "approval":
      return (
        <ApprovalCard
          block={block}
          busy={busyApprovalKey === block.key}
          steps={steps}
          // 整个载荷按引用透传：之前这里只转发三个位置参数，用户写的驳回理由
          // 被静默丢掉，模型收到的还是"就是不批"，没有任何可执行信息。
          onDecide={onDecide}
        />
      );
    case "result":
      return <ResultCard block={block} />;
    case "boundary":
      return <BoundaryCard block={block} />;
    case "change":
      return <ChangeCard block={block} onHighlight={onHighlight} />;
    case "changes":
      return <ChangesCard block={block} />;
    case "hop":
      return <HopRow block={block} />;
    default:
      return <SystemRow block={block} />;
  }
}
