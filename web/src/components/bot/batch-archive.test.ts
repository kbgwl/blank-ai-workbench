import assert from "node:assert/strict";
import { test } from "node:test";

import type { Thread } from "../tasks/api";
import {
  archiveConfirmMessage,
  countUnfinished,
  isArchivable,
  partitionThreads,
  selectAllVisible,
  summarizeBatchResult,
  toggleSelected,
} from "./batch-archive.ts";

function thread(id: string, status = "active", lastTurnStatus = ""): Thread {
  return {
    id,
    projectId: "p1",
    title: id,
    status,
    forkedFrom: "",
    turnCount: 0,
    lastTurnStatus,
    createdAt: "2026-11-24T00:00:00Z",
    updatedAt: "2026-11-24T00:00:00Z",
  };
}

test("按状态分区：活跃与已归档各归各的", () => {
  const { active, archived } = partitionThreads([
    thread("a"),
    thread("b", "archived"),
    thread("c"),
  ]);
  assert.deepEqual(active.map((item) => item.id), ["a", "c"]);
  assert.deepEqual(archived.map((item) => item.id), ["b"]);
});

test("只有 active 能归档", () => {
  assert.equal(isArchivable(thread("a")), true);
  assert.equal(isArchivable(thread("b", "archived")), false);
  assert.equal(isArchivable(thread("c", "weird")), false);
});

test("切换选择返回新集合，不修改原集合", () => {
  const first = new Set<string>();
  const second = toggleSelected(first, "a");
  const third = toggleSelected(second, "a");
  assert.deepEqual([...first], []);
  assert.deepEqual([...second], ["a"]);
  assert.deepEqual([...third], []);
});

test("全选只选中可见的活跃会话，已归档不进来", () => {
  const selected = selectAllVisible([
    thread("a"),
    thread("archived", "archived"),
    thread("b"),
  ]);
  assert.deepEqual([...selected].sort(), ["a", "b"]);
});

test("全选保留之前选中的其他会话", () => {
  const selected = selectAllVisible([thread("b")], new Set(["a"]));
  assert.deepEqual([...selected].sort(), ["a", "b"]);
});

test("未结束会话数按最后一轮状态算", () => {
  const rows = [
    thread("draft", "active", "draft"),
    thread("queued", "active", "queued"),
    thread("running", "active", "running"),
    thread("waiting", "active", "waiting_approval"),
    thread("paused", "active", "paused"),
    thread("done", "active", "succeeded"),
    thread("failed", "active", "failed"),
    thread("empty", "active", ""),
  ];
  assert.equal(countUnfinished(rows), 5);
});

test("整批确认只说一次，并说明归档不停止未结束任务", () => {
  const plain = archiveConfirmMessage(3, 0);
  assert.match(plain, /归档所选 3 条会话？/);
  assert.doesNotMatch(plain, /尚未结束/);

  const running = archiveConfirmMessage(2, 1);
  assert.match(running, /其中 1 条尚未结束/);
  assert.match(running, /归档不会停止它们，任务会在后台继续/);
});

test("归档提示把 unchanged 计入成功，missing 单独说明", () => {
  assert.equal(
    summarizeBatchResult({ archived: ["a", "b"], unchanged: [], missing: [] }),
    "已归档 2 条会话，可在「已归档」中找回",
  );
  assert.equal(
    summarizeBatchResult({ archived: ["a"], unchanged: ["b"], missing: [] }),
    "已归档 2 条会话，可在「已归档」中找回",
  );
  assert.equal(
    summarizeBatchResult({ archived: ["a"], unchanged: [], missing: ["x", "y"] }),
    "已归档 1 条会话，可在「已归档」中找回；2 条已不存在",
  );
});

test("全部 id 都不存在时不说「已归档 0 条」", () => {
  assert.equal(
    summarizeBatchResult({ archived: [], unchanged: [], missing: ["x"] }),
    "所选 1 条会话已不存在，没有可归档的会话",
  );
});
