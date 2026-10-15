import { spawn } from "node:child_process";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const apiDir = path.join(root, "services", "api");

const PG_URL = "postgresql+asyncpg://blank:blank_dev_password@localhost:5432/blank";
process.env.BLANK_DATABASE_URL ??= PG_URL;

const children = [];

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
