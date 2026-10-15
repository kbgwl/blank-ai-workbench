"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Button, Field, Input, Notice, StateLine } from "@/components/projects/ui";
import { Panel } from "@/components/panel";
import { errorMessage, type Project, type Provider, type Task, type TaskDraft } from "./api";
import { StatusBadge } from "./bits";
import { formatDateTime } from "./format";
import { useScene } from "@/components/scenes/scenes";

type Props = {
  projects: Project[] | null;
  providers: Provider[] | null;
  projectsError: string | null;
  providersError: string | null;
  tasks: Task[] | null;
  tasksError: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: (draft: TaskDraft, autoRun: boolean) => Promise<Task | null>;
};

const PROJECT_PLACEHOLDER = "__none__";

export function TaskListPane({
  projects,
  providers,
  projectsError,
  providersError,
  tasks,
  tasksError,
  selectedId,
  onSelect,
  onCreate,
}: Props) {
  const [projectId, setProjectId] = useState("");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [criteria, setCriteria] = useState("");
  const [providerId, setProviderId] = useState("");
  const [showComposer, setShowComposer] = useState(false);
  const [submitting, setSubmitting] = useState<"idle" | "create" | "create-run">("idle");
  const [error, setError] = useState<string | null>(null);
  const { scene } = useScene();

  const usableProjects = projects ?? [];
  const chosenProject = projectId || usableProjects[0]?.id || "";
  const projectReady = usableProjects.length > 0;

  async function submit(run: boolean, event?: FormEvent) {
    event?.preventDefault();
    if (!projectReady || !chosenProject) {
      setError("还没有项目，请先在「项目」页创建项目并绑定工作区。");
      return;
    }
    if (!prompt.trim()) {
      setError("请填写任务描述。");
      return;
    }
    setSubmitting(run ? "create-run" : "create");
    setError(null);
    const draft: TaskDraft = {
      projectId: chosenProject,
      title: title.trim() || prompt.trim().split("\n")[0].slice(0, 60),
      prompt: `${scene.instruction}\n\n用户任务：${prompt.trim()}`,
      acceptanceCriteria: criteria
        .split("\n")
        .map((line) => line.replace(/^\s*[-*\d.、)]+\s*/, "").trim())
        .filter(Boolean).concat(scene.defaultCriteria.filter((item) => !criteria.trim())),
      providerId,
    };
    try {
      await onCreate(draft, run);
      setTitle("");
      setPrompt("");
      setCriteria("");
      setShowComposer(false);
    } catch (caught: unknown) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting("idle");
    }
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-col gap-3 overflow-y-auto pr-1">
      <div className="flex items-center justify-between px-1">
        <span className="text-[12px] font-medium text-text">任务</span>
        <button type="button" onClick={() => setShowComposer((value) => !value)} className="rounded-md px-2 py-1 text-[11px] text-muted hover:bg-surface-2 hover:text-text">
          {showComposer ? "关闭" : "+ 新建"}
        </button>
      </div>
      {showComposer && <Panel title="新建任务" hint={projectReady ? `${usableProjects.length} 个项目` : "待创建项目"}>
        <form className="flex flex-col gap-3" onSubmit={(event) => void submit(false, event)}>
          <Field label="项目">
            {projectReady ? (
              <select
                value={chosenProject}
                onChange={(event) => setProjectId(event.target.value)}
                aria-label="选择项目"
                className="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] text-text focus:border-brand/60 focus:outline-none"
              >
                {usableProjects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                    {project.workspaces.length === 0 ? "（未绑定工作区）" : `· ${project.workspaces[0]?.label || "工作区"}`}
                  </option>
                ))}
              </select>
            ) : (
              <Input value={PROJECT_PLACEHOLDER} readOnly disabled placeholder={projectsError ?? "正在读取项目…"} aria-label="选择项目" />
            )}
          </Field>

          <Field label="任务标题（可留空，自动取描述首行）">
            <Input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如：给登录接口增加邮箱校验"
              aria-label="任务标题"
              maxLength={200}
            />
          </Field>

          <Field label="任务描述">
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="用自然语言描述要让 Agent 做什么，例如：在 src/app/page.tsx 的概览页增加任务成功率卡片，并补充对应测试。"
              aria-label="任务描述"
              rows={4}
              className="w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] leading-relaxed text-text placeholder:text-faint focus:border-brand/60 focus:outline-none"
            />
          </Field>

          <Field label="验收条件（每行一条）">
            <textarea
              value={criteria}
              onChange={(event) => setCriteria(event.target.value)}
              placeholder={"邮箱格式非法时返回 422\n补充 pytest 用例并通过"}
              aria-label="验收条件"
              rows={3}
              className="w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[12px] leading-relaxed text-text placeholder:text-faint focus:border-brand/60 focus:outline-none"
            />
          </Field>

          <Field label="模型供应商">
            <select
              value={providerId}
              onChange={(event) => setProviderId(event.target.value)}
              aria-label="选择模型供应商"
              className="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] text-text focus:border-brand/60 focus:outline-none"
            >
              <option value="">跟随默认供应商</option>
              {(providers ?? []).map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                  {provider.model ? ` · ${provider.model}` : ""}
                  {provider.enabled ? "" : "（已停用）"}
                </option>
              ))}
              {providers !== null && providers.length === 0 && <option value="">后端没有可用供应商</option>}
            </select>
          </Field>
          {providersError && <Notice tone="muted">供应商列表读取失败：{providersError}</Notice>}
          {!projectReady && projectsError && <Notice tone="warn">项目列表读取失败：{projectsError}</Notice>}
          {!projectReady && !projectsError && (
            <Notice tone="muted">
              还没有项目，先到{" "}
              <Link href="/projects" className="text-brand underline-offset-2 hover:underline">
                项目页
              </Link>{" "}
              创建并绑定工作区。
            </Notice>
          )}
          {error && <Notice>{error}</Notice>}

          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" disabled={submitting !== "idle"}>
              {submitting === "create" ? "创建中" : "创建任务"}
            </Button>
            <Button type="button" disabled={submitting !== "idle"} onClick={(event) => void submit(true, event as unknown as FormEvent)}>
              {submitting === "create-run" ? "创建并运行中" : "创建并运行"}
            </Button>
          </div>
        </form>
      </Panel>}

      <Panel title="最近任务" hint={tasks ? `${tasks.length} 条` : "读取中"}>
        {tasksError ? (
          <div className="flex flex-col gap-2">
            <Notice tone="warn">任务列表读取失败：{tasksError}</Notice>
            <p className="text-[11px] leading-relaxed text-faint">
              后端未提供任务接口时这里会保留错误提示，不影响其他页面。
            </p>
          </div>
        ) : tasks === null ? (
          <StateLine>读取任务列表…</StateLine>
        ) : tasks.length === 0 ? (
          <StateLine>还没有任务，用上方表单创建</StateLine>
        ) : (
          <ul className="flex max-h-[52vh] flex-col gap-1.5 overflow-y-auto pr-1">
            {tasks.map((task) => {
              const active = task.id === selectedId;
              const projectName = projects?.find((project) => project.id === task.projectId)?.name;
              return (
                <li key={task.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(task.id)}
                    aria-current={active}
                    className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                      active
                        ? "border-brand/40 bg-brand/[0.07]"
                        : "border-line bg-surface/60 hover:border-line-strong hover:bg-surface-2/70"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="min-w-0 flex-1 truncate text-[13px] text-text">{task.title}</span>
                      <StatusBadge status={task.status} />
                    </div>
                    <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-faint">
                      <span className="truncate">{projectName ?? (task.projectId ? task.projectId.slice(0, 8) : "无项目")}</span>
                      <span className="ml-auto shrink-0">{formatDateTime(task.updatedAt || task.createdAt)}</span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
    </div>
  );
}
