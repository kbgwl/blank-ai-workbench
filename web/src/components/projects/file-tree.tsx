"use client";

import { useCallback, useEffect, useState } from "react";
import { errorDetails, fetchTree, type TreeEntry } from "./api";
import { formatSize } from "./format";
import { Button, ChevronIcon, FileIcon, FolderIcon, Notice, Spinner, StateLine } from "./ui";

type Failure = { code: string; message: string };

type Cache = {
  key: string;
  nodes: Record<string, TreeEntry[]>;
  expanded: string[];
  errors: Record<string, Failure>;
};

type Props = {
  projectId: string;
  workspaceId: string;
  activePath: string | null;
  onOpen: (entry: TreeEntry) => void;
};

const ROOT = "";

/** 后端错误码 → 界面可执行的下一步说明（消息本身仍是后端的中文文案）。 */
export function describeFailure(failure: Failure): { text: string; tone: "error" | "warn" } {
  if (failure.code === "workspace_not_bound") {
    return { text: `${failure.message} · 请先在上方绑定工作区目录`, tone: "warn" };
  }
  if (failure.code === "path_escape_denied") {
    return { text: `${failure.message} · 文件树只允许访问工作区内的路径`, tone: "warn" };
  }
  return { text: failure.message, tone: "error" };
}

export function FileTree({ projectId, workspaceId, activePath, onOpen }: Props) {
  const [cache, setCache] = useState<Cache | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const key = `${projectId}:${workspaceId}:${nonce}`;

  useEffect(() => {
    let ignore = false;
    fetchTree(projectId, ROOT, 1)
      .then((tree) => {
        if (!ignore) setCache({ key, nodes: { [ROOT]: tree.entries }, expanded: [ROOT], errors: {} });
      })
      .catch((error: unknown) => {
        if (!ignore) setCache({ key, nodes: {}, expanded: [ROOT], errors: { [ROOT]: errorDetails(error) } });
      });
    return () => {
      ignore = true;
    };
  }, [key, projectId]);

  const patch = useCallback((update: (current: Cache) => Cache) => {
    setCache((current) => (current ? update(current) : current));
  }, []);

  const expand = useCallback(
    async (path: string) => {
      setPending(path);
      try {
        const tree = await fetchTree(projectId, path, 1);
        patch((current) => {
          const errors = { ...current.errors };
          delete errors[path];
          return {
            ...current,
            nodes: { ...current.nodes, [path]: tree.entries },
            expanded: [...current.expanded, path],
            errors,
          };
        });
      } catch (error) {
        patch((current) => ({ ...current, errors: { ...current.errors, [path]: errorDetails(error) } }));
      } finally {
        setPending(null);
      }
    },
    [projectId, patch],
  );

  function toggle(entry: TreeEntry) {
    const isOpen = cache?.expanded.includes(entry.path) ?? false;
    if (isOpen) {
      patch((current) => ({ ...current, expanded: current.expanded.filter((path) => path !== entry.path) }));
      return;
    }
    if (cache?.nodes[entry.path]) {
      patch((current) => ({ ...current, expanded: [...current.expanded, entry.path] }));
      return;
    }
    void expand(entry.path);
  }

  function renderEntries(paths: string[], depth: number) {
    const entries = paths.flatMap((path) => cache?.nodes[path] ?? []);
    if (entries.length === 0) {
      return <div className="py-1 pl-6 font-mono text-[10px] text-faint">空目录</div>;
    }
    return (
      <ul className={`flex flex-col gap-px ${depth === 0 ? "" : "ml-3 border-l border-line pl-2"}`}>
        {entries.map((entry) => {
          const open = entry.is_dir && (cache?.expanded.includes(entry.path) ?? false);
          const active = !entry.is_dir && entry.path === activePath;
          return (
            <li key={entry.path}>
              <button
                type="button"
                onClick={() => (entry.is_dir ? toggle(entry) : onOpen(entry))}
                aria-expanded={entry.is_dir ? open : undefined}
                className={`flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left font-mono text-[11px] transition-colors ${
                  active
                    ? "bg-surface-2 text-brand"
                    : entry.is_dir
                      ? "text-text/90 hover:bg-surface-2/70"
                      : "text-muted hover:bg-surface-2/70 hover:text-text"
                }`}
              >
                <span className={entry.is_dir ? "text-faint" : "w-3"}>
                  {entry.is_dir && <ChevronIcon open={open} />}
                </span>
                {entry.is_dir ? <FolderIcon /> : <FileIcon />}
                <span className="min-w-0 flex-1 truncate">{entry.name}</span>
                {pending === entry.path && <Spinner />}
                {!entry.is_dir && (
                  <span className="shrink-0 text-[10px] text-faint">{formatSize(entry.size)}</span>
                )}
              </button>
              {open && renderEntries([entry.path], depth + 1)}
              {cache?.errors[entry.path] && (
                <div className="ml-6 py-1">
                  <Notice>{describeFailure(cache.errors[entry.path]).text}</Notice>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  }

  const loaded = cache?.key === key;
  const rootError = cache?.errors[ROOT];

  if (!loaded) return <StateLine>读取文件树…</StateLine>;
  if (rootError) {
    const failure = describeFailure(rootError);
    return (
      <div className="flex flex-col gap-2">
        <Notice tone={failure.tone}>文件树读取失败：{failure.text}</Notice>
        <Button className="self-start" onClick={() => setNonce((value) => value + 1)}>
          重试
        </Button>
      </div>
    );
  }

  return (
    <div className="max-h-[26rem] overflow-y-auto pr-1">
      {cache?.nodes[ROOT]?.length ? renderEntries([ROOT], 0) : <StateLine>工作区为空</StateLine>}
    </div>
  );
}
