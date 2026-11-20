/**
 * 触发记录的文案纪律：不说"每天"，不给必然 409 的按钮。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { triggerActions, triggerStatus, triggerTaskHref, triggerWhen } from "./trigger-view.ts";
import type { Trigger } from "./api.ts";

const row = (over: Partial<Trigger> = {}): Trigger => ({
  id: "t1",
  event_type: "user_task",
  source: "记一下明早提醒我交周报",
  status: "pending",
  fire_at: "2026-11-21T09:00:00",
  fired_at: "",
  expires_at: "",
  created_at: "2026-11-20T12:00:00",
  task_id: "abc",
  ...over,
});

test("只有真的跑过才写「跑于」，没跑的说「触发」", () => {
  const now = Date.parse("2026-11-20T12:00:00Z");
  assert.match(triggerWhen(row(), now), /触发/);
  assert.match(triggerWhen(row({ fired_at: "2026-11-21T09:00:00" }), now), /跑于/);
  // 过了点还没跑，是"等放行"，不能继续显示成"待触发"让人以为时间还没到。
  assert.match(triggerWhen(row({ fire_at: "2026-11-19T09:00:00" }), now), /已过点/);
  // 后端没给时间就不编一个。
  assert.equal(triggerWhen(row({ fire_at: "", fired_at: "" })), "");
});

test("已经跑完的不再给「立即触发」——那是个必然 409 的按钮", () => {
  assert.deepEqual(triggerActions(row({ status: "pending" })), ["fire", "pause", "cancel"]);
  assert.deepEqual(triggerActions(row({ status: "paused" })), ["resume", "cancel"]);
  assert.deepEqual(triggerActions(row({ status: "fired" })), []);
  assert.deepEqual(triggerActions(row({ status: "expired" })), []);
  assert.deepEqual(triggerActions(row({ status: "cancelled" })), []);
});

test("状态有中文说法，没见过的状态原样保留不猜", () => {
  assert.equal(triggerStatus("fired").label, "已触发");
  assert.equal(triggerStatus("skipped").label, "已跳过");
  assert.equal(triggerStatus("weird_new_status").label, "weird_new_status");
});

test("没有 task_id 就别说看任务，那是点不动的链接", () => {
  assert.match(triggerTaskHref(row()), /tasks\/detail/);
  assert.equal(triggerTaskHref(row({ task_id: "" })), "");
});
