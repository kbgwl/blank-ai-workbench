import assert from "node:assert/strict";
import { test } from "node:test";

import { backgroundTaskTarget, visibleBackgroundTasks } from "./background.ts";
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
