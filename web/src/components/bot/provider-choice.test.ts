import assert from "node:assert/strict";
import { test } from "node:test";

import type { Provider } from "../tasks/api.ts";
import {
  defaultModelLabel,
  defaultProviderId,
  providerLabel,
  usableProviderId,
} from "./provider-choice.ts";

function provider(overrides: Partial<Provider> & { id: string }): Provider {
  return {
    name: overrides.id,
    model: "",
    kind: "openai",
    enabled: true,
    api_key_hint: "sk-…abcd",
    ...overrides,
  };
}

test("存过的供应商还在，照旧用它", () => {
  assert.equal(usableProviderId("p-glm", ["p-glm", "p-openai"]), "p-glm");
});

test("供应商已被删掉：不再沿用这个悬空 id", () => {
  // 真实场景：localStorage 里留着上一次测试用的临时 provider，界面显示「默认模型」，
  // 任务却带着这个 id 落库，一运行就报「模型供应商不存在」。
  assert.equal(usableProviderId("p-deleted", ["p-glm"]), "");
});

test("从没选过就是空，交给后端按默认选", () => {
  assert.equal(usableProviderId("", ["p-glm"]), "");
});

test("列表为空时也不沿用旧 id：宁可回到默认，也不带一个看不见的选择下去", () => {
  assert.equal(usableProviderId("p-glm", []), "");
});

test("选中了供应商：显示「名字 · 模型」，不报警", () => {
  const providers = [provider({ id: "p-glm", name: "智谱", model: "glm-4.6" })];
  assert.deepEqual(providerLabel("p-glm", providers), { text: "智谱 · glm-4.6", warn: false });
});

test("没显式选但有可用供应商：是「默认模型」，不是「未配置模型」", () => {
  const providers = [provider({ id: "p-glm", name: "智谱", model: "glm-4.6" })];
  assert.deepEqual(providerLabel("", providers), { text: "默认模型", warn: false });
});

test("一家可用的都没有：这才该报「未配置模型」", () => {
  const providers = [provider({ id: "p-glm", name: "智谱", model: "glm-4.6", api_key_hint: "" })];
  assert.deepEqual(providerLabel("", providers), { text: "未配置模型", warn: true });
});

test("悬空 id 按没选处理：有可用供应商就回到默认，不报错", () => {
  const providers = [provider({ id: "p-glm", name: "智谱", model: "glm-4.6" })];
  assert.deepEqual(providerLabel("p-deleted", providers), { text: "默认模型", warn: false });
});

test("供应商还没读回来：说「读取中」，不许先把「未配置模型」扣在用户头上", () => {
  // 首屏 providers 是空数组，那只是"还没读到"。这一瞬间报红，用户看到的是
  // 自己明明配好的模型被否认了一次。
  assert.deepEqual(providerLabel("", [], false), { text: "读取中", warn: false });
  assert.deepEqual(providerLabel("p-glm", [], false), { text: "读取中", warn: false });
});

test("选中了一家没配 key 的：照样显示它，但要报警", () => {
  // 界面写着"anthropic · sonnet"、发出去 401，用户会去猜是哪一环坏了。
  const { text, warn } = providerLabel(
    "a1",
    [{ id: "a1", name: "anthropic", model: "claude-sonnet", enabled: true, api_key_hint: "" } as never],
  );
  assert.equal(text, "anthropic · claude-sonnet");
  assert.equal(warn, true);
});

test("选中了一家已停用的：文案里说出来，也要报警", () => {
  const { text, warn } = providerLabel(
    "a1",
    [{ id: "a1", name: "anthropic", model: "sonnet", enabled: false, api_key_hint: "k" } as never],
  );
  assert.match(text, /已停用/);
  assert.equal(warn, true);
});

test("默认模型名跟后端标出的 is_default 走，不按列表第一项猜", () => {
  const providers = [
    provider({ id: "p-ds", name: "deepseek", model: "deepseek-flash" }),
    provider({ id: "p-spark", name: "讯飞星火", model: "spark-x2.6", is_default: true }),
  ];
  assert.equal(defaultModelLabel(providers), "spark-x2.6");
  // 用户显式选了一家：显示它，不是默认那家。
  assert.equal(defaultModelLabel(providers, "p-ds"), "deepseek-flash");
});

test("默认模型名不会选到没 Key 的供应商", () => {
  const providers = [
    provider({ id: "p-off", name: "未配置", model: "nope", api_key_hint: "", is_default: true }),
    provider({ id: "p-ds", name: "deepseek", model: "deepseek-flash" }),
  ];
  assert.equal(defaultModelLabel(providers), "deepseek-flash");
});

test("全站默认供应商跳过没 Key 的，并优先后端标出的 is_default", () => {
  const providers = [
    provider({ id: "p-anthropic", name: "anthropic", model: "claude", api_key_hint: "" }),
    provider({ id: "p-ds", name: "deepseek", model: "deepseek-flash" }),
    provider({ id: "p-spark", name: "讯飞星火", model: "spark-x2.6", is_default: true }),
  ];
  assert.equal(defaultProviderId(providers), "p-spark");
  // 全都没 Key：不编一个 id，交给界面显示「未配置模型」。
  assert.equal(
    defaultProviderId([provider({ id: "p-x", api_key_hint: "" })]),
    "",
  );
});
