import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeEvidence, guardianFromPayload, type Boundaries, type GuardianHistory } from "./api.ts";
import { buildBlocks, type Block } from "./timeline.ts";
import {
  boundariesNeedWarn,
  enforcedFlagLabel,
  guardianDecisionLabel,
  guardianHeadline,
  guardianReviewLabel,
  guardianWindowLabel,
  mcpBoundaryLabel,
  missingPathLabel,
  networkGuardLabel,
  permissionEntryLabel,
  permissionProfileTypeLabel,
  sandboxModesLabel,
  sandboxTierLabel,
  shellGrantLabel,
  toolCallBoundaryLabel,
} from "./status.ts";

/** 后端 evidence.boundaries 的真实形状（ev-41c2847 上抓下来的实物，字段名逐字照抄）。 */
const realBoundaries = (): Record<string, unknown> => ({
  sandbox: {
    tier: "off",
    requested: "workspace",
    backend: "none",
    strength: "none",
    degraded: true,
    filesystem_enforced: false,
    network_enforced: false,
    notice: "部署策略关闭了沙箱：本任务无隔离，命令直接以宿主完整权限执行",
  },
  mcp: { boundary: "none", note: "没有已连接的 MCP 服务端", servers: [], calls: 0 },
  tool_calls: { blocked: 0, mcp_host_process: 0, process: 0, in_process: 16, unattributed: 0 },
  sandbox_modes: ["off"],
  permission_profile: {
    type: "managed",
    enforced_by: "policy",
    missing_path_behavior: "skip",
    file_system: {
      type: "restricted",
      entries: [
        { path: "<workspace:ws>", access: "write", enforced_by: "policy" },
        { path: "*", access: "deny", enforced_by: "policy" },
      ],
    },
    network: { requested: "none", enforced_by: "policy", command_gate: "deny" },
  },
});

/** 档位与要求一致、且系统真在强制的那一种理想部署：用来当负对照的底。 */
const osEnforcedSandbox = () => ({
  tier: "workspace",
  requested: "workspace",
  backend: "windows_job_object",
  strength: "os_enforced",
  degraded: false,
  filesystem_enforced: true,
  network_enforced: true,
  notice: "",
});

const realGuardian = (): Record<string, unknown> => ({
  decisions: [
    {
      seq: 64,
      run_id: "task:41c82847-a48f-45e1-a29e-38db97a16d78:attempt:1",
      tool: "run_shell",
      approval_key: "37a558073ca543f682ad94880a71dafd224ab48ff3c01cd986394d2333ba753d",
      verdict: "allow",
      reasons: ["dir /b 只列出当前目录文件名，不修改或删除任何内容"],
      model: "deepseek-flash",
      latency_ms: 1043,
      timed_out: false,
      error: "",
      outcome: "asked",
    },
  ],
  auto_approvals: [],
  windows: [
    {
      seq: 133,
      window_number: 4,
      items_reviewed: 22,
      items_in_model_context: 9,
      reviewed_chars: 10808,
      reviews: 2,
      in_effect: { window_tokens: 3000, approval_mode: "default", guardian_mode: "veto" },
    },
  ],
  windows_unrecorded: 0,
});

const boundariesOf = (source: unknown): Boundaries => normalizeEvidence(source).boundaries;
const guardianOf = (source: unknown): GuardianHistory => normalizeEvidence(source).guardian_history;

test("后端没给 boundaries 键时标为未记录，不许当成就没人碰过边界", () => {
  const b = boundariesOf({ status: "succeeded" });
  assert.equal(b.recorded, false);
  assert.equal(b.tier, "");
  assert.equal(b.notice, "");
  assert.equal(b.toolCalls.inProcess, null, "缺键必须是 null，写成 0 就是把「没记」说成「零次」");
  assert.equal(b.profile.type, "");
  assert.deepEqual(b.profile.entries, []);
});

test("真实物投影：档位/降级/强制层逐字段等于接口值", () => {
  const b = boundariesOf({ boundaries: realBoundaries() });
  assert.equal(b.recorded, true);
  assert.equal(b.tier, "off");
  assert.equal(b.requested, "workspace");
  assert.equal(b.strength, "none");
  assert.equal(b.degraded, true);
  assert.equal(b.filesystemEnforced, false);
  assert.equal(b.notice, "部署策略关闭了沙箱：本任务无隔离，命令直接以宿主完整权限执行");
  assert.equal(b.toolCalls.inProcess, 16);
  assert.deepEqual(b.sandboxModes, ["off"]);
  assert.equal(b.profile.entries.length, 2);
  assert.equal(b.profile.entries[0].path, "<workspace:ws>");
  assert.equal(b.profile.entries[1].access, "deny");
  assert.equal(b.profile.network?.commandGate, "deny");
});

