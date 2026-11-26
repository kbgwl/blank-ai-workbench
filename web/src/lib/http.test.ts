import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveApiBase, toApiError } from "./http.ts";

test("壳注入的地址优先于构建时烘焙值", () => {
  assert.equal(resolveApiBase("http://127.0.0.1:8123", "http://127.0.0.1:8000"), "http://127.0.0.1:8123");
});

test("没有壳注入时：桌面壳环境退回烘焙值", () => {
  const g = globalThis as { blankDesktop?: unknown };
  g.blankDesktop = {};
  assert.equal(resolveApiBase(undefined, "http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  assert.equal(resolveApiBase("", "http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  delete g.blankDesktop;
});

test("纯浏览器（CLI 静态站）烘焙的绝对地址跨源会被拦，走相对路径", () => {
  const g = globalThis as { blankDesktop?: unknown };
  assert.equal(g.blankDesktop, undefined);
  assert.equal(resolveApiBase(undefined, "http://127.0.0.1:8000"), "");
  assert.equal(resolveApiBase("", "http://127.0.0.1:8000"), "");
});

test("网页构建两者都没有时走相对路径", () => {
  assert.equal(resolveApiBase(undefined, undefined), "");
});

test("注入值去空白与尾部斜杠，非字符串按未注入处理", () => {
  assert.equal(resolveApiBase("  http://127.0.0.1:8123/  ", "http://127.0.0.1:8000"), "http://127.0.0.1:8123");
  const g = globalThis as { blankDesktop?: unknown };
  g.blankDesktop = {};
  assert.equal(resolveApiBase(8123, "http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  delete g.blankDesktop;
  assert.equal(resolveApiBase(8123, "http://127.0.0.1:8000"), "");
});

test("429 走统一嵌套错误契约", () => {
  const error = toApiError(429, { error: { code: "rate_limit_exceeded", message: "请求过于频繁，请稍后再试" } });
  assert.equal(error.status, 429);
  assert.equal(error.code, "rate_limit_exceeded");
  assert.equal(error.message, "请求过于频繁，请稍后再试");
});

test("旧版扁平错误体也能解析出业务码", () => {
  const error = toApiError(429, { error: "rate_limit_exceeded", message: "Too many requests" });
  assert.equal(error.code, "rate_limit_exceeded");
  assert.equal(error.message, "Too many requests");
});

test("代理吐出的无结构化 5xx 说明后端可能正在重启，并带请求路径", () => {
  // 开发态 Next rewrite 在上游不可达时返回 HTML 500：parseJson 得到 null。
  const error = toApiError(500, null, "POST /api/v1/tasks/t1/approve");
  assert.equal(error.status, 500);
  assert.match(error.message, /后端暂时不可用（HTTP 500）/);
  assert.match(error.message, /可能正在重启/);
  assert.match(error.message, /POST \/api\/v1\/tasks\/t1\/approve/);
});

test("后端自己的 500 仍显示后端消息，不误报成重启", () => {
  const error = toApiError(500, { error: { code: "internal_error", message: "unexpected server error" } });
  assert.equal(error.message, "unexpected server error");
});

test("普通 4xx 的兜底文案带请求路径", () => {
  const error = toApiError(404, null, "GET /api/v1/tasks/missing");
  assert.equal(error.message, "请求失败（HTTP 404） · GET /api/v1/tasks/missing");
});
