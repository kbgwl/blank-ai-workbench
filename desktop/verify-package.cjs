/**
 * 打包后自检：确认「包里跑的确实是刚打的代码」。
 *
 * 起因：桌面版的 Python 运行时里曾带着一份 `site-packages/blank_api` 的旧副本，
 * 而 uvicorn 从 `api/` 目录启动时找不到 `api/src/blank_api`，于是静默加载旧副本。
 * 结果前端是新的、接口是 404，界面上一片 "Not Found"，但打包日志一切正常。
 *
 * 更麻烦的是：用 `sys.path.insert(0, "api/src")` 去验证会强行指向源码，
 * 恰好把这个问题盖住 —— 所以这个脚本必须复刻**应用真实的启动方式**，不许改 sys.path。
 *
 * 用法：node verify-package.cjs
 */
const { execFileSync } = require("node:child_process");
const { closeSync, existsSync, openSync, readFileSync, readdirSync, readSync } = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "dist", "win-unpacked");
const RESOURCES = path.join(ROOT, "resources");
const PYTHON = path.join(RESOURCES, "backend", "runtime", "python.exe");
const API_DIR = path.join(RESOURCES, "backend", "api");
const WEB_DIR = path.join(RESOURCES, "web");

const failures = [];
const ok = (label, detail = "") => console.log(`  ok    ${label}${detail ? "  " + detail : ""}`);
const bad = (label, detail = "") => {
  failures.push(label);
  console.log(`  FAIL  ${label}${detail ? "  " + detail : ""}`);
};

console.log(`检查 ${ROOT}\n`);

// ── 1. 产物是否齐 ───────────────────────────────────────────────────────────
for (const [label, target] of [
  ["包内 Python", PYTHON],
  ["后端源码", path.join(API_DIR, "src", "blank_api", "app.py")],
  ["前端静态站", path.join(WEB_DIR, "index.html")],
]) {
  existsSync(target) ? ok(label) : bad(label, `缺失 ${target}`);
}

// ── 1b. asar 里的主进程文件（壳 require 的东西漏打，装完就是双击没反应）──────
// asar 头是 `[8 字节 pickle 头][JSON 头]`，offset 12 是头字符串长度，16 起是 JSON。
// 只读头，不把整个 asar 读进内存。
const ASAR = path.join(RESOURCES, "app.asar");
if (!existsSync(ASAR)) {
  bad("缺 app.asar");
} else {
  try {
    const fd = openSync(ASAR, "r");
    const head = Buffer.alloc(16);
    readSync(fd, head, 0, 16, 0);
    const headerSize = head.readUInt32LE(12);
    const headerBuf = Buffer.alloc(headerSize);
    readSync(fd, headerBuf, 0, headerSize, 16);
    closeSync(fd);
    const entries = Object.keys(JSON.parse(headerBuf.toString("utf8")).files ?? {});
    for (const name of ["main.cjs", "gpu-guard.cjs", "preload.cjs"]) {
      entries.includes(name) ? ok(`asar 含 ${name}`) : bad(`asar 缺 ${name}`, "壳启动时会 require 失败");
    }
  } catch (error) {
    bad("app.asar 头解析失败", error.message);
  }
}

// ── 2. site-packages 里不许有 blank_api 旧副本 ──────────────────────────────
const sitePackages = path.join(RESOURCES, "backend", "runtime", "Lib", "site-packages");
const stale = path.join(sitePackages, "blank_api");
if (existsSync(stale)) {
  bad("site-packages 里有 blank_api 旧副本", "它会盖掉 api/src 里的新代码");
} else {
  ok("site-packages 无 blank_api 副本");
}

const pth = path.join(sitePackages, "blank_api.pth");
if (!existsSync(pth)) {
  bad("缺 blank_api.pth", "运行时将找不到源码包");
} else {
  const content = readFileSync(pth, "utf8").trim();
  content === "../../../api/src"
    ? ok("blank_api.pth 指向 api/src")
    : bad("blank_api.pth 内容不对", JSON.stringify(content));
}

// ── 3. 用包内解释器按真实方式 import（cwd = api，不改 sys.path）────────────
if (existsSync(PYTHON)) {
  try {
    const code = [
      "import json, blank_api",
      "from blank_api.api.routes import api_router",
      "import blank_api.api.routes as routes",
      "print(json.dumps({",
      "  'file': blank_api.__file__,",
      "  'has_settings': hasattr(routes, 'settings'),",
      "  'has_threads': hasattr(routes, 'threads'),",
      "}))",
    ].join("\n");
    const raw = execFileSync(PYTHON, ["-c", code], { cwd: API_DIR, encoding: "utf8" });
    const info = JSON.parse(raw.trim().split("\n").pop());
    const fromSource = info.file.replace(/\\/g, "/").includes("/backend/api/src/blank_api/");
    fromSource ? ok("import 落在 api/src", info.file) : bad("import 落点错误", info.file);
    info.has_settings ? ok("settings 路由已注册") : bad("settings 路由缺失");
    info.has_threads ? ok("threads 路由已注册") : bad("threads 路由缺失");
  } catch (error) {
    bad("包内解释器导入失败", error.message.split("\n")[0]);
  }
}

// ── 4. 前端是不是最新一版（关键文案抽样）───────────────────────────────────
const settingsHtml = path.join(WEB_DIR, "settings.html");
if (existsSync(settingsHtml)) {
  const html = readFileSync(settingsHtml, "utf8");
  html.includes("0.4.0 新增")
    ? ok("前端含新设置页")
    : bad("前端是旧版本", "settings.html 没有新分组标记");
}

// 会话条的文案打包后落在 JS chunk 里，页面 HTML 上看不到，只能扫 chunk。
// 抽样一句只有会话条才有的文案，避免"前端是上一版、界面看起来正常但点不出会话"。
const chunksDir = path.join(WEB_DIR, "_next", "static", "chunks");
if (existsSync(chunksDir)) {
  const hit = readdirSync(chunksDir)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(path.join(chunksDir, name), "utf8").includes("继续聊"));
  hit ? ok("前端含会话条") : bad("前端是旧版本", "没有找到会话条文案");
} else {
  bad("前端静态资源目录缺失", chunksDir);
}

console.log("");
if (failures.length) {
  console.log(`未通过 ${failures.length} 项：${failures.join(" / ")}`);
  process.exit(1);
}
console.log("包内自检全部通过");