test("档位文案要说清「要求什么、实际在哪、降级没有」", () => {
  const b = boundariesOf({ boundaries: realBoundaries() });
  const label = sandboxTierLabel(b);
  assert.match(label, /off/);
  assert.match(label, /要求 workspace/);
  assert.match(label, /已降级/);
  assert.match(label, /无内核强制/, "off 档必须紧跟「没有任何内核层在管」这半句，否则后面的数字像有人在执行");
  assert.equal(boundariesNeedWarn(b), true);
});

test("档位与要求一致、且系统真强制时不报降级、不亮警告", () => {
  const b = boundariesOf({
    boundaries: {
      ...realBoundaries(),
      sandbox: osEnforcedSandbox(),
    },
  });
  const label = sandboxTierLabel(b);
  assert.doesNotMatch(label, /要求 workspace/, "档位没变时不必重复要求");
  assert.doesNotMatch(label, /已降级/);
  assert.match(label, /文件与网络由系统强制/);
  assert.equal(boundariesNeedWarn(b), false);
});

test("逐条授权：谁在保证这条，必须写在行尾", () => {
  const b = boundariesOf({ boundaries: realBoundaries() });
  const lines = b.profile.entries.map(permissionEntryLabel);
  assert.deepEqual(lines, [
    "<workspace:ws> · 可写 · 策略闸门（执行前判定，不是系统拒）",
    "* · 拒绝 · 策略闸门（执行前判定，不是系统拒）",
  ]);
});

test("enforced_by=none 那一档要直说没人管，且整块亮警告", () => {
  const b = boundariesOf({
    boundaries: {
      ...realBoundaries(),
      permission_profile: {
        type: "unrestricted",
        enforced_by: "none",
        missing_path_behavior: "not_applicable",
        file_system: { type: "unrestricted", entries: [{ path: "*", access: "write", enforced_by: "none" }] },
        network: { requested: "open", enforced_by: "none", command_gate: "allow" },
      },
    },
  });
  assert.equal(permissionEntryLabel(b.profile.entries[0]), "* · 可写 · 无人强制");
  assert.equal(boundariesNeedWarn(b), true);
  assert.equal(
    networkGuardLabel(b.profile.network),
    "要求可通网 · 无人强制 · 命令闸门放行",
    "命令闸门那一格要说：通不通网是它在管，不是内核"
  );
});

test("网络闸门整串钉死：要求、谁在强制、命令闸门三格一个不许省", () => {
  const b = boundariesOf({ boundaries: realBoundaries() });
  assert.equal(
    networkGuardLabel(b.profile.network),
    "要求不通网 · 策略闸门（执行前判定，不是系统拒） · 命令闸门拒绝"
  );
});

test("系统强制了文件、却有一条写入没人强制时，警告只能由那条 ACE 触发", () => {
  const withAce = (enforcedBy: string) =>
    boundariesOf({
      boundaries: {
        ...realBoundaries(),
        sandbox: osEnforcedSandbox(),
        permission_profile: {
          type: "managed",
          enforced_by: "os",
          missing_path_behavior: "skip",
          file_system: { type: "restricted", entries: [{ path: "<tmp>", access: "write", enforced_by: enforcedBy }] },
          network: { requested: "none", enforced_by: "os", command_gate: "deny" },
        },
      },
    });
  const none = withAce("none");
  assert.equal(boundariesNeedWarn(none), true, "这一档只有那条 ACE 能触发警告：档位、降级、强制层全是好的");
  assert.match(permissionEntryLabel(none.profile.entries[0]), /无人强制/);
  // 负对照：只换 enforced_by 这一格，警告必须灭掉——否则上面那个 true 是白给的。
  assert.equal(boundariesNeedWarn(withAce("os")), false);
});

test("不存在的目标怎么算，是闸门的真实语义，不是抄来的常量", () => {
  assert.equal(missingPathLabel("skip"), "不存在的目标不参与越界判定（skip）");
  assert.match(missingPathLabel("not_applicable"), /未开「仅限工作区」/);
  assert.equal(missingPathLabel(""), "", "没记就不显示这行，不编一个行为出来");
  assert.equal(missingPathLabel("whatever"), "whatever", "认不出的取值原样露出来，不套中文标签");
});

