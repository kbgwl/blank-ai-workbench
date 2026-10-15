"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { Notice, Spinner } from "@/components/projects/ui";
import { isTerminalStatus, type AgentEvent, type Task } from "./api";
import { LiveBadge, StatusBadge } from "./bits";
import { formatDateTime, relativeTime } from "./format";
import { buildBlocks, type Block } from "./timeline";
import { TimelineBlock } from "./timeline-blocks";
import type { LiveMode } from "./use-task-events";

type Props = {
  task: Task | null;
  detailError: string | null;
  events: AgentEvent[];
  mode: LiveMode;
  notice: string | null;
  actionError: string | null;
  busyAction: string | null;
  approvalKey: string | null;
  onHighlight: (path: string) => void;
  onAction: (action: "run" | "cancel" | "approve" | "deny", block?: Extract<Block, { kind: "approval" }>) => void;
  onReload: () => void;
};

const FILTERS: { key: Block["kind"] | "all"; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "plan", label: "计划" },
  { key: "tool", label: "工具调用" },
  { key: "terminal", label: "终端" },
  { key: "approval", label: "审批" },
  { key: "result", label: "结果" },
];

export function TaskConsole({
  task,
  detailError,
  events,
  mode,
  notice,
  actionError,
  busyAction,
  approvalKey,
  onHighlight,
  onAction,
  onReload,
}: Props) {
  const [filter, setFilter] = useState<Block["kind"] | "all">("all");
  // cacheComponents 下预渲染不允许 Date.now()，时间在 effect 里取。
  const [now, setNow] = useState(0);

  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const id = setInterval(tick, 20000);
    return () => clearInterval(id);
  }, []);

  // TaskOut 只有 provider_id，模型名从 task.started 事件里取，避免把 UUID 直接甩在控制台上。
  const providerLabel = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      const name = (event.payload as { provider?: string } | null)?.provider;
      if (event.type === "task.started" && name) return name;
    }
    return null;
  }, [events]);

  if (!task) {
    return (
      <Panel title="任务详情" hint="未选择任务">
        <div className="flex flex-col gap-3">
          <Notice tone={detailError ? "error" : "muted"}>
            {detailError ?? "在左侧新建任务或从任务列表选择一条，执行过程会显示在这里。"}
          </Notice>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onReload}
              className="rounded-md bg-surface-2 px-3 py-1.5 text-[12px] text-muted ring-1 ring-line hover:text-text hover:ring-line-strong"
            >
              重新读取
            </button>
            <Link href="/projects" className="text-[12px] text-brand hover:underline">
              去创建项目与工作区
            </Link>
          </div>
        </div>
      </Panel>
    );
  }

  const blocks = buildBlocks(events);
  const shown = filter === "all" ? blocks : blocks.filter((block) => block.kind === filter);
  const terminal = isTerminalStatus(task.status);
  const canRun = terminal || task.status === "draft" || task.status === "paused";
  const canCancel = !terminal;
  const runLabel = terminal
    ? "重试"
    : task.status === "draft"
      ? "运行"
      : task.status === "paused"
        ? "继续"
        : task.status === "waiting_approval"
          ? "批准并继续"
          : "重新运行";

  return (
    <Panel
      title={task.title}
      hint={`${events.length} 条事件${task.updatedAt && now > 0 ? ` · ${relativeTime(task.updatedAt, now)}` : ""}`}
    >
      <div className="flex flex-col gap-4">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line pb-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-[15px] font-semibold tracking-tight">{task.title}</span>
              <StatusBadge status={task.status} />
              <LiveBadge mode={mode} />
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-faint">
              <span title={task.id}>任务 {task.id.slice(0, 8)}</span>
              <span title={task.projectId}>项目 {task.projectId ? task.projectId.slice(0, 8) : "未绑定"}</span>
              <span>
                模型 {task.providerName || providerLabel || (task.providerId ? `#${task.providerId.slice(0, 8)}` : "默认")}
              </span>
              {task.stepsUsed !== null && (
                <span>
                  步数 {task.stepsUsed}
                  {task.maxSteps !== null ? ` / ${task.maxSteps}` : ""}
                </span>
              )}
              {task.updatedAt && <span>{formatDateTime(task.updatedAt)}</span>}
              <Link href={`/tasks/${task.id}`} className="text-brand/80 hover:text-brand">
                深链
              </Link>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busyAction !== null}
              onClick={() => onAction("run")}
              title={terminal ? "重新运行该任务" : "继续执行任务"}
              className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                canRun
                  ? "bg-brand text-bg hover:bg-brand/85"
                  : "bg-surface-2 text-muted ring-1 ring-line hover:text-text hover:ring-line-strong"
              }`}
            >
              {busyAction === "run" && <Spinner />}
              {runLabel}
            </button>
            <button
              type="button"
              disabled={busyAction !== null || !canCancel}
              onClick={() => onAction("cancel")}
              className="inline-flex items-center gap-1.5 rounded-md bg-danger/10 px-3 py-1.5 text-[12px] font-medium text-danger ring-1 ring-danger/30 transition-colors hover:bg-danger/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busyAction === "cancel" && <Spinner />}
              取消
            </button>
          </div>
        </header>

        <div className="rounded-xl border border-line bg-surface-2/50 px-3.5 py-3">
          <div className="mb-1 font-mono text-[10px] tracking-wide text-faint uppercase">任务描述</div>
          <p className="text-[13px] leading-relaxed whitespace-pre-wrap text-text/90">{task.prompt || "后端未返回任务描述"}</p>
          {task.acceptanceCriteria.length > 0 && (
            <>
              <div className="mt-2.5 mb-1 font-mono text-[10px] tracking-wide text-faint uppercase">验收条件</div>
              <ul className="flex flex-col gap-1">
                {task.acceptanceCriteria.map((item, index) => (
                  <li key={`${item}-${index}`} className="flex items-start gap-2 text-[12px] leading-relaxed">
                    <span className="mt-1 size-1.5 shrink-0 rounded-full bg-line-strong" />
                    <span className="text-muted">{item}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((item) => {
            const count = item.key === "all" ? blocks.length : blocks.filter((block) => block.kind === item.key).length;
            const active = filter === item.key;
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => setFilter(item.key)}
                className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                  active
                    ? "border-brand/40 bg-brand/10 text-brand"
                    : "border-line bg-surface-2 text-muted hover:text-text hover:ring-line-strong"
                }`}
              >
                {item.label}
                <span className="ml-1 font-mono text-[10px] text-faint">{count}</span>
              </button>
            );
          })}
        </div>

        {notice && <Notice tone="warn">{notice}</Notice>}
        {actionError && <Notice>{actionError}</Notice>}

        {shown.length === 0 ? (
          <EmptyTimeline blocks={blocks.length} events={events.length} />
        ) : (
          <div className="flex flex-col gap-2.5">
            {shown.map((block) => (
              <TimelineBlock
                key={block.key}
                block={block}
                busyApprovalKey={approvalKey}
                onHighlight={onHighlight}
                onDecide={(decision, target) => onDecideAction(onAction, decision, target)}
              />
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}

function onDecideAction(
  onAction: Props["onAction"],
  decision: "approve" | "deny",
  target: Extract<Block, { kind: "approval" }>,
) {
  onAction(decision === "approve" ? "approve" : "deny", target);
}

function EmptyTimeline({ blocks, events }: { blocks: number; events: number }) {
  if (events === 0) {
    return (
      <div className="flex flex-col gap-2 px-1 py-8 text-center font-mono text-[11px] text-faint">
        <p>还没有执行事件</p>
        <p>点上方「运行」让 Agent 开始工作，新事件会自动出现在这里</p>
      </div>
    );
  }
  return (
    <p className="px-1 py-8 text-center font-mono text-[11px] text-faint">
      当前筛选没有区块（共 {events} 条事件、{blocks} 个区块）
    </p>
  );
}
