import assert from "node:assert/strict";
import { test } from "node:test";

import { approvalApproveLabel, approvalQuestion, isPlanGate } from "./approval-copy.ts";

test("命令类审批问「要让它跑这条命令吗」，而不是报工具名", () => {
  // 「需要你批准：run_shell」得先在脑子里翻译一遍，才知道自己在批准什么。
  assert.equal(approvalQuestion("run_shell", { command: "curl -s localhost" }), "要让它跑这条命令吗？");
  assert.equal(approvalQuestion("bash", {}), "要让它跑这条命令吗？");
  assert.equal(approvalQuestion("execute_command", {}), "要让它跑这条命令吗？");
});

test("改文件的审批把文件名说出来——同名文件有好几个时，只说「改文件」等于没说", () => {
  assert.equal(approvalQuestion("write_file", { path: "web/src/a.ts" }), "要让它改 web/src/a.ts 吗？");
  assert.equal(approvalQuestion("edit", { file_path: "b.ts" }), "要让它改 b.ts 吗？");
  assert.equal(approvalQuestion("patch", {}), "要让它改这些文件吗？");
});

test("联网类与未知工具有各自的问法，未知的也带工具名而不是空着", () => {
  assert.equal(approvalQuestion("web_fetch", {}), "要让它联网查东西吗？");
  assert.equal(approvalQuestion("deploy", {}), "要让它执行 deploy 吗？");
});

test("工具名大小写不影响判断", () => {
  assert.equal(approvalQuestion("RUN_SHELL", {}), "要让它跑这条命令吗？");
  assert.equal(approvalQuestion("Write_File", { path: "x" }), "要让它改 x 吗？");
});

test("计划门问的是「按这份计划开始改吗」，不是「执行 exit_plan_mode 吗」", () => {
  // 工具名是实现细节。用户在这一格要答的是"这份计划行不行"。
  assert.equal(approvalQuestion("exit_plan_mode", {}), "计划做完了，按这份开始改吗？");
  assert.equal(approvalQuestion("EXIT_PLAN_MODE", { plan: "1. 改 A" }), "计划做完了，按这份开始改吗？");
  assert.equal(approvalQuestion("enter_plan_mode", {}), "要先进计划模式（只看不改）吗？");
  // 不能因为工具名里带 plan 就落到"改文件"那一档（反向断言，防止正则接错）。
  assert.notEqual(approvalQuestion("exit_plan_mode", {}), "要让它改这些文件吗？");
});

test("计划门的按钮叫「开始执行」，别的卡不改口", () => {
  assert.equal(approvalApproveLabel("exit_plan_mode", "once", "允许这一次"), "开始执行");
  // 更宽的授权档仍是原来的说法：那时候用户批的是"以后都别问"，不只是"开始"。
  assert.equal(approvalApproveLabel("exit_plan_mode", "tool", "本会话都允许"), "本会话都允许");
  // 进入计划模式是收紧方向，按钮不该变成"开始执行"。
  assert.equal(approvalApproveLabel("enter_plan_mode", "once", "允许这一次"), "允许这一次");
  assert.equal(approvalApproveLabel("run_shell", "once", "允许这一次"), "允许这一次");
  assert.equal(isPlanGate("exit_plan_mode"), true);
  assert.equal(isPlanGate("write_file"), false);
});
