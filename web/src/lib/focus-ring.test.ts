/**
 * 全局焦点环必须在 utilities 层里，且 specificity 为 0（见 globals.css 里那段注释）。
 *
 * 这条规则踩的坑和默认边框色一模一样，但更隐蔽：焦点环只在**键盘聚焦**时出现，
 * 平时截图看不到。裸写一条未分层的 `:focus-visible { outline: … }` 会盖掉组件上的
 * `focus:outline-none`——实测症状是输入框圆角卡片里被画出一个**方角**矩形，
 * 像素色 (143,143,143) 正好是品牌色 50% 叠白底，看上去像"框里又套了一个输入框"。
 *
 * 判据读源码：运行时到底谁赢由层与特异性决定，我们能固定住的只有源码形态。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "..", "app", "globals.css"), "utf8");

/**
 * 取全部 `@layer <name> { ... }` 块（同名 layer 可以出现多次，CSS 会合并）。
 * 按花括号配平，不靠正则猜。
 */
function layerBlocks(source: string): { name: string; start: number; end: number; body: string }[] {
  const blocks: { name: string; start: number; end: number; body: string }[] = [];
  const pattern = /@layer\s+([\w-]+)\s*\{/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) !== null) {
    const bodyStart = match.index + match[0].length;
    let depth = 1;
    let i = bodyStart;
    for (; i < source.length; i++) {
      if (source[i] === "{") depth += 1;
      else if (source[i] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    blocks.push({ name: match[1], start: match.index, end: i, body: source.slice(bodyStart, i) });
  }
  return blocks;
}

test("全局焦点环没有写成未分层规则", () => {
  // 未分层 > 任何 layer：写在外面就会盖掉所有 outline-none 工具类。
  assert.ok(
    !/(^|\n)\s*:focus-visible\s*\{/m.test(css),
    "globals.css 里出现了未分层的 `:focus-visible { … }`——它会盖掉组件上的 focus:outline-none",
  );
});

test("焦点环在 @layer utilities 里，并且是 :where 形态", () => {
  const at = css.indexOf(":where(:focus-visible)");
  assert.ok(at >= 0, "焦点环必须写成 `:where(:focus-visible)`：同层内靠特异性决胜，不加 :where 就压不住工具类");
  // 同名 layer 可能分成多块写（边框色一块、焦点环一块），只要它落在**某一块** utilities 里就算数。
  const inside = layerBlocks(css).some((block) => block.name === "utilities" && at > block.start && at < block.end);
  assert.ok(inside, "焦点环没有写在 @layer utilities 里——未分层规则会盖掉 outline-none");
  assert.ok(css.includes("outline-offset: 1px"), "焦点环要留 1px 间隙，不然贴着元素边框看不清");
  assert.ok(css.includes("var(--brand-color)"), "焦点环颜色应跟随品牌色");
});
