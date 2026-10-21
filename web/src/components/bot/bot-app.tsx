"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  archiveThread,
  answerTask,
  decideApproval,
  errorMessage,
  fetchEvidence,
  fetchTask,
  steerTask,
  type ApprovalScope,
  type Evidence,
  type PendingApproval,
  type PendingQuestion,
  type Project,
  type Provider,
  type Task,
  type Thread,
} from "../tasks/api";
import { useTaskEvents } from "../tasks/use-task-events";
import { NEW_CHAT_EVENT } from "@/lib/commands";
import { loadBoot, loadConversation, loadConversations, sendMessage, stopTask, type BotTurn, type Conversation } from "./bot-api";
import { Composer } from "./composer";
import { TaskPanel } from "./panel";
import { Sidebar } from "./sidebar";
import { TurnView } from "./turn";
import { buildSteps, isTurnRunning, touchedFiles, turnReply } from "./turns";

const SUGGESTIONS = [
  "把这个仓库跑起来，缺什么补什么",
  "找出最近一次失败的测试并修掉",
  "给登录接口加上限流，写清为什么这么限",
  "读一遍 README，指出和代码对不上的地方",
];

const PROVIDER_KEY = "blank.bot.provider";

export function BotApp() {
  const [boot, setBoot] = useState<{ projects: Project[]; providers: Provider[] } | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState("");
  const [providerId, setProviderId] = useState("");

  const [threads, setThreads] = useState<Thread[] | null>(null);
  const [threadsError, setThreadsError] = useState<string | null>(null);
  const [threadsNonce, setThreadsNonce] = useState(0);

  const [conv, setConv] = useState<Conversation | null>(null);
  const [convError, setConvError] = useState<string | null>(null);
  const [loadingConv, setLoadingConv] = useState(false);

  /** 当前要盯住的那一轮。它决定 SSE 连谁、右侧面板显示谁。 */
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [liveTask, setLiveTask] = useState<Task | null>(null);

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [composerHint, setComposerHint] = useState<string | null>(null);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [panelOpen, setPanelOpen] = useState(true);
  const [evidence, setEvidence] = useState<Evidence | null>(null);

  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // ── 启动：项目与供应商 ────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    loadBoot()
      .then((data) => {
        if (!alive) return;
        setBoot(data);
        setProjectId((current) => current || data.projects[0]?.id || "");
        // 存过的模型选择在这里读：不在 useState 初始化里碰 localStorage，
        // 否则服务端渲染出空值、客户端渲染出选中值，hydration 会报不一致。
        try {
          const saved = window.localStorage.getItem(PROVIDER_KEY) ?? "";
          if (saved) setProviderId(saved);
        } catch {
          // 隐私模式下 localStorage 会抛错，忽略即可，用默认模型照样能聊。
        }
      })
      .catch((error: unknown) => {
        if (alive) setBootError(errorMessage(error));
      });
    return () => {
      alive = false;
    };
  }, []);

  // ── 会话列表 ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    loadConversations(projectId)
      .then((rows) => {
        if (!alive) return;
        setThreads(rows);
        setThreadsError(null);
      })
      .catch((error: unknown) => {
        if (!alive) return;
        setThreads([]);
        setThreadsError(`会话列表读取失败：${errorMessage(error)}`);
      });
    return () => {
      alive = false;
    };
  }, [projectId, threadsNonce]);

  // ── 当前轮的事件流 ────────────────────────────────────────────────────
  const activeTurn = conv?.turns.find((turn) => turn.id === activeTaskId) ?? null;
  const liveStatus = liveTask?.id === activeTaskId ? liveTask.status : (activeTurn?.status ?? "");
  const live = useTaskEvents(activeTaskId, activeTaskId ? liveStatus : null);
  const running = Boolean(activeTaskId) && isTurnRunning(liveStatus, live.events);

  // 任务从跑到停：把状态补一次。事件里已经有终态事件，但任务的 status 与失败原因
  // 只有任务详情才有——只靠事件会漏掉"被策略拒绝"这类没有终态事件的情况。
  useEffect(() => {
    if (!activeTaskId || running) return;
    let alive = true;
    fetchTask(activeTaskId)
      .then((task) => {
        if (alive && task) setLiveTask(task);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [activeTaskId, running]);

  // 待审批 / 待回答：只有跑着的时候才有意义，且跟着事件推进刷新。
  useEffect(() => {
    if (!activeTaskId || !running) return;
    let alive = true;
    const timer = setTimeout(() => {
      fetchEvidence(activeTaskId)
        .then((next) => {
          if (alive) setEvidence(next);
        })
        .catch(() => undefined);
    }, 350);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [activeTaskId, running, live.revision]);

  // ── 滚动：贴着底部，除非用户自己往上翻 ─────────────────────────────────
  useEffect(() => {
    const node = scroller.current;
    if (!node || !stick.current) return;
    node.scrollTop = node.scrollHeight;
  }, [live.revision, live.streaming, conv?.turns.length]);

  const onScroll = useCallback(() => {
    const node = scroller.current;
    if (!node) return;
    stick.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
  }, []);

  // ── 打开一条会话 ──────────────────────────────────────────────────────
  const openThread = useCallback((threadId: string) => {
    stick.current = true;
    setConvError(null);
    setLoadingConv(true);
    setEvidence(null);
    setActiveTaskId(null);
    setLiveTask(null);
    // 地址栏跟着走：刷新页面能回到同一条对话，也能把链接直接发给别人。
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("thread", threadId);
      window.history.replaceState(null, "", url);
    } catch {
      // 静态导出下也有可能拿不到完整 URL，忽略即可，不影响打开。
    }
    loadConversation(threadId)
      .then((next) => {
        if (!next) {
          setConvError("后端没有返回这条对话");
          return;
        }
        setConv(next);
        const last = next.turns[next.turns.length - 1];
        setActiveTaskId(last?.id ?? null);
      })
      .catch((error: unknown) => setConvError(errorMessage(error)))
      .finally(() => setLoadingConv(false));
  }, []);

  // 带 ?thread= 进来时直接打开那条对话（深链 / 刷新后回到原处）。
  useEffect(() => {
    if (!projectId) return;
    let target = "";
    try {
      target = new URLSearchParams(window.location.search).get("thread") ?? "";
    } catch {
      target = "";
    }
    if (!target) return;
    // 推迟一个 tick：openThread 上来就会置 loading 状态，而 effect 里同步 setState
    // 会多触发一轮渲染（这条 lint 规则挡的正是它）。放到异步回调里，
    // 就能和随后的请求结果合并成一次更新。
    const timer = setTimeout(() => openThread(target), 0);
    return () => clearTimeout(timer);
  }, [projectId, openThread]);

  const startNew = useCallback(() => {
    setConv(null);
    setConvError(null);
    setActiveTaskId(null);
    setLiveTask(null);
    setEvidence(null);
    setSendError(null);
    setComposerHint(null);
    stick.current = true;
    // 清掉地址栏里的会话 id：否则刷新一下又跳回刚才那条。
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("thread");
      window.history.replaceState(null, "", url);
    } catch {
      // 拿不到 URL 不影响清空。
    }
  }, []);

  // 命令面板里的「新对话」是同路由动作，靠事件回来。
  useEffect(() => {
    const onNew = () => startNew();
    window.addEventListener(NEW_CHAT_EVENT, onNew);
    return () => window.removeEventListener(NEW_CHAT_EVENT, onNew);
  }, [startNew]);

  // ── 发送 ─────────────────────────────────────────────────────────────
  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || sending || !projectId) return;
    setSending(true);
    setSendError(null);
    setActionError(null);
    setComposerHint(null);
    try {
      const result = await sendMessage({ projectId, threadId: conv?.thread.id ?? "", text, providerId });
      const created: BotTurn = {
        id: result.task.id,
        title: result.task.title,
        goal: text,
        status: result.task.status,
        summary: "",
        createdAt: result.task.createdAt,
        events: [],
        eventsLoaded: false,
      };
      setConv((current) =>
        current && current.thread.id === result.thread.id
          ? {
              thread: { ...current.thread, turnCount: current.thread.turnCount + 1 },
              turns: [...current.turns, created],
            }
          : { thread: { ...result.thread, turns: [] }, turns: [created] },
      );
      setActiveTaskId(result.task.id);
      setLiveTask(result.task);
      setEvidence(null);
      setDraft("");
      stick.current = true;
      // 每次都刷新左侧：不只新会话要出现，老会话也要更新"几轮"和"最近活动时间"，
      // 否则左侧列表停在发消息之前的样子，看起来像没发出去。
      setThreadsNonce((value) => value + 1);
      // 地址栏换到这条会话：发完消息刷新页面应该还在原地，而不是回到空态。
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("thread", result.thread.id);
        window.history.replaceState(null, "", url);
      } catch {
        // 拿不到 URL 就算了，不影响这一轮继续跑。
      }
      if (result.runError) setSendError(`消息已经记下，但启动失败：${result.runError}`);
    } catch (error: unknown) {
      setSendError(errorMessage(error));
    } finally {
      setSending(false);
    }
  }, [conv, draft, projectId, providerId, sending]);

  const stop = useCallback(() => {
    if (!activeTaskId) return;
    setActionBusy(true);
    stopTask(activeTaskId)
      .catch((error: unknown) => setActionError(errorMessage(error)))
      .finally(() => setActionBusy(false));
  }, [activeTaskId]);

  /**
   * 运行中插话。后端语义是"从下一次模型调用开始生效"，不撤回已经执行的动作——
   * 所以提示里写的是"已排队"，不写成"已改好"，否则用户会以为刚才那步被撤销了。
   */
  const steer = useCallback(() => {
    const text = draft.trim();
    if (!text || !activeTaskId) return;
    setSendError(null);
    setComposerHint(null);
    steerTask(activeTaskId, text)
      .then((result) => {
        setDraft("");
        setComposerHint(result.queued ? "已经排上了，从它下一次想想的时候开始生效。" : "后端没有确认排队，这句话可能没生效。");
      })
      .catch((error: unknown) => setSendError(errorMessage(error)));
  }, [activeTaskId, draft]);

  const decide = useCallback(
    (approval: PendingApproval, decision: "approve" | "deny", scope: ApprovalScope) => {
      if (!activeTaskId) return;
      setActionBusy(true);
      setActionError(null);
      decideApproval(activeTaskId, { decision, scope })
        .then(() => {
          setEvidence(null);
          live.reload();
        })
        .catch((error: unknown) => setActionError(errorMessage(error)))
        .finally(() => setActionBusy(false));
    },
    [activeTaskId, live],
  );

  const answer = useCallback(
    (questions: PendingQuestion[], answers: Record<string, string>) => {
      if (!activeTaskId) return;
      setActionBusy(true);
      setActionError(null);
      // 只有真的填了答案的问题才提交：把空串一起发回去，等于替用户回答了"空"。
      const payload: Record<string, string> = {};
      for (const question of questions) {
        const value = (answers[question.id] ?? "").trim();
        if (value) payload[question.id] = value;
      }
      answerTask(activeTaskId, payload)
        .then(() => {
          setEvidence(null);
          live.reload();
        })
        .catch((error: unknown) => setActionError(errorMessage(error)))
        .finally(() => setActionBusy(false));
    },
    [activeTaskId, live],
  );

  const archive = useCallback(
    (thread: Thread) => {
      const ok = window.confirm(`归档「${thread.title}」？\n\n它不再接受新的一轮，已有的轮次全部保留。`);
      if (!ok) return;
      archiveThread(thread.id)
        .then(() => {
          if (conv?.thread.id === thread.id) startNew();
          setThreadsNonce((value) => value + 1);
        })
        .catch((error: unknown) => setActionError(errorMessage(error)));
    },
    [conv?.thread.id, startNew],
  );

  const retry = useCallback(() => {
    const last = conv?.turns[conv.turns.length - 1];
    if (!last) return;
    setDraft(last.goal);
  }, [conv]);

  // ── 渲染数据 ─────────────────────────────────────────────────────────
  const currentTurnView = useMemo(() => {
    if (!conv) return [];
    return conv.turns.map((turn) => {
      const isLive = turn.id === activeTaskId;
      const events = isLive && live.events.length > 0 ? live.events : turn.events;
      const steps = buildSteps(events);
      const verdict = turnReply(events);
      const status = isLive ? liveStatus || turn.status : turn.status;
      const isRunning = isLive ? running : false;
      return {
        id: turn.id,
        goal: turn.goal,
        steps,
        reply: verdict.text || turn.summary,
        reason: verdict.reason,
        status,
        running: isRunning,
        // 实时文字只挂在还跑着的那一轮：任务结束后事件里已经有完整回复，
        // 两处同时显示会出现一段重复的话。
        streaming: isLive && isRunning ? live.streaming : "",
        createdAt: turn.createdAt,
        files: touchedFiles(steps),
        stepCount: steps.length,
        approvals: isLive ? (evidence?.pendingApprovals ?? []) : [],
        questions: isLive ? (evidence?.pendingQuestions ?? []) : [],
      };
    });
  }, [conv, activeTaskId, live.events, live.streaming, liveStatus, running, evidence]);

  const lastTurn = currentTurnView[currentTurnView.length - 1] ?? null;
  const activeEvidence = running ? evidence : null;

  return (
    <div className="flex h-dvh overflow-hidden bg-bg">
      <Sidebar
        threads={threads}
        activeId={conv?.thread.id ?? null}
        error={threadsError}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((value) => !value)}
        onNew={startNew}
        onSelect={openThread}
        onArchive={archive}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-11 shrink-0 items-center gap-2 border-b border-line px-4">
          <h1 className="min-w-0 flex-1 truncate text-[13px] font-medium">
            {conv?.thread.title || "新对话"}
          </h1>
          {conv && (
            <span className="shrink-0 font-mono text-[10px] text-faint">{conv.turns.length} 轮</span>
          )}
          <button
            type="button"
            onClick={() => setPanelOpen((value) => !value)}
            title={panelOpen ? "收起任务面板" : "打开任务面板"}
            className={`grid size-7 place-items-center rounded-md transition-colors hover:bg-surface-2 ${
              panelOpen ? "text-text" : "text-faint"
            }`}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-4" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M15 4v16" />
            </svg>
          </button>
        </header>

        <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-4 py-6">
            {bootError && (
              <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[12px] text-danger">
                连不上后端：{bootError}
              </p>
            )}
            {convError && (
              <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[12px] text-danger">
                对话读取失败：{convError}
              </p>
            )}
            {actionError && (
              <p className="mb-4 rounded-lg border border-warn/30 bg-warn/[0.05] px-3 py-2 text-[12px] text-warn">
                {actionError}
              </p>
            )}

            {currentTurnView.length === 0 && !loadingConv && (
              <div className="flex min-h-[56vh] flex-col items-center justify-center text-center">
                <span className="grid size-12 place-items-center rounded-2xl bg-[#1b2a4a]">
                  <span className="size-4 rounded-[4px] bg-[#5eead4]" />
                </span>
                <h2 className="mt-5 text-[24px] font-semibold tracking-tight">今天想做点什么？</h2>
                <p className="mt-2 max-w-lg text-[13.5px] leading-relaxed text-muted">
                  说清楚要什么，它会先读代码再动手。改了什么、为什么改，都会留在对话里。
                </p>
                <div className="mt-8 grid w-full max-w-xl gap-2 sm:grid-cols-2">
                  {SUGGESTIONS.map((text) => (
                    <button
                      key={text}
                      type="button"
                      onClick={() => setDraft(text)}
                      className="rounded-xl border border-line bg-surface px-4 py-3 text-left text-[12.5px] leading-relaxed text-muted transition-colors hover:border-line-strong hover:bg-surface-2 hover:text-text"
                    >
                      {text}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {loadingConv && currentTurnView.length === 0 && (
              <p className="py-10 text-center font-mono text-[11px] text-faint">读取对话…</p>
            )}

            <div className="space-y-8">
              {currentTurnView.map((turn) => (
                <TurnView
                  key={turn.id}
                  goal={turn.goal}
                  steps={turn.steps}
                  reply={turn.reply}
                  reason={turn.reason}
                  status={turn.status}
                  running={turn.running}
                  streaming={turn.streaming}
                  createdAt={turn.createdAt}
                  files={turn.files}
                  stepCount={turn.stepCount}
                  approvals={turn.approvals}
                  questions={turn.questions}
                  actionBusy={actionBusy}
                  onDecide={decide}
                  onAnswer={answer}
                  onStop={stop}
                  onRetry={retry}
                />
              ))}
            </div>

            {live.notice && (
              <p className="mt-4 font-mono text-[10.5px] text-faint">{live.notice}</p>
            )}
          </div>
        </div>

        <Composer
          projects={boot?.projects ?? []}
          providers={boot?.providers ?? []}
          projectId={projectId}
          onProjectChange={(id) => {
            setProjectId(id);
            setThreads(null);
            startNew();
          }}
          providerId={providerId}
          onProviderChange={(id) => {
            setProviderId(id);
            try {
              window.localStorage.setItem(PROVIDER_KEY, id);
            } catch {
              // 存不下就算了，下次用默认模型。
            }
          }}
          draft={draft}
          onDraftChange={setDraft}
          onSend={() => void send()}
          onSteer={steer}
          onStop={stop}
          running={running}
          error={sendError}
          hint={composerHint}
          disabled={(boot !== null && !projectId) || sending}
        />
      </main>

      {panelOpen && lastTurn && (
        <TaskPanel
          title={conv?.thread.title ?? ""}
          status={lastTurn.status}
          running={lastTurn.running}
          steps={lastTurn.steps}
          changes={activeEvidence?.changes ?? evidence?.changes ?? []}
          usage={activeEvidence?.usage ?? evidence?.usage ?? null}
          onClose={() => setPanelOpen(false)}
        />
      )}
    </div>
  );
}
