"use client";

import { useState, type FormEvent } from "react";
import { Panel } from "@/components/panel";
import { activeWorkspace, bindWorkspace, errorMessage, unbindWorkspace, type Project, type TreeEntry } from "./api";
import { FileTree } from "./file-tree";
import { FileViewer } from "./file-viewer";
import { ActiveDot, Button, Field, Input, Notice, StateLine } from "./ui";

type Props = {
  project: Project | null;
  onChanged: () => void | Promise<void>;
};

export function WorkspacePane({ project, onChanged }: Props) {
  const [openFile, setOpenFile] = useState<TreeEntry | null>(null);
  const [path, setPath] = useState("");
  const [label, setLabel] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [binding, setBinding] = useState(false);
  const [unbindingId, setUnbindingId] = useState<string | null>(null);

  const workspace = activeWorkspace(project);

  async function submitBind(event: FormEvent) {
    event.preventDefault();
    if (!project || binding || !path.trim()) return;
    setBinding(true);
    setFormError(null);
    try {
      await bindWorkspace(project.id, { path: path.trim(), label: label.trim() });
      setPath("");
      setLabel("");
      setOpenFile(null);
      await onChanged();
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setBinding(false);
    }
  }

  async function submitUnbind(workspaceId: string) {
    if (!project || unbindingId) return;
    setUnbindingId(workspaceId);
    setFormError(null);
    try {
      await unbindWorkspace(project.id, workspaceId);
      setOpenFile(null);
      await onChanged();
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setUnbindingId(null);
    }
  }

  if (!project) {
    return (
      <div className="flex min-w-0 flex-col gap-4">
        <Panel title="工作区" hint="未选择项目">
          <StateLine>在左侧选择一个项目</StateLine>
        </Panel>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Panel
        title="工作区"
        hint={workspace ? `${project.workspaces.length} 个绑定` : "0 个绑定"}
      >
        <div className="flex flex-col gap-3">
          {workspace && (
            <ul className="flex flex-col gap-2">
              {project.workspaces.map((item) => (
                <li
                  key={item.id}
                  className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface/60 px-3 py-2"
                >
                  <ActiveDot active={item.is_active} />
                  <span className="min-w-0 flex-1 truncate text-[13px]">{item.label}</span>
                  <span className="shrink-0 font-mono text-[10px] text-faint">{item.path}</span>
                  {item.is_active && (
                    <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
                      活动
                    </span>
                  )}
                  <Button
                    disabled={unbindingId === item.id}
                    onClick={() => void submitUnbind(item.id)}
                  >
                    {unbindingId === item.id ? "解绑中" : "解绑"}
                  </Button>
                </li>
              ))}
            </ul>
          )}

          <form onSubmit={submitBind} className="flex flex-col gap-2 border-t border-line pt-3">
            <div className="flex flex-col gap-2 md:flex-row">
              <Field label="工作区绝对路径">
                <Input
                  value={path}
                  onChange={(event) => setPath(event.target.value)}
                  placeholder="E:\kbgai\services\api"
                  aria-label="工作区绝对路径"
                  spellCheck={false}
                  autoComplete="off"
                  className="font-mono text-[12px]"
                  maxLength={1000}
                />
              </Field>
              <Field label="标签（可选）">
                <Input
                  value={label}
                  onChange={(event) => setLabel(event.target.value)}
                  placeholder="默认取目录名"
                  aria-label="工作区标签"
                  autoComplete="off"
                  maxLength={200}
                />
              </Field>
            </div>
            <p className="text-[11px] leading-relaxed text-faint">
              路径必须是运行后端的这台机器上已存在的目录，相对路径会被拒绝。
            </p>
            <Button type="submit" variant="primary" className="self-start" disabled={binding || !path.trim()}>
              {binding ? "绑定中" : "绑定工作区"}
            </Button>
          </form>
          {formError && <Notice>{formError}</Notice>}
        </div>
      </Panel>

      <Panel title="文件" hint={workspace ? `${workspace.path} · 懒加载 depth 1` : "需要先绑定工作区"}>
        {workspace ? (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
            <div className="flex min-w-0 flex-col gap-2">
              <span className="font-mono text-[10px] tracking-wide text-faint uppercase">文件树</span>
              <FileTree
                projectId={project.id}
                workspaceId={workspace.id}
                activePath={openFile?.path ?? null}
                onOpen={setOpenFile}
              />
            </div>
            <div className="flex min-w-0 flex-col gap-2">
              <span className="font-mono text-[10px] tracking-wide text-faint uppercase">文件内容</span>
              <FileViewer projectId={project.id} workspaceId={workspace.id} entry={openFile} />
            </div>
          </div>
        ) : (
          <StateLine>该项目尚未绑定工作区目录</StateLine>
        )}
      </Panel>
    </div>
  );
}
