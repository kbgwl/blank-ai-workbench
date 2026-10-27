"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { Button, Notice } from "@/components/projects/ui";
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
import { approvalGrantOptions, type ApprovalScope } from "./api";
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
    return <p className="font-mono text-[11px] text-faint">无参数</p>;
  }
  const text = JSON.stringify(value, null, 2);
  return (
    <pre className="max-h-40 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre text-text/80">
      {text}
    </pre>
  );
}

function OutputBlock({ text }: { text: string }) {
  if (!text) return <p className="font-mono text-[11px] text-faint">暂无输出</p>;
  return (
    <pre className="max-h-44 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words text-text/80">
      {text}
    </pre>
  );
}

function CardShell({
  block,
  accent,
  icon,
  title,
  trailing,
  children,
}: {
  block: Block;
  accent: string;
  icon: React.ReactNode;
  title: string;
  trailing?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <article className={`rounded-xl border px-3.5 py-3 ${accent}`}>
      <header className="flex flex-wrap items-center gap-2">
        <span className="shrink-0">{icon}</span>
        <h3 className="min-w-0 flex-1 text-[13px] font-medium tracking-tight">{title}</h3>
        {trailing}
      </header>
      <div className="mt-2 flex flex-col gap-2">{children}</div>
    </article>
  );
}

