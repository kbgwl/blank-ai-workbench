#!/usr/bin/env node
/**
 * 团队第二阶段端到端门禁：起一套隔离的 stub 模型 + 后端 + 前端，跑真实 HTTP 链路，
 * 用外层证据（群状态、工具卡片、审批挂起、system prompt 落盘、搜索结果、浏览器 DOM）判分。
 *
 * 为什么单独有这个脚本：单测能验证函数行为，但「高风险工具会挂起等审批、放行后成员真的变化、
 * 管家挑的资料真的进了提示」这类结论要跨进程才成立——上一轮那个「主理人加不到成员」的缺陷
 * 就是在 684 个全绿的单测里藏着的。
 *
 * 用法：
 *   node evals/team_e2e/run.mjs                     # 全套，含浏览器点验
 *   node evals/team_e2e/run.mjs --skip-web          # 只跑后端链路（无 Chrome 的机器）
 *   node evals/team_e2e/run.mjs --only-browser      # 沿用 .work 现场，只重点验 UI
 *   node evals/team_e2e/run.mjs --web-mode build    # 用生产构建 + next start 验证（默认 dev）
 *   node evals/team_e2e/run.mjs --keep              # 跑完不关服务，便于手动排查
 *   node evals/team_e2e/run.mjs --api-port 8155 --web-port 3105
 *
 * 隔离原则：默认端口 8097/8055/3005，数据库、记忆、日志、截图全落在 evals/team_e2e/.work/
 * （已 gitignore）。绝不允许指向开发库或用户常驻的 8000/3000。
 */
