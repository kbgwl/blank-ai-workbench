import assert from "node:assert/strict";
import { test } from "node:test";

import { previewExpiry, previewHeadline, previewIsolation } from "./preview-copy.ts";

test("只把 running 说成运行中；认不出的状态不假装活着", () => {
  assert.equal(previewHeadline(null), "预览未启动");
  assert.equal(previewHeadline({ status: "stopped" }), "预览未启动");
  assert.equal(previewHeadline({ status: "running", url: "http://127.0.0.1:1/" }), "预览运行中");
  assert.equal(previewHeadline({ status: "weird" }), "预览不可用");
});

test("到期时间解析失败时不显示，不编时间", () => {
  assert.equal(previewExpiry({ status: "running", expires_at: "not-a-date" }), "");
  assert.equal(previewExpiry({ status: "running" }), "");
  assert.match(
    previewExpiry({ status: "running", expires_at: "2026-11-23T10:00:00Z" }),
    /^到期 /,
  );
});

test("Job 没挂上时如实写「未挂上」，不写成已隔离", () => {
  assert.equal(previewIsolation({ status: "running", isolation: "local-job" }), "本机 + Job Object");
  assert.equal(previewIsolation({ status: "running", isolation: "local" }), "本机（Job 未挂上）");
  assert.equal(previewIsolation({ status: "running", isolation: "docker" }), "docker");
  assert.equal(previewIsolation({ status: "running" }), "");
});
