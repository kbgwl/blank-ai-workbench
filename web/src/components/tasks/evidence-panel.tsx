"use client";

import { useEffect, useState } from "react";
import { Notice } from "@/components/projects/ui";
import { formatDuration, formatNumber, orNone } from "./format";
import { StatusBadge } from "./bits";
import { errorMessage, fetchTaskSnapshots, restoreTaskSnapshot, type Evidence, type TaskSnapshot } from "./api";

function Row({ label, value, tone }: { label: string; value: string; tone?: "accent" | "danger" | "warn" }) {
  const valueClass =
    tone === "accent" ? "text-accent" : tone === "danger" ? "text-danger" : tone === "warn" ? "text-warn" : "text-text/90";
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12px]">
      <span className="shrink-0 text-faint">{label}</span>
      <span className={`min-w-0 truncate text-right font-mono ${valueClass}`} title={value}>
        {value}
      </span>
    </div>
  );
}

export function EvidencePanel({ taskId, evidence, error }: { taskId: string; evidence: Evidence | null; error: string | null }) {
  const [snapshots, setSnapshots] = useState<TaskSnapshot[]>([]);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  useEffect(() => {
    if (!taskId) return;
    void fetchTaskSnapshots(taskId).then(setSnapshots).catch(() => setSnapshots([]));
  }, [taskId, evidence?.status]);

  async function restore(snapshot: TaskSnapshot) {
    if (!window.confirm("恢复此快照会覆盖工作区中的文件，确定继续吗？")) return;
    setRestoring(snapshot.id);
    setSnapshotError(null);
    try {
      await restoreTaskSnapshot(taskId, snapshot.id);
      setSnapshotError("已恢复工作区快照");
    } catch (restoreError) {
      setSnapshotError(errorMessage(restoreError));
    } finally {
      setRestoring(null);
    }
  }
  if (error) {
    return (
      <div className="flex flex-col gap-2">
        <Notice>{error}</Notice>
        <p className="text-[11px] leading-relaxed text-faint">{"交付证明包需要后端提供 GET /tasks/{id}/evidence。"}</p>
      </div>
    );
  }
  if (!evidence) {
    return <p className="font-mono text-[11px] text-faint">读取证明包…</p>;
  }

  const covered = evidence.acceptance.filter((item) => item.covered === true).length;
  const test = evidence.test;

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-muted">任务结论</span>
        <StatusBadge status={evidence.status} />
      </div>

      <section className="flex flex-col gap-1.5">
        <h4 className="flex items-center justify-between font-mono text-[10px] tracking-wide text-faint uppercase">
          <span>验收条件</span>
          {evidence.acceptance.length > 0 && (
            <span className="text-muted normal-case">
              已通过 {covered} / {evidence.acceptance.length}
            </span>
          )}
        </h4>
        {evidence.acceptance.length === 0 ? (
          <p className="text-[11px] text-faint">暂无验收条件</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {evidence.acceptance.map((item, index) => (
              <li key={`${item.criterion}-${index}`} className="flex items-start gap-2">
                <span
                  className={`mt-0.5 grid size-4 shrink-0 place-items-center rounded-full font-mono text-[9px] ${
                    item.covered === true
                      ? "bg-accent/20 text-accent"
                      : item.covered === false
                        ? "bg-danger/15 text-danger"
                        : "bg-surface-2 text-faint"
                  }`}
                >
                  {item.covered === true ? "✓" : item.covered === false ? "×" : "?"}
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className={`block text-[12px] leading-relaxed ${
                      item.covered === true ? "text-muted line-through decoration-line-strong" : "text-text/90"
                    }`}
                  >
                    {item.criterion}
                  </span>
                  {item.note && <span className="mt-0.5 block font-mono text-[10px] text-faint">{item.note}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-1.5 border-t border-line pt-3">
        <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">测试命令与结果</h4>
        {!test ? (
          <p className="text-[11px] text-faint">暂无（后端未返回 test/validation）</p>
        ) : (
          <>
            <Row label="命令" value={orNone(test.command)} />
            <Row
              label="退出码"
              value={test.exitCode === null ? "暂无" : String(test.exitCode)}
              tone={test.exitCode === null ? undefined : test.exitCode === 0 ? "accent" : "danger"}
            />
            <Row
              label="结论"
              value={test.passed === true ? "通过" : test.passed === false ? "未通过" : orNone(test.status)}
              tone={test.passed === true ? "accent" : test.passed === false ? "danger" : undefined}
            />
            {test.output ? (
              <details className="group rounded-md border border-line bg-black/30">
                <summary className="cursor-pointer list-none px-2.5 py-1.5 font-mono text-[10px] text-faint hover:text-muted">
                  输出片段（{test.output.split("\n").length} 行）
                </summary>
                <pre className="max-h-40 overflow-auto border-t border-line px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words text-muted">
                  {test.output}
                </pre>
              </details>
            ) : (
              <p className="font-mono text-[10px] text-faint">无输出内容</p>
            )}
          </>
        )}
      </section>

      <section className="flex flex-col gap-1.5 border-t border-line pt-3">
        <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">Token 用量</h4>
        <Row label="输入" value={formatNumber(evidence.usage.promptTokens)} />
        <Row label="输出" value={formatNumber(evidence.usage.completionTokens)} />
        <Row label="合计" value={formatNumber(evidence.usage.totalTokens)} tone="accent" />
        <Row label="耗时" value={evidence.usage.latencyMs === null ? "暂无" : orNone(formatDuration(evidence.usage.latencyMs))} />
        <Row label="调用次数" value={formatNumber(evidence.usage.calls)} />
        <Row label="模型" value={evidence.usage.models.length > 0 ? evidence.usage.models.join("、") : "暂无"} />
      </section>

      {snapshots.length > 0 && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">回滚点</h4>
          {snapshots.map((snapshot) => (
            <div key={snapshot.id} className="flex items-center justify-between gap-2 rounded-md border border-line bg-black/20 px-2.5 py-2">
              <span className="min-w-0 truncate text-[11px] text-muted" title={snapshot.reason}>{snapshot.reason}</span>
              <button type="button" disabled={restoring !== null} onClick={() => void restore(snapshot)} className="shrink-0 rounded border border-line px-2 py-1 text-[10px] text-muted hover:bg-surface-2 hover:text-text disabled:opacity-50">
                {restoring === snapshot.id ? "恢复中" : "恢复"}
              </button>
            </div>
          ))}
          {snapshotError && <p className="text-[11px] text-muted">{snapshotError}</p>}
        </section>
      )}

      {evidence.unverified.length > 0 && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-warn uppercase">仍需关注</h4>
          <ul className="flex flex-col gap-1">
            {evidence.unverified.map((item, index) => (
              <li key={index} className="text-[11px] leading-relaxed text-warn">
                · {item}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
