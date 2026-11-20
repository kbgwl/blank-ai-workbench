/**
 * 记忆面板的三件事都得说准。
 *
 * 这里唯一的硬要求是**别让用户以为记着**：容量那句说错了，用户会以为三个月前
 * 交代过的事还在——而写满之后最旧的段落已经被挤掉了，于是助手"忘了"，
 * 他还以为是谁的错。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { groupMemory, memoryKindLabel, memoryUsage } from "./memory.ts";
import type { ProjectMemory } from "./api.ts";

const memory = (over: Partial<ProjectMemory> = {}): ProjectMemory => ({
  exists: true,
  path: "data/memory/kbgai-abc.md",
  chars: 1000,
  limit: 12000,
  entries: [],
  text: "",
  ...over,
});

test("有类型标记就用人话，没标记就用后端认出来的前缀名", () => {
  assert.equal(memoryKindLabel({ kind: "user", label: "user" }), "你是谁");
  assert.equal(memoryKindLabel({ kind: "FEEDBACK", label: "" }), "做事方式");
  // 旧条目：kind 为空，后端按开头的「结果：」认成"任务结论"。
  assert.equal(memoryKindLabel({ kind: "", label: "任务结论" }), "任务结论");
  // 两样都没有才说未分类——那是真没线索，不是"分类未知"。
  assert.equal(memoryKindLabel({ kind: "", label: "" }), "未分类");
});

test("按段落分组，最新的段落排在最上面", () => {
  const groups = groupMemory([
    { section: "2026-11-18 · 旧", kind: "user", label: "user", text: "a" },
    { section: "2026-11-19 · 新", kind: "", label: "任务结论", text: "b" },
    { section: "2026-11-19 · 新", kind: "", label: "验证命令", text: "c" },
  ]);
  assert.deepEqual(
    groups.map((group) => group.section),
    ["2026-11-19 · 新", "2026-11-18 · 旧"],
  );
  // 同一段的两条留在一起：拆散之后看不出它们是因为同一件事记的。
  assert.equal(groups[0].entries.length, 2);
});

test("容量那句：快满了必须说出来，而且说清会怎样", () => {
  const empty = memoryUsage(memory({ exists: false, chars: 0, entries: [] }));
  assert.match(empty.text, /还没有记忆/);
  assert.equal(empty.tone, "plain");

  const half = memoryUsage(memory({ chars: 3000, limit: 12000 }));
  assert.match(half.text, /3000 \/ 12000/);
  assert.equal(half.tone, "plain");

  // 实测这份文件已经到 11962 / 12000：界面不能只写个百分比就算交代了。
  const full = memoryUsage(memory({ chars: 11962, limit: 12000 }));
  assert.equal(full.tone, "warn");
  assert.match(full.text, /最旧的挤掉/);
});

test("后端没给上限时不编一个数字", () => {
  const copy = memoryUsage(memory({ chars: 500, limit: 0 }));
  assert.ok(!copy.text.includes("0 /"), copy.text);
  assert.equal(copy.tone, "plain");
});
