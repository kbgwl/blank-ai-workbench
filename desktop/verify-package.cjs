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
const { closeSync, existsSync, openSync, readFileSync, readdirSync, readSync, statSync, unlinkSync, writeFileSync } = require("node:fs");
const os = require("node:os");
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
  // 托盘图标在 asar 外（extraResources）：nativeImage 吃 asar 路径不可靠，
  // 漏打时壳的托盘整条挂掉，所以按产物断言它存在。
  ["托盘图标", path.join(RESOURCES, "shell", "icon.ico")],
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
    for (const name of ["main.cjs", "gpu-guard.cjs", "shell-policy.cjs", "preload.cjs"]) {
      entries.includes(name) ? ok(`asar 含 ${name}`) : bad(`asar 缺 ${name}`, "壳启动时会 require 失败");
    }
  } catch (error) {
    bad("app.asar 头解析失败", error.message);
  }
}

// ── 1c. 壳里那几份 .cjs 必须与仓库逐字节一致 ────────────────────────────────
// 「asar 含 main.cjs」只说明文件名在目录头里；装进包的若是改动前的旧副本，
// 应用照跑、界面照开，缺陷只在真实行为上露头（这次是 #72 的后端地址解析）。
// 偏移口径先在真包上量过：文件区紧跟在「16 + 头 JSON 长度（按 4 字节补齐）」之后，
// 头里每个 offset 相对文件区。取出来的字节拿去和仓库原文件比——比上了才说明读对了偏移，
// 读错只会拿到对不上的内容，这条判定自己就红，不需要另设自检。
function asarFile(asarPath, name) {
  const fd = openSync(asarPath, "r");
  try {
    const head = Buffer.alloc(16);
    readSync(fd, head, 0, 16, 0);
    const headerSize = head.readUInt32LE(12);
    const headerBuf = Buffer.alloc(headerSize);
    readSync(fd, headerBuf, 0, headerSize, 16);
    const meta = JSON.parse(headerBuf.toString("utf8")).files?.[name];
    if (!meta || meta.unpacked) return null;
    const dataStart = 16 + Math.ceil(headerSize / 4) * 4;
    const body = Buffer.alloc(Number(meta.size));
    readSync(fd, body, 0, Number(meta.size), dataStart + Number(meta.offset));
    return body;
  } finally {
    closeSync(fd);
  }
}

if (existsSync(ASAR)) {
  for (const name of ["api-base.cjs", "gpu-guard.cjs", "shell-policy.cjs", "main.cjs", "preload.cjs"]) {
    const packed = asarFile(ASAR, name);
    const source = path.join(__dirname, name);
    if (packed === null) {
      bad(`asar 里没有 ${name}`);
    } else if (!existsSync(source)) {
      bad(`仓库里找不到 ${name}`, "没法对账，这条判定不能空过");
    } else if (packed.equals(readFileSync(source))) {
      ok(`包内 ${name} 与仓库逐字节一致`, `${packed.length} 字节`);
    } else {
      bad(`包内 ${name} 是旧副本`, `包里 ${packed.length} 字节 / 仓库 ${statSync(source).size} 字节`);
    }
  }
}

