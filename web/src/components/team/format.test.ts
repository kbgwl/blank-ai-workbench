import assert from "node:assert/strict";
import test from "node:test";

import {
  argumentPreview,
  initials,
  insertMention,
  messageStatusLabel,
  roundActive,
  roundStatusLabel,
  shortTokens,
  splitHighlight,
  usagePercent,
} from "./format.ts";

test("active rounds are the ones still moving", () => {
  assert.equal(roundActive("running"), true);
  assert.equal(roundActive("waiting_approval"), true);
  assert.equal(roundActive("cancelling"), true);
  assert.equal(roundActive("completed"), false);
  assert.equal(roundActive("failed"), false);
  assert.equal(roundActive(null), false);
  assert.equal(roundStatusLabel("waiting_approval"), "等待确认");
});

test("mention insertion keeps a single space and does not duplicate", () => {
  assert.equal(insertMention("", "后端"), "@后端 ");
  assert.equal(insertMention("看一下", "后端"), "看一下 @后端 ");
  assert.equal(insertMention("看一下 ", "后端"), "看一下 @后端 ");
  assert.equal(insertMention("请 @后端 处理", "后端"), "请 @后端 处理");
});

test("usage percent is clamped and tolerates a zero budget", () => {
  assert.equal(usagePercent(3000, 60000), 5);
  assert.equal(usagePercent(90000, 60000), 100);
  assert.equal(usagePercent(10, 0), 0);
  assert.equal(usagePercent(-5, 100), 0);
});

test("token counts stay short enough for a header", () => {
  assert.equal(shortTokens(0), "0");
  assert.equal(shortTokens(950), "950");
  assert.equal(shortTokens(1250), "1.3k");
  assert.equal(shortTokens(60000), "60k");
  assert.equal(shortTokens(1500000), "1.5m");
});

test("initials work for Chinese and Latin names", () => {
  assert.equal(initials("小白管家"), "小");
  assert.equal(initials("backend"), "BA");
  assert.equal(initials(""), "?");
});

test("message status labels are explicit for silent and paused turns", () => {
  assert.equal(messageStatusLabel("pass"), "没有补充");
  assert.equal(messageStatusLabel("paused"), "已暂停");
  assert.equal(messageStatusLabel("succeeded"), "");
});

test("argument preview prefers the shell command", () => {
  assert.equal(argumentPreview({ command: "git status" }), "git status");
  assert.equal(argumentPreview({ path: "src/main.py" }), '{"path":"src/main.py"}');
  assert.equal(argumentPreview({}), "");
});

test("search snippets split on the longest matching token", () => {
  const parts = splitHighlight("登录接口超时了", ["登录", "接口"]);
  assert.deepEqual(parts, [
    { text: "登录", hit: true },
    { text: "接口", hit: true },
    { text: "超时了", hit: false },
  ]);
  assert.deepEqual(splitHighlight("abc", []), [{ text: "abc", hit: false }]);
  assert.deepEqual(splitHighlight("", ["a"]), []);
  // 正则元字符按字面量处理，不能被当成通配
  assert.deepEqual(splitHighlight("a+b", ["+"]), [
    { text: "a", hit: false },
    { text: "+", hit: true },
    { text: "b", hit: false },
  ]);
});
