"use client";

import { useState, type FormEvent } from "react";
import { Panel } from "@/components/panel";
import { canPickDirectory, pickDirectory } from "@/lib/desktop";
import {
  activateWorkspace,
  activeWorkspace,
  bindWorkspace,
  errorMessage,
  revealWorkspacePath,
  unbindWorkspace,
  type Project,
  type TreeEntry,
} from "./api";
import { FileTree } from "./file-tree";
import { FileViewer } from "./file-viewer";
import { ActiveDot, Button, Field, Input, Notice, StateLine } from "./ui";

type Props = {
  project: Project | null;
  onChanged: () => void | Promise<void>;
};

/** 从绝对路径取目录名，作为标签的默认值。两种分隔符都要认：粘贴来的可能是 `/`。 */
function dirName(path: string): string {
  const trimmed = path.trim().replace(/[\\/]+$/, "");
  const parts = trimmed.split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

export function WorkspacePane({ project, onChanged }: Props) {
  const [openFile, setOpenFile] = useState<TreeEntry | null>(null);
  const [path, setPath] = useState("");
  const [label, setLabel] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [binding, setBinding] = useState(false);
  const [unbindingId, setUnbindingId] = useState<string | null>(null);
  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  /** 已点开的那几行的真实路径。键是 workspace.id，没点开就不在表里。 */
  const [revealed, setRevealed] = useState<Record<string, string>>({});

  const workspace = activeWorkspace(project);
  const nativePicker = canPickDirectory();

  async function browse() {
    if (!nativePicker || picking) return;
    setPicking(true);
    setFormError(null);
    try {
      const picked = await pickDirectory(path);
      if (!picked) return;
      setPath(picked);
      // 标签留空时自动补目录名：用户选了目录之后通常只需要再点一下"绑定"，
      // 不该被一个可选字段拦住。
      setLabel((current) => current.trim() || dirName(picked));
    } finally {
      setPicking(false);
    }
  }

  async function submitActivate(workspaceId: string) {
    if (!project || activatingId) return;
    setActivatingId(workspaceId);
    setFormError(null);
    try {
      await activateWorkspace(project.id, workspaceId);
      setOpenFile(null);
      await onChanged();
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setActivatingId(null);
    }
  }

  async function reveal(workspaceId: string) {
    if (!project || revealed[workspaceId]) return;
    try {
      const real = await revealWorkspacePath(project.id, workspaceId);
      setRevealed((current) => ({ ...current, [workspaceId]: real }));
    } catch (error) {
      setFormError(errorMessage(error));
    }
  }

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
          {/* 条件用"有没有绑定"，不是"有没有活动"。旧数据里可能一条活动都没有，
              那样整段列表会消失，用户看到的是"我一个都没绑过"——而其实绑了。 */}
          {project.workspaces.length > 0 && (
            <ul className="flex flex-col gap-2">
              {project.workspaces.map((item) => {
                const real = revealed[item.id];
                const masked = item.path.startsWith("<workspace:");
                return (
                  <li
                    key={item.id}
                    className="flex flex-col gap-1.5 rounded-lg border border-line bg-surface/60 px-3 py-2"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <ActiveDot active={item.is_active} />
                      <span className="min-w-0 flex-1 truncate text-[13px]">{item.label}</span>
                      {item.is_active ? (
                        <span className="shrink-0 rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
                          活动
                        </span>
                      ) : (
                        <Button
                          disabled={activatingId === item.id}
                          onClick={() => void submitActivate(item.id)}
                        >
                          {activatingId === item.id ? "切换中" : "切到这里"}
                        </Button>
                      )}
                      <Button
                        disabled={unbindingId === item.id}
                        onClick={() => void submitUnbind(item.id)}
                      >
                        {unbindingId === item.id ? "解绑中" : "解绑"}
                      </Button>
                    </div>
                    {/* 默认只给打码路径。想确认绑的是哪个目录时点一下，单独取一次真路径。 */}
                    {masked && !real ? (
                      <button
                        type="button"
                        onClick={() => void reveal(item.id)}
                        title="显示完整路径"
                        className="self-start truncate font-mono text-[10px] text-faint underline decoration-dotted transition-colors hover:text-muted"
                      >
                        {item.path} · 点击显示完整路径
                      </button>
                    ) : (
                      <span className="truncate font-mono text-[10px] text-faint" title={real ?? item.path}>
                        {real ?? item.path}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          <form onSubmit={submitBind} className="flex flex-col gap-2 border-t border-line pt-3">
            <div className="flex flex-col gap-2 md:flex-row">
              <Field label="工作区绝对路径">
                <div className="flex items-center gap-2">
                  <Input
                    value={path}
                    onChange={(event) => setPath(event.target.value)}
                    placeholder="E:\kbgai\services\api"
                    aria-label="工作区绝对路径"
                    spellCheck={false}
                    autoComplete="off"
                    className="flex-1 font-mono text-[12px]"
                    maxLength={1000}
                  />
                  {/* 只有桌面壳里才有系统目录对话框；浏览器里不渲染，避免点了没反应。 */}
                  {nativePicker && (
                    <Button
                      type="button"
                      onClick={() => void browse()}
                      disabled={picking}
                      className="shrink-0 whitespace-nowrap"
                    >
                      {picking ? "选择中" : "浏览…"}
                    </Button>
                  )}
                </div>
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
              {nativePicker
                ? "点「浏览…」选一个已存在的目录；手填时必须写绝对路径。"
                : "路径必须是运行后端的这台机器上已存在的目录，相对路径会被拒绝。"}
            </p>
            <Button type="submit" variant="primary" className="self-start" disabled={binding || !path.trim()}>
              {binding ? "绑定中" : "绑定工作区"}
            </Button>
          </form>
          {formError && <Notice>{formError}</Notice>}
        </div>
      </Panel>

      <Panel title="文件" hint={workspace ? `${workspace.label} · 懒加载 depth 1` : "需要先绑定工作区"}>
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
