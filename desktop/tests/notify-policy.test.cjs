"use strict";

/**
 * 桌面通知投递策略的测试（noticeText / decideNotification / seen 集合）。
 *
 * 这里钉的不是"该不该打扰"——那是后端 notify.py 的事，这张表已经在别处测过。
 * 这里钉的是壳自己那三条判断，因为每一条错了用户都能直接感觉到：
 *
 * 1. 去重失效 → 同一个提醒弹两遍（游标是秒精度，同批必然被读到两次）；
 * 2. 聚焦时不压 → 用户正看着界面，任务卡已经变了，弹窗纯属打扰；
 * 3. 防刷屏失效 → 一次跑完十几个子任务，屏幕上摞十几条 toast。
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  BURST_LIMIT,
  alreadyShown,
  decideNotification,
  noticeText,
  rememberSeen,
} = require("../notify-policy.cjs");

const item = (over = {}) => ({
  id: "evt-1",
  task_id: "task-1",
  goal: "检查一下部署",
  kind: "deliverable",
  origin: "user",
  ...over,
});

// ── 文案 ────────────────────────────────────────────────────────────────────

test("定时起的任务弹「到点了」，不是「做完了」", () => {
  const text = noticeText(item({ origin: "schedule" }));
  assert.equal(text.title, "到点了");
  assert.equal(text.body, "检查一下部署");
});

test("盯着工作区的任务弹「有变化了」", () => {
  const text = noticeText(item({ origin: "watch" }));
  assert.equal(text.title, "有变化了");
});

test("审批优先于来源：定时任务卡在审批时，标题是「等你批准」", () => {
  const text = noticeText(item({ kind: "approval", origin: "schedule" }));
  assert.equal(text.title, "等你批准", "审批比来源更急，不能被定时文案盖掉");
});

test("失败的通知不说「做完了」", () => {
  const text = noticeText(item({ kind: "failure" }));
  assert.equal(text.title, "没跑成");
});

test("没有 goal 时也给出能读的正文，不弹空壳", () => {
  const text = noticeText({ id: "evt-1", kind: "deliverable", goal: "", title: "" });
  assert.ok(text.body.length > 0, "空正文的 toast 等于什么都没说");
  assert.ok(text.title.length > 0);
});

test("超长正文被截断，不会撑爆 toast", () => {
  const text = noticeText(item({ goal: "很长的目标".repeat(80) }));
  assert.ok(text.body.length <= 120, `实际 ${text.body.length}`);
  assert.ok(text.body.endsWith("…"), "截断要有省略号，读者才知道后面还有");
});

// ── 去重 ────────────────────────────────────────────────────────────────────

test("同一条 id 弹过一次就不再弹", () => {
  const seen = rememberSeen([], "evt-1");
  const decision = decideNotification({ item: item(), seenIds: seen, nowMs: 1000 });
  assert.equal(decision.show, false);
  assert.match(decision.reason, /弹过/);
});

test("换个 id 就是新的，照弹", () => {
  const seen = rememberSeen([], "evt-1");
  const decision = decideNotification({ item: item({ id: "evt-2" }), seenIds: seen, nowMs: 1000 });
  assert.equal(decision.show, true);
});

test("去重集合只留最近 MAX_SEEN 条，老的被挤掉", () => {
  let seen = [];
  for (let index = 0; index < 205; index += 1) {
    seen = rememberSeen(seen, `evt-${index}`);
  }
  assert.equal(seen.length, 200);
  assert.equal(seen.at(-1), "evt-204", "最新的必须还在");
  assert.equal(seen.includes("evt-0"), false);
});

test("重复记同一个 id 不会把集合撑大", () => {
  const once = rememberSeen([], "evt-1");
  const twice = rememberSeen(once, "evt-1");
  assert.deepEqual(twice, ["evt-1"]);
});

test("空 id 不被当成「弹过」，否则整个集合会被一条坏数据污染", () => {
  assert.equal(alreadyShown(["", "evt-1"], ""), false);
  assert.deepEqual(rememberSeen(["evt-1"], ""), ["evt-1"]);
});

// ── 焦点 ────────────────────────────────────────────────────────────────────

test("窗口聚焦且可见时不弹", () => {
  const decision = decideNotification({
    item: item(),
    seenIds: [],
    windowFocused: true,
    windowVisible: true,
  });
  assert.equal(decision.show, false);
  assert.match(decision.reason, /正看着/);
});

test("窗口不可见（缩到托盘/被藏起来）时照弹", () => {
  const decision = decideNotification({ item: item(), seenIds: [], windowFocused: false, windowVisible: false });
  assert.equal(decision.show, true);
});

test("窗口可见但没焦点（切到别的应用）时照弹", () => {
  const decision = decideNotification({ item: item(), seenIds: [], windowFocused: false, windowVisible: true });
  assert.equal(decision.show, true);
});

test("焦点判断对审批同样生效：用户就在界面上，别打断", () => {
  const decision = decideNotification({
    item: item({ kind: "approval" }),
    seenIds: [],
    windowFocused: true,
    windowVisible: true,
  });
  assert.equal(decision.show, false);
});

// ── 防刷屏 ──────────────────────────────────────────────────────────────────

test("一分钟内弹满三条就压住，不继续刷屏", () => {
  const decision = decideNotification({
    item: item(),
    seenIds: [],
    shownAt: [1000, 2000, 3000],
    nowMs: 4000,
  });
  assert.equal(decision.show, false);
  assert.match(decision.reason, /已经弹过 3 条/);
});

test("一分钟窗口外的旧记录不算数", () => {
  const decision = decideNotification({
    item: item(),
    seenIds: [],
    shownAt: [0, 1000, 2000],
    nowMs: 200_000,
  });
  assert.equal(decision.show, true, "两分钟前弹过三条，不该一直压着");
});

test("审批不受防刷屏限制：压着等于让任务白等", () => {
  const decision = decideNotification({
    item: item({ kind: "approval" }),
    seenIds: [],
    shownAt: [1000, 2000, 3000],
    nowMs: 4000,
  });
  assert.equal(decision.show, true);
  assert.match(decision.reason, /审批/);
});

test("防刷屏阈值就是 BURST_LIMIT 那一条，不多不少", () => {
  const atLimit = decideNotification({
    item: item(),
    seenIds: [],
    shownAt: Array.from({ length: BURST_LIMIT - 1 }, (_, index) => index * 100),
    nowMs: 1000,
  });
  assert.equal(atLimit.show, true, "少一条时还该弹");

  const overLimit = decideNotification({
    item: item(),
    seenIds: [],
    shownAt: Array.from({ length: BURST_LIMIT }, (_, index) => index * 100),
    nowMs: 1000,
  });
  assert.equal(overLimit.show, false);
});

// ── 记账（settle）─────────────────────────────────────────────────────────
//
// show 与 settle 是两件事：不弹的也可能要记账。写错这一格，要么同一条提醒
// 被反复评估（浪费轮询），要么被防刷屏压住的那条永久消失（用户永远收不到）。

test("弹出去的那条要记账", () => {
  assert.equal(decideNotification({ item: item(), seenIds: [], nowMs: 0 }).settle, true);
});

test("用户正看着而压下的那条要记账——他在界面上已经看见了", () => {
  const decision = decideNotification({
    item: item(),
    seenIds: [],
    windowFocused: true,
    windowVisible: true,
  });
  assert.equal(decision.show, false);
  assert.equal(decision.settle, true, "切走之后再补弹一次就是重复告知");
});

test("被防刷屏压住的那条不能记账——下一分钟还得弹", () => {
  const decision = decideNotification({
    item: item(),
    seenIds: [],
    shownAt: [1000, 2000, 3000],
    nowMs: 4000,
  });
  assert.equal(decision.show, false);
  assert.equal(decision.settle, false, "记成已处理等于永久丢掉这条通知");
});

test("没 id 或已弹过的都算处理完了，不必再评估", () => {
  assert.equal(decideNotification({ item: null }).settle, true);
  const seen = rememberSeen([], "evt-1");
  assert.equal(decideNotification({ item: item(), seenIds: seen }).settle, true);
});

// ── 坏输入 ──────────────────────────────────────────────────────────────────

test("没有 item 时不崩，只是不弹", () => {
  assert.equal(decideNotification({ item: null }).show, false);
  assert.equal(decideNotification({}).show, false);
});

test("item 没有 id 时当作不可弹（去重无从谈起）", () => {
  assert.equal(decideNotification({ item: { goal: "没有 id" } }).show, false);
});
