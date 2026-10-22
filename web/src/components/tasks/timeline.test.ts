import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentEvent } from "./api.ts";
import { buildBlocks } from "./timeline.ts";

const validation = (payload: Record<string, unknown>): AgentEvent => ({
  seq: 1,
  node: "run_validation",
  type: "validation.completed",
  payload,
  createdAt: "2026-10-22T00:00:00+00:00",
});

const system = (event: AgentEvent) => {
  const block = buildBlocks([event]).find((item) => item.kind === "system");
  assert.ok(block, "验证事件必须在时间线里留一行，否则用户看不到有没有校验过");
  if (block.kind !== "system") throw new Error("unexpected block");
  return block;
};

test("跳过的校验报「未做自动化校验」，不报通过", () => {
  const block = system(
    validation({
      command: "pytest -q",
      passed: null,
      status: "skipped",
      skipped_reason: "命令执行未获授权，本次未做自动化校验",
    })
  );
  assert.equal(block.title, "未做自动化校验");
  assert.equal(block.tone, "warn");
  assert.match(block.detail, /命令执行未获授权/);
  assert.doesNotMatch(`${block.title}${block.detail}`, /通过/);
});

test("没带 passed 字段也不能当成通过", () => {
  // 老事件、别的运行时写的记录都可能压根没这个字段。缺证据 = 未验证。
  const block = system(validation({ command: "pytest -q", status: "unknown" }));
  assert.equal(block.title, "未做自动化校验");
  assert.doesNotMatch(`${block.title}${block.detail}`, /通过/);
});

test("真跑过且退码 0 才算通过", () => {
  const block = system(validation({ command: "pytest -q", passed: true, status: "ok", exit_code: 0 }));
  assert.equal(block.title, "验证完成");
  assert.equal(block.tone, "accent");
  assert.match(block.detail, /结果 通过/);
});

test("真失败如实说未通过", () => {
  const block = system(validation({ command: "pytest -q", passed: false, status: "failed", exit_code: 1 }));
  assert.equal(block.title, "验证未通过");
  assert.equal(block.tone, "danger");
  assert.match(block.detail, /结果 未通过/);
});
