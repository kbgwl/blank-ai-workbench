import assert from "node:assert/strict";
import { test } from "node:test";

import { criteriaText, evidenceDigest, hasEvidence, testState } from "./format.ts";
import type { Evidence } from "../tasks/api.ts";

const emptyEvidence = (over: Partial<Evidence> = {}): Evidence =>
  ({
    status: "succeeded",
    acceptance: [],
    changes: [],
    test: null,
    usage: { totalTokens: null },
    ...over,
  }) as unknown as Evidence;

test("测试四态：通过、失败、跳过、没有记录各说各的", () => {
  assert.equal(testState(null), "none");
  assert.equal(testState({ command: "", exitCode: null, output: "", passed: null, status: "", skippedReason: "" }), "none");
  assert.equal(testState({ command: "pytest", exitCode: 0, output: "", passed: true, status: "passed", skippedReason: "" }), "passed");
  assert.equal(testState({ command: "pytest", exitCode: 1, output: "", passed: false, status: "failed", skippedReason: "" }), "failed");
  assert.equal(
    testState({ command: "pytest", exitCode: null, output: "", passed: null, status: "skipped", skippedReason: "没有可用测试" }),
    "skipped",
  );
});

test("有命令但没判定：念「未判定」，不许念「通过」", () => {
  assert.equal(testState({ command: "pytest -q", exitCode: null, output: "", passed: null, status: "", skippedReason: "" }), "unknown");
});

test("跳过的原因要跟着一起说：只写「测试跳过」，用户不知道该等什么", () => {
  const digest = evidenceDigest(
    emptyEvidence({
      test: { command: "pytest", exitCode: null, output: "", passed: null, status: "skipped", skippedReason: "后端未启动" },
    }),
  );
  assert.equal(digest.test, "skipped");
  assert.equal(digest.testText, "测试跳过：后端未启动");
});

test("验收三态分开计数：没覆盖是要补写，未判定是要判定", () => {
  const digest = evidenceDigest(
    emptyEvidence({
      acceptance: [
        { criterion: "a", covered: true, note: "" },
        { criterion: "b", covered: false, note: "" },
        { criterion: "c", covered: null, note: "" },
      ],
    }),
  );
  assert.deepEqual(
    { total: digest.criteriaTotal, covered: digest.covered, uncovered: digest.uncovered, unjudged: digest.unjudged },
    { total: 3, covered: 1, uncovered: 1, unjudged: 1 },
  );
  assert.equal(criteriaText(digest), "1/3 覆盖 · 1 条没覆盖 · 1 条未判定");
});

test("同一文件改多次算一个文件", () => {
  const digest = evidenceDigest(
    emptyEvidence({
      changes: [
        { path: "a.py", diff: "", changeType: "modified" },
        { path: "a.py", diff: "", changeType: "modified" },
        { path: "b.py", diff: "", changeType: "created" },
      ],
    }),
  );
  assert.equal(digest.files, 2);
});

test("没有验收条件时说实话，不写 0/0 覆盖", () => {
  assert.equal(criteriaText(evidenceDigest(emptyEvidence())), "没有验收条件");
});

test("四项全空的任务不算有证据：展开只会得到一张空面板", () => {
  assert.equal(hasEvidence(evidenceDigest(emptyEvidence())), false);
  assert.equal(hasEvidence(evidenceDigest(emptyEvidence({ changes: [{ path: "a.py", diff: "", changeType: "modified" }] }))), true);
  assert.equal(hasEvidence(evidenceDigest(emptyEvidence({ usage: { totalTokens: 12 } } as never))), true);
});