test("老任务没有权限快照时念后端那句说明，不按档位反推一份假授权", () => {
  const b = boundariesOf({
    boundaries: {
      ...realBoundaries(),
      permission_profile: { type: "unrecorded", note: "该任务没有权限快照（M4 之前的老任务），不按档位反推" },
    },
  });
  assert.equal(b.profile.type, "unrecorded");
  assert.deepEqual(b.profile.entries, []);
  assert.equal(b.profile.note, "该任务没有权限快照（M4 之前的老任务），不按档位反推");
  assert.equal(missingPathLabel(b.profile.missingPathBehavior), "");
});

test("调用归账：五类分开数，未归属单独说，不并进进程内", () => {
  const label = toolCallBoundaryLabel(boundariesOf({ boundaries: realBoundaries() }));
  assert.match(label, /进程内 16/);
  assert.match(label, /起进程 0/);
  assert.match(label, /MCP 宿主 0/);
  assert.match(label, /被档位拦 0/);
  assert.match(label, /未归属 0/);
  assert.doesNotMatch(label, /没记边界/, "一条都没漏时不该出现「漏记」这句");
});

test("老任务的调用落在未归属里：数字要露出来，话要说清不猜", () => {
  const b = boundariesOf({
    boundaries: { ...realBoundaries(), tool_calls: { blocked: 0, mcp_host_process: 0, process: 3, in_process: 0, unattributed: 9 } },
  });
  const label = toolCallBoundaryLabel(b);
  assert.match(label, /未归属 9/);
  assert.match(label, /当时没记边界/);
  assert.equal(b.toolCalls.unattributed, 9);
});

test("tool_calls 整块缺失时逐项按未记，绝不补 0", () => {
  const b = boundariesOf({ boundaries: { sandbox: {}, mcp: {}, sandbox_modes: [], permission_profile: {} } });
  assert.equal(b.toolCalls.inProcess, null);
  assert.equal(b.toolCalls.unattributed, null);
  // 五个键逐个钉死：少钉一个，那一个就能被偷偷补成 0。
  assert.deepEqual(b.toolCalls, { blocked: null, mcpHostProcess: null, process: null, inProcess: null, unattributed: null });
  const label = toolCallBoundaryLabel(b);
  assert.match(label, /进程内 未记/);
  assert.match(label, /被档位拦 未记/);
  assert.match(label, /MCP 宿主 未记/);
  assert.match(label, /起进程 未记/);
  assert.match(label, /未归属 未记/);
});

test("MCP 宿主进程这条事实要单独说出来，含服务数与调用数", () => {
  const b = boundariesOf({
    boundaries: {
      ...realBoundaries(),
      mcp: { boundary: "host", note: "MCP 服务端在宿主进程里跑，不进任务沙箱", servers: ["browser-use", "builtin"], calls: 4 },
    },
  });
  assert.equal(b.mcp.boundary, "host");
  assert.deepEqual(b.mcp.servers, ["browser-use", "builtin"]);
  assert.equal(b.mcp.calls, 4);
  assert.equal(b.mcp.note, "MCP 服务端在宿主进程里跑，不进任务沙箱");
});

test("审阅轨迹缺键=这台后端还没记过，与记过但一次都没审是两个状态", () => {
  const missing = guardianOf({ status: "succeeded" });
  assert.equal(missing.recorded, false);
  assert.deepEqual(missing.decisions, []);
  assert.equal(missing.windowsUnrecorded, null, "缺键不许读成 0");

  const empty = guardianOf({ guardian_history: { decisions: [], auto_approvals: [], windows: [], windows_unrecorded: 0 } });
  assert.equal(empty.recorded, true);
  assert.equal(empty.windowsUnrecorded, 0);
});

test("真实物投影：判定逐字段照抄，窗口六键一个不许丢", () => {
  const g = guardianOf({ guardian_history: realGuardian() });
  assert.equal(g.decisions.length, 1);
  assert.equal(g.decisions[0].seq, 64);
  assert.equal(g.decisions[0].outcome, "asked");
  assert.equal(g.decisions[0].latencyMs, 1043);
  assert.deepEqual(g.decisions[0].reasons, ["dir /b 只列出当前目录文件名，不修改或删除任何内容"]);
  assert.equal(g.windows[0].itemsReviewed, 22);
  assert.equal(g.windows[0].itemsInModelContext, 9);
  assert.equal(g.windows[0].reviewedChars, 10808);
  assert.equal(g.windows[0].reviews, 2);
  assert.equal(g.windows[0].inEffect.guardian_mode, "veto");
});

