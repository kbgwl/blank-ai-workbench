"use client";

import { useCallback, useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { PageBody, PageHeader } from "@/components/page";
import { errorMessage } from "@/lib/http";
import { SLASH_COMMANDS } from "@/lib/slash-commands";
import { formatDateTime } from "@/components/tasks/format";
import {
  acceptMemoryCandidate,
  acceptSkillProposal,
  deleteSkill,
  disableSkill,
  enableSkill,
  fetchMemoryCandidates,
  fetchSkillLibrary,
  fetchSkills,
  installSkill,
  uninstallSkill,
  rejectMemoryCandidate,
  type MemoryCandidate,
  type Skill,
  type SkillLibrary,
  type SkillLibraryItem,
} from "./api";
import { preview, SKILL_STATE_LABEL, skillState, skillSummary } from "./format";
import { SkillMarketPanel } from "./market-panel";

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
  const [library, setLibrary] = useState<SkillLibrary | null>(null);
  // 安装表单：来源 / 范围 / 覆盖。安装是唯一会往磁盘写东西的动作，
  // 所以默认收起，点开才出现，且每次结果都要明说。
  const [installing, setInstalling] = useState(false);
  const [installSource, setInstallSource] = useState("");
  const [installScope, setInstallScope] = useState<"user" | "project">("user");
  const [installOverwrite, setInstallOverwrite] = useState(false);
  const [installBusy, setInstallBusy] = useState(false);
  const [installNote, setInstallNote] = useState<{ text: string; danger?: boolean } | null>(null);
  const [removing, setRemoving] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const refreshLibrary = useCallback(async () => {
    const next = await fetchSkillLibrary().catch(() => null);
    if (next) setLibrary(next);
  }, []);

  async function runInstall() {
    setInstallBusy(true);
    setInstallNote(null);
    try {
      const result = await installSkill({
        source: installSource.trim(),
        scope: installScope,
        overwrite: installOverwrite,
      });
      setInstallNote({ text: `已安装「${result.name}」，刷新后进入技能库。` });
      setInstallSource("");
      await refreshLibrary();
    } catch (cause) {
      setInstallNote({ text: errorMessage(cause), danger: true });
    } finally {
      setInstallBusy(false);
    }
  }

  async function runUninstall(item: SkillLibraryItem) {
    if (!window.confirm(`删除技能「${item.display_name || item.name}」？目录会被整个删掉，不可恢复。`)) return;
    setRemoving(item.name);
    setInstallNote(null);
    try {
      await uninstallSkill({ name: item.name, scope: item.source === "user" ? "user" : "project", path: item.path });
      await refreshLibrary();
    } catch (cause) {
      setInstallNote({ text: errorMessage(cause), danger: true });
    } finally {
      setRemoving("");
    }
  }

  // 初次加载在 effect 里做（无 query 库，fetch 是唯一入口）。
  // alive 守卫保证：卸载后不继续 setState；effect 里没有同步 setState
  // （所有落 state 都在 await 之后），不触发 set-state-in-effect。
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [skillRows, candidateRows, library] = await Promise.all([
          fetchSkills(),
          fetchMemoryCandidates(),
          // 技能库单独失败不拖垮整页：记忆技能是主体，库只是"还能用什么"的补充。
          fetchSkillLibrary().catch(() => null),
        ]);
        if (!alive) return;
        setSkills(skillRows);
        setCandidates(candidateRows);
        setLibrary(library);
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
    <PageBody narrow>
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
        {/* 先说"你能用什么"。之前这一页只有模型从对话里学到的做法，
            用户完全不知道输入框里还有斜杠命令可用。 */}
        <Panel title="对话里能用的命令" hint="在输入框打 / 触发">
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {SLASH_COMMANDS.map((command) => (
              <div
                key={command.id}
                className="flex min-w-0 items-baseline gap-2 rounded-md border border-line bg-surface-2/50 px-2.5 py-1.5"
              >
                <span className="shrink-0 font-mono text-[12px] text-text">{command.label}</span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-faint">{command.hint}</span>
              </div>
            ))}
          </div>
        </Panel>

        {/* 技能库：真正会被加载的 SKILL.md（内置 + 我的 + 项目 + 插件）。
            以前这一页只列"记忆技能"，用户以为产品只有几条能力——
            而运行时明明还有随包内置的那一套。 */}
        {library && (
          <Panel
            title="技能库"
            hint={`共 ${library.items.length} 条 · 内置 ${library.counts.builtin ?? 0} · 近 ${library.usage_days} 天有调用的 ${Object.keys(library.usage).length} 条`}
          >
            <div className="mb-2.5 flex flex-wrap items-start justify-between gap-2">
              <p className="min-w-0 flex-1 text-[12px] leading-relaxed text-muted">
                已装好的做法。模型遇到对应任务会自己加载正文；也可以直接点名，例如「用 code-review 审一下这次改动」。
              </p>
              <button
                type="button"
                onClick={() => setInstalling((value) => !value)}
                className="shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
              >
                {installing ? "收起" : "安装技能"}
              </button>
            </div>

            {installing && (
              <div className="mb-3 flex flex-col gap-2 rounded-lg border border-line bg-surface-2/40 p-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    value={installSource}
                    onChange={(event) => setInstallSource(event.target.value)}
                    placeholder="地址（.md / .zip）或本机目录，例如 E:////skills////code-review"
                    aria-label="技能来源"
                    className="min-w-[16rem] flex-1 rounded-md border border-line bg-surface px-2 py-1.5 font-mono text-[12px] text-text placeholder:text-faint focus:outline-none"
                  />
                  <select
                    value={installScope}
                    onChange={(event) => setInstallScope(event.target.value as "user" | "project")}
                    aria-label="安装范围"
                    className="rounded-md border border-line bg-surface px-2 py-1.5 text-[12px] text-text focus:outline-none"
                  >
                    <option value="user">装到「我的」</option>
                    <option value="project">装到当前项目</option>
                  </select>
                  <label className="flex items-center gap-1 text-[12px] text-muted">
                    <input
                      type="checkbox"
                      checked={installOverwrite}
                      onChange={(event) => setInstallOverwrite(event.target.checked)}
                    />
                    覆盖同名
                  </label>
                  <button
                    type="button"
                    disabled={installBusy || installSource.trim() === ""}
                    onClick={() => void runInstall()}
                    className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
                  >
                    {installBusy ? "安装中…" : "安装"}
                  </button>
                </div>
                <p className="text-[11px] leading-relaxed text-faint">
                  只支持 .md 单文件、含 SKILL.md 的 .zip 或本机目录；会先校验格式与体积再落盘，不执行里面的任何脚本。
                </p>
                {installNote && (
                  <p className={`text-[12px] ${installNote.danger ? "text-danger" : "text-accent"}`}>{installNote.text}</p>
                )}
              </div>
            )}

            {library.items.length === 0 ? (
              <p className="text-[12px] text-faint">没有找到技能目录（内置技能随产品发布，缺失说明安装不完整）。</p>
            ) : (
              <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {library.items.map((item) => {
                  const used = library.usage[item.name];
                  const removable = item.source === "user" || item.source === "project";
                  return (
                    <div key={item.name} className="min-w-0 rounded-lg border border-line bg-surface px-3 py-2">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate text-[13px] font-medium">{item.display_name || item.name}</span>
                        <SourceBadge source={item.source} />
                        {item.paths.length > 0 && (
                          <span className="shrink-0 rounded border border-line px-1 py-0.5 font-mono text-[11px] text-faint">
                            按路径激活
                          </span>
                        )}
                        {removable && (
                          <button
                            type="button"
                            disabled={removing === item.name}
                            onClick={() => void runUninstall(item)}
                            title="从磁盘删掉这份技能"
                            className="ml-auto shrink-0 text-[11px] text-faint transition-colors hover:text-danger disabled:opacity-50"
                          >
                            {removing === item.name ? "删除中…" : "删除"}
                          </button>
                        )}
                      </div>
                      <p className="mt-1 text-[12px] leading-relaxed text-muted">{item.description}</p>
                      {item.when_to_use && (
                        <p className="mt-1 text-[11px] leading-relaxed text-faint">何时用：{item.when_to_use}</p>
                      )}
                      <p className="mt-1 font-mono text-[11px] text-faint">
                        {used
                          ? `近 ${library.usage_days} 天：${used.calls} 次 · ${used.tasks} 个任务 · ${used.tokens} tokens`
                          : "近 7 天未调用"}
                        {item.installed_from ? ` · 来自 ${shortSource(item.installed_from)}` : ""}
                      </p>
                    </div>
                  );
                })}
              </div>
            )}
            {library.errors.length > 0 && (
              <p className="mt-2 text-[12px] text-danger">有 {library.errors.length} 个技能文件读取失败。</p>
            )}
          </Panel>
        )}

        <SkillMarketPanel onInstalled={() => void refreshLibrary()} />

        {proposals.length > 0 && (
          <Panel title="待采用的提案" hint={`${proposals.length} 条模型提的建议`}>
            <p className="mb-2 text-[12px] leading-relaxed text-muted">
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
                        <span className="truncate text-[13px] font-medium">{proposal.name}</span>
                        <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${STATE_TONE.proposed}`}>
                          {kind === "improvement" ? "改进" : "新技能"}
                        </span>
                      </div>
                      {target && (
                        <p className="mt-0.5 font-mono text-[11px] text-faint">目标：{target}</p>
                      )}
                      <p className="mt-1 text-[12px] leading-relaxed text-muted">
                        {metaText(proposal, "description") || preview(proposal.instructions) || "（没有说明）"}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <button
                        type="button"
                        disabled={busy === proposal.id}
                        onClick={() => void adopt(proposal)}
                        className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
                      >
                        采用
                      </button>
                      <button
                        type="button"
                        disabled={busy === proposal.id}
                        onClick={() => void discardProposal(proposal)}
                        className="rounded-md border border-line-strong bg-surface px-2 py-1 text-[12px] text-muted transition-colors hover:text-danger disabled:opacity-50"
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

        {/* 和上面的「技能库」是两套东西，标题必须说清，不然"上面 12 条、这里 0 条"
            看起来像数据不同步。 */}
        <Panel
          title="你确认过的做法"
          hint={skills === null ? "读取中…" : `${skillSummary(confirmed.map((s) => s.status))} · 会作为工作偏好进入每轮提示`}
        >
          {skills === null && <div className="space-y-2">{[0, 1].map((row) => <div key={row} className="h-10 animate-pulse rounded-md bg-surface-2" />)}</div>}

          {skills !== null && confirmed.length === 0 && (
            <p className="py-4 text-center text-[13px] leading-relaxed text-faint">
              这里还没有条目——它和上面的技能库不是一回事：
              <br />
              技能库是「装好的能力」（随包 / 你装的），这里是「你点头认可的做法」，会写进每一轮的系统提示。
              <br />
              下面候选记忆被接受后会先落成草案，草案不启用就不影响任何任务。
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
                      <span className="truncate text-[13px] font-medium">{skill.name}</span>
                      <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${STATE_TONE[state]}`}>
                        {SKILL_STATE_LABEL[state]}
                      </span>
                      <span className="shrink-0 font-mono text-[11px] text-faint">v{skill.version}</span>
                    </div>
                    <p className="mt-1 text-[12px] leading-relaxed text-muted">{preview(skill.instructions) || "（没有说明）"}</p>
                    <p className="mt-1 font-mono text-[11px] text-faint">更新于 {formatDateTime(skill.updated_at)}</p>
                  </div>
                  <button
                    type="button"
                    disabled={busy === skill.id}
                    onClick={() => void toggle(skill)}
                    className={`shrink-0 rounded-md border px-2 py-1 text-[12px] transition-colors disabled:opacity-50 ${
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
                    className="shrink-0 rounded-md border border-transparent px-2 py-1 text-[12px] text-faint transition-colors hover:border-danger/30 hover:text-danger disabled:opacity-50"
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
                  <p className="text-[13px] leading-relaxed">{candidate.text}</p>
                  <p className="mt-1 font-mono text-[11px] text-faint">
                    {candidate.source || "未标来源"} · {formatDateTime(candidate.created_at)}
                  </p>
                </div>
                <div className="flex shrink-0 gap-1">
                  <button
                    type="button"
                    disabled={busy === candidate.id}
                    onClick={() => void decide(candidate, true)}
                    className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
                  >
                    接受
                  </button>
                  <button
                    type="button"
                    disabled={busy === candidate.id}
                    onClick={() => void decide(candidate, false)}
                    className="rounded-md border border-line-strong bg-surface px-2 py-1 text-[12px] text-muted transition-colors hover:text-danger disabled:opacity-50"
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

/** 技能来源徽章：内置 / 我的 / 项目 / 插件。用户要能一眼分清"谁写的"。 */
function SourceBadge({ source }: { source: string }) {
  const label =
    source === "builtin" ? "内置" : source === "user" ? "我的" : source.startsWith("plugin:") ? "插件" : "项目";
  const tone =
    source === "builtin"
      ? "border-brand/30 bg-brand/10 text-brand"
      : source === "user"
        ? "border-accent/30 bg-accent/10 text-accent"
        : "border-line bg-surface-2 text-muted";
  return (
    <span className={`shrink-0 rounded border px-1 py-0.5 text-[11px] ${tone}`}>{label}</span>
  );
}

/** 来源太长就把中间压掉：卡片上只需要认出"从哪来的"，不需要读完整 URL。 */
function shortSource(source: string): string {
  if (source.length <= 32) return source;
  return `${source.slice(0, 14)}…${source.slice(-12)}`;
}
