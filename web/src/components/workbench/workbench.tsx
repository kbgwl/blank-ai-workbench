"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  answerTask,
  cancelTask,
  createTask,
  decideApproval,
  errorMessage,
  fetchEvidence,
  fetchProjects,
  fetchProviders,
  fetchTask,
  fetchTasks,
  isTerminalStatus,
  runTask,
  steerTask,
  type ApprovalScope,
  type Evidence,
  type Project,
  type Provider,
  type Task,
  type TaskDraft,
} from "@/components/tasks/api";
import { statusMeta } from "@/components/tasks/status";
import { diffStats, formatDateTime, formatNumber } from "@/components/tasks/format";
import { buildBlocks, type Block } from "@/components/tasks/timeline";
import { TimelineBlock } from "@/components/tasks/timeline-blocks";
import { DiffPanel } from "@/components/tasks/diff-panel";
import { EvidencePanel } from "@/components/tasks/evidence-panel";
import { useTaskEvents } from "@/components/tasks/use-task-events";
import { QUIET_RULE, useScene } from "@/components/scenes/scenes";
import { HealthPill } from "@/components/health-pill";

type RightTab = "diff" | "evidence" | "activity";
type Busy = "run" | "cancel" | "decide" | "answer" | "send" | null;

const STEERABLE = new Set(["running", "queued", "waiting_approval", "paused"]);

/** 任务提示词里带着执行规则和场景说明，界面上只展示用户真正写的那句。 */
function userText(prompt: string): string {
  if (!prompt) return "";
  const marker = prompt.lastIndexOf("用户任务：");
  const tail = marker >= 0 ? prompt.slice(marker + "用户任务：".length) : prompt;
  return tail
    .split("\n")
    .filter((line) => !line.startsWith("执行规则：") && !line.trim().startsWith("你处于"))
    .join("\n")
    .trim();
}

function Dot({ status }: { status: string }) {
  const tone = statusMeta(status);
  return <span className={`size-1.5 shrink-0 rounded-full ${tone.dot.replace("animate-pulse", "")}`} />;
}

