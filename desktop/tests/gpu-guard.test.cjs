"use strict";

/**
 * GPU 降级的判定规则。
 *
 * 这些用例守的是一件事：**一次异常退出不能变成永久降级**。
 * 旧实现的判据是 `previous.ok === false`，而 `reason` 只会写 `"starting"`，
 * 于是任何没走到「窗口就绪」的启动（强退、断电、装机时并发重写）都会让这台
 * 机器此后每次都跑软件渲染，且自己走不回来。
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { GPU_MAX_ATTEMPTS, readAttempts, shouldDowngrade, startingState, readyState } = require("../gpu-guard.cjs");

test("第一次启动（没有状态文件）不降级", () => {
  assert.equal(readAttempts({}), 0);
  assert.equal(shouldDowngrade({}), false);
});

test("上次正常走到窗口就绪，下次仍然用硬件加速", () => {
  const state = { ok: true, reason: "ready", attempts: 0 };
  assert.equal(readAttempts(state), 0);
  assert.equal(shouldDowngrade(state), false);
});

test("老格式的 starting 只算一次，不构成降级", () => {
  // 0.7.0 及更早写的就是这个形状，没有 attempts 字段。
  // 旧代码把它当成永久降级——这正是用户机器被锁在软件渲染上的原因。
  const legacy = { ok: false, reason: "starting", at: "2026-10-18T02:48:03.841Z" };
  assert.equal(readAttempts(legacy), 1);
  assert.equal(shouldDowngrade(legacy), false);
});

test("连续一次未就绪不够，两次才降级", () => {
  assert.equal(shouldDowngrade({ ok: false, attempts: 1 }), false);
  assert.equal(shouldDowngrade({ ok: false, attempts: GPU_MAX_ATTEMPTS }), true);
  assert.equal(shouldDowngrade({ ok: false, attempts: GPU_MAX_ATTEMPTS + 3 }), true);
});

test("BLANK_FORCE_GPU 能绕过降级记录再试一次", () => {
  const bad = { ok: false, attempts: 9 };
  assert.equal(shouldDowngrade(bad, true), false);
});

test("启动标记把次数加一", () => {
  const first = startingState({});
  assert.equal(first.ok, false);
  assert.equal(first.reason, "starting");
  assert.equal(first.attempts, 1);

  const second = startingState(first);
  assert.equal(second.attempts, 2);
  // 老格式接续也算一次
  assert.equal(startingState({ ok: false, reason: "starting" }).attempts, 2);
});

test("走到就绪就复位，降级状态能自己解除", () => {
  const bad = { ok: false, attempts: 5 };
  assert.equal(shouldDowngrade(bad), true);

  const recovered = readyState();
  assert.equal(recovered.ok, true);
  assert.equal(recovered.attempts, 0);
  assert.equal(shouldDowngrade(recovered), false, "降级启动成功一次之后，下次应当重新尝试硬件加速");
});

test("坏掉的 attempts 不会把机器判死或误判成健康", () => {
  // 写成 null / 负数 / 非数字时按「一次」处理：宁可多试一次，也不要永久降级。
  for (const broken of [null, -1, 0, "abc", NaN]) {
    assert.equal(readAttempts({ ok: false, attempts: broken }), 1, `attempts=${String(broken)}`);
  }
  // 传了小数就向下取整，不制造 1.5 次这种状态
  assert.equal(readAttempts({ ok: false, attempts: 2.7 }), 2);
});

test("状态不是对象时按首次启动处理", () => {
  for (const weird of [undefined, null, 0, "", []]) {
    assert.equal(readAttempts(weird), 0);
    assert.equal(shouldDowngrade(weird), false);
  }
});

test("完整链路：崩两次才降级，成功一次就恢复", () => {
  let state = {};
  // 第 1 次启动：写 starting(1)，然后崩了
  state = startingState(state);
  assert.equal(shouldDowngrade(state), false, "崩一次还应该继续用硬件加速");

  // 第 2 次启动：写 starting(2)，又崩了
  state = startingState(state);
  assert.equal(state.attempts, 2);
  assert.equal(shouldDowngrade(state), true, "连续两次没起来，才改用软件渲染");

  // 第 3 次：软件渲染起来了 → 复位
  state = readyState();
  assert.equal(shouldDowngrade(state), false, "成功一次之后重新优先硬件加速");
});
