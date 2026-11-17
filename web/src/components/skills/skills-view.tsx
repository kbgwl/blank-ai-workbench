"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { PageBody, PageHeader } from "@/components/page";
import { errorMessage } from "@/lib/http";
import { formatDateTime } from "@/components/tasks/format";
import {
  acceptMemoryCandidate,
  acceptSkillProposal,
  deleteSkill,
  disableSkill,
  enableSkill,
  fetchMemoryCandidates,
  fetchSkills,
  rejectMemoryCandidate,
  type MemoryCandidate,
  type Skill,
} from "./api";
import { preview, SKILL_STATE_LABEL, skillState, skillSummary } from "./format";

const STATE_TONE: Record<ReturnType<typeof skillState>, string> = {
  active: "border-accent/30 bg-accent/10 text-accent",
  draft: "border-warn/30 bg-warn/10 text-warn",
  disabled: "border-line bg-surface-2 text-muted",
  proposed: "border-brand/30 bg-brand/10 text-brand",
  unknown: "border-danger/25 bg-danger/[0.06] text-danger",
};

function metaText(skill: Skill, key: string): string {
  const value = skill.metadata?.[key];
  return typeof value === "string" ? value : "";
}

/**
 * 技能：用户确认过的做法，启用后会作为「工作偏好」进入每一轮的系统提示。
 *
 * 这里只做三件事：看见有哪些、启用 / 停用、确认或拒掉一条候选记忆。
 * 刻意不做在线编辑——技能的正文是给模型看的，随手改一句就可能与安全规则冲突，
 * 那种改动应该在草案阶段（来源那一段）说清楚，而不是在这里临时修文。
 */
