#!/usr/bin/env node

import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import http from "node:http";
import path from "node:path";
import process from "node:process";

const args = process.argv.slice(2);
const command = args[0] ?? "start";

const API_PORT = Number(process.env.BLANK_API_PORT ?? 8000);
const WEB_PORT = Number(process.env.BLANK_WEB_PORT ?? 3000);

function findRoot(start = process.cwd()) {
  // 干净目录模式：CLI 安装到全局后，用户的工作目录不是源码树。
  // BLANK_SOURCE_ROOT 显式指到后端/前端资源所在位置，cwd 只承载用户数据。
  if (process.env.BLANK_SOURCE_ROOT) {
    const root = path.resolve(process.env.BLANK_SOURCE_ROOT);
    if (existsSync(path.join(root, "web", "out")) && existsSync(path.join(root, "services", "api", "src"))) {
      return root;
    }
    console.error(`BLANK_SOURCE_ROOT=${root} 里找不到 services/api/src 与 web/out。`);
  }
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
 * `blank-ai doctor --sandbox` 不自己判断边界：本机能不能拦住越界，只有真跑一遍才知道。
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
  const root = findRoot();
  console.log(`工作区：${root ?? "未检测到项目工作区"}`);
  // 环境自检（任务书 §11）：缺什么就给出具体安装命令，不让人自己去猜。
  if (root) {
    const python = findPython(root);
    console.log(`后端 Python：${python ?? "未找到（services/api/.venv 不存在，也没设 BLANK_PYTHON）"}`);
    if (!python) {
      console.log("");
      console.log("安装指引（任选其一）：");
      console.log("  1) 安装 uv（推荐，自动管理 Python 与依赖）：");
      console.log("     Windows:  powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"");
      console.log("     macOS/Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh");
      console.log("  2) 或自行安装 Python 3.12：https://www.python.org/downloads/");
      console.log("     然后在 services/api 目录执行：python -m venv .venv");
      console.log("");
      console.log("不想装本地环境？桌面安装版内置了完整运行时：见 README 的桌面端下载说明。");
    }
  }
  if (args.includes("--sandbox") || args.includes("--compare")) sandboxDoctor(args.slice(1));
}

function listPackages() {
  const config = readConfig();
  const packages = config.packages ?? {};
  const names = Object.keys(packages);
  if (!names.length) {
    console.log("还没有启用可选功能。使用 blank-ai add <package> 按需启用。");
    return;
  }
  console.log("已启用功能：");
  for (const name of names) console.log(`- ${name}${packages[name]?.enabled === false ? "（停用）" : ""}`);
}

function packageCommand(action, packageName) {
  if (!packageName) {
    console.error(`用法：blank-ai ${action} <package>`);
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

function openBrowser(url) {
  const cmd =
    process.platform === "win32"
      ? spawn("cmd", ["/c", "start", "", url], { stdio: "ignore", detached: true })
      : process.platform === "darwin"
        ? spawn("open", [url], { stdio: "ignore", detached: true })
        : spawn("xdg-open", [url], { stdio: "ignore", detached: true });
  cmd.on("error", () => console.log(`（无法自动打开浏览器，请手动访问 ${url}）`));
}

/** 静态站点服务：web/out 存在时的零依赖模式（Node 内置 http，不需要 pnpm/next）。 */
function serveStatic(root, children) {
  const outDir = path.join(root, "web", "out");
  const mime = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
  };
  const server = http.createServer((req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://localhost");
      let file = path.join(outDir, decodeURIComponent(url.pathname));
      if (!file.startsWith(outDir)) {
        res.writeHead(403).end();
        return;
      }
      // 目录请求回落到 index.html；SPA 深链同理（存在同名目录时就近找 index）。
      if (existsSync(file) && statSync(file).isDirectory()) file = path.join(file, "index.html");
      if (!existsSync(file)) file = path.join(outDir, "404.html");
      if (!existsSync(file)) {
        res.writeHead(404).end("Not Found");
        return;
      }
      const body = readFileSync(file);
      res.writeHead(200, { "content-type": mime[path.extname(file)] ?? "application/octet-stream" });
      res.end(body);
    } catch {
      // 单个请求失败不能拖垮整个进程——坏请求就按 500 回，服务继续活着。
      if (!res.headersSent) res.writeHead(500);
      res.end("Internal Server Error");
    }
  });
  server.listen(WEB_PORT, "127.0.0.1", () => {
    console.log(`→ 界面:  http://127.0.0.1:${WEB_PORT}`);
    openBrowser(`http://127.0.0.1:${WEB_PORT}`);
  });
  children.push(server);
}

function start() {
  init();
  const root = findRoot();
  if (!root) {
    console.log("当前目录不是空白格源码工作区，也没设 BLANK_SOURCE_ROOT。");
    console.log("两种方式启动：");
    console.log("  1) 设置 BLANK_SOURCE_ROOT 指向含 services/api 与 web/out 的目录（推荐，工作目录保持干净）；");
    console.log("  2) 进入项目源码目录直接运行。");
    console.log("用法：blank-ai init / blank-ai doctor");
    process.exitCode = 2;
    return;
  }
  console.log("启动空白格本地工作台…");
  const children = [];
  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    for (const child of children) {
      try {
        child.kill();
      } catch {
        // 子进程可能已经退出，忽略。
      }
    }
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  const apiDir = path.join(root, "services", "api");
  const venvPython = findPython(root);
  // 优先 uv（自动管理依赖与 Python 版本）；其次源码工作区里的 .venv。
  // 都没有就停下来说明怎么装——静默失败会让用户以为"命令坏了"。
  const hasUv = spawn("uv", ["--version"], { stdio: "ignore" }).once("error", () => undefined);
  let apiStarted = false;

  function launchApi(cmd, cmdArgs) {
    const child = spawn(cmd, cmdArgs, {
      cwd: apiDir,
      stdio: "inherit",
      env: {
        ...process.env,
        // Windows 的 GBK 控制台会把后端中文日志打成乱码，统一 utf-8。
        PYTHONIOENCODING: "utf-8",
        PYTHONPATH: [path.join(apiDir, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    });
    children.push(child);
    child.on("exit", (code) => {
      if (!shuttingDown && apiStarted) {
        console.error(`后端退出（code ${code}），正在关闭。`);
        shutdown();
      }
    });
    apiStarted = true;
  }

  if (venvPython) {
    console.log("→ 后端:  使用 services/api/.venv 的 Python");
    launchApi(venvPython, ["-X", "utf8", "-m", "uvicorn", "blank_api.app:app", "--port", String(API_PORT)]);
  } else {
    // uv 是异步探测的：查到就用，查不到给安装指引并退出。
    const probe = spawn("uv", ["--version"], { stdio: "ignore" });
    probe.on("error", () => {
      console.error("没有找到可用的后端 Python 运行时（services/api/.venv 与 uv 都不存在）。");
      console.error("安装 uv（推荐）：powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"");
      console.error("或安装 Python 3.12 后在 services/api 执行：python -m venv .venv");
      console.error("桌面安装版内置完整运行时，见 README。");
      process.exitCode = 2;
    });
    probe.on("exit", (code) => {
      if (code === 0) {
        console.log("→ 后端:  使用 uv 管理（自动装依赖）");
        launchApi("uv", ["run", "--project", apiDir, "uvicorn", "blank_api.app:app", "--port", String(API_PORT)]);
      }
    });
  }

  // 静态导出存在就直接用零依赖模式；否则退回开发模式（需要 pnpm，见 scripts/dev.mjs）。
  if (existsSync(path.join(root, "web", "out", "index.html"))) {
    serveStatic(root, children);
  } else {
    console.log(`→ 界面:  未找到 web/out 静态导出，请访问 API 文档 http://127.0.0.1:${API_PORT}/docs`);
    console.log("        （想要完整界面：仓库根目录执行 pnpm --dir web build，产物在 web/out）");
    openBrowser(`http://127.0.0.1:${API_PORT}/docs`);
  }
  console.log(`后端 http://127.0.0.1:${API_PORT}  ·  Ctrl+C 退出`);
}

switch (command) {
  case "start":
  case "run":
  case undefined:
    start();
    break;
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
  case "--help":
  case "-h":
    console.log("blank-ai — 空白格 AI 工程操作台");
    console.log("\n用法：blank-ai [start|init|doctor|list|add|remove]");
    console.log("\n示例：");
    console.log("  blank-ai                 启动本地工作台（自检环境，缺依赖时给出安装命令）");
    console.log("  blank-ai init            初始化本地配置");
    console.log("  blank-ai doctor          检查 Node、Python、配置和工作区");
    console.log("  blank-ai list            查看已启用的可选功能");
    console.log("  blank-ai add brainstorm  启用头脑风暴功能");
    console.log("  blank-ai doctor --sandbox  逐条攻击八条执行边界，并和这次执行的账本比对");
    console.log("\n环境变量：");
    console.log("  BLANK_SOURCE_ROOT  干净目录启动时指向源码根（含 services/api 与 web/out）");
    console.log("  BLANK_API_PORT     后端端口（默认 8000）");
    console.log("  BLANK_WEB_PORT     界面端口（默认 3000）");
    break;
  default:
    console.error(`未知命令：${command}`);
    process.exitCode = 1;
}
