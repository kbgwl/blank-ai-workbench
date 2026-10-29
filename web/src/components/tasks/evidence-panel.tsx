"use client";

import { useEffect, useState } from "react";
import { Notice } from "@/components/projects/ui";
import { formatDuration, formatNumber, orNone } from "./format";
import { StatusBadge } from "./bits";
import {
  errorMessage,
  fetchTaskSnapshots,
  restoreTaskSnapshot,
  sandboxBoundaryLabel,
  sandboxBoundaryNeedsWarn,
  type Boundaries,
  type Evidence,
  type GuardianHistory,
  type TaskSnapshot,
  type TestInfo,
  type UsageInfo,
} from "./api";
import { verdictOf } from "./verdict";
import {
  boundariesNeedWarn,
  delegatedSpendLabel,
  enforcedFlagLabel,
  guardianDecisionLabel,
  guardianHeadline,
  guardianWindowLabel,
  mcpBoundaryLabel,
  missingPathLabel,
  networkGuardLabel,
  permissionEntryLabel,
  permissionProfileTypeLabel,
  sandboxModesLabel,
  sandboxTierLabel,
  shellGrantLabel,
  toolCallBoundaryLabel,
  usageBranchLabel,
  usageCacheLabel,
  usageParallelGroupLabel,
  usageTrailLabel,
  usageTurnLabel,
  usageTurnsLabel,
} from "./status";

/** 轮/分支条目的可见上限：并行派十几个子代理时不把面板撑爆，但剩余条数要说出来。 */
const ROLLUP_VISIBLE = 8;

function RollupList({ heading, entries, emptyText }: { heading: string; entries: string[]; emptyText: string }) {
  const shown = entries.slice(0, ROLLUP_VISIBLE);
  const hidden = entries.length - shown.length;
  return (
    <div className="flex flex-col gap-1">
      <h5 className="font-mono text-[10px] tracking-wide text-faint uppercase">{heading}</h5>
      {entries.length === 0 ? (
        <p className="text-[11px] text-faint">{emptyText}</p>
      ) : (
        <>
          <ul className="flex flex-col gap-0.5">
            {shown.map((entry, index) => (
              <li key={`${heading}-${index}`} className="truncate font-mono text-[11px] text-text/80" title={entry}>
                {entry}
              </li>
            ))}
          </ul>
          {hidden > 0 && <p className="text-[11px] text-faint">另有 {hidden} 条未展开</p>}
        </>
      )}
    </div>
  );
}

function Row({ label, value, tone, title }: { label: string; value: string; tone?: "accent" | "danger" | "warn"; title?: string }) {
  const valueClass =
    tone === "accent" ? "text-accent" : tone === "danger" ? "text-danger" : tone === "warn" ? "text-warn" : "text-text/90";
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12px]">
      <span className="shrink-0 text-faint" title={title}>
        {label}
      </span>
      <span className={`min-w-0 truncate text-right font-mono ${valueClass}`} title={value}>
        {value}
      </span>
    </div>
  );
}

