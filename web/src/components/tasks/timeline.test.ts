import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentEvent } from "./api.ts";
import { boundaryLabel, boundaryNeedsWarn, compactionHeadline, compactionTriggerLabel, compactionWaterLabel } from "./status.ts";
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

test("治理快照不进对话流，只在证据包里看", () => {
  // turn.context 一条任务可能有几条，摊进对话流就是把「cpus 1.0」这种工程细节
  // 重新倒回用户脸上——那正是这批区块以前被砍掉的原因。
  const blocks = buildBlocks([
    { seq: 2, node: "call_model", type: "turn.context", payload: { step: 0, sandbox: { backend: "none" } }, createdAt: "2026-10-22T00:00:00+00:00" },
  ]);
  assert.deepEqual(blocks, []);
});

const toolEvent = (payload: Record<string, unknown>, seq = 3): AgentEvent => ({
  seq,
  node: "execute_action",
  type: "tool.completed",
  payload,
  createdAt: "2026-10-22T00:00:00+00:00",
});

function toolBlock(payload: Record<string, unknown>) {
  const block = buildBlocks([toolEvent(payload)]).find((item) => item.kind === "tool");
  assert.ok(block, "工具卡片必须出现在时间线里");
  if (block.kind !== "tool") throw new Error("unexpected block");
  return block;
}

test("宿主直连执行的命令卡片要把它说出来", () => {
  // 后端下发的是 `sandbox_backend: null`（键在、值为 null）——"跑过进程但没有沙箱"。
  // 这一档最不该被安静地省略：用户以为命令被隔离过，其实直接落在自己机器上。
  const block = toolBlock({ tool: "run_shell", status: "ok", sandbox_backend: null });
  assert.equal(block.boundary, "host");
  assert.equal(boundaryLabel(block.boundary), "宿主执行（未隔离）");
  assert.equal(boundaryNeedsWarn(block.boundary), true);
});

test("有沙箱后端时照原样报名字，不写成宿主", () => {
  const block = toolBlock({ tool: "run_shell", status: "ok", sandbox_backend: "win-native" });
  assert.equal(block.boundary, "win-native");
  assert.equal(boundaryLabel(block.boundary), "沙箱：win-native");
  assert.equal(boundaryNeedsWarn(block.boundary), false);
});

test("没带这个事实的事件不许显示边界", () => {
  // 只读工具、被拒绝的调用、以及这次改动之前的老任务都没有这个键。
  // 把缺字段读成"宿主执行"，等于给每条老任务都打一个没发生过的警告。
  const block = toolBlock({ tool: "read_file", status: "ok" });
  assert.equal(block.boundary, "");
  assert.equal(boundaryLabel(block.boundary), "");
  assert.equal(boundaryNeedsWarn(block.boundary), false);
});

test("发起与完成配对后仍保留完成事件里的边界", () => {
  const blocks = buildBlocks([
    { seq: 1, node: "execute_action", type: "tool.requested", payload: { tool: "run_shell", arguments: { command: "pytest -q" } }, createdAt: "2026-10-22T00:00:00+00:00" },
    toolEvent({ tool: "run_shell", status: "ok", sandbox_backend: null, duration_ms: 12 }),
  ]);
  const block = blocks.find((item) => item.kind === "tool");
  assert.ok(block && block.kind === "tool");
  assert.equal(block.boundary, "host");
  assert.equal(block.durationMs, 12);
});

test("只有内核限额时角标说清强制了哪三项、没强制哪两项", () => {
  // 本机档位 job_only 是实测出来的：内存/进程数/进程树真被 Job Object 拦，文件与网络没有。
  // 只写「沙箱：local」会让用户以为整条边界都在，只写「宿主执行」又抹掉了真的那部分。
  const block = toolBlock({
    tool: "run_shell",
    status: "ok",
    sandbox_backend: "local",
    sandbox_enforcement: { limits_applied: true, boundary_strength: "job_only" },
  });
  assert.equal(block.boundary, "local");
  assert.deepEqual(block.boundaryFacts, { strength: "job_only", limitsApplied: true });
  const label = boundaryLabel(block.boundary, block.boundaryFacts);
  assert.match(label, /内核限额/);
  assert.match(label, /文件与网络未强制/);
  assert.doesNotMatch(label, /已隔离/);
  assert.equal(boundaryNeedsWarn(block.boundary, block.boundaryFacts), true);
});

