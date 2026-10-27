"use strict";

/**
 * 桌面壳主进程的接线装置（单实例 / 托盘 / 全局快捷键 / 更新骨架）。
 *
 * 为什么不是「再补几条纯函数用例」：shell-policy.test.cjs 已经钉住了判定，
 * 但 main.cjs 里「把 Electron 的状态递给判定、再执行返回的动作」这一段从来没被跑过——
 * 注释里四处写着「本机不许启动壳，未验」。那些没验的正是接线：close 有没有真
 * preventDefault、second-instance 有没有真 show/focus、快捷键回调是不是真挂上了、
 * 更新骨架会不会偷偷下载。
 *
 * 办法：把 `electron` 模块换成内存里的假实现（窗口/托盘/快捷键/生命周期都是可观测的
 * 桩），用**真 HTTP**（127.0.0.1 随机端口）当健康检查与更新源，然后 require 真的 main.cjs。
 * 全程不建 GUI、不碰 :8000/:3000、不起后端进程——启动路径上的「后端已可达」由本地
 * 健康桩满足，main.cjs 自己会走「复用已有后端」这一支。
 *
 * 变异约定：BLANK_MAIN_OVERRIDE 指向 desktop/ 下的副本（相对 require 才解析得到），
 * 变异电池用它证明每条判据都能失败。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const MAIN = path.resolve(__dirname, "..", "main.cjs");
const MAIN_OVERRIDE = process.env.BLANK_MAIN_OVERRIDE
  ? path.resolve(process.env.BLANK_MAIN_OVERRIDE)
  : null;

const MANAGED_ENV = [
  "BLANK_API_BASE",
  "BLANK_BACKEND_PORT",
  "BLANK_BACKEND_HEALTH",
  "BLANK_BACKEND_CMD",
  "BLANK_DISABLE_GPU",
  "BLANK_FORCE_GPU",
  "BLANK_HOTKEY",
  "BLANK_LOG_FILE",
  "BLANK_NO_SANDBOX",
  "BLANK_PROBE",
  "BLANK_SHOT",
  "BLANK_START_PATH",
  "BLANK_TRAY",
  "BLANK_UPDATE_FEED_URL",
];

const SHA_64 = "a".repeat(64);

function buildElectronStub({ lock, trayMode, hotkeyMode, userData }) {
  const windows = [];
  const appHandlers = new Map();
  const trayRecords = { created: 0, menus: [], tooltips: [], handlers: {} };
  const shortcut = { registered: [], callback: null, unregisterAll: 0 };
  const dialogs = [];

  const app = {
    isPackaged: false,
    _ready: false,
    _quitCalled: false,
    commandLine: { appendSwitch() {} },
    requestSingleInstanceLock: () => lock,
    getPath: () => userData,
    getVersion: () => "0.8.5",
    disableHardwareAcceleration() {},
    isReady: () => app._ready,
    whenReady: () => {
      queueMicrotask(() => {
        app._ready = true;
      });
      return Promise.resolve();
    },
    quit() {
      if (app._quitCalled) return;
      app._quitCalled = true;
      app.emit("before-quit");
      app.emit("will-quit");
    },
    on(name, fn) {
      if (!appHandlers.has(name)) appHandlers.set(name, []);
      appHandlers.get(name).push(fn);
    },
    emit(name, ...args) {
      for (const fn of appHandlers.get(name) ?? []) fn(...args);
    },
  };

  class FakeBrowserWindow {
    constructor(options) {
      this.options = options;
      this.handlers = new Map();
      this.actions = [];
      this._visible = false;
      this._minimized = false;
      this._focused = false;
      this._destroyed = false;
      this._wcHandlers = new Map();
      this.webContents = {
        once: (name, fn) => this._onWc(name, fn),
        on: (name, fn) => this._onWc(name, fn),
        setWindowOpenHandler() {},
        executeJavaScript: async () => 0,
        capturePage: async () => ({ toPNG: () => Buffer.alloc(0) }),
      };
      windows.push(this);
      setTimeout(() => {
        this.emit("ready-to-show");
        this._emitWc("did-finish-load");
      }, 0);
    }

    static getAllWindows() {
      return windows.filter((win) => !win._destroyed);
    }

    static fromWebContents() {
      return null;
    }

    _onWc(name, fn) {
      if (!this._wcHandlers.has(name)) this._wcHandlers.set(name, []);
      this._wcHandlers.get(name).push(fn);
    }

    _emitWc(name, ...args) {
      for (const fn of this._wcHandlers.get(name) ?? []) fn(...args);
    }

    on(name, fn) {
      if (!this.handlers.has(name)) this.handlers.set(name, []);
      this.handlers.get(name).push(fn);
    }

    once(name, fn) {
      let fired = false;
      this.on(name, (...args) => {
        if (fired) return;
        fired = true;
        fn(...args);
      });
    }

    emit(name, ...args) {
      for (const fn of this.handlers.get(name) ?? []) fn(...args);
    }

    loadURL(url) {
      this.loadedUrl = url;
    }

    isDestroyed() {
      return this._destroyed;
    }

    isMinimized() {
      return this._minimized;
    }

    isVisible() {
      return this._visible;
    }

    isFocused() {
      return this._focused;
    }

    show() {
      this._visible = true;
      this._focused = true;
      this.actions.push("show");
    }

    hide() {
      this._visible = false;
      this._focused = false;
      this.actions.push("hide");
    }

    focus() {
      this._focused = true;
      this.actions.push("focus");
    }

    restore() {
      this._minimized = false;
      this._visible = true;
      this.actions.push("restore");
    }

    reload() {
      this.actions.push("reload");
    }

    /** 模拟用户按 ✕（或 app.quit 收窗口）：close 未被拦就继续 closed / window-all-closed。 */
    close() {
      const event = {
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
      };
      this.emit("close", event);
      if (!event.defaultPrevented) {
        this._destroyed = true;
        this.emit("closed");
        if (FakeBrowserWindow.getAllWindows().length === 0) app.emit("window-all-closed");
      }
      return event;
    }
  }

  class FakeTray {
    constructor(image) {
      if (trayMode === "throw") throw new Error("tray boom");
      trayRecords.created += 1;
      this.image = image;
      this.handlers = {};
    }

    setToolTip(text) {
      trayRecords.tooltips.push(text);
    }

    setContextMenu(menu) {
      trayRecords.menus.push(menu);
    }

    on(name, fn) {
      this.handlers[name] = fn;
      trayRecords.handlers[name] = fn;
    }
  }

  const electron = {
    app,
    BrowserWindow: FakeBrowserWindow,
    Menu: { buildFromTemplate: (template) => template },
    Tray: FakeTray,
    nativeImage: {
      createFromPath: () => ({ isEmpty: () => trayMode === "empty" }),
    },
    globalShortcut: {
      register(accelerator, callback) {
        shortcut.registered.push(accelerator);
        if (hotkeyMode === "occupied") return false;
        if (hotkeyMode === "throw") throw new Error("register boom");
        shortcut.callback = callback;
        return true;
      },
      unregisterAll() {
        shortcut.unregisterAll += 1;
      },
    },
    dialog: {
      showOpenDialog: async () => ({ canceled: true, filePaths: [] }),
      showMessageBox: async (...args) => {
        dialogs.push(args);
        return { response: 2 };
      },
    },
    shell: { openPath: async () => "", openExternal: async () => {} },
    ipcMain: { on() {}, handle() {} },
    protocol: { registerSchemesAsPrivileged() {}, handle() {} },
    clipboard: { writeText() {} },
  };

  return { electron, windows, app, trayRecords, shortcut, dialogs };
}