test("判定行念出出生轮的可读名，内部 id 与审批指纹不上页面", () => {
  const g = guardianOf({ guardian_history: realGuardian() });
  const line = guardianDecisionLabel(g.decisions[0]);
  assert.match(line, /第 64 条/);
  assert.match(line, /run_shell/);
  assert.match(line, /放行/);
  assert.match(line, /去向 交给人批准/);
  assert.match(line, /主线程#1/, "attempt:1 这形状要念成主线程#1");
  assert.match(line, /deepseek-flash 1,043ms/);
  for (const raw of ["attempt:", "task:", "37a558073ca543f", "dir /b"]) {
    assert.ok(!line.includes(raw), `${raw} 是内部标识或命令原文，不该出现在这行里`);
  }
});

test("老事件的 outcome 缺键时不编去向，新事件有键就必须念出来", () => {
  const old = guardianOf({
    guardian_history: { decisions: [{ seq: 19, run_id: "task:x:attempt:1", tool: "run_shell", verdict: "allow" }], windows: [] },
  });
  assert.equal(old.decisions[0].outcome, null);
  assert.doesNotMatch(guardianDecisionLabel(old.decisions[0]), /去向/);

  const timedOut = guardianOf({
    guardian_history: {
      decisions: [{ seq: 20, run_id: "task:x:attempt:1", tool: "write_file", verdict: "deny", outcome: "blocked", timed_out: true }],
      windows: [],
    },
  });
  const line = guardianDecisionLabel(timedOut.decisions[0]);
  assert.match(line, /否决/);
  assert.match(line, /去向 审查直接拒/);
  assert.match(line, /超时/);
});

test("压缩行把审阅侧与模型侧并排念，这才是「丢的是模型的历史」", () => {
  const g = guardianOf({ guardian_history: realGuardian() });
  const line = guardianWindowLabel(g.windows[0]);
  assert.match(line, /第 4 窗/);
  assert.match(line, /审阅侧 22 条 · 模型侧 9 条/);
  assert.match(line, /判定 2 条随账/);
  assert.match(line, /10,808 字/);
  assert.match(line, /审查=veto 审批=default 窗口 3,000/);
});

test("窗口计数缺键念「未记」，不印 0", () => {
  const g = guardianOf({ guardian_history: { decisions: [], windows: [{ seq: 21, window_number: 1 }] } });
  const line = guardianWindowLabel(g.windows[0]);
  assert.match(line, /第 1 窗/);
  assert.match(line, /审阅侧 未记/);
  assert.match(line, /判定 未记/);
  assert.doesNotMatch(line, /0 条/);
});

test("轨迹汇总：判定条数、带上判定的窗数、未记轨迹的压缩数分开念", () => {
  const g = guardianOf({
    guardian_history: {
      decisions: [{ seq: 1, verdict: "allow" }, { seq: 2, verdict: "deny" }],
      auto_approvals: [{ seq: 3, approval_key: "k" }],
      windows: [
        { seq: 10, window_number: 1, reviews: 0 },
        { seq: 11, window_number: 2, reviews: 2 },
      ],
      windows_unrecorded: 1,
    },
  });
  const head = guardianHeadline(g);
  assert.match(head, /判定 2 条/);
  assert.match(head, /代批 1 次/);
  assert.match(head, /压缩 2 窗（其中 1 窗带上判定）/);
  assert.match(head, /另有 1 次压缩没记轨迹/);
  assert.equal(guardianHeadline(guardianOf({})), "", "后端没给这块时不许编一句汇总");
});

test("审批卡上的审查意见：缺键、关着、说过话是三种状态", () => {
  const source = (over: Record<string, unknown>) => normalizeEvidence({ pending_approvals: [over] }).pendingApprovals[0];
  assert.equal(guardianReviewLabel(source({ tool: "run_shell" }).guardian), "", "老任务的卡上没有这个键，不显示");
  assert.match(guardianReviewLabel(source({ tool: "run_shell", guardian: null }).guardian), /独立审查未开启/);
  const line = guardianReviewLabel(
    source({ tool: "run_shell", guardian: { verdict: "deny", reasons: ["会覆盖密钥"], model: "m", latency_ms: 20, timed_out: false } }).guardian,
  );
  assert.match(line, /审查员：否决/);
  assert.match(line, /会覆盖密钥/);
});

// --- 面板上还剩下的那几个判定：强制层、权限快照来历、MCP、档位变化、shell ---

test("强制层三态：没这一格、记了没强制、真强制", () => {
  assert.equal(enforcedFlagLabel(null), "未记");
  assert.equal(enforcedFlagLabel(false), "未强制", "off 档要念成「没强制」，不是「没这一说」");
  assert.equal(enforcedFlagLabel(true), "系统强制");
});

test("权限快照的来历要分开念，unrestricted 不是「没配」", () => {
  const typeOf = (over: Record<string, unknown>) => boundariesOf({ boundaries: { permission_profile: over } }).profile;
  assert.match(permissionProfileTypeLabel(typeOf({ type: "managed" })), /受管权限/);
  assert.match(permissionProfileTypeLabel(typeOf({ type: "unrestricted" })), /不受限/);
  assert.match(permissionProfileTypeLabel(typeOf({ type: "unrecorded" })), /无权限快照/);
  assert.match(permissionProfileTypeLabel(typeOf({})), /未记/, "老服务没给 type：要说没记，不许默认成受管");
  assert.match(permissionProfileTypeLabel(typeOf({ type: "whatever" })), /whatever/, "认不出的取值原样露出来");
});

test("MCP 那一行含边界、服务数与调用数；调用数缺键念未记", () => {
  const none = mcpBoundaryLabel(boundariesOf({ boundaries: realBoundaries() }));
  assert.match(none, /MCP 边界 none/);
  assert.match(none, /服务端 0 个/);
  assert.match(none, /调用 0 次/);
  const withServers = mcpBoundaryLabel(
    boundariesOf({ boundaries: { mcp: { boundary: "host", servers: ["browser-use", "builtin"], calls: 4, note: "宿主进程里跑" } } }),
  );
  assert.match(withServers, /browser-use、builtin/);
  assert.match(withServers, /调用 4 次/);
  assert.match(withServers, /宿主进程里跑/);
  assert.match(
    mcpBoundaryLabel(boundariesOf({ boundaries: { mcp: { boundary: "host", servers: ["x"] } } })),
    /调用 未记/,
    "calls 缺键不许念 0 次",
  );
  assert.equal(mcpBoundaryLabel(boundariesOf({ status: "succeeded" })), "", "后端没给这块时不铺一串空值");
});

test("档位变化：只念真出现过的档位，空列表不念成 off", () => {
  assert.equal(sandboxModesLabel(boundariesOf({ boundaries: realBoundaries() }).sandboxModes), "off");
  assert.equal(sandboxModesLabel(["workspace", "off"]), "workspace → off");
  assert.equal(sandboxModesLabel(boundariesOf({ status: "succeeded" }).sandboxModes), "");
});

test("shell 这一格：载荷没给 ≠ shell 关着", () => {
  assert.equal(shellGrantLabel(null), "", "缺格时整行不显示，显示出来就是编的");
  assert.match(shellGrantLabel(true), /允许/);
  assert.match(shellGrantLabel(false), /禁止/);
});

// --- 审批卡上的审查意见要从事件载荷一路传到 block ---

const approvalBlock = (
  payload: Record<string, unknown>,
  type = "approval.required",
): Extract<Block, { kind: "approval" }> => {
  const block = buildBlocks([
    { seq: 65, node: "run_tools", type, payload, createdAt: "2026-10-25T00:00:00+00:00" },
  ]).find((item) => item.kind === "approval");
  assert.ok(block, "审批事件要在时间线里建卡");
  if (block.kind !== "approval") throw new Error("unexpected block");
  return block;
};

test("审批卡的 guardian 三态一路传到 block：老卡、关着、说过话不许塌成一个", () => {
  assert.equal(approvalBlock({ tool: "run_shell" }).guardian, undefined, "键不在=这张卡早于审查进载荷");
  const off = approvalBlock({ tool: "run_shell", guardian: null });
  assert.equal(off.guardian, null);
  assert.match(guardianReviewLabel(off.guardian), /独立审查未开启/);
  const on = approvalBlock({
    tool: "run_shell",
    guardian: { verdict: "allow", reasons: ["只列目录"], model: "deepseek-flash", latency_ms: 1043, timed_out: false },
  });
  assert.equal(on.guardian?.verdict, "allow");
  assert.equal(on.guardian?.latencyMs, 1043);
  assert.match(guardianReviewLabel(on.guardian), /审查员：放行/);
});

test("task.waiting_approval 建卡时审查意见藏在 interrupt 里，也要读到", () => {
  const block = approvalBlock(
    { status: "waiting_approval", interrupt: { tool: "run_shell", guardian: { verdict: "deny", reasons: ["会覆盖密钥"] } } },
    "task.waiting_approval",
  );
  assert.match(guardianReviewLabel(block.guardian), /审查员：否决/);
  assert.match(guardianReviewLabel(block.guardian), /会覆盖密钥/);
});

test("载荷直接读时同样守住三态（面板与时间线共用一个读法）", () => {
  assert.equal(guardianFromPayload({ tool: "run_shell" }), undefined);
  assert.equal(guardianFromPayload({ guardian: null }), null);
  assert.equal(guardianFromPayload({ guardian: { verdict: "abstain" } })?.verdict, "abstain");
});
