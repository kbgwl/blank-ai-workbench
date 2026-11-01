#!/usr/bin/env node
/**
 * 组装随包分发的 Python 运行时（`desktop/build/runtime`，约 129MB）。
 *
 * 这一步过去只有口头指令、没有脚本，于是「CI 里跑不了 verify-package」——因为自检脚本
 * 第一条断言就是「包里有没有 python.exe」，而造出那份运行时的手艺只存在于打包人的机器上。
 * 没有脚本 ⇒ 换台机器就复现不出交付物 ⇒ 打包自检只能靠手工，门禁也就无从谈起。
 *
 * 做法与实测口径（本机 2026-10-31 复现出的目录与既有那份一致）：
 *   1. `uv python install 3.12` 取一份可重定位的独立 CPython（cpython-3.12.14-windows-x86_64-none）；
 *   2. 整棵树拷进目标目录 —— 它的布局（`python.exe` / `DLLs` / `Lib/site-packages` / `Scripts`）
 *      正是 `verify-package.cjs` 断言的那一套；
 *   3. 依赖用 `uv export` 从 `services/api/uv.lock` 冻结导出，再 `uv pip install` 进这份解释器；
 *      导出里的 `-e .` 必须剔掉：产品要的是「源码在 api/src、由 .pth 指过去」，
 *      可编辑安装会把一个绝对路径写进 site-packages，装到用户机上必然指错（见 verify-package 第 2 段）；
 *   4. 手写 `Lib/site-packages/blank_api.pth` = `../../../api/src`（包内相对路径，见包内布局）；
 *   5. 删掉 `Lib/EXTERNALLY-MANAGED`：uv 的独立构建带这个标记，留着就装不进去。
 *
 * 纪律：不在目标目录里就地改写。先在同盘 scratch 里装好、验完，再改名换上去；
 * 旧目录改名留档而不是删掉（本机挂 safe-delete 钩子，删 129MB 目录会被拦，且不可逆）。
 *
 * 用法：
 *   node scripts/prepare_desktop_runtime.mjs                 # 组装到 desktop/build/runtime
 *   node scripts/prepare_desktop_runtime.mjs --out <dir>     # 换目标（CI 与本地点验用）
 *   node scripts/prepare_desktop_runtime.mjs --check         # 只体检既有运行时，不重建
 */
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const API_DIR = path.join(ROOT, "services", "api");
const DEFAULT_OUT = path.join(ROOT, "desktop", "build", "runtime");
const PYTHON_VERSION = process.env.BLANK_RUNTIME_PYTHON || "3.12";
const PTH_CONTENT = "../../../api/src";
// 装完必须能 import 的几包：覆盖「Web 框架 / ORM / 图引擎 / 迁移 / 加密」五条主干。
// 少任何一条，包里都会表现为「应用起不来但打包成功」。
const SMOKE_IMPORTS = ["fastapi", "uvicorn", "sqlalchemy", "alembic", "aiosqlite", "langgraph", "cryptography"];

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : fallback;
}
const OUT = path.resolve(arg("--out", DEFAULT_OUT));
const CHECK_ONLY = process.argv.includes("--check");

const say = (text) => console.log(text);
const fail = (text) => {
  console.error(`\n✗ ${text}`);
  process.exit(1);
};

// ── 体检（--check）：不重建，只按 verify-package 的口径看这份运行时能不能用 ──
function inspect(dir) {
  const py = path.join(dir, "python.exe");
  const sitePackages = path.join(dir, "Lib", "site-packages");
  const pth = path.join(sitePackages, "blank_api.pth");
  const problems = [];
  if (!existsSync(py)) problems.push("缺 python.exe");
  if (!existsSync(sitePackages)) problems.push("缺 Lib/site-packages");
  if (!existsSync(pth)) problems.push("缺 blank_api.pth");
  else {
    const content = readFileSync(pth, "utf8").trim();
    if (content !== PTH_CONTENT) problems.push(`blank_api.pth 内容不对：${JSON.stringify(content)}`);
  }
  // 空白副本：它会盖掉 api/src 里的新代码，verify-package 第 2 段同样断言这一条。
  if (existsSync(path.join(sitePackages, "blank_api"))) problems.push("site-packages 里有 blank_api 副本");
  const dists = existsSync(sitePackages)
    ? readdirSync(sitePackages).filter((name) => name.endsWith(".dist-info")).length
    : 0;
  const bytes = existsSync(dir) ? directoryBytes(dir) : 0;
  return { py, sitePackages, dists, bytes, problems };
}

function directoryBytes(dir) {
  let total = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    try {
      total += entry.isDirectory() ? directoryBytes(full) : statSync(full).size;
    } catch {}
  }
  return total;
}

const mb = (bytes) => `${(bytes / 1024 / 1024).toFixed(1)}MB`;

if (CHECK_ONLY) {
  if (!existsSync(OUT)) fail(`没有运行时：${OUT}`);
  const info = inspect(OUT);
  say(`检查 ${OUT}`);
  say(`  包数 ${info.dists}  体积 ${mb(info.bytes)}`);
  if (info.problems.length) fail(`未通过：${info.problems.join(" / ")}`);
  say("  运行时布局与 .pth 都符合包内口径");
  process.exit(0);
}

