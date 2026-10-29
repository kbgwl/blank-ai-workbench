import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildSteps,
  composerAction,
  dayBucket,
  filterSteps,
  isTurnRunning,
  resolveTouchedPath,
  threadTitleFrom,
  touchedFiles,
  turnEvidence,
  turnReply,
  type BotStep,
  type RawEvent,
} from "./turns.ts";

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

test("取消请求也算结束：这时候再输入该开新一轮，不该往停下的那一轮里塞", () => {
  // 用户点停止后，后端可能还没走到收尾（模型调用挂着就永远走不到）。
  // 若这里仍判"在跑"，输入会被当成插话发给一个已结束的任务，全部打回
  // 409「任务已结束，无法插话」——界面看起来就是彻底不能用。
  assert.equal(isTurnRunning("running", [ev(1, "task.cancel_requested", { status: "cancelled" })]), false);
  assert.equal(isTurnRunning("cancelled", []), false);
  // 驳回高风险操作后还能接着说一句，所以 paused 不算结束。
  assert.equal(isTurnRunning("paused", []), true);
  assert.equal(isTurnRunning("waiting_approval", []), true);
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

// ── 依据条：把过程事实从正文里挪出来的那块统计 ──────────────────────

test("依据条数清看了几次、改了哪些、跑了什么、失败几次", () => {
  const steps = buildSteps([
    ev(1, "tool.requested", { tool: "read_file", arguments: { path: "a.ts" } }),
    ev(2, "tool.completed", { tool: "read_file", ok: true, duration_ms: 100 }),
    ev(3, "tool.requested", { tool: "run_shell", arguments: { command: "pytest -q" } }),
    ev(4, "tool.failed", { tool: "run_shell", ok: false, duration_ms: 300, summary: "1 failed" }),
    ev(5, "file.changed", { path: "a.ts", change_type: "modified" }),
  ]);
  const evidence = turnEvidence(steps);
  assert.equal(evidence.calls, 2);
  assert.equal(evidence.looked, 1);
  assert.equal(evidence.files, 1);
  assert.equal(evidence.failed, 1);
  assert.equal(evidence.durationMs, 400);
  assert.deepEqual(evidence.commands, ["pytest -q"]);
  assert.equal(evidence.commandCount, 1);
});

test("同一个文件改三次算一个文件", () => {
  // 用户问的是"动了哪些文件"，不是"写了几次"。按事件数算会把 1 个文件说成 3 个。
  const steps = buildSteps([
    ev(1, "file.changed", { path: "a.ts" }),
    ev(2, "file.changed", { path: "a.ts" }),
    ev(3, "file.changed", { path: "b.ts" }),
  ]);
  assert.equal(turnEvidence(steps).files, 2);
});

test("命令去重，且超过 8 条时总数仍然准", () => {
  // commands 只留 8 条给悬浮提示，用它的长度当总数会把 20 条说成 8 条。
  const events: RawEvent[] = [];
  for (let index = 0; index < 20; index += 1) {
    events.push(ev(index + 1, "tool.requested", { tool: "run_shell", arguments: { command: `cmd ${index}` } }));
  }
  events.push(ev(99, "tool.requested", { tool: "run_shell", arguments: { command: "cmd 0" } }));
  const evidence = turnEvidence(buildSteps(events));
  assert.equal(evidence.commands.length, 8);
  assert.equal(evidence.commandCount, 20);
});

test("没有 command 参数的工具不占命令数", () => {
  // run_tests 这类工具本来就没有 command 参数。硬塞一条空串会让
  // 「跑了 3 条命令」变成「跑了 3 条命令（其中一条是空的）」。
  const steps = buildSteps([
    ev(1, "tool.requested", { tool: "run_tests", arguments: {} }),
    ev(2, "tool.completed", { tool: "run_tests", ok: true }),
  ]);
  assert.equal(turnEvidence(steps).calls, 1);
  assert.equal(turnEvidence(steps).commandCount, 0);
  assert.deepEqual(turnEvidence(steps).commands, []);
});

test("只写不读时 looked 为 0，这是「没看就改」的可见信号", () => {
  const steps = buildSteps([
    ev(1, "tool.requested", { tool: "apply_patch", arguments: { path: "a.ts" } }),
    ev(2, "tool.completed", { tool: "apply_patch", ok: true }),
    ev(3, "file.changed", { path: "a.ts" }),
  ]);
  const evidence = turnEvidence(steps);
  assert.equal(evidence.looked, 0);
  assert.equal(evidence.files, 1);
});

test("正文里的文件名能落到本轮改动的那个文件上", () => {
  const files = ["web/src/components/bot/composer.tsx", "services/intent.py"];
  assert.equal(resolveTouchedPath("web/src/components/bot/composer.tsx", files), files[0]);
  assert.equal(resolveTouchedPath("composer.tsx", files), files[0]);
  assert.equal(resolveTouchedPath("bot/composer.tsx", files), files[0]);
  assert.equal(resolveTouchedPath("intent.py", files), files[1]);
});

test("同名文件都改过时不猜，宁可点不动", () => {
  // 两个 index.ts 都改过，写 `index.ts` 的人心里指的是哪一个从文本里看不出来。
  // 给一个点不动的代码样式，好过给一个点错地方的链接。
  const files = ["packages/a/index.ts", "packages/b/index.ts"];
  assert.equal(resolveTouchedPath("index.ts", files), "");
  assert.equal(resolveTouchedPath("packages/b/index.ts", files), files[1]);
});

test("不做包含匹配，避免 test.py 命中 contest.py", () => {
  const files = ["src/contest.py"];
  assert.equal(resolveTouchedPath("test.py", files), "");
});

test("不像路径的代码片段不动它", () => {
  const files = ["src/a.ts"];
  assert.equal(resolveTouchedPath("npm test", files), "");
  assert.equal(resolveTouchedPath("", files), "");
});

test("筛选：只看改动 / 只看失败", () => {
  const steps: BotStep[] = buildSteps([
    ev(1, "tool.requested", { tool: "run_shell", arguments: { command: "x" } }),
    ev(2, "tool.failed", { tool: "run_shell", ok: false }),
    ev(3, "file.changed", { path: "a.ts" }),
    ev(4, "plan.created", { plan: ["第一步", "第二步"] }),
  ]);
  assert.equal(filterSteps(steps, "all").length, steps.length);
  assert.deepEqual(
    filterSteps(steps, "changes").map((step) => step.kind),
    ["change"],
  );
  assert.equal(filterSteps(steps, "failures").length, 1);
});

test("输入框只有一个按钮：跑着是停止，闲着是发送", () => {
  // 以前跑着的时候是「停止 + 插话」两个圆钮，对话区还另有一个「停止」文字按钮。
  // 三处都能停，用户得先分清哪个是哪个；合成一个之后，插话只走 Enter。
  assert.equal(composerAction(true, true), "stop");
  assert.equal(composerAction(true, false), "stop");
  assert.equal(composerAction(false, true), "send");
  assert.equal(composerAction(false, false), "disabled");
});
