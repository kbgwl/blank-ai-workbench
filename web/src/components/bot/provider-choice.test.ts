import assert from "node:assert/strict";
import { test } from "node:test";

import { usableProviderId } from "./provider-choice.ts";

test("存过的供应商还在，照旧用它", () => {
  assert.equal(usableProviderId("p-glm", ["p-glm", "p-openai"]), "p-glm");
});

test("供应商已被删掉：不再沿用这个悬空 id", () => {
  // 真实场景：localStorage 里留着上一次测试用的临时 provider，界面显示「默认模型」，
  // 任务却带着这个 id 落库，一运行就报「模型供应商不存在」。
  assert.equal(usableProviderId("p-deleted", ["p-glm"]), "");
});

test("从没选过就是空，交给后端按默认选", () => {
  assert.equal(usableProviderId("", ["p-glm"]), "");
});

test("列表为空时也不沿用旧 id：宁可回到默认，也不带一个看不见的选择下去", () => {
  assert.equal(usableProviderId("p-glm", []), "");
});
