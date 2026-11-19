import assert from "node:assert/strict";
import { test } from "node:test";

import type { Provider } from "../tasks/api.ts";
import { providerLabel, usableProviderId } from "./provider-choice.ts";

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
