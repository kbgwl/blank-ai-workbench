import assert from "node:assert/strict";
import { test } from "node:test";

import {
  INTENT_LABEL,
  clockTime,
  intentLabel,
  intentTone,
  judgementOutcome,
  parseStamp,
  pendingLine,
  relativeTime,
  sourceLine,
  statusLabel,
  statusTone,
} from "./format.ts";

/** v165 §2.2 的十六种意图，一个都不能少：少一个那类消息就会显示成空白。 */
const V165_INTENTS = [
  "answer",
  "inspect",
  "execute",
  "continue",
  "retry",
  "resume",
  "pause",
  "cancel",
  "approve",
  "reject",
  "brainstorm",
  "schedule",
  "watch",
  "remember",
  "forget",
  "ask_user",
];

test("十六种意图全都有中文标签", () => {
  for (const intent of V165_INTENTS) {
    assert.ok(INTENT_LABEL[intent], `缺少 ${intent} 的标签`);
    assert.notEqual(intentLabel(intent), intent, `${intent} 没有被翻成中文`);
  }
});

test("认不出的意图给原值，不给空白", () => {
  assert.equal(intentLabel("teleport"), "teleport");
  assert.equal(intentLabel(""), "未判定");
});

test("按色调分组：动手 / 回答 / 控制 / 等待", () => {
  assert.equal(intentTone("execute"), "act");
  assert.equal(intentTone("remember"), "act");
  assert.equal(intentTone("answer"), "reply");
  assert.equal(intentTone("pause"), "control");
  assert.equal(intentTone("watch"), "defer");
  assert.equal(intentTone("teleport"), "unknown");
});

test("控制词优先于建任务：说「暂停」不能显示成「已建任务」", () => {
  const line = judgementOutcome({
    intent: "pause",
    control_action: "pause",
    needs_execution: true,
    status: "paused",
  });
  assert.ok(line.includes("已有的那一轮"));
  assert.ok(!line.includes("建了任务"), "控制词不该被说成又开了一轮");
});

test("接着上一轮要先说接着", () => {
  assert.ok(
    judgementOutcome({ intent: "execute", continues_goal: true, needs_execution: true }).includes("接着上一轮"),
  );
});

test("要动手时说明交给了执行侧", () => {
  assert.ok(judgementOutcome({ intent: "execute", needs_execution: true }).includes("建了任务"));
});

test("纯问答、先问、发散、等待各自有话说", () => {
  assert.ok(judgementOutcome({ intent: "answer" }).includes("没碰工具"));
  assert.ok(judgementOutcome({ intent: "ask_user" }).includes("先问你"));
  assert.ok(judgementOutcome({ intent: "brainstorm" }).includes("只发散"));
  assert.ok(judgementOutcome({ intent: "watch" }).includes("待触发"));
});

test("draft / queued 算在跑，waiting_approval 算等你", () => {
  assert.equal(statusTone("draft"), "running");
  assert.equal(statusTone("queued"), "running");
  assert.equal(statusTone("waiting_approval"), "waiting");
  assert.equal(statusTone("paused"), "waiting");
  assert.equal(statusTone("failed"), "failed");
  assert.equal(statusTone("succeeded"), "idle");
});

test("状态有中文，认不出就沿用原值", () => {
  assert.equal(statusLabel("running"), "运行中");
  assert.equal(statusLabel("weird"), "weird");
  assert.equal(statusLabel(""), "未知");
});

test("相对时间只到小时，再久给日期", () => {
  const now = Date.parse("2026-11-08T12:00:00Z");
  assert.equal(relativeTime("2026-11-08T11:59:40", now), "刚刚");
  assert.equal(relativeTime("2026-11-08T11:45:00", now), "15 分钟前");
  assert.equal(relativeTime("2026-11-08T09:00:00", now), "3 小时前");
  assert.equal(relativeTime("2026-11-01T09:00:00", now), "11/1");
  assert.equal(relativeTime("", now), "");
});

test("后端给的是 UTC，必须按 UTC 解析——真机上差过 8 小时", () => {
  // 真实读数：库里 03:30(UTC) 的判定，界面显示成 03:30，而用户当时是 11:30。
  const iso = "2026-11-08T03:30:00";
  assert.equal(parseStamp(iso), Date.UTC(2026, 10, 8, 3, 30, 0));
  const local = new Date(Date.UTC(2026, 10, 8, 3, 30, 0));
  const pad = (value: number) => String(value).padStart(2, "0");
  assert.equal(clockTime(iso), `${pad(local.getHours())}:${pad(local.getMinutes())}`);
});

test("时钟解析不出来给破折号，不留空", () => {
  assert.equal(clockTime(""), "—");
  assert.equal(clockTime("不是时间"), "—");
});

test("永远等不到的那条必须说破", () => {
  const line = pendingLine({
    waiting_for: "等一个不会到来的事件：没有到点时间，也没有对应的事件源",
    never_fires: true,
  });
  assert.ok(line.startsWith("永远等不到"));
});

test("能到点的照原样说", () => {
  assert.equal(
    pendingLine({ waiting_for: "到点 2026-11-08T13:00 放行", never_fires: false }),
    "到点 2026-11-08T13:00 放行",
  );
});

test("未接通就说未接通，不说「暂不支持」", () => {
  assert.equal(sourceLine({ label: "企业微信", connected: false, note: "" }), "企业微信 · 未接通");
  assert.equal(sourceLine({ label: "你发的消息", connected: true, note: "" }), "你发的消息 · 已接通");
});
