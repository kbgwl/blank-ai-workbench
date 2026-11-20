"use client";

import type { BackgroundRow } from "./background";

/**
 * 后台任务清单。
 *
 * 顶部那一格写「后台 3 个」，点它只跳到其中一条——另外两条既看不见也点不到。
 * 三条同时在跑的时候，用户只能去任务列表里挨个翻；而"哪条在跑、跑了多久、
 * 卡在哪"正是他判断"要不要等"的全部依据。
 *
 * 默认收起：常驻三行会一直占着对话顶部，而绝大多数时候只有一条在跑。
 * 展开后每行给三样东西——在做什么、到哪一步了、能不能停。
 */
export function BackgroundList({
  rows,
  onOpen,
  onStop,
  busy,
}: {
  rows: BackgroundRow[];
  onOpen: (row: BackgroundRow) => void;
  onStop: (row: BackgroundRow) => void;
  busy: boolean;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="shrink-0 border-b border-line bg-surface/60 px-3 py-1.5">
      <ul className="flex flex-col">
        {rows.map((row) => (
          <li key={row.id} className="flex items-center gap-2 py-1 text-[12px]">
            <span
              className={`size-1.5 shrink-0 rounded-full ${
                row.waiting ? "bg-warn" : row.status === "running" ? "bg-brand" : "bg-faint"
              }`}
            />
            <button
              type="button"
              onClick={() => onOpen(row)}
              title={row.title}
              className="min-w-0 flex-1 truncate text-left text-muted transition-colors hover:text-text"
            >
              {row.title}
            </button>
            {/* 等待和运行是两件事：一个在等人，一个在干活。同一句「12 分钟」放在
                这两种状态上意思完全不同，所以动词必须跟着状态变。 */}
            {row.elapsed && <span className="shrink-0 font-mono text-[11px] text-faint">{row.elapsed}</span>}
            {row.waiting && <span className="shrink-0 text-[11px] text-warn">等你确认</span>}
            {row.stoppable && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onStop(row)}
                className="shrink-0 rounded border border-line px-1.5 py-0.5 text-[11px] text-faint transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
              >
                停止
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
