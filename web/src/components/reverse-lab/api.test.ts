import assert from "node:assert/strict";
import test from "node:test";

import {
  adapterLabel,
  adapterReason,
  adapterTone,
  createReaInvestigation,
  fetchReaLabStatus,
  fetchReaStrings,
  fetchReaTargets,
  findingLabel,
  formatBytes,
  offsetLabel,
  targetHint,
  type AdapterStatus,
  type ReaTarget,
} from "./api.ts";

/** 把一次 fetch 的入参记下来，返回一个固定响应体。 */
function capture<T>(body: T): { calls: { url: string; init?: RequestInit }[]; restore: () => void } {
  const calls: { url: string; init?: RequestInit }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof globalThis.fetch;
  return { calls, restore: () => (globalThis.fetch = original) };
}

test("status 只带 project_id，refresh 是可选开关", async () => {
  const probe = capture({ project_id: "p1", root: "<workspace:demo>", adapter: {} });
  try {
    await fetchReaLabStatus("p1");
    await fetchReaLabStatus("p1", true);
  } finally {
    probe.restore();
  }
  assert.equal(probe.calls[0].url, "/api/v1/reverse-lab/status?project_id=p1");
  assert.equal(probe.calls[1].url, "/api/v1/reverse-lab/status?project_id=p1&refresh=true");
});

test("targets 的子目录为空时不塞一个空 path 参数", async () => {
  const probe = capture({ targets: [] });
  try {
    await fetchReaTargets("p1");
    await fetchReaTargets("p1", "build", 20);
  } finally {
    probe.restore();
  }
  // 空串在 query() 里被丢掉：后端默认就是根目录，多发一个 path= 只会让 URL 难读
  assert.equal(probe.calls[0].url, "/api/v1/reverse-lab/targets?project_id=p1&limit=60");
  assert.equal(probe.calls[1].url, "/api/v1/reverse-lab/targets?project_id=p1&path=build&limit=20");
});

test("strings 用后端的下划线参数名，且目标必须被编码", async () => {
  const probe = capture({ strings: [] });
  try {
    await fetchReaStrings("p1", "build/我的 app.exe", { minLength: 6, limit: 12 });
  } finally {
    probe.restore();
  }
  const url = probe.calls[0].url;
  assert.match(url, /min_length=6/);
  assert.match(url, /limit=12/);
  // 空格与中文必须被百分号编码，否则斜杠与空格会把路径切坏
  assert.ok(!url.includes("我的 app.exe"), url);
  assert.ok(url.includes("build%2F"), url);
});

test("investigation 是 POST 且字段用 snake_case", async () => {
  const probe = capture({ state: "ok", findings: [] });
  try {
    await createReaInvestigation({ projectId: "p1", target: "app.asar", title: "看看入口" });
  } finally {
    probe.restore();
  }
  const { url, init } = probe.calls[0];
  assert.equal(url, "/api/v1/reverse-lab/investigations");
  assert.equal(init?.method, "POST");
  assert.deepEqual(JSON.parse(String(init?.body)), {
    project_id: "p1",
    target: "app.asar",
    title: "看看入口",
  });
});

test("适配器四种状态落到三档色调", () => {
  assert.equal(adapterTone("ready"), "ok");
  assert.equal(adapterTone("partial"), "warn");
  assert.equal(adapterTone("failed"), "bad");
  assert.equal(adapterTone("disabled"), "bad");
  // 后端将来加了新状态：不认识的按"不能用"处理，不假装能用
  assert.equal(adapterTone("half-warm"), "bad");
  assert.equal(adapterLabel("partial"), "工具表不全");
});

test("适配器不可用时照抄服务端原话，而不是另编一句", () => {
  const base: AdapterStatus = {
    state: "failed",
    server: "rea-artifacts",
    transport: "stdio",
    root: "<workspace:demo>",
    expected: [],
    declared: [],
    tools: [],
    tools_count: 0,
    missing: [],
    boundary: "unknown",
    connect_ms: null,
    boundary_note: "REA 服务端进程没有起来",
  };
  assert.equal(adapterReason(base), "REA 服务端进程没有起来");
  assert.equal(adapterReason({ ...base, error: "spawn python.exe ENOENT" }), "spawn python.exe ENOENT");
  assert.equal(
    adapterReason({ ...base, missing: ["inspect_target", "find_strings"] }),
    "缺少工具：inspect_target、find_strings",
  );
});

test("字节与偏移的读法", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(999), "999 B");
  assert.equal(formatBytes(1024), "1.0 KB");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(11 * 1024 * 1024), "11 MB");
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(-1), "—");
  assert.equal(offsetLabel(26), "0x0000001a");
  assert.equal(offsetLabel(0x1234abcd), "0x1234abcd");
  assert.equal(offsetLabel(Number.NaN), "0x00000000");
});

test("候选目标的副标题写「像是可执行」，因为幻数只是猜", () => {
  const exe: ReaTarget = { target: "app.exe", size: 2048, format: "PE32+", runnable_guess: true };
  const bin: ReaTarget = { target: "blob.bin", size: 512, format: null, runnable_guess: false };
  assert.equal(targetHint(exe), "PE32+ · 2.0 KB · 像是可执行");
  assert.equal(targetHint(bin), "认不出格式 · 512 B");
  assert.equal(findingLabel({ kind: "entry", value: "main", evidence_id: "ev-1" }), "entry｜main");
  assert.equal(findingLabel({ kind: "", value: "x", evidence_id: "ev-1" }), "结论｜x");
});
