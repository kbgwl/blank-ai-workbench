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
import { mkdtempSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
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
// 点击/输入/按键之后先让页面喘一口气：真实页面的分析/聊天几乎都是异步请求，
// 模型写的 steps 很容易"点完立刻断言"；browser_check 不应该靠模型记得每一步
// 都补 wait。默认 600ms，可用 --settle 覆盖。
const SETTLE_MS = Math.max(0, Number(opt("settle", "600")));
const VIEWPORT = opt("viewport", "1280x800");
const EXPECT = argv.reduce((acc, item, i) => {
  if (item === "--expect") acc.push(argv[i + 1]);
  return acc;
}, []).filter(Boolean);
const EVAL = opt("eval", "");
// 动作序列：一个 JSON 文件，里面是一串「做什么 + 期望什么」。
//
// 为什么需要它：`--expect` 只能断言"页面上有这个东西"，而一个页面最容易骗过
// 这种断言——它只要**长得像**就够了。真正的验收必须走一遍：填、点、拖、刷新，
// 然后看状态有没有按预期变。区别在于断言的是**动作之后的状态**，不是静态结构。
const STEPS = opt("steps", "");
// 首屏快照：动作跑完之后页面已经不是"用户打开时看到的那一屏"了。
// 验收要的是**打开时**的样子，所以动作之前单独拍一张。
const SHOT_OPEN = opt("shot-open", "");

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

/** 在页面里跑一段表达式，把异常变成可读的错误（而不是让整个探针挂掉）。 */
async function pageEval(client, expression, awaitPromise = true) {
  const res = await client.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise,
    userGesture: true,
  });
  if (res.exceptionDetails) {
    const detail = res.exceptionDetails;
    throw new Error(detail.exception?.description || detail.text || "页面脚本抛异常");
  }
  return res.result?.value;
}

/** 等页面真的就绪（刷新之后 document.readyState 会先回到 loading）。 */
async function waitReady(client, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const state = await pageEval(client, "document.readyState", false);
      if (state === "complete") {
        await sleep(120);
        return;
      }
    } catch {
      // 导航进行中，取不到就再等一轮
    }
    await sleep(80);
  }
  throw new Error("页面没有回到就绪状态");
}

async function centerOf(client, selector) {
  const box = await pageEval(
    client,
    `(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      // 长页面上的按钮常常在首屏之外；不先滚进视口，CDP 的点击坐标会落在
      // 视口外或错误元素上，表现为“动作通过、断言仍为 0”。先滚到中间再取坐标。
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    })()`,
  );
  if (!box) throw new Error(`找不到元素：${selector}`);
  return box;
}

async function clickAt(client, x, y) {
  await client.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
  await client.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await client.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
}

/**
 * 拖放只能靠合成事件：CDP 的真实拖拽需要 Input.dispatchDragEvent 加一份
 * DragData，而页面里走的往往是 HTML5 的 dragstart/dragover/drop 那一套。
 * 这里按浏览器自己派发的方式补一遍，验的是"处理链通不通"，不是"鼠标好不好用"。
 */
async function dragBetween(client, from, to) {
  return pageEval(
    client,
    `(() => {
      const src = document.querySelector(${JSON.stringify(from)});
      const dst = document.querySelector(${JSON.stringify(to)});
      if (!src) return "找不到拖拽源：" + ${JSON.stringify(from)};
      if (!dst) return "找不到落点：" + ${JSON.stringify(to)};
      const dt = new DataTransfer();
      const fire = (el, type) =>
        el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
      fire(src, "dragstart");
      fire(dst, "dragenter");
      fire(dst, "dragover");
      fire(dst, "drop");
      fire(src, "dragend");
      return "";
    })()`,
  );
}

