import assert from "node:assert/strict";
import { test } from "node:test";

import { TASK_STATUS_FILTERS, taskMatchesFilter } from "./status.ts";

/**
 * 任务列表的状态筛选（学 Qoder 的「筛选与排序 / 按状态看 Issue」）。
 * 纯函数：分组判据错了，用户就会在"进行中"里看到已完成的、或找不到失败的任务。
 */

test("全部：任何状态都可见，包括未知状态", () => {
  for (const status of ["draft", "queued", "running", "waiting_approval", "succeeded", "failed", "cancelled", "", "weird"]) {
    assert.equal(taskMatchesFilter(status, "all"), true, status);
  }
});

test("进行中：草稿/排队/运行/待审批都在，终态不在", () => {
  for (const status of ["draft", "queued", "running", "waiting_approval"]) {
    assert.equal(taskMatchesFilter(status, "active"), true, status);
  }
  for (const status of ["succeeded", "failed", "cancelled", "weird"]) {
    assert.equal(taskMatchesFilter(status, "active"), false, status);
  }
});

test("已完成：只有 succeeded", () => {
  assert.equal(taskMatchesFilter("succeeded", "done"), true);
  for (const status of ["running", "failed", "cancelled", "queued"]) {
    assert.equal(taskMatchesFilter(status, "done"), false, status);
  }
});

test("未成功：失败与取消，不含已完成", () => {
  assert.equal(taskMatchesFilter("failed", "failed"), true);
  assert.equal(taskMatchesFilter("cancelled", "failed"), true);
  assert.equal(taskMatchesFilter("succeeded", "failed"), false);
});

test("大小写不敏感；未知状态只落在全部", () => {
  assert.equal(taskMatchesFilter("SUCCEEDED", "done"), true);
  assert.equal(taskMatchesFilter("Running", "active"), true);
  // 不猜未知状态属于哪一组，避免把它塞进某个分组后用户找不到。
  for (const filter of ["active", "done", "failed"] as const) {
    assert.equal(taskMatchesFilter("something_new", filter), false, filter);
  }
});

test("筛选组齐全且互斥地覆盖常见状态", () => {
  assert.deepEqual(
    TASK_STATUS_FILTERS.map((item) => item.key),
    ["all", "active", "done", "failed"],
  );
  const common = ["draft", "queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"];
  for (const status of common) {
    const hits = TASK_STATUS_FILTERS.filter((f) => f.key !== "all" && taskMatchesFilter(status, f.key));
    assert.equal(hits.length, 1, `${status} 应恰好落在一个分组，实际 ${hits.map((h) => h.key)}`);
  }
});
