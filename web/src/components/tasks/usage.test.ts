import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentEvent } from "./api.ts";
import { normalizeEvidence } from "./api.ts";
import { usageCacheLabel, usageTrailLabel } from "./status.ts";
import { buildBlocks } from "./timeline.ts";

/** 后端 evidence.py 的 usage 块真实形状（键名即跨语言契约）。 */
const usagePayload = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  providers: ["deepseek"],
  models: ["deepseek-chat"],
  trace_ids: ["t-1"],
  calls: 2,
  prompt_tokens: 1500,
  completion_tokens: 150,
  total_tokens: 1650,
  cached_tokens: 1250,
  cache_write_tokens: 0,
  reasoning_tokens: 40,
  cache_hit_ratio: 0.8333,
  records: 2,
  latency_ms: 4200,
  levels: {
    call: {
      prompt_tokens: 500,
      completion_tokens: 50,
      cached_tokens: 500,
      cache_write_tokens: 0,
      reasoning_tokens: 0,
      total_tokens: 550,
      calls: 1,
      model: "deepseek-chat",
      provider: "deepseek",
      latency_ms: 1800,
      status: "ok",
      response_id: "a4ec27d7-cdd7-4f33-9696-bd813dd75025",
    },
    turn: {
      prompt_tokens: 1500,
      completion_tokens: 150,
      cached_tokens: 1250,
      cache_write_tokens: 0,
      reasoning_tokens: 40,
      total_tokens: 1650,
      calls: 2,
    },
    task: {
      prompt_tokens: 3400,
      completion_tokens: 300,
      cached_tokens: 2900,
      cache_write_tokens: 0,
      reasoning_tokens: 40,
      total_tokens: 3700,
      calls: 5,
    },
  },
  ...over,
});

const evidenceOf = (usage: Record<string, unknown> | null) =>
  normalizeEvidence({ usage: usage ?? {} }).usage;

test("缓存、推理与三级口径从后端键名搬到前端", () => {
  const usage = evidenceOf(usagePayload());
  assert.equal(usage.cachedTokens, 1250);
  assert.equal(usage.reasoningTokens, 40);
  assert.equal(usage.cacheWriteTokens, 0);
  assert.equal(usage.cacheHitRatio, 0.8333);
  assert.equal(usage.levels?.call?.totalTokens, 550);
  assert.equal(usage.levels?.turn?.calls, 2);
  assert.equal(usage.levels?.task?.totalTokens, 3700);
  assert.equal(usage.levels?.task?.cachedTokens, 2900);
  // 供应商那一笔的 id 也在同一个 call 口径里：三级口径说"花了多少"，这一枚说"去哪家查这一笔"
  assert.equal(usage.levels?.call?.responseId, "a4ec27d7-cdd7-4f33-9696-bd813dd75025");
});

test("call 里没有 response_id 时是空串，不拿别的字段顶上", () => {
  // 本地 Ollama 与字段上线前的老任务都没有这枚 id。前端读成 model/provider 的名字
  // 会让人去供应商后台搜一个不存在的东西，那比显示「暂无」坏得多。
  const withoutId = usagePayload({
    levels: { call: { prompt_tokens: 10, completion_tokens: 1, model: "deepseek-chat", provider: "deepseek" } },
  });
  const usage = evidenceOf(withoutId);
  assert.equal(usage.levels?.call?.responseId, "");
  // 整级缺失时保持 null，不能被 normalize 成"这一级有记录、只是 id 空"
  assert.equal(usage.levels?.turn, null);
});

test("老任务缺字段就是 null，不当成 0", () => {
  // 三级记录上线前的证据包只有 prompt/completion。缺项显示成 0 会说「这一步没花钱」，
  // 而事实是「这一项当时没记」。
  const usage = evidenceOf({ prompt_tokens: 100, completion_tokens: 10, total_tokens: 110, calls: 1 });
  assert.equal(usage.cachedTokens, null);
  assert.equal(usage.reasoningTokens, null);
  assert.equal(usage.cacheHitRatio, null);
  assert.equal(usage.levels, null);
  assert.equal(usage.totalTokens, 110);
});

test("后端 levels 为 null 时前端保持 null", () => {
  const usage = evidenceOf(usagePayload({ levels: null }));
  assert.equal(usage.levels, null);
});

test("levels 里某一级缺 total_tokens 时按输入+输出补，但整级缺失不给 0", () => {
  const usage = evidenceOf(
    usagePayload({
      levels: {
        call: { prompt_tokens: 70, completion_tokens: 7, cached_tokens: 0, calls: 1 },
        turn: null,
        task: { total_tokens: 999, calls: 3 },
      },
    })
  );
  assert.equal(usage.levels?.call?.totalTokens, 77);
  assert.equal(usage.levels?.call?.cachedTokens, 0);
  assert.equal(usage.levels?.turn, null);
  assert.equal(usage.levels?.task?.totalTokens, 999);
  assert.equal(usage.levels?.task?.promptTokens, null);
});

test("缓存命中行要有分母，缺一个数就不给百分比", () => {
  assert.equal(usageCacheLabel(1250, 1500), "83% 走缓存（1,250 / 1,500）");
  assert.equal(usageCacheLabel(0, 1500), "0% 走缓存（0 / 1,500）");
  assert.equal(usageCacheLabel(null, 1500), "");
  assert.equal(usageCacheLabel(1250, null), "");
  assert.equal(usageCacheLabel(0, 0), "");
});

test("三级口径行：缺项写破折号，不写 0", () => {
  assert.equal(usageTrailLabel(550, 1650, 3700), "单次 550 · 本回合 1,650 · 任务累计 3,700");
  assert.equal(usageTrailLabel(550, null, 3700), "单次 550 · 本回合 — · 任务累计 3,700");
  assert.equal(usageTrailLabel(0, 0, 0), "单次 0 · 本回合 0 · 任务累计 0");
  assert.equal(usageTrailLabel(null, null, null), "");
});

test("用量事件不进对话流：一步一条，摊出来会把执行过程淹没", () => {
  const events: AgentEvent[] = [
    {
      seq: 1,
      node: "execute_action",
      type: "model.usage",
      payload: { role: "coder", call: { total_tokens: 550 }, turn: { total_tokens: 1650 }, task: { total_tokens: 3700 } },
      createdAt: "2026-10-22T00:00:00+00:00",
    },
    {
      seq: 2,
      node: "execute_action",
      type: "model.usage",
      payload: { role: "coder", call: { total_tokens: 20 }, turn: { total_tokens: 1670 }, task: { total_tokens: 3720 } },
      createdAt: "2026-10-22T00:00:01+00:00",
    },
  ];
  assert.deepEqual(buildBlocks(events), []);
  // 但同期一条真事件（工具结果）照常渲染，证明不是整条流被判空
  const withTool = buildBlocks([
    ...events,
    {
      seq: 3,
      node: "execute_action",
      type: "tool.completed",
      payload: { tool: "write_file", tool_call_id: "c1", output: "已写入", name: "a.txt" },
      createdAt: "2026-10-22T00:00:02+00:00",
    },
  ]);
  assert.ok(withTool.some((block) => block.kind === "tool"));
});
