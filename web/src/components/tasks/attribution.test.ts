import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeEvidence } from "./api.ts";
import { usageBranchLabel, usageTurnLabel, usageTurnsLabel } from "./status.ts";

/**
 * #43 的前端一侧：钱要能回答「这一轮花了多少」和「这条分支是谁引起的」。
 *
 * 八条判定，每一条都能失败：
 * 1. 后端 turns / branches / unattributed_calls 三个键原样搬到前端，字段逐条对上；
 *    normalizeUsageTurn 里任何一条 numOf 接错键名就红。
 * 2. 「后端没说」与「后端说了没有」是两件事：缺键 → null（面板「暂无」），
 *    给了空数组 → []（面板写「共 0 轮」）。把缺键兜底成 [] 就红——那等于替后端说话。
 * 3. 单项里缺的数读成 null，界面写破折号，不许写 0：0 会说「这一轮没花钱」。
 *    同时真·零必须能显示成 0，否则破折号分支就成了万能遮羞布。
 * 4. 老账（没有 run_id 的行）只以「归属未记 N 笔」的条数出现，绝不进任何一轮，
 *    也不许被读成「主代理本身」——那种线程连出身都没记过。
 * 5. 投影只搬声明过的键：后端在条目里多塞任何字段（含正文）都不跟着进结果。
 * 6. 前端不无中生有：轮数与分支数取自后端数组长度，不自己数、不自己合。
 * 7. 标签里不许出现主机路径，也不许出现后端没给过的「第 N 轮」编号。
 * 8. 真链路上的 id 形状必须念得出来：run 台账的 uuid 取前八位，主线程的 attempt 键
 *    念成「主线程#N」——砍前八位会让同一任务每次运行的父引用长成一个样。
 */

const TURN_KEYS = [
  "runId",
  "threads",
  "subagentCalls",
  "promptTokens",
  "completionTokens",
  "totalTokens",
  "cachedTokens",
  "calls",
];
const BRANCH_KEYS = [
  "traceId",
  "agentPath",
  "parentTraceId",
  "rootTraceId",
  "runId",
  "promptTokens",
  "completionTokens",
  "totalTokens",
  "cachedTokens",
  "calls",
];

const turnPayload = (over: Record<string, unknown> = {}) => ({
  run_id: "0f3a9c61-1b2c-4d5e-8f90-abcdef123456",
  threads: 2,
  subagent_calls: 3,
  calls: 5,
  prompt_tokens: 1500,
  completion_tokens: 150,
  total_tokens: 1650,
  cached_tokens: 1250,
  ...over,
});

const branchPayload = (over: Record<string, unknown> = {}) => ({
  trace_id: "thread-root-0f3a9c61",
  agent_path: "root/sub-1",
  parent_thread_id: "thread-main-9f2c",
  root_thread_id: "thread-main-9f2c",
  run_id: "0f3a9c61-1b2c-4d5e-8f90-abcdef123456",
  calls: 2,
  prompt_tokens: 700,
  completion_tokens: 70,
  total_tokens: 770,
  cached_tokens: 400,
  ...over,
});

const usageOf = (usage: Record<string, unknown>) => normalizeEvidence({ usage }).usage;

/** 走一遍真投影再交给标签函数：标签测的是前端类型，不是后端的蛇形键。 */
const turnOf = (over: Record<string, unknown> = {}) => {
  const item = usageOf({ turns: [turnPayload(over)] }).turns?.[0];
  assert.ok(item, "turns 里这一条必须投影出来");
  return item;
};
const branchOf = (over: Record<string, unknown> = {}) => {
  const item = usageOf({ branches: [branchPayload(over)] }).branches?.[0];
  assert.ok(item, "branches 里这一条必须投影出来");
  return item;
};

test("轮次与分支两张表按后端键名搬到前端", () => {
  const usage = usageOf({
    total_tokens: 2420,
    turns: [turnPayload()],
    branches: [branchPayload()],
    unattributed_calls: 0,
  });
  const turn = usage.turns?.[0];
  assert.ok(turn, "有 turns 就必须投影出一轮");
  assert.equal(turn.runId, "0f3a9c61-1b2c-4d5e-8f90-abcdef123456");
  assert.equal(turn.threads, 2);
  assert.equal(turn.subagentCalls, 3);
  assert.equal(turn.calls, 5);
  assert.equal(turn.promptTokens, 1500);
  assert.equal(turn.completionTokens, 150);
  assert.equal(turn.totalTokens, 1650);
  assert.equal(turn.cachedTokens, 1250);
  const branch = usage.branches?.[0];
  assert.ok(branch, "有 branches 就必须投影出一条线程");
  assert.equal(branch.traceId, "thread-root-0f3a9c61");
  assert.equal(branch.agentPath, "root/sub-1");
  assert.equal(branch.parentTraceId, "thread-main-9f2c");
  assert.equal(branch.rootTraceId, "thread-main-9f2c");
  assert.equal(branch.runId, "0f3a9c61-1b2c-4d5e-8f90-abcdef123456");
  assert.equal(branch.totalTokens, 770);
  assert.equal(usage.unattributedCalls, 0);
  // 一笔都没归属不上时不写「0 笔归属未记」：没有老账就不该提这件事。
  assert.equal(usageTurnsLabel(usage.turns, usage.unattributedCalls), "共 1 轮");
});

