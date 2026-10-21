"use client";

import { useState } from "react";
import { formatDuration } from "../tasks/format";
import type { BotStep } from "./turns";

const ICONS: Record<string, string> = {
  read: "M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5v-13ZM11 4h7.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H11",
  write: "m4 20 4.5-1 9-9a2.1 2.1 0 0 0-3-3l-9 9L4 20Z",
  shell: "m5 6 5 5-5 5M12 17h7",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16.5 16.5 21 21",
  plan: "M4 6h3v3H4zM4 15h3v3H4zM10 7.5h10M10 16.5h10",
  other: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM12 3v2M12 19v2M3 12h2M19 12h2",
};

function kindOf(name: string): keyof typeof ICONS {
  const lower = name.toLowerCase();
  if (/^(read|list|open|view|cat)/.test(lower)) return "read";
  if (/(write|patch|edit|create|delete|move|rename|apply)/.test(lower)) return "write";
  if (/(shell|bash|exec|run|test|command)/.test(lower)) return "shell";
  if (/(search|grep|glob|find|query)/.test(lower)) return "search";
  if (/plan|todo|task/.test(lower)) return "plan";
  return "other";
}

function ToolIcon({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="size-3.5 shrink-0"
      aria-hidden
    >
      <path d={ICONS[kindOf(name)]} />
    </svg>
  );
}

function StatusDot({ status }: { status: "running" | "ok" | "failed" }) {
  if (status === "running") {
    return (
      <span className="relative flex size-2 shrink-0">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-brand/60" />
        <span className="relative inline-flex size-2 rounded-full bg-brand" />
      </span>
    );
  }
  return (
    <span
      className={`size-1.5 shrink-0 rounded-full ${status === "failed" ? "bg-danger" : "bg-accent"}`}
      aria-label={status === "failed" ? "失败" : "完成"}
    />
  );
}

function ToolStepRow({ step }: { step: Extract<BotStep, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  // 参数里的 command / path 比工具名更能说明"它在干什么"，优先拿它当副标题。
  const hint =
    String(step.args.command ?? "") ||
    String(step.args.path ?? "") ||
    String(step.args.pattern ?? "") ||
    String(step.args.query ?? "") ||
    "";
  const hasDetail = Object.keys(step.args).length > 0 || step.output !== "";

  return (
    <div className="group/step rounded-md">
      <button
        type="button"
        onClick={() => hasDetail && setOpen((value) => !value)}
        disabled={!hasDetail}
        className={`flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors ${
          hasDetail ? "hover:bg-surface-2" : "cursor-default"
        }`}
      >
        <span className={step.status === "failed" ? "text-danger" : "text-faint"}>
          <ToolIcon name={step.name} />
        </span>
        <span className="shrink-0 font-mono text-[11.5px] text-muted">{step.name}</span>
        {hint && (
          <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-faint" title={hint}>
            {hint}
          </span>
        )}
        {!hint && <span className="flex-1" />}
        {step.durationMs !== null && (
          <span className="shrink-0 font-mono text-[10px] text-faint">{formatDuration(step.durationMs)}</span>
        )}
        <StatusDot status={step.status} />
        {hasDetail && (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            className={`size-3 shrink-0 text-faint transition-transform ${open ? "rotate-90" : ""}`}
            aria-hidden
          >
            <path d="m9 6 6 6-6 6" />
          </svg>
        )}
      </button>

      {open && (
        <div className="mt-1 mb-1.5 ml-[22px] space-y-1.5 border-l border-line pl-3">
          {Object.keys(step.args).length > 0 && (
            <pre className="max-h-52 overflow-auto rounded-md bg-surface-2 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted">
              {JSON.stringify(step.args, null, 2)}
            </pre>
          )}
          {step.output && (
            <pre className="max-h-72 overflow-auto rounded-md border border-line bg-surface px-2.5 py-2 font-mono text-[11px] leading-relaxed text-text">
              {step.output}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function PlanStep({ items }: { items: string[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="my-1.5 rounded-lg border border-line bg-surface-2/40">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        <span className="text-faint">
          <ToolIcon name="plan" />
        </span>
        <span className="text-[12px] font-medium text-muted">计划</span>
        <span className="font-mono text-[10px] text-faint">{items.length} 步</span>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          className={`ml-auto size-3 text-faint transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden
        >
          <path d="m9 6 6 6-6 6" />
        </svg>
      </button>
      {open && (
        <ol className="space-y-1 px-3 pb-2.5 pl-8 text-[12.5px] text-muted">
          {items.map((item, index) => (
            <li key={index} className="list-decimal marker:text-faint">
              {item}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

const CHANGE_LABEL: Record<string, string> = {
  created: "新增",
  modified: "修改",
  deleted: "删除",
};

const CHANGE_TONE: Record<string, string> = {
  created: "text-accent",
  modified: "text-warn",
  deleted: "text-danger",
};

function ChangeStep({ path, changeType }: { path: string; changeType: string }) {
  return (
    <div className="flex items-center gap-2 px-1.5 py-0.5">
      <span className="text-faint">
        <ToolIcon name="write" />
      </span>
      <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-text" title={path}>
        {path}
      </span>
      <span className={`shrink-0 font-mono text-[10px] ${CHANGE_TONE[changeType] ?? "text-muted"}`}>
        {CHANGE_LABEL[changeType] ?? changeType ?? "变更"}
      </span>
    </div>
  );
}

export function StepView({ step }: { step: BotStep }) {
  if (step.kind === "tool") return <ToolStepRow step={step} />;
  if (step.kind === "plan") return <PlanStep items={step.items} />;
  if (step.kind === "change") return <ChangeStep path={step.path} changeType={step.changeType} />;
  if (step.kind === "question") {
    return (
      <div className="my-1.5 rounded-lg border border-warn/30 bg-warn/[0.05] px-3 py-2">
        <p className="text-[12px] font-medium text-warn">问了你一句</p>
        <p className="mt-0.5 text-[12.5px] text-text">{step.text}</p>
        {step.answered && <p className="mt-1 font-mono text-[11px] text-muted">→ {step.answered}</p>}
      </div>
    );
  }
  if (step.kind === "approval") {
    return (
      <div className="flex items-start gap-2 px-1.5 py-1">
        <span className={step.granted === false ? "text-danger" : "text-accent"}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5" aria-hidden>
            {step.granted === false ? <path d="m6 6 12 12M18 6 6 18" /> : <path d="m5 13 4 4L19 7" />}
          </svg>
        </span>
        <span className="text-[12px] text-muted">{step.text}</span>
      </div>
    );
  }
  return (
    <p
      className={`px-1.5 py-0.5 text-[12px] ${
        step.tone === "danger" ? "text-danger" : step.tone === "warn" ? "text-warn" : "text-muted"
      }`}
    >
      {step.text}
    </p>
  );
}
