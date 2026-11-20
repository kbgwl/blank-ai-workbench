import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildSteps,
  collapseRepeatedNotes,
  composerAction,
  dayBucket,
  dispatchAutoStart,
  filterSteps,
  isTurnRunning,
  planOf,
  quoteBlock,
  quoteDraft,
  reasonText,
  resolveTouchedPath,
  shouldAutoRunTurn,
  splitTitleStamp,
  thinkVerb,
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
  const fromArray = planOf([ev(1, "plan.created", { plan: ["读代码", "改 bug"] })]);
  assert.deepEqual(fromArray?.items.map((item) => item.text), ["读代码", "改 bug"]);

  const fromText = planOf([
    ev(1, "plan.created", { text: JSON.stringify({ steps: ["确认范围", "执行改动"] }) }),
  ]);
  assert.deepEqual(fromText?.items.map((item) => item.text), ["确认范围", "执行改动"]);

  // 既不是数组也不是 JSON：不生成空计划块，避免界面上出现一行没有内容的"计划"
  assert.equal(planOf([ev(1, "plan.created", { text: "我在想想" })]), null);
});

test("计划不混进步骤流：两处都画会让人以为执行了两遍", () => {
  const steps = buildSteps([ev(1, "plan.created", { plan: ["读代码", "改 bug"] })]);
  assert.equal(steps.length, 0);
});

test("plan.updated 覆盖步骤并带上完成进度", () => {
  const plan = planOf([
    ev(1, "plan.created", { plan: ["查依赖", "改实现", "跑测试"] }),
    ev(2, "plan.updated", {
      steps: [
        { step: "查依赖", status: "completed" },
        { step: "改实现", status: "in_progress" },
        { step: "跑测试", status: "pending" },
      ],
    }),
  ]);
  assert.equal(plan?.total, 3);
  assert.equal(plan?.completed, 1);
  assert.deepEqual(
    plan?.items.map((item) => item.status),
    ["completed", "in_progress", "pending"],
  );
});

