import assert from "node:assert/strict";
import { test } from "node:test";

import {
  artifactKind,
  artifactsExport,
  groupArtifacts,
  KIND_LABELS,
} from "./artifacts.ts";

test("按扩展名分类，大小写与无扩展名都稳", () => {
  assert.equal(artifactKind("public/index.HTML"), "page");
  assert.equal(artifactKind("src/app.tsx"), "code");
  assert.equal(artifactKind("data/report.csv"), "data");
  assert.equal(artifactKind("shots/open.png"), "image");
  assert.equal(artifactKind("README.md"), "doc");
  assert.equal(artifactKind("Makefile"), "other");
  assert.equal(artifactKind("archive."), "other");
});

test("搜索只过滤不重组：空搜索返回全部，命中的组保持固定顺序", () => {
  const items = [
    { path: "README.md" },
    { path: "public/index.html" },
    { path: "src/app.tsx" },
    { path: "data/report.csv" },
  ];

  assert.equal(groupArtifacts(items).length, 4);

  const groups = groupArtifacts(items, "app");
  assert.deepEqual(
    groups.map((group) => group.kind),
    ["code"],
  );
  assert.deepEqual(
    groups[0].items.map((item) => item.path),
    ["src/app.tsx"],
  );
});

test("空组不出现，组顺序固定为页面→代码→数据→图片→文档→其他", () => {
  const groups = groupArtifacts([
    { path: "shot.png" },
    { path: "README.md" },
    { path: "index.html" },
    { path: "app.js" },
    { path: "data.json" },
    { path: "Makefile" },
  ]);

  assert.deepEqual(
    groups.map((group) => group.kind),
    ["page", "code", "data", "image", "doc", "other"],
  );
  assert.deepEqual(
    groups.map((group) => group.label),
    ["页面", "代码", "数据", "图片", "文档", "其他"],
  );
  assert.equal(groups.every((group) => group.items.length > 0), true);
  assert.equal(KIND_LABELS.other, "其他");
});

test("导出 JSON 只带路径与变更类型，Markdown 按组列出", () => {
  const items = [
    { path: "index.html", changeType: "created" },
    { path: "src/app.tsx", changeType: "modified" },
  ];

  const json = JSON.parse(artifactsExport(items, "json"));
  assert.deepEqual(json, [
    { path: "index.html", changeType: "created" },
    { path: "src/app.tsx", changeType: "modified" },
  ]);

  const markdown = artifactsExport(items, "markdown");
  assert.match(markdown, /# 产物清单/);
  assert.match(markdown, /## 页面（1）/);
  assert.match(markdown, /## 代码（1）/);
  assert.match(markdown, /`index\.html` · created/);
});

test("导出空列表不崩，也不编造条目", () => {
  assert.equal(JSON.parse(artifactsExport([], "json")).length, 0);
  const markdown = artifactsExport([], "markdown");
  assert.match(markdown, /# 产物清单/);
  assert.doesNotMatch(markdown, /## /);
});
