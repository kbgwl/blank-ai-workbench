"use strict";

/**
 * GPU 降级的判定。
 *
 * 这段逻辑原来写在 main.cjs 里，但只要 `previous.ok === false` 就降级，而
 * `reason` 只会写 `"starting"`（从没有 `"fail"`）——于是**一次没走到「窗口就绪」
 * 的启动**就足以让这台机器此后每次都强制软件渲染。而真到了软件渲染也起不来的
 * 机器上，它就再没机会回到硬件加速：降级状态自己不会解除。
 *
 * 正确的问题不是「上次成没成功」，而是「连续失败了几次」。一次不算：用户强退、
 * 断电、装包时并发重写文件，都会留下一个没走到 ready 的标记，而它们的下一次
 * 启动其实完全正常。
 *
 * 抽成独立文件是为了能在 Electron 之外直接测——main.cjs 一旦 require('electron')
 * 就跑不进普通的 node 测试。
 */

/** 连续多少次「启动未就绪」才判定 GPU 不可用。 */
const GPU_MAX_ATTEMPTS = 2;

/**
 * 上次启动留下了多少「未就绪」的账。
 *
 * - 没有记录 / 上次 ok：0
 * - 带 attempts：用它
 * - 老格式（只有 `ok:false`，没有 attempts）：算 1 次
 *   兼容用：0.7.0 及更早只写 `{ok:false, reason:"starting"}`，而那份记录在旧代码里
 *   等同于永久降级。按 1 次算，它就不足以触发降级，用户能自己走出来。
 */
function readAttempts(state) {
  if (!state || typeof state !== "object") return 0;
  if (state.ok === true) return 0;
  // 从没写过状态（readGpuState 读不到文件时给的是 `{}`）→ 0。
  // 这一条不能少：空对象如果落进下面的分支，会被当成「一次失败」，
  // 于是首次启动就先记成 1、再 +1，变成 2 —— 新装的机器第一崩就降级。
  if (state.ok === undefined && state.attempts === undefined) return 0;
  const attempts = Number(state.attempts);
  return Number.isFinite(attempts) && attempts > 0 ? Math.floor(attempts) : 1;
}

/**
 * 这次要不要降级到软件渲染。
 * `forceGpu` 对应 BLANK_FORCE_GPU=1：换了机器或更新了驱动之后，用它强行再试一次。
 */
function shouldDowngrade(state, forceGpu = false) {
  return !forceGpu && readAttempts(state) >= GPU_MAX_ATTEMPTS;
}

/** 启动前落盘的标记：+1 次，等窗口就绪再改写成 readyState。 */
function startingState(previous, now = new Date()) {
  return {
    ok: false,
    reason: "starting",
    attempts: readAttempts(previous) + 1,
    at: now.toISOString(),
  };
}

/**
 * 走到「窗口就绪」才算这次启动成立，计数归零。
 * 降级那次成功也走这里 —— 降级不是判决，只是一次临时措施。
 */
function readyState(now = new Date()) {
  return { ok: true, reason: "ready", attempts: 0, at: now.toISOString() };
}

module.exports = { GPU_MAX_ATTEMPTS, readAttempts, shouldDowngrade, startingState, readyState };
