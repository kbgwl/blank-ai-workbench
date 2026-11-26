import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const apiDir = path.join(root, "services", "api");

// 本机 E 盘绑定：C 盘空间紧张，dev 拉起的后端/前端子进程不再往 C 盘 TEMP 写。
// 其他平台或没有 E 盘时保持系统默认；BLANK_TEMP_ROOT 可覆盖落点。
if (process.platform === "win32" && existsSync("E:\\")) {
  const tempRoot = process.env.BLANK_TEMP_ROOT || "E:\\env\\blank-tmp";
  try {
    mkdirSync(tempRoot, { recursive: true });
    process.env.TEMP = tempRoot;
    process.env.TMP = tempRoot;
  } catch {
    /* 建不了就退回系统默认，不阻断 dev 启动 */
  }
  process.env.UV_CACHE_DIR ??= "E:\\env\\uv-cache";
}

const PG_URL = "postgresql+asyncpg://blank:blank_dev_password@localhost:5432/blank";
process.env.BLANK_DATABASE_URL ??= PG_URL;

const children = [];

function hasCommand(cmd, args = ["--version"]) {
  const probe = spawnSync(cmd, args, { shell: true, stdio: "ignore", env: process.env });
  return !probe.error && probe.status === 0;
}

const missing = [];
if (!hasCommand("docker", ["compose", "version"])) missing.push("Docker Compose（postgres + redis）");
if (!hasCommand("uv")) missing.push("uv（后端依赖与 alembic）");
if (!hasCommand("pnpm")) missing.push("pnpm（web dev）");
if (missing.length > 0) {
  console.error(`前置条件不满足，未启动任何进程：\n  - ${missing.join("\n  - ")}`);
  process.exit(2);
}

function run(cmd, args, cwd) {
  const child = spawn(cmd, args, { cwd, shell: true, stdio: "inherit", env: process.env });
  children.push(child);
  return new Promise((resolve, reject) => {
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`))));
    child.on("error", reject);
  });
}

function start(cmd, args, cwd) {
  const child = spawn(cmd, args, { cwd, shell: true, stdio: "inherit", env: process.env });
  children.push(child);
  return child;
}

function shutdown() {
  for (const c of children) c.kill();
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

console.log("→ docker compose up (postgres + redis)");
await run("docker", ["compose", "up", "-d", "--wait"], root);

console.log("→ alembic upgrade head");
await run("uv", ["run", "alembic", "upgrade", "head"], apiDir);

console.log("→ uvicorn :8000");
start("uv", ["run", "uvicorn", "blank_api.app:app", "--reload", "--port", "8000"], apiDir);

console.log("→ next dev :3000");
start("pnpm", ["--dir", "web", "dev"], root);