test("投影只搬声明过的键：后端多塞的字段（含正文）不跟着进来", () => {
  const usage = usageOf({
    turns: [turnPayload({ content: "这一轮的原始正文 MARKER-7788", messages: [{ role: "user" }] })],
    branches: [branchPayload({ payload: { hidden: 1 } })],
  });
  assert.deepEqual(Object.keys(usage.turns?.[0] ?? {}).sort(), [...TURN_KEYS].sort());
  assert.deepEqual(Object.keys(usage.branches?.[0] ?? {}).sort(), [...BRANCH_KEYS].sort());
  assert.doesNotMatch(JSON.stringify(usage.turns), /MARKER-7788/);
});

test("缺键读成 null（后端没说），空数组读成 []（后端说了没有）", () => {
  const silent = usageOf({ total_tokens: 100 });
  assert.equal(silent.turns, null);
  assert.equal(silent.branches, null);
  assert.equal(silent.unattributedCalls, null);
  // 「没说」在界面上是「暂无」，不是「共 0 轮」——后者是对后端的指控。
  assert.equal(usageTurnsLabel(silent.turns, silent.unattributedCalls), "");

  const spoken = usageOf({ turns: [], branches: [], unattributed_calls: 5 });
  assert.deepEqual(spoken.turns, []);
  assert.deepEqual(spoken.branches, []);
  assert.equal(usageTurnsLabel(spoken.turns, spoken.unattributedCalls), "共 0 轮 · 5 笔归属未记");
});

test("单项缺的数写破折号，真·零照样写 0", () => {
  const bare = turnOf({ run_id: "r-1", threads: undefined, subagent_calls: undefined, calls: undefined, prompt_tokens: undefined, completion_tokens: undefined, total_tokens: undefined, cached_tokens: undefined });
  assert.equal(bare.totalTokens, null);
  assert.equal(bare.calls, null);
  assert.equal(bare.threads, null);
  assert.equal(bare.subagentCalls, null);
  assert.equal(usageTurnLabel(bare), "r-1 · 合计 — · — 笔");
  assert.equal(branchOf({ trace_id: "t-1", total_tokens: undefined }).totalTokens, null);

  assert.equal(usageTurnLabel(turnOf({ run_id: "r-0", total_tokens: 0, calls: 0, subagent_calls: 0 })), "r-0 · 合计 0 · 0 笔");
});

test("老账只报条数：不进任何一轮，也不冒充主代理", () => {
  const legacy = usageOf({
    turns: [],
    branches: [branchPayload({ trace_id: "t-old", run_id: "", parent_thread_id: "", agent_path: "" })],
    unattributed_calls: 770,
  });
  assert.deepEqual(legacy.turns, [], "老账一条都不许被合成一轮");
  const label = usageBranchLabel(legacy.branches?.[0] ?? ({} as never));
  assert.equal(label, "无路径 · 合计 770 · 2 笔 · 归属未记");
  assert.doesNotMatch(label, /主代理/);
  assert.doesNotMatch(label, /第\s*\d+\s*轮/);
});

test("有轮 id 却没有父线程才是主代理本身；有父线程就写清派自谁", () => {
  assert.equal(
    usageBranchLabel(branchOf({ trace_id: "t-main", agent_path: "root", parent_thread_id: "", run_id: "r-1" })),
    "root · 合计 770 · 2 笔 · 主代理本身"
  );
  const child = usageBranchLabel(branchOf({ trace_id: "t-child", parent_thread_id: "thread-main-9f2c" }));
  assert.equal(child, "root/sub-1 · 合计 770 · 2 笔 · 派自 thread-m");
  assert.doesNotMatch(`${child}${usageTurnLabel(turnOf())}`, /[A-Za-z]:[\\/]/, "标签里不许出现主机路径");
});

test("轮数与分支数取自后端数组；「含子代理」只在后端真给了非零数时出现", () => {
  const usage = usageOf({
    turns: [turnPayload({ run_id: "r-1" }), turnPayload({ run_id: "r-2" })],
    branches: [branchPayload({ trace_id: "t-1" }), branchPayload({ trace_id: "t-2" }), branchPayload({ trace_id: "t-3" })],
    unattributed_calls: 2,
  });
  assert.equal(usage.turns?.length, 2);
  assert.equal(usage.branches?.length, 3);
  assert.equal(usageTurnsLabel(usage.turns, usage.unattributedCalls), "共 2 轮 · 2 笔归属未记");
  assert.equal(usageTurnLabel(usage.turns?.[1] ?? ({} as never)), "r-2 · 合计 1,650 · 5 笔 · 含子代理 3 笔");
  assert.doesNotMatch(usageTurnLabel(turnOf({ run_id: "r-3", subagent_calls: 0 })), /含子代理/);
});

