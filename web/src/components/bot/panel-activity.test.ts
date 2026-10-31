import assert from "node:assert/strict";
import { test } from "node:test";

import { activityRows } from "./panel-activity.ts";
import type { BotStep } from "./turns.ts";

const tool = (over: Partial<Extract<BotStep, { kind: "tool" }>> = {}): BotStep => ({
  kind: "tool",
  key: "k1",
  name: "read_file",
  args: {},
  status: "ok",
  durationMs: null,
  output: "",
  risk: "",
  ...over,
});

test("顺序原样保留：活动列表是时间线，不是排行榜", () => {
  const rows = activityRows([
    tool({ key: "a", name: "read_file" }),
    { kind: "note", key: "b", text: "说明", tone: "muted" },
    tool({ key: "c", name: "run_shell" }),
  ]);
  assert.deepEqual(rows.map((row) => row.key), ["a", "b", "c"]);
});

test("工具行取路径/命令作副行，失败与进行中各自一种色", () => {
  const rows = activityRows([
    tool({ key: "a", name: "read_file", args: { path: "src/app.ts" } }),
    tool({ key: "b", name: "run_shell", args: { command: "pytest -q" }, status: "failed" }),
    tool({ key: "c", name: "grep", status: "running" }),
  ]);
  assert.equal(rows[0].detail, "src/app.ts");
  assert.equal(rows[1].detail, "pytest -q");
  assert.equal(rows[1].tone, "failed");
  assert.equal(rows[2].tone, "running");
});

test("没有已知参数时退回输出的第一行，而不是留空", () => {
  const rows = activityRows([tool({ args: {}, output: "第一行\n第二行" })]);
  assert.equal(rows[0].detail, "第一行");
});

test("提问与审批：没答/没批是待办，不是完成", () => {
  const rows = activityRows([
    { kind: "question", key: "q", text: "用哪个库？", answered: "" },
    { kind: "question", key: "q2", text: "用哪个库？", answered: "zod" },
    { kind: "approval", key: "p", text: "删掉 build 目录", granted: null },
    { kind: "approval", key: "p2", text: "删掉 build 目录", granted: false },
  ]);
  assert.equal(rows[0].tone, "warn");
  assert.equal(rows[0].label, "提问");
  assert.equal(rows[1].detail, "用哪个库？ → zod");
  assert.equal(rows[2].label, "等待批准");
  assert.equal(rows[3].tone, "failed");
});

test("计划行只列前三步，并说明总步数", () => {
  const rows = activityRows([{ kind: "plan", key: "p", items: ["一", "二", "三", "四"] }]);
  assert.equal(rows[0].detail, "一 → 二 → 三 …共 4 步");
});

test("改动行认三种类型，认不出的不改写", () => {
  const rows = activityRows([
    { kind: "change", key: "c1", path: "a.ts", changeType: "created" },
    { kind: "change", key: "c2", path: "b.ts", changeType: "renamed" },
  ]);
  assert.equal(rows[0].label, "新增 a.ts");
  assert.equal(rows[1].label, "改动 b.ts");
});
