"use client";

import { useState } from "react";
import type { ChangeItem, UsageInfo } from "../tasks/api";
import { formatDuration } from "../tasks/format";
import type { BotStep } from "./turns";
import { turnEvidence } from "./turns";

function DiffText({ diff }: { diff: string }) {
  const lines = diff.split("\n").slice(0, 400);
  return (
    <pre className="max-h-80 overflow-auto rounded-md border border-line bg-surface font-mono text-[11px] leading-[1.55]">
      {lines.map((line, index) => {
        const tone = line.startsWith("+++") || line.startsWith("---")
          ? "text-faint"
          : line.startsWith("@@")
            ? "text-brand bg-brand/[0.05]"
            : line.startsWith("+")
              ? "text-accent bg-accent/[0.06]"
              : line.startsWith("-")
                ? "text-danger bg-danger/[0.05]"
                : "text-muted";
        return (
          <div key={index} className={`px-2 whitespace-pre-wrap ${tone}`}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

const CHANGE_TONE: Record<string, string> = {
  created: "text-accent",
  modified: "text-warn",
  deleted: "text-danger",
};
const CHANGE_LABEL: Record<string, string> = { created: "新增", modified: "修改", deleted: "删除" };

/**
 * 同一个文件改多次只留一行。
 *
 * 后端的改动记录按"每次写入"记一条，一个文件被改四轮就有四条。把四条原样铺出来，
 * 右键面板显示的数是 11，而对话里依据条按去重算是 6——两处数字对不上，用户没有理由
 * 相信任何一个；更实际的问题是同一个文件名重复四遍，点哪一条都像是点错了。
 */
function uniqueChanges(rows: ChangeItem[]): ChangeItem[] {
  const merged = new Map<string, ChangeItem>();
  for (const row of rows) {
    const existing = merged.get(row.path);
    if (!existing) {
      merged.set(row.path, row);
      continue;
    }
    // 先建的记录说 created，后面又改过：对用户来说它就是"这一轮动过的文件"。
    // 差异保留第一次出现的类型（新增仍是新增），diff 取第一个非空的。
    merged.set(row.path, { ...existing, diff: existing.diff || row.diff });
  }
  return [...merged.values()];
}

export type TaskPanelProps = {
  title: string;
  status: string;
  running: boolean;
  steps: BotStep[];
  changes: ChangeItem[];
  usage: UsageInfo | null;
  onClose: () => void;
};

/**
 * 右侧面板：只回答"这一轮到底动了什么"。
 *
 * 刻意不把过程再抄一遍——过程在对话里已经按发生顺序展开了，在这里再列一次
 * 会让人分不清哪份是实时的。这里给的是**结果**：改了哪些文件、花了多少。
 */
export function TaskPanel(props: TaskPanelProps) {
  const [openPath, setOpenPath] = useState<string | null>(null);
  // 和对话里那条依据条用同一个函数算。两处数字必须同源——左边说改了 3 个文件、
  // 右边说 4 个，用户就没有理由相信任何一个。耗时优先用模型侧统计，缺了回落到
  // 工具耗时之和（离线跑、老事件里没有 usage 时就是这种情形）。
  const evidence = turnEvidence(props.steps);
  const files = props.changes.length > 0
    ? uniqueChanges(props.changes)
    : props.steps
        .filter((step): step is Extract<BotStep, { kind: "change" }> => step.kind === "change")
        .reduce<ChangeItem[]>((rows, step) => {
          if (!rows.some((row) => row.path === step.path)) rows.push({ path: step.path, diff: "", changeType: step.changeType });
          return rows;
        }, []);
  const latency = props.usage?.latencyMs || evidence.durationMs;

  return (
    <aside className="flex w-[320px] shrink-0 flex-col border-l border-line bg-canvas">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[12.5px] font-medium">最新一轮</span>
        <span
          className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
            props.running ? "bg-brand/[0.08] text-brand" : props.status === "failed" ? "bg-danger/[0.08] text-danger" : "bg-surface-2 text-muted"
          }`}
        >
          {props.running ? "进行中" : props.status === "failed" ? "失败" : props.status === "cancelled" ? "已停止" : "完成"}
        </span>
        <button
          type="button"
          onClick={props.onClose}
          title="收起面板"
          className="ml-auto grid size-6 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5" aria-hidden>
            <path d="m14 6-6 6 6 6" />
          </svg>
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        <section>
          <p className="mb-1.5 truncate text-[12px] text-muted" title={props.title}>
            {props.title || "未命名对话"}
          </p>
          <dl className="grid grid-cols-2 gap-1.5">
            <div className="rounded-lg bg-surface px-2.5 py-1.5">
              <dt className="font-mono text-[9.5px] tracking-wide text-faint uppercase">工具调用</dt>
              <dd className="font-mono text-[13px] text-text">
                {evidence.calls}
                {evidence.failed > 0 && <span className="ml-1 text-[11px] text-danger">（{evidence.failed} 失败）</span>}
              </dd>
            </div>
            <div className="rounded-lg bg-surface px-2.5 py-1.5">
              <dt className="font-mono text-[9.5px] tracking-wide text-faint uppercase">改动文件</dt>
              <dd className="font-mono text-[13px] text-text">
                {files.length}
                {evidence.commandCount > 0 && (
                  <span className="ml-1 text-[10.5px] text-faint">/ {evidence.commandCount} 条命令</span>
                )}
              </dd>
            </div>
            <div className="rounded-lg bg-surface px-2.5 py-1.5">
              <dt className="font-mono text-[9.5px] tracking-wide text-faint uppercase">耗时</dt>
              <dd className="font-mono text-[13px] text-text">{latency > 0 ? formatDuration(latency) : "—"}</dd>
            </div>
            <div className="rounded-lg bg-surface px-2.5 py-1.5">
              <dt className="font-mono text-[9.5px] tracking-wide text-faint uppercase">Token</dt>
              <dd className="font-mono text-[13px] text-text">
                {props.usage?.totalTokens ? props.usage.totalTokens.toLocaleString() : "—"}
              </dd>
            </div>
          </dl>
        </section>

        <section>
          <p className="mb-1.5 font-mono text-[10px] tracking-wide text-faint uppercase">改动的文件</p>
          {files.length === 0 && <p className="px-1 py-2 text-[11.5px] text-faint">这一轮没有改文件。</p>}
          <div className="space-y-1">
            {files.map((file) => {
              const open = openPath === file.path;
              return (
                <div key={file.path} className="overflow-hidden rounded-lg border border-line bg-surface">
                  {/* 没有 diff 也要能点开：以前这里 disabled，行看着是按钮，
                      点下去一点反应都没有，用户只会怀疑整个面板坏了。
                      现在点开至少说明"这一轮没有记录 diff"。 */}
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setOpenPath(open ? null : file.path)}
                    title={file.diff ? "展开看改动" : "这一轮没有记录 diff"}
                    className="flex w-full items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
                  >
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-text" title={file.path}>
                      {file.path}
                    </span>
                    <span className={`shrink-0 font-mono text-[9.5px] ${CHANGE_TONE[file.changeType] ?? "text-muted"}`}>
                      {CHANGE_LABEL[file.changeType] ?? file.changeType}
                    </span>
                  </button>
                  {open && (
                    <div className="border-t border-line p-1.5">
                      {file.diff ? (
                        <DiffText diff={file.diff} />
                      ) : (
                        <p className="px-1 py-1 text-[11px] text-faint">
                          这一轮只记录了文件路径，没有留下 diff（历史轮次或改动未落盘）。
                        </p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      </div>
    </aside>
  );
}
