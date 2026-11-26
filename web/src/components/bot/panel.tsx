"use client";

import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import Image from "next/image";
import { readString, writeString } from "@/lib/safe-storage";
import type { AcceptanceItem, ChangeItem, TestInfo, UsageInfo } from "../tasks/api";
import { diffStats } from "../tasks/format";
import { errorMessage, fetchFile, fetchFileRaw, fetchTree, type FileContent, type TreeEntry } from "../projects/api";
import { artifactsExport, groupArtifacts } from "./artifacts";
import { FilePreview } from "./file-preview";
import { previewExpiry, previewHeadline, previewIsolation } from "./preview-copy";
import { fetchPreview, startPreview, stopPreview, type PreviewState } from "./preview-api";
import { CodeChangeView } from "./change-view";
import { RailEmpty } from "../rail-empty";
import type { BotStep } from "./turns";

const PANEL_WIDTH_KEY = "blank:bot-panel-width";
const PANEL_WIDTH_DEFAULT = 620;
const PANEL_WIDTH_MIN = 420;
const PANEL_WIDTH_MAX = 900;

function isImagePath(path: string): boolean {
  return /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
}

function clampPanelWidth(width: number): number {
  const viewportMax = typeof window === "undefined" ? PANEL_WIDTH_MAX : Math.max(PANEL_WIDTH_MIN, window.innerWidth - 320);
  return Math.min(Math.min(PANEL_WIDTH_MAX, viewportMax), Math.max(PANEL_WIDTH_MIN, width));
}

function uniqueChanges(rows: ChangeItem[]): ChangeItem[] {
  const merged = new Map<string, ChangeItem>();
  for (const row of rows) {
    const existing = merged.get(row.path);
    if (!existing || (!existing.diff && row.diff)) merged.set(row.path, row);
  }
  return [...merged.values()];
}

function changesFromSteps(steps: BotStep[]): ChangeItem[] {
  return steps
    .filter((step): step is Extract<BotStep, { kind: "change" }> => step.kind === "change")
    .reduce<ChangeItem[]>((rows, step) => {
      if (!rows.some((row) => row.path === step.path)) {
        rows.push({ path: step.path, diff: "", changeType: step.changeType });
      }
      return rows;
    }, []);
}

function statusOf(status: string, running: boolean): { label: string; badge: string; dot: string } {
  if (status === "approval_expired") return { label: "审批已过期", badge: "bg-warn/[0.12] text-warn", dot: "bg-warn" };
  if (status === "waiting_approval") return { label: "等你批准", badge: "bg-warn/[0.12] text-warn", dot: "bg-warn" };
  if (status === "paused") return { label: "已暂停", badge: "bg-warn/[0.12] text-warn", dot: "bg-warn" };
  if (running) return { label: "进行中", badge: "bg-brand/[0.1] text-brand", dot: "bg-brand" };
  if (status === "failed" || status === "cancelled") {
    return { label: status === "failed" ? "失败" : "已停止", badge: "bg-danger/[0.1] text-danger", dot: "bg-danger" };
  }
  return { label: "完成", badge: "bg-accent/[0.1] text-accent", dot: "bg-accent" };
}

export type TaskPanelProps = {
  title: string;
  status: string;
  running: boolean;
  steps: BotStep[];
  changes: ChangeItem[];
  workspaceChanges?: ChangeItem[];
  usage: UsageInfo | null;
  projectId: string;
  acceptance?: AcceptanceItem[];
  test?: TestInfo | null;
  onClose: () => void;
};

type ResultView = "artifacts" | "files" | "changes" | "preview";
type OpenTab = { id: string; view: ResultView | "file"; label: string; path?: string };

const VIEW_LABELS: Record<ResultView, string> = {
  artifacts: "产物",
  files: "文件",
  changes: "改动",
  preview: "预览",
};

