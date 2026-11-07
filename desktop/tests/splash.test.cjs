"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  SPLASH_MIN_MS,
  SPLASH_MAX_MS,
  SPLASH_FADE_MS,
  MAIN_WINDOW_SIZE,
  decideSplashReveal,
} = require("../splash-policy.cjs");

const mainSource = fs.readFileSync(path.join(__dirname, "..", "main.cjs"), "utf8");

const base = {
  mainReady: true,
  minElapsed: false,
  skipped: false,
  forced: false,
  failed: false,
  revealed: false,
};

test("阈值：动画播得完，兜底又不会把用户锁住", () => {
  assert.equal(SPLASH_MIN_MS, 2600, "最短展示要覆盖 2.6 秒的四拍动画");
  assert.equal(SPLASH_MAX_MS, 20000, "后端卡死时最多等 20 秒就进应用");
  assert.ok(SPLASH_MIN_MS < SPLASH_MAX_MS);
});

test("主窗口没就绪时一律 hold：不能单独销毁启动屏", () => {
  for (const extra of [
    { minElapsed: true },
    { skipped: true },
    { forced: true },
    { failed: true },
    { minElapsed: true, skipped: true, forced: true, failed: true },
  ]) {
    assert.equal(
      decideSplashReveal({ ...base, mainReady: false, ...extra }),
      "hold",
      `主窗口未就绪时 ${JSON.stringify(extra)} 也不能收幕`,
    );
  }
});

test("只满足「主窗口就绪」还不够揭幕，得等动画播完", () => {
  assert.equal(decideSplashReveal(base), "hold");
});

test("四条放行路径：最短时长到 / 用户跳过 / 最长兜底 / 启动屏自己加载失败", () => {
  for (const key of ["minElapsed", "skipped", "forced", "failed"]) {
    assert.equal(
      decideSplashReveal({ ...base, [key]: true }),
      "reveal",
      `${key} 为真时应该收幕`,
    );
  }
});

test("已经收过幕就幂等，不重复 show / 不重复 destroy", () => {
  assert.equal(decideSplashReveal({ ...base, minElapsed: true, revealed: true }), "already");
});

test("空状态与缺省字段都不会意外揭幕", () => {
  assert.equal(decideSplashReveal({}), "hold");
  assert.equal(decideSplashReveal(null), "hold");
  assert.equal(decideSplashReveal({ mainReady: true }), "hold");
});

test("启动屏与主窗口共用同一组尺寸", () => {
  assert.deepEqual(MAIN_WINDOW_SIZE, { width: 1280, height: 820 });
  assert.ok(Object.isFrozen(MAIN_WINDOW_SIZE), "共用常量不该被运行时改掉");
  // 两个 BrowserWindow 构造都必须展开它：只改一处就会重新出现「小卡片变大窗口」。
  const uses = mainSource.match(/\.\.\.MAIN_WINDOW_SIZE/g) ?? [];
  assert.equal(uses.length, 2, "createSplash 与 createWindow 都要展开共用尺寸");
  assert.doesNotMatch(mainSource, /width:\s*440\s*,/, "旧的 440×320 小卡片不能回来");
  assert.doesNotMatch(mainSource, /height:\s*320\s*,/);
  assert.ok(SPLASH_FADE_MS > 0 && SPLASH_FADE_MS < 600, "收幕淡出要短，别拖成第二次等待");
});

test("收幕先对齐外框、再显示主窗口、最后窗口级淡出", () => {
  assert.match(mainSource, /splash\.setBounds\(win\.getBounds\(\)\)/, "两个窗口要完全重合");
  assert.match(mainSource, /win\.setOpacity\(/, "必须淡出整个窗口，不能只淡网页 body");
  assert.doesNotMatch(
    mainSource,
    /classList\.add\('out'\)/,
    "只淡 body 会留下不透明的窗口底色，是先前那块纯色矩形的来源",
  );
  assert.match(mainSource, /win\.setIgnoreMouseEvents\(true\)/, "淡出期间不该吃掉主窗口点击");
  const showAt = mainSource.search(/win\.show\(\);\s+win\.focus\(\);/);
  const closeAt = mainSource.search(/closeSplash\(\);\s+\}/);
  assert.ok(showAt > 0 && closeAt > showAt, "顺序不能反：主窗口先可见，再销毁启动屏");
});

test("启动屏等首帧才显示，且不把「显示」当成启动前置条件", () => {
  assert.match(mainSource, /once\("ready-to-show", showOnce\)/);
  assert.match(mainSource, /setTimeout\(showOnce, 500\)/, "ready-to-show 不来也要有兜底");
  assert.match(mainSource, /show: false,[\s\S]{0,80}backgroundColor/, "启动屏首帧前要藏起来");
});
