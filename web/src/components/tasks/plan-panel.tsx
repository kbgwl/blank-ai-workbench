"use client";

/**
 * 任务计划面板：目标 + 里程碑 + 步骤 + 进度。
 *
 * 与时间线里那张 PlanCard 的分工：
 * - PlanCard 是**当时那一刻**的计划快照（事件回放，只读）；
 * - 这个面板是**现在**的计划（落库的那一份，可勾选、可存成模板）。
 *
 * 两条都必须存在。只留快照，用户改不动；只留现状，回放时看不到当时是怎么想的。
 */

import { useMemo, useState } from "react";

import {
  type Task,
  type TaskPlan,
  ApiError,
  errorMessage,
  saveTaskPlanAsTemplate,
  togglePlanStep,
} from "./api";
import { groupByMilestone, statusLabel } from "./plan";

type Props = {
  task: Task;
  /** 计划被改过之后回调。让外面的任务对象跟着刷新，避免两处进度显示不一致。 */
  onChanged?: (plan: TaskPlan) => void;
};

function criteriaOf(spec: Record<string, unknown>, key: string): string[] {
  const value = spec[key];
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean);
  if (typeof value === "string" && value.trim()) return [value];
  return [];
}

export function PlanPanel({ task, onChanged }: Props) {
  const [steps, setSteps] = useState(task.planSteps);
  const [milestones, setMilestones] = useState(task.milestones);
  const [revision, setRevision] = useState(task.planRevision);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  // 本地可编辑状态（勾选走乐观更新）必须跟上外部刷新，但不能无条件覆盖：
  // 只在「换了任务」或「计划版本真的变了」时重置——渲染期条件 setState 是
  // React 官方替代 useEffect 同步的写法，切换任务不会闪现上一个任务的计划，
  // 外部刷新也不会盖掉用户正在编辑而 revision 未变的本地状态。
  const [seen, setSeen] = useState({ taskId: task.id, revision: task.planRevision });
  if (seen.taskId !== task.id || seen.revision !== task.planRevision) {
    setSeen({ taskId: task.id, revision: task.planRevision });
    setSteps(task.planSteps);
    setMilestones(task.milestones);
    setRevision(task.planRevision);
  }

  const progress = useMemo(() => {
    const total = steps.length;
    const completed = steps.filter((step) => step.status === "completed").length;
    return { total, completed, percent: total ? Math.round((completed / total) * 100) : 0 };
  }, [steps]);

  const groups = useMemo(() => groupByMilestone(steps, milestones), [steps, milestones]);

  const objective = String(task.goalSpec.objective ?? "");
  const doneWhen = criteriaOf(task.goalSpec, "done_when");
  const constraints = criteriaOf(task.goalSpec, "constraints");
  const outOfScope = criteriaOf(task.goalSpec, "out_of_scope");

  if (steps.length === 0 && !objective && doneWhen.length === 0) return null;

  const toggle = async (stepId: string, completed: boolean) => {
    setBusy(stepId);
    setError("");
    // 先动本地：勾选要即时反馈，等一次往返会让人以为没点上。
    setSteps((prev) =>
      prev.map((step) =>
        step.id === stepId ? { ...step, status: completed ? "completed" : "pending" } : step,
      ),
    );
    try {
      const plan = await togglePlanStep(task.id, stepId, completed);
      setSteps(plan.steps);
      setMilestones(plan.milestones);
      setRevision(plan.revision);
      onChanged?.(plan);
    } catch (cause) {
      // 失败要把本地那一勾退回去，否则界面在撒谎（后端没改成，看起来却成了）。
      setSteps((prev) =>
        prev.map((step) =>
          step.id === stepId ? { ...step, status: completed ? "pending" : "completed" } : step,
        ),
      );
      setError(cause instanceof ApiError ? errorMessage(cause) : "改计划失败");
    } finally {
      setBusy(null);
    }
  };

  const saveAsTemplate = async () => {
    const name = window.prompt("模板名称（以后新建任务时可以直接套用）", task.title);
    if (!name?.trim()) return;
    setBusy("template");
    setError("");
    try {
      await saveTaskPlanAsTemplate(task.id, { name: name.trim(), description: task.prompt.slice(0, 200) });
      setSaved("已存为模板");
    } catch (cause) {
      setError(cause instanceof ApiError ? errorMessage(cause) : "存模板失败");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="rounded-xl border border-line bg-surface-2/50 px-3.5 py-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-[10px] tracking-wide text-faint uppercase">执行计划</span>
          <span className="font-mono text-[10px] text-faint">
            {progress.completed}/{progress.total}
            {revision > 0 ? ` · 第 ${revision} 版` : ""}
          </span>
        </div>
        <button
          type="button"
          disabled={busy !== null || steps.length === 0}
          onClick={saveAsTemplate}
          className="shrink-0 rounded-md border border-line px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-40"
        >
          {busy === "template" ? "保存中…" : saved || "存为模板"}
        </button>
      </div>

      {progress.total > 0 && (
        <div className="mb-2.5 flex items-center gap-2">
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-brand/15">
            <div
              className="h-full rounded-full bg-brand transition-[width] duration-300"
              style={{ width: `${progress.percent}%` }}
            />
          </div>
          <span className="shrink-0 font-mono text-[10px] text-faint">{progress.percent}%</span>
        </div>
      )}

      {(objective || doneWhen.length > 0) && (
        <div className="mb-2.5 rounded-lg border border-line/70 bg-surface px-2.5 py-2">
          {objective && <p className="text-[12px] leading-relaxed text-text/85">{objective}</p>}
          {doneWhen.length > 0 && (
            <ul className="mt-1 flex flex-col gap-0.5">
              {doneWhen.map((item, index) => (
                <li key={`${item}-${index}`} className="flex items-start gap-1.5 text-[11px] text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-brand/60" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          )}
          {constraints.length > 0 && (
            <p className="mt-1 text-[11px] text-faint">约束：{constraints.join(" · ")}</p>
          )}
          {outOfScope.length > 0 && (
            <p className="mt-0.5 text-[11px] text-faint">不做：{outOfScope.join(" · ")}</p>
          )}
        </div>
      )}

      <div className="flex flex-col gap-2">
        {groups.map((group) => (
          <div key={group.milestone?.id ?? "loose"} className="flex flex-col gap-1">
            {group.milestone && (
              <div className="flex items-center gap-1.5">
                <span
                  className={[
                    "size-1.5 shrink-0 rounded-full",
                    group.milestone.status === "completed"
                      ? "bg-brand"
                      : group.milestone.status === "in_progress"
                        ? "bg-brand/70 ring-2 ring-brand/25"
                        : "bg-line-strong",
                  ].join(" ")}
                />
                <span className="truncate text-[11px] font-medium text-text/75">
                  {group.milestone.title}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-faint">
                  {group.milestone.total > 0
                    ? `${group.milestone.completed}/${group.milestone.total}`
                    : "—"}
                </span>
              </div>
            )}
            <ul className="flex flex-col gap-1">
              {group.steps.map((step) => (
                <li key={step.id} className="flex items-start gap-2">
                  <button
                    type="button"
                    disabled={busy === step.id}
                    onClick={() => toggle(step.id, step.status !== "completed")}
                    title={`${statusLabel(step.status)} · 点击切换`}
                    className={[
                      "mt-0.5 grid size-4 shrink-0 place-items-center rounded-full border text-[9px] transition-colors",
                      step.status === "completed"
                        ? "border-brand bg-brand text-white"
                        : step.status === "in_progress"
                          ? "border-brand/60 bg-brand/15 text-brand"
                          : "border-line bg-surface-3 text-transparent hover:border-line-strong",
                      busy === step.id ? "opacity-50" : "",
                    ].join(" ")}
                  >
                    ✓
                  </button>
                  <span
                    className={[
                      "min-w-0 text-[12px] leading-relaxed",
                      step.status === "completed"
                        ? "text-text/50 line-through decoration-line"
                        : "text-text/85",
                    ].join(" ")}
                  >
                    {step.text}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {error && <p className="mt-2 text-[11px] text-danger">{error}</p>}
    </section>
  );
}