const VIEW_ICON: Record<ResultView, string> = {
  artifacts: "m12 3 8 4-8 4-8-4zM4 12l8 4 8-4M4 17l8 4 8-4",
  files: "M3 7h7l2 2h9v9H3z",
  changes: "m8 9-4 3 4 3M16 9l4 3-4 3M14 5l-4 14",
  preview: "M3 5h18v12H3zM8 21h8M12 17v4",
};

export function TaskPanel(props: TaskPanelProps) {
  const [openTabs, setOpenTabs] = useState<OpenTab[]>([
    { id: "changes", view: "changes", label: "改动" },
    { id: "files", view: "files", label: "文件" },
  ]);
  const [activeTabId, setActiveTabId] = useState("changes");
  const [artifactQuery, setArtifactQuery] = useState("");
  const [copiedPath, setCopiedPath] = useState("");
  const [previewState, setPreviewState] = useState<PreviewState | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [fullscreen, setFullscreen] = useState(false);
  /** 面板宽度：窄的时候顶部标签只留图标与名称，避免把中文挤成竖排。 */
  const [panelWidth, setPanelWidth] = useState(PANEL_WIDTH_DEFAULT);
  const [treeEntries, setTreeEntries] = useState<TreeEntry[]>([]);
  const [fileContents, setFileContents] = useState<Record<string, FileContent>>({});
  const [fileImages, setFileImages] = useState<Record<string, string>>({});
  const [fileLoadingPath, setFileLoadingPath] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const panelRef = useRef<HTMLElement | null>(null);
  const savedWidth = useRef("");
  const fileImagesRef = useRef<Record<string, string>>({});
  const activeTab = openTabs.find((item) => item.id === activeTabId) ?? openTabs[0] ?? null;
  const activeView = activeTab?.view ?? "changes";
  const fileTabs = openTabs.filter((item) => item.view === "file");

  useEffect(() => {
    const stored = Number(readString(PANEL_WIDTH_KEY));
    const width = Number.isFinite(stored) && stored > 0 ? clampPanelWidth(stored) : PANEL_WIDTH_DEFAULT;
    if (panelRef.current && !fullscreen) panelRef.current.style.width = `${width}px`;
  }, [fullscreen]);

  useEffect(() => {
    const node = panelRef.current;
    if (!node) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (width > 0) setPanelWidth(width);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const compact = !fullscreen && panelWidth < 640;
  const narrow = !fullscreen && panelWidth < 420;

  useEffect(() => {
    fileImagesRef.current = fileImages;
  }, [fileImages]);

  useEffect(() => {
    return () => {
      Object.values(fileImagesRef.current).forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (activeView !== "files" || !props.projectId) return;
    let cancelled = false;
    void fetchTree(props.projectId, "", 4)
      .then((response) => {
        if (!cancelled) setTreeEntries(response.entries);
      })
      .catch(() => {
        if (!cancelled) setTreeEntries([]);
      });
    return () => {
      cancelled = true;
    };
  }, [activeView, props.projectId]);

  useEffect(() => {
    if (activeView !== "preview" || !props.projectId) return;
    let cancelled = false;
    void fetchPreview(props.projectId)
      .then((state) => {
        if (!cancelled) setPreviewState(state);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeView, props.projectId]);

  const setWidth = (width: number): number => {
    const next = clampPanelWidth(width);
    if (panelRef.current && !fullscreen) panelRef.current.style.width = `${next}px`;
    return next;
  };
  const persistWidth = (): void => {
    writeString(PANEL_WIDTH_KEY, String(Math.round(panelRef.current?.getBoundingClientRect().width ?? PANEL_WIDTH_DEFAULT)));
  };
  const startResize = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (fullscreen) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelRef.current?.getBoundingClientRect().width ?? PANEL_WIDTH_DEFAULT;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const move = (next: PointerEvent): void => {
      setWidth(startWidth + startX - next.clientX);
    };
    const stop = (): void => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      persistWidth();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (fullscreen) return;
    const current = panelRef.current?.getBoundingClientRect().width ?? PANEL_WIDTH_DEFAULT;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setWidth(current + 24);
      persistWidth();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setWidth(current - 24);
      persistWidth();
    } else if (event.key === "Home") {
      event.preventDefault();
      setWidth(PANEL_WIDTH_DEFAULT);
      persistWidth();
    }
  };
  const toggleFullscreen = (): void => {
    if (!panelRef.current) return;
    if (!fullscreen) {
      savedWidth.current = panelRef.current.style.width;
      panelRef.current.style.width = "";
    } else {
      panelRef.current.style.width = savedWidth.current || `${PANEL_WIDTH_DEFAULT}px`;
    }
    setFullscreen(!fullscreen);
  };
  const openView = (view: ResultView): void => {
    const id = view;
    setOpenTabs((current) => (current.some((item) => item.id === id) ? current : [...current, { id, view, label: VIEW_LABELS[view] }]));
    setActiveTabId(id);
  };
  const openFileTab = (path: string, label?: string): void => {
    if (!props.projectId) return;
    const id = `file:${path}`;
    const name = label || path.split("/").pop() || path;
    setOpenTabs((current) => (current.some((item) => item.id === id) ? current : [...current, { id, view: "file", label: name, path }]));
    setActiveTabId(id);
    if (fileContents[path] || fileImages[path] || fileLoadingPath === path) return;
    setFileLoadingPath(path);
    if (isImagePath(path)) {
      void fetchFileRaw(props.projectId, path)
        .then((blob) => {
          const url = URL.createObjectURL(blob);
          setFileImages((current) => ({ ...current, [path]: url }));
        })
        .catch(() => undefined)
        .finally(() => setFileLoadingPath((current) => (current === path ? null : current)));
      return;
    }
    void fetchFile(props.projectId, path)
      .then((content) => setFileContents((current) => ({ ...current, [path]: content })))
      .catch(() => undefined)
      .finally(() => setFileLoadingPath((current) => (current === path ? null : current)));
  };
  const closeTab = (id: string): void => {
    const index = openTabs.findIndex((item) => item.id === id);
    const next = openTabs.filter((item) => item.id !== id);
    setOpenTabs(next);
    if (activeTabId === id) setActiveTabId(next[Math.min(index, next.length - 1)]?.id ?? "");
  };
  const shareActive = (): void => {
    const value = activeTab?.path ?? props.title;
    void navigator.clipboard?.writeText(value).catch(() => undefined);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };
  const copyArtifactPath = (path: string): void => {
    void navigator.clipboard?.writeText(path).catch(() => undefined);
    setCopiedPath(path);
    window.setTimeout(() => setCopiedPath((current) => (current === path ? "" : current)), 1600);
  };
  const downloadArtifacts = (format: "json" | "markdown"): void => {
    if (!artifacts.length) return;
    const text = artifactsExport(artifacts, format);
    const blob = new Blob([text], {
      type: format === "json" ? "application/json;charset=utf-8" : "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `artifacts-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${format === "json" ? "json" : "md"}`;
    anchor.click();
    URL.revokeObjectURL(url);
  };
  const beginPreview = (): void => {
    if (!props.projectId) return;
    setPreviewBusy(true);
    setPreviewError("");
    void startPreview(props.projectId)
      .then((state) => setPreviewState(state))
      .catch((error: unknown) => setPreviewError(errorMessage(error)))
      .finally(() => setPreviewBusy(false));
  };
  const endPreview = (): void => {
    if (!props.projectId) return;
    setPreviewBusy(true);
    setPreviewError("");
    void stopPreview(props.projectId)
      .then((state) => setPreviewState(state))
      .catch((error: unknown) => setPreviewError(errorMessage(error)))
      .finally(() => setPreviewBusy(false));
  };

  const stepChanges = changesFromSteps(props.steps);
  const files = uniqueChanges([...props.changes, ...(props.workspaceChanges ?? []), ...stepChanges]);
  const artifacts = files.filter((change) => change.changeType === "created" || change.diff.startsWith("diff --git"));
  const artifactGroups = groupArtifacts(artifacts, artifactQuery);
  const status = statusOf(props.status, props.running);
  const viewCounts: Record<ResultView, number> = {
    artifacts: artifacts.length,
    files: treeEntries.length,
    changes: files.length,
    preview: 0,
  };
  const activeFileContent = activeTab?.path ? fileContents[activeTab.path] ?? null : null;
  const activeImageUrl = activeTab?.path ? fileImages[activeTab.path] ?? null : null;

  return (
    <aside
      ref={panelRef}
      className={
        fullscreen
          ? "glass fixed inset-0 z-50 flex w-screen flex-col"
          : "glass relative flex w-[620px] shrink-0 flex-col"
      }
    >
      {!fullscreen && (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="调整面板宽度，双击恢复默认"
          tabIndex={0}
          onPointerDown={startResize}
          onDoubleClick={() => {
            setWidth(PANEL_WIDTH_DEFAULT);
            persistWidth();
          }}
          onKeyDown={resizeWithKeyboard}
          className="group absolute -left-1 top-0 z-30 h-full w-2 cursor-col-resize outline-none"
        >
          <span className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-transparent transition-colors group-hover:bg-brand/60 group-focus-visible:bg-brand/80" />
        </div>
      )}

      <div className="relative flex h-12 shrink-0 items-center gap-2 border-b border-line/70 bg-transparent px-3">
        <span className="grid size-6 shrink-0 place-items-center rounded-lg bg-brand/10 text-brand">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5" aria-hidden>
            <path d="m12 3 8 4-8 4-8-4zM4 12l8 4 8-4M4 17l8 4 8-4" />
          </svg>
        </span>
        <span className="shrink-0 text-[12px] font-semibold tracking-[-0.01em]">结果</span>

        <div className="ml-2 flex min-w-0 items-center gap-0.5 overflow-x-auto rounded-lg bg-surface-2/70 p-0.5">
          {(["artifacts", "changes", "files", "preview"] as ResultView[]).map((view) => {
            const active = activeView === view;
            return (
              <button
                key={view}
                type="button"
                onClick={() => openView(view)}
                title={`${VIEW_LABELS[view]} · ${viewCounts[view]}`}
                className={`flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-2 text-[11px] transition-colors ${
                  active ? "bg-surface text-text shadow-[var(--shadow-sm)]" : "text-muted hover:text-text"
                }`}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5 shrink-0" aria-hidden>
                  <path d={VIEW_ICON[view]} />
                </svg>
                {/* 按面板实际宽度取舍：先掉数量，再掉文字。此前按视口 lg 判断，
                    面板被拖窄后四个中文标签仍全显示，字被挤成竖排。 */}
                {!narrow && <span>{VIEW_LABELS[view]}</span>}
                {!compact && <span className="font-mono text-[9px] text-faint">{viewCounts[view]}</span>}
              </button>
            );
          })}
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={toggleFullscreen}
            title={fullscreen ? "退出全屏" : "全屏查看结果"}
            className="grid size-7 place-items-center rounded-md text-faint transition-colors hover:bg-surface-hover hover:text-text"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5" aria-hidden>
              {fullscreen ? <path d="M9 3v6H3M15 21v-6h6M15 3v6h6M9 21v-6H3" /> : <path d="M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6" />}
            </svg>
          </button>
          <button
            type="button"
            onClick={props.onClose}
            title="收起面板"
            className="grid size-7 place-items-center rounded-md text-faint transition-colors hover:bg-surface-hover hover:text-text"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5" aria-hidden>
              <path d="m14 6-6 6 6 6" />
            </svg>
          </button>
        </div>
      </div>

      {fileTabs.length > 0 && (
        <div className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto border-b border-line/70 px-2">
          {fileTabs.map((tab) => {
            const active = tab.id === activeTabId;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTabId(tab.id)}
                title={tab.path || tab.label}
                className={`group flex h-7 max-w-[220px] shrink-0 items-center gap-1.5 rounded-md px-2 text-[11px] transition-colors ${
                  active ? "bg-surface text-text shadow-[var(--shadow-sm)]" : "text-muted hover:bg-surface-hover hover:text-text"
                }`}
              >
                <span className="min-w-0 truncate">{tab.label}</span>
                <span
                  onClick={(event) => {
                    event.stopPropagation();
                    closeTab(tab.id);
                  }}
                  className="grid size-3.5 shrink-0 place-items-center rounded text-faint hover:bg-surface-3 hover:text-text"
                  title="关闭"
                >
                  ×
                </span>
              </button>
            );
          })}
        </div>
      )}

      {/* 「改动」视图直接从文件条开始：标题/搜索/范围/证据/活动这一整块已去掉，
          只保留顶部四个视图切换。其他视图仍需要标签标题、状态与复制路径。 */}
      {activeView !== "changes" && (
        <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line/70 bg-transparent px-3">
          <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-text" title={activeTab?.path || activeTab?.label || "结果"}>
            {activeTab?.label || "结果"}
          </span>
          {activeTab?.path && (
            <span className="hidden min-w-0 max-w-[240px] truncate font-mono text-[10px] text-faint sm:block" title={activeTab.path}>
              {activeTab.path}
            </span>
          )}
          <span className={`hidden rounded-full px-2 py-0.5 text-[10px] font-medium sm:inline-flex ${status.badge}`}>{status.label}</span>
          <button
            type="button"
            onClick={shareActive}
            title={copied ? "已复制" : "复制当前路径"}
            className="grid size-7 shrink-0 place-items-center rounded-md text-faint transition-colors hover:bg-surface-hover hover:text-text"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5" aria-hidden>
              {copied ? <path d="m5 13 4 4L19 7" /> : <path d="M9 9h10v10H9zM5 15V5h10" />}
            </svg>
          </button>
        </div>
      )}

      <div className={activeView === "file" ? "min-h-0 flex-1 overflow-hidden" : "min-h-0 flex-1 overflow-hidden px-3 pb-3 pt-2"}>
        {activeView === "changes" && (
          <div className="min-h-0 flex-1 overflow-hidden">
            <CodeChangeView changes={files} scopeLabel="本会话" treeMode={false} />
          </div>
        )}

        {activeView === "artifacts" && (
          <div className="flex h-full flex-col rounded-[var(--radius-md)] border border-line bg-surface">
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line px-3 py-2">
              <input
                type="text"
                value={artifactQuery}
                onChange={(event) => setArtifactQuery(event.target.value)}
                placeholder="搜索产物路径"
                aria-label="搜索产物路径"
                className="field min-w-0 flex-1 rounded-md px-2 py-1 text-[12px] outline-none"
              />
              <span className="shrink-0 font-mono text-[11px] text-faint">
                {artifactGroups.reduce((sum, group) => sum + group.items.length, 0)} / {artifacts.length}
              </span>
              <button
                type="button"
                disabled={!artifacts.length}
                onClick={() => downloadArtifacts("markdown")}
                className="rounded-md border border-line bg-surface px-2 py-1 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
              >
                导出 MD
              </button>
              <button
                type="button"
                disabled={!artifacts.length}
                onClick={() => downloadArtifacts("json")}
                className="rounded-md border border-line bg-surface px-2 py-1 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
              >
                JSON
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              {artifacts.length === 0 ? (
                <RailEmpty
                  compact
                  fill
                  icon={<RailIcon d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" />}
                  title="还没有产物"
                  body="任务生成或新写入的文件会出现在这里。"
                />
              ) : artifactGroups.length === 0 ? (
                <RailEmpty
                  compact
                  fill
                  icon={<RailIcon d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />}
                  title="没有匹配的产物"
                  body={`没有路径包含「${artifactQuery.trim()}」；清空搜索可看全部 ${artifacts.length} 个。`}
                />
              ) : (
                <div className="flex flex-col gap-4">
                  {artifactGroups.map((group) => (
                    <section key={group.kind}>
                      <div className="mb-2 flex items-center gap-2">
                        <h3 className="text-[12px] font-medium text-text">{group.label}</h3>
                        <span className="font-mono text-[10px] text-faint">{group.items.length}</span>
                      </div>
                      <div className="grid gap-2 sm:grid-cols-2">
                        {group.items.map((change) => {
                          const stats = diffStats(change.diff);
                          return (
                            <div
                              key={change.path}
                              className="rounded-[var(--radius-md)] border border-line bg-surface-2 p-3 transition-colors hover:border-brand/40"
                            >
                              <button
                                type="button"
                                onClick={() => openFileTab(change.path)}
                                className="block w-full text-left"
                              >
                                <span className="block truncate font-mono text-[12px] text-text" title={change.path}>{change.path}</span>
                                <span className="mt-2 flex items-center gap-2 font-mono text-[10px]">
                                  <span className="text-accent">+{stats.added}</span>
                                  <span className="text-danger">−{stats.removed}</span>
                                  <span className="text-faint">{change.changeType || "modified"}</span>
                                </span>
                              </button>
                              <div className="mt-2 flex items-center justify-between gap-2 border-t border-line pt-2">
                                <span className="min-w-0 truncate font-mono text-[10px] text-faint">
                                  {copiedPath === change.path ? "已复制路径" : ""}
                                </span>
                                <button
                                  type="button"
                                  onClick={() => copyArtifactPath(change.path)}
                                  className="shrink-0 rounded border border-line bg-surface px-2 py-0.5 text-[10px] text-muted hover:border-line-strong"
                                >
                                  复制路径
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </section>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {activeView === "files" && (
          <div className="flex h-full overflow-hidden rounded-[var(--radius-md)] border border-line bg-surface">
            <div className="w-[210px] shrink-0 overflow-y-auto border-r border-line/70 bg-transparent p-1.5">
              {!props.projectId ? (
                <p className="px-2 py-3 text-[11px] leading-relaxed text-faint">当前没有选择项目，无法浏览工作区文件。</p>
              ) : treeEntries.length === 0 ? (
                <p className="px-2 py-3 text-[11px] leading-relaxed text-faint">工作区里还没有可显示的文件。</p>
              ) : (
                treeEntries.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    disabled={entry.is_dir}
                    onClick={() => openFileTab(entry.path, entry.name)}
                    className={`flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left transition-colors ${
                      entry.is_dir ? "text-muted" : "text-text hover:bg-surface-hover"
                    }`}
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-3.5 shrink-0" aria-hidden>
                      {entry.is_dir ? <path d="M3 7h7l2 2h9v9H3z" /> : <path d="M6 3h8l4 4v14H6zM14 3v4h4" />}
                    </svg>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={entry.path}>{entry.name}</span>
                  </button>
                ))
              )}
            </div>
            <div className="grid min-w-0 flex-1 place-items-center px-4 text-center">
              <p className="text-[11px] leading-relaxed text-faint">从左侧选择文件，会打开为新标签页。</p>
            </div>
          </div>
        )}

        {activeView === "file" && (
          <div className="h-full bg-transparent">
            {fileLoadingPath === activeTab?.path ? (
              <div className="grid h-full place-items-center">
                <div className="text-center">
                  <div className="mx-auto size-8 animate-spin rounded-full border-2 border-line border-t-brand" />
                  <p className="mt-3 text-[11px] text-faint">页面加载中</p>
                </div>
              </div>
            ) : activeImageUrl ? (
              <div className="relative h-full">
                <Image
                  unoptimized
                  fill
                  src={activeImageUrl}
                  alt={activeTab?.label || "图片预览"}
                  sizes="(max-width: 768px) 100vw, 50vw"
                  className="object-contain p-4"
                />
              </div>
            ) : activeFileContent ? (
              <FilePreview
                path={activeTab?.path ?? ""}
                content={activeFileContent}
                files={files.map((change) => change.path)}
                onFileClick={openFileTab}
              />
            ) : (
              <div className="grid h-full place-items-center px-4 text-center">
                <p className="text-[11px] leading-relaxed text-danger">文件读取失败。</p>
              </div>
            )}
          </div>
        )}

        {activeView === "preview" && (
          <div className="flex h-full flex-col rounded-[var(--radius-md)] border border-line bg-surface">
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line px-3 py-2">
              <span className="min-w-0 flex-1 truncate text-[12px] text-text">
                {previewHeadline(previewState)}
              </span>
              {previewState?.status === "running" ? (
                <>
                  <span className="font-mono text-[10px] text-faint">{previewIsolation(previewState)}</span>
                  <span className="font-mono text-[10px] text-faint">{previewExpiry(previewState)}</span>
                  {previewState.url && (
                    <a
                      href={previewState.url}
                      target="_blank"
                      rel="noreferrer"
                      className="rounded-md border border-line bg-surface px-2 py-1 text-[11px] text-muted hover:border-line-strong"
                    >
                      新标签打开
                    </a>
                  )}
                  <button
                    type="button"
                    disabled={previewBusy}
                    onClick={() => endPreview()}
                    className="rounded-md border border-line bg-surface px-2 py-1 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
                  >
                    停止
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  disabled={previewBusy || !props.projectId}
                  onClick={() => beginPreview()}
                  className="rounded-md bg-brand px-2.5 py-1 text-[11px] font-medium text-on-brand hover:bg-brand-hover disabled:opacity-40"
                >
                  {previewBusy ? "启动中…" : "启动预览"}
                </button>
              )}
            </div>
            {previewError && (
              <p className="border-b border-line px-3 py-2 text-[11px] leading-relaxed text-danger">{previewError}</p>
            )}
            <div className="min-h-0 flex-1">
              {!props.projectId ? (
                <div className="grid h-full place-items-center px-4 text-center">
                  <p className="text-[11px] leading-relaxed text-faint">当前没有选择项目，无法启动预览。</p>
                </div>
              ) : previewState?.status === "running" && previewState.url ? (
                <iframe
                  title="应用预览"
                  src={previewState.url}
                  sandbox="allow-scripts allow-same-origin allow-forms"
                  referrerPolicy="no-referrer"
                  className="h-full w-full border-0 bg-white"
                />
              ) : (
                <div className="grid h-full place-items-center px-4 text-center">
                  <div className="max-w-[380px]">
                    <div className="mx-auto grid size-10 place-items-center rounded-md bg-brand/[0.08] text-brand">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} className="size-5" aria-hidden>
                        <path d="M3 5h18v12H3zM8 21h8M12 17v4" />
                      </svg>
                    </div>
                    <p className="mt-3 text-[13px] font-semibold text-text">应用预览</p>
                    <p className="mt-1 text-[11px] leading-relaxed text-faint">
                      只支持 <code className="font-mono">package.json</code> 的 <code className="font-mono">scripts.start</code> 为
                      <code className="font-mono"> node &lt;文件&gt;</code> 的项目；不执行 npm。
                      服务只绑 127.0.0.1，10 分钟后自动停止。
                    </p>
                    {previewState?.log_tail && (
                      <pre className="mt-3 max-h-40 overflow-auto rounded-md border border-line bg-surface-2 p-2 text-left font-mono text-[10px] leading-relaxed text-muted">
                        {previewState.log_tail}
                      </pre>
                    )}
                  </div>
                </div>
              )}
            </div>
            {previewState?.notes?.length ? (
              <div className="shrink-0 border-t border-line px-3 py-2">
                {previewState.notes.map((note, index) => (
                  <p key={`${index}-${note}`} className="text-[10px] leading-relaxed text-faint">{note}</p>
                ))}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </aside>
  );
}

function RailIcon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}
