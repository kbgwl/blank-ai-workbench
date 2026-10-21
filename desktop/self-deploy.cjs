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
 *   node self-deploy.cjs             # 后端 + 前端都同步
 *   node self-deploy.cjs --api       # 只同步后端
 *   node self-deploy.cjs --web       # 只重建并同步前端
 *   node self-deploy.cjs --dry-run   # 只打印会做什么
 *
 * 边界：主进程（main.cjs / preload.cjs）在 app.asar 里，这个脚本不动它——
 * 那部分改动必须重新打包安装。所以它只适用于界面与后端逻辑。
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
const doApi = !onlyWeb;
const doWeb = !onlyApi;

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
      // 桌面壳里前端直连本机后端，和打包时用同一个地址，避免部署完打不开接口。
      NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000",
      NODE_OPTIONS: "",
    },
  });
  if (result.status !== 0) fail(`前端构建失败（exit=${result.status}）`);
  console.log(`  ✓ 前端构建完成，用时 ${((Date.now() - started) / 1000).toFixed(1)}s`);
  if (!fs.existsSync(out)) fail("构建完成但没有 out 目录，检查 next.config 的 output=export");
  return out;
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
      "",
      "注意：主进程（main.cjs / preload.cjs）在 app.asar 里，本脚本不处理，改动要重新打包。",
    ].join(os.EOL),
  );
}

main();
