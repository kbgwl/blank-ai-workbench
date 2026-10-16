/**
 * 空白格 AI 操作台 · 桌面壳主进程
 *
 * 职责边界（当前阶段）：把静态导出的前端装进一个真实窗口。
 * 后端进程守护、托盘、全局快捷键、单实例、自动更新属于后续阶段。
 *
 * 用 CommonJS 而不是 ESM：Electron 的 ESM 主进程在本机拿不到
 * `import { BrowserWindow } from "electron"` 的具名导出，且 sandbox 模式下
 * preload 只支持 CommonJS。CJS 是这两处都稳的选择。
 */
const { app, BrowserWindow, protocol, shell } = require("electron");
const { existsSync } = require("node:fs");
const { readFile, writeFile } = require("node:fs/promises");
const path = require("node:path");
const process = require("node:process");

/** 打包后静态站随 extraResources 落到 resources/web；开发态直接读 web/out。 */
const STATIC_ROOT = app.isPackaged
  ? path.join(process.resourcesPath, "web")
  : path.resolve(__dirname, "..", "web", "out");

const APP_ORIGIN = "app://bundle";
const BACKEND_HEALTH = process.env.BLANK_BACKEND_HEALTH ?? "http://127.0.0.1:8000/api/v1/health";

/**
 * 无 GPU 的环境（CI、无显卡的远程会话、部分虚拟化环境）里 GPU 进程会反复崩溃，
 * 最终 `GPU process isn't usable. Goodbye.` 直接终止应用。此时改用软件渲染。
 * 正常桌面环境不要开这个开关，硬件加速对界面流畅度有意义。
 */
if (process.env.BLANK_DISABLE_GPU === "1") {
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
  if (process.env.BLANK_NO_SANDBOX === "1") {
    app.commandLine.appendSwitch("no-sandbox");
  }
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

async function backendReachable() {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    const response = await fetch(BACKEND_HEALTH, { signal: controller.signal });
    clearTimeout(timer);
    return response.ok;
  } catch {
    return false;
  }
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
    win.show();
  });
  win.on("closed", () => console.log("[desktop] 窗口被关闭"));
  win.webContents.on("did-finish-load", () => console.log("[desktop] 页面加载完成"));
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("did-fail-load", (_event, code, desc, url) => {
    console.error(`[desktop] 页面加载失败 ${code} ${desc} ${url}`);
  });

  void win.loadURL(`${APP_ORIGIN}/index.html`);
  return win;
}

/** 等前端真正渲染出内容（客户端组件需要时间挂载），再截图，避免拍到空白。 */
async function waitForContent(win, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const length = await win.webContents
      .executeJavaScript("document.body ? document.body.innerText.trim().length : 0")
      .catch(() => 0);
    if (length > 40) return length;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return 0;
}

/** 自检用：BLANK_PROBE=1 时从渲染进程直连一次后端，把结果打进日志（排查跨源问题）。 */
async function probeBackendFromRenderer(win) {
  if (process.env.BLANK_PROBE !== "1") return;
  const base = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
  const result = await win.webContents
    .executeJavaScript(
      `fetch(${JSON.stringify(`${base}/api/v1/health`)})
         .then(r => "ok " + r.status)
         .catch(e => "fail " + e.message)`,
    )
    .catch((error) => `execute-failed ${error.message}`);
  console.log(`[desktop] 渲染进程直连后端 ${base} → ${result}`);
}

/** 自检用：设置 BLANK_SHOT=<png 路径> 后启动，渲染完自动截图并退出。 */
async function maybeScreenshot(win) {
  const target = process.env.BLANK_SHOT;
  if (!target) return;
  const length = await waitForContent(win);
  console.log(`[desktop] 渲染内容长度 ${length}`);
  await probeBackendFromRenderer(win);
  const image = await win.webContents.capturePage();
  await writeFile(target, image.toPNG());
  console.log(`[desktop] 截图已保存 ${target}`);
  app.quit();
}

app.whenReady().then(async () => {
  registerAppProtocol();
  console.log(`[desktop] 静态根目录 ${STATIC_ROOT}`);
  console.log(`[desktop] 后端可达性 ${(await backendReachable()) ? "正常" : "不可达（界面会给出错误态）"}`);

  const win = createWindow();
  win.webContents.once("did-finish-load", () => {
    void maybeScreenshot(win);
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