test("系统真强制文件与网络时不再打警告色", () => {
  const block = toolBlock({
    tool: "run_shell",
    status: "ok",
    sandbox_backend: "local",
    sandbox_enforcement: { limits_applied: true, boundary_strength: "os_enforced" },
  });
  assert.match(boundaryLabel(block.boundary, block.boundaryFacts), /系统强制的文件\/网络边界/);
  assert.equal(boundaryNeedsWarn(block.boundary, block.boundaryFacts), false);
});

test("限额压根没生效时不许显示成普通宿主执行", () => {
  // 关档（sandbox 未启用）走的是后端 limits_not_applied：limits_applied=false。
  // 这一档和 job_only 都叫 local，但一个连内存上限都没在执行，文案必须分得开。
  const off = toolBlock({
    tool: "run_shell",
    status: "ok",
    sandbox_backend: "host",
    sandbox_enforcement: { limits_applied: false, boundary_strength: "" },
  });
  assert.equal(boundaryLabel(off.boundary, off.boundaryFacts), "宿主执行（未隔离，限额未生效）");
  assert.notEqual(boundaryLabel(off.boundary, off.boundaryFacts), "宿主执行（未隔离）");
});

test("经批准的单次越界要单独说出来，不能读成关档或普通宿主执行", () => {
  // #115：越界后 enforcement 只剩「限额全没生效」，与关档同形；区分它的是事件顶层的
  // sandbox_bypass=true。只说「宿主执行」会把一次显式授权读成机器本来就没隔离。
  const block = toolBlock({
    tool: "run_shell",
    status: "ok",
    sandbox_backend: null,
    sandbox_bypass: true,
    sandbox_bypass_reason: "经人工批准单次绕过沙箱（原档位 workspace），命令在宿主上执行",
    sandbox_enforcement: { limits_applied: false, boundary_strength: "" },
  });
  assert.equal(block.boundary, "host");
  assert.equal(block.boundaryFacts?.bypassed, true);
  assert.equal(boundaryLabel(block.boundary, block.boundaryFacts), "宿主执行（单次越界：已绕过沙箱）");
  assert.equal(boundaryNeedsWarn(block.boundary, block.boundaryFacts), true);
  assert.notEqual(
    boundaryLabel(block.boundary, block.boundaryFacts),
    boundaryLabel(block.boundary, { limitsApplied: false })
  );
});

test("没有越界事实的事件不许显示越界（缺键与 false 都不算）", () => {
  // 缺键 = 老任务没记过；false = 记了、确实没越界。两者都不能长成「已绕过沙箱」。
  const missing = toolBlock({ tool: "run_shell", status: "ok", sandbox_backend: null });
  assert.equal(missing.boundaryFacts?.bypassed, undefined);
  assert.doesNotMatch(boundaryLabel(missing.boundary, missing.boundaryFacts), /越界/);

  const recorded = toolBlock({
    tool: "run_shell",
    status: "ok",
    sandbox_backend: null,
    sandbox_bypass: false,
    sandbox_enforcement: { limits_applied: false, boundary_strength: "" },
  });
  assert.equal(recorded.boundaryFacts?.bypassed, undefined);
  assert.doesNotMatch(boundaryLabel(recorded.boundary, recorded.boundaryFacts), /越界/);
});

test("越界事件即使没带 enforcement 也仍然要说越界", () => {
  // 越界是独立事实，不该被 enforcement 的缺失吞掉。
  const block = toolBlock({ tool: "run_shell", status: "ok", sandbox_backend: null, sandbox_bypass: true });
  assert.deepEqual(block.boundaryFacts, { bypassed: true });
  assert.match(boundaryLabel(block.boundary, block.boundaryFacts), /单次越界/);
});

