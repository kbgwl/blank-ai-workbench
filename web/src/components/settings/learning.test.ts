import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeCount,
  kindLabel,
  lintCount,
  originLabel,
  quoteText,
  statusLabel,
} from "./learning.ts";

test("状态与来源都有中文说法，认不出时保留原值而不是编一个", () => {
  assert.equal(statusLabel("active"), "生效中");
  assert.equal(statusLabel("candidate"), "候选");
  assert.equal(statusLabel("conflict"), "冲突停用");
  assert.equal(statusLabel("future"), "future");
  assert.equal(originLabel("explicit"), "你明说的");
  assert.equal(originLabel("harvested"), "从任务描述摘的");
  assert.equal(kindLabel("pitfall"), "坑");
  assert.equal(kindLabel("procedure"), "做法");
});

test("生效数只算 active；体检总数把两个库相加", () => {
  assert.equal(activeCount({ active: 3, candidate: 2, retired: 1 }), 3);
  assert.equal(activeCount({}), 0);
  assert.equal(lintCount([{ kind: "expired" }], [{ kind: "conflict" }]), 2);
  assert.equal(lintCount([], []), 0);
});

test("原话拼接去掉空项，不留下空分隔符", () => {
  assert.equal(quoteText([{ text: "先跑测试" }, { text: "" }, { text: "再改代码" }]), "先跑测试 / 再改代码");
  assert.equal(quoteText([]), "");
  assert.equal(quoteText([{ text: "  " }]), "");
});
