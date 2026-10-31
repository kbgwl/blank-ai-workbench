import assert from "node:assert/strict";
import { test } from "node:test";

import {
  computeProgress,
  groupByMilestone,
  parseMilestones,
  parsePlanSteps,
  parseProgress,
} from "./plan.ts";

// 计划是用户唯一能看见的执行依据。解析错了不是"显示得不好看"：
// 进度条会撒谎、验收指错步骤，而用户没有任何别的渠道能发现这件事。

test("老事件里的纯字符串步骤也要解析出来", () => {
  const steps = parsePlanSteps({ steps: ["读代码", "写实现"] });
  assert.equal(steps.length, 2);
  assert.deepEqual(
    steps.map((step) => step.text),
    ["读代码", "写实现"],
  );
  // 没有状态字段的老数据按待开始算，不猜"已经做完了"。
  assert.ok(steps.every((step) => step.status === "pending"));
  // id 必须非空：没有 id 就没法勾选，也没法被里程碑引用。
  assert.ok(steps.every((step) => step.id));
});

test("结构化步骤保留 id / 状态 / 里程碑归属 / 证据", () => {
  const steps = parsePlanSteps([
    { id: "s1", step: "读代码", status: "completed", milestone_id: "m1", evidence: "读了 3 个文件" },
    { id: "s2", step: "写实现", status: "in_progress" },
  ]);
  assert.equal(steps[0].id, "s1");
  assert.equal(steps[0].status, "completed");
  assert.equal(steps[0].milestoneId, "m1");
  assert.equal(steps[0].evidence, "读了 3 个文件");
  assert.equal(steps[1].status, "in_progress");
  assert.equal(steps[1].milestoneId, "");
});

test("进度只数 completed，in_progress 不算做完", () => {
  const steps = parsePlanSteps([
    { step: "a", status: "completed" },
    { step: "b", status: "in_progress" },
    { step: "c", status: "pending" },
    { step: "d", status: "pending" },
  ]);
  const progress = computeProgress(steps);
  assert.deepEqual(
    { total: progress.total, completed: progress.completed, inProgress: progress.inProgress, percent: progress.percent },
    { total: 4, completed: 1, inProgress: 1, percent: 25 },
  );
});

test("后端给了进度就用后端的，两边不一致时以后端为准", () => {
  const steps = parsePlanSteps([{ step: "a", status: "pending" }]);
  const progress = parseProgress({ total: 7, completed: 3, percent: 43 }, steps);
  assert.deepEqual(
    { total: progress.total, completed: progress.completed, percent: progress.percent },
    { total: 7, completed: 3, percent: 43 },
  );
});

test("没有进度载荷时退回本地计算，不会算出 NaN", () => {
  const progress = parseProgress(undefined, parsePlanSteps(["a"]));
  assert.equal(progress.total, 1);
  assert.equal(progress.percent, 0);
});

test("空计划不会除零", () => {
  assert.equal(computeProgress([]).percent, 0);
});

test("步骤按里程碑分组，没归组的排最后", () => {
  const steps = parsePlanSteps([
    { id: "s1", step: "调研", milestone_id: "m1" },
    { id: "s2", step: "实现", milestone_id: "m2" },
    { id: "s3", step: "收尾" },
  ]);
  const milestones = parseMilestones([
    { id: "m1", title: "调研阶段", total: 1, completed: 0 },
    { id: "m2", title: "实现阶段", total: 1, completed: 0 },
  ]);
  const groups = groupByMilestone(steps, milestones);
  assert.equal(groups.length, 3);
  assert.equal(groups[0].milestone?.title, "调研阶段");
  assert.deepEqual(
    groups[0].steps.map((step) => step.text),
    ["调研"],
  );
  // 最后一组没有里程碑，装着没归组的步骤。
  assert.equal(groups[2].milestone, null);
  assert.deepEqual(
    groups[2].steps.map((step) => step.text),
    ["收尾"],
  );
});

test("指向不存在的里程碑的步骤归到未分组，不静默丢失", () => {
  const steps = parsePlanSteps([{ id: "s1", step: "孤儿步骤", milestone_id: "ghost" }]);
  const milestones = parseMilestones([{ id: "m1", title: "唯一阶段" }]);
  const groups = groupByMilestone(steps, milestones);
  const loose = groups.find((group) => group.milestone === null);
  assert.ok(loose, "没归组的步骤必须有落脚处");
  assert.deepEqual(
    loose.steps.map((step) => step.text),
    ["孤儿步骤"],
  );
});

test("空里程碑仍然出现在分组里", () => {
  const steps = parsePlanSteps([{ id: "s1", step: "a", milestone_id: "m1" }]);
  const milestones = parseMilestones([
    { id: "m1", title: "有步骤" },
    { id: "m2", title: "还没排步骤" },
  ]);
  const groups = groupByMilestone(steps, milestones);
  const empty = groups.find((group) => group.milestone?.id === "m2");
  assert.ok(empty, "空阶段本身是信息，不能因为没步骤就消失");
  assert.equal(empty.steps.length, 0);
});
