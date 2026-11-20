/**
 * 步骤行说人话：`read_file` 是接口字段名，用户要的是「正在读取文件」。
 *
 * 判据不是"文案好不好听"，是两条硬要求：
 * 1. 三种阶段都得读得通。中文的进行体是加前缀、失败是加后缀，所以不该维护
 *    61 × 3 张表——那必然漏，漏了就是半截翻译（`正在apply_patch`）。
 * 2. 没登记的工具宁可退回原始名字。`正在read_weird_tool` 比 `read_weird_tool`
 *    更难懂，也比它更假——它让人以为界面认识这个工具。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { describeTool, hasToolLabel, toolPhaseLabel } from "./tool-display.ts";

test("同一个工具，三种阶段三种说法", () => {
  assert.equal(toolPhaseLabel("read_file", "running"), "正在读取文件");
  assert.equal(toolPhaseLabel("read_file", "ok"), "读取文件");
  assert.equal(toolPhaseLabel("read_file", "failed"), "读取文件失败");
});

test("大小写不影响，工具名里带后缀也不影响", () => {
  assert.equal(toolPhaseLabel("READ_FILE", "running"), "正在读取文件");
  assert.equal(toolPhaseLabel("Run_Shell", "failed"), "执行命令失败");
});

test("没登记的工具退回原始名字，不硬拼一个假的中文", () => {
  assert.equal(toolPhaseLabel("read_weird_tool", "running"), "");
  assert.equal(toolPhaseLabel("", "ok"), "");
  assert.equal(hasToolLabel("read_weird_tool"), false);
  assert.equal(hasToolLabel("read_file"), true);
});

test("后端真实存在的工具都有说法——61 个全查一遍", () => {
  // 这份清单来自后端 registry；数量对不上就说明又加了工具没补文案。
  const tools = [
    "list_files", "read_file", "search_code", "glob", "apply_patch", "write_file", "edit_file",
    "notebook_edit", "run_shell", "repl", "run_tests", "git", "lsp", "browser_check",
    "web_search", "web_fetch", "update_plan", "set_goal", "propose_goal", "enter_plan_mode",
    "exit_plan_mode", "enter_worktree", "exit_worktree", "memory_search", "memory_write",
    "read_notes", "write_notes", "spawn_agent", "spawn_agents", "request_user_input",
    "report_findings", "tool_search", "find_skills", "list_curated_skills", "propose_skills",
    "install_market_skill", "skill", "monitor", "schedule_wakeup", "send_message", "workflow",
    "cron_create", "cron_list", "cron_delete", "list_agents", "list_mcp_resources",
    "read_mcp_resource", "read_mcp_resource_dir", "refresh_mcp_tools", "task_create",
    "task_list", "task_get", "task_update", "task_output", "task_stop", "team_status",
    "team_create_bot", "team_update_bot", "team_add_member", "team_remove_member",
    "team_assign_task",
  ];
  assert.equal(tools.length, 61, "清单长度变了：要么补工具，要么改这里的断言");
  const missing = tools.filter((name) => !hasToolLabel(name));
  assert.deepEqual(missing, [], `这些工具没有中文说法：${missing.join(", ")}`);
  for (const name of tools) {
    assert.ok(!toolPhaseLabel(name, "running").includes("_"), `${name} 的进行体里还有下划线`);
  }
});

test("翻译层不影响参数与结果的既有读法", () => {
  const view = describeTool("read_file", { path: "web/src/a.ts" }, "a\nb\n", false);
  assert.equal(view.label, "读取文件");
  assert.match(view.target, /web\/src\/a\.ts/);
  assert.match(view.result, /2 行/);
});