export function Workbench() {
  const { scene } = useScene();
  const [projects, setProjects] = useState<Project[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [tab, setTab] = useState<RightTab>("diff");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [focusPath, setFocusPath] = useState<string | null>(null);
  const [approvalKey, setApprovalKey] = useState<string | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);
  const detailSeq = useRef(0);

  const { events, mode, notice: liveNotice, revision, reload } = useTaskEvents(selectedId, task?.status ?? null);

  const loadTasks = useCallback(() => {
    return fetchTasks()
      .then((rows) => {
        setTasks(rows);
        setSelectedId((current) => {
          if (current && rows.some((item) => item.id === current)) return current;
          return rows[0]?.id ?? null;
        });
      })
      .catch((error: unknown) => {
        setTasks((current) => current ?? []);
        setNotice(errorMessage(error));
      });
  }, []);

  useEffect(() => {
    fetchProjects().then(setProjects).catch(() => setProjects([]));
    fetchProviders().then(setProviders).catch(() => setProviders([]));
    void loadTasks();
  }, [loadTasks]);

  const loadDetail = useCallback((taskId: string | null) => {
    if (!taskId) {
      setTask(null);
      setEvidence(null);
      setEvidenceError(null);
      return Promise.resolve();
    }
    const seq = ++detailSeq.current;
    return Promise.allSettled([fetchTask(taskId), fetchEvidence(taskId)]).then(([taskResult, evidenceResult]) => {
      if (seq !== detailSeq.current) return;
      if (taskResult.status === "fulfilled") {
        setTask(taskResult.value);
      } else {
        setTask(null);
        setNotice(errorMessage(taskResult.reason));
      }
      if (evidenceResult.status === "fulfilled") {
        setEvidence(evidenceResult.value);
        setEvidenceError(null);
      } else {
        setEvidence(null);
        setEvidenceError(errorMessage(evidenceResult.reason));
      }
    });
  }, []);

  useEffect(() => {
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  useEffect(() => {
    if (!selectedId || revision === 0) return;
    const id = setTimeout(() => void loadDetail(selectedId), 300);
    return () => clearTimeout(id);
  }, [revision, selectedId, loadDetail]);

  const project = useMemo(() => {
    if (task) return projects.find((item) => item.id === task.projectId) ?? projects[0] ?? null;
    return projects.find((item) => item.workspaces.length > 0) ?? projects[0] ?? null;
  }, [projects, task]);

  const provider = useMemo(() => {
    if (task?.providerId) return providers.find((item) => item.id === task.providerId) ?? null;
    return providers.find((item) => item.enabled) ?? providers[0] ?? null;
  }, [providers, task]);

  const blocks = useMemo(() => buildBlocks(events), [events]);
  const filteredTasks = useMemo(() => {
    const list = tasks ?? [];
    const keyword = query.trim().toLowerCase();
    if (!keyword) return list;
    return list.filter((item) => item.title.toLowerCase().includes(keyword));
  }, [tasks, query]);

  const terminal = task ? isTerminalStatus(task.status) : true;
  const steerable = Boolean(task && STEERABLE.has(task.status));

  const refresh = useCallback(
    (taskId: string | null) => {
      reload();
      void loadTasks();
      void loadDetail(taskId);
    },
    [loadDetail, loadTasks, reload],
  );

  const onSend = useCallback(async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy("send");
    setNotice(null);
    try {
      if (task && steerable) {
        await steerTask(task.id, text);
        setDraft("");
        setNotice("已插话：排在下一步生效，已经跑完的动作不会被撤回。");
        refresh(task.id);
        return;
      }
      const targetProject = project;
      if (!targetProject) {
        setNotice("还没有可用的工作区，先到「项目」页绑定一个。");
        return;
      }
      const promptText = `${QUIET_RULE}\n${scene.instruction}\n\n用户任务：${text}`;
      const payload: TaskDraft = {
        projectId: targetProject.id,
        providerId: provider?.id,
        title: text.split("\n")[0].slice(0, 60),
        prompt: promptText,
        acceptanceCriteria: scene.defaultCriteria,
      };
      const created = await createTask(targetProject.id, payload);
      setTasks((current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
      setSelectedId(created.id);
      setTask(created);
      setDraft("");
      await runTask(created.id).catch((error: unknown) => setNotice(`任务已创建，但启动失败：${errorMessage(error)}`));
      refresh(created.id);
    } catch (error: unknown) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }, [busy, draft, project, provider, refresh, scene.defaultCriteria, scene.instruction, steerable, task]);

  const onRun = useCallback(async () => {
    if (!selectedId) return;
    setBusy("run");
    setNotice(null);
    try {
      await runTask(selectedId);
      refresh(selectedId);
    } catch (error: unknown) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }, [refresh, selectedId]);

  const onCancel = useCallback(async () => {
    if (!selectedId) return;
    setBusy("cancel");
    try {
      await cancelTask(selectedId);
      refresh(selectedId);
    } catch (error: unknown) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }, [refresh, selectedId]);

  const onDecide = useCallback(
    async (decision: "approve" | "deny", block: Extract<Block, { kind: "approval" }>, scope: ApprovalScope = "once") => {
      if (!selectedId) return;
      setBusy("decide");
      setApprovalKey(block.key);
      try {
        await decideApproval(selectedId, { decision, scope });
        refresh(selectedId);
      } catch (error: unknown) {
        setNotice(errorMessage(error));
      } finally {
        setBusy(null);
        setApprovalKey(null);
      }
    },
    [refresh, selectedId],
  );

  const onAnswer = useCallback(
    async (answers: Record<string, string>, block: Extract<Block, { kind: "question" }>) => {
      if (!selectedId) return;
      setBusy("answer");
      setApprovalKey(block.key);
      try {
        await answerTask(selectedId, answers);
        refresh(selectedId);
      } catch (error: unknown) {
        setNotice(errorMessage(error));
      } finally {
        setBusy(null);
        setApprovalKey(null);
      }
    },
    [refresh, selectedId],
  );

  const newTask = useCallback(() => {
    setSelectedId(null);
    setTask(null);
    setEvidence(null);
    setDraft("");
    setNotice(null);
    composer.current?.focus();
  }, []);

  const hasNoWorkspace = projects.length === 0;

  return (
    <div className="grid h-full min-h-0 grid-cols-[240px_minmax(0,1fr)_300px] bg-bg text-text">
      {/* ── 左栏 ── */}
      <aside className="flex min-h-0 flex-col border-r border-line bg-sidebar">
        <div className="flex items-center gap-2 px-3 pt-3 pb-2">
          <span className="grid size-6 place-items-center rounded-[7px] bg-[#1b2a4a]">
            <span className="size-2.5 rounded-[2px] bg-[#5eead4]" />
          </span>
          <span className="text-[13.5px] font-semibold tracking-tight">空白格</span>
        </div>
        <button
          type="button"
          onClick={newTask}
          className="mx-3 mb-2 flex items-center gap-2 rounded-lg border border-line-strong bg-surface px-2.5 py-2 text-left text-[13px] font-medium hover:bg-surface-2"
        >
          <Icon d="M12 5v14M5 12h14" />
          新的任务
        </button>
        <div className="mx-3 mb-2 flex items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[12.5px] text-faint">
          <Icon d="M11 11a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM20 20l-3.5-3.5" box={24} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索任务与文件"
            className="min-w-0 flex-1 bg-transparent text-text outline-none placeholder:text-faint"
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          <Group>工作区</Group>
          {projects.length === 0 && <p className="px-2 py-1 text-[11.5px] text-faint">还没有工作区</p>}
          {projects.map((item) => {
            const active = project?.id === item.id;
            return (
              <Link
                key={item.id}
                href="/projects"
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] ${active ? "bg-surface-3 font-semibold text-text" : "text-muted hover:bg-surface-3 hover:text-text"}`}
              >
                <Icon d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
                <span className="truncate">{item.workspaces[0]?.label || item.name}</span>
              </Link>
            );
          })}

          <Group>最近任务</Group>
          {tasks === null && <p className="px-2 py-1 text-[11.5px] text-faint">读取中…</p>}
          {tasks !== null && filteredTasks.length === 0 && (
            <p className="px-2 py-1 text-[11.5px] text-faint">{query ? "没有匹配的任务" : "还没有任务，说一句就开始"}</p>
          )}
          {filteredTasks.map((item) => {
            const active = item.id === selectedId;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSelectedId(item.id)}
                title={item.title}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] ${active ? "bg-surface-3 font-semibold text-text" : "text-muted hover:bg-surface-3 hover:text-text"}`}
              >
                <span className="min-w-0 flex-1 truncate">{item.title}</span>
                <Dot status={item.status} />
              </button>
            );
          })}
        </div>

        <div className="border-t border-line px-2 py-2">
          <RailLink href="/tasks" label="全部任务" d="M4 6h16M4 12h16M4 18h10" />
          <RailLink href="/reverse-lab" label="逆向实验室" d="m10 8-4 4 4 4M14 8l4 4-4 4" />
          <RailLink href="/projects" label="项目与工作区" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
          <div className="my-1.5 h-px bg-line" />
          <RailLink href="/settings" label="设置" d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4 0a1.7 1.7 0 0 0 .3 1.9 2 2 0 1 1-2.8 2.8 1.7 1.7 0 0 0-2.9 1.2 2 2 0 0 1-4 0 1.7 1.7 0 0 0-1.9-1.2 2 2 0 1 1-2.8-2.8A1.7 1.7 0 0 0 4.6 15a2 2 0 0 1 0-4 1.7 1.7 0 0 0 1.2-1.1 1.7 1.7 0 0 0-.3-1.9 2 2 0 1 1 2.8-2.8A1.7 1.7 0 0 0 11 4.6a2 2 0 0 1 4 0 1.7 1.7 0 0 0 2.9 1.2 2 2 0 1 1 2.8 2.8 1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1 2 2 0 0 1 0 4z" />
          <p className="truncate px-2 pt-1 text-[10.5px] text-faint">
            {scene.name}场景 · {provider?.model || "未配置模型"}
          </p>
          <p className="px-2 pt-0.5">
            <HealthPill compact />
          </p>
        </div>
      </aside>

      {/* ── 中栏 ── */}
      <main className="flex min-h-0 min-w-0 flex-col bg-canvas">
        <header className="flex h-[47px] shrink-0 items-center gap-2.5 border-b border-line bg-bg px-5">
          <h1 className="min-w-0 truncate text-[13.5px] font-semibold">{task?.title ?? "新的任务"}</h1>
          {task && (
            <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${statusMeta(task.status).chip}`}>
              {statusMeta(task.status).label}
            </span>
          )}
          <span className="ml-auto flex shrink-0 items-center gap-2">
            {task && !terminal && (
              <button type="button" onClick={() => void onCancel()} disabled={busy !== null} className={ghostBtn}>
                取消
              </button>
            )}
            {task && (terminal || task.status === "draft" || task.status === "paused") && (
              <button type="button" onClick={() => void onRun()} disabled={busy !== null} className={primaryBtn}>
                {busy === "run" ? "启动中…" : task.status === "draft" ? "运行" : task.status === "paused" ? "继续" : "重试"}
              </button>
            )}
          </span>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto py-6">
          <div className="mx-auto w-full max-w-[720px] px-6">
            {(liveNotice || notice) && (
              <div className="mb-4 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-[12px] text-warn">
                {notice ?? liveNotice}
              </div>
            )}

            {!task ? (
              <div className="flex min-h-[44vh] flex-col items-center justify-center text-center">
                <div className="grid size-10 place-items-center rounded-xl bg-brand/10">
                  <span className="size-3 rounded-[3px] bg-brand" />
                </div>
                <h2 className="mt-4 text-[17px] font-semibold">今天要做什么？</h2>
                <p className="mt-2 max-w-md text-[12.5px] leading-relaxed text-muted">
                  直接说目标，Agent 在{scene.name}场景下执行；下面输入框按 Enter 会创建并立刻跑起来。
                </p>
                {hasNoWorkspace && <p className="mt-3 text-[11.5px] text-faint">还没有工作区，先去「项目」绑定一个目录。</p>}
              </div>
            ) : (
              <>
                <Message who="你" time={task.createdAt}>
                  <div className="inline-block max-w-[88%] rounded-[10px] bg-surface-2 px-3.5 py-2.5 text-[13.5px] leading-relaxed whitespace-pre-wrap">
                    {userText(task.prompt) || task.title}
                  </div>
                </Message>

                <Message who="空白格" agent time={task.createdAt}>
                  {blocks.length === 0 ? (
                    <p className="text-[13px] leading-relaxed text-muted">
                      {terminal && !task.resultSummary ? "还没有执行记录。" : "已接收，正在准备执行计划…"}
                    </p>
                  ) : (
                    <div className="flex flex-col gap-2.5">
                      {blocks.map((block) => (
                        <TimelineBlock
                          key={block.key}
                          block={block}
                          busyApprovalKey={approvalKey}
                          onHighlight={setFocusPath}
                          onDecide={(decision, target, scope) => void onDecide(decision, target, scope)}
                          onAnswer={(answers, target) => void onAnswer(answers, target)}
                        />
                      ))}
                    </div>
                  )}
                </Message>
              </>
            )}
          </div>
        </div>

        <div className="shrink-0 border-t border-line bg-bg py-3">
          <div className="mx-auto w-full max-w-[720px] px-6">
            <div className="rounded-[10px] border border-line-strong bg-surface p-2.5 shadow-[0_1px_2px_rgba(16,24,40,0.04)] focus-within:border-brand">
              <textarea
                ref={composer}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void onSend();
                  }
                }}
                rows={1}
                placeholder={steerable ? "继续说 —— 补充要求、追问，或改方向…" : "描述要做的事，Enter 开始执行"}
                className="max-h-40 min-h-[22px] w-full resize-none bg-transparent text-[13.5px] leading-relaxed outline-none placeholder:text-faint"
              />
              <div className="mt-2 flex items-center gap-1.5">
                <Chip>{project?.workspaces[0]?.label || project?.name || "未绑定工作区"}</Chip>
                <Chip>{provider?.model || "跟随默认模型"}</Chip>
                {steerable && <Chip>插话 · 下一步生效</Chip>}
                <button
                  type="button"
                  onClick={() => void onSend()}
                  disabled={busy !== null || !draft.trim()}
                  title={steerable ? "插话" : "创建并运行"}
                  className="ml-auto grid size-[30px] place-items-center rounded-full bg-brand text-white transition-colors hover:brightness-110 disabled:opacity-40"
                >
                  <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 19V5M5 12l7-7 7 7" />
                  </svg>
                </button>
              </div>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[11px] text-faint">
              <span className="h-1 w-16 overflow-hidden rounded-full bg-surface-2 ring-1 ring-line">
                <span className="block h-full bg-brand" style={{ width: `${contextPercent(evidence)}%` }} />
              </span>
              <span>{tokenLabel(evidence)}</span>
              {task?.stepsUsed !== null && task?.stepsUsed !== undefined && (
                <span>
                  第 {task.stepsUsed} 步{task.maxSteps ? ` / ${task.maxSteps}` : ""}
                </span>
              )}
              <span>{events.length} 条事件</span>
              <span className="ml-auto">{mode === "sse" ? "实时推送" : mode === "polling" ? "轮询同步" : mode === "snapshot" ? "历史快照" : "空闲"}</span>
            </div>
          </div>
        </div>
      </main>

      {/* ── 右栏 ── */}
      <aside className="flex min-h-0 flex-col border-l border-line bg-sidebar">
        <div className="flex h-[47px] shrink-0 items-center gap-1 border-b border-line px-3">
          {([
            ["diff", "变更"],
            ["evidence", "证据"],
            ["activity", "活动"],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`rounded-md px-2.5 py-1.5 text-[12.5px] font-medium transition-colors ${tab === key ? "bg-surface-3 text-text" : "text-faint hover:text-text"}`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
          {!task ? (
            <p className="px-1 py-6 text-[11.5px] leading-relaxed text-faint">选一条任务后，这里显示这次改了哪些文件、证据和全部活动。</p>
          ) : tab === "diff" ? (
            <div className="flex flex-col gap-4">
              <Block label="本次改动">
                {(evidence?.changes.length ?? 0) === 0 ? (
                  <p className="px-1 text-[11.5px] text-faint">还没有文件改动</p>
                ) : (
                  evidence?.changes.map((change) => {
                    const stat = diffStats(change.diff);
                    const label = changeLabel(change.changeType);
                    return (
                      <button
                        key={change.path}
                        type="button"
                        onClick={() => setFocusPath(change.path)}
                        className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-[12.5px] text-muted hover:bg-surface-3 hover:text-text"
                      >
                        <Icon d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6" />
                        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]">{change.path}</span>
                        <span className="shrink-0 font-mono text-[10.5px] text-warn">
                          {stat.added || stat.removed ? `+${stat.added} −${stat.removed}` : label}
                        </span>
                      </button>
                    );
                  })
                )}
              </Block>

              <Block label="证据">
                <Kv k="测试" v={evidence?.test ? evidence.test.status || (evidence.test.passed ? "通过" : "未通过") : "未运行"} />
                <Kv k="验收覆盖" v={`${evidence?.acceptance.filter((item) => item.covered === true).length ?? 0} / ${evidence?.acceptance.length ?? 0}`} />
                <Kv k="已用步数" v={`${task.stepsUsed ?? 0}${task.maxSteps ? ` / ${task.maxSteps}` : ""}`} />
                <Kv k="用量" v={tokenLabel(evidence)} />
              </Block>

              <Block label="运行环境">
                <Kv k="模型" v={provider?.model || task.providerName || "未配置"} mono />
                <Kv k="项目" v={project?.name ?? "未绑定"} />
                <Kv k="任务" v={task.id.slice(0, 8)} mono />
              </Block>

              <div className="border-t border-line pt-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-[11px] font-semibold text-faint">Diff</span>
                  <button type="button" onClick={() => setTab("evidence")} className="text-[11px] text-brand hover:underline">
                    完整证据 →
                  </button>
                </div>
                <DiffPanel changes={evidence?.changes ?? []} focusPath={focusPath} />
              </div>
            </div>
          ) : tab === "evidence" ? (
            <EvidencePanel taskId={task.id} evidence={evidence} error={evidenceError} />
          ) : (
            <div className="flex flex-col">
              <div className="mb-1.5 text-[11px] font-semibold text-faint">全部活动</div>
              {events.length === 0 && <p className="px-1 py-4 text-[11.5px] text-faint">还没有事件</p>}
              {events.map((event) => (
                <div key={`${event.seq}-${event.type}`} className="flex items-start gap-2 border-b border-dashed border-line px-1 py-1.5 text-[11.5px] last:border-0">
                  <span className="w-9 shrink-0 font-mono text-[10.5px] text-faint">#{event.seq}</span>
                  <span className="min-w-0 flex-1 truncate text-muted">{event.type}</span>
                  <span className="shrink-0 text-[10.5px] text-faint">{event.node}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

const primaryBtn =
  "rounded-md bg-brand px-2.5 py-1 text-[12px] font-medium text-white transition-colors hover:brightness-110 disabled:opacity-50";
const ghostBtn =
  "rounded-md border border-line-strong bg-surface px-2.5 py-1 text-[12px] font-medium text-muted transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-50";

const CHANGE_LABELS: Record<string, string> = {
  created: "新增",
  added: "新增",
  modified: "修改",
  deleted: "删除",
  renamed: "重命名",
};

function changeLabel(value: string): string {
  if (!value) return "修改";
  return CHANGE_LABELS[value.toLowerCase()] ?? value;
}

function contextPercent(evidence: Evidence | null): number {
  const total = evidence?.usage.totalTokens;
  if (!total) return 4;
  return Math.min(100, Math.max(4, Math.round((total / 200_000) * 100)));
}

function tokenLabel(evidence: Evidence | null): string {
  const usage = evidence?.usage;
  if (!usage) return "用量 —";
  const total = usage.totalTokens ?? ((usage.promptTokens ?? 0) + (usage.completionTokens ?? 0) || null);
  if (!total) return "用量 —";
  const calls = usage.calls ? ` · ${usage.calls} 次调用` : "";
  return `${formatNumber(total)} tok${calls}`;
}

function Group({ children }: { children: React.ReactNode }) {
  return <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-faint">{children}</div>;
}

function Block({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 text-[11px] font-semibold text-faint">{label}</div>
      <div className="flex flex-col">{children}</div>
    </div>
  );
}

function Kv({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-dashed border-line px-1.5 py-1.5 text-[12.5px] last:border-0">
      <span className="shrink-0 text-faint">{k}</span>
      <span className={`min-w-0 truncate text-right font-medium text-text ${mono ? "font-mono text-[11.5px]" : ""}`} title={v}>
        {v}
      </span>
    </div>
  );
}

function Message({
  who,
  time,
  agent,
  children,
}: {
  who: string;
  time: string;
  agent?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-5">
      <div className="mb-1.5 flex items-center gap-2 text-[12px] font-semibold text-muted">
        <span
          className={`grid size-[18px] place-items-center rounded-[5px] text-[9.5px] font-bold ${agent ? "bg-accent-soft text-brand" : "bg-surface-3 text-muted"}`}
        >
          {agent ? "AI" : "你"}
        </span>
        {who}
        <span className="ml-auto text-[11px] font-normal text-faint">{time ? formatDateTime(time).slice(11) : ""}</span>
      </div>
      <div className="pl-[26px] text-[13.5px] leading-relaxed">{children}</div>
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-2 py-1 text-[11.5px] text-muted">
      {children}
    </span>
  );
}

function RailLink({ href, label, d }: { href: string; label: string; d: string }) {
  return (
    <Link href={href} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] text-muted hover:bg-surface-3 hover:text-text">
      <Icon d={d} />
      <span className="truncate">{label}</span>
    </Link>
  );
}

function Icon({ d, box = 16 }: { d: string; box?: number }) {
  return (
    <svg viewBox={`0 0 ${box} ${box}`} className="size-[14px] shrink-0" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}