// ── 1d. 包内那份 api-base.cjs 真跑一遍 ──────────────────────────────────────
// 逐字节一致仍然只是「形状对上了」：换端口这条判定的实体是一段函数的返回值。
// 把包里那份抽成临时文件、按应用真实的 require 方式加载，与仓库那份对同一组 env
// 逐条比答案——两边不同就是包里那份在骗人，两边相同才叫产物点验。
const ENV_CASES = [
  {},
  { BLANK_BACKEND_PORT: "8123" },
  { BLANK_API_BASE: "http://10.0.0.5:9000", BLANK_BACKEND_PORT: "8123" },
  { BLANK_API_BASE: "http://127.0.0.1:8123///" },
  { BLANK_API_BASE: "   ", BLANK_BACKEND_PORT: "8123" },
  { BLANK_BACKEND_PORT: "  " },
];
if (existsSync(ASAR)) {
  const packed = asarFile(ASAR, "api-base.cjs");
  const tmp = path.join(os.tmpdir(), `blank-api-base-${process.pid}.cjs`);
  if (packed === null) {
    bad("没法点验包内 api-base.cjs", "asar 里取不出来");
  } else {
    try {
      writeFileSync(tmp, packed);
      const fromPackage = require(tmp).resolveApiBase;
      const fromSource = require(path.join(__dirname, "api-base.cjs")).resolveApiBase;
      const rows = ENV_CASES.map((env) => [JSON.stringify(env), fromPackage(env), fromSource(env)]);
      const drift = rows.filter(([, packedValue, sourceValue]) => packedValue !== sourceValue);
      if (drift.length) {
        bad("包内 api-base.cjs 与仓库解析结果不同", drift.map(([env, a, b]) => `${env}→${a}|${b}`).join("、"));
      } else {
        ok("包内 api-base.cjs 真跑：六种 env 与仓库同解", rows.map(([, v]) => v).join(" "));
      }
      // 默认必须仍是本机 8000：这条钉的是「产物跑出来的值」，不是两份互相对账的一致性。
      rows[0][1] === "http://127.0.0.1:8000"
        ? ok("包内解析默认 8000")
        : bad("包内 api-base.cjs 默认地址不对", rows[0][1]);
    } catch (error) {
      bad("包内 api-base.cjs 加载或执行失败", String(error.message).split("\n")[0]);
    } finally {
      try {
        unlinkSync(tmp);
      } catch {}
    }
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

// ── 3b. 安装包内的边界探针：用包内解释器真跑一遍 doctor --sandbox ───────────
// 只检查 doctor.py 存在等于没检查：探针要按 .pth 的源码路径真的执行，八条边界
// 都要有结论，且不允许出现"不确定"。这一步也能抓到"包内解释器缺 stdlib 模块"。
const doctorReport = path.join(os.tmpdir(), `blank-doctor-${process.pid}.json`);
const DOCTOR_IDS = ["P-READ", "P-WRITE", "P-ENV", "P-NET", "P-PROC", "P-ESCAPE", "P-INHERIT", "P-AUDIT"];
if (existsSync(PYTHON)) {
  try {
    execFileSync(
      PYTHON,
      ["-X", "utf8", "-m", "blank_api.doctor", "--sandbox", "--json", doctorReport],
      {
        cwd: API_DIR,
        encoding: "utf8",
        timeout: 180000,
        env: { ...process.env, PYTHONIOENCODING: "utf-8", BLANK_SANDBOX_ALLOW_LOCAL: "1" },
      }
    );
    const report = JSON.parse(readFileSync(doctorReport, "utf8"));
    const ids = (report.boundaries ?? []).map((item) => item.id);
    report.capability?.apparatus_ok
      ? ok("包内边界探针装置可信")
      : bad("包内边界探针装置不可信", JSON.stringify(report.capability));
    ids.join(",") === DOCTOR_IDS.join(",")
      ? ok("包内八条边界都跑了")
      : bad("包内边界探针跑漏", ids.join(",") || "没有产出");
    report.exit_code === 0
      ? ok("包内边界报告无不确定项")
      : bad("包内边界报告有不确定项", JSON.stringify(report.summary));
  } catch (error) {
    bad("包内边界探针执行失败", String(error.message).split("\n")[0]);
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

// 逆向实验室三栏（#145）同理只认 chunk 里独有的一句。挑的是右栏那句边界说明：
// 它在页面文案里唯一，且只有真的调了四个端点的那一版才会渲染出来。
// 抽样文案别选版本号或「REVERSE LAB」这类会随改版过期/别处也有的字样。
if (existsSync(chunksDir)) {
  const labChunk = readdirSync(chunksDir)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(path.join(chunksDir, name), "utf8").includes("候选实物"));
  labChunk ? ok("前端含逆向实验室三栏") : bad("前端是旧版本", "没有找到逆向实验室左栏的文案");
} else {
  bad("前端静态资源目录缺失", chunksDir);
}

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

// #82 把「这一档免问多大一片」端上卡片。可这张卡的两半分别住在包里两个地方：
// 免问范围那句话是**后端算出来的**（`approval_options[].warning`），只会作为接口载荷出现，
// 拿它扫前端 chunk 一定假红；界面上真正新增的实物只有档位前缀「本会话档：/工具档：」。
// 所以两半各认一条，缺一条就是缺一条，不许互相顶。
if (existsSync(chunksDir)) {
  const hit = readdirSync(chunksDir)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(path.join(chunksDir, name), "utf8").includes("本会话档："));
  hit ? ok("前端含审批档位前缀") : bad("前端是旧版本", "chunk 里没有「本会话档：」——免问范围那行上不了屏");
}

const packedPermissions = path.join(RESOURCES, "backend", "api", "src", "blank_api", "services", "permissions.py");
if (existsSync(packedPermissions)) {
  const py = readFileSync(packedPermissions, "utf8");
  const missing = ["只免问这一条命令", "ARGUMENT_SCOPED_TOOLS", "tool_grant_too_wide"].filter((s) => !py.includes(s));
  missing.length
    ? bad("包内后端是旧版本", `permissions.py 缺 ${missing.join("、")}`)
    : ok("包内后端含 #82 的窄档位与免问范围文案");
} else {
  bad("包内后端 permissions.py 缺失", packedPermissions);
}

// 桌面导出没有服务器，rewrite 不生效，README 要求构建时给
// `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000`。漏掉时烘焙进产物的是空串，
// 界面会去 fetch 相对路径 `/api/v1`——`file://` 下这条路必死，而打包日志一切正常。
// 壳的 preload 会在运行态注入覆盖它，所以这条只钉「兜底那一份是绝对地址」，
// 不替运行态注入背书（注入本身另有 1d 的逐条对账）。
if (existsSync(chunksDir)) {
  const hit = readdirSync(chunksDir)
    .filter((name) => name.endsWith(".js"))
    .some((name) => readFileSync(path.join(chunksDir, name), "utf8").includes("http://127.0.0.1:8000"));
  hit
    ? ok("前端烘焙了绝对后端地址")
    : bad("前端没烘焙后端地址", "构建时漏了 NEXT_PUBLIC_API_BASE，脱离壳注入就打不开接口");
}

// 0.8.0 起首页就是 bot 对话页。首页是唯一一个「换掉整套界面」的改动，
// 出问题时的表现是「能打开、但看到的是上一版操作台」——和正常只差一眼，
// 装完最容易漏。所以单独认一句只有输入框才有的文案。
//
// 0.8.12 起要认两句：静态导出的首屏里项目还没读回来，占位文案渲染的是
// "正在读取项目…"；数据回来之后才是"说点什么…"。两句都只出现在 bot 输入框上，
// 旧的操作台一句都不会有，判据一样能拦住"装成上一版"。
const homeHtml = path.join(WEB_DIR, "index.html");
if (existsSync(homeHtml)) {
  const html = readFileSync(homeHtml, "utf8");
  html.includes("说点什么") || html.includes("正在读取项目")
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
