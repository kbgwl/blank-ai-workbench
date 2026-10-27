#!/usr/bin/env node
// CI job 的本地等价装置（任务 #104）：本机跑不了 GitHub runner、也装不了 docker，
// 这里用同一套判据在本地复现两条 job 的核心判定：
//   api-dialect —— 对应 .github/workflows/ci.yml 的 api-dialect job（三方言真 HTTP 对照 + 两条建库路径对账）
//   eval-gate   —— 对应 ci.yml 新增的 eval-gate job（脚本化桩跑评测全集 + evals/eval_gate.mjs 门禁）
// 判据纪律（与 evals/dialect_e2e.py 同）：读数只认装置自己打出的那行 `退出码=`，包装命令的 rc 不算证据；
//   0=每格全绿；1=有格红；2=这一格今天没法判——「跳过」和「中止」分开，「今天没法判」绝不允许伪装成「通过」。
// 本机形态（实测 2026-10-26）：无 docker、127.0.0.1:5432 无监听 ⇒ PostgreSQL 格今天没法判 ⇒
//   api-dialect 的本地判定一律 [SKIP] rc=2（CI 用 --require-postgres 把这件事变成失败，本地把它如实变成 2）。
//   MySQL 格：本机 127.0.0.1:3306 有实例；root 口令只经环境变量 KBGAI_MYSQL_ROOT_PASSWORD 注入
//   （或整条 BLANK_E2E_MYSQL_URL），绝不写进仓库任何文件、绝不打进任何日志——装置会逐条脱敏。
// 用法：
//   node scripts/local_ci.mjs --job api-dialect|eval-gate|all [--with-eval-run] [--results FILE] [--self-test]
// 硬约束：不许碰 :8000/:3000（端口被显式设成这两个之一时本装置直接拒绝）；不启动桌面壳。
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, existsSync, openSync, writeFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const IS_WIN = process.platform === "win32";
const NODE = process.execPath;
const PY = path.join(ROOT, "services", "api", ".venv", "Scripts", IS_WIN ? "python.exe" : "python");
const STAMP = new Date().toISOString().replace(/[:.]/g, "-");
// scratch 与仓库同盘（本机纪律：一切产物落 E 盘，不写 C 盘）；非 Windows 退回系统临时目录。
const SAME_DRIVE_TMP = /^[A-Za-z]:/.test(ROOT) ? `${ROOT.slice(0, 2)}/tmp` : os.tmpdir();
const SCRATCH = process.env.BLANK_LOCAL_CI_SCRATCH || path.join(SAME_DRIVE_TMP, `kbgai-local-ci-${STAMP}`);

const PASS = "pass";
const RED = "red";
const SKIP = "skip"; // 这一格今天没法判

function parseArgs(argv) {
  const args = { job: "all", withEvalRun: false, results: null, selfTest: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--job") args.job = argv[++i];
    else if (a === "--with-eval-run") args.withEvalRun = true;
    else if (a === "--results") args.results = argv[++i];
    else if (a === "--self-test") args.selfTest = true;
    else { console.log(`[ABORT] 不认识的参数：${a}`); process.exit(2); }
  }
  if (!["api-dialect", "eval-gate", "all"].includes(args.job)) {
    console.log(`[ABORT] 不认识的 job：${args.job}`);
    process.exit(2);
  }
  return args;
}

// —— 凭据纪律：口令只在内存里存在；一切要落盘/上屏的文本先过一遍脱敏 ——
const SECRETS = new Set();
function rememberSecret(value) {
  if (value && value.length >= 4) SECRETS.add(value);
}
function sanitize(text) {
  let out = String(text ?? "");
  for (const secret of SECRETS) out = out.split(secret).join("〈凭据：仅经环境变量注入，不落日志〉");
  return out;
}

function probeTcp(host, port, timeout = 1500) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port });
    const done = (ok) => { sock.destroy(); resolve(ok); };
    sock.setTimeout(timeout);
    sock.once("connect", () => done(true));
    sock.once("timeout", () => done(false));
    sock.once("error", () => done(false));
  });
}

