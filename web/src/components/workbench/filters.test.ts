import assert from "node:assert/strict";
import { test } from "node:test";

import { parseTaskFilters, writeTaskFilters } from "./filters.ts";

test("空地址栏 = 全部 + 空搜索", () => {
  assert.deepEqual(parseTaskFilters(""), { query: "", status: "all" });
  assert.deepEqual(parseTaskFilters("?"), { query: "", status: "all" });
});

test("读回搜索词与状态；非法状态回到全部", () => {
  assert.deepEqual(parseTaskFilters("?q=%E6%97%A5%E6%8A%A5&status=failed"), {
    query: "日报",
    status: "failed",
  });
  assert.deepEqual(parseTaskFilters("?status=wat"), { query: "", status: "all" });
  assert.deepEqual(parseTaskFilters("?q=%20%20"), { query: "", status: "all" });
});

test("写回筛选时保留其他参数，只改 q/status", () => {
  const next = writeTaskFilters("/tasks?project=p1&status=done&q=old", {
    query: "修复",
    status: "active",
  });
  assert.equal(next, "/tasks?project=p1&status=active&q=%E4%BF%AE%E5%A4%8D");
});

test("清空筛选会把 q/status 从地址栏删掉", () => {
  const next = writeTaskFilters("/tasks?project=p1&q=x&status=failed", {
    query: "",
    status: "all",
  });
  assert.equal(next, "/tasks?project=p1");
});

test("读写可以来回：写进去的地址解析回同一组筛选", () => {
  const url = writeTaskFilters("/tasks", { query: "计划 书", status: "failed" });
  assert.deepEqual(parseTaskFilters(new URL(url, "http://x").search), {
    query: "计划 书",
    status: "failed",
  });
});
