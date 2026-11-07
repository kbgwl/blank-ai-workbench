"use strict";

/**
 * 启动屏收幕时序。
 *
 * 为什么抽成纯函数：开机屏最容易出的两类事故都不是「不好看」，而是
 *  1) 主窗口还没就绪就把启动屏销毁 → window-all-closed 触发，后端被回收、应用自杀；
 *  2) 后端迁移卡住 → 启动屏永远不收，用户对着动画以为程序死了。
 * 时序判定必须能被单测钉住，主进程只负责照着执行。
 */
const SPLASH_MIN_MS = 2600;
const SPLASH_MAX_MS = 20000;

/**
 * 启动屏与主窗口必须共用同一组尺寸。曾经启动屏是 440×320 的小卡片、主窗口
 * 1280×820，揭幕那一刻会「小卡片浮在大窗口上 → 突然变大」，中间还有一帧
 * 两个窗口叠在一起。把这个常量同时展开进两个 BrowserWindow，尺寸就再也漂不了。
 */
const MAIN_WINDOW_SIZE = Object.freeze({ width: 1280, height: 820 });

/** 收幕用窗口级淡出，不是只淡出网页：网页淡出后窗口自己的底色仍是不透明色块。 */
const SPLASH_FADE_MS = 260;

/**
 * @param {object} state
 * @param {boolean} state.mainReady  主窗口已走到 ready-to-show
 * @param {boolean} state.minElapsed 最短展示时长已到
 * @param {boolean} state.skipped    用户点了跳过
 * @param {boolean} state.forced     最长兜底已到（后端启动异常慢）
 * @param {boolean} state.failed     启动屏页面自身加载失败
 * @param {boolean} state.revealed   已经收过幕
 * @returns {"reveal" | "hold" | "already"}
 */
function decideSplashReveal(state) {
  const {
    mainReady = false,
    minElapsed = false,
    skipped = false,
    forced = false,
    failed = false,
    revealed = false,
  } = state ?? {};
  if (revealed) return "already";
  // 主窗口没就绪时绝不单独销毁启动屏：那会让它成为「最后一个窗口」。
  if (!mainReady) return "hold";
  if (failed || skipped || forced || minElapsed) return "reveal";
  return "hold";
}

module.exports = {
  SPLASH_MIN_MS,
  SPLASH_MAX_MS,
  SPLASH_FADE_MS,
  MAIN_WINDOW_SIZE,
  decideSplashReveal,
};
