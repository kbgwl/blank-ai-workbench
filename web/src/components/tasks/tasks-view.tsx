"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Notice } from "@/components/projects/ui";
import {
  answerTask,
  archiveThread,
  cancelTask,
  createTask,
  createThread,
  createTurn,
  decideApproval,
  errorMessage,
  fetchEvidence,
  fetchProjects,
  fetchProviders,
  fetchTask,
  fetchTasks,
  fetchThread,
  forkThread,
  isMissingEndpoint,
  runTask,
  type ApprovalScope,
  type Evidence,
  type Project,
  type Provider,
  type Task,
  type TaskDraft,
  type ThreadDetail,
} from "./api";
import { DiffPanel } from "./diff-panel";
import { EvidencePanel } from "./evidence-panel";
import { FilePanel } from "./file-panel";
import { TaskConsole } from "./task-console";
import { TaskListPane } from "./task-list-pane";
import { ThreadBar } from "./thread-bar";
import { buildBlocks, type Block } from "./timeline";
import { TimelineBlock } from "./timeline-blocks";
import { useTaskEvents } from "./use-task-events";

type Props = {
  initialTaskId?: string | null;
};

type ActionKind = "run" | "cancel" | "approve" | "deny";
type RightTab = "files" | "diff" | "terminal" | "evidence";
/** 新建时选的是「新开一条会话」，不是一个已有会话的 id。 */
const NEW_THREAD = "__new__";

