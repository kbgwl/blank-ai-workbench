import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeQuestions } from "./api.ts";

// 推荐项要从载荷里活着走到界面上。断掉的后果不是"少个徽标"：
// 用户看不到哪个是默认值，却又会被告知"不选就按默认继续"——等于让他盲签。
test("推荐项穿过归一化，界面才标得出哪个是默认值", () => {
  const rows = normalizeQuestions({
    questions: [
      {
        id: "name",
        question: "公司叫什么名字？",
        options: [
          { label: "星河科技", description: "先用这个", recommended: true },
          { label: "我另给一个" },
        ],
      },
    ],
  });

  assert.equal(rows.length, 1);
  assert.equal(rows[0].options[0].recommended, true);
  assert.equal(rows[0].options[1].recommended, false);
});

// 老载荷没有这个字段：不能猜成 true，否则"跳过"会出现在不该出现的地方。
test("缺字段就是没有推荐项", () => {
  const rows = normalizeQuestions({
    questions: [{ id: "phone", question: "真实电话？", options: [{ label: "我填一个" }] }],
  });
  assert.equal(rows[0].options[0].recommended, false);
});
