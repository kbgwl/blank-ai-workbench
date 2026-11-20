import assert from "node:assert/strict";
import { test } from "node:test";

import {
  backgroundRows,
  backgroundRowsFor,
  backgroundTaskTarget,
  visibleBackgroundTasks,
} from "./background.ts";
import type { Task } from "../tasks/api.ts";

const task = (id: string, threadId: string): Task =>
  ({
    id,
    threadId,
    title: `任务 ${id}`,
    status: "running",
    projectId: "p1",
    prompt: "",
    providerId: "m1",
    providerName: "m1",
    acceptanceCriteria: [],
    resultSummary: "",
    failureReason: "",
    stepsUsed: 0,
    maxSteps: 10,
    goalSpec: {},
    planSteps: [],
    milestones: [],
    planProgress: { total: 0, completed: 0, inProgress: 0, pending: 0, percent: 0, milestones: [] },
    planRevision: 0,
    turnIndex: 1,
    createdAt: "",
    updatedAt: "",
    raw: {},
  }) as Task;

test("孤儿任务不能被后台横幅过滤掉：没有当前会话时也要显示", () => {
  const orphan = task("t-orphan", "");
  assert.deepEqual(
    visibleBackgroundTasks([orphan], undefined).map((row) => row.id),
    ["t-orphan"],
  );
});

test("孤儿任务在已有当前会话时同样显示", () => {
  const orphan = task("t-orphan", "");
  const other = task("t-other", "thread-2");
  assert.deepEqual(
    visibleBackgroundTasks([orphan, other], "thread-1").map((row) => row.id),
    ["t-orphan", "t-other"],
  );
});

test("正在看的同一条会话不重复报一遍", () => {
  const current = task("t-current", "thread-1");
  assert.deepEqual(visibleBackgroundTasks([current], "thread-1"), []);
});

test("孤儿任务兜底到按 task_id 打开的任务详情", () => {
  assert.deepEqual(backgroundTaskTarget(task("a b&c", "")), {
    kind: "detail",
    href: "/tasks/detail?id=a%20b%26c",
  });
});

test("有会话的任务回到那条会话", () => {
  assert.deepEqual(backgroundTaskTarget(task("t-1", "thread-1")), {
    kind: "thread",
    threadId: "thread-1",
  });
});

test("清单每一行都说得出「在做什么、跑了多久」", () => {
  const now = Date.parse("2026-11-20T12:00:00Z");
  const rows = backgroundRows(
    [
      { ...task("a", "t1"), status: "running", createdAt: "2026-11-20T11:48:00Z" },
      { ...task("b", "t2"), status: "waiting_approval", createdAt: "2026-11-20T11:57:00Z" },
      { ...task("c", ""), status: "queued", createdAt: "2026-11-20T11:59:00Z" },
    ],
    now,
  );
  // 在跑的排前面，然后等审批的、排队的。
  assert.deepEqual(
    rows.map((row) => row.id),
    ["a", "b", "c"],
  );
  // 同一段时间，动词跟着状态变：等 3 分钟不是跑 3 分钟。
  assert.equal(rows[0].elapsed, "跑了 12 分钟");
  assert.equal(rows[1].elapsed, "已等 3 分钟");
  assert.equal(rows[2].elapsed, "排了 1 分钟");
  assert.equal(rows[1].waiting, true);
  // 没有 threadId 的孤儿任务也要有去处（任务详情），不能点不动。
  assert.equal(rows[2].target.kind, "detail");
  assert.ok(rows.every((row) => row.stoppable));
});

test("还在动的才算清单，已经结束的不混进来", () => {
  const rows = backgroundRows([
    { ...task("a", "t1"), status: "running" },
    { ...task("b", "t2"), status: "failed" },
    { ...task("c", "t3"), status: "succeeded" },
  ]);
  assert.deepEqual(
    rows.map((row) => row.id),
    ["a"],
  );
});

test("正在看的那条不重复报，但孤儿任务一定留下", () => {
  const rows = backgroundRowsFor(
    [
      { ...task("a", "t1"), status: "running" },
      { ...task("b", ""), status: "running" },
    ],
    "t1",
  );
  assert.deepEqual(
    rows.map((row) => row.id),
    ["b"],
  );
});
