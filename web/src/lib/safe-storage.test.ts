import assert from "node:assert/strict";
import { test } from "node:test";

import { readJson, readString, removeString, writeJson, writeString } from "./safe-storage.ts";

type FakeStorage = {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
  removeItem: (key: string) => void;
};

function install(storage: FakeStorage): void {
  (globalThis as unknown as { window: { localStorage: FakeStorage } }).window = { localStorage: storage };
}

function memoryBacked(): FakeStorage {
  const rows = new Map<string, string>();
  return {
    getItem: (key) => rows.get(key) ?? null,
    setItem: (key, value) => { rows.set(key, value); },
    removeItem: (key) => { rows.delete(key); },
  };
}

test("安全存储正常读写字符串与 JSON", () => {
  install(memoryBacked());
  writeString("test:safe-storage:string", "abc");
  assert.equal(readString("test:safe-storage:string"), "abc");
  writeJson("test:safe-storage:json", { a: 1 });
  assert.deepEqual(readJson("test:safe-storage:json", { a: 0 }), { a: 1 });
  removeString("test:safe-storage:string");
  assert.equal(readString("test:safe-storage:string"), "");
});

test("读取抛异常时回落到内存副本", () => {
  install({
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
    removeItem: () => { throw new Error("blocked"); },
  });
  writeString("test:safe-storage:blocked", "kept");
  assert.equal(readString("test:safe-storage:blocked"), "kept");
});

test("坏 JSON 回落默认值并清掉坏值", () => {
  const rows = new Map<string, string>([["test:safe-storage:bad", "{oops"]]);
  install({
    getItem: (key) => rows.get(key) ?? null,
    setItem: (key, value) => { rows.set(key, value); },
    removeItem: (key) => { rows.delete(key); },
  });
  assert.deepEqual(readJson("test:safe-storage:bad", ["fallback"]), ["fallback"]);
  assert.equal(rows.has("test:safe-storage:bad"), false);
});