/** 回合归属：后端没说（null）和说了「没有」（[]）必须长得不一样，所以整块自己判。 */
function UsageAttribution({ usage }: { usage: UsageInfo }) {
  const { turns, branches, unattributedCalls, parallelGroups } = usage;
  const delegated = delegatedSpendLabel(usage.delegatedTokens, usage.delegatedCalls);
  return (
    <>
      <Row
        label="轮次账本"
        value={orNone(usageTurnsLabel(turns, unattributedCalls))}
        title="按轮（run）汇总的账，对标 Codex 的 turn_attribution。「归属未记」是回合归属上线前落下的老账：只报条数，不并进任何一轮"
      />
      <RollupList
        heading={turns === null ? "按轮记账" : `按轮记账（${turns.length} 轮）`}
        entries={(turns ?? []).map((turn) => usageTurnLabel(turn))}
        emptyText={
          turns === null
            ? "暂无：后端没给 turns 这个键（服务版本早于回合归属）"
            : "一轮都归不上账——这些钱只以「归属未记」的条数出现，不会被并成第 0 轮"
        }
      />
      <RollupList
        heading={branches === null ? "按分支记账" : `按分支记账（${branches.length} 条线程）`}
        entries={(branches ?? []).map((branch) => usageBranchLabel(branch))}
        emptyText={branches === null ? "暂无：后端没给 branches 这个键" : "没有任何一条线程记到账上"}
      />
      <Row
        label="派给子代理"
        value={orNone(delegated)}
        title="账本上 node=subagent 那些行的合计。#45 起这部分也算进主线程这一轮的 token 预算——派出去不是免费的"
      />
      <RollupList
        heading={parallelGroups === null ? "按并行批次记账" : `按并行批次记账（${parallelGroups.length} 批）`}
        entries={(parallelGroups ?? []).map((batch) => usageParallelGroupLabel(batch))}
        emptyText={
          parallelGroups === null
            ? "暂无：后端没给 parallel_groups 这个键（服务版本早于批次归账）"
            : "没有一次并行编排记到账上；单发的子代理不属于任何一批，不会出现在这里"
        }
      />
    </>
  );
}

function TestDetails({ test }: { test: TestInfo }) {
  const verdict = verdictOf(test.passed, test.status);
  return (
    <>
      <Row label="命令" value={orNone(test.command)} />
      <Row
        label="退出码"
        value={test.exitCode === null ? "暂无" : String(test.exitCode)}
        tone={test.exitCode === null ? undefined : test.exitCode === 0 ? "accent" : "danger"}
      />
      <Row label="结论" value={verdict.label} tone={verdict.tone} />
      {test.skippedReason && <p className="font-mono text-[10px] text-faint">原因：{test.skippedReason}</p>}
      {test.output ? (
        <details className="group rounded-md border border-line bg-surface-2">
          <summary className="cursor-pointer list-none px-2.5 py-1.5 font-mono text-[10px] text-faint hover:text-muted">
            输出片段（{test.output.split("\n").length} 行）
          </summary>
          <pre className="max-h-40 overflow-auto border-t border-line px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words text-muted">
            {test.output}
          </pre>
        </details>
      ) : (
        <p className="font-mono text-[10px] text-faint">无输出内容</p>
      )}
    </>
  );
}

