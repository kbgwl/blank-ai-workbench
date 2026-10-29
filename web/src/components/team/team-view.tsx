"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { MarkdownView } from "@/components/bot/markdown-view";
import { Button, Field, Input, Notice, Spinner, StateLine } from "@/components/projects/ui";
import { guardianFromPayload } from "@/components/tasks/api";
import { guardianReviewLabel } from "@/components/tasks/status";
import { errorCode, errorMessage } from "@/lib/http";
import {
  answerTeamQuestion,
  createTeamBot,
  createTeamGroup,
  decideTeamApproval,
  deleteTeamBot,
  deleteTeamGroup,
  fetchTeamBoot,
  fetchTeamGroupState,
  searchTeamChat,
  sendTeamMessage,
  stopTeamRound,
  updateTeamBot,
  updateTeamGroup,
  type ApprovalScope,
  type BotInput,
  type ChatSearchHit,
  type ChatSearchResult,
  type GroupInput,
  type PendingTurn,
  type PermissionMode,
  type TeamApproval,
  type TeamBot,
  type TeamBoot,
  type TeamGroupState,
  type TeamGroupSummary,
  type TeamMember,
  type TeamMessage,
  type TeamQuestion,
} from "./api";
import {
  argumentPreview,
  botRoundTokens,
  initials,
  insertMention,
  messageStatusLabel,
  messageTokens,
  roundActive,
  roundGapLabel,
  roundSpend,
  roundStatusLabel,
  shortTokens,
  splitHighlight,
  usagePercent,
} from "./format";

type Tab = "chat" | "bots";

const MODE_LABEL: Record<PermissionMode, string> = {
  default: "默认 · 高风险先确认",
  accept_edits: "自动接受文件编辑",
  plan: "只读计划",
  bypass: "全部放行",
};

const selectClass =
  "w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] text-text focus:border-brand/60 focus:outline-none";