async function waitFor(predicate, label, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`等待超时：${label}`);
}

async function bootShell(options = {}) {
  const {
    lock = true,
    tray = "ok",
    hotkey = "ok",
    update = null,
    hotkeyValue = "ctrl+alt+b",
  } = options;

  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "blank-shell-"));
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    const send = (status, body) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(body);
    };
    if (request.url === "/health") return send(200, '{"status":"connected"}');
    if (request.url === "/feed500") return send(500, '{"error":"boom"}');
    if (request.url === "/feed") {
      if (update === "500") return send(500, '{"error":"boom"}');
      if (update === "garbage") return send(200, '{"foo":1}');
      if (update === "same") return send(200, JSON.stringify([{ version: "0.8.5" }]));
      return send(
        200,
        JSON.stringify([
          {
            version: "9.9.9",
            url: `http://127.0.0.1:${server.address().port}/package.exe`,
            sha256: SHA_64,
          },
        ]),
      );
    }
    if (request.url.startsWith("/package")) return send(200, '{"binary":true}');
    return send(404, "{}");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  const envValues = {
    BLANK_API_BASE: origin,
    BLANK_BACKEND_PORT: "",
    BLANK_BACKEND_HEALTH: `${origin}/health`,
    BLANK_BACKEND_CMD: "",
    BLANK_DISABLE_GPU: "1",
    BLANK_FORCE_GPU: "",
    BLANK_HOTKEY: hotkeyValue,
    BLANK_LOG_FILE: "",
    BLANK_NO_SANDBOX: "",
    BLANK_PROBE: "",
    BLANK_SHOT: "",
    BLANK_START_PATH: "",
    BLANK_TRAY: tray === "disabled" ? "0" : "",
    BLANK_UPDATE_FEED_URL: update === null ? "" : `${origin}/feed`,
  };
  const savedEnv = new Map();
  for (const key of MANAGED_ENV) {
    savedEnv.set(key, process.env[key]);
    if (envValues[key] === "") delete process.env[key];
    else process.env[key] = envValues[key];
  }

  const logs = [];
  const originalLog = console.log;
  const originalError = console.error;
  const capture = (...args) => logs.push(args.map(String).join(" "));
  console.log = capture;
  console.error = capture;

  const exitListenersBefore = new Set(process.listeners("exit"));
  const stub = buildElectronStub({ lock, trayMode: tray, hotkeyMode: hotkey, userData });
  const originalLoad = Module._load;
  Module._load = function patched(request, parent, isMain) {
    if (request === "electron") return stub.electron;
    return originalLoad.call(this, request, parent, isMain);
  };
  let requireError = null;
  try {
    delete require.cache[MAIN];
    if (MAIN_OVERRIDE) delete require.cache[MAIN_OVERRIDE];
    require(MAIN_OVERRIDE || MAIN);
  } catch (error) {
    requireError = error;
  } finally {
    Module._load = originalLoad;
  }
  const exitListenersAdded = process
    .listeners("exit")
    .filter((fn) => !exitListenersBefore.has(fn));

  const summary = () => logs.slice(-25).join("\n");
  if (requireError) {
    throw new Error(`main.cjs 加载失败：${requireError.stack}\n最近日志：\n${summary()}`);
  }

  try {
    if (!lock) {
      await waitFor(
        () => logs.some((line) => line.includes("已有实例在运行")),
        "第二实例让位日志",
      );
    } else {
      await waitFor(
        () =>
          logs.some(
            (line) => line.includes("更新决策") || line.includes("更新检查跳过"),
          ),
        "whenReady 走完（更新检查已给出结论）",
      );
    }
  } catch (error) {
    await cleanup();
    throw new Error(`${error.message}\n最近日志：\n${summary()}`);
  }

  const state = {
    logs,
    requests,
    stub,
    windows: stub.windows,
    app: stub.app,
    tray: stub.trayRecords,
    shortcut: stub.shortcut,
    get win() {
      return stub.windows[0] ?? null;
    },
    latestTrayMenu() {
      return stub.trayRecords.menus[stub.trayRecords.menus.length - 1] ?? [];
    },
    trayLabels() {
      return this.latestTrayMenu().map((item) => item.label);
    },
    trayItem(id) {
      return this.latestTrayMenu().find((item) => item.id === id) ?? null;
    },
    logIncludes(text) {
      return logs.some((line) => line.includes(text));
    },
    cleanup,
  };

  async function cleanup() {
    try {
      await new Promise((resolve) => server.close(resolve));
    } catch {
      /* 已关闭 */
    }
    for (const key of MANAGED_ENV) {
      const value = savedEnv.get(key);
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    for (const fn of exitListenersAdded) process.removeListener("exit", fn);
    console.log = originalLog;
    console.error = originalError;
    delete require.cache[MAIN];
    fs.rmSync(userData, { recursive: true, force: true });
  }

  return state;
}

