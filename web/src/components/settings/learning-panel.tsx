"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import { errorMessage, fetchProjects, type Project } from "@/components/projects/api";
import {
  fetchLearning,
  promotePreference,
  retireExperience,
  retirePreference,
  type LearningLint,
  type LearningSnapshot,
} from "./learning-api";
import {
  activeCount,
  kindLabel,
  lintCount,
  originLabel,
  quoteText,
  statusLabel,
} from "./learning";

/**
 * 自学习：它学到的经验和你的一贯要求，以及要不要撤掉。
 *
 * 展示与撤销之外什么都不做：经验仍自动召回 1 条，偏好仍按作用域注入，
 * 技能提案仍要人采用。这里不提供"一键清空"——退役是逐条判断，不是
 * 信任崩了就全删。
 */
export function LearningPanel() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState("");
  const [snapshot, setSnapshot] = useState<{ projectId: string; data: LearningSnapshot | null; error: string } | null>(null);
  const [projectsError, setProjectsError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [showRetired, setShowRetired] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((rows) => {
        if (!alive) return;
        setProjects(rows);
        const bound = rows.find((item) => item.workspaces.length > 0);
        setProjectId((current) => current || bound?.id || "");
      })
      .catch((error) => {
        if (alive) setProjectsError(errorMessage(error));
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    fetchLearning(projectId)
      .then((data) => {
        if (alive) setSnapshot({ projectId, data, error: "" });
      })
      .catch((error) => {
        if (alive) setSnapshot({ projectId, data: null, error: errorMessage(error) });
      });
    return () => {
      alive = false;
    };
  }, [projectId, reloadToken]);

  const bound = (projects ?? []).filter((item) => item.workspaces.length > 0);
  const loaded = snapshot && snapshot.projectId === projectId ? snapshot : null;
  const data = loaded?.data ?? null;
  const experiences = data?.experience.entries ?? [];
  const preferences = data?.persona.entries ?? [];
  const experienceVisible = showRetired ? experiences : experiences.filter((item) => item.status !== "retired");
  const preferenceVisible = showRetired ? preferences : preferences.filter((item) => item.status !== "retired");
  const lint: LearningLint[] = [
    ...(data?.experience.lint ?? []),
    ...(data?.persona.lint ?? []),
  ];

  async function run(key: string, action: () => Promise<unknown>, message: string) {
    setBusy(key);
    setNotice("");
    try {
      await action();
      setNotice(message);
      setReloadToken((token) => token + 1);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title="自学习"
        hint="它学到的经验与你的偏好；看得见、撤得掉"
        action={
          bound.length > 1 ? (
            <SelectPopover
              value={projectId}
              onChange={setProjectId}
              options={bound.map((item) => ({ value: item.id, label: item.name }))}
              ariaLabel="选择项目"
            />
          ) : undefined
        }
      >
        {projectsError && <p className="text-[13px] text-danger">项目列表读取失败：{projectsError}</p>}
        {loaded?.error && <p className="text-[13px] text-danger">自学习读取失败：{loaded.error}</p>}
        {!bound.length && !projectsError && (
          <p className="text-[13px] leading-relaxed text-faint">先绑定一个工作区，自学习才有项目可挂。</p>
        )}
        {!loaded && !projectsError && bound.length > 0 && (
          <p className="font-mono text-[12px] text-faint">读取自学习…</p>
        )}
        {data && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
            <span>
              经验 {experiences.length} 条 · 生效 <span className="text-text">{activeCount(data.experience.stats)}</span>
            </span>
            <span>
              偏好 {preferences.length} 条 · 生效 <span className="text-text">{activeCount(data.persona.stats)}</span>
            </span>
            <span className={lint.length ? "text-warn" : "text-faint"}>体检 {lintCount(data.experience.lint, data.persona.lint)} 条</span>
            {experiences.length + preferences.length > 0 && (
              <button
                type="button"
                onClick={() => setShowRetired((value) => !value)}
                className="font-mono text-[11px] text-faint transition-colors hover:text-text"
              >
                {showRetired ? "隐藏已退役" : "显示已退役"}
              </button>
            )}
          </div>
        )}
        {data?.workspace && (
          <p className="mt-1 break-all font-mono text-[11px] text-faint" title="当前工作区">
            {data.workspace}
          </p>
        )}
        {notice && <p className="mt-2 text-[12px] leading-relaxed text-muted">{notice}</p>}
      </Panel>

      <Panel title="经验（学世界）" hint="这里会怎么坏、哪条命令能验；最多自动召回 1 条">
        {experienceVisible.length === 0 ? (
          <p className="text-[13px] leading-relaxed text-faint">
            还没有学到经验。跑几个真实任务、踩到有报错或退出码的坑之后，这里才会出现条目。
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            {experienceVisible.map((item) => (
              <div key={item.fingerprint} className="rounded-md border border-line bg-surface-2 p-3">
                <div className="flex items-start gap-2">
                  <span className="shrink-0 rounded bg-surface-3 px-1.5 py-0.5 text-[11px] text-muted">
                    {kindLabel(item.kind)}
                  </span>
                  <p className="min-w-0 flex-1 text-[13px] leading-relaxed text-text">{item.statement}</p>
                  <button
                    type="button"
                    disabled={busy === item.fingerprint}
                    onClick={() =>
                      void run(item.fingerprint, () => retireExperience(projectId, item.fingerprint), "这条经验已退役，不再被召回。")
                    }
                    className="shrink-0 rounded border border-line bg-surface px-2 py-0.5 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
                  >
                    退役
                  </button>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-faint">
                  <span>{statusLabel(item.status)}</span>
                  <span>置信 {item.confidence.toFixed(2)}</span>
                  <span>命中 {item.hits} / 未中 {item.misses}</span>
                  {item.expired && <span className="text-warn">已过期</span>}
                  {item.trigger_paths.length > 0 && <span>触发 {item.trigger_paths.join("、")}</span>}
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="偏好（学你）" hint="只有你亲口说过的才会进来；一次不算一贯">
        {preferenceVisible.length === 0 ? (
          <p className="text-[13px] leading-relaxed text-faint">
            还没有学到偏好。你在驳回时写下理由、或当面说「以后都先跑测试」这类要求，才会进这里。
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            {preferenceVisible.map((item) => {
              const quote = quoteText(item.quotes);
              const canPromote = item.origin === "harvested" && item.status === "candidate";
              return (
                <div key={item.fingerprint} className="rounded-md border border-line bg-surface-2 p-3">
                  <div className="flex items-start gap-2">
                    <span className="shrink-0 rounded bg-surface-3 px-1.5 py-0.5 text-[11px] text-muted">
                      {item.label || item.dimension}
                    </span>
                    <p className="min-w-0 flex-1 text-[13px] leading-relaxed text-text">{item.rule}</p>
                    <div className="flex shrink-0 items-center gap-1">
                      {canPromote && (
                        <button
                          type="button"
                          disabled={busy === item.fingerprint}
                          onClick={() =>
                            void run(item.fingerprint, () => promotePreference(projectId, item.fingerprint), "这条偏好已生效。")
                          }
                          className="rounded border border-brand/40 bg-brand/10 px-2 py-0.5 text-[11px] text-brand hover:bg-brand/15 disabled:opacity-40"
                        >
                          采用
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={busy === item.fingerprint}
                        onClick={() =>
                          void run(item.fingerprint, () => retirePreference(projectId, item.fingerprint), "这条偏好已退役，不再被注入。")
                        }
                        className="rounded border border-line bg-surface px-2 py-0.5 text-[11px] text-muted hover:border-line-strong disabled:opacity-40"
                      >
                        退役
                      </button>
                    </div>
                  </div>
                  {quote && (
                    <p className="mt-1.5 text-[12px] leading-relaxed text-faint">原话：{quote}</p>
                  )}
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-faint">
                    <span>{originLabel(item.origin)}</span>
                    <span>{statusLabel(item.status)}</span>
                    <span>出现 {item.occurrences} 次</span>
                    <span>作用域 {item.scope}</span>
                    {item.expired && <span className="text-warn">已过期</span>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      {lint.length > 0 && (
        <Panel title="体检" hint="只报告，不自动改">
          <ul className="flex flex-col gap-1.5 text-[12px] leading-relaxed text-muted">
            {lint.map((finding, index) => (
              <li key={`${finding.kind}-${index}`} className="flex flex-wrap gap-x-2">
                <span className="font-mono text-[11px] text-warn">{finding.kind}</span>
                <span className="min-w-0 flex-1">{finding.message}</span>
                {finding.evidence?.length ? (
                  <span className="w-full font-mono text-[10px] text-faint">{finding.evidence.join("；")}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}
