"use client";

import { Button, Notice, StateLine } from "@/components/projects/ui";
import type { UsageSummary } from "./api";
import { StatTile } from "./bits";

type Props = {
  summary: UsageSummary | null;
  error: string | null;
  onRetry: () => void;
};

export function UsagePanel({ summary, error, onRetry }: Props) {
  if (error) {
    return (
      <div className="flex flex-col gap-2">
        <Notice>用量读取失败：{error}</Notice>
        <Button className="self-start" onClick={onRetry}>
          重试
        </Button>
      </div>
    );
  }
  if (summary === null) return <StateLine>载入用量…</StateLine>;

  const rows = [...summary.providers].sort((a, b) => b.calls - a.calls);
  const failed = rows.reduce((sum, row) => sum + row.failed, 0);
  const prompt = rows.reduce((sum, row) => sum + row.prompt_tokens, 0);
  const completion = rows.reduce((sum, row) => sum + row.completion_tokens, 0);
  const cost = summary.total_cost?.amount ?? rows.reduce((sum, row) => sum + (row.cost?.amount ?? 0), 0);
  const currency = summary.total_cost?.currency ?? "USD";
  const successRate =
    summary.success_rate ?? (summary.total_calls ? (summary.total_calls - failed) / summary.total_calls : 0);

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <StatTile label="总调用" value={String(summary.total_calls)} />
        <StatTile
          label="成功率"
          value={`${Math.round(successRate * 100)}%`}
          tone={successRate === 1 ? "text" : "warn"}
        />
        <StatTile label="失败" value={String(failed)} tone={failed > 0 ? "warn" : "text"} />
        <StatTile
          label="TOKENS 输入/输出"
          value={`${prompt.toLocaleString("en-US")} / ${completion.toLocaleString("en-US")}`}
        />
        <StatTile
          label={`估算成本 (${currency})`}
          value={cost > 0 ? cost.toFixed(5) : "0"}
        />
      </div>

      {rows.length === 0 ? (
        <StateLine>暂无调用记录 · 点供应商卡片上的「测试连接」即会产生用量</StateLine>
      ) : (
        <div className="overflow-hidden rounded-lg border border-line">
          <div className="grid grid-cols-[minmax(0,1.6fr)_repeat(5,minmax(0,1fr))] gap-2 border-b border-line bg-surface-2/70 px-3 py-2 font-mono text-[10px] tracking-wide text-faint uppercase">
            <span>供应商 / 模型</span>
            <span className="text-right">调用</span>
            <span className="text-right">失败</span>
            <span className="text-right">TOKENS</span>
            <span className="text-right">平均耗时</span>
            <span className="text-right">成本</span>
          </div>
          <ul className="divide-y divide-line">
            {rows.map((row) => {
              const avg = row.avg_latency_ms ?? (row.calls > 0 ? Math.round(row.total_latency_ms / row.calls) : 0);
              return (
                <li
                  key={`${row.provider}/${row.model}`}
                  className="grid grid-cols-[minmax(0,1.6fr)_repeat(5,minmax(0,1fr))] gap-2 px-3 py-2 text-[12px]"
                >
                  <span className="min-w-0">
                    <span className="block truncate text-text">{row.provider}</span>
                    <span className="block truncate font-mono text-[10px] text-faint">{row.model}</span>
                  </span>
                  <span className="text-right font-mono text-muted">{row.calls}</span>
                  <span className={`text-right font-mono ${row.failed > 0 ? "text-warn" : "text-faint"}`}>
                    {row.failed}
                  </span>
                  <span className="text-right font-mono text-muted">
                    {row.prompt_tokens}
                    <span className="text-faint"> / </span>
                    {row.completion_tokens}
                  </span>
                  <span className="text-right font-mono text-muted">{avg}ms</span>
                  <span className="text-right font-mono text-muted">
                    {row.cost && row.cost.priced ? row.cost.amount.toFixed(6) : "未定价"}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
