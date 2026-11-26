import assert from "node:assert/strict";
import { test } from "node:test";

import { grantExpiry, grantSummary } from "./grants.ts";

test("没有授权时不显示入口文案", () => {
  assert.equal(grantSummary([]), "");
  assert.equal(
    grantSummary([
      {
        thread: "t",
        category: "git_push",
        label: "推送代码",
        granted_at: "",
        expires_at: "",
        source_task: "",
      },
    ]),
    "授权 1",
  );
});

test("到期时间解析失败时不显示，不编时间", () => {
  assert.equal(grantExpiry(""), "");
  assert.equal(grantExpiry("not-a-date"), "");
  assert.match(grantExpiry("2026-11-23T10:00:00Z"), /^到期 /);
});