function run(cmd, cmdArgs, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, cmdArgs, { cwd: opts.cwd ?? ROOT, env: { ...process.env, ...opts.env }, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (out += d));
    child.on("close", (code) => resolve({ rc: code, out: sanitize(out) }));
    child.on("error", (error) => resolve({ rc: null, out: sanitize(`${out}\n[spawn 失败] ${error.message}`) }));
  });
}

function startChild(cmd, cmdArgs, opts = {}) {
  const logPath = path.join(SCRATCH, `${opts.log}.log`);
  mkdirSync(SCRATCH, { recursive: true });
  const fd = openSync(logPath, "w");
  const child = spawn(cmd, cmdArgs, { cwd: opts.cwd ?? ROOT, env: { ...process.env, ...opts.env }, stdio: ["ignore", fd, fd] });
  child.__log = logPath;
  return child;
}

/** 装置唯一的读数：它自己那行 `退出码=`。读不出这一行 = 今天没法判（2），不是红也不是绿。 */
function deviceVerdict(text) {
  const m = /退出码=(\d)/.exec(String(text ?? ""));
  return m ? Number(m[1]) : null;
}

/** 组合判定：红(1) 优先于 没法判(2) 优先于 全绿(0)——但只要有任何一格缺席，0 就被剥夺。 */
function combine(steps) {
  if (steps.some((s) => s.state === RED)) return 1;
  if (steps.some((s) => s.state === SKIP || s.rc === null)) return 2;
  return 0;
}

function printJob(name, steps, jobRc, extraLines = []) {
  console.log(`\n===== 本地等价装置 · job: ${name} =====`);
  for (const line of extraLines) console.log(line);
  for (const s of steps) {
    const tag = s.state === PASS ? "PASS" : s.state === RED ? "RED " : "SKIP";
    console.log(`[${tag}] ${s.step}：${s.detail}`);
  }
  console.log(`job 判定 rc=${jobRc}（0=每一格全绿；1=有格红；2=这一格今天没法判）`);
}