import { spawn, spawnSync } from "node:child_process";
import {
  closeSync as closeFd,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const API_DIR = path.join(ROOT, "services", "api");
const WEB_DIR = path.join(ROOT, "web");
const IS_WIN = process.platform === "win32";
// 相对 web/ 的独立 Next 输出目录：dev 锁与产物都在这里，不碰 .next
const WEB_DIST_DIR = ".team-e2e-next";

function parseArgs(argv) {
  const args = {
    stubPort: Number(process.env.TEAM_E2E_STUB_PORT ?? 8097),
    apiPort: Number(process.env.TEAM_E2E_API_PORT ?? 8055),
    webPort: Number(process.env.TEAM_E2E_WEB_PORT ?? 3005),
    webMode: process.env.TEAM_E2E_WEB_MODE ?? "dev",
    skipWeb: argv.includes("--skip-web"),
    onlyBrowser: argv.includes("--only-browser"),
    keep: argv.includes("--keep"),
    reuse: argv.includes("--reuse"),
  };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--stub-port") args.stubPort = Number(argv[++i]);
    else if (argv[i] === "--api-port") args.apiPort = Number(argv[++i]);
    else if (argv[i] === "--web-port") args.webPort = Number(argv[++i]);
    else if (argv[i] === "--web-mode") args.webMode = argv[++i];
    else if (argv[i] === "--help") {
      console.log(readFileSync(new URL(import.meta.url), "utf8").split("*/")[0]);
      process.exit(0);
    }
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
const WORK = path.resolve(process.env.TEAM_E2E_WORK ?? path.join(HERE, ".work"));
const PY =
  process.env.TEAM_E2E_PY ??
  path.join(API_DIR, ".venv", IS_WIN ? "Scripts" : "bin", IS_WIN ? "python.exe" : "python");
const NODE = process.env.TEAM_E2E_NODE ?? process.execPath;
const CHROME =
  process.env.TEAM_E2E_CHROME ??
  (IS_WIN ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "/usr/bin/google-chrome");
const API_BASE = `http://127.0.0.1:${args.apiPort}`;
const WEB_BASE = `http://127.0.0.1:${args.webPort}`;
const DB_URL = `sqlite+aiosqlite:///${String(path.join(WORK, "e2e.db")).replace(/\\/g, "/")}`;

const ENV = {
  ...process.env,
  TEAM_E2E_WORK: WORK,
  TEAM_E2E_STUB_PORT: String(args.stubPort),
  TEAM_E2E_API: API_BASE,
  BLANK_DATABASE_URL: DB_URL,
  TEAM_E2E_DB: path.join(WORK, "e2e.db"),
  BLANK_MEMORY_DIR: path.join(WORK, "memory"),
  BLANK_SECRET_KEY_FILE: path.join(WORK, "secret.key"),
  BLANK_SETTINGS_FILE: path.join(WORK, "settings.json"),
  PYTHONPATH: path.join(API_DIR, "src"),
  PYTHONIOENCODING: "utf-8",
  NO_PROXY: "127.0.0.1,localhost",
  no_proxy: "127.0.0.1,localhost",
};

const children = [];
const stages = [];

function busy(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    socket.once("connect", () => {
      socket.end();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
    socket.setTimeout(600, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

async function waitReady(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await fetch(url).catch(() => null);
    if (response && response.status < 500) return true;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
}

function start(name, command, cmdArgs, cwd, env) {
  const fd = openSync(path.join(WORK, `${name}.log`), "a");
  // 单独成组（Windows 上还要 unref，否则父进程一退，服务跟着没了 --keep 的意义）
  const child = spawn(command, cmdArgs, { cwd, env, stdio: ["ignore", fd, fd], windowsHide: true, detached: true });
  child.on("exit", (code) => console.log(`  [服务] ${name} 退出 code=${code}`));
  child.unref();
  children.push({ name, pid: child.pid, fd });
  return child;
}

function closeFds() {
  for (const child of children) {
    try {
      closeFd(child.fd);
    } catch {
      /* fd 可能已随进程一起回收 */
    }
  }
}

function stopAll() {
  for (const child of children.reverse()) {
    try {
      if (IS_WIN) spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
      else process.kill(-child.pid, "SIGTERM");
    } catch {
      /* 已经退出 */
    }
  }
  closeFds();
}

async function runStage(name, command, cmdArgs, cwd, env, timeoutMs) {
  process.stdout.write(`→ ${name}\n`);
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(command, cmdArgs, { cwd, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let output = "";
    child.stdout.on("data", (chunk) => (output += chunk));
    child.stderr.on("data", (chunk) => (output += chunk));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      output += "\n[超时]";
    }, timeoutMs);
    child.on("exit", (code) => {
      clearTimeout(timer);
      const counts = [...output.matchAll(/(\d+)\/(\d+) 通过/g)].map((m) => ({ passed: Number(m[1]), total: Number(m[2]) }));
      const tally = counts.reduce((acc, item) => ({ passed: acc.passed + item.passed, total: acc.total + item.total }), { passed: 0, total: 0 });
      const failedNames = output
        .split("\n")
        .filter((line) => line.startsWith("FAIL "))
        .map((line) => line.slice(5).split(" ::")[0]);
      const stage = {
        name,
        code,
        ms: Date.now() - started,
        passed: tally.passed,
        total: tally.total,
        failed: failedNames,
        tail: output.trim().split("\n").slice(-12).join("\n"),
      };
      stages.push(stage);
      if (code !== 0) process.stdout.write(`${output}\n`);
      else if (tally.total) process.stdout.write(`  ${tally.passed}/${tally.total} 项通过（${(stage.ms / 1000).toFixed(0)}s）\n`);
      resolve(stage);
    });
  });
}

async function findPlaywright() {
  const candidates = [
    process.env.TEAM_E2E_PLAYWRIGHT,
    path.join(ROOT, "node_modules", "playwright-core", "index.mjs"),
    path.join(WEB_DIR, "node_modules", "playwright-core", "index.mjs"),
    "E:/env/pwtest/node_modules/playwright-core/index.mjs",
  ].filter(Boolean);
  for (const candidate of candidates) if (existsSync(candidate)) return candidate;
  return null;
}

async function main() {
  if (args.onlyBrowser) args.reuse = true;
  if (!existsSync(PY)) throw new Error(`找不到后端 Python：${PY}（先建 venv 或设 TEAM_E2E_PY）`);
  const ports = [args.stubPort, args.apiPort, args.webPort];
  if (!args.reuse) {
    for (const port of ports) {
      if (await busy(port)) throw new Error(`端口 ${port} 已被占用：换端口或先停掉占用进程（本门禁不杀别人的进程）`);
    }
  }

  if (!args.reuse) {
    rmSync(WORK, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 });
    mkdirSync(WORK, { recursive: true });
    // 上一轮被强杀会留下 dev 锁文件，清掉才谈得上可重跑
    rmSync(path.join(WEB_DIR, WEB_DIST_DIR), { recursive: true, force: true, maxRetries: 3, retryDelay: 200 });
    // 场景开关：铺数据那一轮的后端回复要带唯一标记，搜索点验才有确定的目标
    writeFileSync(path.join(WORK, "scenario.json"), JSON.stringify({ seed_reply: true }), "utf8");
  } else mkdirSync(WORK, { recursive: true });

  console.log(`隔离环境：db=${DB_URL}  work=${WORK}`);
  const migrated = spawnSync(PY, ["-m", "alembic", "upgrade", "head"], {
    cwd: API_DIR,
    env: ENV,
    encoding: "utf8",
  });
  if (migrated.status !== 0) throw new Error(`迁移失败：\n${(migrated.stdout ?? "") + (migrated.stderr ?? "")}`.slice(-2000));

  start("stub", PY, [path.join(HERE, "stub_model.py")], WORK, ENV);
  // stub 只对 POST /v1/chat/completions 有意义，就绪与否按端口能不能连通判。
  for (let attempt = 0; attempt < 50 && !(await busy(args.stubPort)); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  if (!(await busy(args.stubPort))) throw new Error(`stub 模型服务没监听 ${args.stubPort}，见 stub.log`);
  start(
    "backend",
    PY,
    ["-m", "uvicorn", "blank_api.app:app", "--port", String(args.apiPort), "--log-level", "warning"],
    API_DIR,
    ENV,
  );
  if (!(await waitReady(`${API_BASE}/api/v1/health`, 60_000))) {
    throw new Error(`后端没起来，见 ${path.join(WORK, "backend.log")}`);
  }
  console.log(`后端就绪 ${API_BASE}，stub 就绪 ${args.stubPort}`);

  const web = await startWeb();
  if (web) console.log(`前端就绪 ${WEB_BASE}（${args.webMode}）`);

  try {
    // 阶段层层依赖：铺数据或灌历史失败，后面的门禁与 UI 点验只会堆出一串假失败，
    // 直接把这一轮判成不成立更诚实。
    let broken = false;
    if (args.onlyBrowser) {
      console.log("   只跑浏览器点验：沿用 .work 里的库与 seed.json");
    } else {
      const sequence = [
        ["铺数据（真实 HTTP + stub 模型）", "seed_stack.py", 300_000],
        ["灌历史消息并回填索引", "fill_history.py", 180_000],
        ["后端门禁 S0–S16", "gate.py", 900_000],
      ];
      for (const [label, script, timeout] of sequence) {
        const stage = await runStage(label, PY, [path.join(HERE, script)], API_DIR, ENV, timeout);
        if (stage.code !== 0) {
          broken = true;
          break;
        }
      }
    }
    if (broken) {
      stages.push({ name: "浏览器点验", code: null, skipped: true, reason: "前序后端阶段失败，现场不可信", tail: "" });
      console.log("   浏览器点验已跳过：前序后端阶段失败");
    } else if (!args.skipWeb && web) {
      const playwright = await findPlaywright();
      if (!playwright || !existsSync(CHROME)) {
        stages.push({
          name: "浏览器点验",
          code: null,
          skipped: true,
          reason: `缺 ${!playwright ? "playwright-core " : ""}${!existsSync(CHROME) ? CHROME : ""}`.trim(),
          tail: "",
        });
        console.log(`   浏览器点验已跳过：${stages.at(-1).reason}`);
      } else {
        await runStage(
          "浏览器点验 C1–C9（Chrome headless 1440×900）",
          NODE,
          [path.join(HERE, "browser_check.mjs")],
          ROOT,
          { ...ENV, TEAM_E2E_WEB: WEB_BASE, TEAM_E2E_CHROME: CHROME, TEAM_E2E_PLAYWRIGHT: playwright },
          600_000,
        );
      }
    } else if (args.skipWeb) {
      stages.push({ name: "浏览器点验", code: null, skipped: true, reason: "--skip-web", tail: "" });
      console.log("   浏览器点验已跳过：--skip-web");
    }
  } finally {
    if (args.keep) {
      console.log(`--keep：服务留着（stub:${args.stubPort} api:${args.apiPort} web:${args.webPort}），排查完手动停。`);
      closeFds();
    } else stopAll();
  }

  const skipped = stages.filter((stage) => stage.skipped);
  const failed = stages.filter((stage) => stage.code !== null && stage.code !== 0);
  const checks = stages.reduce((acc, stage) => acc + (stage.total ?? 0), 0);
  const passed = stages.reduce((acc, stage) => acc + (stage.passed ?? 0), 0);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outFile = path.join(ROOT, "evals", "results", `team-e2e-${stamp}.json`);
  mkdirSync(path.dirname(outFile), { recursive: true });
  writeFileSync(
    outFile,
    JSON.stringify({ at: stamp, passed: failed.length === 0, checks: { passed, total: checks }, skipped, stages, ports: { stub: args.stubPort, api: args.apiPort, web: args.webPort }, db: DB_URL }, null, 2),
    "utf8",
  );

  console.log(`\n判定项 ${passed}/${checks} 通过；阶段失败 ${failed.length ? failed.map((stage) => stage.name).join("、") : "无"}`);
  for (const stage of stages) {
    for (const name of stage.failed ?? []) console.log(`  ✗ ${stage.name}：${name}`);
  }
  if (skipped.length) console.log(`未执行：${skipped.map((stage) => `${stage.name}（${stage.reason}）`).join("、")}`);
  console.log(`结果写入 ${outFile}`);
  // 跳过的阶段要在退出码上留痕：门禁"少跑了一半"不能和"全绿"同一个信号。
  process.exit(failed.length ? 1 : skipped.length ? 3 : 0);
}

async function startWeb() {
  if (args.skipWeb) return false;
  const nextBin = path.join(WEB_DIR, "node_modules", "next", "dist", "bin", "next");
  if (!existsSync(nextBin)) throw new Error(`找不到 next：${nextBin}（先在 web 下装依赖）`);
  // 独立 distDir：Next 的 dev 锁按目录判定，用户正在跑的 `pnpm dev` 不会和我们抢，
  // 我们也不覆盖他的构建缓存。
  const env = {
    ...ENV,
    API_PROXY_TARGET: API_BASE,
    NEXT_TELEMETRY_DISABLED: "1",
    BLANK_WEB_DIST_DIR: WEB_DIST_DIR,
  };
  if (args.webMode === "build") {
    const built = spawnSync(NODE, [nextBin, "build"], { cwd: WEB_DIR, env, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
    if (built.status !== 0) throw new Error(`前端构建失败：${String(built.stdout ?? "").slice(-2000)}${String(built.stderr ?? "").slice(-800)}`);
    start("web", NODE, [nextBin, "start", "--port", String(args.webPort)], WEB_DIR, env);
  } else {
    // dev 模式下 next.config 的 rewrite 在请求时取值，能把 /api 代理到隔离后端，
    // 又不会覆盖用户现有的生产构建。
    start("web", NODE, [nextBin, "dev", "--port", String(args.webPort)], WEB_DIR, env);
  }
  if (!(await waitReady(`${WEB_BASE}/team`, 180_000))) {
    throw new Error(`前端没起来，见 ${path.join(WORK, "web.log")}`);
  }
  return true;
}

main().catch((error) => {
  console.error(String(error?.stack ?? error));
  stopAll();
  process.exit(1);
});