test("单实例：拿不到锁的第二实例在建窗口/建托盘/探后端之前就退出", async () => {
  const shell = await bootShell({ lock: false });
  try {
    assert.equal(shell.app._quitCalled, true, "第二实例必须自己退出");
    assert.equal(shell.windows.length, 0, "第二实例不许建窗口");
    assert.equal(shell.tray.created, 0, "第二实例不许建托盘");
    assert.equal(
      shell.requests.filter((url) => url === "/health").length,
      0,
      "第二实例不许探后端（更不能 spawn）",
    );
  } finally {
    await shell.cleanup();
  }
});

test("启动接线：复用已有后端、托盘就绪、更新骨架只取元数据不下载", async () => {
  const shell = await bootShell({ update: "available" });
  try {
    assert.equal(shell.windows.length, 1, "主实例要建一个窗口");
    assert.ok(shell.win.loadedUrl.startsWith("app://bundle/"), "静态站必须走 app:// 协议");
    assert.ok(shell.logIncludes("检测到已有后端在运行，直接复用"), "健康桩可达时应复用而不是 spawn");
    assert.equal(shell.tray.created, 1, "托盘要真的建出来");
    assert.ok(shell.trayLabels().includes("后端状态：运行中"), "托盘菜单要反映后端在线");
    assert.ok(
      shell.trayLabels().some((label) => label.includes("更新：发现新版本 9.9.9")),
      `托盘菜单应显示更新决策，实际：${JSON.stringify(shell.trayLabels())}`,
    );
    assert.equal(
      shell.requests.filter((url) => url === "/feed").length,
      1,
      "更新源只该被取一次元数据",
    );
    assert.equal(
      shell.requests.filter((url) => url.startsWith("/package")).length,
      0,
      "更新骨架不许下载安装包（install 恒为 false）",
    );
    assert.ok(shell.logIncludes("更新决策 action=available"));
  } finally {
    await shell.cleanup();
  }
});

