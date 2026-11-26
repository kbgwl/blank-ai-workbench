import assert from "node:assert/strict";
import { test } from "node:test";

import { parsePricingDraft } from "./budget.ts";

test("空串表示没有价目表，不估价也不拦任务", () => {
  assert.deepEqual(parsePricingDraft(""), { ok: true, pricing: {} });
  assert.deepEqual(parsePricingDraft("   "), { ok: true, pricing: {} });
});

test("键统一小写，缺省字段补 0", () => {
  const parsed = parsePricingDraft(
    '{"DeepSeek": {"input": 0.28, "cached_input": 0.028}, "default": {"output": 8}}',
  );
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.deepEqual(parsed.pricing.deepseek, {
    input: 0.28,
    cached_input: 0.028,
    output: 0,
  });
  assert.deepEqual(parsed.pricing.default, { input: 0, cached_input: 0, output: 8 });
});

test("坏 JSON、数组、非对象档位都给出可定位的错误", () => {
  assert.equal(parsePricingDraft("{").ok, false);
  assert.equal(parsePricingDraft("[]").ok, false);
  const badRow = parsePricingDraft('{"default": "free"}');
  assert.equal(badRow.ok, false);
  if (!badRow.ok) assert.match(badRow.error, /default/);
});

test("负数与非数字单价被拒绝，而不是悄悄写成 0", () => {
  const negative = parsePricingDraft('{"default": {"input": -1}}');
  assert.equal(negative.ok, false);
  if (!negative.ok) assert.match(negative.error, /input/);

  const text = parsePricingDraft('{"default": {"output": "abc"}}');
  assert.equal(text.ok, false);
});
