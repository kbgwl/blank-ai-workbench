import assert from "node:assert/strict";
import { test } from "node:test";

import { keywordsOf, suggestSkill, type SkillLike } from "./skill-suggest.ts";

const review: SkillLike = {
  name: "code-review",
  display_name: "代码审查",
  description: "审查本次改动，按「会出错 / 应该改 / 可忽略」分级给结论",
  when_to_use: "用户说 review、看看有没有问题、合并前把关；或改动碰到鉴权、数据写入、并发、金额计算",
};

const audit: SkillLike = {
  name: "ui-audit",
  display_name: "页面体检",
  description: "页面逐项体检：宽度、对齐、字号档位、空态、重复入口、状态色是否一致",
  when_to_use: "用户说页面乱/不好看/对不齐；或一次改动了布局骨架、侧栏宽度、字号",
};

const debug: SkillLike = {
  name: "root-cause-debug",
  display_name: "根因定位",
  description: "先复现、再二分、后修根因；不允许“看着像”就改代码",
  when_to_use: "出现报错、测试失败、行为与预期不符，且原因不明",
};

test("命中两个以上关键词才提示", () => {
  const hit = suggestSkill("帮我 review 一下这次改动，看看有没有问题", [review]);
  assert.equal(hit?.name, "code-review");
  assert.ok(hit?.reason.includes("review"));
});

test("只命中一个关键词不提示：那多半是巧合", () => {
  // 只提到"review"一个词，没有任何其它线索。
  assert.equal(suggestSkill("review", [review]), null);
  assert.equal(suggestSkill("这个页面还行吧我看看", [audit]), null);
});

test("输入太短不猜", () => {
  assert.equal(suggestSkill("继续", [review]), null);
  assert.equal(suggestSkill("", [review]), null);
});

test("已经用过的技能不再提示", () => {
  const text = "帮我 review 一下这次改动，看看有没有问题";
  assert.equal(suggestSkill(text, [review], { used: ["code-review"] }), null);
});

test("多条都命中时只给分数最高的一条", () => {
  const text = "这个页面左边太宽了，字号也不一致，帮我看看布局骨架";
  const hit = suggestSkill(text, [review, audit, debug]);
  assert.equal(hit?.name, "ui-audit");
});

test("描述里的关键词也算数，不只看 when_to_use", () => {
  const hit = suggestSkill("跑测试失败三遍了，原因不明，行为与预期不符", [debug]);
  assert.equal(hit?.name, "root-cause-debug");
});

test("关键词切分去掉噪音词与连接词前缀", () => {
  const words = keywordsOf("用户说 review、或改动碰到鉴权");
  assert.ok(words.includes("review"));
  // 前缀动词要剥掉，否则用户说"碰到鉴权了"永远匹配不上"改动碰到鉴权"。
  assert.ok(words.includes("碰到鉴权"));
  assert.equal(words.includes("用户说"), false);
});
