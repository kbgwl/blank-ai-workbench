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
const botApp = readFileSync(join(here, "bot-app.tsx"), "utf-8");

test("完全访问档不再承诺装依赖全自动", () => {
  const bypassDesc = composer.match(/label: "完全访问",[\s\S]*?description: "([^"]*)"/);
  assert.ok(bypassDesc, "找不到完全访问档的 description");
  assert.ok(!bypassDesc[1].includes("装依赖"), `描述里不该再写「装依赖」全自动：${bypassDesc[1]}`);
});

test("完全访问档如实说明首次装依赖会问一次", () => {
  assert.match(composer, /首次装依赖会问一次/);
  assert.match(botApp, /装依赖首次会问一次/);
});

test("完全访问档警示列出真实仍会确认的高危项", () => {
  const warning = composer.match(/label: "完全访问",[\s\S]*?warning: "([^"]*)"/);
  assert.ok(warning, "找不到完全访问档的 warning");
  for (const item of ["删除", "git push", "数据库迁移"]) {
    assert.ok(warning[1].includes(item), `警示里应该列出「${item}」：${warning[1]}`);
  }
});
