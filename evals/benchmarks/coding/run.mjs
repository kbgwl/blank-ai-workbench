#!/usr/bin/env node
/**
 * v157 §4 coding benchmark runner。
 *
 * 口径：
 * - 独立评测后端（绝不拿日常库）：BLANK_BENCH_API 指向一个按 evals 口径起好的
 *   后端（复制 blank.db + .blank_secret.key，BLANK_DATABASE_URL 指副本，端口 8060）。
 * - provider 两档：
 *     --mock     用内置 openai 兼容 mock（验证 harness，不验证模型）；
 *     --provider <id> 用已配置在评测后端里的真实 provider（真实模型跑分）。
 * - 判分不信任 agent 自述：
 *     1. expectedStatus 必须等于任务终态；
 *     2. testCommand 在工作区真实执行，退出码 0 才算过；
 *     3. expectedFiles 逐文件与 fixture 原始内容比对，被改才算动过；
 *        其余文件被改计入「误修改文件数」；
 *     4. reverse_analysis 任务用 answerKeys 匹配交付文本；
 *     5. cancel_resume 任务判 status==cancelled 且交付文本不含「已完成」。
 *
 * 用法：
 *   node evals/benchmarks/coding/run.mjs --mock --limit 2      # harness 冒烟
 *   node evals/benchmarks/coding/run.mjs --provider <id>       # 真实全量
 *   node evals/benchmarks/coding/run.mjs --report out.json     # 结果落盘
 */
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

const ROOT = path.resolve(import.meta.dirname);
const TASKS_DIR = path.join(ROOT, "tasks");
const FIXTURES_DIR = path.join(ROOT, "fixtures");
const API = process.env.BLANK_BENCH_API ?? "http://127.0.0.1:8060/api/v1";
const PY = process.env.BLANK_BENCH_PYTHON ?? path.join(ROOT, "..", "..", "..", "services", "api", ".venv", "Scripts", "python.exe");

const argv = process.argv.slice(2);
const argOf = (name, fallback = null) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[i + 1] : fallback;
};
const MOCK = argv.includes("--mock");
const PROVIDER_ID = argOf("--provider");
const LIMIT = Number(argOf("--limit", "0")) || Infinity;
const ONLY = argOf("--only"); // 逗号分隔任务 id
const REPORT = argOf("--report");

function die(msg) {
  console.error(`✗ ${msg}`);
  process.exit(1);
}

if (!MOCK && !PROVIDER_ID) die("必须给 --mock 或 --provider <id>，没有模型来源就不许跑——不许静默假装跑过");

/* ── mock provider（openai 兼容最小实现，随 runner 起/停）── */
let mockProc = null;
let mockUrl = "";
async function startMock() {
  // mock 只证明 harness 全链路通（建任务→跑图→判分），不证明模型好——
  // 每次调用都回同一句文本，任务会以「无修改 succeeded」结束，判分器如实记 FAIL。
  mockUrl = "http://127.0.0.1:8391/v1";
  const script = [
    "import http from 'node:http';",
    "http.createServer((req, res) => {",
    "  let body = ''; req.on('data', (c) => (body += c));",
    "  req.on('end', () => {",
    "    let p = {}; try { p = JSON.parse(body || '{}'); } catch {}",
    "    const model = p.model || 'mock-bench';",
    "    res.writeHead(200, {'content-type': 'application/json'});",
    "    res.end(JSON.stringify({id: 'chat', object: 'chat.completion', model: model,",
    "      choices: [{index: 0, message: {role: 'assistant', content: '（mock harness 响应）'}, finish_reason: 'stop'}],",
    "      usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}}));",
    "  });",
    "}).listen(8391, '127.0.0.1', () => console.log('mock-bench ready'));",
  ].join("\n");
  const mockFile = path.join(tmpdir(), `blank-bench-mock-${Date.now()}.mjs`);
  writeFileSync(mockFile, script, "utf8");
  const { spawn } = await import("node:child_process");
  mockProc = spawn(process.execPath, [mockFile], { stdio: "ignore" });
  // 探活：mock 没起来不许开跑，宁可失败也不静默
  for (let i = 0; i < 20; i += 1) {
    try {
      const r = await fetch("http://127.0.0.1:8391/v1/chat/completions", { method: "POST", body: "{}" });
      if (r.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 300));
  }
  die("mock LLM 8391 起不来，终止评测");
}

