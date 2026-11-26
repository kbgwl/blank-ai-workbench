import assert from "node:assert/strict";
import { test } from "node:test";

import { configuredProviders } from "./configured-providers.ts";

function provider(id: string, enabled: boolean, api_key_hint: string | null) {
  return { id, enabled, api_key_hint };
}

test("只保留启用且填了 Key 的供应商", () => {
  const rows = [
    provider("deepseek", true, "****1558"),
    provider("anthropic", true, ""),
    provider("openai", false, "****abcd"),
    provider("ollama", true, null),
  ];
  assert.deepEqual(
    configuredProviders(rows).map((item) => item.id),
    ["deepseek"],
  );
});

test("保持原列表顺序，不替调用方重排", () => {
  const rows = [
    provider("b", true, "k"),
    provider("a", true, "k"),
    provider("c", true, "k"),
  ];
  assert.deepEqual(
    configuredProviders(rows).map((item) => item.id),
    ["b", "a", "c"],
  );
});

test("没有配置项时返回空列表，调用方据此显示未配置", () => {
  assert.deepEqual(configuredProviders([]), []);
  assert.deepEqual(configuredProviders([provider("x", true, "")]), []);
});