test("只有 update 没有 created 也能显示计划", () => {
  const plan = planOf([ev(1, "plan.updated", { steps: [{ step: "只做一件事", status: "completed" }] })]);
  assert.equal(plan?.completed, 1);
  assert.equal(plan?.items[0].text, "只做一件事");
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

test("终态码：细分的 detail 优先，族码兜底，旧数据退回 payload.code", () => {
  // 后端 agent/terminal.py 的两个层次：族码是封闭枚举（界面能穷举），
  // detail 是族下面的种（verifier 的 zero_change、供应商的 model_timeout）。
  const blocked = turnReply([
    ev(1, "task.failed", {
      reason: "目标要求改动，但工作区没有任何文件变化",
      terminal: { code: "verification_blocked", detail: "zero_change" },
    }),
  ]);
  assert.equal(blocked.code, "zero_change");

  const family = turnReply([
    ev(1, "task.failed", { reason: "模型请求超时", terminal: { code: "model_error" } }),
  ]);
  assert.equal(family.code, "model_error");

  // 升级前的数据只有 payload.code：行为必须与从前一致，不许变成空串。
  const legacy = turnReply([ev(1, "task.failed", { reason: "x", code: "model_timeout" })]);
  assert.equal(legacy.code, "model_timeout");

  // detail 比 payload.code 更具体时才用它；两者都在时先看 detail。
  const both = turnReply([
    ev(1, "task.failed", { code: "model_error", terminal: { code: "model_error", detail: "model_timeout" } }),
  ]);
  assert.equal(both.code, "model_timeout");

  // 形状不对的 terminal（不是对象、字段不是字符串）一律当没有，不许抛。
  const broken = turnReply([
    ev(1, "task.failed", { reason: "x", code: "model_timeout", terminal: "oops" }),
  ]);
  assert.equal(broken.code, "model_timeout");
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

test("只有新草稿要自动跑：控制词返回的是已有任务", () => {
  // Chat Supervisor 把「暂停 / 继续 / 同意」作用在旧任务上，再 /start 一次
  // 会把刚暂停的任务拉起来，或者撞上「任务已在执行」的 409。
  assert.equal(shouldAutoRunTurn("draft"), true);
  for (const status of ["queued", "running", "paused", "waiting_approval", "failed", "succeeded", "cancelled"]) {
    assert.equal(shouldAutoRunTurn(status), false, status);
  }
});

test("后端下发判定为不自动跑时，draft 也不许替用户点运行", () => {
  // v165 §5.1 / §8：手动模式、等待审批、等待补充信息都返回 auto_start=false。
  // 界面不能只看 status 就把「用户明确点击执行」这一档绕过去。
  assert.equal(shouldAutoRunTurn("draft", false), false);
  assert.equal(shouldAutoRunTurn("draft", true), true);
  assert.equal(shouldAutoRunTurn("draft", null), true);
  assert.equal(dispatchAutoStart({ dispatch: { auto_start: false } }), false);
  assert.equal(dispatchAutoStart({ dispatch: { auto_start: true } }), true);
  assert.equal(dispatchAutoStart({}), null);
  assert.equal(dispatchAutoStart(undefined), null);
  assert.equal(dispatchAutoStart({ dispatch: "bad" }), null);
});

test("插话从排队改写成已生效，不重复占两行", () => {
  const steps = buildSteps([
    ev(1, "steer.queued", { message: "改成用 Postgres", queued_at: 1000 }),
    ev(2, "steer.applied", { step: 4, chars: 8, delay_ms: 4200 }),
  ]);

  assert.equal(steps.length, 1);
  const note = steps[0];
  assert.equal(note.kind, "note");
  // 三件事都要在：这句话是什么、从第几步生效、等了多久。
  // 只写"已生效"用户不知道生效的是哪一句；不写等待时长，长任务里他会以为没生效。
  assert.match((note as Extract<BotStep, { kind: "note" }>).text, /Postgres/);
  assert.match((note as Extract<BotStep, { kind: "note" }>).text, /第 4 步/);
  assert.match((note as Extract<BotStep, { kind: "note" }>).text, /4 秒/);
});

test("找不到排队记录的生效事件也要显示出来", () => {
  // 事件被裁剪、或这句来自更早的一轮时，只看到 applied。悄悄吞掉等于
  // 让用户以为插话没进上下文，于是他会再发一遍。
  const steps = buildSteps([ev(1, "steer.applied", { step: 2, chars: 6, delay_ms: 0 })]);
  assert.equal(steps.length, 1);
  assert.match((steps[0] as Extract<BotStep, { kind: "note" }>).text, /第 2 步/);
});

test("两条插话按先后各自配对", () => {
  const steps = buildSteps([
    ev(1, "steer.queued", { message: "第一句" }),
    ev(2, "steer.queued", { message: "第二句" }),
    ev(3, "steer.applied", { step: 3, delay_ms: 0 }),
    ev(4, "steer.applied", { step: 5, delay_ms: 0 }),
  ]);

  assert.equal(steps.length, 2);
  assert.match((steps[0] as Extract<BotStep, { kind: "note" }>).text, /第一句/);
  assert.match((steps[1] as Extract<BotStep, { kind: "note" }>).text, /第二句/);
});

test("同一句话被提交两次：只显示一条，标出次数", () => {
  // 连按两次回车曾经会真的发两次（draft 异步清空），后端各记一条，
  // 界面上就是两段逐字相同的话，看着像它在复读。
  const steps = buildSteps([
    ev(1, "steer.queued", { message: "换一条命令" }),
    ev(2, "steer.queued", { message: "换一条命令" }),
  ]);
  const collapsed = collapseRepeatedNotes(steps);
  assert.equal(collapsed.length, 1);
  assert.equal((collapsed[0] as Extract<BotStep, { kind: "note" }>).repeat, 2);
});

test("中间隔着别的步骤，同样的话算「又说了一遍」而不是重复", () => {
  const steps = buildSteps([
    ev(1, "steer.queued", { message: "继续" }),
    ev(2, "tool.requested", { tool: "read_file", arguments: { path: "a.ts" } }),
    ev(3, "steer.queued", { message: "继续" }),
  ]);
  assert.equal(collapseRepeatedNotes(steps).length, 3);
});

test("会话标题末尾的时间拆到右列", () => {
  assert.deepEqual(splitTitleStamp("雷罡 21:51:06"), { base: "雷罡", stamp: "21:51:06" });
  assert.deepEqual(splitTitleStamp("修登录按钮 09:05"), { base: "修登录按钮", stamp: "09:05" });
});

test("标题里没有时间就原样返回，不改写", () => {
  assert.deepEqual(splitTitleStamp("改一版首页"), { base: "改一版首页", stamp: "" });
  // 冒号在中间的不是时间尾巴
  assert.deepEqual(splitTitleStamp("把 3:1 改成 2:1"), { base: "把 3:1 改成 2:1", stamp: "" });
  assert.deepEqual(splitTitleStamp(""), { base: "", stamp: "" });
});

test("「在想…」的词由开始时间定死，同一轮不会每帧跳", () => {
  const at = Date.parse("2026-11-19T10:00:00");
  assert.equal(thinkVerb(at), thinkVerb(at));
  // 相隔不到一秒：还是同一个词（重新渲染不该换词）
  assert.equal(thinkVerb(at), thinkVerb(at + 800));
});

test("不同的开始时间会落进不同的词，六秒一轮回", () => {
  const seen = new Set<string>();
  for (let i = 0; i < 6; i += 1) seen.add(thinkVerb(1_700_000_000_000 + i * 1000));
  assert.equal(seen.size, 6);
  assert.equal(thinkVerb(1_700_000_000_000), thinkVerb(1_700_000_006_000));
});

test("没有开始时间也不空白，回落第一个词", () => {
  assert.equal(thinkVerb(null), "在想");
  assert.equal(thinkVerb(Number.NaN), "在想");
});

test("失败原因里的机器码不裸奔给用户", () => {
  // 实测页面上出现过一行红字就写着 approval_expired——那是变量名，不是"发生了什么"。
  assert.match(reasonText("approval_expired"), /审批卡.*有效期/);
  assert.match(reasonText("APPROVAL_EXPIRED"), /审批卡/);
  // 后端抄回来的真实错误原样保留，那是排查依据。
  assert.equal(reasonText("流在工具调用 write_file 的参数中途结束"), "流在工具调用 write_file 的参数中途结束");
  // 没登记过的码也要套一句人话，把码留在括号里。
  assert.equal(reasonText("some_new_code"), "这一轮失败了（some_new_code）");
  assert.equal(reasonText(""), "");
});

test("失败事件的原因经过同一个翻译，页面读到的不是标识符", () => {
  const verdict = turnReply([ev(1, "task.failed", { status: "failed", reason: "approval_expired" })]);
  assert.equal(verdict.status, "failed");
  assert.ok(!verdict.reason.includes("approval_expired"), verdict.reason);
});

test("选中一段话变成引用块，塞在用户已经在打的内容前面", () => {
  const block = quoteBlock("就这一句话");
  assert.equal(block, "> 就这一句话\n\n");

  // 多行：每一行都带 >，否则渲染时引用块会断成两段。
  assert.equal(quoteBlock("第一行\n第二行"), "> 第一行\n> 第二行\n\n");
  // 空行写成单个 >，不能变成真正的空行。
  assert.equal(quoteBlock("上\n\n下"), "> 上\n>\n> 下\n\n");

  // 引用在前、已有的草稿在后：读起来才是"针对这段，我想问…"。
  assert.equal(quoteDraft("我想问个事", "原文"), "> 原文\n\n我想问个事");
  assert.equal(quoteDraft("", "原文"), "> 原文\n\n");
});

test("引用的三条底线：空的不插、超长截断、同一段不插两遍", () => {
  assert.equal(quoteBlock(""), "");
  assert.equal(quoteBlock("   \n  "), "");
  assert.equal(quoteDraft("原有内容", "   "), "原有内容");

  // 超长截到句边界，且标出还有下文。
  const long = "第一句话在这里。".repeat(80);
  const cut = quoteBlock(long, 60);
  assert.ok([...cut].length < 90, `没截断：${cut.length}`);
  assert.match(cut, /……/);
  assert.ok(cut.trimEnd().endsWith("。……"), cut);

  // 连点两次「引用」应该是没反应，不是同一段话出现两次。
  const once = quoteDraft("", "同一段话");
  assert.equal(quoteDraft(once, "同一段话"), once);
});

test("失败事件带上原始原因和错误码：错误分层要用它们认成因", () => {
  const verdict = turnReply([
    ev(3, "task.failed", { status: "failed", reason: "模型请求超时：qwen", code: "model_timeout" }),
  ]);
  assert.equal(verdict.code, "model_timeout");
  assert.equal(verdict.rawReason, "模型请求超时：qwen");
  // 给人看的那句已经翻译过，不能拿去匹配关键词。
  assert.equal(verdict.reason, "模型请求超时：qwen");
  // 老数据没有 code，不能报错也不能编一个。
  assert.equal(turnReply([ev(4, "task.failed", { reason: "x" })]).code, "");
});

test("打回成功在对话页留一行说明，不用去任务页对账", () => {
  // 计划卡只会在成功时跟着变；没有这一行，用户看不出"退回的是第几步"。
  const steps = buildSteps([ev(1, "plan.rewound", { step_id: "s2", index: 1, steps: [] })]);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].kind, "note");
  if (steps[0].kind !== "note") return;
  assert.equal(steps[0].tone, "muted");
  assert.match(steps[0].text, /第 2 步/, "序号要按 1 起算，与计划面板一致");
});

test("打回没生效在对话页是警告行，且写明计划没有重开", () => {
  // 这一档计划根本不动，不给行说明的话，用户看到的就是"点了没反应"。
  const steps = buildSteps([
    ev(1, "plan.rewind_missed", { step_id: "legacy-0", known_step_ids: ["a1", "b2", "c3"] }),
  ]);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].kind, "note");
  if (steps[0].kind !== "note") return;
  assert.equal(steps[0].tone, "warn");
  assert.match(steps[0].text, /legacy-0/);
  assert.match(steps[0].text, /3 步/);
  assert.match(steps[0].text, /没有重开/);
});
