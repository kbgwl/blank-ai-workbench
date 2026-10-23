import assert from "node:assert/strict";
import test from "node:test";

import {
  argumentPreview,
  botRoundTokens,
  initials,
  insertMention,
  messageStatusLabel,
  roundActive,
  roundGapLabel,
  roundSpend,
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

test("the round header reads the ledger, not the orchestrator counter", () => {
  // 一轮里两个成员各有一个任务：账本合起来 1,440，编排器只加到 1,000（另一次发言没收尾）。
  const round = { tokens: 1_000, usage: { tokens: 1_440, calls: 9, tasks: 2, runs: 2 } };
  const spend = roundSpend(round);
  assert.equal(spend.tokens, 1_440, "本轮花费按发起轮从账本筛；拿轮计数就是把没走完的发言当没花钱");
  assert.equal(spend.fromLedger, true);
  assert.equal(spend.extra, 440);
  assert.deepEqual([spend.calls, spend.tasks, spend.runs], [9, 2, 2]);
  // 旧接口/还没有账本的那一轮：退回轮计数，并且不谎称自己来自账本。
  const fallback = roundSpend({ tokens: 700 });
  assert.deepEqual(
    [fallback.tokens, fallback.fromLedger, fallback.extra],
    [700, false, 0],
    "读不到账本要退回轮计数，但显示口径必须如实",
  );
  assert.equal(roundSpend({}).tokens, 0);
  assert.equal(roundSpend(null as never).tokens, 0);
  // 账本低于轮计数（理论不该发生）时差额不许是负数——页面不能显示 "+-440"。
  assert.equal(roundSpend({ tokens: 1_440, usage: { tokens: 1_000 } }).extra, 0);
});

test("the ledger gap is only spelled out when there is one", () => {
  assert.equal(roundGapLabel(0), "", "没有差额就别给页面板一个 0");
  assert.equal(roundGapLabel(-5), "");
  assert.equal(roundGapLabel(440), "+440 未计入");
  assert.equal(roundGapLabel(12_345), "+12,345 未计入");
});

test("each member chip shows that member's share of the round", () => {
  const round = {
    usage: {
      by_bot: [
        { bot_id: "bot-a", tokens: 900 },
        { bot_id: "bot-b", tokens: 540 },
        { bot_id: "", tokens: 440 },
      ],
    },
  };
  assert.equal(botRoundTokens(round, "bot-a"), 900);
  assert.equal(botRoundTokens(round, "bot-b"), 540);
  // 装置自检：三档加起来必须等于本轮合计，页面上"每人一档"与"整轮"才是同一笔钱
  assert.equal(round.usage.by_bot.reduce((sum, item) => sum + item.tokens, 0), 1_880);
  assert.equal(botRoundTokens(round, "bot-missing"), 0, "查不到就显示 0，不许拿别人的数顶");
  assert.equal(botRoundTokens(round, ""), 0);
  assert.equal(botRoundTokens(null, "bot-a"), 0);
  assert.equal(botRoundTokens({ usage: undefined }, "bot-a"), 0);
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
