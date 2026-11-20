/**
 * 助手档案：内置助手排前面，"没写限制"和"没有工具"分开说。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { agentSourceLabel, agentToolsText, orderAgents } from "./agent-profile.ts";
import type { AgentProfile } from "./api.ts";

const agent = (name: string, source = "builtin"): AgentProfile => ({
  name,
  source,
  description: "",
  chars: 10,
  path: "",
});

test("内置助手排前面，其余按名字——用户第一眼看到的该是他真会用的", () => {
  const rows = orderAgents([agent("zeta", "project"), agent("plan"), agent("alpha", "user"), agent("general-purpose")]);
  assert.deepEqual(
    rows.map((item) => item.name),
    ["general-purpose", "plan", "alpha", "zeta"],
  );
});

test("没写工具限制 ≠ 没有可用工具", () => {
  assert.match(agentToolsText(undefined, undefined), /没写/);
  assert.match(agentToolsText([], undefined), /没有可用工具/);
  assert.match(agentToolsText(["read_file", "write_file"], ["run_shell"]), /可用 read_file、write_file/);
  assert.match(agentToolsText(["read_file"], ["run_shell"]), /禁止 run_shell/);
});

test("来源有中文说法，没见过的原样保留", () => {
  // 后端实际写的是 built-in（带连字符），不是 builtin。
  assert.equal(agentSourceLabel("built-in"), "内置");
  assert.equal(agentSourceLabel("plugin"), "插件");
  assert.equal(agentSourceLabel("user"), "我的");
  assert.equal(agentSourceLabel("something_new"), "something_new");
});
