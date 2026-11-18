import assert from "node:assert/strict";
import { test } from "node:test";

import { matchSlash, SLASH_COMMANDS } from "./slash-commands.ts";

test("只有开头是 / 且还没打空格才算命令", () => {
  assert.ok(matchSlash("/"));
  assert.ok(matchSlash("/设置"));
  // 打空格就是要写正文了，面板必须让开
  assert.equal(matchSlash("/设置 帮我改这个"), null);
  assert.equal(matchSlash("你好 /设置"), null);
  assert.equal(matchSlash(""), null);
  assert.equal(matchSlash("/usr/local"), null, "路径不能被当成命令");
});

test("裸 / 列出全部命令", () => {
  const match = matchSlash("/");
  assert.equal(match?.items.length, SLASH_COMMANDS.length);
});

test("按中文标签前缀过滤", () => {
  const match = matchSlash("/设");
  assert.deepEqual(match?.items.map((item) => item.id), ["settings"]);
});

test("英文别名也能命中", () => {
  const match = matchSlash("/sk");
  assert.deepEqual(match?.items.map((item) => item.id), ["skills"]);
});

test("每条命令都必须有可执行的去处", () => {
  for (const command of SLASH_COMMANDS) {
    const hasAction = Boolean(command.action);
    const hasLink = Boolean(command.href);
    assert.ok(hasAction !== hasLink, `${command.id} 必须且只能有 action 或 href 之一`);
    assert.ok(command.label.startsWith("/"), `${command.id} 的标签要以 / 开头`);
    assert.ok(command.hint.length > 0, `${command.id} 要有说明`);
  }
});

test("命令 id 唯一，否则快捷键与 key 会撞", () => {
  const ids = SLASH_COMMANDS.map((item) => item.id);
  assert.equal(new Set(ids).size, ids.length);
});
