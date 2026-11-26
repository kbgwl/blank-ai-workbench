// 权限档位文案必须与后端真实行为一致。
//
// 真机 a69c01d2：界面说「完全访问 = 工作区内自动读写、装依赖」，用户据此选了
// 完全访问，结果装依赖连弹两张卡（带管道的 pip 命令记不住前缀，卡片只剩「仅本次」）。
// 文案承诺与后端判定一旦分叉，用户就会觉得「授权失效了」。这里把两处内联文案
// 钉住：既不能再说「装依赖全自动」，也必须如实说明「首次装依赖会问一次」。

import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const composer = readFileSync(join(here, "composer.tsx"), "utf-8");

test("完全访问档不再承诺装依赖全自动", () => {
  const bypassDesc = composer.match(/label: "完全访问",[\s\S]*?description: "([^"]*)"/);
  assert.ok(bypassDesc, "找不到完全访问档的 description");
  assert.ok(!bypassDesc[1].includes("装依赖"), `描述里不该再写「装依赖」全自动：${bypassDesc[1]}`);
});

test("完全访问档如实说明首次装依赖会问一次", () => {
  assert.match(composer, /首次装依赖会问一次/);
});

test("完全访问档警示列出真实仍会确认的高危项", () => {
  const warning = composer.match(/label: "完全访问",[\s\S]*?warning: "([^"]*)"/);
  assert.ok(warning, "找不到完全访问档的 warning");
  for (const item of ["删除", "git push", "数据库迁移"]) {
    assert.ok(warning[1].includes(item), `警示里应该列出「${item}」：${warning[1]}`);
  }
});

test("完全访问档说明批准一次后本会话同类不再逐条问", () => {
  const warning = composer.match(/label: "完全访问",[\s\S]*?warning: "([^"]*)"/);
  assert.ok(warning, "找不到完全访问档的 warning");
  assert.match(warning[1], /本会话同类不再逐条问/);
  assert.match(warning[1], /过期/);
});

test("发送栏常驻提示也点名内联脚本与装依赖", () => {
  // 选项说明默认收起，用户看到的是输入框下方那行常驻提示；它漏掉「装依赖」时，
  // 用户会以为完全访问下装包也该自动放行。
  assert.match(composer, /首次装依赖、内联脚本/);
});

test("完全访问：跨盘只读自动，写删逐次确认", () => {
  assert.match(composer, /工作区外只读盘点自动放行/);
  assert.match(composer, /写\/删工作区外路径会逐次弹确认/);
  assert.match(composer, /不会自动清理/);
});

test("纯聊天说明单个目录只覆盖该目录", () => {
  assert.match(composer, /单个目录只覆盖该目录/);
  assert.match(composer, /盘根/);
});
