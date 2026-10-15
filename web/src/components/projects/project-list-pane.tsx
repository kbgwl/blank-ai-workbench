"use client";

import { useState, type FormEvent } from "react";
import { Panel } from "@/components/panel";
import { activeWorkspace, createProject, deleteProject, errorMessage, updateProject, type Project } from "./api";
import { ActiveDot, Button, IconButton, Input, Notice, PencilIcon, StateLine, TrashIcon } from "./ui";

type Props = {
  projects: Project[] | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreated: (project: Project) => void;
  onChanged: () => void | Promise<void>;
};

export function ProjectListPane({ projects, error, selectedId, onSelect, onCreated, onChanged }: Props) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [draftDescription, setDraftDescription] = useState("");
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null);

  async function submitCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createProject({ name: trimmed, description: description.trim() });
      setName("");
      setDescription("");
      onCreated(created);
      await onChanged();
    } catch (e) {
      setCreateError(errorMessage(e));
    } finally {
      setCreating(false);
    }
  }

  async function submitEdit(project: Project) {
    const trimmed = draft.trim();
    if (!trimmed) return;
    setBusyId(project.id);
    setRowError(null);
    try {
      await updateProject(project.id, { name: trimmed, description: draftDescription.trim() });
      setEditingId(null);
      await onChanged();
    } catch (e) {
      setRowError({ id: project.id, message: errorMessage(e) });
    } finally {
      setBusyId(null);
    }
  }

  async function submitDelete(project: Project) {
    setBusyId(project.id);
    setRowError(null);
    try {
      await deleteProject(project.id);
      setConfirmId(null);
      await onChanged();
    } catch (e) {
      setRowError({ id: project.id, message: errorMessage(e) });
      setConfirmId(null);
    } finally {
      setBusyId(null);
    }
  }

  const count = projects?.length ?? 0;

  return (
    <Panel title="项目" hint={projects === null ? "载入中" : `共 ${count} 个 · /api/v1/projects`}>
      <form onSubmit={submitCreate} className="flex flex-col gap-2 border-b border-line pb-4">
        <div className="flex flex-col gap-2">
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="项目名称"
            aria-label="项目名称"
            maxLength={200}
          />
          <Input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="项目描述（可选）"
            aria-label="项目描述"
          />
        </div>
        <Button type="submit" variant="primary" disabled={creating || !name.trim()} className="self-start">
          {creating ? "创建中" : "新建项目"}
        </Button>
        {createError && <Notice>{createError}</Notice>}
      </form>

      {error && (
        <div className="mt-4 flex flex-col gap-2">
          <Notice>项目列表加载失败：{error}</Notice>
          <Button className="self-start" onClick={() => void onChanged()}>
            重试
          </Button>
        </div>
      )}

      <div className="mt-4">
        {projects === null ? (
          <StateLine>载入项目…</StateLine>
        ) : count === 0 && !error ? (
          <StateLine>暂无项目</StateLine>
        ) : (
          <ul className="flex flex-col gap-2">
            {projects?.map((project) => {
              const workspace = activeWorkspace(project);
              const extra = project.workspaces.length - 1;
              const selected = project.id === selectedId;
              const busy = busyId === project.id;
              return (
                <li
                  key={project.id}
                  className={`group rounded-xl border transition-colors ${
                    selected
                      ? "border-brand/40 bg-surface-2"
                      : "border-line bg-surface/60 hover:border-line-strong"
                  }`}
                >
                  <div className="flex items-start gap-2 p-3">
                    <button type="button" onClick={() => onSelect(project.id)} className="min-w-0 flex-1 text-left">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`truncate text-[13px] font-medium ${selected ? "text-brand" : "text-text"}`}
                        >
                          {project.name}
                        </span>
                        {workspace ? (
                          <span className="flex items-center gap-1.5 rounded-md border border-line bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted">
                            <ActiveDot active={workspace.is_active} />
                            <span className="max-w-[9rem] truncate">{workspace.label}</span>
                            {extra > 0 && <span className="text-faint">+{extra}</span>}
                          </span>
                        ) : (
                          <span className="font-mono text-[10px] text-warn">未绑定工作区</span>
                        )}
                      </div>
                      {project.description && (
                        <p className="mt-1 line-clamp-2 text-[12px] leading-relaxed text-muted">
                          {project.description}
                        </p>
                      )}
                    </button>
                    <div className="flex shrink-0 gap-1 opacity-60 transition-opacity group-hover:opacity-100">
                      <IconButton
                        label="编辑"
                        onClick={() => {
                          setConfirmId(null);
                          setRowError(null);
                          setEditingId(project.id);
                          setDraft(project.name);
                          setDraftDescription(project.description);
                        }}
                      >
                        <PencilIcon />
                      </IconButton>
                      <IconButton
                        label="删除"
                        tone="danger"
                        onClick={() => {
                          setEditingId(null);
                          setRowError(null);
                          setConfirmId(project.id);
                        }}
                      >
                        <TrashIcon />
                      </IconButton>
                    </div>
                  </div>

                  {editingId === project.id && (
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        void submitEdit(project);
                      }}
                      className="flex flex-col gap-2 border-t border-line px-3 py-2"
                    >
                      <Input
                        value={draft}
                        autoFocus
                        aria-label="新的项目名称"
                        maxLength={200}
                        className="py-1.5 text-[12px]"
                        onChange={(event) => setDraft(event.target.value)}
                      />
                      <Input
                        value={draftDescription}
                        aria-label="新的项目描述"
                        placeholder="项目描述（可选）"
                        className="py-1.5 text-[12px]"
                        onChange={(event) => setDraftDescription(event.target.value)}
                      />
                      <div className="flex gap-2">
                        <Button type="submit" variant="primary" disabled={busy || !draft.trim()}>
                          保存
                        </Button>
                        <Button
                          onClick={() => {
                            setEditingId(null);
                            setRowError(null);
                          }}
                        >
                          取消
                        </Button>
                      </div>
                    </form>
                  )}

                  {confirmId === project.id && (
                    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line bg-danger/[0.04] px-3 py-2">
                      <span className="text-[12px] text-muted">删除「{project.name}」及其工作区绑定？</span>
                      <div className="flex gap-2">
                        <Button
                          onClick={() => {
                            setConfirmId(null);
                            setRowError(null);
                          }}
                        >
                          取消
                        </Button>
                        <Button variant="danger" disabled={busy} onClick={() => void submitDelete(project)}>
                          {busy ? "删除中" : "删除"}
                        </Button>
                      </div>
                    </div>
                  )}

                  {rowError?.id === project.id && (
                    <div className="border-t border-line px-3 py-2">
                      <Notice>{rowError.message}</Notice>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}