test("第二实例信号：藏进托盘的窗口被 show + focus 唤回", async () => {
  const shell = await bootShell();
  try {
    const win = shell.win;
    win.hide();
    assert.equal(win.isVisible(), false);
    const actionsBefore = win.actions.length;
    shell.app.emit("second-instance");
    const actions = win.actions.slice(actionsBefore);
    assert.ok(actions.includes("show"), `隐藏态必须 show，实际动作 ${JSON.stringify(actions)}`);
    assert.ok(actions.includes("focus"), "唤回后要聚焦");
    assert.equal(win.isVisible(), true);
    assert.equal(shell.windows.length, 1, "唤回不该再建第二个窗口");

    // 托盘双击走的是同一条唤回路径，一并验掉，免得只有菜单「显示」那一支被钉住。
    win.hide();
    const beforeDoubleClick = win.actions.length;
    shell.tray.handlers["double-click"]();
    assert.ok(win.actions.slice(beforeDoubleClick).includes("show"));
  } finally {
    await shell.cleanup();
  }
});

test("关窗路由：托盘可用时藏进托盘，托盘菜单「退出」后放行", async () => {
  const shell = await bootShell();
  try {
    const win = shell.win;
    const hidden = win.close();
    assert.equal(hidden.defaultPrevented, true, "托盘可用时 ✕ 必须被拦下");
    assert.equal(win.isVisible(), false, "拦下之后要藏进托盘");
    assert.equal(shell.app._quitCalled, false, "关窗不等于退出");

    const quit = shell.trayItem("quit");
    assert.ok(quit && typeof quit.click === "function", "托盘菜单必须有「退出」");
    quit.click();
    assert.equal(shell.app._quitCalled, true, "托盘「退出」必须真的退出");

    const allowed = win.close();
    assert.equal(allowed.defaultPrevented, false, "显式退出后 ✕ 必须放行，不能再被托盘吞掉");
  } finally {
    await shell.cleanup();
  }
});

