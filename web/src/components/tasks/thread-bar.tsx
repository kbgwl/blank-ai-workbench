"use client";

import { useEffect, useState } from "react";
import { Notice } from "@/components/projects/ui";
import { errorMessage, type ThreadDetail } from "./api";
import { StatusBadge } from "./bits";
import { clip, relativeTime } from "./format";
import { fetchGrants, revokeAllGrants, revokeGrant, type SessionGrant } from "./grants-api";
import { grantExpiry, grantSummary } from "./grants";

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
  const [grants, setGrants] = useState<SessionGrant[]>([]);
  const [grantsOpen, setGrantsOpen] = useState(false);
  const [grantBusy, setGrantBusy] = useState("");
  const [grantError, setGrantError] = useState("");
  const [grantNonce, setGrantNonce] = useState(0);
  const threadId = thread?.id ?? "";
  const lastTurnStatus = thread?.turns[thread.turns.length - 1]?.status ?? "";
  // 不切 threadId 就清空 state（effect 里同步 setState 会触发级联渲染）：
  // 渲染时按 thread 过滤，切会话那一帧就不会把上一条会话的授权带过来。
  const visibleGrants = grants.filter((grant) => grant.thread === threadId);

  useEffect(() => {
    if (!threadId) return;
    let alive = true;
    fetchGrants(threadId)
      .then((rows) => {
        if (alive) setGrants(rows);
      })
      .catch((error: unknown) => {
        if (alive) setGrantError(errorMessage(error));
      });
    return () => {
      alive = false;
    };
  }, [threadId, lastTurnStatus, grantNonce]);

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

  async function revokeOne(category: string) {
    if (!threadId) return;
    setGrantBusy(category);
    setGrantError("");
    try {
      await revokeGrant(threadId, category);
      setGrantNonce((value) => value + 1);
    } catch (error) {
      setGrantError(errorMessage(error));
    } finally {
      setGrantBusy("");
    }
  }

  async function revokeAll() {
    if (!threadId) return;
    setGrantBusy("all");
    setGrantError("");
    try {
      await revokeAllGrants(threadId);
      setGrantNonce((value) => value + 1);
    } catch (error) {
      setGrantError(errorMessage(error));
    } finally {
      setGrantBusy("");
    }
  }

  function submit() {
    const text = draft.trim();
    if (!text || pending || archived) return;
    onContinue(text);
    // 清空交给调用方决定成败：这里只是把输入框还回去，失败时任务不会丢，
    // 因为 createTurn 只在成功后才会被选中。
    setDraft("");
  }

  return (
    <section className="glass-soft mb-3 rounded-lg">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <span className="shrink-0 font-mono text-[11px] tracking-wide text-faint">会话</span>
        <span className="min-w-0 flex-1 truncate text-[14px] text-text">{thread.title}</span>
        {thread.forkedFrom && (
          <span className="shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[11px] text-muted" title={thread.forkedFrom}>
            分叉
          </span>
        )}
        <span className="shrink-0 font-mono text-[11px] text-faint">{thread.turnCount} 轮</span>
        {archived && <span className="shrink-0 font-mono text-[11px] text-muted">已归档</span>}
        {grantSummary(visibleGrants) && (
          <button
            type="button"
            onClick={() => setGrantsOpen((value) => !value)}
            title="本会话批准过的同类动作；可以按类别或全部撤销"
            className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
              grantsOpen
                ? "border-brand/40 bg-brand/10 text-brand"
                : "border-line bg-surface text-muted hover:border-line-strong hover:text-text"
            }`}
          >
            {grantSummary(visibleGrants)}
          </button>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onFork}
            disabled={pending}
            className="rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
            title="带上这条会话的上下文另开一条线，不复制已有轮次"
          >
            {busy === "fork" ? "分叉中" : "分叉"}
          </button>
          <button
            type="button"
            onClick={onArchive}
            disabled={pending || archived}
            className="rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-40"
            title="归档后不再接受新的一轮，已有轮次全部保留"
          >
            {busy === "archive" ? "归档中" : "归档"}
          </button>
        </div>
      </header>

      {grantsOpen && visibleGrants.length > 0 && (
        <div className="border-b border-line px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-[11px] text-faint">本会话授权</span>
            <button
              type="button"
              disabled={Boolean(grantBusy)}
              onClick={() => void revokeAll()}
              className="rounded border border-line bg-surface px-2 py-0.5 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
            >
              全部撤销
            </button>
          </div>
          <ul className="mt-1 flex flex-col gap-1">
            {visibleGrants.map((grant) => (
              <li key={grant.category} className="flex items-center gap-2 text-[12px]">
                <span className="min-w-0 flex-1 truncate text-text" title={grant.category}>
                  {grant.label}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-faint">{grantExpiry(grant.expires_at)}</span>
                <button
                  type="button"
                  disabled={grantBusy === grant.category}
                  onClick={() => void revokeOne(grant.category)}
                  className="shrink-0 rounded border border-line bg-surface px-2 py-0.5 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
                >
                  撤销
                </button>
              </li>
            ))}
          </ul>
          {grantError && <p className="mt-1 text-[11px] leading-relaxed text-danger">{grantError}</p>}
        </div>
      )}

      {error && (
        <p className="border-b border-line px-3 py-1 text-[11px] text-muted">刷新会话失败：{error}</p>
      )}

      {turns.length === 0 ? (
        <p className="px-3 py-3 text-[12px] leading-relaxed text-faint">
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
                    active ? "bg-brand/[0.08]" : "hover:bg-surface-2"
                  }`}
                >
                  <span className={`mt-0.5 w-5 shrink-0 text-right font-mono text-[11px] ${active ? "text-brand" : "text-faint"}`}>
                    {index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline gap-2">
                      <span className="min-w-0 flex-1 truncate text-[13px] text-text">{turn.title}</span>
                      <StatusBadge status={turn.status} />
                      <span className="shrink-0 font-mono text-[11px] text-faint">{relativeTime(turn.createdAt)}</span>
                    </span>
                    {turn.resultSummary && (
                      <span className="mt-0.5 block truncate text-[12px] text-muted">{clip(turn.resultSummary, 90)}</span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}

      <div className="border-t border-line px-3 py-2">
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
            className="min-h-0 w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] leading-relaxed text-text placeholder:text-faint focus:border-brand/60 focus:outline-none disabled:opacity-50"
          />
          <button
            type="button"
            onClick={submit}
            disabled={pending || archived || !draft.trim()}
            className="shrink-0 rounded-lg bg-brand px-3 py-2 text-[13px] font-medium text-on-brand transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {busy === "continue" ? "创建中" : "继续聊"}
          </button>
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-faint">
          新的一轮会带上这条会话前面几轮的结论（不是全部消息），并自动开始执行。
        </p>
      </div>
    </section>
  );
}