/** 依次执行动作；每步都留一条记录，失败不中断——一次跑完才能看到全貌。 */
async function runSteps(client, steps) {
  const records = [];
  for (let i = 0; i < steps.length; i += 1) {
    const step = steps[i] || {};
    const action = String(step.do || "").trim();
    const rec = { index: i + 1, do: action, text: String(step.text || ""), ok: true, actual: null };
    try {
      if (action === "assert") {
        const min = step.min === undefined ? null : Number(step.min);
        // 与工具 schema 对齐：assert 也支持 {selector, min} 统计匹配元素数量。
        // 之前只读 expr，模型写 selector+min 时永远按 false 处理，误报整页失败。
        if (step.selector && !step.expr) {
          const count = await pageEval(
            client,
            `document.querySelectorAll(${JSON.stringify(String(step.selector))}).length`,
          );
          const target = min === null ? 1 : min;
          rec.ok = Number(count) >= target;
          rec.actual = count;
        } else {
          const value = await pageEval(client, String(step.expr || "false"));
          rec.ok = min === null ? Boolean(value) : Number(value) >= min;
          rec.actual = value;
        }
      } else if (action === "eval") {
        // 只跑不留判：用来记基准值（window.__x = ...），后面的断言就能写成
        // "比刚才多 1 张"而不是"等于 4 张"——绝对数字会把用例焊死在初始状态上。
        rec.actual = await pageEval(client, String(step.expr || "undefined"));
      } else if (action === "click") {
        const at = await centerOf(client, String(step.selector || ""));
        await clickAt(client, at.x, at.y);
        if (SETTLE_MS) await sleep(SETTLE_MS);
        rec.actual = "已点击";
      } else if (action === "type") {
        const at = await centerOf(client, String(step.selector || ""));
        await clickAt(client, at.x, at.y);
        await client.send("Input.insertText", { text: String(step.value ?? "") });
        if (SETTLE_MS) await sleep(SETTLE_MS);
        rec.actual = String(step.value ?? "");
      } else if (action === "press") {
        const key = String(step.key || "Enter");
        const code = key === "Enter" ? 13 : key === "Tab" ? 9 : key === "Escape" ? 27 : 0;
        await client.send("Input.dispatchKeyEvent", { type: "keyDown", key, code: key, windowsVirtualKeyCode: code });
        await client.send("Input.dispatchKeyEvent", { type: "keyUp", key, code: key, windowsVirtualKeyCode: code });
        if (SETTLE_MS) await sleep(SETTLE_MS);
        rec.actual = key;
      } else if (action === "drag") {
        const problem = await dragBetween(client, String(step.from || ""), String(step.to || ""));
        rec.ok = !problem;
        if (SETTLE_MS) await sleep(SETTLE_MS);
        rec.actual = problem || "已拖放";
      } else if (action === "reload") {
        await client.send("Page.reload", {});
        await waitReady(client, TIMEOUT_MS);
        rec.actual = "已刷新";
      } else if (action === "wait") {
        const ms = Number(step.ms || 300);
        await sleep(ms);
        rec.actual = `等了 ${ms}ms`;
      } else {
        rec.ok = false;
        rec.actual = `不认识的步骤：${action}`;
      }
    } catch (error) {
      rec.ok = false;
      rec.actual = String(error?.message || error);
    }
    records.push(rec);
  }
  return records;
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
  const result = { ok: false, url: URL_ARG, title: "", console: [], errors: [], asserts: [], steps: [], shots: {} };
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

    if (SHOT_OPEN) {
      const png = await client.send("Page.captureScreenshot", { format: "png" });
      writeFileSync(SHOT_OPEN, Buffer.from(png.data, "base64"));
      result.shots.open = SHOT_OPEN;
    }

    if (STEPS) {
      const raw = JSON.parse(readFileSync(STEPS, "utf8"));
      const list = Array.isArray(raw) ? raw : raw.steps || [];
      result.steps = await runSteps(client, list);
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
    const stepsFailed = (result.steps || []).filter((item) => !item.ok);
    result.ok = hard.length === 0 && assertsFailed.length === 0 && stepsFailed.length === 0;
    result.error_count = hard.length;
    result.assert_failed = assertsFailed.length;
    result.step_failed = stepsFailed.length;
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
