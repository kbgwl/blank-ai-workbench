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
import path from "node:path";
import process from "node:process";

const ROOT = path.resolve(import.meta.dirname);
// 工作区固定落 E 盘：Windows 默认 os.tmpdir() 在 C 盘，评测每跑一次就复制一份
// 工作区；本机 C 盘空间紧张。需要换位置时用 BLANK_BENCH_WORK_ROOT 覆盖。
const WORK_ROOT = path.resolve(
  process.env.BLANK_BENCH_WORK_ROOT ?? path.join(ROOT, "..", "..", "..", ".bench-tmp")
);
mkdirSync(WORK_ROOT, { recursive: true });
// 任务与 fixture 目录可换：日常场景的题放在 benchmarks/daily，复用同一套判分。
let TASKS_DIR = path.join(ROOT, "tasks");
let FIXTURES_DIR = path.join(ROOT, "fixtures");
const API = process.env.BLANK_BENCH_API ?? "http://127.0.0.1:8060/api/v1";
const PY = process.env.BLANK_BENCH_PYTHON ?? path.join(ROOT, "..", "..", "..", "services", "api", ".venv", "Scripts", "python.exe");

const argv = process.argv.slice(2);
const argOf = (name, fallback = null) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[i + 1] : fallback;
};
const TASKS_ARG = argOf("--tasks-dir");
const FIXTURES_ARG = argOf("--fixtures-dir");
if (TASKS_ARG) TASKS_DIR = path.resolve(TASKS_ARG);
if (FIXTURES_ARG) FIXTURES_DIR = path.resolve(FIXTURES_ARG);
const MOCK = argv.includes("--mock");
const PROVIDER_ID = argOf("--provider");
const LIMIT = Number(argOf("--limit", "0")) || Infinity;
const ONLY = argOf("--only") ?? argOf("--task"); // 逗号分隔任务 id（--task 是 --only 的别名）
const REPORT = argOf("--report");
const PAUSE_SECONDS = Number(argOf("--pause", "0")) || 0;
// 产品间对比用：goal 直接是 CLI 产品收到的同一段原文，不再单发验收条件/约束。
// 判分口径完全不变（仍按 testCommand + expectedFiles 硬比对）。
const PLAIN_PROMPT = argv.includes("--plain-prompt");
// Anthropic 式严格模式：
//   隐藏测试——工作区里 test_*.py / conftest.py 在开跑前移走，判分时才装回；
//   参考对照——refs/<id>/ 的参考实现必须让同一套隐藏测试全绿，且原始 fixture 必须红；
//   独立多采样——每题重复 N 次，每轮全新工作区，报 pass@1/pass@k。
const HIDDEN_TESTS = argv.includes("--hidden-tests");
const CONTROL = argv.includes("--control");
const TRIALS = Math.max(1, Number(argOf("--trials", "1")) || 1);
// 单次采样的等待窗口。默认 10 分钟是 2026-11-01 基线（平均 3 分钟/题）的十倍余量；
// 供应商变慢时用 --trial-minutes 放宽，超窗如实记 timeout（不进有效采样分母）。
const TRIAL_MINUTES = Math.max(0.05, Number(argOf("--trial-minutes", process.env.BLANK_BENCH_TRIAL_MINUTES ?? "10")) || 10);
const HIDDEN_DIR = path.join(ROOT, "hidden");
const REFS_DIR = path.join(ROOT, "refs");
const TEST_FILE_RE = /^(test_.*\.py|.*_test\.py|conftest\.py)$/;

function die(msg) {
  console.error(`✗ ${msg}`);
  process.exit(1);
}

/**
 * 这段话是不是在说「供应商那边不行」，而不是「Agent 能力不行」。
 *
 * 一条规则两处用：任务跑到终态的 `failure_reason`，和 `/run` 直接返回的错误体。
 * 早先只有前者走了这条判断，后者被一律记成 `setup_failed`——于是 glm 免费档的
 * 429 在汇总里变成「环境没搭好」（setupFailed=10，其中 7 例其实是限流），
 * 既不重跑也不进 provider 分类，报告读起来像装置坏了。
 */
const PROVIDER_FAILURE_RE =
  /429|rate.?limit|限流|model_unreachable|model_timeout|模型不可达|模型请求超时|模型返回 5\d\d|fetch failed|ECONNRESET|ETIMEDOUT|502|503|504/i;
const isProviderFailure = (text) => PROVIDER_FAILURE_RE.test(String(text ?? ""));

import { detectFalseClaim, detectZeroChangeSuccess } from "../../lib/judgement.mjs";
import { fetchJson } from "../../lib/fetch-json.mjs";
import { requestLong } from "../../lib/long-request.mjs";

if (!MOCK && !PROVIDER_ID && !CONTROL) die("必须给 --mock 或 --provider <id>，没有模型来源就不许跑——不许静默假装跑过");

