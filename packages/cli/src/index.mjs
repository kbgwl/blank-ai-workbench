#!/usr/bin/env node

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";

const args = process.argv.slice(2);
const command = args[0] ?? "start";

function findRoot(start = process.cwd()) {
  let current = path.resolve(start);
  while (true) {
    if (existsSync(path.join(current, "scripts", "dev.mjs")) && existsSync(path.join(current, "web"))) return current;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

function configDir() {
  return process.env.BLANK_HOME
    ? path.resolve(process.env.BLANK_HOME)
    : process.platform === "win32"
    ? path.join(process.env.USERPROFILE ?? process.cwd(), ".blank")
    : path.join(process.env.HOME ?? process.cwd(), ".blank");
}

function configPath() {
  return path.join(configDir(), "config.json");
}

function readConfig() {
  init();
  try {
    return JSON.parse(readFileSync(configPath(), "utf8"));
  } catch {
    return { version: 1, packages: {}, providers: [] };
  }
}

function writeConfig(value) {
  mkdirSync(configDir(), { recursive: true });
  writeFileSync(configPath(), JSON.stringify(value, null, 2) + "\n", "utf8");
}

function init() {
  const dir = configDir();
  mkdirSync(dir, { recursive: true });
  const config = path.join(dir, "config.json");
  if (!existsSync(config)) {
    writeFileSync(config, JSON.stringify({ version: 1, packages: {}, providers: [] }, null, 2) + "\n", "utf8");
  }
  console.log(`空白格配置已初始化：${dir}`);
}

function findPython(root) {
  const candidates = [
    process.env.BLANK_PYTHON,
    root && path.join(root, "services", "api", ".venv", "Scripts", "python.exe"),
    root && path.join(root, "services", "api", ".venv", "bin", "python"),
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

/**
 * `blank doctor --sandbox` 不自己判断边界：本机能不能拦住越界，只有真跑一遍才知道。
 * 这里只负责把同一个 API 包里的 doctor 探针拉起来，并沿用源码工作区的 PYTHONPATH。
 */
function sandboxDoctor(rest) {
  const root = findRoot();
  const python = findPython(root);
  if (!python) {
    console.error("找不到后端 Python：设置 BLANK_PYTHON，或在源码工作区里先建 services/api/.venv。");
    process.exitCode = 2;
    return;
  }
  const pythonPath = root ? path.join(root, "services", "api", "src") : "";
  const env = { ...process.env, PYTHONIOENCODING: "utf-8" };
  if (pythonPath) {
    env.PYTHONPATH = [pythonPath, env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  }
  const child = spawn(python, ["-X", "utf8", "-m", "blank_api.doctor", ...rest], {
    cwd: root ? path.join(root, "services", "api") : process.cwd(),
    stdio: "inherit",
    env,
  });
  child.on("error", (error) => {
    console.error(`拉起边界探针失败：${error.message}`);
    process.exitCode = 2;
  });
  child.on("exit", (code) => process.exit(code ?? 1));
}

function doctor() {
  console.log(`Node.js: ${process.version}`);
  console.log(`平台：${process.platform}`);
  console.log(`配置：${configDir()}`);
  console.log(`工作区：${findRoot() ?? "未检测到项目工作区"}`);
  if (args.includes("--sandbox") || args.includes("--compare")) sandboxDoctor(args.slice(1));
}

function listPackages() {
  const config = readConfig();
  const packages = config.packages ?? {};
  const names = Object.keys(packages);
  if (!names.length) {
    console.log("还没有启用可选功能。使用 blank add <package> 按需启用。");
    return;
  }
  console.log("已启用功能：");
  for (const name of names) console.log(`- ${name}${packages[name]?.enabled === false ? "（停用）" : ""}`);
}

function packageCommand(action, packageName) {
  if (!packageName) {
    console.error(`用法：blank ${action} <package>`);
    process.exitCode = 1;
    return;
  }
  const config = readConfig();
  config.packages ??= {};
  if (action === "add") {
    config.packages[packageName] = { enabled: true, installedAt: new Date().toISOString() };
    writeConfig(config);
    console.log(`已启用功能：${packageName}`);
    console.log("首次使用该功能时，空白格会检查并安装可选依赖。");
    return;
  }
  delete config.packages[packageName];
  writeConfig(config);
  console.log(`已停用功能：${packageName}`);
}

function start() {
  init();
  const root = findRoot();
  if (!root) {
    console.log("当前目录不是空白格源码工作区。");
    console.log("请先进入项目目录，或使用已发布版本的本地运行器。 ");
    console.log("用法：blank init / blank doctor");
    return;
  }
  console.log("启动空白格本地工作台…");
  const child = spawn(process.execPath, [path.join(root, "scripts", "dev.mjs")], {
    cwd: root,
    stdio: "inherit",
    env: process.env,
  });
  child.on("exit", (code) => process.exit(code ?? 0));
}

switch (command) {
  case "init":
    init();
    break;
  case "doctor":
    doctor();
    break;
  case "list":
    listPackages();
    break;
  case "add":
  case "remove":
    packageCommand(command, args[1]);
    break;
  case "start":
  case "run":
    start();
    break;
  case "--help":
  case "-h":
    console.log("blank — 空白格 AI 工程操作台");
    console.log("\n用法：blank [start|init|doctor|list|add|remove]");
    console.log("\n示例：");
    console.log("  blank                 启动当前源码工作区");
    console.log("  blank init            初始化本地配置");
    console.log("  blank list            查看已启用的可选功能");
    console.log("  blank add brainstorm  启用头脑风暴功能");
    console.log("  blank doctor          检查 Node、配置和工作区");
  console.log("  blank doctor --sandbox  逐条攻击八条执行边界，并和这次执行的账本比对");
    break;
  default:
    console.error(`未知命令：${command}`);
    process.exitCode = 1;
}
