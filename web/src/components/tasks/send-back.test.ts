import assert from "node:assert/strict";
import { test } from "node:test";

import { sendBackChoices } from "./approval-copy.ts";

const step = (id: string, text: string, status: string) => ({ id, text, status, milestoneId: "", evidence: "" });

test("只能打回到做过或正在做的步骤", () => {
  // 打回到一个还没开始的步骤没有意义：那里没有可重做的产物，
  // 列出来只会让「打回」看起来像「跳到未来」。
  const choices = sendBackChoices([
    step("s1", "调研", "completed"),
    step("s2", "实现", "in_progress"),
    step("s3", "测试", "pending"),
    step("s4", "上线", "cancelled"),
  ]);
  assert.deepEqual(
    choices.map((item) => item.id),
    ["s1", "s2"],
  );
});

test("选项按人话编号，不暴露内部 id", () => {
  const choices = sendBackChoices([step("s1", "调研", "completed"), step("s2", "实现", "completed")]);
  assert.deepEqual(
    choices.map((item) => item.label),
    ["第 1 步 调研", "第 2 步 实现"],
  );
  // id 仍然留着，提交时要用它。
  assert.equal(choices[0].id, "s1");
});

test("步骤标题过长时截断，不把下拉框撑爆", () => {
  const long = "实现".repeat(40);
  const [choice] = sendBackChoices([step("s1", long, "completed")]);
  assert.ok(choice.label.length <= 24, `label 太长：${choice.label.length}`);
});

test("没有计划或没有可打回的步骤时返回空数组", () => {
  assert.deepEqual(sendBackChoices([]), []);
  assert.deepEqual(sendBackChoices([step("s1", "调研", "pending")]), []);
});

test("缺少 id 或文本的步骤直接跳过，不生成空选项", () => {
  const choices = sendBackChoices([
    { id: "", text: "没有 id", status: "completed", milestoneId: "", evidence: "" },
    { id: "s3", text: "  ", status: "completed", milestoneId: "", evidence: "" },
    step("s4", "有效的", "completed"),
  ]);
  assert.deepEqual(choices.map((item) => item.id), ["s4"]);
});
