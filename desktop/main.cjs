/**
 * 空白格 AI 操作台 · 桌面壳主进程
 *
 * 职责：
 *  1. 用自定义 app:// 协议承载静态导出的前端
 *  2. 守护本机后端：探测 → 未就绪则拉起 → 等健康检查 → 失败给可操作出口
 *  3. 应用退出时回收后端进程（不留下孤儿进程）
 *
 * 用 CommonJS 而不是 ESM：Electron 的 ESM 主进程在主进程侧拿不到 `electron` 的具名
 * 导出，且 sandbox 模式下 preload 只支持 CommonJS。CJS 是这两处都稳的选择。
 */
const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  globalShortcut,
  ipcMain,
  nativeImage,
  protocol,
  shell,
  Tray,
} = require("electron");
const { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const { readFile, writeFile } = require("node:fs/promises");
const { spawn } = require("node:child_process");
const path = require("node:path");
const process = require("node:process");
const { readAttempts, shouldDowngrade, startingState, readyState } = require("./gpu-guard.cjs");
const { resolveApiBase } = require("./api-base.cjs");
const {
  DEFAULT_HOTKEY,
  buildTrayMenuModel,
  decideUpdate,
  decideWindowClose,
  describeShortcutRegistration,
  extractFeedEntries,
  normalizeHotkey,
  parseUpdateFeedUrl,
  pickLatestEntry,
  planHotkeyToggle,
  planSecondInstanceReveal,
  shouldAbortSecondaryInstance,
} = require("./shell-policy.cjs");

/**
 * 主进程日志落文件。
 *
 * Electron 在 Windows 是 GUI 子系统程序：由父进程拉起（脚本、快捷方式、双击）时
 * 没有可写的控制台，`console.log` 直接消失。启动失败在这种情况下等于「双击没反应」，
 * 一句日志都拿不到。设置 BLANK_LOG_FILE 后把主进程输出同时写进文件，
 * 是这类问题唯一可靠的观测手段。
 */
const LOG_FILE = process.env.BLANK_LOG_FILE;
if (LOG_FILE) {
  try {
    mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    appendFileSync(LOG_FILE, `\n===== ${new Date().toISOString()} 启动 =====\n`);
  } catch {
    /* 日志目录不可写不该拦住应用启动 */
  }
  const tee = (level) => (chunk) => {
    try {
      appendFileSync(LOG_FILE, `[${new Date().toISOString()}] ${level} ${String(chunk)}\n`);
    } catch {
      /* 同上：日志失败不影响主流程 */
    }
  };
  console.log = tee("info");
  console.error = tee("error");
}

const PORT = String(process.env.BLANK_BACKEND_PORT ?? "").trim() || "8000";

// ── 单实例 ──────────────────────────────────────────────────────────────────

/**
 * 同机只允许一个壳。拿不到锁的实例必须在建窗口、spawn 后端、写 GPU 启动标记
 * 之前就让位（shouldAbortSecondaryInstance 的判定在 shell-policy.cjs，可单测）：
 * - 写 gpu-state 都算副作用——第二实例那次 +1 会把「连续没就绪」的账记花；
 * - 两份壳还会抢同一个后端端口、各 spawn 一个 uvicorn。
 *
 * ⚠️ 未验：本机不允许启动桌面壳（:8000/:3000 是用户的服务），
 * 「双实例真实启动时第二个放弃并把第一个唤回」只测了纯函数层，需要人工按下面步骤点验：
 *   1) 正常启动一个壳；2) 再启动一次；3) 第二个应无窗口闪现直接退出，
 *   4) 把第一个关进托盘/最小化后再启动第二次，窗口应被唤回并聚焦。
 */
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (shouldAbortSecondaryInstance(gotSingleInstanceLock)) {
  console.log("[desktop] 已有实例在运行，本实例放弃启动（不建窗口、不拉后端）");
  app.quit();
}

// 壳自身状态。显式退出只由 quitApplication() 或 before-quit 置位；
// 「关窗口」永远不该把它置真——那就是「关窗=退出」复活的入口。
const shellState = {
  trayAvailable: false,
  explicitQuit: false,
  backendOnline: false,
  hotkeyAccelerator: null,
  update: null,
};
// preload 把这个值注入渲染进程；前端构建时烘焙的 NEXT_PUBLIC_API_BASE 只是兜底。
const API_BASE = resolveApiBase(process.env);
const HEALTH_URL = process.env.BLANK_BACKEND_HEALTH ?? `${API_BASE}/api/v1/health`;
const APP_ORIGIN = "app://bundle";

/** 打包后静态站随 extraResources 落到 resources/web；开发态直接读 web/out。 */
const STATIC_ROOT = app.isPackaged
  ? path.join(process.resourcesPath, "web")
  : path.resolve(__dirname, "..", "web", "out");

/** 后端源码目录：打包后随 extraResources 落到 resources/backend/api。 */
const API_DIR = app.isPackaged
  ? path.join(process.resourcesPath, "backend", "api")
  : path.resolve(__dirname, "..", "services", "api");

/** 打包后自带的 Python 运行时（见 docs/APP-DESIGN 第 4 节）。缺失时退回本机环境。 */
const BUNDLED_PYTHON = path.join(process.resourcesPath ?? "", "backend", "runtime", "python.exe");

/** 用户数据目录：打包态落 %APPDATA%\blank-workbench。 */
const DATA_DIR = app.getPath("userData");

/**
 * 无 GPU 的环境（CI、远程会话、部分虚拟化）里 GPU 进程会反复崩溃，
 * 最终 `GPU process isn't usable. Goodbye.` 直接终止应用。此时改用软件渲染。
 *
 * 默认仍然优先用硬件加速（画 diff / 长列表明显更顺），但会记住上一次启动是否真的
 * 走到了「窗口就绪」：启动前先写一个 attempt 标记，等窗口 ready-to-show 再改写成 ok。
 * 连续两次都没走到 ok 才退到软件渲染——一次不算，因为强退、断电、装机时并发写文件
 * 都会留下这种标记，而它们的下一次启动其实完全正常。
 * 降级那次成功一次即复位，所以降级不是判决。
 */
const GPU_STATE_FILE = path.join(DATA_DIR, "gpu-state.json");

function readGpuState() {
  try {
    return JSON.parse(readFileSync(GPU_STATE_FILE, "utf8"));
  } catch {
    return {};
  }
}

function writeGpuState(state) {
  try {
    mkdirSync(DATA_DIR, { recursive: true });
    writeFileSync(GPU_STATE_FILE, JSON.stringify(state));
  } catch {
    /* 状态文件写不了就退回默认行为 */
  }
}

const previousGpuState = readGpuState();
// BLANK_FORCE_GPU=1 可以忽略降级记录、重新试一次硬件加速（换了机器或驱动更新后用）。
// 判据是「连续几次没走到窗口就绪」，不是「上次有没有成功」——一次强退或断电就会留下
// 一个没写 ok 的标记，那不该让这台机器此后永远跑软件渲染（详见 gpu-guard.cjs）。
const gpuUnstable = shouldDowngrade(previousGpuState, process.env.BLANK_FORCE_GPU === "1");

if (process.env.BLANK_DISABLE_GPU === "1" || gpuUnstable) {
  app.disableHardwareAcceleration();
  for (const flag of [
    "disable-gpu",
    "disable-gpu-compositing",
    "disable-gpu-sandbox",
    "in-process-gpu",
    "disable-software-rasterizer",
  ]) {
    app.commandLine.appendSwitch(flag);
  }
  if (process.env.BLANK_NO_SANDBOX === "1" || gpuUnstable) {
    // 实测：这类虚拟化/远程环境里，即使关了 GPU，Chromium 的渲染进程沙箱仍会卡住
    // （窗口永远不 ready-to-show），必须一起关掉沙箱。
    app.commandLine.appendSwitch("no-sandbox");
  }
  if (gpuUnstable) {
    console.log(
      `[desktop] 连续 ${readAttempts(previousGpuState)} 次启动没走到窗口就绪，这次改用软件渲染（成功一次即复位）`,
    );
  }
}

// 启动前先记「这次要试 GPU」，成功打开窗口后才改写成 ready。
// 让位中的第二实例不写：它没有窗口，写了就是白记一次「未就绪」，会把降级计数记花。
if (gotSingleInstanceLock && !process.env.BLANK_DISABLE_GPU && !gpuUnstable) {
  writeGpuState(startingState(previousGpuState));
}

/**
 * 静态站必须走自定义协议，不能直接用 file://。
 * 原因：Next 导出的资源引用是绝对路径（/_next/...），file:// 下会解析到盘符根目录；
 * 而且客户端路由（/tasks/detail?id=x）需要一个能兜底到对应 .html 的解析器。
 */
protocol.registerSchemesAsPrivileged([
  {
    scheme: "app",
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true },
  },
]);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ── 静态资源协议 ────────────────────────────────────────────────────────────

