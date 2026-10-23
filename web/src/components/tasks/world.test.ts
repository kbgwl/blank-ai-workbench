import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeEvidence } from "./api.ts";

/** 后端 evidence.py 的 world 块真实形状（键名即跨语言契约）。 */
const worldPayload = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  snapshots: 2,
  current: {
    full: true,
    state: {
      operating_mode: {
        mode: "default",
        model: "deepseek-chat",
        provider: "deepseek",
        readonly: false,
        guardian_mode: "off",
        instructions: "3f2c1d0e9a8b7c6d5e4f30123456789abcdef001",
      },
      instructions: [
        {
          source: "project",
          path: "<workspace:kbgai>/AGENTS.md",
          chars: 2360,
          sha: "eecb63b20ef5160b397449dc315a6775660fd0e3",
          truncated: false,
        },
      ],
      environments: {
        local: {
          cwd: "<workspace:kbgai>",
          status: "captured",
          shell: "cmd.exe",
          os: "Windows 11",
          machine: "AMD64",
          python: "python.exe",
          has_venv: true,
          venv_pinned: true,
          path_entries: 18,
          env_names: ["PATH", "SYSTEMROOT"],
        },
      },
      context_window_guidance: {
        context_window: 64000,
        trigger_ratio: 0.82,
        reserve_tokens: 16384,
        trigger_at: 52480,
      },
      current_date: "2026-10-22",
      timezone: "中国标准时间",
    },
  },
  changes: [{ seq: 41, node: "execute_action", date: "2026-10-23" }],
  ...over,
});

const worldOf = (world: Record<string, unknown> | null) => normalizeEvidence({ world: world ?? {} }).world;

test("环境快照与指令指纹从后端键名搬到前端", () => {
  const world = worldOf(worldPayload());
  assert.equal(world.snapshots, 2);
  const current = world.current;
  assert.ok(current);
  assert.equal(current.cwd, "<workspace:kbgai>");
  assert.equal(current.shell, "cmd.exe");
  assert.equal(current.os, "Windows 11");
  assert.equal(current.pathEntries, 18);
  assert.equal(current.contextWindow, 64000);
  assert.equal(current.triggerAt, 52480);
  assert.equal(current.mode, "default");
  assert.equal(current.instructionsDigest, "3f2c1d0e9a8b7c6d5e4f30123456789abcdef001");
  assert.equal(current.instructions.length, 1);
  assert.equal(current.instructions[0].sha, "eecb63b20ef5160b397449dc315a6775660fd0e3");
  assert.equal(current.instructions[0].chars, 2360);
  assert.equal(world.changes[0].seq, 41);
});

test("老任务没有这类事件：快照 0 条、现状 null，不拿今天的环境补", () => {
  const world = worldOf({});
  assert.equal(world.snapshots, 0);
  assert.equal(world.current, null);
  assert.deepEqual(world.changes, []);
});

test("缺的数字键是 null 不是 0", () => {
  // path_entries 显示成 0 会说「当时 PATH 是空的」，而事实是「那会儿没记这一项」。
  const payload = worldPayload();
  const state = (payload.current as Record<string, unknown>).state as Record<string, unknown>;
  const local = (state.environments as Record<string, unknown>).local as Record<string, unknown>;
  delete local.path_entries;
  delete state.context_window_guidance;
  const world = worldOf(payload);
  assert.equal(world.current?.pathEntries, null);
  assert.equal(world.current?.contextWindow, null);
  assert.equal(world.current?.cwd, "<workspace:kbgai>");
});

test("state 缺失但快照有条数时现状仍是 null", () => {
  const world = worldOf({ snapshots: 1, current: {}, changes: [] });
  assert.equal(world.snapshots, 1);
  assert.equal(world.current, null);
});
