import assert from "node:assert/strict";
import { test } from "node:test";

import { approvalQuestion } from "./approval-copy.ts";

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
