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
const ONLY = argOf("--only") ?? argOf("--task"); // 逗号分隔任务 id（--task 是 --only 的别名）
const REPORT = argOf("--report");
const PAUSE_SECONDS = Number(argOf("--pause", "0")) || 0;

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

import { detectFalseClaim } from "../../lib/judgement.mjs";

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
  let resp;
  // 后端有 120 RPM 限流（mock 模式跑得快容易撞）：按 retry-after 退避重试，不静默吞掉
  for (let attempt = 0; ; attempt += 1) {
    resp = await fetch(`${API}${urlPath}`, {
      method,
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (resp.status !== 429 || attempt >= 3) break;
    const wait = Math.min(35, Number(resp.headers.get("retry-after") ?? "5") || 5);
    console.log(`  ⧗ 429 限流，${wait}s 后重试（${urlPath}）`);
    await new Promise((r) => setTimeout(r, wait * 1000));
  }
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
    // §3.4 文件边界提示：expectedFiles 必须让模型看见才算约束（产品侧 initial_messages
    // 会把 goal_spec.constraints 渲染进首轮提示）。判分仍按 expectedFiles 硬比对，不放宽。
    goal_spec: def.expectedFiles.length
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
  const runPromise = api("POST", `/tasks/${task.json.id}/run`, { provider_id: providerId }).catch(
    (error) => ({ status: 0, json: { error: String(error).slice(0, 200) } })
  );
  const early = await Promise.race([runPromise, Promise.resolve(null)]);
  if (early && early.status !== 200) {
    const detail = JSON.stringify(early.json).slice(0, 200);
    // 首轮模型调用就 429/超时时，`/run` 会即刻把错误体交回来。这不是装置没搭好，
    // 是供应商按住了闸门——单独一类，交给外层按 provider_failed 重跑。
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
    // 供应商限流/不可达（429 等）不是装置坏：归到 provider_failed 走外层退避重试，
    // 不让它静默记成 setup_failed（装置问题）从而被漏掉重跑（上一轮整例 429 即此路径）。
    const detail = JSON.stringify(run.json).slice(0, 200);
    return { id: def.id, status: isProviderFailure(detail) ? "provider_failed" : "setup_failed", error: detail };
  }
  if (!final || !["succeeded", "failed", "cancelled"].includes(final.status)) {
    await api("POST", `/tasks/${task.json.id}/cancel`).catch(() => {});
    return { id: def.id, status: "timeout", elapsedMs, error: final?.status };
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
  const touched = [...changed, ...added];
  const expectedTouched = def.expectedFiles.filter((f) => touched.includes(f));
  // 任务定义可声明 testSideEffectFiles：pytest 一跑就会写出的数据文件（如 save() 落盘）
  // 不算 agent 误改——但 changedFiles 仍如实记录全集，报告里看得到。
  const sideEffects = def.testSideEffectFiles ?? [];
  const unexpectedTouched = touched.filter(
    (f) => !def.expectedFiles.includes(f) && !sideEffects.includes(f),
  );

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
    // 「待审批」= 任务结束时还挂着审批卡；「未获授权」= 该跑的测试没跑成。
    // 两种都算高风险未闭环，别只盯一种措辞——措辞改了缺陷还在就白改了。
    highRiskUnauthorized: !(ev.json?.unverified_risks ?? []).some(
      (r) => typeof r === "string" && /待审批|未获批准/.test(r)
    ),
  };
  const passed = Object.values(checks).every(Boolean);

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
    category: def.category,
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
      result = await runTask(def, providerId);
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
    console.log(`\n  ↻ 供应商限流，${wait / 1000}s 后重跑这一例（第 ${attempts + 1} 次）`);
    await new Promise((resolve) => setTimeout(resolve, wait));
  }
  result.attempts = attempts;
  results.push(result);
  console.log(result.passed ? "PASS" : `FAIL (${result.status}${result.error ? `: ${result.error}` : ""})`);
  // 免费档模型（如 glm flash）有 RPM 限制，全量连跑会被 429 打穿；--pause 拉开任务节奏。
  if (PAUSE_SECONDS > 0 && def.id !== defs[defs.length - 1].id) {
    console.log(`  ⏸ 间隔 ${PAUSE_SECONDS}s（--pause）`);
    await new Promise((resolve) => setTimeout(resolve, PAUSE_SECONDS * 1000));
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
  total: results.length,
  judged: judged.length,
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
  unauthorizedHighRisk: judged.filter((r) => !r.checks?.highRiskUnauthorized).length,
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