// ---------------------------------------------------------------------------
// job: api-dialect —— 复现 ci.yml 的 api-dialect job
// CI 三格 sqlite,mysql,postgres + --require-mysql --require-postgres；本地无 docker，
// PostgreSQL 格先天没法判 → 本 job 的本地判定在 CI 语义下应落 2（中止≠跳过≠通过）。
// 只有当对照判据至少能在本机成立（sqlite+mysql 两格都可跑）时才真启动 evals/dialect_e2e.py——
// 单跑 sqlite 一格给不出「换库没问题」的任何证据，还会占住 :8071/:8072 和操作者串行跑的通道打架。
// 本装置对 evals/dialect_e2e.py 只读不改。
// ---------------------------------------------------------------------------
async function jobApiDialect() {
  const steps = [];
  const lines = [];

  // —— MySQL 格可用性：URL 或 root 口令只从环境变量来 ——
  let mysqlUrl = (process.env.BLANK_E2E_MYSQL_URL ?? "").trim();
  const mysqlPw = (process.env.KBGAI_MYSQL_ROOT_PASSWORD ?? "").trim();
  if (mysqlPw) rememberSecret(mysqlPw);
  let mysqlReason = "";
  if (!mysqlUrl && mysqlPw) {
    const alive = await probeTcp("127.0.0.1", 3306);
    if (alive) mysqlUrl = `mysql+aiomysql://root:${mysqlPw}@127.0.0.1:3306/kbgai_e2e`;
    else mysqlReason = "127.0.0.1:3306 无监听（且未给 BLANK_E2E_MYSQL_URL）";
  } else if (!mysqlUrl) {
    mysqlReason = mysqlPw ? "有口令但未探到 127.0.0.1:3306" : "KBGAI_MYSQL_ROOT_PASSWORD 未注入、BLANK_E2E_MYSQL_URL 未给（口令只走环境变量，装置不代填、不落盘）";
  }

  // —— PostgreSQL 格可用性：本机没有 docker（实测），CI 的 postgres:17 service 不可复现 ——
  let pgUrl = (process.env.BLANK_E2E_POSTGRES_URL ?? "").trim();
  let pgReason = "";
  if (!pgUrl) {
    const alive = await probeTcp("127.0.0.1", 5432);
    pgReason = alive
      ? "127.0.0.1:5432 有监听但未给 BLANK_E2E_POSTGRES_URL——连不上即没法判，装置不猜 URL"
      : "本机无 docker、127.0.0.1:5432 无监听、BLANK_E2E_POSTGRES_URL 未给";
  }

  const canRunDevice = mysqlUrl !== ""; // sqlite 格恒可跑；对照判据最少要 mysql 在场
  if (canRunDevice) {
    const only = pgUrl ? "sqlite,mysql,postgres" : "sqlite,mysql";
    const requireArgs = ["--require-mysql", ...(pgUrl ? ["--require-postgres"] : [])];
    lines.push(`启动 evals/dialect_e2e.py --only ${only} ${requireArgs.join(" ")}（真 HTTP 端到端，串行单进程；URL 仅经环境变量传入）`);
    const result = await run(PY, ["-X", "utf8", path.join("evals", "dialect_e2e.py"), "--only", only, ...requireArgs], {
      env: { PYTHONIOENCODING: "utf-8", ...(mysqlUrl ? { BLANK_E2E_MYSQL_URL: mysqlUrl } : {}), ...(pgUrl ? { BLANK_E2E_POSTGRES_URL: pgUrl } : {}) },
    });
    const verdict = deviceVerdict(result.out);
    mkdirSync(SCRATCH, { recursive: true });
    writeFileSync(path.join(SCRATCH, `dialect-${STAMP}.log`), result.out, "utf8");
    if (verdict === null)
      steps.push({ step: "三方言端到端对照（dialect_e2e）", state: SKIP, detail: `装置没打出「退出码=」行——这一格今天没法判（包装 rc=${result.rc} 不算证据）；脱敏轨迹：${path.join(SCRATCH, `dialect-${STAMP}.log`)}` });
    else if (verdict === 2)
      steps.push({ step: "三方言端到端对照（dialect_e2e）", state: SKIP, detail: `装置自报 退出码=2——这一格今天没法判（脱敏轨迹：${path.join(SCRATCH, `dialect-${STAMP}.log`)}）` });
    else
      steps.push({ step: "三方言端到端对照（dialect_e2e）", state: verdict === 0 ? PASS : RED, detail: `装置自报 退出码=${verdict}（0=每一格全绿；1=有格红）；脱敏轨迹：${path.join(SCRATCH, `dialect-${STAMP}.log`)}` });
  } else {
    steps.push({ step: "三方言端到端对照（dialect_e2e）", state: SKIP, detail: `[SKIP] MySQL 这一格今天没法判：${mysqlReason}；单跑对照格没有可比性，且 :8071/:8072 要留给操作者串行跑装置` });
  }
  if (!pgUrl) steps.push({ step: "PostgreSQL 格（CI --require-postgres）", state: SKIP, detail: `[SKIP] ${pgReason}——CI 里这一格连不上即失败，本地如实落「今天没法判」，不并入 0` });

  // —— 两条建库路径对账（CI 的 schema-agreement 步骤）——
  if (mysqlUrl) {
    const testUrl = mysqlUrl.replace("/kbgai_e2e", "/mysql");
    const r = await run(PY, ["-X", "utf8", "-m", "pytest", "-q", "tests/test_schema_agreement_mysql_v40.py"], {
      cwd: path.join(ROOT, "services", "api"), env: { PYTHONIOENCODING: "utf-8", BLANK_TEST_MYSQL_URL: testUrl },
    });
    steps.push({ step: "MySQL 两条建库路径对账", state: r.rc === 0 ? PASS : RED, detail: `pytest rc=${r.rc}；尾部：${r.out.split(/\r?\n/).filter(Boolean).slice(-2).join(" / ")}` });
  } else {
    steps.push({ step: "MySQL 两条建库路径对账", state: SKIP, detail: `[SKIP] 同 MySQL 格缺席原因：${mysqlReason}` });
  }
  if (pgUrl) {
    const r = await run(PY, ["-X", "utf8", "-m", "pytest", "-q", "tests/test_schema_agreement_pg_v33.py"], {
      cwd: path.join(ROOT, "services", "api"), env: { PYTHONIOENCODING: "utf-8", BLANK_TEST_POSTGRES_URL: pgUrl.replace("/kbgai_e2e", "/postgres") },
    });
    steps.push({ step: "PG 两条建库路径对账", state: r.rc === 0 ? PASS : RED, detail: `pytest rc=${r.rc}；尾部：${r.out.split(/\r?\n/).filter(Boolean).slice(-2).join(" / ")}` });
  } else {
    steps.push({ step: "PG 两条建库路径对账", state: SKIP, detail: "[SKIP] 无 PostgreSQL：本机没 docker、没 5432" });
  }

  return { name: "api-dialect", steps, lines, rc: combine(steps) };
}

