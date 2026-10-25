#!/usr/bin/env node
/**
 * 把工作区里的源码同步进已安装的客户端。
 *
 * 为什么需要这一步：装好的客户端跑的是 `resources/` 下的副本（后端是
 * `resources/backend/api/src/blank_api`，前端是 `resources/web`），而不是工作区里的
 * 源码。所以"改了源码 + 测试通过"和"打开客户端看到变化"之间还差一次同步。
 * 少了它，改完看着没反应，很容易被当成没生效或者改错了。
 *
 * 用法：
 *   node self-deploy.cjs             # 后端 + 前端 + 壳入口（app.asar）都同步
 *   node self-deploy.cjs --api       # 只同步后端
 *   node self-deploy.cjs --web       # 只重建并同步前端
 *   node self-deploy.cjs --shell     # 只同步壳入口（main.cjs / preload.cjs / gpu-guard.cjs）
 *   node self-deploy.cjs --dry-run   # 只打印会做什么
 *
 * 壳入口（主进程与 preload）在打包产物 `resources/app.asar` 里，改它们要重新打包——
 * 一次 electron-builder 十几分钟，而实际改动往往只有那三个 .cjs 文件。所以这里
 * 解包 → 换文件 → 重新打包，效果等价于重新打包，但不用碰 Electron 与运行时。
 */

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const WEB = path.join(ROOT, "web");
const API_SRC = path.join(ROOT, "services", "api", "src", "blank_api");

const argv = process.argv.slice(2);
const dryRun = argv.includes("--dry-run");
const onlyApi = argv.includes("--api");
const onlyWeb = argv.includes("--web");
const onlyShell = argv.includes("--shell");
const doApi = !onlyWeb && !onlyShell;
const doWeb = !onlyApi && !onlyShell;
// 壳入口默认跟着"全量同步"走；只点了 --api / --web 时不动它——
// 那是在改后端或界面，顺手重打包 app.asar 只会让排查多一个变量。
const doShell = onlyShell || (!onlyWeb && !onlyApi);

function fail(message) {
  console.error(`✗ ${message}`);
  process.exit(1);
}

/** 安装目录不硬编码：和 install-local.cjs 读同一份配置，换机器只改一处。 */
function installDir() {
  const file = path.join(__dirname, "install-target.local.json");
  if (!fs.existsSync(file)) {
    fail("缺少 desktop/install-target.local.json（内容形如 {\"dir\": \"D:/a/b\"}）");
  }
  const dir = JSON.parse(fs.readFileSync(file, "utf8")).dir;
  if (!dir || !path.isAbsolute(dir)) fail(`install-target.local.json 里的 dir 不是绝对路径：${dir}`);
  if (!fs.existsSync(dir)) fail(`安装目录不存在：${dir}`);
  return dir;
}

/**
 * 先删再拷，而不是直接覆盖。
 *
 * 直接覆盖会在目标里留下源码中已经删掉的文件：旧模块还在，import 得到，
 * 于是"删掉的代码"照样生效，排查起来极其费解。
 */
function syncTree(source, target, label) {
  if (!fs.existsSync(source)) fail(`${label} 源目录不存在：${source}`);
  const count = walk(source).length;
  if (dryRun) {
    console.log(`  [dry-run] ${label}: ${count} 个文件 ${source} -> ${target}`);
    return;
  }
  fs.rmSync(target, { recursive: true, force: true });
  fs.cpSync(source, target, {
    recursive: true,
    filter: (item) => {
      const base = path.basename(item);
      // __pycache__ 必须排除：包内 Python 版本与开发机可能不同，
      // 带进去的 .pyc 会让 import 拿到字节码而忽略新改的 .py。
      return base !== "__pycache__" && !base.endsWith(".pyc");
    },
  });
  console.log(`  ✓ ${label}: ${count} 个文件 -> ${target}`);
}

function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "__pycache__") continue;
      out.push(...walk(full));
    } else if (!entry.name.endsWith(".pyc")) {
      out.push(full);
    }
  }
  return out;
}

/** 前端要走一遍静态导出：客户端没有服务器，打开的是导出的文件。 */
function buildWeb() {
  const nextBin = path.join(WEB, "node_modules", "next", "dist", "bin", "next");
  if (!fs.existsSync(nextBin)) fail(`找不到 next：${nextBin}（先在 web 下装依赖）`);
  const out = path.join(WEB, "out");
  if (dryRun) {
    console.log(`  [dry-run] 前端: ${process.execPath} ${nextBin} build（cwd=web）`);
    return out;
  }
  // 旧产物先挪开：上次构建的残留页面会被一起拷进去，表现为"删掉的页面还能打开"。
  if (fs.existsSync(out)) fs.rmSync(out, { recursive: true, force: true });
  const started = Date.now();
  const result = spawnSync(process.execPath, [nextBin, "build"], {
    cwd: WEB,
    stdio: "inherit",
    env: {
      ...process.env,
      BLANK_DESKTOP_BUILD: "1",
      // 这里只是烘焙兜底值：壳运行时会用 BLANK_API_BASE / BLANK_BACKEND_PORT
      // 注入真实地址（见 preload.cjs），换端口不必重新构建。
      NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000",
      NODE_OPTIONS: "",
    },
  });
  if (result.status !== 0) fail(`前端构建失败（exit=${result.status}）`);
  console.log(`  ✓ 前端构建完成，用时 ${((Date.now() - started) / 1000).toFixed(1)}s`);
  if (!fs.existsSync(out)) fail("构建完成但没有 out 目录，检查 next.config 的 output=export");
  return out;
}

