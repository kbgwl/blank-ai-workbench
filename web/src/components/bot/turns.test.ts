import assert from "node:assert/strict";
import { test } from "node:test";

import { buildSteps, dayBucket, isTurnRunning, threadTitleFrom, touchedFiles, turnReply, type RawEvent } from "./turns.ts";

const ev = (seq: number, type: string, payload: Record<string, unknown> = {}): RawEvent => ({
  seq,
  type,
  payload,
});

test("同一次工具调用的开始与结束配成一条，不重复显示", () => {
  const steps = buildSteps([
    ev(1, "tool.requested", { tool: "read_file", arguments: { path: "a.ts" } }),
    ev(2, "tool.completed", { tool: "read_file", ok: true, duration_ms: 12, summary: "文件内容" }),
  ]);
  assert.equal(steps.length, 1);
  const step = steps[0];
  assert.equal(step.kind, "tool");
  if (step.kind !== "tool") return;
  assert.equal(step.name, "read_file");
  assert.equal(step.status, "ok");
  assert.equal(step.durationMs, 12);
  assert.equal(step.output, "文件内容");
  assert.deepEqual(step.args, { path: "a.ts" });
});

test("连续两次同名工具各自配对自己的结果，不会串到第一条上", () => {
  const steps = buildSteps([
    ev(1, "tool.requested", { tool: "run_shell", arguments: { command: "npm test" } }),
    ev(2, "tool.requested", { tool: "run_shell", arguments: { command: "npm build" } }),
    ev(3, "tool.completed", { tool: "run_shell", ok: true, summary: "先结束的" }),
    ev(4, "tool.completed", { tool: "run_shell", ok: false, summary: "后结束的" }),
  ]);
  assert.equal(steps.length, 2);
  const [first, second] = steps;
  assert.equal(first.kind === "tool" && first.output, "先结束的");
  assert.equal(second.kind === "tool" && second.output, "后结束的");
  assert.equal(second.kind === "tool" && second.status, "failed");
});

test("没有开始事件的结果仍然保留——真实发生过的操作不能因为配不上就消失", () => {
  const steps = buildSteps([ev(1, "tool.completed", { tool: "apply_patch", ok: true, summary: "改好了" })]);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].kind === "tool" && steps[0].name, "apply_patch");
});

test("内部观测事件不进对话", () => {
  const steps = buildSteps([
    ev(1, "node.started", { node: "planner" }),
    ev(2, "context.measured", { tokens: 900 }),
    ev(3, "guardian.reviewed", { risk: "low" }),
    ev(4, "model.streamed", { chars: 100 }),
    ev(5, "memory.consolidated", {}),
    ev(6, "tool.requested", { tool: "list_files" }),
  ]);
  assert.deepEqual(
    steps.map((step) => step.kind),
    ["tool"],
  );
});

test("计划优先取数组，取不到时再从 text 里的 JSON 解", () => {
  const fromArray = buildSteps([ev(1, "plan.created", { plan: ["读代码", "改 bug"] })]);
  assert.deepEqual(fromArray[0].kind === "plan" ? fromArray[0].items : [], ["读代码", "改 bug"]);

  const fromText = buildSteps([
    ev(1, "plan.created", { text: JSON.stringify({ steps: ["确认范围", "执行改动"] }) }),
  ]);
  assert.deepEqual(fromText[0].kind === "plan" ? fromText[0].items : [], ["确认范围", "执行改动"]);

  // 既不是数组也不是 JSON：不生成空计划块，避免界面上出现一行没有内容的"计划"
  const broken = buildSteps([ev(1, "plan.created", { text: "我在想想" })]);
  assert.equal(broken.length, 0);
});

test("文件变更去重保序，同一文件多次改动只算一次", () => {
  const steps = buildSteps([
    ev(1, "file.changed", { path: "src/a.ts", change_type: "modified" }),
    ev(2, "file.changed", { path: "src/b.ts", change_type: "created" }),
    ev(3, "file.changed", { path: "src/a.ts", change_type: "modified" }),
  ]);
  assert.deepEqual(touchedFiles(steps), [
    { path: "src/a.ts", changeType: "modified" },
    { path: "src/b.ts", changeType: "created" },
  ]);
});

test("提问在回答后回填答案，而不是留一个悬空的问题", () => {
  const steps = buildSteps([
    ev(1, "question.asked", { question: "用哪个分支？" }),
    ev(2, "question.answered", { answer: "main" }),
  ]);
  assert.equal(steps.length, 1);
  const step = steps[0];
  assert.equal(step.kind, "question");
  if (step.kind !== "question") return;
  assert.equal(step.text, "用哪个分支？");
  assert.equal(step.answered, "main");
});

test("审批结论带得上结果", () => {
  const steps = buildSteps([
    ev(1, "approval.decided", { approved: false, message: "拒绝执行 rm -rf" }),
  ]);
  assert.equal(steps[0].kind, "approval");
  assert.equal(steps[0].kind === "approval" && steps[0].granted, false);
});

test("这一轮的最后回答取最后一条 task.completed，失败时取原因", () => {
  const ok = turnReply([
    ev(1, "task.completed", { status: "succeeded", summary: "第一版" }),
    ev(2, "task.completed", { status: "succeeded", summary: "最终版" }),
  ]);
  assert.equal(ok.text, "最终版");

  const failed = turnReply([ev(9, "task.failed", { reason: "模型超时" })]);
  assert.equal(failed.reason, "模型超时");
  assert.equal(failed.status, "failed");
});

test("跑没跑完：终态事件与终态状态都算结束", () => {
  assert.equal(isTurnRunning("running", []), true);
  assert.equal(isTurnRunning("succeeded", [ev(1, "tool.requested", { tool: "x" })]), false);
  assert.equal(isTurnRunning("running", [ev(1, "task.completed", { status: "succeeded" })]), false);
  assert.equal(isTurnRunning("running", [ev(1, "task.failed", { reason: "x" })]), false);
});

test("会话标题取第一行、削 markdown 前缀、按字符截断", () => {
  assert.equal(threadTitleFrom("# 修一下登录接口\n多的不要"), "修一下登录接口");
  assert.equal(threadTitleFrom("- 帮我看看这个 bug"), "帮我看看这个 bug");
  assert.equal(threadTitleFrom("\n\n   "), "新对话");
  // 以数字开头的正常句子不能被当成有序列表削掉
  assert.equal(threadTitleFrom("2026 年那次重构的逻辑"), "2026 年那次重构的逻辑");
  const long = threadTitleFrom("一".repeat(40));
  assert.equal([...long].length, 29);
  assert.ok(long.endsWith("…"));
});

test("会话按本地日历日分组，不看「距今几小时」", () => {
  const now = new Date(2026, 9, 21, 9, 0, 0).getTime(); // 10-21 09:00 本地
  assert.equal(dayBucket("2026-10-21T08:00:00+08:00", now), "today");
  // 昨天傍晚按小时只差 15 小时，但日历上是昨天
  assert.equal(dayBucket("2026-10-20T18:00:00+08:00", now), "yesterday");
  assert.equal(dayBucket("2026-10-18T10:00:00+08:00", now), "week");
  assert.equal(dayBucket("2026-09-01T10:00:00+08:00", now), "earlier");
  // 解析不出来不炸，归到更早
  assert.equal(dayBucket("", now), "earlier");
  assert.equal(dayBucket("不是时间", now), "earlier");
});