// ---------------------------------------------------------------------------
// job: eval-gate —— 复现 ci.yml 的 eval-gate job
// --with-eval-run：脚本化桩（不烧真模型）+ 独立 sqlite 实例（默认 :8050/:8099，绝不碰 :8000/:3000）
//   → alembic upgrade head → uvicorn → register_mock_provider → run_evals 全集 → eval_gate 读数。
// 不带 --with-eval-run：直接对既有最新一份结果出门禁判定（门禁是纯读数装置，不重新烧评测）。
// ---------------------------------------------------------------------------
async function jobEvalGate(opts) {
  const steps = [];
  const lines = [];
  const children = [];
  const killAll = () => { for (const c of children) { try { c.kill(); } catch { /* 已退出 */ } } };
  try {
    if (opts.withEvalRun) {
      const apiPort = Number(process.env.BLANK_LOCAL_CI_API_PORT ?? 8050);
      const mockPort = Number(process.env.BLANK_LOCAL_CI_MOCK_PORT ?? 8099);
      if (apiPort === 8000 || mockPort === 8000 || apiPort === 3000 || mockPort === 3000) {
        steps.push({ step: "端口纪律", state: SKIP, detail: `[ABORT] :8000/:3000 是用户的，本装置拒绝` });
        return { name: "eval-gate", steps, lines, rc: 2 };
      }
      const busy = [];
      if (await probeTcp("127.0.0.1", apiPort, 500)) busy.push(`127.0.0.1:${apiPort}`);
      if (await probeTcp("127.0.0.1", mockPort, 500)) busy.push(`127.0.0.1:${mockPort}`);
      if (busy.length) {
        steps.push({ step: "评测流水线（--with-eval-run）", state: SKIP, detail: `[SKIP] 端口已被占用：${busy.join(", ")}（可能是别的助手常驻；本装置不抢、不改端口硬跑）` });
        return { name: "eval-gate", steps, lines, rc: 2 };
      }
      mkdirSync(SCRATCH, { recursive: true });
      lines.push(`流水线 scratch：${SCRATCH}；API=127.0.0.1:${apiPort}，桩=127.0.0.1:${mockPort}（脚本化桩，不烧真模型、不调真实供应商）`);

      const mig = await run(PY, ["-X", "utf8", "-m", "alembic", "upgrade", "head"], {
        cwd: path.join(ROOT, "services", "api"),
        env: { PYTHONIOENCODING: "utf-8", BLANK_DATABASE_URL: `sqlite+aiosqlite:///${path.join(SCRATCH, "gate.db").replace(/\\/g, "/")}` },
      });
      steps.push({ step: "隔离库建库（alembic upgrade head）", state: mig.rc === 0 ? PASS : RED, detail: `rc=${mig.rc}` });
      if (mig.rc !== 0) return { name: "eval-gate", steps, lines, rc: 1 };

      const mock = startChild(NODE, [path.join("scripts", "mock-model-server.mjs")], { log: "mock", env: { MOCK_MODEL_PORT: String(mockPort) } });
      children.push(mock);
      const api = startChild(PY, ["-X", "utf8", "-m", "uvicorn", "blank_api.app:app", "--host", "127.0.0.1", "--port", String(apiPort)], {
        log: "api", cwd: path.join(ROOT, "services", "api"),
        env: {
          PYTHONIOENCODING: "utf-8",
          BLANK_DATABASE_URL: `sqlite+aiosqlite:///${path.join(SCRATCH, "gate.db").replace(/\\/g, "/")}`,
          BLANK_SETTINGS_FILE: path.join(SCRATCH, "settings.json"),
          BLANK_SECRET_KEY_FILE: path.join(SCRATCH, "secret.key"),
          BLANK_SANDBOX_ENABLED: "false", // 本机无 docker：桌面壳/装置同款姿态，否则一切任务在装配层 409
        },
      });
      children.push(api);
      let healthy = false;
      const deadline = Date.now() + 90_000;
      while (Date.now() < deadline && !healthy) {
        await new Promise((r) => setTimeout(r, 1500));
        try {
          const h = await fetch(`http://127.0.0.1:${apiPort}/api/v1/health`, { signal: AbortSignal.timeout(3000) });
          healthy = h.ok && (await h.json()).database === "connected";
        } catch { /* 还没起来 */ }
      }
      steps.push({ step: "起桩 + 起 API + 健康检查", state: healthy ? PASS : RED, detail: healthy ? `/health database=connected` : `90s 内 :${apiPort} 没说 database=connected（日志：${api.__log}）` });
      if (!healthy) return { name: "eval-gate", steps, lines, rc: 1 };

      const apiBase = `http://127.0.0.1:${apiPort}`;
      const reg = await run(NODE, [path.join("scripts", "register_mock_provider.mjs"), `http://127.0.0.1:${mockPort}/v1`], { env: { BLANK_API_BASE: `${apiBase}/api/v1` } });
      let providerId = null;
      try { providerId = JSON.parse(reg.out.trim().split(/\r?\n/).pop()).id; } catch { /* 红在下一条 */ }
      steps.push({ step: "注册脚本化桩供应商", state: providerId ? PASS : RED, detail: providerId ? `id=${providerId}` : `读数失败：${reg.out.slice(0, 200)}` });
      if (!providerId) return { name: "eval-gate", steps, lines, rc: 1 };

      const evals = await run(NODE, [path.join("evals", "run_evals.mjs"), "--provider", providerId, "--concurrency", "3"], { env: { BLANK_API_BASE: apiBase } });
      const resultsLine = (evals.out.match(/结果写入 (.+)/) ?? [])[1]?.trim();
      steps.push({ step: "评测全集 32 例（脚本化桩）", state: resultsLine ? PASS : RED, detail: resultsLine ? `写出：${resultsLine}；runner rc=${evals.rc}` : `没等到「结果写入」行（runner rc=${evals.rc}）——上一轮没跑完就是这个形状` });
      if (!resultsLine) return { name: "eval-gate", steps, lines, rc: 2 };
      if (opts.results === undefined && existsSync(resultsLine)) opts.results = resultsLine;
    }

    const gateArgs = [path.join("evals", "eval_gate.mjs")];
    if (opts.results) gateArgs.push("--results", opts.results);
    const gate = await run(NODE, gateArgs);
    process.stdout.write(gate.out.endsWith("\n") ? gate.out : `${gate.out}\n`);
    const verdict = deviceVerdict(gate.out);
    if (verdict === null) steps.push({ step: "成功率门禁（eval_gate）", state: SKIP, detail: `门禁没打出「退出码=」行——今天没法判（包装 rc=${gate.rc} 不算证据）` });
    else steps.push({ step: "成功率门禁（eval_gate）", state: verdict === 0 ? PASS : verdict === 1 ? RED : SKIP, detail: `门禁自报 退出码=${verdict}（包装 rc=${gate.rc}${String(gate.rc) === String(verdict) ? "，一致" : "，不一致——以门禁自报行为准"}）` });
    return { name: "eval-gate", steps, lines, rc: combine(steps) };
  } finally {
    killAll();
  }
}

