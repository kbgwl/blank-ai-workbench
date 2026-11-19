// 浏览器探针：打开一个页面，收集控制台错误，按需断言，落一张截图。
//
// 为什么不引 Playwright：我们只要四个动作（打开、收错、截图、断言），而
// Playwright 会往安装包里塞一份浏览器、并带来一层和版本绑定的黑盒。这里用
// **本机已有的 Chromium**（Chrome / Edge / 桌面壳自带的那份）加 CDP 直连，
// Node 22+ 内置 WebSocket，零依赖。
//
// 输出永远是**一行 JSON**（stdout 最后一行），调用方（Python 工具）只认它：
//   { ok, url, title, console: [...], errors: [...], asserts: [...], shots: {...} }
// 调试信息走 stderr，免得污染那行 JSON。
//
// 用法：
//   node browser_probe.mjs --url file:///... --shot out.png --expect "canvas:1" --wait 800

import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";

const argv = process.argv.slice(2);
const opt = (name, fallback = "") => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? argv[i + 1] : fallback;
};
const flag = (name) => argv.includes(`--${name}`);

const URL_ARG = opt("url");
const SHOT = opt("shot");
const WAIT_MS = Number(opt("wait", "800"));
const TIMEOUT_MS = Number(opt("timeout", "20000"));
const VIEWPORT = opt("viewport", "1280x800");
const EXPECT = argv.reduce((acc, item, i) => {
  if (item === "--expect") acc.push(argv[i + 1]);
  return acc;
}, []).filter(Boolean);
const EVAL = opt("eval", "");

const [VW, VH] = VIEWPORT.split("x").map((n) => Number(n) || 0);

/** 本机可用的 Chromium：Chrome → Edge → 环境变量显式指定。 */
function findBrowser() {
  const explicit = process.env.BLANK_CHROME || "";
  const candidates = [
    explicit,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    join(process.env.LOCALAPPDATA || "", "Google/Chrome/Application/chrome.exe"),
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ];
  return candidates.find((path) => path && existsSync(path)) || "";
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function jsonList(port, tries = 60) {
  for (let i = 0; i < tries; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (res.ok) return await res.json();
    } catch {
      // 还没起来
    }
    await sleep(150);
  }
  throw new Error("浏览器调试端口没有就绪");
}

function cdp(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    let seq = 0;
    const pending = new Map();
    const listeners = [];
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && pending.has(msg.id)) {
        const { resolve: res, reject: rej } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) rej(new Error(msg.error.message));
        else res(msg.result);
      } else if (msg.method) {
        for (const fn of listeners) fn(msg);
      }
    });
    ws.addEventListener("open", () =>
      resolve({
        send: (method, params = {}) =>
          new Promise((res, rej) => {
            const id = ++seq;
            pending.set(id, { resolve: res, reject: rej });
            ws.send(JSON.stringify({ id, method, params }));
          }),
        on: (fn) => listeners.push(fn),
        close: () => ws.close(),
      }),
    );
    ws.addEventListener("error", () => reject(new Error("CDP 连接失败")));
  });
}

/** "canvas:1" → 至少 1 个 canvas；"#app" → 存在；"div.card:3" → 至少 3 个。 */
function parseExpect(spec) {
  const cut = spec.lastIndexOf(":");
  if (cut > 0 && /^\d+$/.test(spec.slice(cut + 1))) {
    return { selector: spec.slice(0, cut), min: Number(spec.slice(cut + 1)) };
  }
  return { selector: spec, min: 1 };
}

