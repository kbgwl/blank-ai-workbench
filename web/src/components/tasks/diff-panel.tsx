"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Notice, StateLine } from "@/components/projects/ui";
import { clip, diffStats, parseDiff } from "./format";
import type { ChangeItem } from "./api";

const CHANGE_LABELS: Record<string, string> = {
  created: "新增",
  modified: "修改",
  deleted: "删除",
  renamed: "重命名",
};

function changeLabel(value: string): string {
  if (!value) return "";
  return CHANGE_LABELS[value.toLowerCase()] ?? value;
}

const lineTone: Record<string, string> = {
  add: "text-accent bg-accent/[0.07]",
  del: "text-danger bg-danger/[0.07]",
  hunk: "text-brand/80",
  ctx: "text-muted",
  meta: "text-faint",
};

export function DiffPanel({ changes, focusPath }: { changes: ChangeItem[]; focusPath: string | null }) {
  const files = useMemo(
    () =>
      changes.map((change) => ({
        ...change,
        lines: parseDiff(change.diff),
        stats: diffStats(change.diff),
      })),
    [changes],
  );
  const signature = files.map((file) => file.path).join("|");
  // 展开状态：null 表示跟随自动规则（只有一个文件时默认展开），切换后记录用户选择。
  const [manual, setManual] = useState<{ signature: string; paths: string[] } | null>(null);
  const auto = files.length <= 1 ? files.map((file) => file.path) : [];
  const base = manual && manual.signature === signature ? manual.paths : auto;
  const expanded = focusPath && !base.includes(focusPath) ? [...base, focusPath] : base;
  const refs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    if (!focusPath) return;
    const target = files.find((file) => file.path === focusPath || file.path.endsWith(focusPath));
    refs.current[target?.path ?? focusPath]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [focusPath, files]);

  if (files.length === 0) {
    return <StateLine>后端未返回 changes（任务尚未修改文件，或证据包缺字段）</StateLine>;
  }

  const allOpen = files.every((file) => expanded.includes(file.path));

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between font-mono text-[10px] text-faint">
        <span>{files.length} 个文件</span>
        <button
          type="button"
          onClick={() => setManual({ signature, paths: allOpen ? [] : files.map((file) => file.path) })}
          className="text-brand/80 hover:text-brand"
        >
          {allOpen ? "全部收起" : "全部展开"}
        </button>
      </div>
      {files.map((file) => {
        const open = expanded.includes(file.path);
        return (
          <div
            key={file.path}
            ref={(node) => {
              refs.current[file.path] = node;
            }}
            className={`overflow-hidden rounded-lg border ${
              file.path === focusPath ? "border-brand/40" : "border-line"
            } bg-surface/50`}
          >
            <button
              type="button"
              onClick={() =>
                setManual({
                  signature,
                  paths: open ? base.filter((path) => path !== file.path) : [...base, file.path],
                })
              }
              className="flex w-full items-center gap-2 px-2.5 py-2 text-left hover:bg-surface-2/70"
            >
              <span className={`min-w-0 flex-1 truncate font-mono text-[11px] ${open ? "text-brand" : "text-text/90"}`} title={file.path}>
                {clip(file.path, 46)}
              </span>
              {changeLabel(file.changeType) && (
                <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
                  {changeLabel(file.changeType)}
                </span>
              )}
              <span className="shrink-0 font-mono text-[10px]">
                <span className="text-accent">+{file.stats.added}</span>
                <span className="mx-1 text-faint">/</span>
                <span className="text-danger">−{file.stats.removed}</span>
              </span>
            </button>
            {open &&
              (file.lines.length === 0 || !file.diff ? (
                <div className="px-2.5 pb-2.5">
                  <Notice tone="muted">后端只返回了文件路径，没有 diff 内容</Notice>
                </div>
              ) : (
                <div className="max-h-72 overflow-auto border-t border-line bg-black/35 px-2.5 py-2 font-mono text-[11px] leading-relaxed">
                  {file.lines.map((line, index) => (
                    <div key={index} className={`whitespace-pre-wrap break-words px-1 ${lineTone[line.tone] ?? "text-muted"}`}>
                      {line.text || " "}
                    </div>
                  ))}
                </div>
              ))}
          </div>
        );
      })}
    </div>
  );
}
