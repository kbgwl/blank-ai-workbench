#!/usr/bin/env node
import { existsSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const root = path.resolve(import.meta.dirname, "..");
const api = path.join(root, "services", "api");
const candidates = process.platform === "win32"
  ? [path.join(api, ".venv", "Scripts", "python.exe"), "uv", "python"]
  : [path.join(api, ".venv", "bin", "python"), "uv", "python3", "python"];

const args = process.argv.slice(2).filter((arg) => arg !== "--");
let command;
let commandArgs;
for (const candidate of candidates) {
  if (path.isAbsolute(candidate) && !existsSync(candidate)) continue;
  if (candidate === "uv") {
    command = candidate;
    commandArgs = ["run", "--project", api, "pytest", "-q", ...args];
  } else {
    command = candidate;
    commandArgs = ["-m", "pytest", "-q", ...args];
  }
  break;
}

if (!command) {
  console.error("找不到 Python/uv。请先安装 Python，或在 services/api 创建 .venv。");
  process.exit(1);
}

const result = spawnSync(command, commandArgs, { cwd: api, stdio: "inherit", shell: false });
if (result.error) {
  console.error(`无法启动 API 测试运行器：${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