test("事件没带强制事实时禁止编造档位", () => {
  // 老任务只有 sandbox_backend 没有 sandbox_enforcement。
  // 拿默认值补一个 strength 就把"未知"渲染成了"内核限额"——那是本立项要消灭的那类谎。
  const block = toolBlock({ tool: "run_shell", status: "ok", sandbox_backend: "local" });
  assert.equal(block.boundaryFacts, undefined);
  assert.equal(boundaryLabel(block.boundary, block.boundaryFacts), "宿主执行（未隔离）");
  assert.doesNotMatch(boundaryLabel(block.boundary, block.boundaryFacts), /限额/);
});

test("配对后完成事件的强制事实要留在卡片上", () => {
  const blocks = buildBlocks([
    { seq: 1, node: "execute_action", type: "tool.requested", payload: { tool: "run_shell", arguments: { command: "python alloc.py 256" } }, createdAt: "2026-10-22T00:00:00+00:00" },
    toolEvent({
      tool: "run_shell",
      status: "failed",
      sandbox_backend: "local",
      duration_ms: 88,
      sandbox_enforcement: { limits_applied: true, boundary_strength: "job_only" },
    }),
  ]);
  const block = blocks.find((item) => item.kind === "tool");
  assert.ok(block && block.kind === "tool");
  assert.deepEqual(block.boundaryFacts, { strength: "job_only", limitsApplied: true });
  // 限额被内核拒的时候走的正是失败这一支，文案不能退化。
  assert.match(boundaryLabel(block.boundary, block.boundaryFacts), /内核限额/);
});

const modeChanged = (payload: Record<string, unknown>, seq = 5): AgentEvent => ({
  seq,
  node: "execute_action",
  type: "permission.mode_changed",
  payload,
  createdAt: "2026-10-22T00:00:05+00:00",
});

test("进入计划模式要在对话流里说清从此只读", () => {
  // 权限模式是整段会话的范围变化，比某一次工具结果重要：用户必须看得见
  // "从现在起不写文件、不执行命令"，否则会以为任务卡住了。
  const block = system(modeChanged({ tool: "enter_plan_mode", from: "default", to: "plan" }));
  assert.equal(block.title, "已进入计划模式");
  assert.equal(block.tone, "muted");
  assert.match(block.detail, /只读取证/);
  assert.doesNotMatch(block.detail, /计划：/);
});

test("退出计划模式带上批准的计划摘要", () => {
  const block = system(
    modeChanged({
      tool: "exit_plan_mode",
      from: "plan",
      to: "default",
      plan: "1. 改 core.py\n2. 跑 pytest -q",
    })
  );
  assert.equal(block.title, "已退出计划模式");
  assert.equal(block.tone, "accent");
  assert.match(block.detail, /恢复默认权限/);
  assert.match(block.detail, /计划：1\. 改 core\.py/);
});

test("缺方向字段的模式切换事件不许说成已退出", () => {
  // 老事件或别的运行时写的记录可能没有 to。把缺失读成"退出" = 把只读状态
  // 谎报成已恢复写，方向恰好反了。
  const block = system(modeChanged({ tool: "enter_plan_mode" }));
  assert.equal(block.title, "权限模式已变化");
  assert.doesNotMatch(`${block.title}${block.detail}`, /退出|恢复默认权限/);
});

const compacted = (payload: Record<string, unknown>, seq = 7): AgentEvent => ({
  seq,
  node: "execute_action",
  type: "context.compacted",
  payload,
  createdAt: "2026-10-22T00:00:07+00:00",
});

function boundaryBlock(payload: Record<string, unknown>) {
  const block = buildBlocks([compacted(payload)]).find((item) => item.kind === "boundary");
  assert.ok(block, "压缩边界必须成为时间线里的一张卡，否则用户看不出历史从哪里开始不是原文");
  if (block.kind !== "boundary") throw new Error("unexpected block");
  return block;
}

