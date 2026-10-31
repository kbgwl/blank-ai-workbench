"use client";

import { Button, Notice, StateLine } from "@/components/projects/ui";
import type { Ledger, LedgerTaskRow } from "./api";
import { StatTile } from "./bits";

type Props = {
  ledger: Ledger | null;
  error: string | null;
  days: number;
  onDays: (days: number) => void;
  onRetry: () => void;
};

const BUCKETS = [
  { key: "delivered", label: "换成了产物", className: "bg-brand" },
  { key: "advised", label: "换成了判断", className: "bg-muted" },
  { key: "wasted", label: "变成了废纸", className: "bg-danger" },
  { key: "running", label: "还没结算", className: "bg-line-strong" },
] as const;

const STATUS_LABEL: Record<string, string> = {
  succeeded: "完成",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
  error: "出错",
  running: "进行中",
  waiting_approval: "等审批",
};

/** 数字只在跨量级时有意义：一万五千写成 15K 比写全更容易比较。 */
function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function OffenderRow({ row }: { row: LedgerTaskRow }) {
  return (
    <li className="flex items-center gap-2 px-3 py-2 text-[12px]">
      <span className="min-w-0 flex-1">
        <span className="block truncate text-text">{row.title || "未命名任务"}</span>
        <span className="block truncate font-mono text-[10px] text-faint">
          {STATUS_LABEL[row.status] ?? row.status} · {row.calls} 次调用 · {row.tools} 个工具动作
          {row.failed_tools > 0 && ` · ${row.failed_tools} 失败`}
          {row.blocked_calls > 0 && ` · 拦掉 ${row.blocked_calls} 次重复`}
        </span>
      </span>
      {row.files > 0 && (
        <span className="shrink-0 font-mono text-[10.5px] text-faint">{row.files} 文件</span>
      )}
      <span className="shrink-0 font-mono text-[12px] text-text">{compact(row.tokens)}</span>
    </li>
  );
}

/**
 * 用量台账：不回答「谁在花钱」，回答「钱花得值不值」。
 *
 * 只看总额没有意义——九百万 token 里可能有七百万出自一个跑废的任务。所以这里
 * 先把钱按去向拆开（产物 / 判断 / 废纸），再给四个判「贵不贵」的口径。
 */
export function LedgerPanel({ ledger, error, days, onDays, onRetry }: Props) {
  if (error) {
    return (
      <div className="flex flex-col gap-2">
        <Notice>台账读取失败：{error}</Notice>
        <Button className="self-start" onClick={onRetry}>
          重试
        </Button>
      </div>
    );
  }
  if (ledger === null) return <StateLine>载入台账…</StateLine>;

  const { summary } = ledger;
  if (summary.tasks === 0) {
    return (
      <div className="flex flex-col gap-2">
        <StateLine>这段时间还没有任务记录。跑一个任务就会出现在这里。</StateLine>
        <div className="flex gap-1">
          {[7, 30, 90].map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onDays(value)}
              className={`rounded-md border px-2 py-1 text-[11px] transition-colors ${
                days === value
                  ? "border-brand/50 bg-brand/[0.06] text-brand"
                  : "border-line bg-surface text-muted hover:text-text"
              }`}
            >
              近 {value} 天
            </button>
          ))}
        </div>
      </div>
    );
  }

  const buckets = BUCKETS.map((item) => ({ ...item, ...summary.buckets[item.key] })).filter(
    (item) => item.tokens > 0,
  );
  const spinBad = summary.calls_per_tool > 2;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="grid flex-1 grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <StatTile
            label="有效产出"
            value={percent(summary.effective_share)}
            tone={summary.effective_share >= 0.5 ? "text" : "warn"}
          />
          <StatTile
            label="每文件 TOKEN"
            value={summary.tokens_per_file ? compact(summary.tokens_per_file) : "—"}
          />
          <StatTile label="单次上下文" value={summary.per_call_context ? compact(summary.per_call_context) : "—"} />
          <StatTile
            label="调用/工具"
            value={summary.calls_per_tool ? summary.calls_per_tool.toFixed(1) : "—"}
            tone={spinBad ? "warn" : "text"}
          />
          <StatTile label="缓存命中" value={percent(summary.cache_hit)} />
          {/* 拦截数是"没白花"的直接证据：这些调用没有执行，也没有把整段上下文再发一遍。
              它和失败率是两件事——失败率说错了多少，拦截数说错了之后有没有继续错下去。 */}
          <StatTile
            label="空转拦截"
            value={summary.blocked_calls ? String(summary.blocked_calls) : "0"}
            tone="text"
          />
        </div>
      </div>

      <div className="flex gap-1">
        {[7, 30, 90].map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => onDays(value)}
            className={`rounded-md border px-2 py-1 text-[11px] transition-colors ${
              days === value
                ? "border-brand/50 bg-brand/[0.06] text-brand"
                : "border-line bg-surface text-muted hover:text-text"
            }`}
          >
            近 {value} 天
          </button>
        ))}
        <span className="ml-auto self-center font-mono text-[10.5px] text-faint">
          共 {summary.tasks} 个任务 · {compact(summary.total_tokens)} TOKEN
        </span>
      </div>

      <section>
        <p className="mb-1.5 font-mono text-[10px] tracking-wide text-faint uppercase">这些钱换成了什么</p>
        <div className="flex h-2.5 overflow-hidden rounded-full bg-surface-2">
          {buckets.map((item) => (
            <span
              key={item.key}
              title={`${item.label}：${compact(item.tokens)}（${percent(item.share)}）`}
              className={item.className}
              style={{ width: `${Math.max(item.share * 100, item.tokens > 0 ? 1 : 0)}%` }}
            />
          ))}
        </div>
        <ul className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-4">
          {buckets.map((item) => (
            <li key={item.key} className="rounded-lg bg-surface px-2.5 py-1.5">
              <p className="flex items-center gap-1.5 text-[11px] text-muted">
                <span className={`size-1.5 rounded-full ${item.className}`} />
                {item.label}
              </p>
              <p className="font-mono text-[12.5px] text-text">
                {compact(item.tokens)}
                <span className="ml-1 text-[10.5px] text-faint">{percent(item.share)}</span>
              </p>
            </li>
          ))}
        </ul>
      </section>

      {ledger.offenders.length > 0 && (
        <section>
          <p className="mb-1.5 font-mono text-[10px] tracking-wide text-faint uppercase">花得最多的几个</p>
          <div className="overflow-hidden rounded-lg border border-line">
            <ul className="divide-y divide-line">
              {ledger.offenders.map((row) => (
                <OffenderRow key={row.id} row={row} />
              ))}
            </ul>
          </div>
          {spinBad && (
            <p className="mt-1.5 text-[11px] leading-relaxed text-warn">
              每个工具动作平均伴随着 {summary.calls_per_tool.toFixed(1)} 次模型调用。
              高于 2 通常说明在反复审查或重试——每一步都会把整段上下文重发一遍。
              并行批处理会让这个数偏低，所以只看上限。
            </p>
          )}
          {summary.blocked_calls > 0 && (
            <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
              这一段里有 {summary.blocked_calls} 次调用被拦在重复上：参数与前面失败过的完全相同，
              挂起审批后又重试，或者用到了一条这个环境里没有的命令。它们没有执行，也没有再发一遍上下文。
            </p>
          )}
        </section>
      )}
    </div>
  );
}
