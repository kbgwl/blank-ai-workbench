"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Notice } from "@/components/projects/ui";
import {
  cancelTask,
  createTask,
  decideApproval,
  errorMessage,
  isMissingEndpoint,
  fetchEvidence,
  fetchProjects,
  fetchProviders,
  fetchTask,
  fetchTasks,
  runTask,
  type Evidence,
  type Project,
  type Provider,
  type Task,
  type TaskDraft,
} from "./api";
import { DiffPanel } from "./diff-panel";
import { EvidencePanel } from "./evidence-panel";
import { FilePanel } from "./file-panel";
import { TaskConsole } from "./task-console";
import { TaskListPane } from "./task-list-pane";
import { buildBlocks, type Block } from "./timeline";
import { TimelineBlock } from "./timeline-blocks";
import { useTaskEvents } from "./use-task-events";

type Props = {
  initialTaskId?: string | null;
};

type ActionKind = "run" | "cancel" | "approve" | "deny";
type RightTab = "files" | "diff" | "terminal" | "evidence";

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
  const detailSeq = useRef(0);

  const { events, mode, notice, revision, reload } = useTaskEvents(selectedId, task?.status ?? null);

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

  /** 审批：POST /tasks/{id}/approval，批准会从断点继续，驳回会保留已完成的步骤并置为 paused。 */
  const decide = useCallback(
    async (decision: "approve" | "deny", block?: Extract<Block, { kind: "approval" }>) => {
      if (!selectedId) return;
      setBusyAction(decision);
      setActionError(null);
      if (block) setApprovalKey(block.key);
      try {
        await decideApproval(selectedId, { decision });
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

  const onCreate = useCallback(
    async (draft: TaskDraft, autoRun: boolean): Promise<Task | null> => {
      setActionError(null);
      const created = await createTask(draft.projectId, draft);
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

  const onAction = useCallback(
    async (action: ActionKind, block?: Extract<Block, { kind: "approval" }>) => {
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
        await decide(action, block);
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
      />

      <div className="min-h-0 min-w-0 overflow-y-auto pr-1">
        {actionError && <Notice>{actionError}</Notice>}
        <TaskConsole
          task={task}
          detailError={detailError}
          events={events}
          mode={mode}
          notice={notice}
          actionError={null}
          busyAction={busyAction}
          approvalKey={approvalKey}
          onHighlight={highlightChange}
          onAction={(action, block) => void onAction(action, block)}
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
            {rightBlocks.filter((block) => block.kind === "terminal").map((block) => <TimelineBlock key={block.key} block={block} busyApprovalKey={null} onHighlight={() => undefined} onDecide={() => undefined} />)}
            {rightBlocks.every((block) => block.kind !== "terminal") && <div className="py-8 text-center font-mono text-[11px] text-faint">还没有终端输出</div>}
          </div>}
        </div>
      </aside>
    </div>
  );
}