function FactList({ facts }: { facts: ToolFact[] }) {
  if (facts.length === 0) return null;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px] leading-relaxed">
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
  return (
    <CardShell
      block={block}
      accent="border-brand/35 bg-brand/[0.06]"
      icon={
        <span className="text-brand">
          <BlockIcon kind="plan" />
        </span>
      }
      title="拆分步骤"
      trailing={
        <span className="shrink-0 rounded-md border border-brand/30 bg-brand/10 px-1.5 py-0.5 font-mono text-[10px] text-brand">
          {block.items.length} 步
        </span>
      }
    >
      {block.items.length === 0 ? (
        <p className="text-[12px] text-faint">暂无计划条目</p>
      ) : (
        <ol className="flex flex-col gap-1.5">
          {block.items.map((item, index) => (
            <li key={`${item}-${index}`} className="flex items-start gap-2 text-[13px] leading-relaxed">
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
      <span className="font-mono text-[10px] tracking-wide text-faint uppercase">
        {title}（{refs.length} 条）
      </span>
      <ul className="flex max-h-40 flex-col gap-1 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2">
        {refs.map((ref, index) => (
          <li key={`${ref.index}-${ref.sha}-${index}`} className="font-mono text-[11px] leading-relaxed text-text/80">
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
        <span className="font-mono text-[10px] text-faint">{formatClock(block.at)}</span>
      }
    >
      <p className="text-[12px] leading-relaxed text-muted">
        {compactionHeadline(block.covered, block.folded, block.tokensSaved)}
      </p>
      <FactList facts={facts} />
      {(hasChain || hasReplacement) && (
        <details className="group">
          <summary className="cursor-pointer list-none font-mono text-[10px] tracking-wide text-faint hover:text-muted">
            这一次替换换掉了哪些历史（只有位置、字数与指纹，不含正文）
          </summary>
          <div className="mt-2 flex flex-col gap-2">
            <FactList facts={chainFacts} />
            <RefList title="丢弃" refs={block.replacement.dropped} />
            <RefList title="新进" refs={block.replacement.added} />
            <RefList title="替换后的历史" refs={block.replacement.items} />
            {!hasReplacement && (
              <p className="text-[11px] leading-relaxed text-faint">
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
      accent={failed ? "border-danger/30 bg-danger/[0.05]" : "border-line bg-surface/60"}
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
              className={`rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${
                boundaryNeedsWarn(block.boundary, block.boundaryFacts) ? "border-warn/40 text-warn" : "border-line text-muted"
              }`}
            >
              {boundaryLabel(block.boundary, block.boundaryFacts)}
            </span>
          )}
          {block.risk && (
            <span className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-text/80">
              {riskLabel(block.risk)}
            </span>
          )}
          <span className={`font-mono text-[10px] ${status.className}`}>
            {running && <span className="mr-1 inline-block size-1.5 animate-pulse rounded-full bg-brand align-middle" />}
            {status.label}
          </span>
          <span className="font-mono text-[10px] text-faint">
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
          <span className="block break-words text-[12.5px] leading-relaxed text-text/85">
            {view.target || "无参数"}
          </span>
          {view.result && (
            <span
              className={`mt-0.5 block break-words text-[11px] leading-relaxed ${
                failed ? "text-danger/90" : "text-muted"
              }`}
            >
              {view.result}
            </span>
          )}
        </span>
        <span className="mt-0.5 shrink-0 font-mono text-[10px] text-brand/80 hover:text-brand">
          {open ? "收起" : "详情"}
        </span>
      </button>
      {open && (
        <>
          <FactList facts={view.facts} />
          {view.items.length > 0 && (
            <div className="max-h-56 overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2">
              <pre className="font-mono text-[11px] leading-relaxed whitespace-pre text-text/80">
                {view.items.join("\n")}
              </pre>
            </div>
          )}
          <details className="group">
            <summary className="cursor-pointer list-none font-mono text-[10px] tracking-wide text-faint hover:text-muted">
              原始数据（{block.name}）
            </summary>
            <div className="mt-2 flex flex-col gap-2">
              <JsonBlock value={block.args} />
              <span className="font-mono text-[10px] tracking-wide text-faint uppercase">输出</span>
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
          className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
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
        className="max-h-60 overflow-y-auto pr-1 font-mono text-[11px] leading-relaxed outline-none focus-visible:ring-1 focus-visible:ring-brand/40"
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
    answers: Record<string, string>,
    block: Extract<Block, { kind: "question" }>,
  ) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const answered = block.state === "answered";
  const complete = block.questions.every((item) => (draft[item.id] ?? "").trim().length > 0);

  return (
    <CardShell
      block={block}
      accent={answered ? "border-line bg-surface/60" : "border-brand/35 bg-brand/[0.05]"}
      icon={
        <span className={answered ? "text-faint" : "text-brand"}>
          <BlockIcon kind="question" />
        </span>
      }
      title={block.questions.length > 1 ? `需要你确认 ${block.questions.length} 件事` : "需要你确认"}
      trailing={
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-text/80">
          {answered ? "已回答" : "等待回答"}
        </span>
      }
    >
      {block.questions.map((item) => (
        <div key={item.id} className="space-y-2">
          {item.header && <p className="font-mono text-[10px] uppercase tracking-wide text-faint">{item.header}</p>}
          <p className="text-[13px] leading-relaxed text-text">{item.question}</p>
          {!answered && item.options.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {item.options.map((option) => {
                const active = draft[item.id] === option.label;
                return (
                  <button
                    key={option.label}
                    type="button"
                    title={option.description || undefined}
                    disabled={busy}
                    onClick={() => setDraft((prev) => ({ ...prev, [item.id]: option.label }))}
                    className={`rounded-md border px-2 py-1 text-[12px] transition-colors ${
                      active
                        ? "border-brand/50 bg-brand/15 text-text"
                        : "border-line text-muted hover:border-line/80 hover:text-text"
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
              value={draft[item.id] ?? ""}
              onChange={(event) => setDraft((prev) => ({ ...prev, [item.id]: event.target.value }))}
              placeholder={item.options.length ? "或者直接写你的答案" : "写下你的答案"}
              className="w-full rounded border border-line bg-transparent px-2 py-1.5 text-[12px] text-text outline-none focus:border-brand/60 placeholder:text-faint"
            />
          )}
        </div>
      ))}
      {answered ? (
        <p className="text-[12px] text-text/80">已按你的回答继续执行{block.answers ? `：${clip(block.answers, 120)}` : ""}</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            disabled={busy || !complete}
            onClick={() => onAnswer(draft, block)}
          >
            {busy ? "提交中" : "提交并继续"}
          </Button>
          <span className="text-[11px] text-faint">回答后任务从断点继续，不需要重新运行。</span>
        </div>
      )}
    </CardShell>
  );
}

function ApprovalCard({
  block,
  busy,
  onDecide,
}: {
  block: Extract<Block, { kind: "approval" }>;
  busy: boolean;
  onDecide: (
    decision: "approve" | "deny",
    block: Extract<Block, { kind: "approval" }>,
    scope?: ApprovalScope,
  ) => void;
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
      title={`审批请求 · ${block.tool}`}
      trailing={
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-text/80">
          {block.state === "pending" ? "待审批" : block.state === "granted" ? "已批准" : "已拒绝"}
        </span>
      }
    >
      {block.risk && <p className="text-[12px] text-warn">{riskLabel(block.risk)}操作，需人工确认后才继续执行</p>}
      {block.message && <p className="text-[12px] leading-relaxed text-text/80">{block.message}</p>}
      {reviewNote && (
        <p
          className="text-[11.5px] leading-relaxed text-muted"
          title="独立审查员先看过了；它放行不等于安全，它说话也不会替你批准"
        >
          {reviewNote}
        </p>
      )}
      <JsonBlock value={block.args} />
      {decided ? (
        <p className="font-mono text-[11px] text-faint">该请求已处理，不会重复执行。</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="danger" disabled={busy} onClick={() => onDecide("deny", block)}>
            拒绝
          </Button>
          {grants.map((option) => (
            <Button
              key={option.scope}
              variant={option.scope === "once" ? "primary" : "ghost"}
              disabled={busy}
              onClick={() => onDecide("approve", block, option.scope)}
            >
              {option.scope === "once" && busy ? "提交中" : option.label}
            </Button>
          ))}
          <span className="font-mono text-[10px] text-faint">
            {block.toolCallId ? `调用 ${block.toolCallId.slice(0, 8)}` : ""}
          </span>
        </div>
      )}
      {grants
        .filter((option) => option.warning)
        .map((option) => (
          <p key={option.scope} className="mt-1.5 text-[11px] leading-relaxed text-muted">
            {option.scope === "tool" ? "工具档：" : "本会话档："}
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
        : { accent: "border-line bg-surface/60", text: "text-faint", label: "已取消", tag: "取消" };
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
      trailing={<span className={`font-mono text-[10px] ${tone.text}`}>{tone.tag}</span>}
    >
      {block.summary ? (
        <ReplyView text={block.summary} />
      ) : (
        <p className="text-[12px] text-faint">没有返回内容</p>
      )}
      {block.reason && (
        <Notice tone={block.status === "failed" ? "error" : "muted"}>{block.reason}</Notice>
      )}
    </CardShell>
  );
}

function ChangeCard({ block, onHighlight }: { block: Extract<Block, { kind: "change" }>; onHighlight: (path: string) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface/50 px-3 py-2">
      <span className="text-text/80">
        <BlockIcon kind="change" />
      </span>
      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-text/90" title={block.path}>
        {block.path}
      </span>
      {block.changeType && (
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-text/80">
          {block.changeType}
        </span>
      )}
      <span className="shrink-0 font-mono text-[10px] text-faint">{formatClock(block.at)}</span>
      <button
        type="button"
        onClick={() => onHighlight(block.path)}
        className="shrink-0 rounded-md bg-surface-2 px-2 py-1 font-mono text-[10px] text-brand ring-1 ring-line hover:ring-brand/40"
      >
        在 Diff 面板查看
      </button>
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
      <span className={`min-w-0 truncate font-mono text-[11px] ${tone}`}>{block.title}</span>
      {block.detail && <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-faint">{block.detail}</span>}
      <span className="shrink-0 font-mono text-[10px] text-faint">{formatClock(block.at)}</span>
    </div>
  );
}

export function TimelineBlock({
  block,
  busyApprovalKey,
  onHighlight,
  onDecide,
  onAnswer,
}: {
  block: Block;
  busyApprovalKey: string | null;
  onHighlight: (path: string) => void;
  onDecide: (
    decision: "approve" | "deny",
    block: Extract<Block, { kind: "approval" }>,
    scope?: ApprovalScope,
  ) => void;
  onAnswer: (
    answers: Record<string, string>,
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
          onDecide={(decision, target, scope) => onDecide(decision, target, scope)}
        />
      );
    case "result":
      return <ResultCard block={block} />;
    case "boundary":
      return <BoundaryCard block={block} />;
    case "change":
      return <ChangeCard block={block} onHighlight={onHighlight} />;
    default:
      return <SystemRow block={block} />;
  }
}