/* ── mock provider（openai 兼容最小实现，随 runner 起/停）── */
let mockProc = null;
let mockUrl = "";
async function startMock() {
  // mock 只证明 harness 全链路通（建任务→跑图→判分），不证明模型好——
  // 每次调用都回同一句文本，任务会以「无修改 succeeded」结束，判分器如实记 FAIL。
  mockUrl = "http://127.0.0.1:8391/v1";
  // MOCK_DELAY_MS：只在装置自检时用。把 mock 响应压慢，能复现「轮询窗口到期但
  // /run 还没返回」这条路径，验证不会把已成功任务记成超时（见 2026-11-16 修正）。
  const mockDelayMs = Number(process.env.MOCK_DELAY_MS ?? 0) || 0;
  const mockFile = path.join(WORK_ROOT, `blank-bench-mock-${Date.now()}.mjs`);
  const mockLog = `${mockFile}.hits`;
  const script = [
    "import http from 'node:http';",
    "import fs from 'node:fs';",
    `const logFile = ${JSON.stringify(mockLog)};`,
    "let hits = 0;",
    "http.createServer((req, res) => {",
    "  let body = ''; req.on('data', (c) => (body += c));",
    "  req.on('end', () => {",
    "    let p = {}; try { p = JSON.parse(body || '{}'); } catch {}",
    "    const model = p.model || 'mock-bench';",
    "    hits += 1;",
    "    fs.appendFileSync(logFile, `${Date.now()} hit ${hits}\\n`);",
    `    setTimeout(() => {`,
    "      res.writeHead(200, {'content-type': 'application/json'});",
    "      res.end(JSON.stringify({id: 'chat', object: 'chat.completion', model: model,",
    "        choices: [{index: 0, message: {role: 'assistant', content: '（mock harness 响应）'}, finish_reason: 'stop'}],",
    "        usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}}));",
    `    }, p.model ? ${mockDelayMs} : 0);`,
    "  });",
    "}).listen(8391, '127.0.0.1', () => console.log('mock-bench ready'));",
  ].join("\n");
  writeFileSync(mockFile, script, "utf8");
  if (mockDelayMs) console.log(`  mock 响应延迟 ${mockDelayMs}ms，命中记录 ${mockLog}`);
  const { spawn } = await import("node:child_process");
  mockProc = spawn(process.execPath, [mockFile], { stdio: ["ignore", "ignore", "inherit"] });
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
  let resp;
  // 后端有 120 RPM 限流（mock 模式跑得快容易撞）：按 retry-after 退避重试，不静默吞掉。
  // GET 的连接级错误交给 fetchJson 重试（keep-alive 竞争实测见 2026-11-16 条目）。
  for (let attempt = 0; ; attempt += 1) {
    resp = await fetchJson(`${API}${urlPath}`, { method, body });
    if (resp.status !== 429 || attempt >= 3) break;
    const wait = Math.min(35, Number(resp.json?.retry_after ?? "5") || 5);
    console.log(`  ⧗ 429 限流，${wait}s 后重试（${urlPath}）`);
    await new Promise((r) => setTimeout(r, wait * 1000));
  }
  return resp;
}

/**
 * 长请求专用：全局 fetch（undici）默认 300s bodyTimeout，`POST /tasks/{id}/run`
 * 跑超 5 分钟就会抛 `fetch failed`——任务其实还在跑，却被外层当成供应商失败重跑
 * （2026-11-16 实测：coding-021 t1 在 07:41:31 判 provider_failed 并 90s 后新建
 * 重跑任务，而后端 b98c4176 在 07:41:40 succeeded）。requestLong 走 node:http，
 * 不设客户端超时，响应时长由任务本身决定。
 */
async function apiLong(method, urlPath, body) {
  let resp;
  for (let attempt = 0; ; attempt += 1) {
    resp = await requestLong(`${API}${urlPath}`, method, body);
    if (resp.status !== 429 || attempt >= 3) break;
    const wait = Math.min(35, Number(resp.json?.retry_after ?? "5") || 5);
    console.log(`  ⧗ 429 限流，${wait}s 后重试（${urlPath}）`);
    await new Promise((r) => setTimeout(r, wait * 1000));
  }
  return resp;
}

async function ensureHealth() {
  const { status, json } = await api("GET", "/health");
  if (status !== 200 || json.status !== "ok") die(`评测后端不可用：${API}（${status} ${JSON.stringify(json)}）`);
}

