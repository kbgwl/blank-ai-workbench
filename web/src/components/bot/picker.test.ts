import assert from "node:assert/strict";
import { test } from "node:test";

import {
  childrenOf,
  dirOf,
  insertReference,
  joinPath,
  moveIndex,
  normalizePath,
  parentPath,
  pickerRows,
  rankEntries,
  type PickEntry,
} from "./picker.ts";

const tree: PickEntry[] = [
  { name: "src", path: "src", is_dir: true },
  { name: "docs", path: "docs", is_dir: true },
  { name: "README.md", path: "README.md", is_dir: false },
  { name: "components", path: "src/components", is_dir: true },
  { name: "index.ts", path: "src/index.ts", is_dir: false },
  { name: "panel.tsx", path: "src/components/panel.tsx", is_dir: false },
  { name: "composer.tsx", path: "src/components/composer.tsx", is_dir: false },
];

test("Windows 反斜杠路径先归一，尾斜杠和 ./ 前缀都去掉", () => {
  assert.equal(normalizePath("src\\components\\"), "src/components");
  assert.equal(normalizePath("./src"), "src");
  assert.equal(normalizePath("src\\a\\b.tsx"), "src/a/b.tsx");
});

test("路径拼装与回退在根目录处收住", () => {
  assert.equal(joinPath("", "src"), "src");
  assert.equal(joinPath("src", "index.ts"), "src/index.ts");
  assert.equal(parentPath("src/components"), "src");
  assert.equal(parentPath("src"), "");
  assert.equal(parentPath(""), "");
  assert.equal(dirOf("src/components/panel.tsx"), "src/components");
  assert.equal(dirOf("README.md"), "");
});

test("只列当前目录的直接子项，目录排在文件前面", () => {
  const rows = childrenOf(tree, "");
  assert.deepEqual(
    rows.map((entry) => entry.path),
    ["src", "docs", "README.md"],
  );
  const inner = childrenOf(tree, "src");
  assert.deepEqual(
    inner.map((entry) => entry.path),
    ["src/components", "src/index.ts"],
  );
});

test("反过来搜的路径用反斜杠也能落进同一个目录", () => {
  const rows = childrenOf(tree, "src\\components");
  assert.deepEqual(
    rows.map((entry) => entry.path),
    ["src/components/panel.tsx", "src/components/composer.tsx"],
  );
});

test("排序档位：文件名相等 > 前缀 > 包含 > 仅路径包含", () => {
  const rows = rankEntries(tree, "composer.tsx");
  assert.equal(rows[0]?.path, "src/components/composer.tsx");
  // panel.tsx 的路径里也含 "composer"？不含；但 src/components 目录名含 "com"，故用前缀档验证顺序。
  const prefixed = rankEntries(tree, "comp");
  assert.deepEqual(
    prefixed.slice(0, 2).map((entry) => entry.path),
    ["src/components", "src/components/composer.tsx"],
  );
});

test("同档里目录优先，空查询不出搜索结果", () => {
  const rows = rankEntries(tree, "s");
  assert.equal(rows[0]?.path, "src");
  assert.deepEqual(rankEntries(tree, "   "), []);
});

test("有查询词走全树搜索，没有查询词只走当前目录", () => {
  const searched = pickerRows(tree, "", "panel");
  assert.deepEqual(
    searched.map((entry) => entry.path),
    ["src/components/panel.tsx"],
  );
  const browsed = pickerRows(tree, "src/components", "");
  assert.equal(browsed.length, 2);
  assert.deepEqual(pickerRows(tree, "docs", ""), []);
});

test("上下移动夹在两端，不回绕", () => {
  assert.equal(moveIndex(0, -1, 5), 0);
  assert.equal(moveIndex(4, 1, 5), 4);
  assert.equal(moveIndex(2, 1, 5), 3);
  assert.equal(moveIndex(0, 1, 0), 0);
});

test("插入引用时按需补空格，空草稿不带头空格", () => {
  assert.equal(insertReference("", "src/index.ts"), "@src/index.ts ");
  assert.equal(insertReference("看看", "src/index.ts"), "看看 @src/index.ts ");
  assert.equal(insertReference("看看 ", "src/index.ts"), "看看 @src/index.ts ");
  assert.equal(insertReference("看看\n", "src\\index.ts"), "看看\n@src/index.ts ");
});