test("托盘不可用：关窗退回退出，不做看不见又关不掉的窗口", async () => {
  const shell = await bootShell({ tray: "empty" });
  try {
    assert.equal(shell.tray.created, 0);
    assert.ok(shell.logIncludes("托盘创建失败"), "要留下可诊断的失败日志");
    const event = shell.win.close();
    assert.equal(event.defaultPrevented, false, "没有托盘时关窗必须放行");
    assert.equal(shell.app._quitCalled, true, "没有托盘时关窗就是退出");
  } finally {
    await shell.cleanup();
  }
});

test("全局快捷键：注册成功时真能唤出/隐藏，被占用与抛异常都降级不崩", async () => {
  const ok = await bootShell({ hotkey: "ok" });
  try {
    assert.deepEqual(ok.shortcut.registered, ["Control+Alt+B"]);
    assert.equal(typeof ok.shortcut.callback, "function", "注册成功时回调必须真挂上");
    const win = ok.win;
    assert.equal(win.isVisible(), true);
    ok.shortcut.callback();
    assert.equal(win.isVisible(), false, "可见且聚焦时按组合键应隐藏");
    ok.shortcut.callback();
    assert.equal(win.isVisible(), true, "隐藏后再按应唤回");
  } finally {
    await ok.cleanup();
  }

  const occupied = await bootShell({ hotkey: "occupied" });
  try {
    assert.equal(occupied.windows.length, 1, "被占用也必须照常开窗口");
    assert.ok(occupied.logIncludes("已被其他程序占用"));
    assert.equal(occupied.shortcut.callback, null, "被占用时不许假装注册成功");
    occupied.app.quit();
    assert.equal(occupied.shortcut.unregisterAll, 1, "退出时要注销（哪怕没注册成功）");
  } finally {
    await occupied.cleanup();
  }

  const thrown = await bootShell({ hotkey: "throw" });
  try {
    assert.equal(thrown.windows.length, 1, "register 抛异常也必须照常开窗口");
    assert.ok(thrown.logIncludes("注册抛出异常"));
    assert.equal(thrown.shortcut.callback, null);
  } finally {
    await thrown.cleanup();
  }
});

test("更新骨架的失败形态：500 / 版本读不懂 / 同版本 / 未配置都不安装", async () => {
  const cases = [
    { update: "500", expect: "更新：不可用（原因见日志）", log: "更新源请求失败" },
    { update: "garbage", expect: "更新：不可用（原因见日志）", log: "feed JSON 既不是条目数组" },
    { update: "same", expect: "更新：已是最新", log: "无需更新" },
  ];
  for (const item of cases) {
    const shell = await bootShell({ update: item.update });
    try {
      assert.ok(
        shell.trayLabels().includes(item.expect),
        `${item.update} 应显示「${item.expect}」，实际 ${JSON.stringify(shell.trayLabels())}`,
      );
      assert.ok(shell.logIncludes(item.log), `${item.update} 的日志应包含「${item.log}」`);
      assert.equal(
        shell.requests.filter((url) => url.startsWith("/package")).length,
        0,
        `${item.update} 也不许下载`,
      );
    } finally {
      await shell.cleanup();
    }
  }

  const off = await bootShell();
  try {
    assert.ok(off.logIncludes("更新检查跳过"), "未配置更新源时根本不许发请求");
    assert.equal(off.requests.filter((url) => url === "/feed").length, 0);
    assert.ok(
      off.trayLabels().every((label) => typeof label !== "string" || !label.startsWith("更新：")),
      "没查过就不该在托盘里编一行更新状态",
    );
  } finally {
    await off.cleanup();
  }
});