async function main() {
  if (!URL_ARG) throw new Error("缺少 --url");
  const browser = findBrowser();
  if (!browser) throw new Error("没有找到可用的 Chrome/Edge；用 BLANK_CHROME 指定一个");

  const port = await freePort();
  const profile = mkdtempSync(join(tmpdir(), "blank-probe-"));
  const child = spawn(
    browser,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-background-networking",
      "--hide-scrollbars",
      "--mute-audio",
      "--disable-blink-features=AutomationControlled",
      `--user-data-dir=${profile}`,
      `--window-size=${VW || 1280},${VH || 800}`,
      `--remote-debugging-port=${port}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"], windowsHide: true },
  );
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));

  const started = Date.now();
  const result = { ok: false, url: URL_ARG, title: "", console: [], errors: [], asserts: [], shots: {} };
  let client = null;

  try {
    const targets = await jsonList(port);
    const page = targets.find((item) => item.type === "page");
    if (!page) throw new Error("没有找到可用的页面目标");
    client = await cdp(page.webSocketDebuggerUrl);

    const consoleItems = [];
    const failures = [];
    const push = (level, text, where = "") => {
      if (consoleItems.length >= 50) return;
      consoleItems.push({ level, text: String(text).slice(0, 500), where });
    };

    client.on((msg) => {
      const p = msg.params || {};
      if (msg.method === "Runtime.consoleAPICalled") {
        const level = String(p.type || "");
        if (level !== "error" && level !== "warning") return;
        const text = (p.args || [])
          .map((a) => (a.value !== undefined ? a.value : a.description || a.type))
          .join(" ");
        push(level, text, "console");
      } else if (msg.method === "Runtime.exceptionThrown") {
        const d = p.exceptionDetails || {};
        const text = d.exception?.description || d.text || "未捕获异常";
        push("error", text, "exception");
      } else if (msg.method === "Log.entryAdded") {
        const entry = p.entry || {};
        if (entry.level !== "error") return;
        push("error", `${entry.text || ""} ${entry.url || ""}`, entry.source || "log");
      } else if (msg.method === "Network.loadingFailed") {
        if (p.canceled) return;
        failures.push({
          url: p.url || "",
          error: p.errorText || "",
          type: p.type || "",
        });
      } else if (msg.method === "Network.responseReceived") {
        const r = p.response || {};
        if (typeof r.status === "number" && r.status >= 400) {
          failures.push({ url: r.url || "", error: `HTTP ${r.status}`, type: "response" });
        }
      }
    });

    await client.send("Runtime.enable");
    await client.send("Log.enable");
    await client.send("Page.enable");
    await client.send("Network.enable");
    if (VW && VH) {
      await client.send("Emulation.setDeviceMetricsOverride", {
        width: VW,
        height: VH,
        deviceScaleFactor: 1,
        mobile: false,
      });
    }

    const loaded = new Promise((resolve) => {
      const timer = setTimeout(resolve, TIMEOUT_MS);
      client.on((msg) => {
        if (msg.method === "Page.loadEventFired") {
          clearTimeout(timer);
          resolve();
        }
      });
    });
    await client.send("Page.navigate", { url: URL_ARG });
    await loaded;
    // 一次网络静默：等页面把首屏要的资源拿完，再截图。等不到就按 WAIT_MS 收。
    await sleep(Math.max(200, WAIT_MS));

    const info = await client.send("Runtime.evaluate", {
      expression:
        "({title: document.title, ready: document.readyState, url: location.href, text: document.body ? document.body.innerText.slice(0, 4000) : ''})",
      returnByValue: true,
    });
    const page_ = info.result?.value || {};
    result.title = String(page_.title || "");
    result.ready = String(page_.ready || "");
    result.url = String(page_.url || URL_ARG);
    result.text = String(page_.text || "");

    for (const spec of EXPECT) {
      const { selector, min } = parseExpect(spec);
      const res = await client.send("Runtime.evaluate", {
        expression: `document.querySelectorAll(${JSON.stringify(selector)}).length`,
        returnByValue: true,
      });
      const count = Number(res.result?.value ?? 0);
      result.asserts.push({ selector, min, count, ok: count >= min });
    }

    if (EVAL) {
      const res = await client.send("Runtime.evaluate", {
        expression: EVAL,
        returnByValue: true,
        awaitPromise: true,
      });
      result.eval = res.result?.value ?? null;
      if (res.exceptionDetails) {
        push("error", res.exceptionDetails.exception?.description || "eval 抛出异常", "eval");
      }
    }

    if (SHOT) {
      const png = await client.send("Page.captureScreenshot", { format: "png" });
      const jpg = await client.send("Page.captureScreenshot", { format: "jpeg", quality: 72 });
      writeFileSync(SHOT, Buffer.from(png.data, "base64"));
      const jpegPath = `${SHOT}.jpg`;
      writeFileSync(jpegPath, Buffer.from(jpg.data, "base64"));
      result.shots = { png: SHOT, jpeg: jpegPath };
    }

    result.console = consoleItems;
    result.errors = failures.slice(0, 30);
    const hard = consoleItems.filter((item) => item.level === "error");
    const assertsFailed = result.asserts.filter((item) => !item.ok);
    result.ok = hard.length === 0 && assertsFailed.length === 0;
    result.error_count = hard.length;
    result.assert_failed = assertsFailed.length;
    result.duration_ms = Date.now() - started;
  } finally {
    try {
      client?.close();
    } catch {
      // 忽略
    }
    try {
      child.kill();
    } catch {
      // 忽略
    }
    await sleep(120);
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch {
      // 临时目录删不掉不影响结论
    }
  }

  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((error) => {
  process.stdout.write(`${JSON.stringify({ ok: false, fatal: String(error?.message || error) })}\n`);
  process.exitCode = 1;
});
