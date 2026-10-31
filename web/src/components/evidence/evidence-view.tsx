"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { errorMessage } from "@/lib/http";
import { formatDateTime } from "@/components/tasks/format";
import {
  fetchEvidence,
  fetchProjects,
  fetchTasks,
  normalizeStatus,
  type Evidence,
  type Project,
  type Task,
} from "@/components/tasks/api";
import { criteriaText, evidenceDigest, hasEvidence, type EvidenceDigest } from "./format";

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  queued: "排队中",
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  cancelled: "已取消",
};

const STATUS_TONE: Record<string, string> = {
  running: "border-brand/30 bg-brand/10 text-brand",
  failed: "border-danger/25 bg-danger/[0.06] text-danger",
  cancelled: "border-line bg-surface-2 text-muted",
  succeeded: "border-accent/30 bg-accent/10 text-accent",
};

/**
 * 证据库：跨任务看"每次交付到底证明了什么"。
 *
 * 列表刻意不预先取每一条的证据——那是 N 次请求，一个 50 条的列表就能把首屏拖住。
 * 点开哪条取哪条，取回来的结果按任务 id 缓存，反复展开不再发请求。
 */
export function EvidenceView() {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string>("");
  const [cache, setCache] = useState<Record<string, Evidence | null>>({});
  const [loadingId, setLoadingId] = useState("");

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [taskRows, projectRows] = await Promise.all([fetchTasks(), fetchProjects()]);
        if (!alive) return;
        setTasks(taskRows);
        setProjects(projectRows);
      } catch (cause) {
        if (!alive) return;
        setError(errorMessage(cause));
        setTasks([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const projectName = useMemo(() => {
    const map = new Map(projects.map((item) => [item.id, item.name]));
    return (id: string) => map.get(id) ?? "";
  }, [projects]);

  const rows = useMemo(() => {
    const text = query.trim().toLowerCase();
    return (tasks ?? []).filter((task) => (text ? task.title.toLowerCase().includes(text) : true));
  }, [tasks, query]);

  const expand = useCallback(
    async (task: Task) => {
      if (open === task.id) {
        setOpen("");
        return;
      }
      setOpen(task.id);
      // 取过就复用：展开是"看一眼"，不该每次都打一次后端。
      if (Object.prototype.hasOwnProperty.call(cache, task.id)) return;
      setLoadingId(task.id);
      setError("");
      try {
        const evidence = await fetchEvidence(task.id);
        setCache((rows) => ({ ...rows, [task.id]: evidence }));
      } catch (cause) {
        setError(errorMessage(cause));
        setCache((rows) => ({ ...rows, [task.id]: null }));
      } finally {
        setLoadingId("");
      }
    },
    [cache, open],
  );

  return (
    <div className="mx-auto w-full max-w-4xl px-5 py-6">
      <header className="mb-5">
        <h1 className="text-[17px] font-semibold tracking-tight">证据库</h1>
        <p className="mt-1.5 max-w-2xl text-[12.5px] leading-relaxed text-muted">
          每一次交付留下的验收条件、改动文件、测试结论与用量。点开一条看它当时凭什么说做完了——
          这里不做汇总美化，只把记录原样摆出来。
        </p>
      </header>

      <div className="mb-4 flex items-center gap-2">
        <div className="flex flex-1 items-center gap-1.5 rounded-lg border border-line-strong bg-surface px-2 py-1.5">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5 shrink-0 text-faint" aria-hidden>
            <path d="M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16.5 16.5 21 21" />
          </svg>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按任务名筛选"
            className="w-full bg-transparent text-[12.5px] focus:outline-none"
          />
        </div>
        <span className="shrink-0 font-mono text-[10.5px] text-faint">{rows.length} 条</span>
      </div>

      {error && (
        <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[12px] text-danger">{error}</p>
      )}

      <Panel title="最近的交付" hint={tasks === null ? "读取中…" : "点开看证据"}>
        {tasks === null && (
          <div className="space-y-2">
            {[0, 1, 2].map((row) => (
              <div key={row} className="h-9 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        )}

        {tasks !== null && rows.length === 0 && (
          <p className="py-6 text-center text-[12px] leading-relaxed text-faint">
            {tasks.length === 0 ? "还没有任何交付记录。" : "没有匹配的任务。"}
          </p>
        )}

        <div className="space-y-1.5">
          {rows.map((task) => {
            const status = normalizeStatus(task.status);
            const expanded = open === task.id;
            const evidence = cache[task.id] ?? null;
            const digest = evidence ? evidenceDigest(evidence) : null;
            return (
              <div key={task.id} className="rounded-lg border border-line/70 bg-surface/40">
                <button
                  type="button"
                  onClick={() => void expand(task)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left"
                >
                  <span className="shrink-0 font-mono text-[10px] text-faint">{expanded ? "▾" : "▸"}</span>
                  <span className="min-w-0 flex-1 truncate text-[12.5px]">{task.title}</span>
                  <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${STATUS_TONE[status] ?? "border-line bg-surface-2 text-muted"}`}>
                    {STATUS_LABEL[status] ?? status}
                  </span>
                  {digest && <DigestChips digest={digest} />}
                  <span className="hidden shrink-0 font-mono text-[10px] text-faint sm:block">
                    {formatDateTime(task.createdAt)}
                  </span>
                </button>

                {expanded && (
                  <div className="border-t border-line/70 px-3 py-2.5">
                    {loadingId === task.id && <p className="font-mono text-[11px] text-faint">读取证据…</p>}
                    {!loadingId && evidence === null && (
                      <p className="text-[11.5px] text-faint">这条没有可读的证据记录。</p>
                    )}
                    {evidence && (
                      <div className="space-y-3">
                        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10.5px] text-faint">
                          <span>项目：{projectName(task.projectId) || task.projectId || "未记录"}</span>
                          <span>对话轮次：{task.threadId ? `第 ${task.turnIndex} 轮` : "独立任务"}</span>
                          {digest && <span>{criteriaText(digest)}</span>}
                          {digest && <span>{digest.testText}</span>}
                          {digest?.tokens != null && <span>用量：{digest.tokens.toLocaleString()} token</span>}
                        </div>

                        {evidence.acceptance.length > 0 && (
                          <div>
                            <p className="mb-1 text-[11.5px] font-medium text-muted">验收条件</p>
                            <ul className="space-y-0.5">
                              {evidence.acceptance.map((item, index) => (
                                <li key={index} className="flex items-start gap-1.5 text-[11.5px] leading-relaxed">
                                  <span className={item.covered === true ? "text-accent" : item.covered === false ? "text-danger" : "text-faint"}>
                                    {item.covered === true ? "✓" : item.covered === false ? "✗" : "?"}
                                  </span>
                                  <span className="min-w-0 flex-1">
                                    {item.criterion}
                                    {item.note && <span className="text-faint"> · {item.note}</span>}
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}

                        {evidence.changes.length > 0 && (
                          <div>
                            <p className="mb-1 text-[11.5px] font-medium text-muted">改动文件</p>
                            <ul className="space-y-0.5 font-mono text-[10.5px] text-muted">
                              {uniquePaths(evidence).map((row) => (
                                <li key={row.path} className="truncate">
                                  {row.changeType === "created" ? "+ " : row.changeType === "deleted" ? "- " : "~ "}
                                  {row.path}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}

                        {evidence.test?.command && (
                          <div>
                            <p className="mb-1 text-[11.5px] font-medium text-muted">测试</p>
                            <pre className="max-h-40 overflow-auto rounded-md border border-line bg-surface px-2 py-1.5 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap text-muted">
                              {`$ ${evidence.test.command}`}
                              {evidence.test.output ? `\n${evidence.test.output.slice(0, 1200)}` : ""}
                            </pre>
                          </div>
                        )}

                        {digest && !hasEvidence(digest) && (
                          <p className="text-[11.5px] text-faint">这条交付没有留下验收、改动、测试或用量的记录。</p>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}

function uniquePaths(evidence: Evidence) {
  const seen = new Map<string, { path: string; changeType: string }>();
  for (const row of evidence.changes) {
    if (row.path && !seen.has(row.path)) seen.set(row.path, { path: row.path, changeType: row.changeType });
  }
  return [...seen.values()];
}

/** 列表行上的四格摘要：不点开也能看出这次交付大概齐不齐。 */
function DigestChips({ digest }: { digest: EvidenceDigest }) {
  return (
    <span className="hidden shrink-0 items-center gap-1 md:flex">
      <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 font-mono text-[9.5px] text-muted">
        {digest.covered}/{digest.criteriaTotal} 验收
      </span>
      <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 font-mono text-[9.5px] text-muted">
        {digest.files} 文件
      </span>
      <span
        className={`rounded border px-1 py-0.5 font-mono text-[9.5px] ${
          digest.test === "passed"
            ? "border-accent/30 bg-accent/10 text-accent"
            : digest.test === "failed"
              ? "border-danger/25 bg-danger/[0.06] text-danger"
              : "border-line bg-surface-2/60 text-faint"
        }`}
      >
        {digest.test === "passed" ? "测试通过" : digest.test === "failed" ? "测试失败" : digest.testText}
      </span>
    </span>
  );
}
