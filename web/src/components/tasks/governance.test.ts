import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeEvidence, sandboxBoundaryLabel, sandboxBoundaryNeedsWarn, type Governance } from "./api.ts";

/** 后端 turn.context 快照的真实形状（evidence.py 的 governance 块 + governance.py 的 turn_context）。 */
const snapshot = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  step: 0,
  node: "execute_action",
  role: "worker",
  model: "deepseek-chat",
  provider: "deepseek",
  tier: "full",
  permission_mode: "default",
  readonly: false,
  sandbox: {
    backend: "none",
    enabled: true,
    isolation: "host",
    allow_local: false,
    cpus: 1.0,
    memory: "512m",
    pids: 128,
    tmpfs: "128m",
    read_only_root: true,
    network: false,
    timeout_seconds: 120,
    writable_root: "workspace",
    denied: "Docker 沙箱不可用；如需开发机直连执行，请显式设置 BLANK_SANDBOX_ALLOW_LOCAL=1",
  },
  features: {
    model_routing: { on: false, source: "default" },
    threads: { on: true, source: "settings" },
    tiered_tools: { on: false, source: "env" },
  },
  ...over,
});

const governanceOf = (source: unknown): Governance => normalizeEvidence(source).governance;

test("老任务没有快照时一律空着，不拿当前配置补一份", () => {
  const empty = governanceOf({ governance: { snapshots: 0, first: null, changes: [], models: [], modes: [] } });
  assert.equal(empty.snapshots, 0);
  assert.equal(empty.sandbox, null, "没有当时的记录就不能显示边界，否则是拿今天的策略冒充执行那天");
  assert.deepEqual(empty.features, []);
  assert.deepEqual(empty.changes, []);
});

test("缺 governance 键同样为空，不抛错", () => {
  assert.equal(governanceOf({ status: "succeeded" }).snapshots, 0);
});

test("边界与上限来自当时那条快照", () => {
  const g = governanceOf({ governance: { snapshots: 1, first: snapshot(), changes: [], models: ["deepseek-chat"], modes: ["default"] } });
  assert.equal(g.sandbox?.backend, "none");
  assert.equal(g.sandbox?.network, false);
  assert.equal(g.sandbox?.readOnlyRoot, true);
  assert.match(g.sandbox?.denied ?? "", /ALLOW_LOCAL/);
  assert.match(g.sandbox?.limits ?? "", /memory 512m/);
  assert.match(g.sandbox?.limits ?? "", /timeout_seconds 120/);
});

test("特性标记只留开着的与被环境变量强制关掉的", () => {
  const names = governanceOf({ governance: { snapshots: 1, first: snapshot(), changes: [], models: [], modes: [] } }).features.map(
    (item) => `${item.name}:${item.source}`,
  );
  assert.deepEqual(names, ["threads:settings", "tiered_tools:env"]);
});

test("中途改策略逐条留痕，字段按后端口径投影", () => {
  const g = governanceOf({
    governance: {
      snapshots: 2,
      first: snapshot(),
      changes: [{ seq: 9, step: 3, node: "call_model", model: "deepseek-reasoner", tier: "lean", permission_mode: "plan" }],
      models: ["deepseek-chat", "deepseek-reasoner"],
      modes: ["default", "plan"],
    },
  });
  assert.equal(g.changes.length, 1);
  assert.equal(g.changes[0].step, 3);
  assert.equal(g.changes[0].mode, "plan");
  assert.equal(g.changes[0].tier, "lean");
  assert.deepEqual(g.modes, ["default", "plan"]);
});

test("包在 evidence 外层也要取到", () => {
  const g = governanceOf({ evidence: { governance: { snapshots: 1, first: snapshot(), changes: [], models: [], modes: ["plan"] } } });
  assert.deepEqual(g.modes, ["plan"]);
  assert.equal(g.snapshots, 1);
});