/**
 * 壳入口：就地替换 app.asar 里那三个 .cjs。
 *
 * 前提是客户端没在运行——Windows 下正在被加载的 app.asar 是独占打开的文件，
 * 重新打包会直接 EPERM。与其让用户对着报错猜，不如提前说清"先完全退出"。
 */
function syncShell(dir) {
  const asarPath = path.join(dir, "resources", "app.asar");
  if (!fs.existsSync(asarPath)) {
    console.log("  - 没有 resources/app.asar（可能是解包运行），跳过壳入口");
    return;
  }

  const entries = ["main.cjs", "api-base.cjs", "preload.cjs", "gpu-guard.cjs"];
  const sources = entries.map((name) => path.join(__dirname, name));
  for (const file of sources) {
    if (!fs.existsSync(file)) fail(`缺少壳入口源文件：${file}`);
  }

  const asarBin = path.join(__dirname, "node_modules", "@electron", "asar", "bin", "asar.js");
  if (!fs.existsSync(asarBin)) fail(`找不到 asar 工具：${asarBin}（先在 desktop 下装依赖）`);

  if (dryRun) {
    console.log(`  [dry-run] 壳入口: ${entries.join(" / ")} -> ${asarPath}`);
    return;
  }

  const work = path.join(os.tmpdir(), `blank-asar-${process.pid}`);
  const run = (args) => {
    const result = spawnSync(process.execPath, [asarBin, ...args], { stdio: "inherit" });
    if (result.status !== 0) fail(`asar ${args[0]} 失败（exit=${result.status}）`);
  };

  fs.rmSync(work, { recursive: true, force: true });
  try {
    run(["extract", asarPath, work]);

    // 先比一遍再决定要不要重打包：壳入口十次改动里有九次跟它无关，
    // 每次无脑重写 app.asar 会让安装目录的时间戳天天变，看不出到底动了什么。
    const changed = entries.filter((name) => {
      const inside = path.join(work, name);
      if (!fs.existsSync(inside)) return true;
      return !fs.readFileSync(inside).equals(fs.readFileSync(path.join(__dirname, name)));
    });
    if (changed.length === 0) {
      console.log("  = 壳入口已是最新，无需重打包");
      return;
    }

    for (const name of changed) fs.copyFileSync(path.join(__dirname, name), path.join(work, name));

    // 备份放在同级：重打包中途失败时，把 .bak 改回 app.asar 就能恢复可启动状态。
    const backup = `${asarPath}.bak`;
    fs.copyFileSync(asarPath, backup);
    fs.rmSync(asarPath, { force: true });
    run(["pack", work, asarPath]);
    console.log(`  ✓ 壳入口: ${changed.join(" / ")} -> ${asarPath}（旧包备份 app.asar.bak）`);
  } catch (error) {
    // 独占锁的表现是 EPERM/EBUSY，把它翻译成用户能照做的一句话。
    const code = error && error.code;
    if (code === "EPERM" || code === "EBUSY") {
      fail("app.asar 正被占用：先把客户端完全退出（托盘右键退出，不是关窗口），再重跑这一步");
    }
    throw error;
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
}

function main() {
  const dir = installDir();
  console.log(`目标客户端：${dir}`);
  console.log(`源工作区：  ${ROOT}${dryRun ? "（dry-run）" : ""}`);

  if (doApi) {
    console.log("\n同步后端源码");
    syncTree(API_SRC, path.join(dir, "resources", "backend", "api", "src", "blank_api"), "后端");
  }

  if (doWeb) {
    console.log("\n重建并同步前端");
    const out = buildWeb();
    if (!dryRun) {
      syncTree(out, path.join(dir, "resources", "web"), "前端");
    }
  }

  if (doShell) {
    console.log("\n同步壳入口（主进程 / preload）");
    syncShell(dir);
  }

  if (dryRun) {
    console.log("\n[dry-run] 没有改动任何文件。");
    return;
  }
  console.log(
    [
      "",
      "同步完成。",
      "还需要重启客户端才生效：后端是客户端启动时拉起的子进程，前端要重新加载资源。",
      "  - 完全退出客户端（托盘/任务栏右键退出，不是关窗口）后重新打开",
      "  - 只改了前端时，也可以在客户端窗口里按 Ctrl+R 先看一眼",
    ].join(os.EOL),
  );
}

main();
