import assert from "node:assert/strict";
import { test } from "node:test";

import { verdictOf } from "./verdict.ts";

test("passed 为 null 一律是「未验证」，不能显示成通过", () => {
  // 后端跳过校验时写 passed:null。三种跳过原因（没改文件、命令没授权、还在等审批）
  // 在证据上的含义相同：一行测试都没跑过。
  for (const status of ["skipped", "unknown", ""]) {
    const verdict = verdictOf(null, status);
    assert.equal(verdict.label, "未验证");
    assert.equal(verdict.tone, "warn");
    assert.doesNotMatch(`${verdict.title}${verdict.label}`, /通过/);
  }
});

test("真跑过且通过才说通过", () => {
  assert.equal(verdictOf(true, "ok").label, "通过");
  assert.equal(verdictOf(true, "ok").tone, "accent");
});

test("真失败如实说未通过", () => {
  assert.equal(verdictOf(false, "failed").label, "未通过");
  assert.equal(verdictOf(false, "failed").tone, "danger");
});

test("未验证的标题不带「验证完成」这种结论性说法", () => {
  assert.equal(verdictOf(null, "skipped").title, "未做自动化校验");
});
