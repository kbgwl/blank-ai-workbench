/**
 * 默认边框色的三条硬约束（见 globals.css 里那段注释）。
 *
 * 这条规则看起来是「一行 CSS」，实际有三个独立的坑，每个都能让它**静默失效**：
 * 类名在 DOM 上、元素面板显示匹配、浏览器算出来却是灰的。改坏它不会有任何报错，
 * 只会让全站 140 多处彩色边框一起变灰，而且下一次看页面时大概率以为「本来就这样」。
 *
 * 所以判据直接读源码文本。不用跑浏览器：运行时行为由 Tailwind 决定，
 * 我们能控制、也必须固定住的只有源码形态。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "..", "app", "globals.css"), "utf8");

/** 取 `@layer <name> { ... }` 的块内容（按花括号配平，不靠正则猜）。 */
function layerBody(source: string, name: string): string | null {
  const head = `@layer ${name} {`;
  const start = source.indexOf(head);
  if (start < 0) return null;
  let depth = 0;
  for (let i = start + head.length - 1; i < source.length; i++) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start + head.length, i);
    }
  }
  return null;
}

test("默认边框色写在 @layer utilities 里，不是裸规则", () => {
  // 裸 `* { border-color }` 是未分层，优先级高于任何 layer，会盖掉全部 border-* 工具类。
  assert.ok(
    !/(^|\n)\s*\*\s*\{[^}]*border-color/m.test(css),
    "globals.css 里出现了未分层的 `* { border-color }`——它会盖掉所有 border-* 颜色工具类",
  );

  const utilities = layerBody(css, "utilities");
  assert.ok(utilities, "没有找到 @layer utilities 块");
  assert.match(utilities, /border-color:\s*var\(--line\)/, "utilities 层里没有默认边框色");
});

test("默认边框色不用 @layer base（会被剥壳成裸规则）", () => {
  // Turbopack 侧会把 @layer base 的外壳去掉，把内容提成未分层规则——等于没分层。
  const base = layerBody(css, "base");
  assert.ok(
    !base || !/border-color:\s*var\(--line\)/.test(base),
    "写进 @layer base 会被剥壳成裸规则；默认边框色必须放 utilities 层",
  );
});

test("选择器压到 specificity 0（:where），与工具类同层时不抢", () => {
  const utilities = layerBody(css, "utilities") ?? "";
  const rule = utilities.slice(utilities.indexOf("border-color: var(--line)"));
  const selector = utilities.slice(0, utilities.indexOf("border-color: var(--line)"));
  const lastSelector = selector.slice(selector.lastIndexOf("}") + 1);

  assert.match(lastSelector, /:where\(\*\)/, "默认边框色的选择器必须用 :where(*) 压到 specificity 0");
  assert.ok(rule.length > 0);
});

test("伪元素写成 :where(*)::before，不写 :where(::before)", () => {
  const utilities = layerBody(css, "utilities") ?? "";
  // :where() 里塞伪元素会被 lightningcss 判成无效选择器，压成 `:where()`——等于这条没了。
  assert.ok(
    !utilities.includes(":where(::before)"),
    ":where(::before) 会被压缩器丢掉；写成 :where(*)::before",
  );
  assert.match(utilities, /:where\(\*\)::before/);
  assert.match(utilities, /:where\(\*\)::after/);
});
