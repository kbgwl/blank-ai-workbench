"use client";

import { useState } from "react";
import { Notice } from "@/components/projects/ui";
import type { ThreadDetail } from "./api";
import { StatusBadge } from "./bits";
import { clip, relativeTime } from "./format";

type Busy = null | "continue" | "fork" | "archive";

type Props = {
  thread: ThreadDetail | null;
  error: string | null;
  /** 当前打开的那一轮，用来高亮；fork 之后还没开轮时为空。 */
  currentTaskId: string | null;
  busy: Busy;
  onOpenTurn: (taskId: string) => void;
  onContinue: (prompt: string) => void;
  onFork: () => void;
  onArchive: () => void;
};

/**
 * 会话条：一条会话里发生过哪几轮，按顺序排开。
 *
 * 这里刻意不做"这一轮说了什么"——一轮的全文已经在下面的任务控制台里，
 * 会话条只回答"我们聊到第几轮了、现在看的是哪一轮"。
 */
export function ThreadBar({ thread, error, currentTaskId, busy, onOpenTurn, onContinue, onFork, onArchive }: Props) {
  const [draft, setDraft] = useState("");

  // 会话读不到、也还没拿到过：只报错。已经拿到过就继续显示，把错误挂在上面——
  // 一次读超时不该把「分叉」「归档」一起藏起来。
  if (!thread) {
    return error ? (
      <div className="mb-3">
        <Notice tone="warn">会话读取失败：{error}</Notice>
      </div>
    ) : null;
  }

  const archived = thread.status !== "active";
  const turns = thread.turns;
  const pending = busy !== null;

  function submit() {
    const text = draft.trim();
    if (!text || pending || archived) return;
    onContinue(text);
    // 清空交给调用方决定成败：这里只是把输入框还回去，失败时任务不会丢，
    // 因为 createTurn 只在成功后才会被选中。
    setDraft("");
  }

  return (
    <section className="mb-3 rounded-lg border border-line/80 bg-surface/30">
      <header className="flex flex-wrap items-center gap-2 border-b border-line/60 px-3 py-2">
        <span className="shrink-0 font-mono text-[10px] tracking-wide text-faint">会话</span>
        <span className="min-w-0 flex-1 truncate text-[13px] text-text">{thread.title}</span>
        {thread.forkedFrom && (
          <span className="shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted" title={thread.forkedFrom}>
            分叉
          </span>
        )}
        <span className="shrink-0 font-mono text-[10px] text-faint">{thread.turnCount} 轮</span>
        {archived && <span className="shrink-0 font-mono text-[10px] text-muted">已归档</span>}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onFork}
            disabled={pending}
            className="rounded-md border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
            title="带上这条会话的上下文另开一条线，不复制已有轮次"
          >
            {busy === "fork" ? "分叉中" : "分叉"}
          </button>
          <button
            type="button"
            onClick={onArchive}
            disabled={pending || archived}
            className="rounded-md border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-40"
            title="归档后不再接受新的一轮，已有轮次全部保留"
          >
            {busy === "archive" ? "归档中" : "归档"}
          </button>
        </div>
      </header>

      {error && (
        <p className="border-b border-line/60 px-3 py-1 text-[10px] text-muted">刷新会话失败：{error}</p>
      )}

      {turns.length === 0 ? (
        <p className="px-3 py-3 text-[11px] leading-relaxed text-faint">
          这条会话还没有轮次。说一句要做什么，它就成为第 1 轮；分叉来的会话会带上来源会话的上下文。
        </p>
      ) : (
        <ol className="max-h-40 overflow-y-auto px-2 py-2">
          {turns.map((turn, index) => {
            const active = turn.id === currentTaskId;
            return (
              <li key={turn.id}>
                <button
                  type="button"
                  onClick={() => onOpenTurn(turn.id)}
                  aria-current={active}
                  className={`flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
                    active ? "bg-brand/[0.08]" : "hover:bg-surface-2/70"
                  }`}
                >
                  <span className={`mt-0.5 w-5 shrink-0 text-right font-mono text-[10px] ${active ? "text-brand" : "text-faint"}`}>
                    {index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline gap-2">
                      <span className="min-w-0 flex-1 truncate text-[12px] text-text">{turn.title}</span>
                      <StatusBadge status={turn.status} />
                      <span className="shrink-0 font-mono text-[10px] text-faint">{relativeTime(turn.createdAt)}</span>
                    </span>
                    {turn.resultSummary && (
                      <span className="mt-0.5 block truncate text-[11px] text-muted">{clip(turn.resultSummary, 90)}</span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}

      <div className="border-t border-line/60 px-3 py-2">
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                submit();
              }
            }}
            rows={2}
            disabled={archived}
            aria-label="继续聊"
            placeholder={archived ? "会话已归档，不能再开新的一轮" : "接着说——例如：把刚才那个接口也补上邮箱校验"}
            className="min-h-0 w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12px] leading-relaxed text-text placeholder:text-faint focus:border-brand/60 focus:outline-none disabled:opacity-50"
          />
          <button
            type="button"
            onClick={submit}
            disabled={pending || archived || !draft.trim()}
            className="shrink-0 rounded-lg bg-brand px-3 py-2 text-[12px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {busy === "continue" ? "创建中" : "继续聊"}
          </button>
        </div>
        <p className="mt-1 text-[10px] leading-relaxed text-faint">
          新的一轮会带上这条会话前面几轮的结论（不是全部消息），并自动开始执行。
        </p>
      </div>
    </section>
  );
}