test("字段名兼容 camelCase：别名不是死代码", () => {
  const item = usageOf({
    turns: [
      { runId: "r-9", threads: 1, subagentCalls: 1, promptTokens: 10, completionTokens: 2, totalTokens: 12, cachedTokens: 4, calls: 1 },
    ],
  }).turns?.[0];
  assert.equal(item?.runId, "r-9");
  assert.equal(item?.subagentCalls, 1);
  assert.equal(item?.totalTokens, 12);
});

test("团队链的轮 id 也念得出来：同一轮的三条发言不许砍成同一个前缀", () => {
  // 这三枚是 :8062 真跑一轮群聊（群 87646534…、成员小白管家派了两个子代理）落在
  // model_usage.run_id 上的实物：team.py 给每位发言成员现建一个任务，轮 id 写成
  // team-<群轮 id>-<发言 id>。砍前八位会把 38/39/40 全念成 "team-016"。
  const ROUND = "team-0162c5af-68f1-4eb8-9710-7612e9df00c3";
  const T38 = `${ROUND}-38`;
  const T39 = `${ROUND}-39`;
  const T40 = `${ROUND}-40`;
  assert.equal(usageTurnLabel(turnOf({ run_id: T39 })), "群聊第 39 条发言 · 合计 1,650 · 5 笔 · 含子代理 3 笔");
  const heads = [T38, T39, T40].map((id) => usageTurnLabel(turnOf({ run_id: id })).split(" · ")[0]);
  assert.equal(new Set(heads).size, 3, `同轮三条发言的读法必须互不相同：${heads.join(" / ")}`);
  assert.doesNotMatch(heads.join("|"), /team-016/, "不许出现砍前八位那种读法");
  // 分支的「派自」走同一个读法
  assert.equal(
    usageBranchLabel(branchOf({ parent_thread_id: T38 })),
    "root/sub-1 · 合计 770 · 2 笔 · 派自 群聊第 38 条发言"
  );
  // 放行续跑是另一枚 id（team.py 的 resume 入口写 …-resume），必须与原来那条区分得开。
  // 这一支真跑这轮没挂审批、没有实物，只有生产它的代码路径在（见 §7.11 未覆盖）。
  const resume = usageTurnLabel(turnOf({ run_id: `${T38}-resume` }));
  assert.equal(resume.split(" · ")[0], "群聊第 38 条发言（放行后续跑）");
  assert.notEqual(resume.split(" · ")[0], heads[0]);
  // 不是 team- 前缀的 id 一个都不许多管：uuid 与裸串照旧取前八位
  assert.equal(usageTurnLabel(turnOf({ run_id: "team-abc" })).split(" · ")[0], "team-abc");
});

test("真链路上的 id 形状要念得出来：attempt 键不是砍前八位", () => {
  // 这两形状是 :8062 真跑出来的实物（浏览器点验第一轮就是在这儿变红的）：
  // /start 的轮 id 是 agent_runs 的 uuid；/run 没有台账，轮 id 退回主线程的 attempt 键。
  const ATTEMPT_1 = "task:7d0e3f2c-1111-4222-8888-abcdef000011:attempt:1";
  const ATTEMPT_2 = "task:7d0e3f2c-1111-4222-8888-abcdef000011:attempt:2";
  assert.equal(usageTurnLabel(turnOf({ run_id: ATTEMPT_2 })), "主线程#2 · 合计 1,650 · 5 笔 · 含子代理 3 笔");
  assert.equal(usageTurnLabel(turnOf({ run_id: "545f3993-ec23-4b22-8f45-ce3c301d193a" })), "545f3993 · 合计 1,650 · 5 笔 · 含子代理 3 笔");
  // 同一任务两次运行的父引用必须长得不一样，且谁都不许是 "task:xxxx" 这种前八位
  const first = usageBranchLabel(branchOf({ parent_thread_id: ATTEMPT_1 }));
  const second = usageBranchLabel(branchOf({ parent_thread_id: ATTEMPT_2 }));
  assert.equal(first, "root/sub-1 · 合计 770 · 2 笔 · 派自 主线程#1");
  assert.notEqual(first, second, "两次运行的父引用砍成同一个前缀就等于没指认");
  assert.doesNotMatch(`${first}${second}${usageTurnLabel(turnOf({ run_id: ATTEMPT_1 }))}`, /task:/);
  // 子代理的随机 trace id 不是 attempt 键，照旧取前八位
  assert.equal(
    usageBranchLabel(branchOf({ trace_id: "3c27f762a9f5482984ee37e41d11a7fd", parent_thread_id: "3c27f762a9f5482984ee37e41d11a7fd" })),
    "root/sub-1 · 合计 770 · 2 笔 · 派自 3c27f762"
  );
});