/* ── HTTP 小工具 ── */
async function api(method, urlPath, body) {
  const resp = await fetch(`${API}${urlPath}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = { raw: text };
  }
  return { status: resp.status, json };
}

async function ensureHealth() {
  const { status, json } = await api("GET", "/health");
  if (status !== 200 || json.status !== "ok") die(`评测后端不可用：${API}（${status} ${JSON.stringify(json)}）`);
}

/* ── 工作区快照（判分用）── */
function snapshot(dir) {
  const out = {};
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isFile()) out[name] = readFileSync(p, "utf8");
  }
  return out;
}

function diffSnapshot(before, after) {
  const changed = [];
  const added = [];
  for (const [name, content] of Object.entries(after)) {
    if (!(name in before)) added.push(name);
    else if (before[name] !== content) changed.push(name);
  }
  return { changed, added };
}

/* ── 单任务执行 ── */
async function runTask(def, providerId) {
  const workdir = path.join(tmpdir(), `blank-bench-${def.id}-${Date.now()}`);
  cpSync(path.join(FIXTURES_DIR, def.id), workdir, { recursive: true });
  // 迁移类任务先 seed data.db
  if (existsSync(path.join(workdir, "init_db.py"))) {
    const seed = spawnSync(PY, ["-X", "utf8", "init_db.py"], { cwd: workdir, encoding: "utf8" });
    if (seed.status !== 0) return { id: def.id, status: "seed_failed", error: seed.stderr?.slice(0, 300) };
  }
  const before = snapshot(workdir);

  const project = await api("POST", "/projects", { name: `bench-${def.id}` });
  if (project.status !== 201) return { id: def.id, status: "setup_failed", error: JSON.stringify(project.json).slice(0, 200) };
  const bind = await api("POST", `/projects/${project.json.id}/workspaces`, { path: workdir });
  if (bind.status !== 201) return { id: def.id, status: "setup_failed", error: JSON.stringify(bind.json).slice(0, 200) };
  const task = await api("POST", `/projects/${project.json.id}/tasks`, {
    title: def.id,
    goal: def.goal,
    acceptance_criteria: def.acceptance,
    max_steps: 40,
  });
  if (task.status !== 201) return { id: def.id, status: "setup_failed", error: JSON.stringify(task.json).slice(0, 200) };

  const t0 = Date.now();
  // /run 是长请求：图跑到中断或终态才返回。绝不能 await 它——否则取消窗口永远
  // 赶不上趟（实测 coding-051：cancel 从第 4 秒被拖到第 109 秒，任务带着
  // notes.txt 的修改"成功"收场）。发出去就轮询，靠任务状态与取消端点推进。
  const runPromise = api("POST", `/tasks/${task.json.id}/run`, { provider_id: providerId }).catch(
    (error) => ({ status: 0, json: { error: String(error).slice(0, 200) } })
  );
  const early = await Promise.race([runPromise, Promise.resolve(null)]);
  if (early && early.status !== 200) {
    return { id: def.id, status: "setup_failed", error: JSON.stringify(early.json).slice(0, 200) };
  }

  // 轮询到终态；取消类任务在指定窗口后主动 cancel，waiting_approval 自动审批
  const cancelAfter = def.runHints?.cancelAfterSteps ?? null;
  // steps_used 只在任务收尾时落库，运行中恒为 0——按它触发取消永远赶不上趟
  //（首轮实测：三次 051 全没发成 cancel，任务跑满 90s 后"成功"）。改为按真实
  // 流逝时间触发；对带长 sleep 的取消类任务，几秒内必然落在命令执行途中。
  const cancelAfterMs = def.runHints?.cancelAfterMs ?? null;
  let final = null;
  let approvals = 0;
  let cancelled = false;
  while (Date.now() - t0 < 10 * 60_000) {
    await new Promise((r) => setTimeout(r, 1000));
    const { json } = await api("GET", `/tasks/${task.json.id}`);
    final = json;
    // 取消窗口探测：到了窗口就发 cancel。没赶上（任务先跑完）就如实记
    // statusMatch=false，不篡改判分。
    const hitCancelWindow =
      (cancelAfterMs !== null && Date.now() - t0 >= cancelAfterMs) ||
      (cancelAfterMs === null &&
        cancelAfter !== null &&
        Number(final.steps_used ?? 0) >= Math.min(1, cancelAfter));
    if (hitCancelWindow && !cancelled) {
      const resp = await api("POST", `/tasks/${task.json.id}/cancel`);
      if (resp.status >= 400) console.log(`\n  [warn] cancel 返回 ${resp.status}: ${JSON.stringify(resp.json).slice(0, 120)}`);
      cancelled = true;
      continue;
    }
    if (json.status === "waiting_approval") {
      approvals += 1;
      // benchmark 环境自动批准 run_tests（scope=once），取消类任务直接拒
      const approve = def.expectedStatus !== "cancelled";
      await api("POST", `/tasks/${task.json.id}/approval`, { approve, provider_id: providerId, scope: "once" });
      continue;
    }
    if (["succeeded", "failed", "cancelled"].includes(json.status)) break;
  }
  const elapsedMs = Date.now() - t0;
  const run = await runPromise;
  if (run.status !== 200) {
    return { id: def.id, status: "setup_failed", error: JSON.stringify(run.json).slice(0, 200) };
  }
  if (!final || !["succeeded", "failed", "cancelled"].includes(final.status)) {
    await api("POST", `/tasks/${task.json.id}/cancel`).catch(() => {});
    return { id: def.id, status: "timeout", elapsedMs, error: final?.status };
  }

  const ev = await api("GET", `/tasks/${task.json.id}/evidence`);
  const after = snapshot(workdir);
  const { changed, added } = diffSnapshot(before, after);
  const touched = [...changed, ...added];
  const expectedTouched = def.expectedFiles.filter((f) => touched.includes(f));
  const unexpectedTouched = touched.filter((f) => !def.expectedFiles.includes(f));

  // testCommand 真实执行
  let testExit = null;
  let testTail = "";
  if (def.expectedStatus === "succeeded") {
    const parts = def.testCommand.split(" ");
    // testCommand 里的裸 python 必须换成评测 venv python：系统 PATH 上的
    // Python 没装 pytest，会让所有 pytest 任务假阴。
    const argv0 = /^python(\.exe)?$/.test(parts[0]) ? PY : parts[0];
    const r = spawnSync(argv0, parts.slice(1), { cwd: workdir, encoding: "utf8", timeout: 60_000, shell: true });
    testExit = r.status;
    testTail = `${r.stdout ?? ""}${r.stderr ?? ""}`.slice(-400);
  }

  const deliveryText = JSON.stringify({
    summary: ev.json?.task?.result_summary ?? "",
    risks: ev.json?.unverified_risks ?? [],
    acceptance: ev.json?.acceptance ?? [],
  });
  const answerHit =
    !def.answerKeys || def.answerKeys.every((k) => deliveryText.includes(k));

  const checks = {
    statusMatch: final.status === def.expectedStatus,
    testPassed: def.expectedStatus === "cancelled" ? true : testExit === 0,
    expectedFilesTouched: def.expectedFiles.length === 0 ? true : expectedTouched.length === def.expectedFiles.length,
    noUnexpectedChanges: unexpectedTouched.length === 0,
    answerHit,
    noFalseDone: final.status !== "succeeded" || !/已完成/.test(deliveryText),
    // 「待审批」= 任务结束时还挂着审批卡；「未获授权」= 该跑的测试没跑成。
    // 两种都算高风险未闭环，别只盯一种措辞——措辞改了缺陷还在就白改了。
    highRiskUnauthorized: !(ev.json?.unverified_risks ?? []).some(
      (r) => typeof r === "string" && /待审批|未获批准/.test(r)
    ),
  };
  const passed = Object.values(checks).every(Boolean);

  const usage = ev.json?.task?.token_usage ?? final.token_usage ?? {};
  return {
    id: def.id,
    category: def.category,
    status: final.status,
    expectedStatus: def.expectedStatus,
    stepsUsed: final.steps_used,
    elapsedMs,
    approvals,
    changedFiles: touched,
    unexpectedTouched,
    testExit,
    testTail: testTail.slice(-200),
    tokenUsage: usage,
    checks,
    passed,
  };
}

/* ── 主流程 ── */
await ensureHealth();
if (MOCK) await startMock();
let providerId = PROVIDER_ID;
if (MOCK) {
  const created = await api("POST", "/providers", {
    name: `bench-mock-${Date.now().toString(36)}`,
    kind: "openai_compatible",
    base_url: mockUrl,
    model: "mock-bench",
    api_key: "sk-bench-mock",
    supports_tools: true,
  });
  if (created.status !== 201) die(`mock provider 创建失败：${JSON.stringify(created.json)}`);
  providerId = created.json.id;
}

const defs = readdirSync(TASKS_DIR)
  .filter((f) => f.endsWith(".json"))
  .map((f) => JSON.parse(readFileSync(path.join(TASKS_DIR, f), "utf8")))
  .filter((d) => (ONLY ? ONLY.split(",").map((s) => s.trim()).includes(d.id) : true))
  .slice(0, LIMIT);

console.log(`开始评测 ${defs.length} 个任务（provider=${MOCK ? "mock" : providerId}）`);
const results = [];
for (const def of defs) {
  process.stdout.write(`- ${def.id} (${def.category}) … `);
  let result;
  try {
    result = await runTask(def, providerId);
  } catch (error) {
    result = { id: def.id, category: def.category, status: "runner_error", error: String(error).slice(0, 200), passed: false };
  }
  results.push(result);
  console.log(result.passed ? "PASS" : `FAIL (${result.status}${result.error ? `: ${result.error}` : ""})`);
}

/* 汇总 §4.2 指标 */
const total = results.length;
const succeeded = results.filter((r) => r.status === "succeeded").length;
const firstTryPassed = results.filter((r) => r.passed).length;
const testPassed = results.filter((r) => r.checks?.testPassed).length;
const misEdited = results.reduce((sum, r) => sum + (r.unexpectedTouched?.length ?? 0), 0);
const avgMs = total ? Math.round(results.reduce((s, r) => s + (r.elapsedMs ?? 0), 0) / total) : 0;
const tokens = results.reduce((s, r) => s + Number(r.tokenUsage?.total_tokens ?? r.tokenUsage?.total ?? 0), 0);
const toolCalls = results.reduce((s, r) => s + Number(r.stepsUsed ?? 0), 0);
const summary = {
  total,
  completionRate: total ? `${Math.round((succeeded / total) * 100)}%` : "n/a",
  passRate: total ? `${Math.round((firstTryPassed / total) * 100)}%` : "n/a",
  testPassRate: total ? `${Math.round((testPassed / total) * 100)}%` : "n/a",
  misEditedFiles: misEdited,
  avgElapsedMs: avgMs,
  totalTokens: tokens,
  totalSteps: toolCalls,
  falseDoneCount: results.filter((r) => !r.checks?.noFalseDone).length,
  unauthorizedHighRisk: results.filter((r) => !r.checks?.highRiskUnauthorized).length,
};
console.log("\n===== 基线汇总 =====");
console.log(JSON.stringify(summary, null, 2));

if (REPORT) {
  mkdirSync(path.dirname(path.resolve(REPORT)), { recursive: true });
  writeFileSync(path.resolve(REPORT), JSON.stringify({ summary, results }, null, 2), "utf8");
  console.log(`报告已写入 ${path.resolve(REPORT)}`);
}

if (mockProc) mockProc.kill();
const failed = results.filter((r) => !r.passed);
if (failed.length) {
  console.log(`\n未通过 ${failed.length} 例：${failed.map((f) => f.id).join(", ")}`);
}
