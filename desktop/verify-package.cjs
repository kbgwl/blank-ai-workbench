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
// 抽样串得挑「只有这一页才有的静态文案」，别挑版本号字面量：文案会随改版过期，
// 而版本号那一类几乎必然过期（这次就是「0.4.0 新增」早已从源码消失，包明明是新的
// 却报旧版）。过期断言比没有断言更糟——它只会一直红，然后被人当噪音忽略。
// 选定前先确认唯一性：grep -l 这些串，只在目标页出现的才可用。
if (existsSync(settingsHtml)) {
  const html = readFileSync(settingsHtml, "utf8");
  html.includes("安全与权限")
    ? ok("前端含新设置页")
    : bad("前端是旧版本", "settings.html 没有「安全与权限」分组");
}

// chunk 目录：下面几项"只有某页才有的文案"都要扫它，所以先声明。
const chunksDir = path.join(WEB_DIR, "_next", "static", "chunks");

// 0.8.4 起 /team 是独立页面，靠静态导出产出 team.html。这个路由漏掉时界面不会报错
// ——只是导航里点进去 404，而打包日志一切正常。所以按产物本身判定，不看菜单。
const teamHtml = path.join(WEB_DIR, "team.html");
existsSync(teamHtml) ? ok("包内含 /team 页面") : bad("包内缺 /team 页面", teamHtml);

// 页面在导出时只是空壳，团队视图的文案要等客户端水合，所以只在 chunk 里认一句
// 只有团队页才有的字符串（和会话条同一套路），别拿 team.html 的内容当证据。
const teamChunk = existsSync(chunksDir)
  && readdirSync(chunksDir)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(path.join(chunksDir, name), "utf8").includes("Bot 名册"));
teamChunk ? ok("前端含团队视图") : bad("前端是旧版本", "没有找到团队页文案");

// 会话条的文案打包后落在 JS chunk 里，页面 HTML 上看不到，只能扫 chunk。
// 抽样一句只有会话条才有的文案，避免"前端是上一版、界面看起来正常但点不出会话"。
if (existsSync(chunksDir)) {
  const hit = readdirSync(chunksDir)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(path.join(chunksDir, name), "utf8").includes("继续聊"));
  hit ? ok("前端含会话条") : bad("前端是旧版本", "没有找到会话条文案");
} else {
  bad("前端静态资源目录缺失", chunksDir);
}

// 0.8.0 起首页就是 bot 对话页。首页是唯一一个「换掉整套界面」的改动，
// 出问题时的表现是「能打开、但看到的是上一版操作台」——和正常只差一眼，
// 装完最容易漏。所以单独认一句只有输入框才有的文案。
const homeHtml = path.join(WEB_DIR, "index.html");
if (existsSync(homeHtml)) {
  const html = readFileSync(homeHtml, "utf8");
  html.includes("说点什么")
    ? ok("首页是 bot 对话页")
    : bad("首页还是旧界面", "index.html 没有 bot 输入框文案");
} else {
  bad("首页缺失", homeHtml);
}

// ── 5. 包里不许有本机开发痕迹 ───────────────────────────────────────────────
// 起因：`extraResources` 从 `../services/api` 整目录往外拷，过滤清单只排掉了
// `.venv/data/tests`，于是开发机的 `.env`（指向 ./data 的 SQLite）和 `_verify/`
// （4.9MB 的 pytest 日志、junit）跟着进了客户包。前者是本机配置，后者是验证产物，
// 都不该出现在交付物里；更要命的是它一直"打包成功"，没人会去看。
const FORBIDDEN = /(^|[\\/])(\.env|\.env\..+|_verify|logs|\.git)$/;
const SECRET_FILE = /(\.db|\.db-wal|\.db-shm|\.key)$/;
const apiTree = path.join(RESOURCES, "backend", "api");
if (existsSync(apiTree)) {
  const leaked = [];
  for (const rel of readdirSync(apiTree, { recursive: true })) {
    const name = String(rel);
    if (FORBIDDEN.test(name) || SECRET_FILE.test(name)) leaked.push(name);
  }
  leaked.length
    ? bad("包内含本机开发痕迹", leaked.slice(0, 8).join("、"))
    : ok("包内无 .env/_verify/本地库");
} else {
  bad("包内后端目录缺失", apiTree);
}

// Key 只允许加密存在用户数据目录，绝不允许以明文进包。扫源码目录里的供应商 Key 形态。
const keyPattern = /\bsk-[A-Za-z0-9_-]{16,}\b/;
const srcTree = path.join(apiTree, "src");
if (existsSync(srcTree)) {
  const hits = [];
  for (const rel of readdirSync(srcTree, { recursive: true })) {
    const name = String(rel);
    if (!/\.(py|json|md|txt|toml|ini|env)$/.test(name)) continue;
    const full = path.join(srcTree, name);
    try {
      if (readFileSync(full, "utf8").match(keyPattern)) hits.push(name);
    } catch {}
  }
  hits.length ? bad("包内源码含疑似明文 Key", hits.slice(0, 5).join("、")) : ok("包内源码无明文 Key");
}

console.log("");
if (failures.length) {
  console.log(`未通过 ${failures.length} 项：${failures.join(" / ")}`);
  process.exit(1);
}
console.log("包内自检全部通过");