function timeOf(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function Avatar({ name, admin = false, size = "md" }: { name: string; admin?: boolean; size?: "sm" | "md" }) {
  const box = size === "sm" ? "size-6 text-[10px]" : "size-8 text-[11px]";
  return (
    <span
      className={`grid ${box} shrink-0 place-items-center rounded-lg font-semibold tracking-wide ${
        admin ? "bg-[#1b2a4a] text-[#5eead4]" : "bg-surface-3 text-muted"
      }`}
    >
      {initials(name)}
    </span>
  );
}

function Modal({
  title,
  subtitle,
  onClose,
  children,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div
      data-overlay-open
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[88vh] w-full max-w-xl flex-col overflow-hidden rounded-xl border border-line bg-bg shadow-2xl">
        <header className="flex shrink-0 items-start gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-[14px] font-semibold tracking-tight">{title}</h2>
            {subtitle && <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="grid size-7 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 hover:text-text"
          >
            <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={1.8}>
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">{children}</div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 主视图
// --------------------------------------------------------------------------- #

function Highlighted({ text, tokens }: { text: string; tokens: string[] }) {
  return (
    <>
      {splitHighlight(text, tokens).map((part, index) =>
        part.hit ? (
          <mark key={index} className="rounded-sm bg-brand/20 px-0.5 text-text">
            {part.text}
          </mark>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </>
  );
}

function SearchResultList({
  query,
  result,
  loading,
  error,
  onPick,
}: {
  query: string;
  result: ChatSearchResult | null;
  loading: boolean;
  error: string;
  onPick: (hit: ChatSearchHit) => void;
}) {
  if (error) {
    return <p className="px-2 py-4 text-[11.5px] leading-relaxed text-danger">搜索失败：{error}</p>;
  }
  const hits = result?.results ?? [];
  if (!loading && result && hits.length === 0) {
    return <p className="px-2 py-4 text-[11.5px] leading-relaxed text-faint">没有找到“{query}”相关内容。</p>;
  }
  return (
    <div className="pb-2">
      <p className="px-1 pb-1 font-mono text-[9.5px] tracking-wide text-faint uppercase">
        {loading && !result ? "搜索中…" : `${result?.total ?? 0} 条结果`}
        {result?.truncated ? " · 候选已截断" : ""}
      </p>
      <div className="space-y-1">
        {hits.map((hit) => (
          <button
            key={`${hit.source}-${hit.id}`}
            type="button"
            onClick={() => onPick(hit)}
            className="w-full rounded-lg border border-line bg-surface px-2 py-1.5 text-left transition-colors hover:border-brand/40 hover:bg-surface-2"
          >
            <span className="flex items-center gap-1.5">
              <span
                className={`max-w-[104px] shrink-0 truncate rounded px-1 py-0.5 text-[9px] ${
                  hit.source === "task" ? "bg-brand/[0.08] text-brand" : "bg-surface-3 text-muted"
                }`}
              >
                {hit.source === "task" ? "任务结论" : hit.group_name || "群聊"}
              </span>
              <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-text">
                {hit.source === "task" ? hit.group_name || "任务" : hit.speaker}
              </span>
              <span className="shrink-0 font-mono text-[9px] text-faint">{timeOf(hit.created_at)}</span>
            </span>
            <span className="mt-1 block text-[11px] leading-relaxed text-muted">
              <Highlighted text={hit.snippet || hit.content.slice(0, 80)} tokens={result?.tokens ?? []} />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function TeamView() {
  const [boot, setBoot] = useState<TeamBoot | null>(null);
  const [bootError, setBootError] = useState("");
  const [tab, setTab] = useState<Tab>("chat");
  const [groups, setGroups] = useState<TeamGroupSummary[]>([]);
  const [activeGroupId, setActiveGroupId] = useState("");
  const [state, setState] = useState<TeamGroupState | null>(null);
  const [stateError, setStateError] = useState("");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [actionError, setActionError] = useState("");
  const [botEditing, setBotEditing] = useState<TeamBot | "new" | null>(null);
  const [groupEditing, setGroupEditing] = useState<{ group: TeamGroupSummary | null; memberIds: string[] } | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<ChatSearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  // 命中旧消息时用 around_id 取它附近的一屏；anchor>0 期间轮询也停在这个窗口。
  const [anchorId, setAnchorId] = useState(0);
  const [jump, setJump] = useState<{ groupId: string; messageId: number } | null>(null);
  const [highlightId, setHighlightId] = useState(0);
  const rosterRef = useRef({ group: "", signature: "", status: "" });
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const refreshBoot = useCallback(async (options: { keepSelection?: boolean } = {}) => {
    try {
      const next = await fetchTeamBoot();
      setBoot(next);
      setGroups(next.groups);
      setBootError("");
      if (!options.keepSelection) {
        setActiveGroupId((current) =>
          next.groups.some((group) => group.id === current) ? current : (next.groups[0]?.id ?? ""),
        );
      }
    } catch (error) {
      setBootError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchTeamBoot()
      .then((next) => {
        if (cancelled) return;
        setBoot(next);
        setGroups(next.groups);
        setBootError("");
        setActiveGroupId((current) =>
          next.groups.some((group) => group.id === current) ? current : (next.groups[0]?.id ?? ""),
        );
      })
      .catch((error) => {
        if (!cancelled) setBootError(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 轮询：有活时快，闲时慢。刷新的是同一份群状态，因此不会与发送/审批抢写。
  useEffect(() => {
    if (tab !== "chat" || !activeGroupId) return;
    let cancelled = false;
    let timer = 0;
    const tick = async () => {
      let delay = 5000;
      try {
        const next = await fetchTeamGroupState(activeGroupId, undefined, anchorId || undefined);
        if (cancelled) return;
        setState(next);
        setStateError("");
        setGroups((rows) => rows.map((group) => (group.id === next.group.id ? next.group : group)));
        delay = roundActive(next.round?.status) ? 1200 : 5000;
      } catch (error) {
        if (cancelled) return;
        if (errorCode(error) === "team_group_not_found") {
          // 群可能在另一处被删掉（比如设置面板刚点了删除）：继续轮询只会一直
          // 404，把选择态清掉、重新拉一次清单，让页面落回还有效的群或空态。
          setState(null);
          setStateError("");
          setAnchorId(0);
          setJump(null);
          setActiveGroupId("");
          void refreshBoot();
          return;
        }
        setStateError(errorMessage(error));
        delay = 4000;
      }
      if (!cancelled) timer = window.setTimeout(() => void tick(), delay);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [tab, activeGroupId, refreshBoot, anchorId]);

  useEffect(() => {
    const element = scroller.current;
    if (element && stick.current) element.scrollTop = element.scrollHeight;
  }, [state?.messages.length, activeGroupId]);

  // 搜索防抖：每敲一次字都发请求只会让结果闪烁，300ms 足够区分"还在输入"和"想搜了"。
  // 空串时那三份读数的归零放在事件里做（见 resetSearchReadout），effect 里只跑真有
  // 查询的那条路——在 effect 体内同步 setState 会多渲染一轮，规则也是这么要求的。
  useEffect(() => {
    const query = searchQuery.trim();
    if (!query) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      if (!cancelled) setSearching(true);
      try {
        const next = await searchTeamChat(query, { limit: 20 });
        if (cancelled) return;
        setSearchResult(next);
        setSearchError("");
      } catch (error) {
        if (!cancelled) {
          setSearchResult(null);
          setSearchError(errorMessage(error));
        }
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [searchQuery]);

  // 状态里出现目标消息后再滚动：切换群时 DOM 还没渲染，提前找必然找不到。
  // 这一拍只碰 DOM 这个外部系统，改状态挪到下一帧——在 effect 体内同步 setState
  // 会多渲染一轮，也会让高亮抢在滚动之前出现。
  useEffect(() => {
    if (!jump || !state || state.group.id !== jump.groupId) return;
    const element = document.getElementById(`team-message-${jump.messageId}`);
    if (!element) return;
    stick.current = false;
    const target = jump.messageId;
    element.scrollIntoView({ block: "center", behavior: "smooth" });
    const frame = window.requestAnimationFrame(() => {
      setJump(null);
      setHighlightId(target);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [jump, state]);

  // 高亮淡出的计时器单独挂：跟着 highlightId 走，清理时只清自己那一个，
  // 不会被轮询刷新（state 变）顺手清掉而让高亮一直留着。
  useEffect(() => {
    if (!highlightId) return;
    const timer = window.setTimeout(() => setHighlightId(0), 2600);
    return () => window.clearTimeout(timer);
  }, [highlightId]);

  // 兜底：消息可能已被清理（或 around 窗口没取到），不能一直停在等待态。
  useEffect(() => {
    if (!jump) return;
    const timer = window.setTimeout(() => {
      setJump(null);
      setActionError("没能定位到这条消息，它可能已经被清理。");
    }, 8000);
    return () => window.clearTimeout(timer);
  }, [jump]);

  // 主理人用工具改成员、或一轮结束时，侧栏的成员数与最后消息需要重新拉一次。
  useEffect(() => {
    if (!state) return;
    const signature = state.members.map((member) => `${member.bot_id}:${member.enabled ? 1 : 0}`).join(",");
    const status = state.round?.status ?? "";
    const previous = rosterRef.current;
    rosterRef.current = { group: state.group.id, signature, status };
    if (previous.group !== state.group.id) return;
    if (previous.signature !== signature || (roundActive(previous.status) && !roundActive(status))) {
      void refreshBoot({ keepSelection: true });
    }
  }, [state, refreshBoot]);

  const activeGroup = useMemo(
    () => groups.find((group) => group.id === activeGroupId) ?? state?.group ?? null,
    [groups, activeGroupId, state?.group],
  );
  const round = state?.round ?? null;
  const busyRound = roundActive(round?.status);
  // 本轮的花费以账本为准（跨成员任务、含没走完的发言）；账本读不到时才退回轮计数。
  const spend = useMemo(() => roundSpend(round ?? {}), [round]);
  const gapLabel = roundGapLabel(spend.extra);
  const spendTitle = spend.fromLedger
    ? `账本口径：${spend.calls} 笔 · ${spend.tasks} 个成员任务 · ${spend.runs} 次发言（含放行续跑）\n` +
      `编排器轮计数 ${Math.max(0, Math.round(round?.tokens ?? 0))}，只累加收尾完成的发言`
    : "本轮花费来自编排器的轮计数；账本里还没有这一轮的记录";

  function selectGroup(groupId: string) {
    if (groupId === activeGroupId) return;
    setAnchorId(0);
    setJump(null);
    setActionError("");
    stick.current = true;
    setActiveGroupId(groupId);
  }

  // 清空搜索框时把三份读数一起归零。放在事件里做，而不是靠 effect 监听空串——
  // 那样每次清空都要多一次渲染，且 effect 体内的同步 setState 会被规则拦下。
  function resetSearchReadout() {
    setSearchResult(null);
    setSearchError("");
    setSearching(false);
  }

  function clearSearch() {
    setSearchQuery("");
    resetSearchReadout();
  }

  function openSearchHit(hit: ChatSearchHit) {
    clearSearch();
    if (hit.source === "task") {
      // 任务结论没有群内消息可定位，直接打开任务详情。
      if (hit.task_id) window.location.assign(`/tasks/detail?id=${encodeURIComponent(hit.task_id)}`);
      return;
    }
    if (!hit.group_id || !hit.message_id) return;
    setTab("chat");
    setActionError("");
    setJump({ groupId: hit.group_id, messageId: hit.message_id });
    const inWindow =
      hit.group_id === activeGroupId && (state?.messages ?? []).some((message) => message.id === hit.message_id);
    if (inWindow) {
      setAnchorId(0);
      return;
    }
    setAnchorId(hit.message_id);
    setActiveGroupId(hit.group_id);
  }

  function backToLatest() {
    setAnchorId(0);
    setJump(null);
    stick.current = true;
    setActionError("");
  }

  async function refreshState() {
    if (!activeGroupId) return;
    try {
      const next = await fetchTeamGroupState(activeGroupId, undefined, anchorId || undefined);
      setState(next);
      setGroups((rows) => rows.map((group) => (group.id === next.group.id ? next.group : group)));
      setStateError("");
    } catch (error) {
      setStateError(errorMessage(error));
    }
  }

  async function send() {
    const content = draft.trim();
    if (!content || !activeGroupId || sending || busyRound) return;
    if (anchorId > 0) setAnchorId(0);
    setSending(true);
    setActionError("");
    try {
      const next = await sendTeamMessage(activeGroupId, content);
      setState(next);
      setDraft("");
      stick.current = true;
      setGroups((rows) => rows.map((group) => (group.id === next.group.id ? next.group : group)));
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setSending(false);
    }
  }

  async function stop() {
    if (!activeGroupId || stopping) return;
    setStopping(true);
    setActionError("");
    try {
      await stopTeamRound(activeGroupId);
      await refreshState();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setStopping(false);
    }
  }

  async function decide(messageId: number, approve: boolean, scope: ApprovalScope) {
    setActionError("");
    try {
      await decideTeamApproval(messageId, approve, scope);
      await refreshState();
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function answer(messageId: number, answers: Record<string, string>) {
    setActionError("");
    try {
      await answerTeamQuestion(messageId, answers);
      await refreshState();
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function openGroupEditor(group: TeamGroupSummary | null) {
    setActionError("");
    if (!group) {
      setGroupEditing({ group: null, memberIds: [] });
      return;
    }
    try {
      const detail = await fetchTeamGroupState(group.id);
      setGroupEditing({
        group,
        memberIds: detail.members.filter((member) => !member.is_admin).map((member) => member.bot_id),
      });
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function removeBot(bot: TeamBot) {
    const ok = window.confirm(`删除 Bot「${bot.name}」？\n\n它会从所有群里移除；已发生的对话仍保留。`);
    if (!ok) return;
    try {
      await deleteTeamBot(bot.id);
      await refreshBoot({ keepSelection: true });
      await refreshState();
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  return (
    <div className="flex h-full min-h-0 bg-bg">
      <aside className="flex w-[286px] shrink-0 flex-col border-r border-line bg-sidebar">
        <div className="flex items-center gap-2 px-3 pt-3 pb-2">
          <span className="grid size-6 place-items-center rounded-md bg-[#1b2a4a]">
            <span className="size-2 rounded-[2px] bg-[#5eead4]" />
          </span>
          <span className="text-[13px] font-semibold tracking-tight">Bot 团队</span>
          <span className="rounded bg-surface-3 px-1 py-0.5 font-mono text-[9px] tracking-wide text-muted uppercase">
            team
          </span>
        </div>

        <div className="mx-3 mb-2 grid grid-cols-2 gap-1 rounded-lg bg-surface-2 p-1">
          {(["chat", "bots"] as Tab[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setTab(item)}
              className={`rounded-md px-2 py-1.5 text-[12px] font-medium transition-colors ${
                tab === item ? "bg-surface text-text shadow-sm" : "text-muted hover:text-text"
              }`}
            >
              {item === "chat" ? "群聊" : "Bot 名册"}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {tab === "chat" ? (
            <>
              <div className="px-1 pb-1.5">
                <div className="relative">
                  <svg
                    viewBox="0 0 24 24"
                    className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.9}
                  >
                    <circle cx="11" cy="11" r="6" />
                    <path d="m16 16 4 4" />
                  </svg>
                  <input
                    value={searchQuery}
                    onChange={(event) => {
                      const next = event.target.value;
                      setSearchQuery(next);
                      if (!next.trim()) resetSearchReadout();
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") clearSearch();
                    }}
                    aria-label="搜索聊天与结论"
                    placeholder="搜索所有群与结论"
                    className="w-full rounded-lg border border-line bg-surface-2 py-1.5 pr-7 pl-7 text-[12px] text-text placeholder:text-faint focus:border-brand/60 focus:outline-none"
                  />
                  {searchQuery && (
                    <button
                      type="button"
                      aria-label="清空搜索"
                      onClick={clearSearch}
                      className="absolute top-1/2 right-1.5 grid size-5 -translate-y-1/2 place-items-center rounded text-faint hover:bg-surface-3 hover:text-text"
                    >
                      ×
                    </button>
                  )}
                </div>
              </div>
              {searchQuery.trim() ? (
                <SearchResultList
                  query={searchQuery.trim()}
                  result={searchResult}
                  loading={searching}
                  error={searchError}
                  onPick={openSearchHit}
                />
              ) : (
                <>
                  <div className="px-1 pb-1.5">
                    <button
                      type="button"
                      onClick={() => void openGroupEditor(null)}
                      className="flex w-full items-center gap-2 rounded-lg border border-line-strong bg-surface px-2.5 py-2 text-[12.5px] font-medium text-text transition-colors hover:border-brand/40 hover:bg-surface-2"
                    >
                      <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth={1.9}>
                        <path d="M12 5v14M5 12h14" />
                      </svg>
                      新建群
                    </button>
                  </div>
                  {groups.length === 0 && boot && (
                    <p className="px-2 py-6 text-center text-[11.5px] leading-relaxed text-faint">
                      还没有群。
                      <br />
                      建一个群，把 Bot 拉进来。
                    </p>
                  )}
                  <div className="space-y-0.5">
                    {groups.map((group) => {
                      const active = group.id === activeGroupId;
                      const live = roundActive(group.round_status);
                      return (
                        <button
                          key={group.id}
                          type="button"
                          onClick={() => selectGroup(group.id)}
                          className={`w-full rounded-lg px-2.5 py-2 text-left transition-colors ${
                            active ? "bg-surface-3 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                          }`}
                        >
                          <span className="flex items-center gap-2">
                            <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium">{group.name}</span>
                            {live && <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-brand" />}
                            <span className="shrink-0 font-mono text-[9.5px] text-faint">{group.member_count}</span>
                          </span>
                          <span className="mt-0.5 block truncate text-[11px] text-faint">
                            {group.last_message || group.topic || "还没有消息"}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </>
              )}
            </>
          ) : (
            <>
              <div className="px-1 pb-1.5">
                <button
                  type="button"
                  onClick={() => setBotEditing("new")}
                  className="flex w-full items-center gap-2 rounded-lg border border-line-strong bg-surface px-2.5 py-2 text-[12.5px] font-medium text-text transition-colors hover:border-brand/40 hover:bg-surface-2"
                >
                  <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth={1.9}>
                    <path d="M12 5v14M5 12h14" />
                  </svg>
                  新建 Bot
                </button>
              </div>
              <div className="space-y-0.5">
                {(boot?.bots ?? []).map((bot) => (
                  <button
                    key={bot.id}
                    type="button"
                    onClick={() => setBotEditing(bot)}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-muted transition-colors hover:bg-surface-2 hover:text-text"
                  >
                    <Avatar name={bot.name} admin={bot.is_admin} size="sm" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12.5px] font-medium">{bot.name}</span>
                      <span className="block truncate text-[10.5px] text-faint">
                        {bot.role || (bot.model ? bot.model : "未绑定模型")}
                      </span>
                    </span>
                    {bot.is_admin && <span className="shrink-0 text-[9px] text-brand">主理人</span>}
                    {!bot.enabled && <span className="shrink-0 text-[9px] text-faint">停用</span>}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="border-t border-line px-3 py-2.5">
          <p className="text-[10.5px] leading-relaxed text-faint">
            主理人固定为<b className="text-muted"> 小白管家 </b>，先接住你的话，再按 @ 安排成员；成员可用
            <code className="mx-0.5 rounded bg-surface-2 px-1 font-mono text-[10px]">[PASS]</code>
            保持安静。
          </p>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        {bootError && (
          <div className="px-4 pt-3">
            <Notice>连不上团队接口：{bootError}</Notice>
          </div>
        )}
        {actionError && (
          <div className="px-4 pt-3">
            <Notice>{actionError}</Notice>
          </div>
        )}

        {tab === "bots" ? (
          <BotRoster
            boot={boot}
            onNew={() => setBotEditing("new")}
            onEdit={(bot) => setBotEditing(bot)}
            onDelete={(bot) => void removeBot(bot)}
          />
        ) : !activeGroupId || !activeGroup ? (
          <div className="flex flex-1 items-center justify-center px-6">
            {boot ? (
              <EmptyTeam onCreate={() => void openGroupEditor(null)} />
            ) : (
              <StateLine>{bootError ? "团队接口暂不可用" : "读取团队…"}</StateLine>
            )}
          </div>
        ) : (
          <>
            <header className="flex shrink-0 items-center gap-3 border-b border-line px-4 py-2.5">
              <Avatar name={activeGroup.name} admin />
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <h1 className="truncate text-[13.5px] font-semibold">{activeGroup.name}</h1>
                  {activeGroup.project_name && (
                    <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[9.5px] text-faint">
                      {activeGroup.project_name}
                    </span>
                  )}
                  {round?.status && (
                    <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${roundChipClass(round.status)}`}>
                      {roundStatusLabel(round.status)}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 truncate text-[11px] text-faint">{activeGroup.topic || "没有设置群主题"}</p>
              </div>

              <div className="hidden shrink-0 items-center gap-2 sm:flex">
                <div className="text-right">
                  <p className="font-mono text-[10px] text-faint">
                    {shortTokens(spend.tokens)} / {shortTokens(activeGroup.token_budget)}
                  </p>
                  <div className="mt-1 h-1 w-20 overflow-hidden rounded-full bg-surface-2">
                    <div
                      className="h-full rounded-full bg-brand/70"
                      style={{ width: `${usagePercent(spend.tokens, activeGroup.token_budget)}%` }}
                    />
                  </div>
                  {/* 账本比轮计数多出来的钱：发过言但没走完的那次，只有账本认得。 */}
                  {gapLabel && (
                    <p className="mt-0.5 font-mono text-[9.5px] text-warn" title={spendTitle}>
                      {gapLabel}
                    </p>
                  )}
                </div>
                {busyRound && (
                  <Button variant="danger" onClick={() => void stop()} disabled={stopping}>
                    {stopping ? <Spinner /> : null}
                    停止
                  </Button>
                )}
                <Button onClick={() => void openGroupEditor(activeGroup)}>设置</Button>
              </div>
            </header>

            <MemberStrip members={state?.members ?? []} round={round} messages={state?.messages ?? []} />

            {anchorId > 0 && (
              <div className="flex shrink-0 items-center gap-2 border-b border-line bg-brand/[0.04] px-4 py-1.5 text-[11px] text-muted">
                <span>正在查看搜索结果附近的消息</span>
                <button type="button" onClick={backToLatest} className="font-medium text-brand hover:underline">
                  回到最新
                </button>
              </div>
            )}

            {stateError && (
              <div className="px-4 pt-2">
                <Notice tone="warn">刷新失败，正在重试：{stateError}</Notice>
              </div>
            )}

            <div
              ref={scroller}
              onScroll={(event) => {
                const element = event.currentTarget;
                stick.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
              }}
              className="min-h-0 flex-1 overflow-y-auto"
            >
              <div className="mx-auto w-full max-w-[820px] px-4 py-5">
                {!state ? (
                  <StateLine>读取群聊…</StateLine>
                ) : state.messages.length === 0 ? (
                  <GroupEmpty group={activeGroup} members={state.members} />
                ) : (
                  <div className="space-y-5">
                    {state.messages.map((message) => (
                      <MessageRow
                        key={message.id}
                        message={message}
                        highlight={message.id === highlightId}
                      />
                    ))}
                    {state.pending.map((pending) => (
                      <PendingCard
                        key={`pending-${pending.message_id}`}
                        pending={pending}
                        onDecide={(approve, scope) => void decide(pending.message_id, approve, scope)}
                        onAnswer={(answers) => void answer(pending.message_id, answers)}
                      />
                    ))}
                  </div>
                )}

                {state && state.pending.length === 0 && state.messages.length > 0 && busyRound && (
                  <p className="mt-5 flex items-center gap-2 font-mono text-[10.5px] text-faint">
                    <Spinner /> 正在推进这一轮…
                  </p>
                )}
              </div>
            </div>

            <ComposerBar
              members={state?.members ?? []}
              draft={draft}
              sending={sending}
              busy={busyRound}
              onChange={setDraft}
              onSend={() => void send()}
            />
          </>
        )}
      </main>

      {boot && botEditing !== null && (
        <BotEditor
          boot={boot}
          bot={botEditing === "new" ? null : botEditing}
          onClose={() => setBotEditing(null)}
          onSaved={async () => {
            setBotEditing(null);
            await refreshBoot({ keepSelection: true });
            await refreshState();
          }}
        />
      )}

      {boot && groupEditing && (
        <GroupEditor
          boot={boot}
          group={groupEditing.group}
          initialMemberIds={groupEditing.memberIds}
          onClose={() => setGroupEditing(null)}
          onSaved={async (next) => {
            setGroupEditing(null);
            if (!next) {
              // 删除后不能继续保留被删群的选择态，否则轮询会一直 404。
              setState(null);
              setStateError("");
              setAnchorId(0);
              setJump(null);
              setActiveGroupId("");
              await refreshBoot();
              return;
            }
            await refreshBoot({ keepSelection: true });
            setTab("chat");
            setAnchorId(0);
            setJump(null);
            stick.current = true;
            setActiveGroupId(next.group.id);
            setState(next);
          }}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 群聊
// --------------------------------------------------------------------------- #

function roundChipClass(status: string): string {
  if (status === "running") return "bg-brand/[0.08] text-brand";
  if (status === "waiting_approval") return "bg-warn/[0.1] text-warn";
  if (status === "failed") return "bg-danger/[0.08] text-danger";
  return "bg-surface-2 text-muted";
}

function EmptyTeam({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex flex-col items-center text-center">
      <span className="grid size-12 place-items-center rounded-2xl bg-[#1b2a4a]">
        <span className="size-4 rounded-[4px] bg-[#5eead4]" />
      </span>
      <h2 className="mt-5 text-[22px] font-semibold tracking-tight">组一个能自己干活的 Bot 团队</h2>
      <p className="mt-2 max-w-md text-[13px] leading-relaxed text-muted">
        建一个群，把负责不同方向的 Bot 拉进来。小白管家会先接住你的需求，再按 @ 把活分给合适的人；
        成员可以用 [PASS] 保持安静。
      </p>
      <div className="mt-6">
        <Button variant="primary" onClick={onCreate}>
          新建第一个群
        </Button>
      </div>
    </div>
  );
}

function GroupEmpty({ group, members }: { group: TeamGroupSummary; members: TeamMember[] }) {
  return (
    <div className="flex min-h-[46vh] flex-col items-center justify-center text-center">
      <div className="flex -space-x-2">
        {members.slice(0, 5).map((member) => (
          <span key={member.id} className="rounded-lg ring-2 ring-bg">
            <Avatar name={member.name} admin={member.is_admin} />
          </span>
        ))}
      </div>
      <h2 className="mt-4 text-[19px] font-semibold tracking-tight">{group.name}</h2>
      <p className="mt-2 max-w-md text-[12.5px] leading-relaxed text-muted">
        {group.topic || "这个群还没有主题。直接说你要做什么，主理人会先回答，再安排成员接手。"}
      </p>
      <p className="mt-4 rounded-lg border border-line bg-surface-2/60 px-3 py-2 text-[11.5px] leading-relaxed text-faint">
        试试：<span className="text-muted">@成员</span> 可以直接点名；成员回复里的 @ 会触发下一棒。
      </p>
    </div>
  );
}

function MemberStrip({
  members,
  round,
  messages,
}: {
  members: TeamMember[];
  round: TeamGroupState["round"];
  messages: TeamMessage[];
}) {
  const speakingId = useMemo(
    () => messages.find((message) => message.role === "bot" && message.status === "running")?.bot_id ?? "",
    [messages],
  );
  if (members.length === 0) return null;
  return (
    <div className="flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-line px-4 py-2">
      <span className="mr-1 shrink-0 font-mono text-[9.5px] tracking-wide text-faint uppercase">成员</span>
      {members.map((member) => {
        const speaking = member.bot_id === speakingId;
        const queued = round?.queue?.includes(member.bot_id) ?? false;
        const spoke = round?.spoken?.includes(member.bot_id) ?? false;
        // 每人本轮花了多少：从账本按发起轮筛出来，不是拿发言条数估的。
        const roundTokens = botRoundTokens(round, member.bot_id);
        return (
          <span
            key={member.id}
            title={`${member.role || "成员"}${member.model ? ` · ${member.model}` : ""}${
              member.enabled ? "" : "（已停用）"
            }${roundTokens > 0 ? ` · 本轮 ${roundTokens.toLocaleString("en-US")} tokens` : ""}`}
            className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10.5px] transition-colors ${
              speaking
                ? "border-brand/40 bg-brand/[0.06] text-brand"
                : member.enabled
                  ? "border-line bg-surface-2/60 text-muted"
                  : "border-line bg-transparent text-faint line-through"
            }`}
          >
            <span
              className={`size-1.5 rounded-full ${
                speaking ? "animate-pulse bg-brand" : queued ? "bg-warn" : spoke ? "bg-accent/60" : "bg-line-strong"
              }`}
            />
            {member.name}
            {roundTokens > 0 && <span className="font-mono text-[9px] opacity-70">{shortTokens(roundTokens)}</span>}
            {member.is_admin && <span className="text-[9px] text-brand">主理人</span>}
            {queued && !speaking && <span className="text-[9px] text-warn">排队中</span>}
          </span>
        );
      })}
    </div>
  );
}

function MessageRow({ message, highlight = false }: { message: TeamMessage; highlight?: boolean }) {
  const anchorProps = {
    id: `team-message-${message.id}`,
    "data-message-id": message.id,
  };
  const glow = highlight ? "rounded-xl bg-brand/[0.06] ring-1 ring-brand/25" : "";
  if (message.role === "user") {
    return (
      <div {...anchorProps} className={`flex justify-end transition-colors ${glow}`}>
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-2 px-3.5 py-2 text-[13.5px] leading-relaxed whitespace-pre-wrap">
          {message.content}
        </div>
      </div>
    );
  }

  if (message.role === "system") {
    return (
      <div {...anchorProps} className={`flex items-center gap-2 transition-colors ${glow}`}>
        <span className="h-px flex-1 bg-line" />
        <span
          className={`rounded-full px-2 py-0.5 text-[10.5px] ${
            message.status === "failed" ? "bg-danger/[0.06] text-danger" : "bg-surface-2 text-faint"
          }`}
        >
          {message.content}
        </span>
        <span className="h-px flex-1 bg-line" />
      </div>
    );
  }

  const badge = messageStatusLabel(message.status);
  const running = message.status === "running";
  const pass = message.status === "pass";
  const failed = message.status === "failed";
  const paused = message.status === "paused";

  return (
    <div {...anchorProps} className={`flex gap-2.5 transition-colors ${glow}`}>
      <Avatar name={message.bot_name} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[12.5px] font-semibold">{message.bot_name}</span>
          {message.meta.wrap_up && (
            <span className="rounded bg-brand/[0.08] px-1.5 py-0.5 text-[9.5px] font-medium text-brand">收尾</span>
          )}
          {message.meta.mentions?.length ? (
            <span className="font-mono text-[9.5px] text-faint">
              → {message.meta.mentions.map((name) => `@${name}`).join(" ")}
            </span>
          ) : null}
          {badge && (
            <span
              className={`rounded-full px-1.5 py-0.5 text-[9.5px] ${
                failed
                  ? "bg-danger/[0.08] text-danger"
                  : paused || message.status === "waiting_approval"
                    ? "bg-warn/[0.1] text-warn"
                    : running
                      ? "bg-brand/[0.08] text-brand"
                      : "bg-surface-2 text-faint"
              }`}
            >
              {badge}
            </span>
          )}
          {message.created_at && <span className="ml-auto font-mono text-[9.5px] text-faint">{timeOf(message.created_at)}</span>}
        </div>

        <div className="mt-1 text-[13px] leading-relaxed text-text/90">
          {running && !message.content ? (
            <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-faint">
              <Spinner /> 正在读上下文、调用工具…
            </span>
          ) : pass ? (
            <span className="text-[12px] text-faint italic">这一轮没有补充，保持安静。</span>
          ) : message.content ? (
            <MarkdownView text={message.content} />
          ) : (
            <span className="text-[12px] text-faint">{failed ? "这一轮没有完成。" : "（没有输出结论）"}</span>
          )}
        </div>

        {(messageTokens(message) > 0 || (message.meta.steps ?? 0) > 0) && (
          <div className="mt-1.5 flex items-center gap-2 font-mono text-[9.5px] text-faint">
            {messageTokens(message) > 0 && <span>{shortTokens(messageTokens(message))} tokens</span>}
            {(message.meta.steps ?? 0) > 0 && <span>{message.meta.steps} 步</span>}
            {message.meta.error && (
              <span className="truncate text-danger/80" title={message.meta.error}>
                {message.meta.error}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 审批 / 提问
// --------------------------------------------------------------------------- #

function PendingCard({
  pending,
  onDecide,
  onAnswer,
}: {
  pending: PendingTurn;
  onDecide: (approve: boolean, scope: ApprovalScope) => void;
  onAnswer: (answers: Record<string, string>) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  function run(action: () => void) {
    setBusy(true);
    action();
    // 后端 202 后要等后台恢复；按钮先放开，轮询会把新状态带回来。
    window.setTimeout(() => setBusy(false), 600);
  }

  return (
    <div className="rounded-xl border border-warn/30 bg-warn/[0.04] px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="grid size-5 place-items-center rounded bg-warn/15 text-[11px] text-warn">!</span>
        <span className="text-[12px] font-semibold text-warn">
          {pending.bot_name} 需要你确认
        </span>
        <span className="font-mono text-[9.5px] text-faint">
          {pending.approvals.length > 0 ? `${pending.approvals.length} 个高风险操作` : `${pending.questions.length} 个问题`}
        </span>
      </div>

      {pending.approvals.map((approval) => (
        <ApprovalBlock
          key={approval.tool_call_id || approval.tool}
          approval={approval}
          busy={busy}
          onDecide={(approve, scope) => run(() => onDecide(approve, scope))}
        />
      ))}

      {pending.questions.length > 0 && (
        <div className="mt-3 space-y-3">
          {pending.questions.map((question) => (
            <QuestionBlock
              key={question.id}
              question={question}
              value={answers[question.id] ?? ""}
              onChange={(value) => setAnswers((current) => ({ ...current, [question.id]: value }))}
            />
          ))}
          <div className="flex items-center justify-end gap-2">
            {pending.question_expired === true && (
              <span className="text-[11px] text-faint">问题已过期，重跑这一轮才会再生效。</span>
            )}
            <Button
              variant="ghost"
              disabled={
                busy ||
                pending.question_expired === true ||
                Object.values(answers).every((value) => !value.trim())
              }
              onClick={() => run(() => onAnswer(answers))}
            >
              回答并继续
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function ApprovalBlock({
  approval,
  busy,
  onDecide,
}: {
  approval: TeamApproval;
  busy: boolean;
  onDecide: (approve: boolean, scope: ApprovalScope) => void;
}) {
  const args = argumentPreview(approval.arguments);
  const options = approval.approval_options ?? [];
  const expired = approval.expired === true;
  // 审查意见和任务时间线共用同一个三态读法：缺键、关着、说过话不是一种状态。
  const reviewNote = guardianReviewLabel(guardianFromPayload(approval));
  return (
    <div className="mt-3 rounded-lg border border-line bg-surface px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-text">{approval.tool}</code>
        <span className="rounded bg-danger/[0.06] px-1.5 py-0.5 text-[9.5px] text-danger">
          {approval.risk_level === "critical" ? "极高风险" : "高风险"}
        </span>
        {expired && (
          <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[9.5px] text-faint">已过期</span>
        )}
        {approval.message && <span className="min-w-0 flex-1 truncate text-[11px] text-muted">{approval.message}</span>}
      </div>
      {args && (
        <pre className="mt-2 max-h-28 overflow-auto rounded-md bg-surface-2 px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words text-text/80">
          {args}
        </pre>
      )}
      {reviewNote && (
        <p className="mt-2 text-[11px] leading-relaxed text-muted" title="独立审查员先看过了；它放行不等于安全，它说话也不会替你批准">
          {reviewNote}
        </p>
      )}
      {expired ? (
        <p className="mt-2.5 text-[11px] text-faint">
          这条审批已经过期，批准/驳回都不会再生效；要接着做，重跑这一轮。
        </p>
      ) : (
        <div className="mt-2.5 flex flex-wrap items-center justify-end gap-2">
          <Button variant="danger" disabled={busy} onClick={() => onDecide(false, "once")}>
            驳回
          </Button>
          {(options.length > 0 ? options : [{ scope: "once" as ApprovalScope, label: "允许这一次" }]).map((option) => (
            <Button
              key={option.scope}
              variant={option.scope === "once" ? "primary" : "ghost"}
              disabled={busy}
              title={"warning" in option && option.warning ? option.warning : undefined}
              onClick={() => onDecide(true, option.scope)}
            >
              {option.label}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

function QuestionBlock({
  question,
  value,
  onChange,
}: {
  question: TeamQuestion;
  value: string;
  onChange: (value: string) => void;
}) {
  const options = question.options ?? [];
  const multi = question.multiSelect === true;
  const selected = multi ? value.split("\n").filter(Boolean) : [value].filter(Boolean);

  function toggle(label: string) {
    if (!multi) {
      onChange(label);
      return;
    }
    const next = selected.includes(label) ? selected.filter((item) => item !== label) : [...selected, label];
    onChange(next.join("\n"));
  }

  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2.5">
      {question.header && <p className="font-mono text-[9.5px] tracking-wide text-faint uppercase">{question.header}</p>}
      <p className="mt-0.5 text-[12.5px] leading-relaxed font-medium">{question.question}</p>
      {options.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {options.map((option) => {
            const active = selected.includes(option.label);
            return (
              <button
                key={option.label}
                type="button"
                onClick={() => toggle(option.label)}
                title={option.description || ""}
                className={`rounded-lg border px-2.5 py-1.5 text-left text-[11.5px] transition-colors ${
                  active
                    ? "border-brand/50 bg-brand/[0.07] text-brand"
                    : "border-line bg-surface-2/60 text-muted hover:border-line-strong hover:text-text"
                }`}
              >
                <span className="block font-medium">{option.label}</span>
                {option.description && <span className="mt-0.5 block text-[10px] text-faint">{option.description}</span>}
              </button>
            );
          })}
        </div>
      )}
      {(question.allowFreeText !== false && question.allow_free_text !== false) && (
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={options.length > 0 ? "或者直接输入…" : "输入你的回答…"}
          className="mt-2 w-full rounded-lg border border-line bg-surface-2 px-2.5 py-1.5 text-[12px] placeholder:text-faint focus:border-brand/60 focus:outline-none"
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 输入框
// --------------------------------------------------------------------------- #

function ComposerBar({
  members,
  draft,
  sending,
  busy,
  onChange,
  onSend,
}: {
  members: TeamMember[];
  draft: string;
  sending: boolean;
  busy: boolean;
  onChange: (value: string) => void;
  onSend: () => void;
}) {
  const textarea = useRef<HTMLTextAreaElement>(null);

  function insert(name: string) {
    onChange(insertMention(draft, name));
    textarea.current?.focus();
  }

  return (
    <div className="shrink-0 border-t border-line px-4 py-3">
      <div className="mx-auto w-full max-w-[820px]">
        <div className="mb-1.5 flex flex-wrap items-center gap-1">
          <span className="mr-0.5 font-mono text-[9.5px] text-faint">@</span>
          {members
            .filter((member) => member.enabled)
            .map((member) => (
              <button
                key={member.id}
                type="button"
                onClick={() => insert(member.name)}
                className="rounded-full border border-line bg-surface-2/60 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:border-brand/40 hover:text-brand"
              >
                {member.name}
              </button>
            ))}
        </div>
        <div className="flex items-end gap-2 rounded-xl border border-line bg-surface px-2.5 py-2 focus-within:border-brand/50">
          <textarea
            ref={textarea}
            rows={1}
            value={draft}
            disabled={busy}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                onSend();
              }
            }}
            placeholder={
              busy ? "这一轮还在进行，等它说完再发…" : "说清楚你要做什么；@成员 可以直接点名，Enter 发送"
            }
            className="max-h-40 min-h-[26px] flex-1 resize-none bg-transparent text-[13px] leading-relaxed text-text placeholder:text-faint focus:outline-none disabled:cursor-not-allowed"
          />
          <button
            type="button"
            onClick={onSend}
            disabled={busy || sending || !draft.trim()}
            title="发送（Enter）"
            className="grid size-8 shrink-0 place-items-center rounded-lg bg-text text-bg transition-opacity disabled:opacity-30"
          >
            {sending ? (
              <Spinner />
            ) : (
              <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={2}>
                <path d="M12 19V5M6 11l6-6 6 6" />
              </svg>
            )}
          </button>
        </div>
        <p className="mt-1.5 flex items-center gap-2 font-mono text-[9.5px] text-faint">
          <span>Shift + Enter 换行</span>
          <span className="text-line-strong">·</span>
          <span>成员回 [PASS] 表示这轮不发言</span>
          {busy && (
            <>
              <span className="text-line-strong">·</span>
              <span className="text-warn">当前轮次进行中</span>
            </>
          )}
        </p>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- #
// Bot 名册
// --------------------------------------------------------------------------- #

function BotRoster({
  boot,
  onNew,
  onEdit,
  onDelete,
}: {
  boot: TeamBoot | null;
  onNew: () => void;
  onEdit: (bot: TeamBot) => void;
  onDelete: (bot: TeamBot) => void;
}) {
  const bots = boot?.bots ?? [];
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-[980px] px-6 py-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-[18px] font-semibold tracking-tight">Bot 名册</h1>
            <p className="mt-1 max-w-xl text-[12.5px] leading-relaxed text-muted">
              每个 Bot 绑定自己的模型、工作区与权限模式。团队里的每次发言仍是一条普通任务，
              审批、事件与用量都可以按任务回查。
            </p>
          </div>
          <Button variant="primary" onClick={onNew} disabled={!boot || bots.length >= (boot?.limits.max_bots ?? 8)}>
            新建 Bot
          </Button>
        </div>

        {!boot ? (
          <StateLine>读取名册…</StateLine>
        ) : (
          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            {bots.map((bot) => (
              <article
                key={bot.id}
                className={`rounded-xl border bg-surface px-4 py-3.5 transition-colors ${
                  bot.is_admin ? "border-brand/30" : "border-line hover:border-line-strong"
                }`}
              >
                <div className="flex items-start gap-3">
                  <Avatar name={bot.name} admin={bot.is_admin} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <h2 className="text-[13.5px] font-semibold">{bot.name}</h2>
                      {bot.is_admin && (
                        <span className="rounded bg-brand/[0.08] px-1.5 py-0.5 text-[9.5px] font-medium text-brand">
                          主理人
                        </span>
                      )}
                      {!bot.enabled && (
                        <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[9.5px] text-faint">已停用</span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[11.5px] text-muted">{bot.role || "没有写角色"}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      onClick={() => onEdit(bot)}
                      className="rounded-md px-2 py-1 text-[11px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
                    >
                      编辑
                    </button>
                    {!bot.is_admin && (
                      <button
                        type="button"
                        onClick={() => onDelete(bot)}
                        className="rounded-md px-2 py-1 text-[11px] text-faint transition-colors hover:bg-danger/[0.06] hover:text-danger"
                      >
                        删除
                      </button>
                    )}
                  </div>
                </div>

                <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
                  <div className="col-span-2 flex min-w-0 items-center gap-1.5">
                    <dt className="shrink-0 text-faint">模型</dt>
                    <dd className="min-w-0 truncate text-muted">
                      {bot.provider_name ? `${bot.provider_name} · ${bot.model}` : "跟随默认供应商"}
                    </dd>
                  </div>
                  <div className="col-span-2 flex min-w-0 items-center gap-1.5">
                    <dt className="shrink-0 text-faint">工作区</dt>
                    <dd className="min-w-0 truncate font-mono text-[10.5px] text-muted" title={bot.workspace_path}>
                      {bot.workspace_path || "跟随项目当前工作区"}
                    </dd>
                  </div>
                  <div className="flex min-w-0 items-center gap-1.5">
                    <dt className="shrink-0 text-faint">权限</dt>
                    <dd className="truncate text-muted">{MODE_LABEL[bot.permission_mode] ?? bot.permission_mode}</dd>
                  </div>
                  <div className="flex min-w-0 items-center gap-1.5">
                    <dt className="shrink-0 text-faint">步数</dt>
                    <dd className="text-muted">最多 {bot.max_steps} 步</dd>
                  </div>
                </dl>

                {bot.instructions && (
                  <p className="mt-2.5 line-clamp-2 rounded-lg bg-surface-2/60 px-2.5 py-1.5 text-[11px] leading-relaxed text-faint">
                    {bot.instructions}
                  </p>
                )}
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 编辑器
// --------------------------------------------------------------------------- #

function BotEditor({
  boot,
  bot,
  onClose,
  onSaved,
}: {
  boot: TeamBoot;
  bot: TeamBot | null;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [form, setForm] = useState<BotInput>({
    name: bot?.name ?? "",
    role: bot?.role ?? "",
    instructions: bot?.instructions ?? "",
    provider_id: bot?.provider_id ?? "",
    workspace_id: bot?.workspace_id ?? "",
    permission_mode: bot?.permission_mode ?? "default",
    max_steps: bot?.max_steps ?? 8,
    enabled: bot?.enabled ?? true,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const admin = bot?.is_admin === true;

  const workspaces = useMemo(
    () =>
      boot.projects.flatMap((project) =>
        project.workspaces.map((workspace) => ({
          id: workspace.id,
          label: `${project.name} / ${workspace.label || workspace.path}`,
        })),
      ),
    [boot.projects],
  );

  async function save() {
    if (!form.name.trim()) {
      setError("名字不能为空");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (bot) {
        await updateTeamBot(bot.id, {
          name: admin ? undefined : form.name.trim(),
          role: form.role,
          instructions: form.instructions,
          provider_id: form.provider_id || null,
          workspace_id: form.workspace_id || null,
          permission_mode: form.permission_mode,
          max_steps: form.max_steps,
          enabled: admin ? undefined : form.enabled,
        });
      } else {
        await createTeamBot({ ...form, name: form.name.trim() });
      }
      await onSaved();
    } catch (saveError) {
      setError(errorMessage(saveError));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={bot ? `编辑 ${bot.name}` : "新建 Bot"}
      subtitle={admin ? "主理人固定为小白管家：可以换模型与工作区，但不能改名、停用或删除。" : "给这个 Bot 一个能被人记住的角色，它会在群聊里按这个身份说话。"}
      onClose={onClose}
    >
      <div className="flex flex-col gap-3">
        {error && <Notice>{error}</Notice>}
        <div className="flex gap-3">
          <Field label="名字（用于 @）">
            <Input
              value={form.name}
              disabled={admin}
              onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
              placeholder="后端"
            />
          </Field>
          <Field label="角色">
            <Input
              value={form.role ?? ""}
              onChange={(event) => setForm((current) => ({ ...current, role: event.target.value }))}
              placeholder="服务端与接口"
            />
          </Field>
        </div>

        <div className="flex gap-3">
          <Field label="模型供应商">
            <select
              value={form.provider_id ?? ""}
              onChange={(event) => setForm((current) => ({ ...current, provider_id: event.target.value }))}
              className={selectClass}
            >
              <option value="">跟随默认供应商</option>
              {boot.providers
                .filter((provider) => provider.enabled)
                .map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.name} · {provider.model}
                  </option>
                ))}
            </select>
          </Field>
          <Field label="权限模式">
            <select
              value={form.permission_mode}
              onChange={(event) =>
                setForm((current) => ({ ...current, permission_mode: event.target.value as PermissionMode }))
              }
              className={selectClass}
            >
              {(Object.keys(MODE_LABEL) as PermissionMode[]).map((mode) => (
                <option key={mode} value={mode}>
                  {MODE_LABEL[mode]}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="工作区">
          <select
            value={form.workspace_id ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, workspace_id: event.target.value }))}
            className={selectClass}
          >
            <option value="">跟随项目当前工作区</option>
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.label}
              </option>
            ))}
          </select>
        </Field>

        <div className="flex gap-3">
          <Field label="最多步数">
            <Input
              type="number"
              min={1}
              max={200}
              value={form.max_steps ?? 8}
              onChange={(event) =>
                setForm((current) => ({ ...current, max_steps: Number(event.target.value) || 1 }))
              }
            />
          </Field>
          <Field label="状态">
            <select
              value={form.enabled ? "on" : "off"}
              disabled={admin}
              onChange={(event) => setForm((current) => ({ ...current, enabled: event.target.value === "on" }))}
              className={selectClass}
            >
              <option value="on">启用</option>
              <option value="off">停用（不再被派活）</option>
            </select>
          </Field>
        </div>

        <Field label="工作说明">
          <textarea
            rows={4}
            value={form.instructions ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, instructions: event.target.value }))}
            placeholder="这个 Bot 负责什么、有哪些约定、交付前要做什么检查…"
            className="w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] leading-relaxed placeholder:text-faint focus:border-brand/60 focus:outline-none"
          />
        </Field>

        <div className="mt-1 flex justify-end gap-2">
          <Button onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" onClick={() => void save()} disabled={busy}>
            {busy ? <Spinner /> : null}
            {bot ? "保存" : "创建"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function GroupEditor({
  boot,
  group,
  initialMemberIds,
  onClose,
  onSaved,
}: {
  boot: TeamBoot;
  group: TeamGroupSummary | null;
  initialMemberIds: string[];
  onClose: () => void;
  onSaved: (next: TeamGroupState | null) => Promise<void>;
}) {
  const [form, setForm] = useState<GroupInput>({
    project_id: group?.project_id ?? boot.projects[0]?.id ?? "",
    name: group?.name ?? "",
    topic: group?.topic ?? "",
    member_ids: initialMemberIds,
    max_speakers: group?.max_speakers ?? 5,
    token_budget: group?.token_budget ?? boot.limits.default_token_budget,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const members = form.member_ids ?? [];
  const candidates = boot.bots.filter((bot) => !bot.is_admin);

  function toggleMember(botId: string) {
    setForm((current) => {
      const currentIds = current.member_ids ?? [];
      return {
        ...current,
        member_ids: currentIds.includes(botId)
          ? currentIds.filter((id) => id !== botId)
          : [...currentIds, botId].slice(0, boot.limits.max_members),
      };
    });
  }

  async function save() {
    if (!form.name.trim()) {
      setError("群名不能为空");
      return;
    }
    if (!form.project_id) {
      setError("必须先有一个项目");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const next = group
        ? await updateTeamGroup(group.id, {
            name: form.name.trim(),
            topic: form.topic,
            member_ids: form.member_ids,
            max_speakers: form.max_speakers,
            token_budget: form.token_budget,
          })
        : await createTeamGroup({ ...form, name: form.name.trim() });
      await onSaved(next);
    } catch (saveError) {
      setError(errorMessage(saveError));
      setBusy(false);
    }
  }

  async function remove() {
    if (!group) return;
    const ok = window.confirm(`删除「${group.name}」？群消息与轮次会一起删除。`);
    if (!ok) return;
    setBusy(true);
    try {
      await deleteTeamGroup(group.id);
      await onSaved(null);
    } catch (removeError) {
      setError(errorMessage(removeError));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={group ? `设置 ${group.name}` : "新建群"}
      subtitle="主理人小白管家会自动入群；其余成员最多 4 位。每一轮达到发言数或 token 预算就会收口。"
      onClose={onClose}
    >
      <div className="flex flex-col gap-3">
        {error && <Notice>{error}</Notice>}
        <div className="flex gap-3">
          <Field label="群名">
            <Input
              value={form.name}
              onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
              placeholder="交付群"
            />
          </Field>
          <Field label="项目">
            <select
              value={form.project_id}
              disabled={Boolean(group)}
              onChange={(event) => setForm((current) => ({ ...current, project_id: event.target.value }))}
              className={selectClass}
            >
              {boot.projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="群主题 / 长期目标">
          <textarea
            rows={2}
            value={form.topic ?? ""}
            onChange={(event) => setForm((current) => ({ ...current, topic: event.target.value }))}
            placeholder="这个群长期负责什么？主理人会把它带进每一轮上下文。"
            className="w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] leading-relaxed placeholder:text-faint focus:border-brand/60 focus:outline-none"
          />
        </Field>

        <div>
          <p className="font-mono text-[10px] tracking-wide text-faint uppercase">成员</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {candidates.length === 0 && (
              <p className="text-[11.5px] text-faint">还没有可加入的 Bot，先去名册建一个。</p>
            )}
            {candidates.map((bot) => {
              const active = members.includes(bot.id);
              return (
                <button
                  key={bot.id}
                  type="button"
                  onClick={() => toggleMember(bot.id)}
                  className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11.5px] transition-colors ${
                    active
                      ? "border-brand/50 bg-brand/[0.07] text-brand"
                      : "border-line bg-surface-2/60 text-muted hover:border-line-strong hover:text-text"
                  }`}
                >
                  <Avatar name={bot.name} size="sm" />
                  <span>{bot.name}</span>
                  {!bot.enabled && <span className="text-[9px] text-faint">停用</span>}
                </button>
              );
            })}
          </div>
          <p className="mt-1 font-mono text-[9.5px] text-faint">
            已选 {members.length} / {boot.limits.max_members}
          </p>
        </div>

        <div className="flex gap-3">
          <Field label="每轮最多发言">
            <Input
              type="number"
              min={1}
              max={boot.limits.max_speakers}
              value={form.max_speakers ?? 5}
              onChange={(event) =>
                setForm((current) => ({ ...current, max_speakers: Number(event.target.value) || 1 }))
              }
            />
          </Field>
          <Field label="每轮 token 预算">
            <Input
              type="number"
              min={boot.limits.min_token_budget}
              max={boot.limits.max_token_budget}
              step={1000}
              value={form.token_budget ?? boot.limits.default_token_budget}
              onChange={(event) =>
                setForm((current) => ({ ...current, token_budget: Number(event.target.value) || 0 }))
              }
            />
          </Field>
        </div>

        <div className="mt-1 flex items-center justify-between gap-2">
          {group ? (
            <Button variant="danger" onClick={() => void remove()} disabled={busy}>
              删除群
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <Button onClick={onClose} disabled={busy}>
              取消
            </Button>
            <Button variant="primary" onClick={() => void save()} disabled={busy}>
              {busy ? <Spinner /> : null}
              {group ? "保存" : "创建"}
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
