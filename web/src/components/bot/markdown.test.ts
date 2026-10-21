import assert from "node:assert/strict";
import { test } from "node:test";

import { parseInline, parseMarkdown } from "./markdown.ts";

test("行内的代码、粗体、链接各自成段，普通文字原样保留", () => {
  const spans = parseInline("先跑 `npm test`，**别跳过**，详见 [文档](https://x.dev/a)");
  assert.deepEqual(
    spans.map((span) => span.kind),
    ["text", "code", "text", "bold", "text", "link"],
  );
  assert.equal(spans[1].kind === "code" && spans[1].text, "npm test");
  assert.equal(spans[3].kind === "bold" && spans[3].text, "别跳过");
  assert.equal(spans[5].kind === "link" && spans[5].href, "https://x.dev/a");
});

test("代码块带语言，并且不把里面的星号当粗体", () => {
  const blocks = parseMarkdown("```python\na = 1 * 2\n**not bold**\n```");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].kind, "code");
  if (blocks[0].kind !== "code") return;
  assert.equal(blocks[0].lang, "python");
  assert.match(blocks[0].text, /\*\*not bold\*\*/);
});

test("流式输出里没闭合的围栏也按代码块渲染，后面内容不丢", () => {
  const blocks = parseMarkdown("先这样：\n```ts\nconst a = 1;");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[1].kind, "code");
  assert.equal(blocks[1].kind === "code" && blocks[1].text, "const a = 1;");
});

test("无序与有序列表分别识别，缩进续行并进上一条", () => {
  const blocks = parseMarkdown("- 第一条\n  接着说\n- 第二条");
  assert.equal(blocks[0].kind, "list");
  if (blocks[0].kind !== "list") return;
  assert.equal(blocks[0].ordered, false);
  assert.equal(blocks[0].items.length, 2);
  assert.equal(blocks[0].items[0].map((span) => span.text).join(""), "第一条 接着说");

  const ordered = parseMarkdown("1. 先读代码\n2. 再改");
  assert.equal(ordered[0].kind, "list");
  assert.equal(ordered[0].kind === "list" && ordered[0].ordered, true);
  assert.equal(ordered[0].kind === "list" && ordered[0].items.length, 2);
});

test("标题、引用、分隔线各成块", () => {
  const blocks = parseMarkdown("## 结论\n> 一句引用\n\n---");
  assert.deepEqual(
    blocks.map((block) => block.kind),
    ["heading", "quote", "hr"],
  );
  assert.equal(blocks[0].kind === "heading" && blocks[0].level, 2);
});

test("HTML 原样当文字，不变成结构", () => {
  const blocks = parseMarkdown('<div onclick="alert(1)">hi</div>');
  assert.equal(blocks[0].kind, "p");
  const text = blocks[0].kind === "p" ? blocks[0].spans.map((span) => span.text).join("") : "";
  assert.match(text, /<div/);
});

test("多段正文按空行切开", () => {
  const blocks = parseMarkdown("第一段\n第二行\n\n第二段");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].kind === "p" && blocks[0].spans[0].text, "第一段\n第二行");
});

test("空输入不产生任何块", () => {
  assert.deepEqual(parseMarkdown(""), []);
  assert.deepEqual(parseMarkdown("   \n\n"), []);
});
