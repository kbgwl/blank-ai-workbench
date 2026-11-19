import assert from "node:assert/strict";
import { test } from "node:test";

import { goalStatusLabel, statusBarChips, statusBarVisible, type GoalStatusBarData } from "./goal-status.ts";

const base = (overrides: Partial<GoalStatusBarData> = {}): GoalStatusBarData => ({
  goal: "把登录页按钮改成蓝色",
  goalStatus: "executing",
  backgroundCount: 0,
  serviceOnline: true,
  ...overrides,
});

test("状态条只留两格：目标、后台任务数", () => {
  const chips = statusBarChips(base({ backgroundCount: 2 }));
  assert.deepEqual(
    chips.map((item) => item.key),
    ["goal", "background"],
  );
  assert.equal(chips[0].value, "把登录页按钮改成蓝色 · 执行中");
  assert.equal(chips[1].value, "2 个任务");
});

test("场景 / 模型 / 模式不再出现在状态条上——它们在输入框工具条里各有选择器", () => {
  // 这一条是防回归：曾经顶部有一排「场景 开发 / 模型 xxx / 模式 辅助」，
  // 和底部三个选择器完全重复，用户第一反应是"模式 辅助 是什么东西"。
  const keys = statusBarChips(base({ backgroundCount: 1 })).map((item) => item.key);
  for (const retired of ["scenario", "model", "automation"]) {
    assert.equal(keys.includes(retired), false, `${retired} 不该再出现在状态条上`);
  }
});

test("没有目标、没有后台任务时不显示这两格", () => {
  const chips = statusBarChips(base({ goal: "", goalStatus: "", backgroundCount: 0 }));
  assert.deepEqual(chips, []);
});

test("整条是否渲染：目标 / 后台 / 断线三选一才显示", () => {
  assert.equal(statusBarVisible(base()), true);
  assert.equal(statusBarVisible(base({ backgroundCount: 3 })), true);
  assert.equal(statusBarVisible(base({ goal: "", goalStatus: "", serviceOnline: false })), true);
  // 新对话页：没目标、没后台、服务正常 → 整条不出现
  assert.equal(statusBarVisible(base({ goal: "", goalStatus: "" })), false);
});

test("等审批 / 失败 / 执行中用不同语义色", () => {
  const waiting = statusBarChips(base({ goalStatus: "waiting_approval" })).find((item) => item.key === "goal");
  assert.equal(waiting?.tone, "warn");
  assert.match(waiting?.value ?? "", /等你批准/);
  const failed = statusBarChips(base({ goalStatus: "failed" })).find((item) => item.key === "goal");
  assert.equal(failed?.tone, "danger");
  const running = statusBarChips(base({ goalStatus: "executing" })).find((item) => item.key === "goal");
  assert.equal(running?.tone, "brand");
});

test("未知状态回落而不是显示原始英文键", () => {
  assert.equal(goalStatusLabel("bogus"), "");
  assert.equal(goalStatusLabel("repairing"), "修复中");
});

test("后台那一格说标题，不说数量——「后台 4 个任务」看不出是哪一个", () => {
  const bg = statusBarChips(
    base({ backgroundCount: 3, backgroundLabel: "雷罡", backgroundStatus: "running" }),
  ).find((item) => item.key === "background");
  assert.equal(bg?.value, "3 个 · 雷罡");
  // 在跑就是普通信息，不给颜色。
  assert.equal(bg?.tone, "plain");
});

test("后台任务卡在审批上时用一个小橙点，不整格变橙", () => {
  const bg = statusBarChips(
    base({ backgroundCount: 1, backgroundLabel: "雷罡", backgroundStatus: "waiting_approval" }),
  ).find((item) => item.key === "background");
  // 同一行两个「等你批准」会让人不知道该点哪个：状态由点说，文字只说哪一条。
  assert.equal(bg?.value, "雷罡");
  // 整格变橙会和目标那一格撞成一模一样的两条药丸，所以底色仍是普通灰。
  assert.equal(bg?.tone, "plain");
  assert.equal(bg?.dot, "warn");
});

test("没有标题时退回数量说法：状态条不能变成一格空白", () => {
  const bg = statusBarChips(base({ backgroundCount: 2, backgroundLabel: "", backgroundStatus: "" })).find(
    (item) => item.key === "background",
  );
  assert.equal(bg?.value, "2 个任务");
});
