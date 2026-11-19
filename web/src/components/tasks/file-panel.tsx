"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, ChevronIcon, FileIcon, FolderIcon, Notice, Spinner, StateLine } from "@/components/projects/ui";
import {
  errorMessage,
  fetchFile,
  fetchTree,
  type FileContent,
  type Project,
  type TreeEntry,
} from "./api";
import { HighlightedCode } from "@/components/bot/syntax";
import { clip, formatSize, langOf } from "./format";

const ROOT = "";

type Nodes = Record<string, TreeEntry[]>;
type Errors = Record<string, string>;

type TreeStore = {
  projectId: string;
  nodes: Nodes;
  errors: Errors;
  expanded: string[];
  rootError: string | null;
};

type FileResult =
  | { key: string; data: FileContent }
  | { key: string; failure: string }
  | null;

export function FilePanel({ project }: { project: Project | null }) {
  const [tree, setTree] = useState<TreeStore | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [openFile, setOpenFile] = useState<TreeEntry | null>(null);
  // HTML 产物：源码 / 渲染结果两态。默认源码——用户点开文件通常是想看内容，
  // 但"这个东西长什么样"必须在同一个面板里能立刻回答（原来只能看源码，
  // 要自己去找文件双击）。
  const [renderHtml, setRenderHtml] = useState(false);
  const [result, setResult] = useState<FileResult>(null);
  const projectId = project?.id ?? null;
  const workspaceId = project?.workspaces.find((item) => item.is_active)?.id ?? project?.workspaces[0]?.id ?? "";

  const patch = useCallback((target: string, updater: (current: TreeStore) => TreeStore) => {
    setTree((current) => (current && current.projectId === target ? updater(current) : current));
  }, []);

  const load = useCallback(
    (target: string, path: string) => {
      setPending(path);
      fetchTree(target, path, 1)
        .then((treeResponse) => {
          setTree((current) => {
            const base: TreeStore =
              current && current.projectId === target
                ? current
                : { projectId: target, nodes: {}, errors: {}, expanded: [ROOT], rootError: null };
            const errors = { ...base.errors };
            delete errors[path];
            return {
              ...base,
              nodes: { ...base.nodes, [path]: treeResponse.entries },
              errors,
              expanded: path === ROOT ? base.expanded : [...new Set([...base.expanded, path])],
              rootError: path === ROOT ? null : base.rootError,
            };
          });
        })
        .catch((error: unknown) => {
          const message = errorMessage(error);
          setTree((current) => {
            const base: TreeStore =
              current && current.projectId === target
                ? current
                : { projectId: target, nodes: {}, errors: {}, expanded: [ROOT], rootError: null };
            return {
              ...base,
              nodes: path === ROOT ? {} : base.nodes,
              errors: { ...base.errors, [path]: message },
              rootError: path === ROOT ? message : base.rootError,
            };
          });
        })
        .finally(() => setPending(null));
    },
    [],
  );

  const loadedRef = useRef<string>("");
  useEffect(() => {
    if (!projectId || loadedRef.current === `${projectId}:${workspaceId}`) return;
    loadedRef.current = `${projectId}:${workspaceId}`;
    load(projectId, ROOT);
  }, [projectId, workspaceId, load]);

  const path = openFile?.path ?? null;
  const requestKey = projectId && path ? `${projectId}:${workspaceId}:${path}` : null;
  useEffect(() => {
    if (!projectId || !path || !requestKey) return;
    setRenderHtml(false);
    let ignore = false;
    fetchFile(projectId, path)
      .then((data) => {
        if (!ignore) setResult({ key: requestKey, data });
      })
      .catch((error: unknown) => {
        if (!ignore) setResult({ key: requestKey, failure: errorMessage(error) });
      });
    return () => {
      ignore = true;
    };
  }, [projectId, path, requestKey]);

  if (!project) {
    return <StateLine>先在左侧选择任务所属项目</StateLine>;
  }
  if (project.workspaces.length === 0) {
    return (
      <Notice tone="muted">
        项目「{project.name}」还没有绑定工作区，请到{" "}
        <Link href="/projects" className="text-brand underline-offset-2 hover:underline">
          项目页
        </Link>{" "}
        绑定目录后再浏览文件。
      </Notice>
    );
  }

  const pid: string = project.id;
  // 能直接渲染的产物：HTML。判定放在渲染副作用之外，读起来就是"这是什么文件"。
  const isHtml = /\.html?$/i.test(String(path || ""));
  const store = tree && tree.projectId === projectId ? tree : null;

  function toggle(entry: TreeEntry) {
    const open = store?.expanded.includes(entry.path) ?? false;
    if (open) {
      patch(pid, (current) => ({ ...current, expanded: current.expanded.filter((item) => item !== entry.path) }));
      return;
    }
    if (store?.nodes[entry.path]) {
      patch(pid, (current) => ({ ...current, expanded: [...current.expanded, entry.path] }));
      return;
    }
    load(pid, entry.path);
  }

  function renderLevel(paths: string[], depth: number) {
    const entries = paths.flatMap((item) => store?.nodes[item] ?? []);
    if (entries.length === 0) {
      return <div className="py-1 pl-6 font-mono text-[11px] text-faint">空目录</div>;
    }
    return (
      <ul className={`flex flex-col gap-px ${depth === 0 ? "" : "ml-3 border-l border-line pl-2"}`}>
        {entries.map((entry) => {
          const open = entry.is_dir && (store?.expanded.includes(entry.path) ?? false);
          const active = !entry.is_dir && entry.path === path;
          return (
            <li key={entry.path}>
              <button
                type="button"
                onClick={() => (entry.is_dir ? toggle(entry) : setOpenFile(entry))}
                aria-expanded={entry.is_dir ? open : undefined}
                className={`flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left font-mono text-[12px] transition-colors ${
                  active
                    ? "bg-surface-2 text-brand"
                    : entry.is_dir
                      ? "text-text/90 hover:bg-surface-2"
                      : "text-muted hover:bg-surface-2 hover:text-text"
                }`}
              >
                <span className={entry.is_dir ? "text-faint" : "w-3"}>{entry.is_dir && <ChevronIcon open={open} />}</span>
                {entry.is_dir ? <FolderIcon /> : <FileIcon />}
                <span className="min-w-0 flex-1 truncate">{entry.name || clip(entry.path, 24)}</span>
                {pending === entry.path && <Spinner />}
                {!entry.is_dir && <span className="shrink-0 text-[11px] text-faint">{formatSize(entry.size)}</span>}
              </button>
              {open && renderLevel([entry.path], depth + 1)}
              {!open && store?.errors[entry.path] && (
                <div className="ml-6 py-1">
                  <Notice>{store.errors[entry.path]}</Notice>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  }

  const settled = requestKey && result?.key === requestKey ? result : null;

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <span className="font-mono text-[11px] tracking-wide text-faint uppercase">
          文件树 · {project.workspaces.find((item) => item.is_active)?.label || project.workspaces[0]?.label || project.name}
        </span>
        {store?.rootError ? (
          <div className="flex flex-col gap-2">
            <Notice>{store.rootError}</Notice>
            <Button className="self-start" onClick={() => load(pid, ROOT)}>
              重试
            </Button>
          </div>
        ) : !store || store.nodes[ROOT] === undefined ? (
          <StateLine>读取文件树…</StateLine>
        ) : (
          <div className="max-h-64 overflow-y-auto pr-1">{renderLevel([ROOT], 0)}</div>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <span className="font-mono text-[11px] tracking-wide text-faint uppercase">文件预览</span>
        {!openFile || !path ? (
          <StateLine>从文件树选择文件</StateLine>
        ) : !settled ? (
          <div className="flex items-center gap-2 px-1 py-3 font-mono text-[12px] text-faint">
            <Spinner />
            读取 {clip(path, 34)}
          </div>
        ) : "failure" in settled ? (
          <Notice>{settled.failure}</Notice>
        ) : (
          <div className="overflow-hidden rounded-lg border border-line bg-surface-2">
            <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
              <span className="min-w-0 truncate font-mono text-[12px] text-brand" title={settled.data.path}>
                {clip(settled.data.path, 40)}
              </span>
              <span className="flex shrink-0 items-center gap-2">
                {isHtml && !settled.data.binary && (
                  <span className="flex overflow-hidden rounded-md border border-line font-mono text-[11px]">
                    {([["源码", false], ["预览", true]] as const).map(([label, value]) => (
                      <button
                        key={label}
                        type="button"
                        onClick={() => setRenderHtml(value)}
                        className={`px-2 py-0.5 transition-colors ${
                          renderHtml === value ? "bg-surface text-text" : "text-muted hover:text-text"
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </span>
                )}
                <span className="font-mono text-[11px] text-faint">{formatSize(settled.data.size)}</span>
              </span>
            </div>
            {settled.data.binary ? (
              <div className="p-3">
                <Notice tone="muted">{settled.data.message || "二进制文件，无法预览"}</Notice>
              </div>
            ) : (
              <>
                {settled.data.truncated && (
                  <div className="border-b border-warn/20 bg-warn/[0.06] px-3 py-2 text-[12px] text-warn">
                    {settled.data.message || "文件过大，内容已截断"}
                  </div>
                )}
                {renderHtml && isHtml ? (
                  <iframe
                    title="产物预览"
                    srcDoc={settled.data.content}
                    className="h-72 w-full border-0 bg-white"
                    // 不给 allow-same-origin：脚本能跑，但 iframe 是不透明源，
                    // 拿不到本站的 cookie / localStorage，也发不出带凭据的请求。
                    // 这正是「能看渲染结果」和「不能反过来动我们」的分界。
                    sandbox="allow-scripts allow-forms allow-popups allow-modals"
                  />
                ) : (
                  <div className="max-h-72 overflow-auto">
                    {settled.data.content ? (
                      <HighlightedCode text={settled.data.content} lang={langOf(settled.data.path)} />
                    ) : (
                      <p className="px-3 py-2.5 font-mono text-[12px] text-faint">（空文件）</p>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