test("沙箱关掉时不许显示成「有一种沙箱叫 local」", () => {
  // 这两条的形状抄自 :8061 真实返回的证据包（BLANK_SANDBOX_ENABLED=0）。
  const g = governanceOf({
    governance: {
      snapshots: 1,
      first: snapshot({ sandbox: { backend: "local", enabled: false, isolation: "host", network: false, read_only_root: true, cpus: 1.0, denied: "" } }),
      changes: [],
      models: [],
      modes: [],
    },
  });
  assert.equal(g.sandbox?.enabled, false);
  assert.equal(sandboxBoundaryLabel(g.sandbox), "沙箱未启用 · 宿主直连（不承诺隔离） · 断网 · 根目录只读");
  assert.doesNotMatch(sandboxBoundaryLabel(g.sandbox), /local/);
  assert.equal(sandboxBoundaryNeedsWarn(g.sandbox), true);
});

test("容器隔离且开网时照样报警", () => {
  const g = governanceOf({
    governance: {
      snapshots: 1,
      first: snapshot({ sandbox: { backend: "docker", enabled: true, isolation: "container", network: true, read_only_root: true } }),
      changes: [],
      models: [],
      modes: [],
    },
  });
  assert.equal(sandboxBoundaryLabel(g.sandbox), "docker · 容器隔离 · 可联网 · 根目录只读");
  assert.equal(sandboxBoundaryNeedsWarn(g.sandbox), true);
});

test("没有快照就没有边界文案，不给默认值", () => {
  assert.equal(sandboxBoundaryLabel(null), "暂无");
  assert.equal(sandboxBoundaryNeedsWarn(null), true);
});

/** 把快照里的 sandbox 换成"这台机器实测到的形状"：后端 local + 档位 job_only + enforced 逐字段。 */
const withSandbox = (sandbox: Record<string, unknown>) =>
  governanceOf({
    governance: { snapshots: 1, first: snapshot({ sandbox }), changes: [], models: [], modes: [] },
  });

test("内核只强制三项时，其余限额逐条标出「未强制」", () => {
  // 本机档位（M0 实测）：内存/进程数/进程树由 Job Object 拦，CPU、磁盘、文件、网络谁都没管。
  // 以前 cpus 1.0 就静静躺在那儿，读者以为有人在执行它。
  const g = withSandbox({
    backend: "local",
    enabled: true,
    isolation: "host",
    network: false,
    read_only_root: true,
    cpus: 1.0,
    memory: "512m",
    pids: 128,
    tmpfs: "128m",
    timeout_seconds: 120,
    boundary_strength: "job_only",
    enforcement_source: "measured_job_object",
    enforced: { memory: true, pids: true, tree_kill: true, cpu: false, disk: false, network: false, filesystem: false },
  });
  assert.match(g.sandbox?.limits ?? "", /cpus 1（未强制）/);
  assert.match(g.sandbox?.limits ?? "", /tmpfs 128m（未强制）/);
  assert.match(g.sandbox?.limits ?? "", /memory 512m ·/);
  assert.doesNotMatch(g.sandbox?.limits ?? "", /memory 512m（未强制）/);
  // 超时由我们自己的计时器执行，不属于"内核强制/未强制"这一档，不许标它。
  assert.match(g.sandbox?.limits ?? "", /timeout_seconds 120$/);
  assert.match(sandboxBoundaryLabel(g.sandbox), /只有内存\/进程数\/进程树被内核强制/);
  assert.equal(sandboxBoundaryNeedsWarn(g.sandbox), true, "文件与网络没人拦，这行必须是警告色");
});

test("开档但后端是 local 时，边界行不许把代号印成一种沙箱", () => {
  // 关档那一条早就断过 /local/；开档走的是同一个函数，漏了就会显示「local · 宿主直连…」，
  // 用户读到的是"有一种沙箱叫 local"。这条把开档这一半也钉住。
  const g = withSandbox({
    backend: "local",
    enabled: true,
    isolation: "host",
    network: false,
    read_only_root: true,
    memory: "512m",
    boundary_strength: "job_only",
    enforcement_source: "measured_job_object",
    enforced: { memory: true, pids: true, tree_kill: true, cpu: false, disk: false, network: false, filesystem: false },
  });
  const label = sandboxBoundaryLabel(g.sandbox);
  assert.doesNotMatch(label, /local/, "代号不该出现在给人看的文案里");
  assert.match(label, /^无容器（进程在本机） · 宿主直连（不承诺隔离）/);
});

