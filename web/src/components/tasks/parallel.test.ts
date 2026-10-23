import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentEvent } from "./api.ts";
import { normalizeEvidence } from "./api.ts";
import { delegatedSpendLabel, usageParallelGroupLabel } from "./status.ts";
import { buildBlocks } from "./timeline.ts";

/**
 * #45 的前端一侧：一次并行编排花了多少，得有人直接答得出。
 *
 * 后端原先只在散着的 `subagent.completed` 里各说各的用量，「这批合计多少」
 * 要读的人自己按 `agent_path` 里那个 `par-{group}-{i}` 再加一遍。现在批次号
 * 成了一等列、合计进了事件与工具结果，前端只许搬、不许推：
 * 1. `parallel_groups` 逐键搬到前端，投影只搬声明过的键（后端多塞的字段不进结果）；
 * 2. 「后端没说」（缺键 → null，面板写暂无）与「后端说了没有」（空数组 → []）
 *    必须两种读法；把缺键兜底成 [] 或 0 就是替后端说话；
 * 3. 单项里缺的数读成 null，界面写破折号；但真·零必须显示 0，否则破折号成了遮羞布；
 * 4. 委派那一行不许反推：任务累计没给拆分时，不能拿「总 − 分支合计」凑一个数，
 *    那种减法会把摘要、审查员的开销也算成派出去的；
 * 5. 时间线：预算这一条要说清「耗尽的是派出去的钱」，批次收口那一条要念出合计；
 *    后端没给委派数时标题不许出现「含派出的子代理」——那是没证据的指控。
 */

const GROUP_KEYS = [
  "group",
  "runId",
  "threads",
  "agentPaths",
  "promptTokens",
  "completionTokens",
  "totalTokens",
  "cachedTokens",
  "calls",
];

const groupPayload = (over: Record<string, unknown> = {}) => ({
  group: "8f3a9c61b2",
  run_id: "task:9f2c1d7e-3b4a-4c5d-9e8f-0a1b2c3d4e5f:attempt:1",
  threads: 3,
  agent_paths: ["root/par-8f3a9c61b2-1", "root/par-8f3a9c61b2-2"],
  calls: 6,
  prompt_tokens: 3_000,
  completion_tokens: 300,
  total_tokens: 3_300,
  cached_tokens: 1_000,
  ...over,
});

const usageOf = (usage: Record<string, unknown>) => normalizeEvidence({ usage }).usage;

/** 先走一遍真投影，再交给标签函数：标签测的是前端类型，不是后端的蛇形键。 */
const groupOf = (over: Record<string, unknown> = {}) => {
  const item = usageOf({ parallel_groups: [groupPayload(over)] }).parallelGroups?.[0];
  assert.ok(item, "parallel_groups 里这一条必须投影出来");
  return item;
};

const event = (node: string, type: string, payload: Record<string, unknown>): AgentEvent => ({
  seq: 1,
  node,
  type,
  payload,
  createdAt: "2026-10-22T00:00:00+00:00",
});

const system = (item: AgentEvent) => {
  const block = buildBlocks([item]).find((entry) => entry.kind === "system");
  assert.ok(block, "这一条事件必须在时间线里留一行，否则用户看不到批次的钱去了哪");
  if (block.kind !== "system") throw new Error("unexpected block");
  return block;
};

test("批次表按后端键名搬到前端，九个键逐条对上", () => {
  const usage = usageOf({
    total_tokens: 5_000,
    parallel_groups: [groupPayload()],
    batched_calls: 6,
    unbatched_calls: 4,
    delegated_tokens: 3_300,
    delegated_calls: 6,
  });
  const batch = usage.parallelGroups?.[0];
  assert.ok(batch, "有 parallel_groups 就必须投影出一批");
  assert.equal(batch.group, "8f3a9c61b2");
  assert.equal(batch.runId, "task:9f2c1d7e-3b4a-4c5d-9e8f-0a1b2c3d4e5f:attempt:1");
  assert.equal(batch.threads, 3);
  assert.deepEqual(batch.agentPaths, ["root/par-8f3a9c61b2-1", "root/par-8f3a9c61b2-2"]);
  assert.equal(batch.calls, 6);
  assert.equal(batch.promptTokens, 3_000);
  assert.equal(batch.completionTokens, 300);
  assert.equal(batch.totalTokens, 3_300);
  assert.equal(batch.cachedTokens, 1_000);
  assert.equal(usage.batchedCalls, 6);
  assert.equal(usage.unbatchedCalls, 4);
  assert.equal(usage.delegatedTokens, 3_300);
  assert.equal(usage.delegatedCalls, 6);
});

test("投影只搬声明过的键：后端多塞的字段（含正文）不跟着进来", () => {
  const usage = usageOf({
    parallel_groups: [groupPayload({ summary: "子代理的原始结论 MARKER-4501", children: [{ usage: 1 }] })],
  });
  assert.deepEqual(Object.keys(usage.parallelGroups?.[0] ?? {}).sort(), [...GROUP_KEYS].sort());
  assert.doesNotMatch(JSON.stringify(usage.parallelGroups), /MARKER-4501/);
});

