import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanPath, isNavActive, navPath, NAV_ITEMS, TURN_TONE_LABEL, UTILITY_LINKS, turnTone } from "./nav.ts";

test("常驻导航只留工作区；辅助入口统一收进设置页，不许再摆回侧栏", () => {
  assert.deepEqual(NAV_ITEMS.map((item) => item.id), ["workspace"]);
  for (const item of NAV_ITEMS) {
    assert.ok(item.label, `${item.id} 必须有中文标签，否则侧栏上是个空按钮`);
  }
  // 收进设置页的四项仍要可达：监控/证据库/技能/连接器一个都不能丢。
  assert.deepEqual(UTILITY_LINKS.map((item) => item.href), [
    "/monitor",
    "/evidence",
    "/skills",
    "/settings#tools",
  ]);
  for (const item of UTILITY_LINKS) {
    assert.ok(item.label && item.hint, `${item.href} 在设置页里必须是带说明的入口`);
  }
});

test("档位标签必须覆盖全部四色状态点", () => {
  assert.deepEqual(Object.keys(TURN_TONE_LABEL).sort(), ["done", "failed", "idle", "running"]);
});

test("带 hash 的入口按路径高亮：连接器指向设置页的 tools 分区", () => {
  assert.equal(navPath("/settings#tools"), "/settings");
  assert.equal(navPath("/evidence?days=7"), "/evidence");
  assert.equal(isNavActive("/settings", "/settings#tools"), true);
});

test("静态导出的 /index.html 与开发态的 / 是同一个首页", () => {
  assert.equal(cleanPath("/index.html"), "/");
  assert.equal(cleanPath("/index"), "/");
  assert.equal(cleanPath(""), "/");
  assert.equal(cleanPath(null), "/");
  assert.equal(isNavActive("/index.html", "/"), true);
});

test("子路径算在父级导航里，但前缀相像的不同目录不算", () => {
  assert.equal(isNavActive("/projects/123", "/projects"), true);
  assert.equal(isNavActive("/settings/tools", "/settings"), true);
  // /skillsx 不是 /skills 的子路径。
  assert.equal(isNavActive("/skillsx", "/skills"), false);
  assert.equal(isNavActive("/evidence", "/skills"), false);
});

test("状态点：空串是没说过话，不是已完成", () => {
  assert.equal(turnTone(""), "idle");
  assert.equal(turnTone(null), "idle");
  assert.equal(turnTone(undefined), "idle");
  assert.equal(turnTone("draft"), "idle");
});

test("状态点：运行中、失败与取消、完成分成三色", () => {
  assert.equal(turnTone("running"), "running");
  assert.equal(turnTone("  Running "), "running");
  assert.equal(turnTone("failed"), "failed");
  assert.equal(turnTone("cancelled"), "failed");
  assert.equal(turnTone("succeeded"), "done");
});

test("认不出的状态归 idle：不要把未知说成成功", () => {
  assert.equal(turnTone("weird-state"), "idle");
  assert.equal(turnTone("半途重启"), "idle");
});