export function SkillsView() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [candidates, setCandidates] = useState<MemoryCandidate[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  // 初次加载在 effect 里做（无 query 库，fetch 是唯一入口）。
  // alive 守卫保证：卸载后不继续 setState；effect 里没有同步 setState
  // （所有落 state 都在 await 之后），不触发 set-state-in-effect。
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [skillRows, candidateRows] = await Promise.all([fetchSkills(), fetchMemoryCandidates()]);
        if (!alive) return;
        setSkills(skillRows);
        setCandidates(candidateRows);
        setError("");
      } catch (cause) {
        if (!alive) return;
        setError(errorMessage(cause));
        setSkills([]);
        setCandidates([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  async function toggle(skill: Skill) {
    const active = skillState(skill.status) === "active";
    setBusy(skill.id);
    setError("");
    try {
      const updated = active ? await disableSkill(skill.id) : await enableSkill(skill.id);
      setSkills((rows) => (rows ?? []).map((row) => (row.id === updated.id ? updated : row)));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  async function remove(skill: Skill) {
    // 删除不可恢复（§7.2"记忆可以删除"）：先问一句，不搞二次确认弹层。
    if (!window.confirm(`删除「${skill.name}」？删除后不可恢复。`)) return;
    setBusy(skill.id);
    setError("");
    try {
      await deleteSkill(skill.id);
      setSkills((rows) => (rows ?? []).filter((row) => row.id !== skill.id));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  async function decide(candidate: MemoryCandidate, accept: boolean) {
    setBusy(candidate.id);
    setError("");
    try {
      if (accept) await acceptMemoryCandidate(candidate.id);
      else await rejectMemoryCandidate(candidate.id);
      // 接受会顺手生成一条 draft 技能，列表必须一起刷新——否则用户点完"接受"，
      // 技能那一栏还是空的，会以为没生效。
      const [skillRows, candidateRows] = await Promise.all([fetchSkills(), fetchMemoryCandidates()]);
      setSkills(skillRows);
      setCandidates(candidateRows);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  async function adopt(proposal: Skill) {
    setBusy(proposal.id);
    setError("");
    try {
      // improvement 会并入目标 Skill（提案行随之消失），new 会转成 draft；
      // 两种都得整表重读，不能只替换一行。
      await acceptSkillProposal(proposal.id);
      const rows = await fetchSkills();
      setSkills(rows);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  async function discardProposal(proposal: Skill) {
    if (!window.confirm(`丢弃提案「${proposal.name}」？丢弃后不可恢复。`)) return;
    setBusy(proposal.id);
    setError("");
    try {
      await deleteSkill(proposal.id);
      setSkills((rows) => (rows ?? []).filter((row) => row.id !== proposal.id));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  const proposals = (skills ?? []).filter((row) => skillState(row.status) === "proposed");
  const confirmed = (skills ?? []).filter((row) => skillState(row.status) !== "proposed");
  const pending = (candidates ?? []).filter((row) => row.status === "pending");

  return (
    <PageBody>
      <PageHeader
        title="技能"
        description="技能是你确认过的做法，启用后会作为「工作偏好」进入每一轮任务的系统提示。它压不过安全规则、审批规则和这次任务的目标——只能影响做事的方式。"
      />

      {error && (
        <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[13px] text-danger">
          {error}
        </p>
      )}

      <div className="flex flex-col gap-4">
        {proposals.length > 0 && (
          <Panel title="待采用的提案" hint={`${proposals.length} 条模型提的建议`}>
            <p className="mb-2 text-[12.5px] leading-relaxed text-muted">
              这些是模型在任务里主动提的技能建议。采用才会落成草案或并入目标技能；
              丢弃后就从列表里消失。
            </p>
            <div className="space-y-1.5">
              {proposals.map((proposal) => {
                const target = metaText(proposal, "target");
                const kind = metaText(proposal, "kind");
                return (
                  <div
                    key={proposal.id}
                    className="flex items-start gap-3 rounded-lg border border-brand/25 bg-brand/[0.03] px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-[13.5px] font-medium">{proposal.name}</span>
                        <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${STATE_TONE.proposed}`}>
                          {kind === "improvement" ? "改进" : "新技能"}
                        </span>
                      </div>
                      {target && (
                        <p className="mt-0.5 font-mono text-[11px] text-faint">目标：{target}</p>
                      )}
                      <p className="mt-1 text-[12.5px] leading-relaxed text-muted">
                        {metaText(proposal, "description") || preview(proposal.instructions) || "（没有说明）"}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <button
                        type="button"
                        disabled={busy === proposal.id}
                        onClick={() => void adopt(proposal)}
                        className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12.5px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
                      >
                        采用
                      </button>
                      <button
                        type="button"
                        disabled={busy === proposal.id}
                        onClick={() => void discardProposal(proposal)}
                        className="rounded-md border border-line-strong bg-surface px-2 py-1 text-[12.5px] text-muted transition-colors hover:text-danger disabled:opacity-50"
                      >
                        丢弃
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </Panel>
        )}

        <Panel title="已确认的做法" hint={skills === null ? "读取中…" : skillSummary(confirmed.map((s) => s.status))}>
          {skills === null && <div className="space-y-2">{[0, 1].map((row) => <div key={row} className="h-10 animate-pulse rounded-md bg-surface-2" />)}</div>}

          {skills !== null && confirmed.length === 0 && (
            <p className="py-4 text-center text-[13px] leading-relaxed text-faint">
              还没有技能。
              <br />
              下面的候选记忆被接受之后，会先落成一份草案——草案不启用就不影响任何任务。
            </p>
          )}

          <div className="space-y-1.5">
            {confirmed.map((skill) => {
              const state = skillState(skill.status);
              const active = state === "active";
              return (
                <div
                  key={skill.id}
                  className="flex items-start gap-3 rounded-lg border border-line bg-surface px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-[13.5px] font-medium">{skill.name}</span>
                      <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${STATE_TONE[state]}`}>
                        {SKILL_STATE_LABEL[state]}
                      </span>
                      <span className="shrink-0 font-mono text-[11px] text-faint">v{skill.version}</span>
                    </div>
                    <p className="mt-1 text-[12.5px] leading-relaxed text-muted">{preview(skill.instructions) || "（没有说明）"}</p>
                    <p className="mt-1 font-mono text-[11px] text-faint">更新于 {formatDateTime(skill.updated_at)}</p>
                  </div>
                  <button
                    type="button"
                    disabled={busy === skill.id}
                    onClick={() => void toggle(skill)}
                    className={`shrink-0 rounded-md border px-2 py-1 text-[12.5px] transition-colors disabled:opacity-50 ${
                      active
                        ? "border-line-strong bg-surface text-muted hover:text-danger"
                        : "border-brand/40 bg-brand/10 text-brand hover:bg-brand/15"
                    }`}
                  >
                    {busy === skill.id ? "…" : active ? "停用" : "启用"}
                  </button>
                  <button
                    type="button"
                    disabled={busy === skill.id}
                    onClick={() => void remove(skill)}
                    title="删除这条记忆（不可恢复）"
                    className="shrink-0 rounded-md border border-transparent px-2 py-1 text-[12.5px] text-faint transition-colors hover:border-danger/30 hover:text-danger disabled:opacity-50"
                  >
                    删除
                  </button>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel
          title="待确认的记忆"
          hint={candidates === null ? "读取中…" : pending.length > 0 ? `${pending.length} 条等你定` : "没有待定的"}
        >
          {candidates !== null && pending.length === 0 && (
            <p className="py-4 text-center text-[13px] leading-relaxed text-faint">
              没有待确认的记忆。
              <br />
              只有你点过确认的做法才会变成技能——它不会自己记。
            </p>
          )}

          <div className="space-y-1.5">
            {pending.map((candidate) => (
              <div
                key={candidate.id}
                className="flex items-start gap-3 rounded-lg border border-line bg-surface px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-[13.5px] leading-relaxed">{candidate.text}</p>
                  <p className="mt-1 font-mono text-[11px] text-faint">
                    {candidate.source || "未标来源"} · {formatDateTime(candidate.created_at)}
                  </p>
                </div>
                <div className="flex shrink-0 gap-1">
                  <button
                    type="button"
                    disabled={busy === candidate.id}
                    onClick={() => void decide(candidate, true)}
                    className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12.5px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
                  >
                    接受
                  </button>
                  <button
                    type="button"
                    disabled={busy === candidate.id}
                    onClick={() => void decide(candidate, false)}
                    className="rounded-md border border-line-strong bg-surface px-2 py-1 text-[12.5px] text-muted transition-colors hover:text-danger disabled:opacity-50"
                  >
                    拒绝
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </PageBody>
  );
}
