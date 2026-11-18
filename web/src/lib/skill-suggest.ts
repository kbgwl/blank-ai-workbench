/**
 * 技能提示：这一轮说的话，像不像某条技能正好覆盖的活。
 *
 * 对齐 Claude Code 回复末尾的 `[suggestion:/deploy] staging`：只给**一条**，
 * 只在把握足够时给，不打断、不重复说"你可以用技能"。
 *
 * 判定刻意保守：
 * - 输入太短（< 8 字）不猜——"继续""好的"这种话对不上任何技能；
 * - 至少命中**两个**关键词才算，只中一个多半是巧合（"页面"人人都会说）；
 * - 只取分数最高的一条，不出列表。给三条等于让用户自己再挑一次。
 */

export type SkillLike = {
  name: string;
  display_name: string;
  description: string;
  when_to_use: string;
};

export type SkillSuggestion = {
  name: string;
  display: string;
  /** 命中的关键词，用来解释"为什么是它"。 */
  reason: string;
};

/** 太通用的词不当关键词：它们几乎出现在每句话里，命中也不算证据。 */
const NOISE = new Set([
  "用户",
  "需要",
  "时候",
  "任务",
  "问题",
  "一次",
  "这个",
  "那个",
  "可以",
  "用户说",
  "或一次",
]);

const MIN_INPUT = 8;
const MIN_HITS = 2;

/**
 * 短语开头常见的连接词与动词。
 *
 * 切出来的段落带着它们就没法匹配——用户说的是"布局骨架乱了"，
 * 而清单里写的是"或一次改动了布局骨架"，不剥掉前缀就永远对不上。
 */
const LEADING = /^(或|和|与|及|以及|包括|比如|并且|且|用户说|一次|改动了|改动|出现|看看|帮忙|需要)+/;

/** 从 when_to_use / description 里切出可用于匹配的短语。 */
export function keywordsOf(text: string): string[] {
  if (!text) return [];
  const parts = text
    .toLowerCase()
    // 标点与连接词都当分隔：`用户说 review、看看有没有问题；或改动碰到鉴权`
    .split(/[，,、;；/|（）()【】\[\]\s]+/)
    .map((part) => part.replace(LEADING, "").trim())
    .filter((part) => part.length >= 2 && part.length <= 12)
    .filter((part) => !NOISE.has(part));
  return [...new Set(parts)];
}

/** 这一轮该不该提示某条技能。返回 null 表示不提（绝大多数情况都是 null）。 */
export function suggestSkill(
  text: string,
  items: SkillLike[],
  options: { used?: Iterable<string> } = {},
): SkillSuggestion | null {
  const source = (text || "").trim().toLowerCase();
  if (source.length < MIN_INPUT) return null;
  const used = new Set(options.used ?? []);

  let best: { item: SkillLike; hits: number; matched: string[] } | null = null;
  for (const item of items) {
    // 已经用过的技能不再提示：这一轮已经照它做过了。
    if (used.has(item.name)) continue;
    const keywords = [...keywordsOf(item.when_to_use), ...keywordsOf(item.description)];
    const matched = keywords.filter((word) => source.includes(word));
    if (matched.length < MIN_HITS) continue;
    if (!best || matched.length > best.hits) best = { item, hits: matched.length, matched };
  }
  if (!best) return null;
  return {
    name: best.item.name,
    display: best.item.display_name || best.item.name,
    reason: best.matched.slice(0, 2).join(" / "),
  };
}
