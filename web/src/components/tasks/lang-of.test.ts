/**
 * 语言判定：文件预览和 Diff 面板共用同一份。
 *
 * 为什么单独测它：两处各自猜一次，同一个 `.tsx` 会先显示成两种颜色——
 * 那不是"风格差异"，是同一个文件在两个地方长得不一样。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { langOf } from "./format.ts";

test("常见扩展名映射到高亮器认识的语言", () => {
  const cases: [string, string][] = [
    ["src/a.ts", "ts"],
    ["src/a.tsx", "tsx"],
    ["src/a.js", "js"],
    ["scripts/run.py", "py"],
    ["index.html", "html"],
    ["logo.svg", "xml"],
    ["style.css", "css"],
    ["package.json", "json"],
    ["build.sh", "bash"],
    ["config.yml", "yaml"],
  ];
  for (const [path, expected] of cases) {
    assert.equal(langOf(path), expected, path);
  }
});

test("大写扩展名与大写路径也认", () => {
  assert.equal(langOf("Deliverables/INDEX.HTML"), "html");
  assert.equal(langOf("App.TSX"), "tsx");
});

test("认不出来的扩展名回落 ts，而不是抛错", () => {
  // 高亮器对未知语言本来就回落 JS 规则；这里保持一致，别让预览因为一个怪文件名炸掉。
  assert.equal(langOf("data.bin"), "ts");
  assert.equal(langOf("no-extension"), "ts");
  assert.equal(langOf(""), "ts");
});
