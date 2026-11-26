import assert from "node:assert/strict";
import { test } from "node:test";

import {
  delimiterFor,
  formatJson,
  parseDelimited,
  previewKind,
} from "./preview.ts";

test("预览类型按扩展名判断，大小写不敏感", () => {
  assert.equal(previewKind("index.HTML"), "html");
  assert.equal(previewKind("docs/readme.MD"), "markdown");
  assert.equal(previewKind("data.json"), "json");
  assert.equal(previewKind("report.CSV"), "csv");
  assert.equal(previewKind("notes.txt"), "text");
  assert.equal(previewKind("Makefile"), "text");
  // 后端把 docx/xlsx/pptx 抽成 markdown 文本后再给前端。
  assert.equal(previewKind("计划书.DOCX"), "markdown");
  assert.equal(previewKind("账目.xlsx"), "markdown");
  assert.equal(previewKind("汇报.pptx"), "markdown");
  assert.equal(delimiterFor("data.tsv"), "\t");
  assert.equal(delimiterFor("data.csv"), ",");
});

test("JSON 合法时缩进，非法时保留错误而不是编一个结果", () => {
  assert.deepEqual(formatJson('{"a":1}'), { ok: true, text: '{\n  "a": 1\n}' });
  const bad = formatJson("{");
  assert.equal(bad.ok, false);
  if (!bad.ok) assert.ok(bad.error.length > 0);
});

test("CSV 引号、逗号、双引号转义与 CRLF", () => {
  const table = parseDelimited('name,note\r\n"a,b","say ""hi"""\r\n', ",");
  assert.deepEqual(table.rows, [
    ["name", "note"],
    ["a,b", 'say "hi"'],
  ]);
  assert.equal(table.truncated, false);
});

test("末尾换行不产生空行；空文本返回空表", () => {
  assert.deepEqual(parseDelimited("a,b\n1,2\n", ",").rows, [
    ["a", "b"],
    ["1", "2"],
  ]);
  assert.deepEqual(parseDelimited("   ", ",").rows, []);
});

test("行数与列数超限时截断并标记", () => {
  const table = parseDelimited("a,b,c\n1,2,3\n4,5,6", ",", 2, 2);
  assert.equal(table.rows.length, 2);
  assert.deepEqual(table.rows[0], ["a", "b"]);
  assert.equal(table.truncated, true);
});

test("TSV 用制表符分隔", () => {
  const table = parseDelimited("a\tb\n1\t2", "\t");
  assert.deepEqual(table.rows, [
    ["a", "b"],
    ["1", "2"],
  ]);
});
