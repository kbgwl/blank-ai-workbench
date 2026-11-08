import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanPath, isNavActive, navPath, NAV_ITEMS, turnTone } from "./nav.ts";

test("导航顺序就是设计稿的信息架构，少一项或多一项都要被这里挡住", () => {
  assert.deepEqual(
    NAV_ITEMS.map((item) => item.id),
    ["workspace", "monitor", "evidence", "skills", "connectors"],
  );
  for (const item of NAV_ITEMS) {
    assert.ok(item.label, `${item.id} 必须有中文标签，否则侧栏上是个空按钮`);
  }
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
  assert.equal(isNavActive("/skills", "/skills"), true);
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
