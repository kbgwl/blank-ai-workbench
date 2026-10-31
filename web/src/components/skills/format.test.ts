import assert from "node:assert/strict";
import { test } from "node:test";

import { countSkills, preview, skillState, skillSummary } from "./format.ts";

test("只认后端那三个状态；其余一律念「状态未知」", () => {
  assert.equal(skillState("active"), "active");
  assert.equal(skillState(" DRAFT "), "draft");
  assert.equal(skillState("disabled"), "disabled");
  assert.equal(skillState("archived"), "unknown");
  assert.equal(skillState(""), "unknown");
  assert.equal(skillState(null), "unknown");
});

test("未知状态不能兜底成 active：那会让用户以为一条没生效的偏好在生效", () => {
  assert.notEqual(skillState("跑飞了"), "active");
  assert.notEqual(skillState("跑飞了"), "draft");
});

test("说明折成一行：换行与连续空格都要压掉", () => {
  assert.equal(preview("先读代码\n\n再改动"), "先读代码 再改动");
  assert.equal(preview("   "), "");
  assert.equal(preview(null), "");
});

test("超长说明截断并加省略号，短说明原样返回", () => {
  assert.equal(preview("短"), "短");
  const long = "字".repeat(200);
  const cut = preview(long, 10);
  assert.equal(cut, `${"字".repeat(10)}…`);
  // 恰好等长不截断，否则每一行都会多出一个省略号。
  assert.equal(preview("字".repeat(10), 10), "字".repeat(10));
});

test("计数只数 active，未知状态不算已启用", () => {
  assert.deepEqual(countSkills(["active", "draft", "active", "odd"]), { active: 2, total: 4 });
  assert.deepEqual(countSkills([]), { active: 0, total: 0 });
});

test("摘要文案：空列表说「还没有技能」，不写「0 条已启用 / 共 0 条」", () => {
  assert.equal(skillSummary([]), "还没有技能");
  assert.equal(skillSummary(["active", "disabled"]), "1 条已启用 / 共 2 条");
});