// ---------------------------------------------------------------------------
// --self-test：判定组合器自己先能失败——喂五份合成装置读数，期望必须逐条精确命中。
// 什么缺陷让它红：combine/parse 被改坏（比如把「没法判」并进 0、或解析不到行时当绿），
// 这里对应格立刻红，不需要真库真模型。
// ---------------------------------------------------------------------------
function selfTest() {
  const cases = [
    { name: "装置全绿但 PG 格缺席 → 2", steps: [{ step: "device", state: PASS, rc: 0 }, { step: "pg", state: SKIP }], want: 2 },
    { name: "装置自报 1 → 1（红优先于缺席）", steps: [{ step: "device", state: RED, rc: 1 }, { step: "pg", state: SKIP }], want: 1 },
    { name: "装置自报 2 → 2", steps: [{ step: "device", state: SKIP }], want: 2 },
    { name: "全格在场且全绿 → 0（本机今天到不了这一格，留作 CI 等价态）", steps: [{ step: "device", state: PASS }, { step: "pg", state: PASS }], want: 0 },
    { name: "读数行缺失（deviceVerdict=null）→ 2，不是 0", steps: [{ step: "device", state: SKIP, rc: null }], want: 2 },
  ];
  console.log("===== 本地等价装置 · 自检（判定组合器） =====");
  let bad = 0;
  for (const c of cases) {
    const got = combine(c.steps);
    const parseProbe = c.steps.some((s) => s.rc === null);
    const ok = got === c.want;
    if (!ok) bad += 1;
    console.log(`[${ok ? "PASS" : "RED "}] ${c.name}：组合器给出 ${got}`);
    void parseProbe;
  }
  const parseCases = [
    { text: "退出码=0（0=每一格全绿", want: 0 },
    { text: "退出码=2", want: 2 },
    { text: "没有那一行", want: null },
  ];
  for (const c of parseCases) {
    const got = deviceVerdict(c.text);
    const ok = got === c.want;
    if (!ok) bad += 1;
    console.log(`[${ok ? "PASS" : "RED "}] 解析「${c.text.slice(0, 12)}…」→ ${String(got)}（期望 ${String(c.want)}）`);
  }
  console.log(`自检判定 rc=${bad ? 1 : 0}（红=装置自己不可信，别读它的任何结论）`);
  return bad ? 1 : 0;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.selfTest) process.exit(selfTest());
  const jobs = [];
  if (opts.job === "api-dialect" || opts.job === "all") jobs.push(await jobApiDialect());
  if (opts.job === "eval-gate" || opts.job === "all") jobs.push(await jobEvalGate(opts));
  let overall = 0;
  for (const job of jobs) {
    printJob(job.name, job.steps, job.rc, job.lines);
    if (job.rc === 1) overall = 1;
    else if (job.rc === 2 && overall !== 1) overall = 2;
  }
  console.log(`\n===== 本地等价装置总结 =====`);
  console.log(`覆盖 job：${jobs.map((j) => `${j.name}=${j.rc}`).join("，")}`);
  console.log(`总判定 rc=${overall}（0=每一格全绿；1=有格红；2=这一格今天没法判——本机无 docker 时 api-dialect 的 PostgreSQL 格注定落 2，这条不许被读成「通过」）`);
  process.exit(overall);
}

main().catch((error) => {
  console.error(sanitize(error.stack ?? String(error)));
  process.exit(2);
});