test("压缩边界把折叠位置与省下的 token 原样搬出来", () => {
  const block = boundaryBlock({
    folded: 12,
    covered: 40,
    active_context_tokens_before: 9000,
    active_context_tokens_after: 4200,
    tokens_saved: 4800,
    water_ratio: 0.83,
    context_window: 128000,
    triggered_by: "tokens:fold",
    micro: { clipped: 3, duplicates: 1, base64_removed: 0, chars_saved: 5200 },
  });
  assert.equal(block.folded, 12);
  assert.equal(block.covered, 40);
  assert.equal(block.tokensSaved, 4800);
  assert.equal(block.tokensBefore, 9000);
  assert.equal(block.tokensAfter, 4200);
  assert.equal(block.waterRatio, 0.83);
  assert.equal(block.window, 128000);
  assert.equal(block.triggeredBy, "tokens:fold");
  assert.equal(block.microClipped, 3);
  assert.equal(block.microCharsSaved, 5200);
  assert.equal(compactionTriggerLabel(block.triggeredBy), "按 token 水位 · 折叠了历史");
  assert.match(compactionHeadline(block.covered, block.folded, block.tokensSaved), /前 40 条/);
  assert.equal(compactionWaterLabel(block.waterRatio, block.window), "水位 83% · 窗口 128000");
});

test("水位推进但这次没折新内容，也要留卡并说清是哪一种", () => {
  // 后端在这种情况下仍然发 context.compacted（covered 变大了），
  // 只按 folded 判空就会把这条真实发生的边界丢掉。
  const block = boundaryBlock({ folded: 0, covered: 55, tokens_saved: 0, triggered_by: "chars:micro" });
  assert.equal(block.folded, 0);
  assert.equal(block.covered, 55);
  assert.match(compactionHeadline(block.covered, block.folded, block.tokensSaved), /水位推进/);
  assert.equal(compactionTriggerLabel(block.triggeredBy), "按字符预算 · 只做轻量裁剪");
});

test("触发依据只看冒号前的基底，认不出的原样露出", () => {
  // 真实链路实测到的取值是带阶段后缀的（tokens:fold / tokens:micro / chars:fold），
  // 按等值匹配去查表会把每一条真实事件都判成"未识别"。
  assert.equal(compactionTriggerLabel("tokens:micro"), "按 token 水位 · 只做轻量裁剪");
  assert.equal(compactionTriggerLabel("chars:fold"), "按字符预算 · 折叠了历史");
  assert.equal(compactionTriggerLabel("tokens"), "按 token 水位");
  assert.equal(compactionTriggerLabel("handoff"), "handoff");
  assert.equal(compactionTriggerLabel("handoff:fold"), "handoff:fold");
  assert.equal(compactionTriggerLabel(""), "");
});

test("缺字段的老压缩事件不许编水位百分比", () => {
  const block = boundaryBlock({ folded: 3 });
  assert.equal(block.covered, 0);
  assert.equal(block.window, 0);
  assert.equal(compactionWaterLabel(block.waterRatio, block.window), "");
  assert.match(compactionHeadline(block.covered, block.folded, block.tokensSaved), /这次折入 3 条/);
});

test("逐步的 context.measured 仍然不进对话流", () => {
  // 一次任务里它可能有几百条；若跟着建卡，时间线会被水位读数冲垮。
  const blocks = buildBlocks([
    { seq: 1, node: "execute_action", type: "context.measured", payload: { active_context_tokens_after: 1200 }, createdAt: "" },
    { seq: 2, node: "execute_action", type: "context.microcompacted", payload: { folded: 0, micro: { clipped: 2 } }, createdAt: "" },
  ]);
  assert.equal(blocks.filter((item) => item.kind === "boundary").length, 0);
  assert.equal(blocks.length, 0);
});

test("压缩卡留在它发生的位置，不挪到头或尾", () => {
  const blocks = buildBlocks([
    toolEvent({ tool: "run_shell", status: "ok", sandbox_backend: null }, 1),
    compacted({ folded: 4, covered: 18, tokens_saved: 900, triggered_by: "tokens" }, 2),
    toolEvent({ tool: "read_file", status: "ok" }, 3),
  ]);
  const kinds = blocks.map((item) => item.kind);
  assert.equal(kinds.join(","), "tool,boundary,tool", `压缩卡应留在两条调用之间：${kinds.join(",")}`);
});
