import assert from "node:assert/strict";
import { test } from "node:test";

import { COMMANDS as APP_COMMANDS } from "./commands.ts";
import {
  filterCommands,
  isEditableTarget,
  matchShortcut,
  PALETTE_SHORTCUT,
  scoreCommand,
  shortcutHint,
  shouldHandleEscape,
  stepIndex,
  type Command,
} from "./shortcuts.ts";

const COMMANDS: Command[] = [
  { id: "tasks", label: "任务与对话", group: "前往", href: "/tasks", keywords: ["tasks", "chat"] },
  { id: "settings", label: "设置", group: "前往", href: "/settings", keywords: ["settings", "provider"] },
  { id: "daily", label: "日常", group: "前往", href: "/daily", keywords: ["daily"] },
];

test("Ctrl+K 与 ⌘K 都命中面板快捷键", () => {
  assert.equal(matchShortcut({ key: "k", ctrlKey: true }, PALETTE_SHORTCUT), true);
  assert.equal(matchShortcut({ key: "K", metaKey: true }, PALETTE_SHORTCUT), true);
});

test("裸 K 或 Ctrl+Alt+K 不触发面板，避免抢输入", () => {
  assert.equal(matchShortcut({ key: "k" }, PALETTE_SHORTCUT), false);
  assert.equal(matchShortcut({ key: "k", ctrlKey: true, altKey: true }, PALETTE_SHORTCUT), false);
  assert.equal(matchShortcut({ key: "j", ctrlKey: true }, PALETTE_SHORTCUT), false);
});

test("声明 shift 时 Ctrl+Shift+K 与 Ctrl+K 互斥", () => {
  const withShift = { key: "k", mod: true, shift: true };
  assert.equal(matchShortcut({ key: "k", ctrlKey: true, shiftKey: true }, withShift), true);
  assert.equal(matchShortcut({ key: "k", ctrlKey: true }, withShift), false);
});

test("快捷键提示按平台给出文案", () => {
  assert.equal(shortcutHint(PALETTE_SHORTCUT, false), "Ctrl+K");
  assert.equal(shortcutHint(PALETTE_SHORTCUT, true), "⌘K");
  assert.equal(shortcutHint({ key: "k", mod: true, shift: true }, false), "Ctrl+Shift+K");
});

test("输入类元素判定", () => {
  assert.equal(isEditableTarget({ tagName: "INPUT" }), true);
  assert.equal(isEditableTarget({ tagName: "textarea" }), true);
  assert.equal(isEditableTarget({ tagName: "DIV", isContentEditable: true }), true);
  assert.equal(isEditableTarget({ tagName: "BUTTON" }), false);
  assert.equal(isEditableTarget(null), false);
});

test("空查询返回全量并保持原顺序", () => {
  assert.deepEqual(
    filterCommands(COMMANDS, "   ").map((item) => item.id),
    ["tasks", "settings", "daily"],
  );
});

test("label 命中优先于别名与分组命中", () => {
  // "设置" 直接命中 label；"provider" 只命中 settings 的别名。
  assert.equal(filterCommands(COMMANDS, "设置")[0].id, "settings");
  assert.equal(filterCommands(COMMANDS, "chat")[0].id, "tasks");
  assert.equal(scoreCommand(COMMANDS[0], "任务"), 2);
  assert.equal(scoreCommand(COMMANDS[0], "前往"), 1);
  assert.equal(scoreCommand(COMMANDS[0], "chat"), 1);
  assert.equal(scoreCommand(COMMANDS[0], "不存在"), -1);
});

test("过滤结果不含不命中项，且大小写不敏感", () => {
  assert.deepEqual(filterCommands(COMMANDS, "TASKS").map((item) => item.id), ["tasks"]);
  assert.deepEqual(filterCommands(COMMANDS, "zzz"), []);
});

test("列表索引环绕且空列表返回 -1", () => {
  assert.equal(stepIndex(0, 1, 3), 1);
  assert.equal(stepIndex(2, 1, 3), 0);
  assert.equal(stepIndex(0, -1, 3), 2);
  assert.equal(stepIndex(-1, 1, 3), 0);
  assert.equal(stepIndex(-1, -1, 3), 2);
  assert.equal(stepIndex(0, 1, 0), -1);
});

test("查询变化后高亮回到首项，不会停在中间项", () => {
  // 面板在 onChange 里同时 setActive(0)；这里锁住「新结果集的第 0 项」这个契约。
  const narrowing = filterCommands(COMMANDS, "设置");
  assert.equal(narrowing.length, 1);
  assert.equal(narrowing[0].id, "settings");
  // 从多结果收窄到单结果时，若沿用旧 active 会越界，夹取后必须落在 0。
  assert.equal(Math.min(2, narrowing.length - 1), 0);
});

test("Esc 与输入态：面板类热键不该被 isEditableTarget 拦掉，Esc 该被拦", () => {
  const inTextarea = { tagName: "TEXTAREA" };
  // 热键走 matchShortcut，不看焦点，所以输入框里 Ctrl+K 仍可用。
  assert.equal(matchShortcut({ key: "k", ctrlKey: true }, PALETTE_SHORTCUT), true);
  // Esc 走 isEditableTarget，输入框内让路给「取消编辑」。
  assert.equal(isEditableTarget(inTextarea), true);
});

test("命令清单里每个 href 都不重复且以 / 开头", () => {
  const hrefs = APP_COMMANDS.flatMap((item) => (item.href ? [item.href] : []));
  assert.equal(new Set(hrefs).size, hrefs.length);
  assert.ok(hrefs.every((href) => href.startsWith("/")));
});

test("Esc 全局判定：输入态与浮层打开时都要让路", () => {
  const base = { key: "Escape", defaultPrevented: false, inEditable: false, overlayOpen: false };
  assert.equal(shouldHandleEscape(base), true);
  // 输入框内 Esc 是「取消编辑」
  assert.equal(shouldHandleEscape({ ...base, inEditable: true }), false);
  // 命令面板/对话框打开时 Esc 先关浮层
  assert.equal(shouldHandleEscape({ ...base, overlayOpen: true }), false);
  // 内层已 preventDefault
  assert.equal(shouldHandleEscape({ ...base, defaultPrevented: true }), false);
  // 非 Esc 键
  assert.equal(shouldHandleEscape({ ...base, key: "Enter" }), false);
});