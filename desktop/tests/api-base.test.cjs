"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { resolveApiBase } = require("../api-base.cjs");

test("默认地址是 127.0.0.1:8000", () => {
  assert.equal(resolveApiBase({}), "http://127.0.0.1:8000");
});

test("BLANK_BACKEND_PORT 改变后端地址", () => {
  assert.equal(resolveApiBase({ BLANK_BACKEND_PORT: "8123" }), "http://127.0.0.1:8123");
});

test("BLANK_API_BASE 优先于端口", () => {
  assert.equal(
    resolveApiBase({ BLANK_API_BASE: "http://10.0.0.5:9000", BLANK_BACKEND_PORT: "8123" }),
    "http://10.0.0.5:9000",
  );
});

test("显式地址去掉尾部斜杠，空串退回端口", () => {
  assert.equal(resolveApiBase({ BLANK_API_BASE: "http://127.0.0.1:8123///" }), "http://127.0.0.1:8123");
  assert.equal(resolveApiBase({ BLANK_API_BASE: "   ", BLANK_BACKEND_PORT: "8123" }), "http://127.0.0.1:8123");
});

test("空白端口按默认值处理", () => {
  assert.equal(resolveApiBase({ BLANK_BACKEND_PORT: "  " }), "http://127.0.0.1:8000");
});
