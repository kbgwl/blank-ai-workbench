import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { test } from "node:test";

import { buildBlocks } from "./timeline.ts";
import { hopCode, hopCopy, hopRepeat } from "./transition-copy.ts";

/**
 * 判据都钉在"拿掉实现就红"的位置：
 *
 * - 删掉 `buildBlocks` 里 `agent.transition` 那一支 → 第 1 条红（界面上又看不见了）；
 * - 删掉折叠 → 第 2 条红（连续三次变成三行）；
 * - 认不出时随便编一句话 → 第 3 条红；
 * - 后端加了 rule 而这里没登记 → 第 5 条红（它直接读后端的源码）。
 */

function event(seq: number, type: string, payload: Record<string, unknown> = {}) {
  return { seq, node: "", type, payload, createdAt: `2026-11-21T00:00:0${seq % 10}Z` };
}

function transition(seq: number, from: string, to: string, rule: string, why = "") {
  return event(seq, "agent.transition", { from, to, rule, why });
}

test("三段跳转在时间线上各成一行：待办 / 修复 / 驳回不再是同一个样子", () => {
  const blocks = buildBlocks([
    transition(1, "execute_action", "execute_action", "steps_remaining", "还有 3 项待办，继续执行"),
    transition(2, "run_validation", "execute_action", "validation_failed_retry", "校验没通过（failed），回执行修"),
    transition(3, "review_result", "execute_action", "review_rejected_retry", "审查驳回（2 条问题），退回执行修"),
  ]);
  const hops = blocks.filter((block) => block.kind === "hop");
  assert.equal(hops.length, 3);
  const labels = hops.map((block) => hopCopy(block.rule).label);
  assert.deepEqual(labels, ["还有待办，继续执行", "校验没过，回执行修", "审查驳回，退回执行"]);
  // 后端那句带数字的解释要能读到：只给标签的话，"修到第几轮了"还是不知道。
  assert.match(String(hops[1].why), /failed/);
});

test("连着的同一条跳转折成一行，写「连续 3 次」", () => {
  const blocks = buildBlocks([
    transition(1, "execute_action", "execute_action", "steps_remaining"),
    transition(2, "execute_action", "execute_action", "steps_remaining"),
    transition(3, "execute_action", "execute_action", "steps_remaining"),
  ]);
  const hops = blocks.filter((block) => block.kind === "hop");
  assert.equal(hops.length, 1);
  assert.equal(hops[0].repeat, 3);
  assert.equal(hopRepeat(hops[0]), "连续 3 次");
});

test("中间隔了动作的同一条跳转不折叠，但标出是第几次", () => {
  // 这是最常见的形态：跑一轮工具 → 校验没过 → 再跑一轮工具 → 校验又没过。
  // 折叠成一行会把中间那批工具的位置弄乱，所以保留两行，靠「第 2 次」说明在绕圈。
  const blocks = buildBlocks([
    transition(1, "run_validation", "execute_action", "validation_failed_retry"),
    event(2, "tool.completed", { tool: "run_tests", status: "ok", output: "1 failed" }),
    transition(3, "run_validation", "execute_action", "validation_failed_retry"),
  ]);
  const hops = blocks.filter((block) => block.kind === "hop");
  assert.equal(hops.length, 2);
  assert.equal(hops[0].repeat, 1);
  assert.equal(hopRepeat(hops[0]), "");
  assert.equal(hops[1].occurrence, 2);
  assert.equal(hopRepeat(hops[1]), "第 2 次");
});

test("没登记的 rule 不编理由：标黄、把原始码露出来", () => {
  const blocks = buildBlocks([
    transition(1, "review_result", "run_validation", "some_future_rule"),
  ]);
  const hops = blocks.filter((block) => block.kind === "hop");
  assert.equal(hops.length, 1);
  const copy = hopCopy(hops[0].rule);
  assert.equal(copy.registered, false);
  assert.equal(copy.tone, "warn");
  assert.equal(copy.label, "跳转原因没登记");
  // 原始码与方向要露出来，否则用户没法反查这一跳。
  assert.equal(hopCode(hops[0].rule), "some_future_rule");
  assert.equal(hops[0].from, "review_result");
  assert.equal(hopCode(""), "（空）");
});

test("老任务没有跳转事件时，不凭空长出一行", () => {
  const blocks = buildBlocks([
    event(1, "task.started", { workspace: "x" }),
    event(2, "tool.completed", { tool: "read_file", status: "ok", output: "ok" }),
  ]);
  assert.equal(blocks.filter((block) => block.kind === "hop").length, 0);
});

test("后端每一个 rule 都必须在本文件登记", () => {
  // 读后端源码而不是维护第二份手写清单：清单会跟实现一起漂移，源码不会。
  const here = dirname(fileURLToPath(import.meta.url));
  const source = resolve(
    here,
    "../../../../services/api/src/blank_api/services/agent/transition.py",
  );
  const text = readFileSync(source, "utf8");
  const rules = new Set<string>();
  const pattern = /_result\(\s*[^,]+,\s*[^,]+,\s*"([a-z_]+)"/g;
  for (const match of text.matchAll(pattern)) rules.add(match[1]);
  // 兜底那一支用的是常量 UNEXPLAINED = "unexplained"，单独补上。
  rules.add("unexplained");

  assert.ok(rules.size >= 20, `只认出 ${rules.size} 个 rule，正则可能没跟上后端写法`);
  const missing = [...rules].filter((rule) => !hopCopy(rule).registered);
  assert.deepEqual(missing, [], `后端有、界面没登记的 rule：${missing.join("、")}`);
});
