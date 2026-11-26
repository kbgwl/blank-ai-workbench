"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeItem } from "../tasks/api";
import { diffStats, langOf, parseDiff } from "../tasks/format";
import { TOKEN_CLASSES, tokenize } from "./syntax";

const CHANGE_LABELS: Record<string, string> = {
  created: "新增",
  modified: "修改",
  deleted: "删除",
  renamed: "重命名",
};

const LINE_TONES: Record<string, string> = {
  add: "text-accent bg-accent/[0.07]",
  del: "text-danger bg-danger/[0.07]",
  hunk: "text-brand/80",
  ctx: "text-muted",
  meta: "text-faint",
};

function changeLabel(value: string): string {
  if (!value) return "修改";
  return CHANGE_LABELS[value.toLowerCase()] ?? value;
}

function pathParts(path: string): { name: string; dir: string } {
  const index = path.lastIndexOf("/");
  if (index < 0) return { name: path, dir: "" };
  return { name: path.slice(index + 1), dir: path.slice(0, index) };
}

export function CodeChangeView({
  changes,
  scopeLabel,
  treeMode = false,
}: {
  changes: ChangeItem[];
  scopeLabel: string;
  treeMode?: boolean;
}) {
  const files = useMemo(
    () =>
      changes.map((change) => ({
        ...change,
        lines: parseDiff(change.diff),
        stats: diffStats(change.diff),
      })),
    [changes],
  );
  const [selected, setSelected] = useState<string | null>(null);
  const active = files.find((file) => file.path === selected) ?? files[0] ?? null;
  const groups = useMemo(() => {
    const map = new Map<string, typeof files>();
    for (const file of files) {
      const dir = pathParts(file.path).dir || "(根目录)";
      const group = map.get(dir) ?? [];
      group.push(file);
      map.set(dir, group);
    }
    return [...map.entries()];
  }, [files]);

  // 文件条改成横向：文件多时靠左右按钮滚动，而不是把 diff 挤到左边一条缝里。
  const stripRef = useRef<HTMLDivElement | null>(null);
  const chipRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);
  const updateArrows = useCallback(() => {
    const node = stripRef.current;
    if (!node) return;
    setCanLeft(node.scrollLeft > 4);
    setCanRight(node.scrollLeft + node.clientWidth < node.scrollWidth - 4);
  }, []);
  useEffect(() => {
    updateArrows();
    const node = stripRef.current;
    if (!node) return;
    const observer = new ResizeObserver(updateArrows);
    observer.observe(node);
    return () => observer.disconnect();
  }, [updateArrows, files.length]);
  const scrollStrip = (direction: number) => {
    stripRef.current?.scrollBy({ left: direction * 240, behavior: "smooth" });
  };
  // 从产物/文件标签点进来时，把选中的文件滚进可视区；否则用户只看到 diff 变了，
  // 不知道是哪一条被选中。
  useEffect(() => {
    if (!active) return;
    chipRefs.current[active.path]?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
    const timer = window.setTimeout(updateArrows, 300);
    return () => window.clearTimeout(timer);
  }, [active, updateArrows]);

  const renderChip = (file: (typeof files)[number]) => {
    const parts = pathParts(file.path);
    const activeFile = file.path === active?.path;
    return (
      <button
        key={file.path}
        type="button"
        ref={(node) => {
          chipRefs.current[file.path] = node;
        }}
        onClick={() => setSelected(file.path)}
        title={file.path}
        className={`flex h-8 max-w-[230px] shrink-0 items-center gap-1.5 rounded-md border px-2 text-left transition-colors ${
          activeFile
            ? "border-brand/50 bg-brand/[0.08]"
            : "border-line bg-surface hover:border-line-strong hover:bg-surface-hover"
        }`}
      >
        <span className={`max-w-[150px] truncate font-mono text-[11px] ${activeFile ? "text-brand" : "text-text"}`}>
          {parts.name}
        </span>
        <span className="shrink-0 font-mono text-[9px]">
          <span className="text-accent">+{file.stats.added}</span>
          <span className="mx-0.5 text-faint">/</span>
          <span className="text-danger">−{file.stats.removed}</span>
        </span>
      </button>
    );
  };

  if (!active) {
    return (
      <div className="grid min-h-[320px] flex-1 place-items-center rounded-[var(--radius-md)] border border-dashed border-line bg-surface">
        <div className="max-w-[220px] text-center">
          <div className="mx-auto grid size-9 place-items-center rounded-md bg-surface-2 text-faint">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6" />
            </svg>
          </div>
          <p className="mt-2 text-[12px] font-medium text-text">这里会出现代码改动</p>
          <p className="mt-1 text-[11px] leading-relaxed text-faint">
            Agent 写入文件后，上方列出文件，下方显示对应 diff。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-[360px] flex-1 flex-col overflow-hidden rounded-[var(--radius-md)] border border-line bg-surface shadow-[var(--shadow-sm)]">
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-line bg-surface-2 px-2.5">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold tracking-[0.08em] text-muted uppercase">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5 text-brand" aria-hidden>
            <path d="m8 9-4 3 4 3M16 9l4 3-4 3M14 5l-4 14" />
          </svg>
          代码改动
        </span>
        <span className="rounded bg-surface px-1.5 py-0.5 font-mono text-[10px] text-faint">
          {scopeLabel} · {files.length} 文件
        </span>
      </div>

      {/* 文件条：横排、可滚动；溢出时出现左右按钮（用户只需要一个能往回点的入口）。 */}
      <div className="flex shrink-0 items-center gap-0.5 border-b border-line bg-surface-2/50 px-1 py-1">
        {canLeft && (
          <button
            type="button"
            aria-label="向左查看更多文件"
            title="向左查看更多文件"
            onClick={() => scrollStrip(-1)}
            className="grid size-6 shrink-0 place-items-center rounded text-faint transition-colors hover:bg-surface hover:text-text"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="size-3.5" aria-hidden>
              <path d="m15 6-6 6 6 6" />
            </svg>
          </button>
        )}
        <div
          ref={stripRef}
          data-file-strip
          onScroll={updateArrows}
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto py-0.5"
        >
          {treeMode
            ? groups.map(([dir, group]) => (
                <div key={dir} className="flex shrink-0 items-center gap-1">
                  <span
                    className="shrink-0 pl-1 pr-0.5 text-[9px] font-semibold tracking-[0.08em] text-faint uppercase"
                    title={dir}
                  >
                    {dir === "(根目录)" ? "根目录" : dir}
                  </span>
                  {group.map(renderChip)}
                </div>
              ))
            : files.map(renderChip)}
        </div>
        {canRight && (
          <button
            type="button"
            aria-label="向右查看更多文件"
            title="向右查看更多文件"
            onClick={() => scrollStrip(1)}
            className="grid size-6 shrink-0 place-items-center rounded text-faint transition-colors hover:bg-surface hover:text-text"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="size-3.5" aria-hidden>
              <path d="m9 6 6 6-6 6" />
            </svg>
          </button>
        )}
      </div>

      <div data-diff-scroll className="min-w-0 flex-1 overflow-auto">
        <div className="sticky top-0 left-0 z-10 flex items-center gap-2 border-b border-line bg-surface px-2.5 py-2">
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-text" title={active.path}>
            {active.path}
          </span>
          <span className="shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[9px] text-muted">
            {changeLabel(active.changeType)}
          </span>
        </div>
        <div className="w-max min-w-full font-mono text-[11px] leading-[1.65]">
          {active.lines.length === 0 || !active.diff ? (
            <p className="px-2.5 py-3 text-[11px] text-faint">这一轮只记录了文件路径，没有留下 diff。</p>
          ) : (
            active.lines.map((line, index) => {
              const tokens = line.text ? tokenize(line.text, langOf(active.path))[0] ?? [] : [];
              return (
                <div
                  key={index}
                  className={`w-full whitespace-pre px-1 ${LINE_TONES[line.tone] ?? "text-muted"}`}
                >
                  {tokens.length > 0
                    ? tokens.map((token, tokenIndex) => (
                        <span key={tokenIndex} className={TOKEN_CLASSES[token.kind] ?? ""}>
                          {token.text}
                        </span>
                      ))
                    : line.text || " "}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
