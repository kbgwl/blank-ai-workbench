"use client";

import { useEffect, useRef, useState } from "react";
import { Button, Notice } from "@/components/projects/ui";
import { clip, formatClock, formatDuration } from "./format";
import { nodeLabel, riskLabel, toolStatusClass } from "./status";
import type { Block, TermLine } from "./timeline";
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
    <pre className="max-h-40 overflow-auto rounded-md border border-line bg-black/30 px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre text-muted">
      {text}
    </pre>
  );
}

function OutputBlock({ text }: { text: string }) {
  if (!text) return <p className="font-mono text-[11px] text-faint">暂无输出</p>;
  return (
    <pre className="max-h-44 overflow-auto rounded-md border border-line bg-black/30 px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words text-muted">
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
        <span className="shrink-0 font-mono text-[10px] text-faint">#{block.seq}</span>
      </header>
      <div className="mt-2 flex flex-col gap-2">{children}</div>
    </article>
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
      title="执行计划"
      trailing={
        <span className="shrink-0 rounded-md border border-brand/30 bg-brand/10 px-1.5 py-0.5 font-mono text-[10px] text-brand">
          计划
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
      {block.node && <p className="font-mono text-[10px] text-faint">节点 {nodeLabel(block.node)}</p>}
    </CardShell>
  );
}

function ToolCard({ block }: { block: Extract<Block, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const status = toolStatusClass(block.status);
  const running = block.status === "running";
  return (
    <CardShell
      block={block}
      accent={
        block.status === "error" || block.status === "failed"
          ? "border-danger/30 bg-danger/[0.05]"
          : "border-line bg-surface/60"
      }
      icon={
        <span className={running ? "text-brand" : "text-muted"}>
          <BlockIcon kind="tool" />
        </span>
      }
      title={block.name}
      trailing={
        <span className="flex shrink-0 items-center gap-2">
          {block.risk && (
            <span className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
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
      <div className="flex items-center gap-2">
        <span className="font-mono text-[10px] tracking-wide text-faint uppercase">输入</span>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="ml-auto font-mono text-[10px] text-brand/80 hover:text-brand"
        >
          {open ? "收起参数" : "展开参数"}
        </button>
      </div>
      {open ? <JsonBlock value={block.args} /> : (
        <p className="truncate font-mono text-[11px] text-muted">
          {Object.keys(block.args).length === 0
            ? "无参数"
            : clip(JSON.stringify(block.args).replace(/[{}"]/g, "").slice(0, 120), 120)}
        </p>
      )}
      <span className="font-mono text-[10px] tracking-wide text-faint uppercase">输出</span>
      <OutputBlock text={block.output} />
      {block.node && <p className="font-mono text-[10px] text-faint">节点 {nodeLabel(block.node)}</p>}
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
      accent="border-line-strong bg-black/45"
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

function ApprovalCard({
  block,
  busy,
  onDecide,
}: {
  block: Extract<Block, { kind: "approval" }>;
  busy: boolean;
  onDecide: (decision: "approve" | "deny", block: Extract<Block, { kind: "approval" }>) => void;
}) {
  const decided = block.state !== "pending";
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
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
          {block.state === "pending" ? "待审批" : block.state === "granted" ? "已批准" : "已拒绝"}
        </span>
      }
    >
      {block.risk && <p className="text-[12px] text-warn">{riskLabel(block.risk)}操作，需人工确认后才继续执行</p>}
      {block.message && <p className="text-[12px] leading-relaxed text-muted">{block.message}</p>}
      <JsonBlock value={block.args} />
      {decided ? (
        <p className="font-mono text-[11px] text-faint">该请求已处理，不会重复执行。</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="danger" disabled={busy} onClick={() => onDecide("deny", block)}>
            拒绝
          </Button>
          <Button variant="primary" disabled={busy} onClick={() => onDecide("approve", block)}>
            {busy ? "提交中" : "批准并继续"}
          </Button>
          <span className="font-mono text-[10px] text-faint">
            {block.toolCallId ? `调用 ${block.toolCallId.slice(0, 8)}` : ""}
          </span>
        </div>
      )}
    </CardShell>
  );
}

function ResultCard({ block }: { block: Extract<Block, { kind: "result" }> }) {
  const tone =
    block.status === "succeeded"
      ? { accent: "border-accent/35 bg-accent/[0.06]", text: "text-accent", label: "最终结果 · 成功" }
      : block.status === "failed"
        ? { accent: "border-danger/35 bg-danger/[0.06]", text: "text-danger", label: "最终结果 · 失败" }
        : { accent: "border-line bg-surface/60", text: "text-faint", label: "最终结果 · 已取消" };
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
      trailing={<span className={`font-mono text-[10px] ${tone.text}`}>{block.title}</span>}
    >
      {block.summary ? (
        <p className="text-[13px] leading-relaxed whitespace-pre-wrap text-text/90">{block.summary}</p>
      ) : (
        <p className="text-[12px] text-faint">后端未返回结果摘要</p>
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
      <span className="text-muted">
        <BlockIcon kind="change" />
      </span>
      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-text/90" title={block.path}>
        {block.path}
      </span>
      {block.changeType && (
        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
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
}: {
  block: Block;
  busyApprovalKey: string | null;
  onHighlight: (path: string) => void;
  onDecide: (decision: "approve" | "deny", block: Extract<Block, { kind: "approval" }>) => void;
}) {
  switch (block.kind) {
    case "plan":
      return <PlanCard block={block} />;
    case "tool":
      return <ToolCard block={block} />;
    case "terminal":
      return <TerminalBlockView block={block} />;
    case "approval":
      return (
        <ApprovalCard
          block={block}
          busy={busyApprovalKey === block.key}
          onDecide={(decision, target) => onDecide(decision, target)}
        />
      );
    case "result":
      return <ResultCard block={block} />;
    case "change":
      return <ChangeCard block={block} onHighlight={onHighlight} />;
    default:
      return <SystemRow block={block} />;
  }
}