function GovernanceDetails({ evidence }: { evidence: Evidence }) {
  const g = evidence.governance;
  const sb = g.sandbox;
  const boundary = sandboxBoundaryLabel(sb);
  const risky = sandboxBoundaryNeedsWarn(sb);
  // 中途改过策略必须显眼：一张"当初的边界"覆盖全过程，正是审计最容易骗人的地方。
  const shifted = g.changes.length > 0;
  return (
    <>
      <Row label="审批模式" value={g.modes.length > 0 ? g.modes.join(" → ") : "暂无"} tone={shifted ? "warn" : undefined} />
      <Row
        label="模型（当时配置）"
        value={g.models.length > 0 ? g.models.join(" → ") : "暂无"}
        title="快照记的是这次调用请求的模型名；用量那行是供应商实际服务返回的模型名。两者不一样时说明供应商做了替换。"
      />
      <Row label="执行边界" value={boundary} tone={risky ? "warn" : undefined} />
      {sb?.limits && <Row label="资源上限" value={sb.limits} />}
      {sb?.denied && <p className="text-[11px] leading-relaxed text-warn">命令类工具当时会被直接拒掉：{sb.denied}</p>}
      {g.features.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-0.5">
          {g.features.map((feature) => (
            <span
              key={feature.name}
              className={`rounded border bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] ${
                feature.source === "legacy" ? "border-warn/30 text-warn" : "border-line text-muted"
              }`}
              title={`来源 ${feature.source}`}
            >
              {feature.name}
              <span className="ml-1 text-faint">{feature.source}</span>
            </span>
          ))}
        </div>
      )}
      {shifted && (
        <details className="group rounded-md border border-line bg-surface-2">
          <summary className="cursor-pointer list-none px-2.5 py-1.5 font-mono text-[10px] text-warn">
            执行中策略变过 {g.changes.length} 次
          </summary>
          <ul className="flex flex-col gap-1 border-t border-line px-2.5 py-2">
            {g.changes.map((item, index) => (
              <li key={`${item.step}-${index}`} className="font-mono text-[11px] leading-relaxed text-muted">
                第 {item.step ?? "?"} 步（{item.node || "?"}）：模式 {item.mode || "未知"}
                {item.model ? ` · 模型 ${item.model}` : ""}
                {item.tier ? ` · 工具表 ${item.tier}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="font-mono text-[10px] text-faint">
        共 {g.snapshots} 条快照，只在策略真的变化时才新增。
      </p>
    </>
  );
}

/** 环境快照（对标 Codex world_state）：这一步当时在哪个目录、什么 shell、哪一版指令。
 *
 * 指令只显示指纹，不显示正文——正文从不入账，这是有意的：几万字进事件流会同时污染
 * 时间线、证据包和全文索引。要核对是哪一版，拿这个指纹去和工作区文件的哈希对得上即可。 */
function WorldDetails({ evidence }: { evidence: Evidence }) {
  const world = evidence.world;
  const current = world.current;
  if (!current) {
    return <p className="font-mono text-[10px] text-faint">这类快照上线之前的任务，环境没有记账（不是「环境为空」）。</p>;
  }
  const shifted = world.changes.length > 0;
  return (
    <>
      <Row label="工作目录" value={current.cwd || "暂无"} title="主机绝对路径按设置掩码；打开 expose_workspace_paths 才会写全" />
      <Row
        label="系统 / shell"
        value={[current.os, current.shell].filter(Boolean).join(" · ") || "暂无"}
        title={current.status === "captured" ? "命令当时就在这个环境里被规划" : "这个任务跑的时候还没装配 shell 环境层"}
      />
      <Row
        label="解释器"
        value={
          [current.python, current.pathEntries === null ? "" : `PATH ${current.pathEntries} 条`]
            .filter(Boolean)
            .join(" · ") || "暂无"
        }
      />
      <Row
        label="上下文窗口"
        value={
          current.contextWindow === null
            ? "暂无"
            : `${current.contextWindow}${current.triggerAt === null ? "" : ` · ${current.triggerAt} 触发压缩`}`
        }
      />
      <Row
        label="指令版本"
        value={current.instructionsDigest ? current.instructionsDigest.slice(0, 12) : "暂无"}
        title="整套分层指令（AGENTS.md 等）的内容指纹：正文从不入账，只留指纹，改一个字节就对不上"
      />
      {current.instructions.length > 0 && (
        <details className="group rounded-md border border-line bg-surface-2">
          <summary className="cursor-pointer list-none px-2.5 py-1.5 font-mono text-[10px] text-muted">
            {current.instructions.length} 层指令（按指纹指认）
          </summary>
          <ul className="flex flex-col gap-1 border-t border-line px-2.5 py-2">
            {current.instructions.map((layer, index) => (
              <li key={`${layer.sha}-${index}`} className="font-mono text-[11px] leading-relaxed text-muted">
                {layer.source} · {layer.path || "内联"} · {layer.chars ?? "?"} 字 ·{" "}
                <span title={layer.sha}>{layer.sha.slice(0, 8)}</span>
                {layer.truncated ? " · 已截断" : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
      {shifted && (
        <p className="text-[11px] leading-relaxed text-warn">
          执行中环境换过 {world.changes.length} 次（第 {world.changes.map((item) => item.node).join("、")} 处留下新快照）
        </p>
      )}
      <p className="font-mono text-[10px] text-faint">
        共 {world.snapshots} 条环境快照，每条都是当时的全量，最后一条即终态。
      </p>
    </>
  );
}

/**
 * 边界与授权（#77 的 permission_profile + M4 的 boundary_report）。
 *
 * 这一块的存在理由只有一个：面板上「沙箱档位 workspace」这句话太容易被读成「系统会拒」。
 * 所以每行都要带上"谁在保证"——策略闸门在执行前判定，系统拒绝是另一回事。
 */
function BoundaryDetails({ boundaries }: { boundaries: Boundaries }) {
  const risky = boundariesNeedWarn(boundaries);
  const profile = boundaries.profile;
  const networkLine = networkGuardLabel(profile.network);
  const missingLine = missingPathLabel(profile.missingPathBehavior);
  const modesLine = sandboxModesLabel(boundaries.sandboxModes);
  const mcpLine = mcpBoundaryLabel(boundaries);
  const shellLine = shellGrantLabel(profile.shellEnabled);
  return (
    <>
      <Row
        label="沙箱档位"
        value={orNone(sandboxTierLabel(boundaries))}
        tone={risky ? "warn" : undefined}
        title="要求什么档、实际落在哪档、有没有被部署策略降级，以及这一档由谁强制"
      />
      {boundaries.notice && (
        <p className={`text-[11px] leading-relaxed ${risky ? "text-warn" : "text-muted"}`}>{boundaries.notice}</p>
      )}
      <Row
        label="文件边界"
        value={enforcedFlagLabel(boundaries.filesystemEnforced)}
        tone={boundaries.filesystemEnforced === false ? "warn" : undefined}
        title="后端探测出来的事实：这一层是不是由系统（Job Object / 受限令牌）真的拦住"
      />
      <Row
        label="网络边界"
        value={enforcedFlagLabel(boundaries.networkEnforced)}
        tone={boundaries.networkEnforced === false ? "warn" : undefined}
      />
      {boundaries.backend && <Row label="隔离后端" value={boundaries.backend} />}
      {modesLine && <Row label="档位变化" value={modesLine} title="这次任务里真实出现过的档位序列" />}
      <Row
        label="权限快照"
        value={permissionProfileTypeLabel(profile)}
        tone={profile.type === "unrestricted" ? "warn" : undefined}
        title="这份授权清单的来历：受管、不受限，还是当时压根没记"
      />
      {profile.note && <p className="text-[11px] leading-relaxed text-muted">{profile.note}</p>}
      <RollupList
        heading={`逐条授权（${profile.entries.length} 条）`}
        entries={profile.entries.map(permissionEntryLabel)}
        emptyText={
          profile.type === ""
            ? "暂无：后端没给权限快照这一块"
            : "没有逐条授权记录——只有档位与审批模式在管，别把它读成「全禁止」"
        }
      />
      {missingLine && <p className="font-mono text-[10px] text-faint">不存在的目标：{missingLine}</p>}
      {networkLine && <Row label="网络闸门" value={networkLine} />}
      {shellLine && <Row label="命令执行" value={shellLine} />}
      <Row
        label="调用归账"
        value={toolCallBoundaryLabel(boundaries)}
        tone={(boundaries.toolCalls.unattributed ?? 0) > 0 ? "warn" : undefined}
        title="每一笔工具调用落在哪一层边界里。未归属=当时没记边界，不猜成进程内"
      />
      {mcpLine && (
        <Row
          label="MCP"
          value={mcpLine}
          tone={boundaries.mcp.unboundedServers.length > 0 ? "warn" : undefined}
          title="stdio 服务端尽量挂进 Job；没挂进去的按宿主权限运行。http 服务端的进程在远端"
        />
      )}
    </>
  );
}

/** 审阅轨迹（#79 的 guardian_history）：压缩把模型的历史折掉了，审阅侧还看得见全部。 */
function GuardianTrailDetails({ trail }: { trail: GuardianHistory }) {
  return (
    <>
      <Row
        label="轨迹汇总"
        value={orNone(guardianHeadline(trail))}
        title="判定条数、代批次数、压缩窗数，以及其中几窗真带上了判定"
      />
      <RollupList
        heading={`独立审查判定（${trail.decisions.length} 条）`}
        entries={trail.decisions.map(guardianDecisionLabel)}
        emptyText="审查员开着，但这次没有任何一条操作经过它判定"
      />
      <RollupList
        heading={`压缩窗（${trail.windows.length} 面）`}
        entries={trail.windows.map(guardianWindowLabel)}
        emptyText="这次任务没有发生压缩"
      />
      {trail.autoApprovals.length > 0 && (
        <p className="text-[11px] leading-relaxed text-muted">
          审查代批 {trail.autoApprovals.length} 次（只有可撤销的操作能被代批，审批指纹不上页面）
        </p>
      )}
      {trail.windowsUnrecorded === null && (
        <p className="font-mono text-[10px] text-faint">这台后端还没有「压缩未记轨迹」这一格（早于 #79），不猜条数。</p>
      )}
    </>
  );
}

export function EvidencePanel({ taskId, evidence, error }: { taskId: string; evidence: Evidence | null; error: string | null }) {
  const [snapshots, setSnapshots] = useState<TaskSnapshot[]>([]);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  useEffect(() => {
    if (!taskId) return;
    void fetchTaskSnapshots(taskId).then(setSnapshots).catch(() => setSnapshots([]));
  }, [taskId, evidence?.status]);

  async function restore(snapshot: TaskSnapshot) {
    if (snapshot.restorable === false) return;
    if (!window.confirm("恢复此快照会覆盖工作区中的文件，确定继续吗？")) return;
    setRestoring(snapshot.id);
    setSnapshotError(null);
    try {
      const result = await restoreTaskSnapshot(taskId, snapshot.id);
      const skipped = result.skipped.length;
      const head = `已恢复 ${result.restored.length} 个文件`;
      setSnapshotError(skipped > 0 ? `${head}，跳过 ${skipped} 个：${result.skipped_reason || "原因未记录"}` : head);
      void fetchTaskSnapshots(taskId).then(setSnapshots).catch(() => undefined);
    } catch (restoreError) {
      setSnapshotError(errorMessage(restoreError));
    } finally {
      setRestoring(null);
    }
  }
  if (error) {
    return (
      <div className="flex flex-col gap-2">
        <Notice>{error}</Notice>
        <p className="text-[11px] leading-relaxed text-faint">{"交付证明包需要后端提供 GET /tasks/{id}/evidence。"}</p>
      </div>
    );
  }
  if (!evidence) {
    return <p className="font-mono text-[11px] text-faint">读取证明包…</p>;
  }

  const covered = evidence.acceptance.filter((item) => item.covered === true).length;
  const test = evidence.test;

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-muted">任务结论</span>
        <StatusBadge status={evidence.status} />
      </div>

      <section className="flex flex-col gap-1.5">
        <h4 className="flex items-center justify-between font-mono text-[10px] tracking-wide text-faint uppercase">
          <span>验收条件</span>
          {evidence.acceptance.length > 0 && (
            <span className="text-muted normal-case">
              已通过 {covered} / {evidence.acceptance.length}
            </span>
          )}
        </h4>
        {evidence.acceptance.length === 0 ? (
          <p className="text-[11px] text-faint">暂无验收条件</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {evidence.acceptance.map((item, index) => (
              <li key={`${item.criterion}-${index}`} className="flex items-start gap-2">
                <span
                  className={`mt-0.5 grid size-4 shrink-0 place-items-center rounded-full font-mono text-[9px] ${
                    item.covered === true
                      ? "bg-accent/20 text-accent"
                      : item.covered === false
                        ? "bg-danger/15 text-danger"
                        : "bg-surface-2 text-faint"
                  }`}
                >
                  {item.covered === true ? "✓" : item.covered === false ? "×" : "?"}
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className={`block text-[12px] leading-relaxed ${
                      item.covered === true ? "text-muted line-through decoration-line-strong" : "text-text/90"
                    }`}
                  >
                    {item.criterion}
                  </span>
                  {item.note && <span className="mt-0.5 block font-mono text-[10px] text-faint">{item.note}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-1.5 border-t border-line pt-3">
        <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">测试命令与结果</h4>
        {!test ? (
          <p className="text-[11px] text-faint">暂无（后端未返回 test/validation）</p>
        ) : (
          <TestDetails test={test} />
        )}
      </section>

      <section className="flex flex-col gap-1.5 border-t border-line pt-3">
        <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">Token 用量</h4>
        <Row label="输入" value={formatNumber(evidence.usage.promptTokens)} />
        <Row label="输出" value={formatNumber(evidence.usage.completionTokens)} />
        <Row label="合计" value={formatNumber(evidence.usage.totalTokens)} tone="accent" />
        <Row
          label="缓存命中"
          value={orNone(usageCacheLabel(evidence.usage.cachedTokens, evidence.usage.promptTokens))}
          title="输入里按缓存价结算的部分。没有这一行，「打开了提示词缓存」就只是我们自述"
        />
        <Row
          label="推理"
          value={formatNumber(evidence.usage.reasoningTokens)}
          title="思维链单独计的 token；为 0 表示这次模型没有推理开销，不是「没记到」"
        />
        <Row
          label="三级口径"
          value={orNone(
            usageTrailLabel(
              evidence.usage.levels?.call?.totalTokens ?? null,
              evidence.usage.levels?.turn?.totalTokens ?? null,
              evidence.usage.levels?.task?.totalTokens ?? null
            )
          )}
          title="单次调用 / 本回合 / 整个任务三个口径同时记账（对标 Codex token_usage_record）。老任务没有这类事件"
        />
        <Row label="耗时" value={evidence.usage.latencyMs === null ? "暂无" : orNone(formatDuration(evidence.usage.latencyMs))} />
        <Row label="调用次数" value={formatNumber(evidence.usage.calls)} />
        <Row label="模型" value={evidence.usage.models.length > 0 ? evidence.usage.models.join("、") : "暂无"} title="供应商这一轮实际服务的是哪个模型" />
        <Row
          label="供应商请求 id"
          value={orNone(evidence.usage.levels?.call?.responseId ?? "")}
          title="最近一次调用在供应商那一侧的 request id：限流、计费、内容审核这类争议要拿它去对那一笔。本地 Ollama 没有这种 id，字段上线前的老任务也没有——都显示「暂无」，不是我们补的"
        />
        <UsageAttribution usage={evidence.usage} />
      </section>

      {evidence.governance.snapshots > 0 && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">执行边界与策略</h4>
          <GovernanceDetails evidence={evidence} />
        </section>
      )}

      {evidence.world.snapshots > 0 && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">当时的环境</h4>
          <WorldDetails evidence={evidence} />
        </section>
      )}

      {evidence.boundaries.recorded && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">边界与授权</h4>
          <BoundaryDetails boundaries={evidence.boundaries} />
        </section>
      )}

      {evidence.guardian_history.recorded && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">审阅轨迹</h4>
          <GuardianTrailDetails trail={evidence.guardian_history} />
        </section>
      )}

      {snapshots.length > 0 && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-faint uppercase">回滚点</h4>
          {snapshots.map((snapshot) => {
            const restorable = snapshot.restorable !== false;
            return (
              <div key={snapshot.id} className="flex flex-col gap-1 rounded-md border border-line bg-surface-2 px-2.5 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-[11px] text-muted" title={snapshot.reason}>{snapshot.reason}</span>
                  <button type="button" disabled={restoring !== null || !restorable} title={restorable ? undefined : snapshot.skipped_reason} onClick={() => void restore(snapshot)} className="shrink-0 rounded border border-line px-2 py-1 text-[10px] text-muted hover:bg-surface-2 hover:text-text disabled:opacity-50">
                    {restoring === snapshot.id ? "恢复中" : restorable ? "恢复" : "仅清单"}
                  </button>
                </div>
                {!restorable && (
                  <p className="text-[10px] leading-relaxed text-faint">
                    {snapshot.skipped_reason || "这份快照只记录了文件清单，没有复制文件内容，无法恢复。"}
                  </p>
                )}
              </div>
            );
          })}
          {snapshotError && <p className="text-[11px] text-muted">{snapshotError}</p>}
        </section>
      )}

      {evidence.unverified.length > 0 && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h4 className="font-mono text-[10px] tracking-wide text-warn uppercase">仍需关注</h4>
          <ul className="flex flex-col gap-1">
            {evidence.unverified.map((item, index) => (
              <li key={index} className="text-[11px] leading-relaxed text-warn">
                · {item}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
