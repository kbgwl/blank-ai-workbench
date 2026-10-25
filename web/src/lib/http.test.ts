import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveApiBase } from "./http.ts";

test("壳注入的地址优先于构建时烘焙值", () => {
  assert.equal(resolveApiBase("http://127.0.0.1:8123", "http://127.0.0.1:8000"), "http://127.0.0.1:8123");
});

test("没有壳注入时退回烘焙值", () => {
  assert.equal(resolveApiBase(undefined, "http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  assert.equal(resolveApiBase("", "http://127.0.0.1:8000"), "http://127.0.0.1:8000");
});

test("网页构建两者都没有时走相对路径", () => {
  assert.equal(resolveApiBase(undefined, undefined), "");
});

test("注入值去空白与尾部斜杠，非字符串按未注入处理", () => {
  assert.equal(resolveApiBase("  http://127.0.0.1:8123/  ", "http://127.0.0.1:8000"), "http://127.0.0.1:8123");
  assert.equal(resolveApiBase(8123, "http://127.0.0.1:8000"), "http://127.0.0.1:8000");
});