test("老快照没写 enforced 时逐项按未强制处理", () => {
  // 缺键＝没证据。把"没记录"读成"在执行"就是展示值冒充生效值，正是本立项要消灭的那类谎。
  const g = withSandbox({
    backend: "local",
    enabled: true,
    isolation: "host",
    network: false,
    read_only_root: true,
    cpus: 1.0,
    memory: "512m",
    pids: 128,
  });
  assert.match(g.sandbox?.limits ?? "", /memory 512m（未强制）/);
  assert.match(g.sandbox?.limits ?? "", /pids 128（未强制）/);
  assert.equal(g.sandbox?.boundaryStrength, "");
});

test("系统真强制时不标未强制，也不再报警", () => {
  const g = withSandbox({
    backend: "local",
    enabled: true,
    isolation: "host",
    network: false,
    read_only_root: true,
    cpus: 1.0,
    memory: "512m",
    boundary_strength: "os_enforced",
    enforcement_source: "measured_appcontainer_or_restricted_token",
    enforced: { memory: true, pids: true, tree_kill: true, cpu: false, disk: false, network: true, filesystem: true },
  });
  // 即使档位是 os_enforced，CPU 本期没人执行（后端 cpu_enforced 恒为 False）——标记照样得跟着真值。
  assert.doesNotMatch(g.sandbox?.limits ?? "", /memory 512m（未强制）/);
  assert.match(g.sandbox?.limits ?? "", /cpus 1（未强制）/);
  assert.match(sandboxBoundaryLabel(g.sandbox), /文件与网络边界由系统强制/);
  assert.equal(sandboxBoundaryNeedsWarn(g.sandbox), false);
});

test("容器那一路的 True 来自命令行参数，必须写清未在本机实测", () => {
  const g = withSandbox({
    backend: "docker",
    enabled: true,
    isolation: "container",
    network: false,
    read_only_root: true,
    memory: "512m",
    enforcement_source: "docker_flags",
    enforced: { memory: true, pids: true, cpu: true },
  });
  assert.match(sandboxBoundaryLabel(g.sandbox), /限额由容器参数提交，未在本机实测/);
  assert.equal(sandboxBoundaryNeedsWarn(g.sandbox), false);
});

test("关档时后端名是 local 也算不出任何强制", () => {
  // 与后端 limits_enabled=False 同口径：sandbox 关了就不许有任何一项不带「未强制」。
  const g = withSandbox({
    backend: "local",
    enabled: false,
    isolation: "host",
    network: false,
    read_only_root: true,
    memory: "512m",
    pids: 128,
    boundary_strength: "job_only",
    enforcement_source: "sandbox_disabled",
    enforced: { memory: false, pids: false, tree_kill: false, cpu: false, disk: false, network: false, filesystem: false },
  });
  assert.match(g.sandbox?.limits ?? "", /memory 512m（未强制）/);
  assert.match(sandboxBoundaryLabel(g.sandbox), /^沙箱未启用/);
  assert.equal(sandboxBoundaryNeedsWarn(g.sandbox), true);
  // 关档时探测档位仍然是 job_only（机器确实建得出 Job），但那句说的是"这台机器拦得住什么"，
  // 不是"这次执行被拦了什么"。真页面上这一行曾紧跟「沙箱未启用」写成「只有内存…被内核强制」，
  // 单元测试放过了一句谎，是浏览器点验抓出来的。
  const label = sandboxBoundaryLabel(g.sandbox);
  assert.doesNotMatch(label, /被内核强制/);
  assert.match(label, /本次未强制任何限额/);
});

