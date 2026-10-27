"use strict";

/**
 * 桌面壳策略层（单实例 / 托盘 / 全局快捷键）的判定测试。
 *
 * 能在普通 node 里跑是这套文件存在的前提：shell-policy.cjs 不许 require("electron")，
 * 最后一条用例把这一点本身也钉住了——哪天有人往里加 electron，这条先红。
 *
 * 每条判据都对应一个真实会犯的缺陷：改错实现（比如把 close 一律路由成 quit、
 * 或托盘隐藏态忘了 show）这里必须变红，不是给绿灯装饰用的。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const {
  DEFAULT_HOTKEY,
  buildTrayMenuModel,
  decideWindowClose,
  describeShortcutRegistration,
  normalizeHotkey,
  planHotkeyToggle,
  planSecondInstanceReveal,
  shouldAbortSecondaryInstance,
  trayUpdateLabel,
} = require("../shell-policy.cjs");

// ── 单实例 ──────────────────────────────────────────────────────────────────

test("拿到锁的留下，没拿到锁的让位", () => {
  assert.equal(shouldAbortSecondaryInstance(true), false);
  assert.equal(shouldAbortSecondaryInstance(false), true);
  // undefined（没调用成功/异常）也必须让位：宁可这次没单实例，也不能双实例同跑
  assert.equal(shouldAbortSecondaryInstance(undefined), true);
});

test("第二实例信号：窗口最小化时要 restore + focus", () => {
  const plan = planSecondInstanceReveal({ hasWindow: true, minimized: true, visible: true });
  assert.equal(plan.create, false);
  assert.equal(plan.restore, true);
  assert.equal(plan.focus, true);
});

test("第二实例信号：窗口藏进托盘（不可见）时必须显式 show", () => {
  const plan = planSecondInstanceReveal({ hasWindow: true, minimized: false, visible: false });
  assert.equal(plan.show, true, "隐藏态不 show 就等于唤不回来");
  assert.equal(plan.restore, false);
  assert.equal(plan.focus, true);
});

test("第二实例信号：窗口本来在前排时只聚焦，不做多余动作", () => {
  const plan = planSecondInstanceReveal({ hasWindow: true, minimized: false, visible: true });
  assert.deepEqual(plan, { create: false, restore: false, show: false, focus: true });
});

test("第二实例信号：窗口不存在时重建，而不是对着 null 调方法", () => {
  const plan = planSecondInstanceReveal({ hasWindow: false, minimized: false, visible: false });
  assert.equal(plan.create, true);
  assert.equal(plan.focus, false);
});

// ── 托盘 / 关闭路由 ─────────────────────────────────────────────────────────

test("默认关窗进托盘而不是退出（这条红了=「关窗=退出」复活）", () => {
  assert.equal(decideWindowClose({ explicitQuit: false, trayAvailable: true }), "hide");
});

test("显式退出必须真退（这条红了=托盘把退出吞了，应用永远关不掉）", () => {
  assert.equal(decideWindowClose({ explicitQuit: true, trayAvailable: true }), "quit");
});

test("托盘不可用时关窗退回退出：不能造出看不见又关不掉的窗口", () => {
  assert.equal(decideWindowClose({ explicitQuit: false, trayAvailable: false }), "quit");
  assert.equal(decideWindowClose({ explicitQuit: true, trayAvailable: false }), "quit");
});

test("托盘菜单含显示主窗口/后端状态/退出三项，退出必须在这一份里", () => {
  const model = buildTrayMenuModel({ backendOnline: true, update: null });
  const ids = model.filter((item) => item.id).map((item) => item.id);
  assert.deepEqual(ids, ["show", "backend", "quit"]);
  const backend = model.find((item) => item.id === "backend");
  assert.match(backend.label, /运行中/);
  assert.equal(backend.enabled, false, "后端状态是只读展示，不该是个假按钮");
  assert.ok(!model.some((item) => item.id === "update"), "没查过更新时不出现更新行");
});

test("后端掉线后托盘菜单文案跟着变，更新行只随决策出现", () => {
  const model = buildTrayMenuModel({
    backendOnline: false,
    update: { action: "available", version: "0.9.0", install: false },
  });
  assert.match(model.find((item) => item.id === "backend").label, /未运行/);
  const update = model.find((item) => item.id === "update");
  assert.ok(update, "有决策就该在菜单里给一行状态");
  assert.match(update.label, /0\.9\.0/);
  assert.match(update.label, /不会自动安装/);
});

// ── 全局快捷键 ──────────────────────────────────────────────────────────────

test("默认组合 Control+Alt+B 原样通过", () => {
  const result = normalizeHotkey(DEFAULT_HOTKEY);
  assert.deepEqual(result, { ok: true, accelerator: "Control+Alt+B" });
});

test("大小写与空格被规范化：'ctrl + alt+b' 也是 Control+Alt+B", () => {
  assert.deepEqual(normalizeHotkey("ctrl + alt+b"), { ok: true, accelerator: "Control+Alt+B" });
});

test("裸单字符键被拒：全局吞一个 'B' 会让所有输入框打不出 b", () => {
  const result = normalizeHotkey("B");
  assert.equal(result.ok, false);
  assert.match(result.reason, /修饰键/);
});

test("裸功能键 F1–F24 放行，F25/乱写拒掉", () => {
  assert.deepEqual(normalizeHotkey("f9"), { ok: true, accelerator: "F9" });
  assert.equal(normalizeHotkey("F25").ok, false);
  assert.equal(normalizeHotkey("").ok, false);
  assert.equal(normalizeHotkey("Control+Alt").ok, false, "末位是修饰键=没有主键");
  assert.equal(normalizeHotkey("Control+Control+B").ok, false, "修饰键重复");
  assert.equal(normalizeHotkey("Hyper+B").ok, false, "不认识的修饰键");
  assert.equal(normalizeHotkey("Alt+§").ok, false, "不认识的主键");
  assert.equal(normalizeHotkey("Control+A-Z").ok, false, "键范围不是唤出组合");
});

test("注册三态：成功 / 被占用 / 抛异常，后两态都带降级措辞且不抛", () => {
  const good = describeShortcutRegistration("Control+Alt+B", true);
  assert.equal(good.status, "registered");
  assert.match(good.message, /Control\+Alt\+B/);

  const occupied = describeShortcutRegistration("Control+Alt+B", false);
  assert.equal(occupied.status, "occupied");
  assert.match(occupied.message, /占用/);
  assert.match(occupied.message, /降级/, "红了=占用时没给可操作的解释");

  const thrown = describeShortcutRegistration("Control+Alt+B", new Error("bad combo"));
  assert.equal(thrown.status, "error");
  assert.match(thrown.message, /bad combo/, "红了=异常细节被吞掉");
  assert.match(thrown.message, /降级/);
});

test("快捷键点击对窗口的三态：可见且聚焦→隐藏，其余→唤回", () => {
  assert.equal(planHotkeyToggle({ hasWindow: true, visible: true, focused: true }).hide, true);
  const revealHidden = planHotkeyToggle({ hasWindow: true, visible: false, focused: false });
  assert.equal(revealHidden.hide, false, "红了=对已隐藏的窗口再隐藏一次，永远唤不回");
  assert.equal(revealHidden.show, true);
  assert.equal(revealHidden.focus, true);
  assert.equal(planHotkeyToggle({ hasWindow: false }).create, true);
});

test("trayUpdateLabel 覆盖四种决策形态", () => {
  assert.match(trayUpdateLabel({ action: "none" }), /已是最新/);
  assert.match(trayUpdateLabel({ action: "unavailable" }), /不可用/);
  assert.match(trayUpdateLabel({ action: "available", version: "0.9.0" }), /0\.9\.0/);
  assert.match(trayUpdateLabel(null), /未检查/);
});

// ── 可测性前提 ──────────────────────────────────────────────────────────────

test("shell-policy.cjs 不依赖 electron（否则这套测试根本跑不起来）", () => {
  const source = readFileSync(path.join(__dirname, "..", "shell-policy.cjs"), "utf8");
  // 只扫真代码行：文件头的注释里本来就在说"禁止 require("electron")"，
  // 连注释一起匹会把自己写的规条当成违规，红得莫名其妙。
  const code = source
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .join("\n");
  assert.doesNotMatch(code, /require\(\s*["']electron["']\s*\)/);
});
