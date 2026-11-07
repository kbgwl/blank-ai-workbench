import assert from "node:assert/strict";
import { test } from "node:test";

import {
  automationModeLabel,
  goalStatusLabel,
  scenarioLabel,
  statusBarChips,
  type GoalStatusBarData,
} from "./goal-status.ts";

const base = (overrides: Partial<GoalStatusBarData> = {}): GoalStatusBarData => ({
  scenario: "dev",
  model: "glm-4.6",
  automationMode: "assist",
  goal: "把登录页按钮改成蓝色",
  goalStatus: "executing",
  backgroundCount: 0,
  serviceOnline: true,
  ...overrides,
});

test("状态条六项：场景 / 模型 / 模式 / 目标 / 后台数 / 服务状态", () => {
  const chips = statusBarChips(base({ backgroundCount: 2 }));
  assert.deepEqual(
    chips.map((item) => item.key),
    ["scenario", "model", "automation", "goal", "background"],
  );
  assert.equal(chips[0].value, "开发");
  assert.equal(chips[1].value, "glm-4.6");
  assert.equal(chips[2].value, "辅助");
  assert.equal(chips[3].value, "把登录页按钮改成蓝色 · 执行中");
  assert.equal(chips[4].value, "2 个任务");
});

test("没有目标、没有后台任务时不显示这两格", () => {
  const chips = statusBarChips(base({ goal: "", goalStatus: "", backgroundCount: 0 }));
  assert.deepEqual(
    chips.map((item) => item.key),
    ["scenario", "model", "automation"],
  );
});

test("等审批 / 失败 / 自动驾驶用不同语义色", () => {
  const waiting = statusBarChips(base({ goalStatus: "waiting_approval" })).find((item) => item.key === "goal");
  assert.equal(waiting?.tone, "warn");
  assert.match(waiting?.value ?? "", /等你批准/);
  const failed = statusBarChips(base({ goalStatus: "failed" })).find((item) => item.key === "goal");
  assert.equal(failed?.tone, "danger");
  const auto = statusBarChips(base({ automationMode: "autonomous" })).find((item) => item.key === "automation");
  assert.equal(auto?.tone, "brand");
});

test("未知枚举回落而不是显示原始英文键", () => {
  assert.equal(scenarioLabel("bogus"), "开发");
  assert.equal(automationModeLabel("bypassPermissions"), "辅助");
  assert.equal(goalStatusLabel("bogus"), "");
  assert.equal(goalStatusLabel("repairing"), "修复中");
});