// ── 找 uv：CI 用 setup-uv 装到 PATH，本机在 ~/.local/bin ──
function findUv() {
  const explicit = process.env.UV_BIN || process.env.BLANK_UV_BIN;
  const candidates = [
    explicit,
    "uv",
    path.join(os.homedir(), ".local", "bin", process.platform === "win32" ? "uv.exe" : "uv"),
    path.join(os.homedir(), ".cargo", "bin", process.platform === "win32" ? "uv.exe" : "uv"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["--version"], { encoding: "utf8" });
    // "uv" 走 PATH 时 spawnSync 会 ENOENT；显式路径时 status 才是判据。
    if (!probe.error && probe.status === 0) return candidate;
  }
  return null;
}

const UV = findUv();
if (!UV) fail("找不到 uv（装一个，或用 UV_BIN 指过去）");

// scratch 与目标同盘：本机纪律是「产物不落 C 盘」，而同盘改名才是原子的。
const drive = /^[A-Za-z]:/.test(OUT) ? OUT.slice(0, 2) : null;
const SCRATCH_BASE = drive ? path.join(drive, "tmp") : os.tmpdir();
const STAMP = `${Date.now()}`;
const SCRATCH = path.join(SCRATCH_BASE, `blank-runtime-${STAMP}`);
const UV_PY_DIR = path.join(SCRATCH, "uv-python");
const UV_CACHE = path.join(SCRATCH_BASE, "uv-cache");
mkdirSync(SCRATCH, { recursive: true });

const uvEnv = { ...process.env, UV_PYTHON_INSTALL_DIR: UV_PY_DIR, UV_CACHE_DIR: UV_CACHE };
function uv(args, opts = {}) {
  const result = spawnSync(UV, args, { encoding: "utf8", env: uvEnv, cwd: opts.cwd ?? ROOT, stdio: ["ignore", "pipe", "pipe"] });
  if (result.status !== 0) {
    fail(`uv ${args.join(" ")} 失败（rc=${result.status}）\n${(result.stderr || result.stdout || "").trim().slice(0, 2000)}`);
  }
  return result.stdout ?? "";
}

say(`[1/5] 取独立 CPython ${PYTHON_VERSION}（装到 ${UV_PY_DIR}）`);
uv(["python", "install", PYTHON_VERSION]);
const base = readdirSync(UV_PY_DIR).filter((name) => /^cpython-/.test(name) && existsSync(path.join(UV_PY_DIR, name, "python.exe")));
if (!base.length) fail(`uv python install 之后在 ${UV_PY_DIR} 找不到 cpython-*`);
const sourceRoot = path.join(UV_PY_DIR, base.sort().at(-1));
say(`      ${path.basename(sourceRoot)}`);

say(`[2/5] 整棵树拷进 scratch`);
const staged = path.join(SCRATCH, "runtime");
// 不调用外部 cp：fs.cpSync 递归拷整棵树就够，也不依赖平台命令。
cpSync(sourceRoot, staged, { recursive: true });
// uv 的独立构建带这个标记，留着 `uv pip install` 会直接拒绝。
// 有的构建把它标成只读（Windows 上 `rm` 会 EPERM），那种情况改成"挪出运行时目录"——
// 目的只是让它不在原位，不必真的删掉。
const externallyManaged = path.join(staged, "Lib", "EXTERNALLY-MANAGED");
if (existsSync(externallyManaged)) {
  try {
    unlinkSync(externallyManaged);
  } catch {
    renameSync(externallyManaged, path.join(SCRATCH, "EXTERNALLY-MANAGED.bak"));
  }
}

say(`[3/5] 从 uv.lock 冻结导出依赖并装进这份解释器`);
const exported = uv(["export", "--frozen", "--no-hashes", "--no-dev"], { cwd: API_DIR });
if (!exported.includes("fastapi")) fail("uv export 的输出里没有 fastapi —— 导出的不是后端依赖");
const requirements = exported
  .split("\n")
  // `-e .` 必须剔掉：可编辑安装会把仓库绝对路径写进 site-packages，装到用户机上必然指错。
  .filter((line) => !/^\s*(-e|--editable)\s/.test(line))
  .join("\n");
const reqFile = path.join(SCRATCH, "requirements.txt");
writeFileSync(reqFile, requirements, "utf8");
uv(["pip", "install", "--python", path.join(staged, "python.exe"), "-r", reqFile], { cwd: API_DIR });

say(`[4/5] 写 blank_api.pth 并点验导入`);
const sitePackages = path.join(staged, "Lib", "site-packages");
writeFileSync(path.join(sitePackages, "blank_api.pth"), `${PTH_CONTENT}\n`, "utf8");
const smoke = spawnSync(path.join(staged, "python.exe"), ["-X", "utf8", "-c", SMOKE_IMPORTS.map((name) => `import ${name}`).join("\n")], {
  encoding: "utf8",
  cwd: staged,
});
if (smoke.status !== 0) {
  fail(`装完连 ${SMOKE_IMPORTS.join("/")} 都 import 不了：\n${(smoke.stderr || "").split("\n").slice(-12).join("\n")}`);
}

say(`[5/5] 换上去（旧目录改名留档，不删）`);
mkdirSync(path.dirname(OUT), { recursive: true });
if (existsSync(OUT)) {
  const backup = `${OUT}-old-${STAMP}`;
  renameSync(OUT, backup);
  say(`      旧运行时改名到 ${backup}`);
}
renameSync(staged, OUT);

const info = inspect(OUT);
if (info.problems.length) fail(`组装结果不合口径：${info.problems.join(" / ")}`);
say(`\n✓ ${OUT}`);
say(`  包数 ${info.dists}  体积 ${mb(info.bytes)}  .pth → ${PTH_CONTENT}`);
