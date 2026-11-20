/**
 * 审批载荷的判据。
 *
 * 断言的是"发出去的那个入参长什么样"，不是"源码里出现了某个字段名"：
 * 这一整类 bug（驳回理由 / 打回步骤被静默丢掉）只会在载荷层显形。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { decisionInput } from "./approval-payload.ts";
import { sendBackChoices } from "./approval-copy.ts";

const target = {
  key: "approval-1",
  approvalId: "ap-1",
  toolCallId: "call-1",
  argumentsHash: "h-1",
  toolName: "run_shell",
};

test("驳回带上「退回第 N 步」：步骤 id 原样进 sendBackStepId", () => {
  const [choice] = sendBackChoices([
    { id: "step-a", text: "调研", status: "completed" },
    { id: "step-b", text: "实现", status: "completed" },
  ]);
  const sent = decisionInput({
    decision: "deny",
    scope: "once",
    stepId: "step-b",
    feedback: "  这一步改错了地方  ",
    target,
  });
  // 用户在下拉里看到的「第 2 步」必须就是发回去的那一步，否则前端退的是 A、后端退的是 B。
  assert.equal(choice.label, "第 1 步 调研");
  assert.equal(sent.sendBackStepId, "step-b");
  assert.equal(sent.feedback, "这一步改错了地方", "理由要去掉首尾空白再发");
  assert.equal(sent.decision, "deny");
});

test("批准不带理由也不带打回：这两个字段对后端没有消费者", () => {
  const sent = decisionInput({
    decision: "approve",
    scope: "tool",
    stepId: "step-b",
    feedback: "看着还行",
    target,
  });
  assert.equal(sent.sendBackStepId, undefined, "批准 + 退回某一步是自相矛盾的动作");
  assert.equal(sent.feedback, undefined);
  assert.equal(sent.scope, "tool");
});

test("空步骤 / 纯空格步骤按「普通驳回」处理，不发空串", () => {
  for (const stepId of ["", "   ", undefined]) {
    const sent = decisionInput({ decision: "deny", scope: "once", stepId, target });
    assert.equal(sent.sendBackStepId, undefined);
  }
  // 理由同理：敲几格空格等于没写，发一个空 feedback 过去模型会收到一条空的"用户意见"。
  const blank = decisionInput({ decision: "deny", scope: "once", feedback: "   ", target });
  assert.equal(blank.feedback, undefined);
});

test("卡的身份按字段搬过去；没有目标时留 undefined 而不是空串", () => {
  const withTarget = decisionInput({ decision: "deny", scope: "once", target });
  assert.equal(withTarget.approvalId, "ap-1");
  assert.equal(withTarget.toolCallId, "call-1");
  assert.equal(withTarget.argumentsHash, "h-1");
  assert.equal(withTarget.toolName, "run_shell");

  // 对话页如果拿不到某张卡的 id（老卡片、刷新后事件缺失），不要编一个 ""：
  // 后端会把空串当成"对号失败"而不是"没给"，于是审批永远匹配不上。
  const noTarget = decisionInput({ decision: "approve", scope: "once" });
  assert.equal(noTarget.approvalId, undefined);
  assert.equal(noTarget.toolName, undefined);
});

test("key 不进接口入参：它只是时间线的界面标识", () => {
  const sent = decisionInput({ decision: "deny", scope: "once", target });
  assert.equal(Object.hasOwn(sent, "key"), false);
});