export function TasksView({ initialTaskId = null }: Props) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(initialTaskId);
  const [task, setTask] = useState<Task | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<ActionKind | null>(null);
  const [approvalKey, setApprovalKey] = useState<string | null>(null);
  const [focusPath, setFocusPath] = useState<string | null>(null);
  const [rightTab, setRightTab] = useState<RightTab>("files");
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [threadBusy, setThreadBusy] = useState<null | "continue" | "fork" | "archive">(null);
  const [threadsRevision, setThreadsRevision] = useState(0);
  const detailSeq = useRef(0);

  const { events, mode, notice, revision, streaming, reload } = useTaskEvents(selectedId, task?.status ?? null);

  const loadTasks = useCallback(() => {
    return fetchTasks()
      .then((rows) => {
        setTasks(rows);
        setTasksError(null);
        setSelectedId((current) => {
          if (current && rows.some((item) => item.id === current)) return current;
          return rows[0]?.id ?? null;
        });
      })
      .catch((error: unknown) => {
        setTasks((current) => current ?? []);
        setTasksError(errorMessage(error));
      });
  }, []);

  // 选择器数据：项目与工作区、模型供应商。
  useEffect(() => {
    fetchProjects()
      .then((rows) => {
        setProjects(rows);
        setProjectsError(null);
      })
      .catch((error: unknown) => {
        setProjects([]);
        setProjectsError(errorMessage(error));
      });
    fetchProviders()
      .then((rows) => {
        setProviders(rows);
        setProvidersError(null);
      })
      .catch((error: unknown) => {
        setProviders([]);
        setProvidersError(errorMessage(error));
      });
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const loadDetail = useCallback(
    (taskId: string | null): Promise<void> => {
      if (!taskId) {
        return Promise.resolve().then(() => {
          setTask(null);
          setDetailError(null);
          setEvidence(null);
          setEvidenceError(null);
          // 不清会话：分叉之后就是"还没有选中任务 + 刚分叉出来的会话"这个状态，
          // 清掉会把用户刚建出来的东西从界面上抹掉。
        });
      }
      const seq = ++detailSeq.current;
      return Promise.allSettled([fetchTask(taskId), fetchEvidence(taskId)]).then(([taskResult, evidenceResult]) => {
        if (seq !== detailSeq.current) return;
        if (taskResult.status === "fulfilled") {
          setTask(taskResult.value);
          setDetailError(taskResult.value ? null : "后端没有返回这条任务");
        } else {
          setTask(null);
          setDetailError(errorMessage(taskResult.reason));
        }
        if (evidenceResult.status === "fulfilled") {
          setEvidence(evidenceResult.value);
          setEvidenceError(null);
        } else {
          setEvidence(null);
          setEvidenceError(errorMessage(evidenceResult.reason));
        }
        // 会话跟着任务一起取：一个任务要么属于某条会话、要么不属于，没有第三种。
        // 放在这里而不是单开一个 effect，是因为"当前任务的上下文"本来就该一起刷新——
        // 事件推进时轮次状态会跟着更新，不用手动刷新页面。
        const threadId = taskResult.status === "fulfilled" ? (taskResult.value?.threadId ?? "") : "";
        if (!threadId) {
          setThread(null);
          setThreadError(null);
          return;
        }
        return fetchThread(threadId)
          .then((detail) => {
            if (seq !== detailSeq.current) return;
            setThread(detail);
            setThreadError(detail ? null : "后端没有返回这条会话");
          })
          .catch((error: unknown) => {
            if (seq !== detailSeq.current) return;
            // 会话读失败不把已经拿到的会话丢掉：那会把「分叉」「归档」一起藏起来，
            // 而真正的问题只是这一次读超时。
            setThreadError(errorMessage(error));
          });
      });
    },
    [],
  );

  useEffect(() => {
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // 事件推进后同步刷新任务状态与证明包，页面不需要手动刷新。
  useEffect(() => {
    if (!selectedId || revision === 0) return;
    const id = setTimeout(() => void loadDetail(selectedId), 300);
    return () => clearTimeout(id);
  }, [revision, selectedId, loadDetail]);

  /** 只重读一条会话（归档后用：任务没变，变的是会话状态）。 */
  const loadThread = useCallback((threadId: string): Promise<void> => {
    return fetchThread(threadId)
      .then((detail) => {
        setThread(detail);
        setThreadError(detail ? null : "后端没有返回这条会话");
      })
      .catch((error: unknown) => setThreadError(errorMessage(error)));
  }, []);

  const project = task ? (projects?.find((item) => item.id === task.projectId) ?? null) : (projects?.[0] ?? null);

  const notify = useCallback((message: string) => setActionError(message), []);

  const run = useCallback(
    async (taskId: string, kind: ActionKind) => {
      setBusyAction(kind);
      setActionError(null);
      try {
        await runTask(taskId);
        await loadDetail(taskId);
        reload();
      } catch (error: unknown) {
        notify(errorMessage(error));
      } finally {
        setBusyAction(null);
        setApprovalKey(null);
      }
    },
    [loadDetail, notify, reload],
  );

  /** 审批：POST /tasks/{id}/approval，批准会从断点继续，驳回会保留已完成的步骤并置为 paused。
   *  scope 决定这次批准记多久：once 仅本次、prefix 记住该命令前缀、tool 记住整个工具。 */
  const decide = useCallback(
    async (
      decision: "approve" | "deny",
      block?: Extract<Block, { kind: "approval" }>,
      scope: ApprovalScope = "once",
    ) => {
      if (!selectedId) return;
      setBusyAction(decision);
      setActionError(null);
      if (block) setApprovalKey(block.key);
      try {
        await decideApproval(selectedId, { decision, scope });
        await loadDetail(selectedId);
        reload();
      } catch (error: unknown) {
        notify(
          isMissingEndpoint(error)
            ? "后端未提供审批端点（POST /tasks/{id}/approval），可点「运行」以「批准高风险操作」重试"
            : errorMessage(error),
        );
      } finally {
        setBusyAction(null);
        setApprovalKey(null);
      }
    },
    [loadDetail, notify, reload, selectedId],
  );

  /** 回答 Agent 的提问：POST /tasks/{id}/answer，答案回填后从断点继续。 */
  const answer = useCallback(
    async (
      answers: Record<string, string>,
      block?: Extract<Block, { kind: "question" }>,
    ) => {
      if (!selectedId) return;
      setBusyAction("approve");
      setActionError(null);
      if (block) setApprovalKey(block.key);
      try {
        await answerTask(selectedId, answers);
        await loadDetail(selectedId);
        reload();
        notify("已提交回答，任务继续执行");
      } catch (error: unknown) {
        setActionError(errorMessage(error));
        notify(
          isMissingEndpoint(error)
            ? "后端未提供回答端点（POST /tasks/{id}/answer）"
            : errorMessage(error),
        );
      } finally {
        setBusyAction(null);
        setApprovalKey(null);
      }
    },
    [loadDetail, notify, reload, selectedId],
  );

  const onCreate = useCallback(
    async (draft: TaskDraft, autoRun: boolean): Promise<Task | null> => {
      setActionError(null);
      // 会话里的一轮走 POST /threads/{id}/turns：后端与独立任务共用同一套建行逻辑，
      // 只是顺手带上 thread_id / turn_index。选「新会话」时先开一条空会话，再落第一轮。
      const threadId =
        draft.thread === NEW_THREAD
          ? (await createThread(draft.projectId, { title: draft.title })).id
          : draft.thread;
      const created = threadId ? await createTurn(threadId, draft) : await createTask(draft.projectId, draft);
      if (threadId) setThreadsRevision((value) => value + 1);
      setTasks((current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
      setSelectedId(created.id);
      setTask(created);
      if (autoRun) {
        try {
          await runTask(created.id);
        } catch (error: unknown) {
          notify(`任务已创建，但启动失败：${errorMessage(error)}`);
        }
      }
      await loadDetail(created.id);
      reload();
      return created;
    },
    [loadDetail, notify, reload],
  );

  /** 继续聊：在同一个会话里开新的一轮，并且直接开跑——"接着说"的意思就是要它接着做。 */
  const onContinue = useCallback(
    async (prompt: string) => {
      if (!thread || threadBusy) return;
      setThreadBusy("continue");
      setActionError(null);
      const title = prompt.split("\n")[0]?.trim().slice(0, 60) || `第 ${thread.turnCount + 1} 轮`;
      try {
        const created = await createTurn(thread.id, {
          projectId: thread.projectId,
          title,
          prompt,
          acceptanceCriteria: [],
        });
        setTasks((current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
        setSelectedId(created.id);
        setTask(created);
        setThreadsRevision((value) => value + 1);
        try {
          await runTask(created.id);
        } catch (error: unknown) {
          // 创建成功、启动失败：这一轮还在，用户可以在控制台里手动点运行。
          notify(`这一轮已创建，但启动失败：${errorMessage(error)}`);
        }
        await loadDetail(created.id);
        reload();
      } catch (error: unknown) {
        notify(errorMessage(error));
      } finally {
        setThreadBusy(null);
      }
    },
    [loadDetail, notify, reload, thread, threadBusy],
  );

  /** 分叉：只新建一条会话并记下来源，不复制任何任务——上下文靠 forked_from 回溯。 */
  const onFork = useCallback(async () => {
    if (!thread || threadBusy) return;
    setThreadBusy("fork");
    setActionError(null);
    try {
      const created = await forkThread(thread.id, `${thread.title}（分叉）`);
      setThread({ ...created, turns: [] });
      setThreadError(null);
      // 新会话里还没有轮次，所以清空选中：中间的控制台回到"没有选中任务"，
      // 而不是继续显示来源会话里的那一轮（那会让人以为它属于新会话）。
      setSelectedId(null);
      setThreadsRevision((value) => value + 1);
      notify("已分叉出新会话，接下来这一轮会带上原会话的上下文");
    } catch (error: unknown) {
      notify(errorMessage(error));
    } finally {
      setThreadBusy(null);
    }
  }, [notify, thread, threadBusy]);

  const onArchive = useCallback(async () => {
    if (!thread || threadBusy) return;
    if (!window.confirm(`归档「${thread.title}」？\n归档后不再接受新的一轮，已有轮次全部保留。`)) return;
    setThreadBusy("archive");
    setActionError(null);
    try {
      await archiveThread(thread.id);
      await loadThread(thread.id);
      setThreadsRevision((value) => value + 1);
    } catch (error: unknown) {
      notify(errorMessage(error));
    } finally {
      setThreadBusy(null);
    }
  }, [loadThread, notify, thread, threadBusy]);

  const onAction = useCallback(
    async (
      action: ActionKind,
      block?: Extract<Block, { kind: "approval" }>,
      scope: ApprovalScope = "once",
    ) => {
      if (!selectedId) return;
      if (action === "run") {
        if (task?.status === "waiting_approval") {
          await decide("approve", undefined);
          return;
        }
        await run(selectedId, "run");
        return;
      }
      if (action === "cancel") {
        setBusyAction("cancel");
        setActionError(null);
        try {
          await cancelTask(selectedId);
          await loadDetail(selectedId);
          reload();
        } catch (error: unknown) {
          notify(errorMessage(error));
        } finally {
          setBusyAction(null);
        }
        return;
      }
      if (action === "approve" || action === "deny") {
        await decide(action, block, scope);
      }
    },
    [decide, loadDetail, notify, reload, run, selectedId, task?.status],
  );

  const highlightChange = useCallback((path: string) => {
    setFocusPath(path);
    if (typeof document !== "undefined") {
      document.getElementById("tasks-diff-panel")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, []);

  const rightTabs: { key: RightTab; label: string }[] = [
    { key: "files", label: "文件" },
    { key: "diff", label: "Diff" },
    { key: "terminal", label: "终端" },
    { key: "evidence", label: "证明" },
  ];
  const rightBlocks = buildBlocks(events);

  return (
    <div className="grid h-full min-h-0 min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,17rem)_minmax(0,1fr)_minmax(0,24rem)]">
      <TaskListPane
        projects={projects}
        providers={providers}
        projectsError={projectsError}
        providersError={providersError}
        tasks={tasks}
        tasksError={tasksError}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onCreate={onCreate}
        threadsRevision={threadsRevision}
      />

      <div className="min-h-0 min-w-0 overflow-y-auto pr-1">
        {actionError && <Notice>{actionError}</Notice>}
        <ThreadBar
          thread={thread}
          error={threadError}
          currentTaskId={selectedId}
          busy={threadBusy}
          onOpenTurn={setSelectedId}
          onContinue={(prompt) => void onContinue(prompt)}
          onFork={() => void onFork()}
          onArchive={() => void onArchive()}
        />
        <TaskConsole
          task={task}
          detailError={detailError}
          events={events}
          mode={mode}
          notice={notice}
          actionError={null}
          busyAction={busyAction}
          approvalKey={approvalKey}
          streaming={streaming}
          onHighlight={highlightChange}
          onAction={(action, block, scope) => void onAction(action, block, scope)}
          onAnswer={(answers, block) => void answer(answers, block)}
          onReload={() => {
            reload();
            void loadDetail(selectedId);
            void loadTasks();
          }}
        />
      </div>

      <aside className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-line/80 bg-surface/20">
        <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line/80 px-2">
          {rightTabs.map((tab) => (
            <button key={tab.key} type="button" onClick={() => setRightTab(tab.key)} className={`rounded-md px-2.5 py-1.5 text-[11px] transition-colors ${rightTab === tab.key ? "bg-surface-2 text-text" : "text-muted hover:bg-surface-2/60 hover:text-text"}`}>
              {tab.label}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {rightTab === "files" && <FilePanel project={project} />}
          {rightTab === "diff" && <div id="tasks-diff-panel">{evidenceError ? <Notice>{evidenceError}</Notice> : <DiffPanel changes={evidence?.changes ?? []} focusPath={focusPath} />}</div>}
          {rightTab === "evidence" && <EvidencePanel taskId={selectedId ?? ""} evidence={evidence} error={evidenceError} />}
          {rightTab === "terminal" && <div className="flex flex-col gap-2">
            {rightBlocks.filter((block) => block.kind === "terminal").map((block) => <TimelineBlock key={block.key} block={block} busyApprovalKey={null} onHighlight={() => undefined} onDecide={() => undefined} onAnswer={() => undefined} />)}
            {rightBlocks.every((block) => block.kind !== "terminal") && <div className="py-8 text-center font-mono text-[11px] text-faint">还没有终端输出</div>}
          </div>}
        </div>
      </aside>
    </div>
  );
}
