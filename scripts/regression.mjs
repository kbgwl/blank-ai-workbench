#!/usr/bin/env node
// 版本升级回归门禁：一次跑完后端测试、迁移往返、前端类型检查与构建，可选跑评测样例。
// 用法： pnpm regression [--evals] [--only T01,T03] [--skip-web]
import { spawn } from "node:child_process";
import { writeFile, mkdir } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const apiDir = path.join(root, "services", "api");
const PG_URL = "postgresql+asyncpg://blank:blank_dev_password@localhost:5432/blank";

const argv = process.argv.slice(2);
const flags = {
  evals: argv.includes("--evals"),
  skipWeb: argv.includes("--skip-web"),
  only: argv.includes("--only") ? argv[argv.indexOf("--only") + 1] : null,
};

function run(cmd, args, cwd, env = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(cmd, args, { cwd, shell: true, env: { ...process.env, ...env } });
    let output = "";
    child.stdout.on("data", (chunk) => (output += chunk));
    child.stderr.on("data", (chunk) => (output += chunk));
    child.on("exit", (code) => resolve({ code, ms: Date.now() - started, output }));
  });
}

const steps = [];

async function step(name, cmd, args, cwd, env) {
  process.stdout.write(`→ ${name}\n`);
  const result = await run(cmd, args, cwd, env);
  steps.push({ name, code: result.code, ms: result.ms, tail: result.output.trim().split("\n").slice(-8).join("\n") });
  if (result.code !== 0) process.stdout.write(`${result.output}\n`);
  return result.code === 0;
}

// 迁移往返必须在 Postgres 上验证，SQLite 无法暴露方言差异；
// 但这份 URL 只能注入迁移步骤，否则后端测试会继承它并连不上 SQLite 夹具。
const pgEnv = { BLANK_DATABASE_URL: PG_URL };
const migrated = await step("alembic 迁移往返（PostgreSQL）", "uv", ["run", "alembic", "upgrade", "head"], apiDir, pgEnv);
if (migrated) {
  await step("alembic downgrade base", "uv", ["run", "alembic", "downgrade", "base"], apiDir, pgEnv);
  await step("alembic 重新 upgrade head", "uv", ["run", "alembic", "upgrade", "head"], apiDir, pgEnv);
}

await step("后端 pytest", "uv", ["run", "pytest", "-q"], apiDir, {
  BLANK_DATABASE_URL: "sqlite+aiosqlite:///:memory:",
});

if (!flags.skipWeb) {
  await step("前端类型检查", "pnpm", ["-C", "web", "exec", "tsc", "--noEmit"], root);
  await step("前端生产构建", "pnpm", ["-C", "web", "build"], root);
}

if (flags.evals) {
  const args = ["evals/run_evals.mjs", "--concurrency", "5"];
  if (flags.only) args.push("--only", flags.only);
  await step("评测样例集", "node", args, root);
}

const failed = steps.filter((s) => s.code !== 0);
const report = { at: new Date().toISOString(), passed: failed.length === 0, steps };
const outDir = path.join(root, "evals", "regression");
await mkdir(outDir, { recursive: true });
await writeFile(path.join(outDir, `regression-${Date.now()}.json`), JSON.stringify(report, null, 2), "utf8");

console.log(failed.length ? `回归失败：${failed.map((f) => f.name).join("、")}` : "回归全部通过");
process.exit(failed.length ? 1 : 0);
