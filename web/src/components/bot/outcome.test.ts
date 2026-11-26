import assert from "node:assert/strict";
import { test } from "node:test";

import { outcomeParts, turnOutcome } from "./outcome.ts";

test("状态词只把已知终态说成完成，认不出的不冒充", () => {
  assert.deepEqual(turnOutcome("succeeded", false), { label: "完成", tone: "accent" });
  assert.deepEqual(turnOutcome("failed", false), { label: "失败", tone: "danger" });
  assert.deepEqual(turnOutcome("waiting_approval", false), { label: "等你批准", tone: "warn" });
  assert.deepEqual(turnOutcome("succeeded", true), { label: "正在做", tone: "accent" });
  assert.deepEqual(turnOutcome("mystery", false), { label: "已结束", tone: "muted" });
});

test("摘要只列有值的项，失败单独标红", () => {
  const parts = outcomeParts(
    { looked: 3, files: 2, commandCount: 1, failed: 1, calls: 6 },
    6,
  );
  assert.deepEqual(parts, [
    { text: "看了 3", tone: "muted" },
    { text: "改了 2 个文件", tone: "muted" },
    { text: "跑了 1 条命令", tone: "muted" },
    { text: "1 次失败", tone: "danger" },
  ]);
});

test("一次工具调用都没有时退回步数，不出现空摘要", () => {
  assert.deepEqual(
    outcomeParts({ looked: 0, files: 0, commandCount: 0, failed: 0, calls: 0 }, 2),
    [{ text: "2 步", tone: "muted" }],
  );
  assert.deepEqual(
    outcomeParts({ looked: 0, files: 0, commandCount: 0, failed: 0, calls: 0 }, 0),
    [],
  );
});