function resolveStaticFile(urlPath) {
  const clean = decodeURIComponent(urlPath || "/").replace(/\\/g, "/");
  const rel = clean.replace(/^\/+/, "");
  const candidates = [];
  if (!rel) {
    candidates.push("index.html");
  } else {
    candidates.push(rel);
    if (!path.extname(rel)) {
      candidates.push(`${rel}.html`);
      candidates.push(`${rel}/index.html`);
    }
  }
  for (const candidate of candidates) {
    const full = path.resolve(STATIC_ROOT, candidate);
    // 防目录穿越：解析结果必须仍在静态根下
    if (!full.startsWith(STATIC_ROOT)) continue;
    if (existsSync(full) && path.extname(full)) return full;
  }
  const notFound = path.join(STATIC_ROOT, "404.html");
  return existsSync(notFound) ? notFound : null;
}

function registerAppProtocol() {
  protocol.handle("app", async (request) => {
    const url = new URL(request.url);
    const file = resolveStaticFile(url.pathname);
    if (!file) {
      return new Response("静态资源缺失，请先执行桌面构建（BLANK_DESKTOP_BUILD=1 pnpm -C web build）", {
        status: 404,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    const body = await readFile(file);
    return new Response(body, {
      headers: { "content-type": MIME[path.extname(file).toLowerCase()] ?? "application/octet-stream" },
    });
  });
}

// ── 后端守护 ────────────────────────────────────────────────────────────────

let backend = null;
const backendLog = [];

function remember(line) {
  backendLog.push(line);
  if (backendLog.length > 200) backendLog.shift();
  console.log(`[backend] ${line}`);
}

async function backendReachable(timeoutMs = 1500) {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const response = await fetch(HEALTH_URL, { signal: controller.signal });
    clearTimeout(timer);
    return response.ok;
  } catch {
    return false;
  }
}

/** 在 PATH 里找一个可执行文件（Windows 需要试 .exe / .cmd / .bat）。 */
function findOnPath(name) {
  const dirs = (process.env.PATH ?? "").split(path.delimiter).filter(Boolean);
  for (const dir of dirs) {
    for (const ext of ["", ".exe", ".cmd", ".bat"]) {
      const candidate = path.join(dir, name + ext);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

/**
 * 决定用什么解释器跑后端。优先级：
 *   1. BLANK_BACKEND_CMD 显式指定（调试用）
 *   2. 包内自带的 Python 运行时（自包含分发的正解，见 APP-DESIGN 第 4 节）
 *   3. 包内或项目内的 .venv（最贴近开发环境）
 *   4. 本机 uv（装了 uv 的机器）
 *   5. 本机 python（需已装依赖）
 * 打包态缺 2/3 时会退到 4/5 —— 只能算内测可用，零依赖分发仍需补上第 2 项。
 */
function resolveBackendCommand() {
  const env = {
    ...process.env,
    BLANK_BACKEND_PORT: PORT,
    BLANK_SANDBOX_ENABLED: process.env.BLANK_SANDBOX_ENABLED ?? "false",
  };

  if (app.isPackaged) {
    mkdirSync(DATA_DIR, { recursive: true });
    env.BLANK_DATABASE_URL ??= `sqlite+aiosqlite:///${path.join(DATA_DIR, "blank.db").replace(/\\/g, "/")}`;
    // 主密钥必须跟数据库同寿。留在包内 data/ 的话，升级换包就换了一把钥匙，
    // 之前存进库里的供应商 Key 全部解不开（secret_undecryptable）。
    env.BLANK_SECRET_KEY_FILE ??= path.join(DATA_DIR, ".blank_secret.key");
    // 安全与权限也在这里。留在包内的话，升级一次客户的权限边界就悄悄变回默认值 ——
    // 对"权限可控"来说这比丢个偏好严重得多，所以必须跟数据库同寿。
    env.BLANK_SETTINGS_FILE ??= path.join(DATA_DIR, "settings.json");
  }

  const uvicornArgs = ["-m", "uvicorn", "blank_api.app:app", "--host", "127.0.0.1", "--port", PORT];

  if (process.env.BLANK_BACKEND_CMD) {
    return { command: process.env.BLANK_BACKEND_CMD, args: uvicornArgs, cwd: API_DIR, env, label: "BLANK_BACKEND_CMD" };
  }
  if (existsSync(BUNDLED_PYTHON)) {
    return { command: BUNDLED_PYTHON, args: uvicornArgs, cwd: API_DIR, env, label: "包内 Python" };
  }
  const venvPython = path.join(API_DIR, ".venv", "Scripts", "python.exe");
  if (existsSync(venvPython)) {
    return { command: venvPython, args: uvicornArgs, cwd: API_DIR, env, label: "项目 .venv" };
  }
  const uv = findOnPath("uv");
  if (uv) {
    return {
      command: uv,
      args: ["run", "--project", API_DIR, "uvicorn", "blank_api.app:app", "--host", "127.0.0.1", "--port", PORT],
      cwd: API_DIR,
      env,
      label: "本机 uv（首次会装依赖）",
    };
  }
  const python = findOnPath("python") ?? findOnPath("py");
  if (python) {
    return { command: python, args: uvicornArgs, cwd: API_DIR, env, label: "本机 python（需已装依赖）" };
  }
  return null;
}

/** 首次运行要建表；失败不致命（可能已经迁移过），只记日志。 */
async function migrateDatabase(spec) {
  if (!spec) return;
  await new Promise((resolve) => {
    const child = spawn(spec.command, ["-m", "alembic", "upgrade", "head"], {
      cwd: spec.cwd,
      env: spec.env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout?.on("data", (chunk) => remember(String(chunk).trim()));
    child.stderr?.on("data", (chunk) => remember(String(chunk).trim()));
    child.on("error", () => resolve());
    child.on("exit", () => resolve());
  });
}

async function startBackend() {
  const spec = resolveBackendCommand();
  if (!spec) {
    remember("没有可用的后端解释器：包内无自带运行时，且未找到 .venv / uv");
    return { ok: false, hint: "缺少后端运行时" };
  }
  remember(`启动方式 ${spec.label} → ${spec.command}`);

  try {
    backend = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: spec.env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    remember(`启动失败 ${error.message}`);
    return { ok: false, hint: error.message };
  }

  backend.stdout?.on("data", (chunk) => remember(String(chunk).trim()));
  backend.stderr?.on("data", (chunk) => remember(String(chunk).trim()));
  backend.on("exit", (code, signal) => {
    remember(`后端进程退出 code=${code} signal=${signal}`);
    backend = null;
    shellState.backendOnline = false;
    refreshTrayMenu();
  });
  backend.on("error", (error) => remember(`后端进程错误 ${error.message}`));

  // 最长等 30 秒（首次迁移 + 冷启动）
  for (let i = 0; i < 60; i += 1) {
    if (await backendReachable()) {
      remember("后端就绪");
      shellState.backendOnline = true;
      refreshTrayMenu();
      return { ok: true };
    }
    await sleep(500);
  }
  return { ok: false, hint: "后端启动超时" };
}

async function ensureBackend() {
  if (await backendReachable()) {
    remember("检测到已有后端在运行，直接复用");
    shellState.backendOnline = true;
    return { ok: true };
  }
  const spec = resolveBackendCommand();
  await migrateDatabase(spec);
  return startBackend();
}

function stopBackend() {
  shellState.backendOnline = false;
  refreshTrayMenu();
  if (!backend) return;
  remember("回收后端进程");
  try {
    backend.kill();
  } catch {
    /* 已退出 */
  }
  backend = null;
}

// ── 选择工作区目录 ──────────────────────────────────────────────────────────

/**
 * 原生目录对话框。
 *
 * 渲染进程在 sandbox 里，碰不到 Node，选目录只能回主进程做。加这个的直接原因：
 * 后端只认绝对路径，让用户手打在旁边那个输入框里——盘符、反斜杠、中文目录名
 * 任一处写错就是 422，而且没法在选之前确认那个目录到底在不在。
 */
/**
 * 在系统文件管理器里打开目录。
 *
 * 为什么需要它：绑好工作区之后，用户最想做的第一件事就是"看看它到底在改哪个目录"。
 * 界面上只能显示一串路径，点开才是他熟悉的那个窗口。先 existsSync 再打开，
 * 是因为 shell.openPath 对不存在的路径只会返回一句英文错误，用户看不懂。
 */
/**
 * 渲染进程在 sandbox 里读不到主进程的环境变量，后端地址只能由 preload 同步取一次。
 * 同步是刻意的：`http.ts` 在模块加载时就要拿到基址，异步注入会让首屏请求落回烘焙值。
 */
ipcMain.on("blank:api-base", (event) => {
  event.returnValue = API_BASE;
});

ipcMain.handle("blank:open-path", async (_event, payload) => {
  const target = typeof payload?.path === "string" ? payload.path.trim() : "";
  if (!target) return { ok: false, reason: "路径为空" };
  if (!existsSync(target)) return { ok: false, reason: "目录不存在，可能已被移动或删除" };
  const failure = await shell.openPath(target);
  return failure ? { ok: false, reason: failure } : { ok: true };
});

ipcMain.handle("blank:pick-directory", async (event, payload) => {
  const owner = BrowserWindow.fromWebContents(event.sender);
  const options = {
    title: "选择工作区目录",
    properties: ["openDirectory", "createDirectory"],
    buttonLabel: "用这个目录",
  };
  // 已经填过路径就从那儿打开：用户多半是在改一个已有的绑定，不该每次都从"文档"起跳。
  // 传进来的可能是打码后的 `<workspace:xxx>`，existsSync 为假，自动忽略即可。
  const current = typeof payload?.current === "string" ? payload.current.trim() : "";
  if (current && existsSync(current)) options.defaultPath = current;
  const result = owner
    ? await dialog.showOpenDialog(owner, options)
    : await dialog.showOpenDialog(options);
  if (result.canceled || result.filePaths.length === 0) return { path: "" };
  return { path: result.filePaths[0] };
});

// ── 窗口 ────────────────────────────────────────────────────────────────────

let mainWindow = null;
let tray = null;

/**
 * 托盘图标。开发态读 desktop/build/icon.ico；打包态它不在 app.asar 里
 * （asar 路径喂 nativeImage 不可靠），改从 extraResources 的 resources/shell/ 读。
 *
 * ⚠️ 未验：本机不许启动壳，托盘图标的实际观感（任务栏尺寸缩放、深浅色托盘背景）
 * 需要在能启动 GUI 的会话里人工点验；图标读不到时 createFromPath 返回空图不抛异常，
 * 这里用 isEmpty 判定并降级为「无托盘」，关窗行为退回退出，不会做出一个看不见又关不掉的窗口。
 */
const TRAY_ICON_PATH = app.isPackaged
  ? path.join(process.resourcesPath, "shell", "icon.ico")
  : path.join(__dirname, "build", "icon.ico");

function refreshTrayMenu() {
  if (!tray) return;
  const actions = {
    show: () => showMainWindow("托盘菜单「显示主窗口」"),
    quit: () => quitApplication("托盘菜单「退出」"),
  };
  const template = buildTrayMenuModel({
    backendOnline: shellState.backendOnline,
    update: shellState.update,
  }).map((item) => (item.id ? { ...item, click: actions[item.id] } : item));
  tray.setContextMenu(Menu.buildFromTemplate(template));
}

function createTray() {
  if (String(process.env.BLANK_TRAY ?? "") === "0") {
    console.log("[desktop] BLANK_TRAY=0：不建托盘，关窗行为退回退出");
    return;
  }
  try {
    const image = nativeImage.createFromPath(TRAY_ICON_PATH);
    if (image.isEmpty()) throw new Error(`图标读不到或为空：${TRAY_ICON_PATH}`);
    tray = new Tray(image);
    tray.setToolTip("空白格 AI 操作台");
    shellState.trayAvailable = true;
    refreshTrayMenu();
    tray.on("double-click", () => showMainWindow("双击托盘图标"));
    console.log(`[desktop] 托盘已就绪（${TRAY_ICON_PATH}）`);
  } catch (error) {
    shellState.trayAvailable = false;
    console.error(`[desktop] 托盘创建失败，关窗将退回退出而不是隐藏：${error.message}`);
  }
}

/** 把窗口唤回并聚焦：最小化、关进托盘、被别的窗口挡住三种形态都走这一条。 */
function showMainWindow(via) {
  const hasWindow = !!mainWindow && !mainWindow.isDestroyed();
  const plan = planSecondInstanceReveal({
    hasWindow,
    minimized: hasWindow ? mainWindow.isMinimized() : false,
    visible: hasWindow ? mainWindow.isVisible() : false,
  });
  console.log(`[desktop] 唤出主窗口（${via}）→ ${JSON.stringify(plan)}`);
  if (plan.create) {
    mainWindow = createWindow();
    return;
  }
  if (plan.restore) mainWindow.restore();
  if (plan.show) mainWindow.show();
  if (plan.focus) mainWindow.focus();
}

/**
 * 显式退出路径。托盘菜单、后端失败对话框之外的「退出」都走这里；
 * before-quit 里也会补置一次，覆盖 app.quit() 被别处直接调用的情况（截图自检就是）。
 * 没有这一位，「关窗进托盘」和「真的要退出」就分不开，托盘会把退出吞掉。
 */
function quitApplication(via) {
  console.log(`[desktop] 显式退出（${via}）`);
  shellState.explicitQuit = true;
  app.quit();
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1024,
    minHeight: 680,
    backgroundColor: "#f5f6f8",
    show: false,
    autoHideMenuBar: true,
    title: "空白格 AI 操作台",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once("ready-to-show", () => {
    console.log("[desktop] 窗口就绪并显示");
    // 真正走到这里才说明这次启动没崩在 GPU 上，计数归零（下次仍优先硬件加速）。
    // 降级那次成功也要复位：降级是临时措施，不是判决。旧代码在这里排除了 gpuUnstable，
    // 于是「一旦降级就永远回不到硬件加速」——注释里的承诺并没有兑现。
    if (process.env.BLANK_DISABLE_GPU !== "1") {
      writeGpuState(readyState());
    }
    win.show();
  });

  /**
   * 关闭路由（判定在 shell-policy.cjs 的 decideWindowClose，可单测）：
   * 托盘可用且不是显式退出 → 拦下关闭、藏进托盘；否则放行，走真的关闭。
   * 「显式退出」的位只由 quitApplication / before-quit 置——这条是防
   * 「关窗=退出」复活的闸：把它误接回 close，整个托盘就白做了。
   * 用不了 app.isQuitting()：它不在这份 Electron（38）的公开 API 里，
   * 所以退出意图必须由我们自己在 before-quit（app.quit() 会先于窗口 close 发出）置位。
   *
   * ⚠️ 未验：本机不许启动壳，✕ 键真实按压后进托盘、再从托盘唤回需在带 GUI 会话点验。
   */
  win.on("close", (event) => {
    const decision = decideWindowClose({
      explicitQuit: shellState.explicitQuit,
      trayAvailable: shellState.trayAvailable,
    });
    if (decision === "hide") {
      event.preventDefault();
      win.hide();
      console.log("[desktop] 点关闭：已藏进托盘（不是退出，后端继续运行）");
    }
  });
  win.on("closed", () => {
    if (mainWindow === win) mainWindow = null;
    console.log("[desktop] 窗口被关闭");
  });
  win.webContents.on("did-finish-load", () => console.log("[desktop] 页面加载完成"));
  win.webContents.on("did-fail-load", (_event, code, desc, url) => {
    console.error(`[desktop] 页面加载失败 ${code} ${desc} ${url}`);
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  // BLANK_START_PATH 让自检能直接落在某一页（如 settings.html），不必先点导航。
  const startPath = String(process.env.BLANK_START_PATH ?? "").replace(/^\/+/, "") || "index.html";
  void win.loadURL(`${APP_ORIGIN}/${startPath}`);
  return win;
}

/** 后端起不来时给可操作出口，而不是留一个连不上后端的白屏。 */
async function reportBackendFailure(win, hint) {
  const detail = backendLog.slice(-12).join("\n") || "（没有更多日志）";
  const { response } = await dialog.showMessageBox(win, {
    type: "error",
    title: "本地服务未启动",
    message: "应用需要本机后端在 127.0.0.1 上运行，但它没能启动。",
    detail: `${hint}\n\n最近日志：\n${detail}`,
    buttons: ["重试", "复制日志并退出", "退出"],
    defaultId: 0,
    cancelId: 2,
  });
  if (response === 0) {
    const again = await ensureBackend();
    if (again.ok) {
      win.reload();
    } else {
      await reportBackendFailure(win, again.hint ?? "仍未就绪");
    }
    return;
  }
  if (response === 1) {
    const { clipboard } = require("electron");
    clipboard.writeText(backendLog.join("\n"));
    console.log("[desktop] 后端日志已复制到剪贴板");
  }
  quitApplication("后端失败对话框退出");
}

/** 等前端真正渲染出内容（客户端组件需要时间挂载），再截图，避免拍到空白。 */
async function waitForContent(win, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const length = await win.webContents
      .executeJavaScript("document.body ? document.body.innerText.trim().length : 0")
      .catch(() => 0);
    if (length > 40) return length;
    await sleep(400);
  }
  return 0;
}

/** 自检用：BLANK_PROBE=1 时从渲染进程直连一次后端，把结果打进日志（排查跨源问题）。 */
async function probeBackendFromRenderer(win) {
  if (process.env.BLANK_PROBE !== "1") return;
  const base = API_BASE;
  const result = await win.webContents
    .executeJavaScript(
      `fetch(${JSON.stringify(`${base}/api/v1/health`)})
         .then(r => "ok " + r.status)
         .catch(e => "fail " + e.message)`,
    )
    .catch((error) => `execute-failed ${error.message}`);
  console.log(`[desktop] 渲染进程直连后端 ${base} → ${result}`);
}

/** 自检用：设置 BLANK_SHOT=<png 路径> 后启动，渲染完自动截图并退出。
 *  BLANK_SHOT_DELAY=<ms> 可延迟截图，用来观察需要时间恢复的状态（如健康检查重试）。 */
async function maybeScreenshot(win) {
  const target = process.env.BLANK_SHOT;
  if (!target) return;
  const length = await waitForContent(win);
  console.log(`[desktop] 渲染内容长度 ${length}`);
  await probeBackendFromRenderer(win);

  const readPill = () =>
    win.webContents
      .executeJavaScript(
        `(document.body.innerText.match(/服务正常|后端不可达|数据库[^\\n]*/) || ["<未找到>"])[0]`,
      )
      .catch(() => "<读取失败>");
  console.log(`[desktop] 徽标状态(截图前) ${await readPill()}`);

  const delay = Number(process.env.BLANK_SHOT_DELAY ?? 800);
  if (delay > 0) await sleep(delay);
  console.log(`[desktop] 徽标状态(${delay}ms 后) ${await readPill()}`);

  // 长页面自检：截到下面几屏的内容，否则"面板存在但截不到"和"没做"看起来一样。
  // 不能只调 window.scrollTo —— 设置页的滚动容器是内层 main（外壳是 h-dvh overflow-hidden），
  // 所以这里先找第一个真正可滚的元素，找不到再退回 window。
  const scrollY = Number(process.env.BLANK_SHOT_SCROLL ?? 0);
  if (scrollY > 0) {
    const script = `(() => {
      const y = ${Math.round(scrollY)};
      const scrollable = [document.scrollingElement, ...document.querySelectorAll("main, div")]
        .filter(Boolean)
        .find((node) => {
          if (node.scrollHeight <= node.clientHeight + 20) return false;
          const overflow = getComputedStyle(node).overflowY;
          return overflow === "auto" || overflow === "scroll";
        });
      if (scrollable) { scrollable.scrollTop = y; return scrollable.scrollTop; }
      window.scrollTo(0, y);
      return window.scrollY;
    })()`;
    const moved = await win.webContents.executeJavaScript(script).catch(() => 0);
    console.log(`[desktop] 截图前滚动到 ${moved}px`);
    await sleep(350);
  }

  const image = await win.webContents.capturePage();
  await writeFile(target, image.toPNG());
  console.log(`[desktop] 截图已保存 ${target}`);
  quitApplication("自检截图完成");
}

// ── 全局快捷键 ──────────────────────────────────────────────────────────────

/**
 * 注册唤出/隐藏组合（默认 Control+Alt+B，BLANK_HOTKEY 覆盖）。
 * 三态都在 describeShortcutRegistration 里收成日志：registered / occupied（被别的
 * 助手占了）/ error（register 抛了）。后两态一律**降级继续跑**——快捷键是锦上添花，
 * 它起不来不该拖垮主进程，更不该让窗口打不开。
 *
 * ⚠️ 未验：本机不许启动壳，「真按组合键唤出/隐藏」与「真的和别的助手撞车」
 * 需要在带 GUI 会话里人工点验；这里只钉住了判定与日志的可测部分。
 */
function registerGlobalShortcut() {
  const normalized = normalizeHotkey(process.env.BLANK_HOTKEY || DEFAULT_HOTKEY);
  if (!normalized.ok) {
    shellState.hotkeyAccelerator = null;
    console.error(`[desktop] ${normalized.reason}，降级：本次不启用全局快捷键，主流程不受影响`);
    return;
  }
  const { accelerator } = normalized;
  let outcome = null;
  try {
    // register 返回 false = 被占用；个别组合会直接抛，两者都不许冒到外层。
    outcome = globalShortcut.register(accelerator, toggleMainWindowFromHotkey);
  } catch (error) {
    outcome = error;
  }
  const result = describeShortcutRegistration(accelerator, outcome);
  console.log(`[desktop] ${result.message}`);
  shellState.hotkeyAccelerator = result.status === "registered" ? accelerator : null;
}

function toggleMainWindowFromHotkey() {
  const hasWindow = !!mainWindow && !mainWindow.isDestroyed();
  const plan = planHotkeyToggle({
    hasWindow,
    visible: hasWindow ? mainWindow.isVisible() : false,
    focused: hasWindow ? mainWindow.isFocused() : false,
  });
  if (plan.create) {
    console.log("[desktop] 快捷键：窗口不存在，重建");
    mainWindow = createWindow();
    return;
  }
  if (plan.hide) {
    console.log("[desktop] 快捷键：隐藏主窗口");
    mainWindow.hide();
    return;
  }
  showMainWindow("全局快捷键");
}

// ── 自动更新（骨架）──────────────────────────────────────────────────────────

/**
 * 只取元数据、只做决策、只写日志：**不下载、不安装、不接 electron-updater**。
 * 决策函数 decideUpdate 是纯函数（shell-policy.cjs），所有失败形态
 * （网络失败 / 元数据缺字段 / 签名摘要缺失或不对 / 版本号读不懂）都归成
 * action=unavailable + 可读原因，且 install 恒为 false——这两条都有单测钉住。
 *
 * 默认不碰网络：必须显式配 BLANK_UPDATE_FEED_URL 才检查一次。
 * ⚠️ 未验：真实 feed 的取回与解析没跑过（没有发布通道可指），拿到 feed 地址后要人工
 * 复核一次日志里的 action/reason，并确认桌面端依旧什么都不下载。
 */
async function checkForUpdates() {
  const feed = parseUpdateFeedUrl(process.env.BLANK_UPDATE_FEED_URL);
  if (!feed.ok) {
    console.log(`[desktop] 更新检查跳过：${feed.reason}`);
    return null;
  }
  const current = app.getVersion();
  let fetchError = "";
  let entry = null;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(feed.url, { signal: controller.signal });
      if (!response.ok) {
        fetchError = `HTTP ${response.status}`;
      } else {
        const entries = extractFeedEntries(await response.json());
        if (!entries) {
          fetchError = "feed JSON 既不是条目数组也不是带 version 的单个条目";
        } else {
          entry = pickLatestEntry(entries);
          if (!entry) fetchError = "feed 里没有任何版本可解析的条目";
        }
      }
    } finally {
      clearTimeout(timer);
    }
  } catch (error) {
    fetchError = error?.name === "AbortError" ? "请求超时（8 秒）" : String(error?.message ?? error);
  }
  const decision = decideUpdate({ current, entry, fetchError });
  shellState.update = decision;
  console.log(`[desktop] 更新决策 action=${decision.action}｜${decision.reason}`);
  refreshTrayMenu();
  return decision;
}

// ── 生命周期 ────────────────────────────────────────────────────────────────

/**
 * 第二实例：放弃启动的一侧会调 app.quit()，但 whenReady 不保证因此不 resolve，
 * 这里再挡一道——绝不能出现两个实例各 spawn 一个后端、各记一次 GPU 启动标记。
 * 收到 signal 的一侧按 planSecondInstanceReveal 把已有窗口唤回：
 * 最小化要 restore、藏进托盘要 show，最后 focus（这三态都由单测钉住）。
 */
app.on("second-instance", () => {
  if (!gotSingleInstanceLock) return;
  // 主实例还没 ready（正在跑迁移/拉后端）时窗口尚不存在，此刻 new BrowserWindow 会抛。
  // 唤回没损失：ready 之后 whenReady 流程自己会把窗口开出来。
  if (!app.isReady()) {
    console.log("[desktop] 收到第二实例启动信号（主实例尚未就绪，窗口稍后自会打开）");
    return;
  }
  console.log("[desktop] 收到第二实例启动信号");
  showMainWindow("第二实例唤回");
});

app.whenReady().then(async () => {
  if (!gotSingleInstanceLock) return;
  registerAppProtocol();
  console.log(`[desktop] 静态根目录 ${STATIC_ROOT}`);
  console.log(`[desktop] 后端源码目录 ${API_DIR}`);
  console.log(`[desktop] 数据目录 ${DATA_DIR}`);

  // 托盘先建：后端首启可能要跑迁移（最长 30 秒），这期间图标就得在任务栏待着。
  createTray();

  const started = await ensureBackend();
  console.log(`[desktop] 后端可达性 ${started.ok ? "正常" : "不可达"}`);

  mainWindow = createWindow();
  const win = mainWindow;

  if (started.ok) {
    win.webContents.once("did-finish-load", () => {
      void maybeScreenshot(win);
    });
  } else {
    win.webContents.once("did-finish-load", () => {
      void reportBackendFailure(win, started.hint ?? "未知原因");
    });
  }

  registerGlobalShortcut();
  // 更新检查只在配了 BLANK_UPDATE_FEED_URL 时真的发请求；不下载、不安装。
  void checkForUpdates();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) showMainWindow("activate");
    else mainWindow?.show();
  });
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  // 任何 app.quit()（截图自检、失败对话框、系统关机）都会先走到这里。
  // 置位之后窗口 close 不再进托盘——否则退出会永远停在「隐藏最后一个窗口」上。
  shellState.explicitQuit = true;
  stopBackend();
});

app.on("will-quit", () => {
  try {
    globalShortcut.unregisterAll();
  } catch (error) {
    console.error(`[desktop] 注销全局快捷键失败（不影响退出）：${error.message}`);
  }
});

process.on("exit", stopBackend);