/* ── 工作区快照（判分用）── */
// 递归收集相对路径（统一用 /）：日常任务会建子目录（如 整理/文本/），
// 只看顶层会把子目录里的改动全漏掉。缓存目录跳过，否则 pytest 自己写的
// .pytest_cache / __pycache__ 会被记成「误修改文件」。
const SNAPSHOT_SKIP = new Set([".git", ".blank", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]);
function snapshot(dir, base = dir) {
  const out = {};
  for (const name of readdirSync(dir)) {
    if (SNAPSHOT_SKIP.has(name)) continue;
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) {
      Object.assign(out, snapshot(p, base));
    } else {
      out[path.relative(base, p).split(path.sep).join("/")] = readFileSync(p, "utf8");
    }
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

/**
 * 隐藏测试清单 = fixture 里原本的测试文件 ∪ hidden/<id>/ 里的增强测试。
 * 增强文件同名时以 hidden/ 为准（判分时覆盖装回）。
 */
function hiddenNames(def) {
  const fixtureDir = path.join(FIXTURES_DIR, def.id);
  const fromFixture = existsSync(fixtureDir) ? readdirSync(fixtureDir).filter((n) => TEST_FILE_RE.test(n)) : [];
  const extraDir = path.join(HIDDEN_DIR, def.id);
  const fromHidden = existsSync(extraDir) ? readdirSync(extraDir).filter((n) => statSync(path.join(extraDir, n)).isFile()) : [];
  return [...new Set([...fromFixture, ...fromHidden])];
}

function hiddenSource(def, name) {
  const enhanced = path.join(HIDDEN_DIR, def.id, name);
  return existsSync(enhanced) ? enhanced : path.join(FIXTURES_DIR, def.id, name);
}

// 开跑前把测试移出工作区：模型只能看到业务文件与题面，读不到判据。
function hideTests(workdir, names) {
  const removed = [];
  for (const name of names) {
    const p = path.join(workdir, name);
    if (existsSync(p)) {
      rmSync(p, { force: true });
      removed.push(name);
    }
  }
  return removed;
}

// 判分前装回原版测试；agent 若自己写过同名文件，覆盖并以 hiddenTouched 记账。
function installHiddenTests(def, workdir) {
  const names = hiddenNames(def);
  for (const name of names) cpSync(hiddenSource(def, name), path.join(workdir, name));
  return names;
}

// 装回后逐字节比对：判分用的必须是外部原版，不是 agent 写的同名文件。
function hiddenTestsIntact(def, workdir) {
  return hiddenNames(def).every((name) => {
    const p = path.join(workdir, name);
    return existsSync(p) && readFileSync(p, "utf8") === readFileSync(hiddenSource(def, name), "utf8");
  });
}

function testCommandArgv(def) {
  const parts = def.testCommand.split(" ");
  const argv0 = /^python(\.exe)?$/.test(parts[0]) ? PY : parts[0];
  return [argv0, parts.slice(1)];
}

function runTestCommand(def, workdir) {
  // 这批 fixture 的测试是 pytest 风格（只有 def test_* 定义），直接 `python test_x.py`
  // 会定义完函数就 0 退出——判据空转。参考对照第一次跑就抓到 50/55 题如此：
  // 原始 fixture 也「通过」。所以只要工作区里有 test_*.py，就一律交给 pytest 收集执行；
  // 没有测试文件的任务（如纯文档/取消类）才回退到任务书自带的 testCommand。
  const testFiles = readdirSync(workdir).filter((n) => /^(test_.*\.py|.*_test\.py)$/.test(n)).sort();
  if (testFiles.length) {
    const r = spawnSync(PY, ["-X", "utf8", "-m", "pytest", "-q", ...testFiles], {
      cwd: workdir,
      encoding: "utf8",
      timeout: 60_000,
    });
    return { exit: r.status, tail: `${r.stdout ?? ""}${r.stderr ?? ""}`.slice(-400) };
  }
  const [argv0, rest] = testCommandArgv(def);
  const r = spawnSync(argv0, rest, { cwd: workdir, encoding: "utf8", timeout: 60_000, shell: true });
  return { exit: r.status, tail: `${r.stdout ?? ""}${r.stderr ?? ""}`.slice(-400) };
}

/**
 * 参考对照（Anthropic 的 harness 自检）：
 *   - 原始 fixture 跑隐藏测试必须红（fail-to-pass 真的存在）；
 *   - refs/<id>/ 覆盖后必须绿；
 * 任一不满足 => 这次读数作废，runner 以 rc=2 退出。
 */
function runControl(def) {
  const base = path.join(WORK_ROOT, `blank-control-${def.id}-${Date.now()}`);
  cpSync(path.join(FIXTURES_DIR, def.id), base, { recursive: true });
  const hidden = hideTests(base, hiddenNames(def));
  const leaked = readdirSync(base).filter((n) => TEST_FILE_RE.test(n));
  installHiddenTests(def, base);
  const pristine = runTestCommand(def, base);

  const refDir = path.join(REFS_DIR, def.id);
  if (!existsSync(refDir)) {
    return { id: def.id, control: true, passed: false, error: "缺 refs/<id> 参考实现", pristineExit: pristine.exit };
  }
  const refWork = path.join(WORK_ROOT, `blank-control-ref-${def.id}-${Date.now()}`);
  cpSync(path.join(FIXTURES_DIR, def.id), refWork, { recursive: true });
  hideTests(refWork, hiddenNames(def));
  for (const name of readdirSync(refDir)) cpSync(path.join(refDir, name), path.join(refWork, name));
  installHiddenTests(def, refWork);
  const ref = runTestCommand(def, refWork);
  const missing = def.expectedFiles.filter((f) => !existsSync(path.join(refWork, f)));
  const intact = hiddenTestsIntact(def, refWork);
  const passed = pristine.exit !== 0 && ref.exit === 0 && missing.length === 0 && leaked.length === 0 && intact;
  return {
    id: def.id,
    control: true,
    hiddenFiles: hidden,
    leakedTests: leaked,
    hiddenRemoved: hidden.length,
    testsIntact: intact,
    pristineExit: pristine.exit,
    pristineTail: pristine.tail.slice(-160),
    refExit: ref.exit,
    refTail: ref.tail.slice(-160),
    missingExpected: missing,
    passed,
  };
}


/**
 * 与 CLI 产品（run-wb.mjs / run-qoder.mjs 的 buildPrompt）逐字相同的任务文本。
 * 产品间同提示词盲评时用它当 goal：模型读到的任务描述一致，差异只剩系统提示与
 * 工具面——那才是产品本身。
 */
function plainPrompt(def) {
  const lines = [def.goal, "", "验收标准：", ...def.acceptance.map((a) => `- ${a}`)];
  if (def.expectedFiles.length) {
    lines.push(
      "",
      `只允许创建或修改这些文件：${def.expectedFiles.join("、")}。其余文件（尤其测试文件）一律不要改动。`
    );
  }
  return lines.join("\n");
}

/* ── 单任务执行 ── */
async function runTask(def, providerId, trial = 1) {
  const workdir = path.join(WORK_ROOT, `blank-bench-${def.id}-t${trial}-${Date.now()}`);
  cpSync(path.join(FIXTURES_DIR, def.id), workdir, { recursive: true });
  // 迁移类任务先 seed data.db
  if (existsSync(path.join(workdir, "init_db.py"))) {
    const seed = spawnSync(PY, ["-X", "utf8", "init_db.py"], { cwd: workdir, encoding: "utf8" });
    if (seed.status !== 0) return { id: def.id, trial, status: "seed_failed", error: seed.stderr?.slice(0, 300) };
  }
  // 严格模式：测试在开跑前移出工作区。档案里的「隐藏文件数」是判分自检的一部分。
  const hidden = HIDDEN_TESTS ? hiddenNames(def) : [];
  if (HIDDEN_TESTS) {
    hideTests(workdir, hidden);
    const leaked = hidden.filter((n) => existsSync(path.join(workdir, n)));
    // 隐藏不成功就不许开跑：否则模型看到判据，整批读数失真。
    if (leaked.length) {
      return { id: def.id, trial, status: "runner_error", error: `测试未隐藏：${leaked.join(",")}`, passed: false };
    }
  }
  const before = snapshot(workdir);

  const project = await api("POST", "/projects", { name: `bench-${def.id}` });
  if (project.status !== 201) return { id: def.id, status: "setup_failed", error: JSON.stringify(project.json).slice(0, 200) };
  const bind = await api("POST", `/projects/${project.json.id}/workspaces`, { path: workdir });
  if (bind.status !== 201) return { id: def.id, status: "setup_failed", error: JSON.stringify(bind.json).slice(0, 200) };
  const task = await api("POST", `/projects/${project.json.id}/tasks`, {
    title: def.id,
    goal: PLAIN_PROMPT ? plainPrompt(def) : def.goal,
    acceptance_criteria: PLAIN_PROMPT ? [] : def.acceptance,
    // 日常题显式带 scenario；编程题保持默认 dev。
    scenario: def.scenario || "dev",
    max_steps: 40,
    // §3.4 文件边界提示：expectedFiles 必须让模型看见才算约束（产品侧 initial_messages
    // 会把 goal_spec.constraints 渲染进首轮提示）。判分仍按 expectedFiles 硬比对，不放宽。
    goal_spec: PLAIN_PROMPT
      ? undefined
      : def.expectedFiles.length
      ? {
          constraints: [
            `只允许创建或修改这些文件：${def.expectedFiles.join("、")}。其余文件（尤其测试文件）一律不要改动。`,
          ],
        }
      : undefined,
  });
  if (task.status !== 201) return { id: def.id, status: "setup_failed", error: JSON.stringify(task.json).slice(0, 200) };

  const t0 = Date.now();
  // /run 是长请求：图跑到中断或终态才返回。绝不能 await 它——否则取消窗口永远
  // 赶不上趟（实测 coding-051：cancel 从第 4 秒被拖到第 109 秒，任务带着
  // notes.txt 的修改"成功"收场）。发出去就轮询，靠任务状态与取消端点推进。
  // 走 apiLong 而不是 fetch：后者 300s bodyTimeout 会把长任务掐断并误判成供应商失败。
  const runPromise = apiLong("POST", `/tasks/${task.json.id}/run`, { provider_id: providerId }).catch(
    (error) => ({ status: 0, json: { error: String(error).slice(0, 200) } })
  );
  const early = await Promise.race([runPromise, Promise.resolve(null)]);
  if (early && early.status !== 200) {
    const detail = JSON.stringify(early.json).slice(0, 200);
    // 首轮模型调用就 429/超时时，`/run` 会即刻把错误体交回来。这不是装置没搭好，
    // 是供应商按住了闸门——单独一类，交给外层按 provider_failed 重跑。
    // 重跑会新建工作区；被放弃的这个后端任务必须收掉，否则它继续占着审批/子进程。
    await api("POST", `/tasks/${task.json.id}/cancel`).catch(() => {});
    return { id: def.id, status: isProviderFailure(detail) ? "provider_failed" : "setup_failed", error: detail };
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
  const TERMINAL = ["succeeded", "failed", "cancelled"];
  while (Date.now() - t0 < TRIAL_MINUTES * 60_000) {
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
  const windowExpired = !final || !TERMINAL.includes(final.status);
  // `/run` 可能比轮询窗口晚很久才返回（2026-11-16 实测：coding-008 单题 25 分钟）。
  // 窗口结束时 `final` 还是旧的 running 快照，直接据此判超时会把已经成功的任务
  // 记成 timeout 再 cancel 掉。这里以服务端最新状态为准；请求仍未返回时最多再等
  // 一分钟，之后主动 cancel 并给后端 30 秒落终态。
  const run = await Promise.race([
    runPromise,
    new Promise((resolve) => setTimeout(() => resolve(null), 60_000)),
  ]);
  if (!final || !TERMINAL.includes(final.status)) {
    const refreshed = await api("GET", `/tasks/${task.json.id}`).catch(() => ({}));
    if (refreshed.json?.status) final = refreshed.json;
  }
  if (!final || !TERMINAL.includes(final.status)) {
    await api("POST", `/tasks/${task.json.id}/cancel`).catch(() => {});
    for (let i = 0; i < 30; i += 1) {
      await new Promise((r) => setTimeout(r, 1000));
      const { json } = await api("GET", `/tasks/${task.json.id}`).catch(() => ({}));
      if (json?.status && TERMINAL.includes(json.status)) {
        final = json;
        break;
      }
    }
  }
  if (run && run.status !== 200 && !(final && TERMINAL.includes(final.status))) {
    // 供应商限流/不可达（429 等）不是装置坏：归到 provider_failed 走外层退避重试，
    // 不让它静默记成 setup_failed（装置问题）从而被漏掉重跑（上一轮整例 429 即此路径）。
    // 重跑会新建工作区；后端任务若还活着必须收掉。
    const detail = JSON.stringify(run.json).slice(0, 200);
    await api("POST", `/tasks/${task.json.id}/cancel`).catch(() => {});
    return { id: def.id, status: isProviderFailure(detail) ? "provider_failed" : "setup_failed", error: detail };
  }
  if (!final || !TERMINAL.includes(final.status)) {
    await api("POST", `/tasks/${task.json.id}/cancel`).catch(() => {});
    return { id: def.id, trial, status: "timeout", elapsedMs, error: final?.status };
  }
  // 超窗后我们自己 cancel 出来的 cancelled 不是取消类任务的「预期取消」——
  // 它是没能在窗口内收尾，按 timeout 记（不进有效采样分母），不冒充取消能力。
  if (windowExpired && final.status === "cancelled" && def.expectedStatus !== "cancelled") {
    return { id: def.id, trial, status: "timeout", elapsedMs, error: "window_expired" };
  }

  const ev = await api("GET", `/tasks/${task.json.id}/evidence`);
  // 供应商失败不能当成 Agent 能力失败（任务书 §2.5）：限流/断网时模型连话都没说完，
  // 把这种终态算进完成率，等于用别家的故障给自己的基线扣分；反过来把它算成通过，
  // 又是拿故障冒充能力。单独一类，逐例打印原因，报告里能指名道姓。
  const failureReason = String(ev.json?.task?.failure_reason ?? "");
  const providerFailure = isProviderFailure(failureReason);
  if (providerFailure) console.log(`\n  [provider] ${def.id}: ${failureReason.slice(0, 120)}`);
  const after = snapshot(workdir);
  const { changed, added } = diffSnapshot(before, after);
  // 隐藏测试被 agent 自己写出来/改掉：先记账，再无条件用原版覆盖后才判分。
  // 它不计入「误修改文件」（那是业务文件纪律），但单独列出——判分不靠 agent 的测试。
  const hiddenTouched = hidden.filter((f) => f in after);
  const touched = [...changed, ...added].filter((f) => !hidden.includes(f));
  const expectedTouched = def.expectedFiles.filter((f) => touched.includes(f));
  // 任务定义可声明 testSideEffectFiles：pytest 一跑就会写出的数据文件（如 save() 落盘）
  // 不算 agent 误改——但 changedFiles 仍如实记录全集，报告里看得到。
  const sideEffects = def.testSideEffectFiles ?? [];
  // 只算 agent 自己动的文件：测试跑出来的数据文件（sideEffects）不算它的改动，
  // 否则「跑了一次测试留下一个 .db」会把零改动伪成功洗白。
  const agentTouched = touched.filter((f) => !sideEffects.includes(f));
  const unexpectedTouched = touched.filter(
    (f) => !def.expectedFiles.includes(f) && !sideEffects.includes(f),
  );

  // testCommand 真实执行。严格模式下先把原版隐藏测试装回（覆盖 agent 同名文件），
  // 判分只认外部测试，不认 agent 自己写的验证。
  let testExit = null;
  let testTail = "";
  if (def.expectedStatus === "succeeded") {
    if (HIDDEN_TESTS) {
      installHiddenTests(def, workdir);
      if (!hiddenTestsIntact(def, workdir)) {
        return { id: def.id, trial, status: "runner_error", error: "隐藏测试装回校验失败", passed: false };
      }
    }
    const r = runTestCommand(def, workdir);
    testExit = r.exit;
    testTail = r.tail;
  }

  // 交付正文单独取：risks 里会原样引用验收条件（例如「交付文本不含「已完成」」），
  // 拿整包 JSON 去测「有没有声称完成」会把验收条款本身的字面命中，判成假完成。
  const summaryText = String(ev.json?.task?.result_summary ?? "");
  const deliveryText = JSON.stringify({
    summary: summaryText,
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
    // falseDone 的语义是「错误声称完成」：终态没成功却声称完成才算。
    // 成功任务交付里出现「已完成」与事实一致（文件真改了、测试真过了、无未闭环风险），
    // 不算假完成；成功但带未闭环高风险由 highRiskUnauthorized 独立把关。
    // 只看交付正文（summary），不看 risks/acceptance 这类元数据文本。
    noFalseDone:
      final.status === "succeeded" || !/已完成|任务完成/.test(summaryText),
    // noFalseClaim：终态 succeeded 却"声称验证过"而真测试没过（coding-042 这类）。
    // 由 judgement.detectFalseClaim 统一判断，避免这里的口径和门禁对不上。
    noFalseClaim: detectFalseClaim({ status: final.status, summaryText, testExit }),
    // 零改动伪成功：要求动文件、终态 succeeded、却一个文件都没动（coding-035）。
    // 它不算"说错话"，而是"什么都没做就报成功"，所以是独立的一格。
    noZeroChangeFalseSuccess: detectZeroChangeSuccess({
      status: final.status,
      expectedFiles: def.expectedFiles,
      changedFiles: agentTouched,
    }),
    // 「待审批」= 任务结束时还挂着审批卡；「未获授权」= 该跑的测试没跑成。
    // 两种都算高风险未闭环，别只盯一种措辞——措辞改了缺陷还在就白改了。
    highRiskUnauthorized: !(ev.json?.unverified_risks ?? []).some(
      (r) => typeof r === "string" && /待审批|未获批准/.test(r)
    ),
  };
  const passed = Object.values(checks).every(Boolean);
  // Anthropic 口径的「解出」：终态符合预期且外部隐藏测试全绿。文件纪律仍由 passed
  // （全部 checks）单独把关，两者分开报，避免把「多写了一个文件」混进解出率。
  const solved =
    final.status === def.expectedStatus &&
    (def.expectedStatus === "cancelled" ? true : testExit === 0);

  // 证据包的 usage 段是唯一权威（§4.3）：按账本行汇总，带 by_model 与来源标记。
  // 旧路径 task.token_usage 从来不存在——之前 totalTokens=0 是读错了地方，不是没账。
  const eu = ev.json?.usage ?? {};
  const usage = {
    total_tokens: Number(eu.total_tokens ?? 0),
    prompt_tokens: Number(eu.prompt_tokens ?? 0),
    completion_tokens: Number(eu.completion_tokens ?? 0),
    cached_tokens: Number(eu.cached_tokens ?? 0),
    reasoning_tokens: Number(eu.reasoning_tokens ?? 0),
    calls: Number(eu.calls ?? 0),
    // provider=供应商回的；unknown=没回（全零不等于免费）。
    usage_source: (eu.usage_sources ?? []).join(",") || "unknown",
    // 按行计数：一组调用里丢一次 usage，集合就会同时出现 provider 与 unknown，
    // 只看集合会把"只丢了一次"读成"整段没账"。
    usage_source_calls: eu.usage_source_calls ?? {},
    models: eu.models ?? [],
    by_model: eu.by_model ?? {},
  };
  return {
    id: def.id,
    trial,
    category: def.category,
    hiddenFiles: hidden.length,
    hiddenTouched,
    solved,
    // provider_failed 只表示"这一例的终态是供应商造成的"，判分细节仍原样留在 checks 里。
    status: providerFailure ? "provider_failed" : final.status,
    agentStatus: final.status,
    failureReason,
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
const defs = readdirSync(TASKS_DIR)
  .filter((f) => f.endsWith(".json"))
  .map((f) => JSON.parse(readFileSync(path.join(TASKS_DIR, f), "utf8")))
  .filter((d) => (ONLY ? ONLY.split(",").map((s) => s.trim()).includes(d.id) : true))
  .slice(0, LIMIT);

// 参考对照先跑：对照不过，后面模型读数一律作废（与 uibench 的 rc=2 同一口径）。
let controls = [];
if (CONTROL) {
  console.log(`参考对照：${defs.length} 题（原始 fixture 跑隐藏测试必须红，refs/ 必须绿）`);
  controls = defs.map(runControl);
  for (const c of controls) {
    console.log(
      `  ${c.passed ? "✓" : "✗"} ${c.id} 原始 exit=${c.pristineExit} 参考 exit=${c.refExit}` +
        (c.hiddenRemoved !== undefined ? ` 移出测试=${c.hiddenRemoved} 装回一致=${c.testsIntact}` : "") +
        (c.error ? ` (${c.error})` : "") +
        (c.missingExpected?.length ? ` 缺文件=${c.missingExpected.join(",")}` : "")
    );
    if (!c.passed) console.log(`    原始尾部: ${c.pristineTail}\n    参考尾部: ${c.refTail}`);
  }
  const bad = controls.filter((c) => !c.passed);
  if (bad.length) {
    console.error(`✗ 参考对照未通过 ${bad.length} 题：${bad.map((c) => c.id).join(", ")}——本次读数作废，不出分`);
    if (REPORT) {
      mkdirSync(path.dirname(path.resolve(REPORT)), { recursive: true });
      writeFileSync(path.resolve(REPORT), JSON.stringify({ controls, summary: { control: "failed" } }, null, 2), "utf8");
    }
    process.exit(2);
  }
  console.log(`✓ 参考对照 ${controls.length}/${controls.length} 通过`);
  if (!MOCK && !PROVIDER_ID) {
    // 只跑对照时也落盘：testsIntact / leakedTests 是「判据没被模型污染」的证据。
    if (REPORT) {
      mkdirSync(path.dirname(path.resolve(REPORT)), { recursive: true });
      writeFileSync(path.resolve(REPORT), JSON.stringify({ controls, summary: { control: "passed" } }, null, 2), "utf8");
    }
    process.exit(0);
  }
}

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

console.log(`开始评测 ${defs.length} 个任务 × ${TRIALS} 次（provider=${MOCK ? "mock" : providerId}${HIDDEN_TESTS ? "，隐藏测试" : ""}）`);
const results = [];
for (const def of defs) {
  for (let trial = 1; trial <= TRIALS; trial += 1) {
  process.stdout.write(`- ${def.id} t${trial}/${TRIALS} (${def.category}) … `);
  let result;
  let attempts = 0;
  // 供应商限流（429）不是这例能力不够，给它干净的机会再判：
  // 每次重跑都换新工作区（runTask 自己建目录），不继承上一次的改动。
  // 重跑次数逐例记进 attempts，报告里能看到哪些例子是第二次才跑出来的。
  //
  // 等待从 60s 拉到 90s/180s：glm 免费档的 1302 是账户级限流，实测 60s 不够退干净
  // （上一轮 4 例 provider_failed 在 60s 重跑后仍被 429 打死）。两次重跑共 3 回，
  // 仍不行就如实记 provider_failed——不拿重跑次数去凑完成率。
  const PROVIDER_RETRIES = 2;
  const PROVIDER_WAITS = [90_000, 180_000];
  for (;;) {
    attempts += 1;
    try {
      result = await runTask(def, providerId, trial);
    } catch (error) {
      // 本地连接抖一下（fetch failed / ECONNRESET / 5xx）既不该记成 Agent 失败，
      // 也不该记成装置坏了（runner_error）——它就是供应商那一侧的网络问题，和 429 同属
      // 一类，按 provider_failed 重跑（90s/180s 退避，最多 2 次）。
      // 只有非网络的异常（harness 自身 bug）才记 runner_error 且只重跑一次（30s）。
      const isNetwork = isProviderFailure(error) || /fetch failed|ECONNRESET|ETIMEDOUT|network|socket/i.test(String(error));
      if (isNetwork) {
        result = { id: def.id, category: def.category, status: "provider_failed", error: String(error).slice(0, 200), passed: false };
      } else {
        result = { id: def.id, category: def.category, status: "runner_error", error: String(error).slice(0, 200), passed: false };
        if (attempts < 2) {
          console.log(`\n  ↻ 本地 harness 异常，30s 后重跑这一例（第 ${attempts + 1} 次）`);
          await new Promise((resolve) => setTimeout(resolve, 30_000));
          continue;
        }
        break;
      }
    }
    if (result.status !== "provider_failed" || attempts > PROVIDER_RETRIES) break;
    const wait = PROVIDER_WAITS[attempts - 1];
    // 把原因打出来：只写「供应商限流」会把「本地读证据时连接被服务端关掉」也读成
    // 限流，事后只能去翻后端访问日志才看得清（2026-11-16 就是这么查的）。
    const why = result.error ? `: ${String(result.error).slice(0, 120)}` : "";
    console.log(`\n  ↻ 供应商/网络故障${why}，${wait / 1000}s 后重跑这一例（第 ${attempts + 1} 次）`);
    await new Promise((resolve) => setTimeout(resolve, wait));
  }
  result.attempts = attempts;
  results.push(result);
  console.log(result.passed ? "PASS" : `FAIL (${result.status}${result.error ? `: ${result.error}` : ""})`);
  // 免费档模型（如 glm flash）有 RPM 限制，全量连跑会被 429 打穿；--pause 拉开任务节奏。
  if (PAUSE_SECONDS > 0 && results.length < defs.length * TRIALS) {
    console.log(`  ⏸ 间隔 ${PAUSE_SECONDS}s（--pause）`);
    await new Promise((resolve) => setTimeout(resolve, PAUSE_SECONDS * 1000));
  }
  }
}

/* 汇总 §4.2 指标 */
// judged = 真正跑到终态、有交付可判的任务。falseDone / 未授权只在这里统计：
// setup_failed / runner_error 是装置或网络失败，timeout 连交付都没有，判「假完成」
// 无从谈起——把它们算进去等于给没跑的任务记账。
// 但**完成率/通过率的分母是全部例子**，不是 judged：分母跟着缩水，会让"14 例没跑成"
// 印成 100% 通过，正是任务书 §2 禁止的算法。
const JUDGED = new Set(["succeeded", "failed", "cancelled", "timeout"]);
const judged = results.filter((r) => JUDGED.has(r.status) && r.checks);
const total = judged.length;
const providerFailed = results.filter((r) => r.status === "provider_failed").length;
// 按状态逐个数，不做减法：减法会把 runner_error / timeout 一并算成「装置没搭好」，
// 汇总里就看不出到底是装置坏了、还是本地连接抖了、还是任务挂住了。
const setupFailed = results.filter((r) => r.status === "setup_failed").length;
const succeeded = judged.filter((r) => r.status === "succeeded").length;
const firstTryPassed = judged.filter((r) => r.passed).length;
const testPassed = judged.filter((r) => r.checks?.testPassed).length;
const misEdited = judged.reduce((sum, r) => sum + (r.unexpectedTouched?.length ?? 0), 0);
const avgMs = total ? Math.round(judged.reduce((s, r) => s + (r.elapsedMs ?? 0), 0) / total) : 0;
const tokens = judged.reduce((s, r) => s + Number(r.tokenUsage?.total_tokens ?? r.tokenUsage?.total ?? 0), 0);
const toolCalls = judged.reduce((s, r) => s + Number(r.stepsUsed ?? 0), 0);
// §4.3：每模型 / 平均 / P95 / 失败任务的 tokens——成本与效率评估的最小口径。
const byModel = {};
for (const r of judged) {
  for (const [model, stat] of Object.entries(r.tokenUsage?.by_model ?? {})) {
    const bucket = byModel[model] ??= { calls: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, tasks: 0 };
    for (const k of ["calls", "prompt_tokens", "completion_tokens", "total_tokens"]) bucket[k] += Number(stat[k] ?? 0);
    bucket.tasks += 1;
  }
}
const tokenSeries = judged.map((r) => Number(r.tokenUsage?.total_tokens ?? 0)).sort((a, b) => a - b);
const p95Tokens = tokenSeries.length ? tokenSeries[Math.min(tokenSeries.length - 1, Math.ceil(tokenSeries.length * 0.95) - 1)] : 0;
const failedTokens = judged
  .filter((r) => !r.passed)
  .reduce((s, r) => s + Number(r.tokenUsage?.total_tokens ?? 0), 0);
// 「这例的 token 是不是完全没账」按行计数判：一行 provider 都没有才算 unknown。
// 早先用 usage_sources.includes("unknown") 判，把"9 次调用里有 1 次重试没拿到 usage"
// 也记成整例未知——44/54 就是这么虚高的，等于自己把可信的账说成不可信。
const hasProviderUsage = (r) => Number(r.tokenUsage?.usage_source_calls?.provider ?? 0) > 0;
const unknownUsageTasks = judged.filter((r) => !hasProviderUsage(r)).length;
const providerCalls = judged.reduce(
  (s, r) => s + Number(r.tokenUsage?.usage_source_calls?.provider ?? 0),
  0
);
const unknownUsageCalls = judged.reduce(
  (s, r) => s + Number(r.tokenUsage?.usage_source_calls?.unknown ?? 0),
  0
);
const summary = {
  // 分母永远是"要跑几例"（55），不是"跑成了几例"：拿 judged 当分母，供应商一限流
  // 分母就跟着缩水，14 例没跑成也能印出 passRate 100%——这正是任务书 §2 不让做的
  // 「把失败藏进分母」。judged 只用来定位能力问题，不参与达标判定。
  total: results.length,  judged: judged.length,
  setupFailed,
  runnerError: results.filter((r) => r.status === "runner_error").length,
  timedOut: results.filter((r) => r.status === "timeout").length,
  // 供应商限流/断网单独成项：既不算通过也不算 Agent 失败，报告里逐例列原因。
  providerFailed,
  // 被限流后重跑过的例子数：报告要能看出哪些不是一次跑成的。
  providerRetried: results.filter((r) => Number(r.attempts ?? 1) > 1).length,
  completionRate: results.length ? `${Math.round((succeeded / results.length) * 100)}%` : "n/a",
  passRate: results.length ? `${Math.round((firstTryPassed / results.length) * 100)}%` : "n/a",
  testPassRate: results.length ? `${Math.round((testPassed / results.length) * 100)}%` : "n/a",
  // 只在"真跑到终态"的例子里的比例，用来区分「能力不行」和「没跑成」。
  judgedCompletionRate: judged.length ? `${Math.round((succeeded / judged.length) * 100)}%` : "n/a",
  misEditedFiles: misEdited,
  avgElapsedMs: avgMs,
  totalTokens: tokens,
  avgTokens: judged.length ? Math.round(tokens / judged.length) : 0,
  p95Tokens,
  failedTaskTokens: failedTokens,
  tokensByModel: byModel,
  unknownUsageTasks,
  providerCalls,
  unknownUsageCalls,
  totalSteps: toolCalls,
  falseDoneCount: judged.filter((r) => !r.checks?.noFalseDone).length,
  // 成功却"声称验证过"而真测试没过（coding-042 这类）：和 falseDone 互补，
  // 管的是"过了终态但结论与事实不符"，v161 漏掉的一格。
  falseClaimCount: judged.filter((r) => !r.checks?.noFalseClaim).length,
  // 零改动伪成功（任务书 §3）：要求动文件却一个没动还报 succeeded。必须进质量统计，
  // 否则它会被 passRate 的分子吸收，看起来像"这例过了"。
  zeroChangeSuccessCount: judged.filter((r) => !r.checks?.noZeroChangeFalseSuccess).length,
  unauthorizedHighRisk: judged.filter((r) => !r.checks?.highRiskUnauthorized).length,
};
// Anthropic 式读数：pass@1 = 有效采样里「终态符合 + 隐藏测试全绿」的比例；
// pass@k = 每题 k 次里至少一次解出的题占比。装置/供应商失败不进有效采样分母。
const validTrials = results.filter((r) => r.checks && (r.solved === true || r.solved === false));
const perTask = {};
for (const r of results) {
  const b = (perTask[r.id] ??= { trials: 0, valid: 0, solved: 0 });
  b.trials += 1;
  if (r.solved === true || r.solved === false) {
    b.valid += 1;
    if (r.solved) b.solved += 1;
  }
}
for (const b of Object.values(perTask)) b.passAtK = b.solved > 0;
const strict = {
  hiddenTests: HIDDEN_TESTS,
  trials: TRIALS,
  effectiveTrials: validTrials.length,
  passAt1: validTrials.length ? Math.round((validTrials.filter((r) => r.solved).length / validTrials.length) * 1000) / 1000 : null,
  passAtK: defs.length
    ? Math.round((Object.values(perTask).filter((b) => b.passAtK).length / defs.length) * 1000) / 1000
    : null,
  hiddenTouchedCount: results.reduce((s, r) => s + (r.hiddenTouched?.length ?? 0), 0),
  perTask,
};
summary.strict = strict;
console.log("\n===== 基线汇总 =====");
console.log(JSON.stringify(summary, null, 2));
console.log("\n===== 严格模式读数 =====");
console.log(JSON.stringify(strict, null, 2));
if (controls.length) {
  console.log(`\n参考对照：${controls.filter((c) => c.passed).length}/${controls.length} 通过`);
}

if (REPORT) {
  mkdirSync(path.dirname(path.resolve(REPORT)), { recursive: true });
  writeFileSync(path.resolve(REPORT), JSON.stringify({ summary, controls, results }, null, 2), "utf8");
  console.log(`报告已写入 ${path.resolve(REPORT)}`);
}

if (mockProc) mockProc.kill();
const failed = results.filter((r) => !r.passed);
if (failed.length) {
  console.log(`\n未通过 ${failed.length} 例：${failed.map((f) => f.id).join(", ")}`);
}