test("缺键读成 null（后端没说），空数组读成 []（后端说了没有）", () => {
  const silent = usageOf({ total_tokens: 100 });
  assert.equal(silent.parallelGroups, null);
  assert.equal(silent.batchedCalls, null);
  assert.equal(silent.unbatchedCalls, null);
  assert.equal(silent.delegatedTokens, null);
  assert.equal(silent.delegatedCalls, null);
  // 「没说」在界面上是「暂无」。写成 0 会说「这个任务一个子代理都没派」，那是指控。
  assert.equal(delegatedSpendLabel(silent.delegatedTokens, silent.delegatedCalls), "");

  const spoken = usageOf({ parallel_groups: [], batched_calls: 0, unbatched_calls: 9, delegated_tokens: 0, delegated_calls: 0 });
  assert.deepEqual(spoken.parallelGroups, []);
  // 真·零必须显示 0：委派过但一分钱没花（子代理全在第一步失败）与「没说」是两件事。
  assert.equal(delegatedSpendLabel(spoken.delegatedTokens, spoken.delegatedCalls), "派给子代理 0 · 0 笔");
});

test("批次标签念得出合计、笔数、线程与出身，缺的数写破折号", () => {
  const label = usageParallelGroupLabel(groupOf());
  assert.match(label, /批次 8f3a9c61b2/);
  assert.match(label, /合计 3,300/);
  assert.match(label, /6 笔/);
  assert.match(label, /3 条线程/);
  // 主线程的 attempt 键念成「主线程#1」，砍前八位会让同一任务每次运行的父引用长成一个样。
  assert.match(label, /出自 主线程#1/);
  assert.doesNotMatch(label, /[A-Za-z]:[\\/]/);

  const partial = usageParallelGroupLabel(groupOf({ total_tokens: null, calls: null, threads: null, run_id: "" }));
  assert.match(partial, /合计 —/);
  assert.doesNotMatch(partial, /合计 0/);
  assert.doesNotMatch(partial, /条线程/, "后端没给线程数就不要说有几条");
  assert.doesNotMatch(partial, /出自/, "没有回合归属就不许编一个出身");
});

test("委派那一行只说后端给的那两个数，不拿总量减出来", () => {
  assert.equal(delegatedSpendLabel(3_300, null), "派给子代理 3,300");
  assert.equal(delegatedSpendLabel(null, 6), "6 笔");
  assert.equal(delegatedSpendLabel(3_300, 6), "派给子代理 3,300 · 6 笔");
  // 只有总量时可核的数一个都没有：这一行必须整个不写，而不是显示 0 或把总量算成委派。
  const usage = usageOf({ total_tokens: 9_000, branches: [] });
  assert.equal(delegatedSpendLabel(usage.delegatedTokens, usage.delegatedCalls), "");
  assert.notEqual(usage.totalTokens, null, "总量本身是给了的，缺的只是委派拆分");
});

test("时间线：预算被派出去的钱顶穿时，标题与明细都要点名", () => {
  const block = system(
    event("execute_action", "budget.exceeded", {
      steps: 6,
      tokens: 121_110,
      tokens_self: 110,
      tokens_delegated: 121_000,
      token_budget: 50_000,
      reason: "派给子代理的 token 用完",
    })
  );
  assert.equal(block.title, "预算已用完（含派出的子代理）");
  assert.match(block.detail, /token 121,110\/50,000/);
  assert.match(block.detail, /其中派出 121,000/);
  assert.match(block.detail, /派给子代理的 token 用完/);
  assert.equal(block.tone, "warn");

  // 步数用完的那一次：后端没写委派数，标题不许出现「含派出的子代理」。
  const stepsBlock = system(event("execute_action", "budget.exceeded", { steps: 30, reason: "步数用完" }));
  assert.doesNotMatch(stepsBlock.title, /含派出的子代理/);
  assert.doesNotMatch(stepsBlock.detail, /其中派出/);

  const warnBlock = system(
    event("execute_action", "budget.warning", {
      remaining: 2,
      tokens: 900_000,
      tokens_delegated: 800_000,
      token_budget: 1_000_000,
      reason: "",
    })
  );
  assert.equal(warnBlock.title, "预算即将用完（含派出的子代理）");
  assert.match(warnBlock.detail, /只剩 2 步/);
  assert.match(warnBlock.detail, /其中派出 800,000/);
});

test("时间线：批次收口那一条把合计念出来，缺 usage 就不编数", () => {
  const block = system(
    event("subagent", "subagents.completed", {
      group: "8f3a9c61b2",
      count: 3,
      succeeded: 3,
      merge: false,
      usage: { total_tokens: 3_300, calls: 6 },
    })
  );
  assert.equal(block.title, "并行批次收口");
  assert.match(block.detail, /批次 8f3a9c61b2/);
  assert.match(block.detail, /3\/3 完成/);
  assert.match(block.detail, /合计 3,300 token/);
  assert.match(block.detail, /6 笔/);
  assert.equal(block.tone, "accent");

  const noMoney = system(event("subagent", "subagents.completed", { group: "9a", count: 2, succeeded: 2 }));
  assert.doesNotMatch(noMoney.detail, /合计/, "后端没给合计，前端不许写一个 0 或自己加一遍");
  assert.doesNotMatch(noMoney.detail, /0 token/);

  const partial = system(
    event("subagent", "subagents.completed", { group: "9a", count: 3, succeeded: 1, usage: { total_tokens: 40 } })
  );
  assert.equal(partial.tone, "warn", "没全成功却不着色，用户会以为一批都跑成了");
  assert.match(partial.detail, /1\/3 完成/);
});
