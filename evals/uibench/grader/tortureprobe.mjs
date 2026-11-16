#!/usr/bin/env node
/**
 * 事故指挥台（torture console）探针：静态服务器 + Chrome headless CDP。
 * 逐条对应题面 15 条验收：粘顶/固定条、[hidden]、aria-live 计数、排序、窗口化、
 * roving tabindex、抽屉焦点陷阱、撤销重做、批量三态与作用域、URL/localStorage、
 * 390px、reduced-motion、print、XSS、自测。
 *
 * 用法：node tortureprobe.mjs --dir <交付目录> --out <json> --shots <目录>
 * 退出码：任一判据失败则 1；JSON 始终完整落盘（含每条 detail 与截图名）。
 */
import http from "node:http";
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync, rmSync, statSync } from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const opt = (n, d) => {
  const i = args.indexOf(`--${n}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};
const DIR = path.resolve(opt("dir", ""));
const OUT = opt("out", "E:/tmp/productbench/tortureprobe.json");
const SHOTS = opt("shots", "E:/tmp/productbench/torture-shots");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT_HTTP = Number(opt("http-port", "18994"));
const PORT_CDP = Number(opt("cdp-port", "19224"));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

mkdirSync(SHOTS, { recursive: true });
const record = { dir: DIR, steps: [], console: [], requests: [] };
const step = (name, ok, detail) => {
  record.steps.push({ name, ok: Boolean(ok), detail: detail ?? null });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail !== null && detail !== undefined ? " :: " + JSON.stringify(detail).slice(0, 400) : ""}`);
};
const guard = async (name, fn) => {
  try {
    const r = await fn();
    if (typeof r === "object" && r && "ok" in r) step(name, r.ok, r.detail);
    else step(name, r);
  } catch (error) {
    step(name, false, { error: String(error && error.message ? error.message : error).slice(0, 300) });
  }
};

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
};
const server = http.createServer((req, res) => {
  const url = decodeURIComponent((req.url || "/").split("?")[0]);
  const rel = url === "/" ? "/index.html" : url;
  const file = path.join(DIR, rel);
  if (!file.startsWith(DIR) || !existsSync(file) || statSync(file).isDirectory()) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("404");
    return;
  }
  res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
  res.end(readFileSync(file));
});
await new Promise((r) => server.listen(PORT_HTTP, "127.0.0.1", r));
const BASE = `http://127.0.0.1:${PORT_HTTP}/`;

const profile = path.join("E:/tmp/productbench", `chrome-profile-torture-${Date.now()}`);
const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
    `--remote-debugging-port=${PORT_CDP}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { windowsHide: true, stdio: "ignore" }
);

let version;
for (let i = 0; i < 150; i++) {
  try {
    version = await (await fetch(`http://127.0.0.1:${PORT_CDP}/json/version`)).json();
    break;
  } catch {
    await wait(100);
  }
}
if (!version) throw new Error("chrome cdp 未就绪");

const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res, { once: true });
  ws.addEventListener("error", rej, { once: true });
});
let nextId = 1;
const pending = new Map();
const listeners = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
  }
  for (const l of listeners) l(msg);
});
const raw = (method, params = {}, sessionId) =>
  new Promise((res, rej) => {
    const id = nextId++;
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });

const { targetId } = await raw("Target.createTarget", { url: "about:blank" });
const { sessionId } = await raw("Target.attachToTarget", { targetId, flatten: true });
const cdp = (method, params) => raw(method, params, sessionId);

listeners.push((msg) => {
  if (msg.sessionId !== sessionId) return;
  if (msg.method === "Runtime.exceptionThrown") {
    record.console.push({ type: "exception", text: String(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text).slice(0, 500) });
  }
  if (msg.method === "Runtime.consoleAPICalled" && ["error", "assert"].includes(msg.params.type)) {
    record.console.push({ type: "console." + msg.params.type, text: String((msg.params.args || []).map((a) => a.value ?? a.description ?? "").join(" ")).slice(0, 500) });
  }
  if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
    // 浏览器自动请求 favicon 的 404 不算页面缺陷；其它资源 404 仍然记。
    const entry = msg.params.entry;
    if (entry.url && /\/favicon\.ico$/i.test(entry.url)) return;
    record.console.push({ type: "log.error", text: String(entry.text).slice(0, 500), url: entry.url || "" });
  }
  if (msg.method === "Network.requestWillBeSent") record.requests.push(msg.params.request.url);
});

await cdp("Page.enable");
await cdp("Runtime.enable");
await cdp("Log.enable");
await cdp("Network.enable");

const evaluate = async (expression) => {
  const r = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result?.value;
};
const goto = async (url, ms = 900) => {
  await cdp("Page.navigate", { url });
  await wait(ms);
};
const shot = async (name) => {
  try {
    const { data } = await cdp("Page.captureScreenshot", { format: "png" });
    const file = path.join(SHOTS, `${name}.png`);
    writeFileSync(file, Buffer.from(data, "base64"));
    record.steps.push({ name: `shot:${name}`, ok: true, detail: file });
  } catch (error) {
    record.steps.push({ name: `shot:${name}`, ok: false, detail: String(error) });
  }
};
const key = async (k, code, vk, modifiers = 0) => {
  const base = { key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers };
  await cdp("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
  await cdp("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  await wait(120);
};
const KEYS = {
  Tab: ["Tab", "Tab", 9],
  Enter: ["Enter", "Enter", 13],
  Escape: ["Escape", "Escape", 27],
  ArrowDown: ["ArrowDown", "ArrowDown", 40],
  ArrowUp: ["ArrowUp", "ArrowUp", 38],
};

/* 页面内通用助手：所有选择器只在页面里求值，避免 Node 侧字符串拼接出错。 */
const H = `
window.__p = {
  vis(el){ if(!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
    return s.display!=='none' && s.visibility!=='hidden' && r.width>1 && r.height>1; },
  text(el){ return ((el&&(el.innerText!==undefined?el.innerText:el.textContent))||'').replace(/\\s+/g,' ').trim(); },
  rows(){ return [...document.querySelectorAll('[data-row]')]; },
  header(){ const sels=['header','.topbar','#topbar']; for(const s of sels){ const e=document.querySelector(s); if(e&&this.vis(e)) return e; } return null; },
  toolbar(){ const cands=[...document.querySelectorAll('#toolbar,.toolbar,footer,nav,aside,div')].filter(e=>{
      const s=getComputedStyle(e); const r=e.getBoundingClientRect();
      return s.position==='fixed' && r.height>10 && r.height<220 && r.width>innerWidth*0.5 && r.bottom>innerHeight-30 && e.querySelector('button,select');
    });
    return cands[0]||null; },
  drawer(){ const sels=['#drawer','[role="dialog"]','.drawer','aside']; for(const s of sels){ for(const e of document.querySelectorAll(s)){ if(this.vis(e)) return e; } } return null; },
  search(){ return document.querySelector('input[type="search"]'); },
  filterBlock(){ const el=this.search()||document.querySelector('.filters,#filters'); if(!el) return null;
    let n=el; while(n && n.parentElement && !['MAIN','BODY'].includes(n.parentElement.tagName)) n=n.parentElement; return n; },
  liveText(){ return [...document.querySelectorAll('[aria-live],[role="status"],[role="alert"]')].map(e=>this.vis(e)?this.text(e):'').join(' | '); },
  liveNumber(){ const t=this.liveText(); const m=t.match(/(\\d+)\\s*条/); return m?Number(m[1]):null; },
  selectAll(){ const sels=['#select-all','thead input[type="checkbox"]','[role="columnheader"] input[type="checkbox"]','[role="row"] input[type="checkbox"]'];
    for(const s of sels){ const e=document.querySelector(s); if(e) return e; } return null; },
  rowCheckbox(r){ return r.querySelector('input[type="checkbox"]'); },
  hiddenAncestor(el){ for(let n=el;n&&n!==document.body;n=n.parentElement){ if(getComputedStyle(n).display==='none') return true; } return false; },
  seq(kind){ return this.rows().map(r=>{ const t=this.text(r);
    if(kind==='time'){ const m=t.match(/(\\d{4}-\\d{2}-\\d{2})[ T](\\d{2}:\\d{2})/); return m?m[1]+' '+m[2]:null; }
    const m=t.match(/\\bP([1-4])\\b/); return m?Number(m[1]):null; }); },
  evText(){ return this.text(document.querySelector('#selftest-result')); },
};
'ok'`;

/* 重置到干净视图：清掉 hash 与本地存储后重载。 */
const fresh = async () => {
  await goto(BASE + "?fresh=1");
  await evaluate("try{localStorage.clear()}catch(e){}; location.hash=''; 'cleared'");
  await goto(BASE, 1100);
};
const setSearch = async (value, ms = 600) => {
  await evaluate(`(()=>{const el=document.querySelector('input[type="search"]')||document.querySelector('#search'); if(!el) return 'no-search';
    const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
    d.set.call(el, ${JSON.stringify(value)});
    el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return 'ok';})()`);
  await wait(ms);
};
const clickSel = async (selector) => {
  const r = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)}); if(!el) return 'missing'; el.click(); return 'ok';})()`);
  if (r !== "ok") throw new Error(`找不到 ${selector}`);
};
const clickByText = async (rootSel, needle, tag = "button") => {
  const r = await evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(rootSel)}); if(!root) return 'no-root';
    const el=[...root.querySelectorAll(${JSON.stringify(tag)})].find(b=>window.__p.text(b).includes(${JSON.stringify(needle)}));
    if(!el) return 'no-el'; el.click(); return 'ok';})()`);
  return r;
};
/* 抽屉可能靠 Enter 或行点击打开；先按 Enter，再退到点击，避免把「打开方式不同」判成「没有抽屉」。 */
const ensureDrawer = async () => {
  if (await evaluate("!!window.__p.drawer()")) return { open: true, how: "already" };
  const id = await evaluate("(()=>{const r=window.__p.rows()[0]; if(!r) return null; r.focus(); return r.dataset.id||null;})()");
  await key(...KEYS.Enter);
  await wait(500);
  let open = await evaluate("!!window.__p.drawer()");
  if (!open) {
    await evaluate("(()=>{const r=window.__p.rows()[0]; if(r) r.click(); return 'ok';})()");
    await wait(500);
    open = await evaluate("!!window.__p.drawer()");
  }
  if (open) record.openedRowId = id;
  return { open, how: open ? "opened" : "failed", id };
};
const setSev = async (keep) => {
  const r = await evaluate(`(()=>{const want=${JSON.stringify(keep)};
    let boxes=[...document.querySelectorAll('input.sev, [data-sev], [data-severity], .sev-chips input[type="checkbox"], input[type=checkbox][value^=P]')];
    boxes=boxes.filter(b=>/^P[1-4]$/.test(b.value||''));
    if(boxes.length){
      for(const b of boxes){ const w=want.includes(b.value); if(b.checked!==w){ b.click(); } }
      return 'checks:'+boxes.map(b=>b.value+':'+b.checked).join(',');
    }
    const nodes=[...document.querySelectorAll('button,[role="button"],label')].filter(n=>/^P[1-4]$/.test(window.__p.text(n).trim()));
    if(!nodes.length) return 'missing';
    for(const n of nodes){ const v=window.__p.text(n).trim();
      const on=n.getAttribute('aria-pressed')==='true'||n.getAttribute('aria-checked')==='true'||n.classList.contains('is-active')||n.classList.contains('active');
      if(on!==want.includes(v)) n.click(); }
    return 'buttons:'+nodes.map(n=>window.__p.text(n).trim()).join(',');
  })()`);
  await wait(500);
  return r;
};
/* 「只看未解决」：id 优先，其次按标签文本含「未解决」的 checkbox / 按钮，避免绑死某一种实现。 */
const setUnresolved = async (on) => {
  const r = await evaluate(`(()=>{
    let el=document.querySelector('#only-open, #unresolved-only, #unresolved-toggle, #unresolved-filter');
    if(!el){
      for(const l of document.querySelectorAll('label')){
        if(window.__p.text(l).includes('未解决')){ const i=l.querySelector('input[type="checkbox"]'); if(i){ el=i; break; } }
      }
    }
    if(!el){
      for(const i of document.querySelectorAll('input[type="checkbox"]')){
        const l=i.closest('label'); if(l && window.__p.text(l).includes('未解决')){ el=i; break; }
      }
    }
    if(!el){
      el=[...document.querySelectorAll('button,[role="button"],[role="switch"],label')].find(n=>window.__p.text(n).includes('未解决'))||null;
      if(el && !(el.getAttribute('aria-pressed')||el.getAttribute('aria-checked')||el.classList.contains('is-active')||el.classList.contains('active')||el.querySelector('input'))) el=null;
    }
    if(!el) return 'missing';
    const box=el.type==='checkbox'?el:el.querySelector&&el.querySelector('input[type="checkbox"]');
    if(box){ if(box.checked!==${on}) box.click(); return 'check:'+(box.checked?'on':'off'); }
    const state=el.getAttribute('aria-pressed')==='true'||el.getAttribute('aria-checked')==='true'||el.classList.contains('is-active')||el.classList.contains('active');
    if(state!==${on}) el.click();
    return 'toggle:'+${on};
  })()`);
  await wait(400);
  return r;
};

/* 多个 live 区域时，只认「这次筛选变化后被更新」的那条播报，避免读到页面里写死的总数。
   preferIdx：沿用上一次认定成功的那个区域，避免提示条里的「N 条」抢答。 */
const nOf = (t) => {
  const m = (t || "").match(/(\d+)\s*条/);
  return m ? Number(m[1]) : null;
};
const liveAfterChange = async (action, preferIdx, strict = false) => {
  const before = (await evaluate("window.__p.liveText()")).split(" | ");
  await action();
  await wait(250);
  const after = (await evaluate("window.__p.liveText()")).split(" | ");
  if (Number.isInteger(preferIdx) && after[preferIdx] !== undefined && after[preferIdx] !== before[preferIdx]) {
    const n = nOf(after[preferIdx]);
    if (n !== null) return { n, changed: after[preferIdx].slice(0, 100), idx: preferIdx };
  }
  if (strict && Number.isInteger(preferIdx)) return { n: null, changed: null, idx: -1 };
  for (let i = 0; i < after.length; i++) {
    if (after[i] && after[i] !== before[i]) {
      const n = nOf(after[i]);
      if (n !== null) return { n, changed: after[i].slice(0, 100), idx: i };
    }
  }
  return { n: null, changed: null, idx: -1 };
};

/* ── 起步 ── */
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: H });
await goto(BASE, 1400);
await evaluate(H);
record.firstLoad = await evaluate("({rows: window.__p.rows().length, live: window.__p.liveText().slice(0,80)})");
await shot("desktop-initial");

/* 1. 交付文件与静态约束 */
await guard("1.1 三件套存在", async () => {
  const files = ["index.html", "styles.css", "app.js"].filter((f) => existsSync(path.join(DIR, f)));
  return { ok: files.length === 3, detail: { files } };
});
await guard("1.2 无外部资源/动态网络调用（静态扫描）", async () => {
  const scan = (f) => {
    const p = path.join(DIR, f);
    if (!existsSync(p)) return [];
    const s = readFileSync(p, "utf8");
    const hits = [];
    if (/(src|href)\s*=\s*["']https?:/i.test(s)) hits.push("external-attr");
    if (/url\(\s*['"]?https?:/i.test(s)) hits.push("external-url");
    if (/\bimport\s+.*from\s+['"]https?:/i.test(s)) hits.push("import-url");
    if (/\bdocument\.write\b/.test(s)) hits.push("document.write");
    return hits;
  };
  const hits = [...scan("index.html"), ...scan("styles.css"), ...scan("app.js")];
  return { ok: hits.length === 0, detail: hits };
});
await guard("1.3 运行期无外部请求", async () => {
  const external = record.requests.filter((u) => !u.startsWith(BASE) && !u.startsWith("data:"));
  return { ok: external.length === 0, detail: external.slice(0, 6) };
});
await guard("1.4 首次加载无脚本异常", async () => {
  const bad = record.console.filter((c) => c.type === "exception" || c.type === "log.error" || c.type === "console.error");
  return { ok: bad.length === 0, detail: bad.slice(0, 4) };
});

/* 2. 粘顶头部 + 固定工具条 */
await guard("2.1 滚到 800px 后头部仍贴顶", async () => {
  await evaluate("window.scrollTo(0, 800); 'ok'");
  await wait(400);
  const r = await evaluate("(()=>{const h=window.__p.header(); if(!h) return {err:'no-header'}; const b=h.getBoundingClientRect(); return {top:b.top, bottom:b.bottom, pos:getComputedStyle(h).position};})()");
  return { ok: Math.abs(r.top) <= 2 && r.bottom > 10, detail: r };
});
await guard("2.2 固定工具条在视口内", async () => {
  const r = await evaluate("(()=>{const t=window.__p.toolbar(); if(!t) return {err:'no-toolbar'}; const b=t.getBoundingClientRect(); return {pos:getComputedStyle(t).position, top:b.top, bottom:b.bottom, vh:innerHeight};})()");
  return { ok: r.top >= -2 && r.bottom <= r.vh + 2, detail: r };
});
await evaluate("window.scrollTo(0,0); 'ok'");
await wait(300);

/* 3. hidden 面板 */
await guard("3.1 高级筛选面板初始 display:none", async () => {
  const r = await evaluate("(()=>{const btn=[...document.querySelectorAll('button')].find(b=>window.__p.text(b).includes('高级')); if(!btn) return {err:'no-toggle'}; const id=btn.getAttribute('aria-controls'); const panel=id?document.getElementById(id):null; if(!panel) return {err:'no-panel'}; return {hidden:panel.hidden, display:getComputedStyle(panel).display};})()");
  return { ok: r.display === "none", detail: r };
});
await guard("3.2 展开/收起都能真正切换可见性", async () => {
  const before = await evaluate("(()=>{const btn=[...document.querySelectorAll('button')].find(b=>window.__p.text(b).includes('高级')); btn.click(); const id=btn.getAttribute('aria-controls'); const p=id?document.getElementById(id):null; return {display:getComputedStyle(p).display, hidden:p.hidden};})()");
  await wait(200);
  const after = await evaluate("(()=>{const btn=[...document.querySelectorAll('button')].find(b=>window.__p.text(b).includes('高级')); btn.click(); const id=btn.getAttribute('aria-controls'); const p=id?document.getElementById(id):null; return {display:getComputedStyle(p).display, hidden:p.hidden};})()");
  return { ok: before.display !== "none" && after.display === "none", detail: { before, after } };
});

/* 4. 筛选与 aria-live */
await guard("4.1 初始 aria-live 播报结果数", async () => {
  const r = await evaluate("({live: window.__p.liveText(), n: window.__p.liveNumber()})");
  return { ok: r.n !== null && r.n > 0, detail: r };
});
await guard("4.2 搜索 INC-005000 后只剩 1 条且播报 1", async () => {
  const ch = await liveAfterChange(() => setSearch("INC-005000", 700));
  const r = await evaluate("(()=>{const rows=window.__p.rows(); return {rows:rows.length, ids:rows.map(x=>x.dataset.id||window.__p.text(x).slice(0,12)).slice(0,3)};})()");
  return { ok: r.rows === 1 && ch.n === 1 && r.ids[0] === "INC-005000", detail: { ...r, n: ch.n, announced: ch.changed } };
});
await guard("4.3 「只看未解决」让计数变少", async () => {
  await setSearch("", 500);
  const base = await evaluate("window.__p.liveNumber()");
  const opened = await liveAfterChange(() => setUnresolved(true));
  await setUnresolved(false);
  await wait(300);
  return { ok: opened.n !== null && base !== null && opened.n < base, detail: { base, opened: opened.n, announced: opened.changed } };
});
await guard("4.4 严重级别多选收窄结果", async () => {
  const base = await evaluate("window.__p.liveNumber()");
  const only = await liveAfterChange(() => setSev(["P1"]));
  const back = await liveAfterChange(() => setSev(["P1", "P2", "P3", "P4"]));
  return { ok: only.n !== null && only.n > 0 && only.n < base && back.n === base, detail: { base, only: only.n, back: back.n, announced: only.changed } };
});

/* 5. 排序 */
const headerSeq = async () =>
  evaluate("window.__p.rows().slice(0,10).map(r=>r.dataset.id||window.__p.text(r).slice(0,16))");
const clickHeader = async (needle) =>
  evaluate(`(()=>{const all=[...document.querySelectorAll('th,button,[role="columnheader"]')].filter(e=>window.__p.text(e).includes(${JSON.stringify(needle)}) && e.getBoundingClientRect().height>0);
    if(!all.length) return 'missing';
    /* 取最深的那个：th 里包着排序按钮时必须点按钮，点 th 不会触发排序 */
    all.sort((a,b)=>{ const d=(n)=>{let c=0; for(let p=n;p;p=p.parentElement)c++; return c;}; return d(b)-d(a); });
    all[0].click(); return 'ok';})()`);
const monotonic = (arr, dir) => {
  if (!Array.isArray(arr) || arr.length < 3 || arr.some((x) => x === null)) return false;
  for (let i = 1; i < arr.length; i++) {
    if (dir === "asc" && arr[i - 1] > arr[i]) return false;
    if (dir === "desc" && arr[i - 1] < arr[i]) return false;
  }
  return true;
};
/* 题面只要求「点一次升序、再点反向」，没规定 P1 还是 P4 在前。
   窗口化后首屏可能全被同一级别填满（整屏同值），单看首尾判不出方向，
   所以只认两件事：每次点击后可见序列是单调的，且两次点击的序列确实不同（排序真生效且真反向）。 */
const monoAny = (arr) => monotonic(arr, "asc") || monotonic(arr, "desc");
const sameSeq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sortMark = async (needle) =>
  evaluate(`(()=>{const b=[...document.querySelectorAll('th,button')].find(e=>window.__p.text(e).includes(${JSON.stringify(needle)})); if(!b) return null;
    return {text:window.__p.text(b), sort:b.getAttribute('aria-sort'), html:/[↑↓▲▼]/.test(b.outerHTML)};})()`);
await guard("5.1 开始时间列点两次：先升序再降序，且有可见标记", async () => {
  await fresh();
  const first = await clickHeader("开始时间");
  if (first !== "ok") return { ok: false, detail: "找不到开始时间表头" };
  await wait(500);
  const a1 = await evaluate("window.__p.seq('time')");
  const mark = await sortMark("开始时间");
  await clickHeader("开始时间");
  await wait(500);
  const a2 = await evaluate("window.__p.seq('time')");
  const d1 = monoAny(a1), d2 = monoAny(a2);
  const changed = !sameSeq(a1, a2);
  const ok = Boolean(d1 && d2 && changed && mark && (mark.html || mark.sort));
  return { ok, detail: { mono: [d1, d2], changed, first: a1.slice(0, 3), second: a2.slice(0, 3), mark } };
});
await guard("5.2 严重级别列同样先升后降", async () => {
  const first = await clickHeader("级别");
  if (first !== "ok") return { ok: false, detail: "找不到级别表头" };
  await wait(500);
  const a1 = await evaluate("window.__p.seq('sev')");
  await clickHeader("级别");
  await wait(500);
  const a2 = await evaluate("window.__p.seq('sev')");
  const d1 = monoAny(a1), d2 = monoAny(a2);
  const changed = !sameSeq(a1, a2);
  return { ok: Boolean(d1 && d2 && changed), detail: { mono: [d1, d2], changed, first: a1.slice(0, 6), second: a2.slice(0, 6) } };
});

/* 6. 窗口化 */
await guard("6.1 5000 条下 data-row 不超过 200", async () => {
  await fresh();
  const r = await evaluate("({rows: window.__p.rows().length, totalText: window.__p.liveText().slice(0,60)})");
  return { ok: r.rows > 0 && r.rows <= 200, detail: r };
});
await guard("6.2 滚到列表底部仍受限且能看到末尾记录", async () => {
  await evaluate("window.scrollTo(0, document.documentElement.scrollHeight); 'ok'");
  await wait(700);
  const r = await evaluate(`(()=>{const rows=window.__p.rows(); if(!rows.length) return {err:'no-rows'};
    const last=rows[rows.length-1]; const rect=last.getBoundingClientRect();
    return {count:rows.length, lastId:last.dataset.id||window.__p.text(last).slice(0,14),
      firstId:rows[0].dataset.id||'', lastRect:{top:Math.round(rect.top), bottom:Math.round(rect.bottom), vh:innerHeight},
      atBottom: Math.abs(window.scrollY+innerHeight-document.documentElement.scrollHeight)<3};})()`);
  const ok = r.count > 0 && r.count <= 200 && r.lastRect.bottom <= r.lastRect.vh + 2 && r.lastRect.top >= -2 && r.lastId !== r.firstId && r.atBottom;
  return { ok, detail: r };
});
await guard("6.3 滚到底部能看到最后一条（按编号搜索 INC-005000）", async () => {
  await setSearch("INC-005000", 700);
  const r = await evaluate("(()=>{const rows=window.__p.rows(); return {n:rows.length, id:rows[0]&&(rows[0].dataset.id||window.__p.text(rows[0]).slice(0,14)), vis:rows[0]?window.__p.vis(rows[0]):false};})()");
  return { ok: r.n === 1 && r.id === "INC-005000" && r.vis, detail: r };
});

/* 7. 键盘导航 */
await guard("7.1 上下方向键在行间移动焦点", async () => {
  await fresh();
  const start = await evaluate("(()=>{const r=window.__p.rows()[0]; if(!r) return null; r.focus(); return {id:r.dataset.id, idx:0};})()");
  if (!start) return { ok: false, detail: "no rows" };
  await key(...KEYS.ArrowDown);
  const one = await evaluate("(()=>{const a=document.activeElement; const row=a&&a.closest('[data-row]'); const rows=window.__p.rows(); return {id:row?row.dataset.id:null, idx:row?rows.indexOf(row):-1};})()");
  await key(...KEYS.ArrowDown);
  const two = await evaluate("(()=>{const a=document.activeElement; const row=a&&a.closest('[data-row]'); const rows=window.__p.rows(); return {id:row?row.dataset.id:null, idx:row?rows.indexOf(row):-1};})()");
  return { ok: one.idx === 1 && two.idx === 2, detail: { start, one, two } };
});
await guard("7.2 Enter 打开详情", async () => {
  record.openedRowId = await evaluate("(()=>{const a=document.activeElement; return a?.closest('[data-row]')?.dataset.id||null;})()");
  await key(...KEYS.Enter);
  await wait(450);
  const r = await evaluate("(()=>{const d=window.__p.drawer(); return {open:!!d, text:d?window.__p.text(d).slice(0,60):''};})()");
  record.drawerWasOpen = r.open;
  return { ok: r.open && record.openedRowId !== null, detail: { openedRowId: record.openedRowId, ...r } };
});

/* 8. 抽屉 */
await guard("8.1 抽屉 fixed 定位在右侧", async () => {
  const ens = await ensureDrawer();
  if (!ens.open) return { ok: false, detail: { err: "no-drawer", ens } };
  const r = await evaluate("(()=>{const d=window.__p.drawer(); if(!d) return {err:'no-drawer'}; const b=d.getBoundingClientRect(); return {pos:getComputedStyle(d).position, right:Math.round(innerWidth-b.right), width:Math.round(b.width)};})()");
  return { ok: r.pos === "fixed" && Math.abs(r.right) <= 3, detail: r };
});
await guard("8.2 打开后焦点进入抽屉", async () => {
  const r = await evaluate("(()=>{const d=window.__p.drawer(); return {inside: d? d.contains(document.activeElement) : false, active: document.activeElement?.tagName};})()");
  return { ok: r.inside, detail: r };
});
await guard("8.3 Tab 循环不逃出抽屉", async () => {
  const seq = [];
  for (let i = 0; i < 7; i++) {
    await key(...KEYS.Tab);
    seq.push(await evaluate("(()=>{const d=window.__p.drawer(); return {in:d?d.contains(document.activeElement):false, tag:document.activeElement?.tagName, text:window.__p.text(document.activeElement).slice(0,10)};})()"));
  }
  return { ok: seq.every((s) => s.in), detail: seq.map((s) => s.text || s.tag) };
});
await guard("8.4 抽屉打开时背景不可滚动", async () => {
  const r = await evaluate("(()=>{const b=getComputedStyle(document.body), h=getComputedStyle(document.documentElement); return {body:b.overflow, html:h.overflow};})()");
  const before = await evaluate("window.scrollY");
  await cdp("Input.dispatchMouseEvent", { type: "mouseWheel", x: 200, y: 260, deltaX: 0, deltaY: 500, button: "none", pointerType: "mouse" });
  await wait(400);
  const after = await evaluate("window.scrollY");
  // 两种合法实现：锁 body/html overflow，或滚轮确实推不动页面。
  const locked = r.body === "hidden" || r.html === "hidden" || Math.abs(after - before) < 2;
  return { ok: locked, detail: { ...r, before, after } };
});
await guard("8.5 Esc 关闭并把焦点还给来源行", async () => {
  await key(...KEYS.Escape);
  await wait(500);
  const r = await evaluate("(()=>{const d=window.__p.drawer(); const a=document.activeElement; return {open:!!d, row:a?.closest('[data-row]')?.dataset.id||null, tag:a?.tagName};})()");
  return { ok: !r.open && r.row === record.openedRowId, detail: { openedRowId: record.openedRowId, ...r } };
});

/* 9. 状态流转 + 撤销/重做 + 提示 */
const liveCount = () => evaluate("(()=>{const els=[...document.querySelectorAll('[role=\"status\"],[role=\"alert\"],[aria-live]')]; return els.filter(e=>window.__p.vis(e)).length;})()");
const rowStatus = (idx = 0) => evaluate(`(()=>{const r=window.__p.rows()[${idx}]; if(!r) return null; if(r.dataset.status) return r.dataset.status;
  const t=window.__p.text(r); return /已解决|resolved/.test(t)?'resolved':(/已确认|acknowledged/.test(t)?'acknowledged':'investigating');})()`);
const openFirstSolvable = async () => {
  const attempts = [];
  for (let i = 0; i < 12; i++) {
    const st = await rowStatus(i);
    if (st === "resolved") { attempts.push({ i, st, skip: "resolved" }); continue; }
    await evaluate(`(()=>{const r=window.__p.rows()[${i}]; if(!r) return 'no-row'; r.focus(); return 'ok';})()`);
    await key(...KEYS.Enter);
    await wait(400);
    let open = await evaluate("!!window.__p.drawer()");
    if (!open) {
      await evaluate(`(()=>{const r=window.__p.rows()[${i}]; if(r) r.click(); return 'ok';})()`);
      await wait(400);
      open = await evaluate("!!window.__p.drawer()");
    }
    attempts.push({ i, st, open });
    if (open) return { ok: true, idx: i, attempts };
    await key(...KEYS.Escape);
    await wait(300);
  }
  return { ok: false, attempts };
};
const clickDrawerResolve = async () => evaluate(`(()=>{const d=window.__p.drawer(); if(!d) return 'no-drawer';
  const b=[...d.querySelectorAll('button')].find(x=>/解决|resolved/i.test(window.__p.text(x)));
  if(!b) return 'no-btn'; if(b.disabled) return 'disabled'; b.click(); return 'ok';})()`);
await guard("9.1 抽屉内可改状态", async () => {
  await fresh();
  const opened = await openFirstSolvable();
  if (!opened.ok) return { ok: false, detail: { err: "前 12 行没有非已解决记录或抽屉打不开", attempts: opened.attempts } };
  record.toastBaseline = await liveCount();
  record.solvedIdx = opened.idx;
  const before = await rowStatus(opened.idx);
  await shot("drawer-open");
  const clicked = await clickDrawerResolve();
  if (clicked !== "ok") return { ok: false, detail: "抽屉里找不到解决按钮：" + clicked };
  await wait(500);
  const after = await rowStatus(opened.idx);
  record.statusBefore = before;
  return { ok: before !== "resolved" && after === "resolved", detail: { before, after, baseline: record.toastBaseline } };
});
await guard("9.2 提示出现并在 4 秒后消失", async () => {
  const now = await liveCount();
  await wait(4600);
  const later = await liveCount();
  return { ok: now > record.toastBaseline && later <= record.toastBaseline, detail: { baseline: record.toastBaseline, now, later } };
});
await guard("9.3 Ctrl+Z 撤销、Ctrl+Shift+Z 重做", async () => {
  const idx = Number.isInteger(record.solvedIdx) ? record.solvedIdx : 0;
  const st = () => rowStatus(idx);
  const before = await st();
  await key("z", "KeyZ", 90, 2);
  await wait(500);
  const undone = await st();
  await key("Z", "KeyZ", 90, 10);
  await wait(500);
  const redone = await st();
  return { ok: before === "resolved" && undone === record.statusBefore && redone === "resolved", detail: { before, undone, redone, expectedUndo: record.statusBefore } };
});
await guard("9.4 关闭抽屉后页面无异常", async () => {
  await key(...KEYS.Escape);
  await wait(300);
  const bad = record.console.filter((c) => c.type === "exception" || c.type === "log.error");
  return { ok: bad.length === 0, detail: bad.slice(0, 3) };
});

/* 10. 批量选择：三态 + 作用域 */
await guard("10.1 全选三态正确", async () => {
  await fresh();
  const snap = () => evaluate("(()=>{const a=window.__p.selectAll(); return {checked:a&&a.checked, ind:a&&a.indeterminate};})()");
  const initial = await snap();
  await evaluate("(()=>{const r=window.__p.rows()[0]; const b=window.__p.rowCheckbox(r); b.click(); return 'ok';})()");
  await wait(300);
  const partial = await snap();
  await evaluate("window.__p.selectAll().click(); 'ok'");
  await wait(300);
  const all = await snap();
  await evaluate("window.__p.selectAll().click(); 'ok'");
  await wait(300);
  const none = await snap();
  return {
    ok: initial.checked === false && partial.ind === true && all.checked === true && all.ind === false && none.checked === false && none.ind === false,
    detail: { initial, partial, all, none },
  };
});
await guard("10.2 全选与批量解决只作用于当前筛选结果", async () => {
  await fresh();
  const openOff = await setUnresolved(false);
  if (openOff === "missing") return { ok: false, detail: "找不到「只看未解决」控件" };
  const opened = await liveAfterChange(() => setUnresolved(true));
  const idx = opened.idx >= 0 ? opened.idx : undefined;
  const p2 = await liveAfterChange(() => setSev(["P2"]), idx, true);
  const p1 = await liveAfterChange(() => setSev(["P1"]), idx, true);
  if (!(p1.n > 0 && p2.n > 0)) return { ok: false, detail: { p2open: p2.n, p1open: p1.n, announced: p1.changed } };
  await evaluate("window.__p.selectAll().click(); 'ok'");
  await wait(300);
  const selectedState = await evaluate("(()=>{const a=window.__p.selectAll(); return {checked:a.checked, ind:a.indeterminate};})()");
  let bulkErr = null;
  const after = await liveAfterChange(async () => {
    bulkErr = await evaluate(`(()=>{const t=window.__p.toolbar(); if(!t) return 'no-toolbar';
      const b=[...t.querySelectorAll('button')].find(x=>window.__p.text(x).includes('解决')); if(!b) return 'no-btn'; b.click(); return 'ok';})()`);
  }, p1.idx >= 0 ? p1.idx : undefined, true);
  if (bulkErr !== "ok") return { ok: false, detail: "批量解决按钮：" + bulkErr };
  const p2back = await liveAfterChange(() => setSev(["P2"]), p1.idx >= 0 ? p1.idx : undefined, true);
  return {
    ok: after.n === 0 && p2back.n === p2.n && selectedState.checked === true,
    detail: { p2open: p2.n, p1open: p1.n, p1after: after.n, p2after: p2back.n, idx: p1.idx, selectedState },
  };
});

/* 11. URL hash 往返 */
await guard("11.1 视图写进 hash 且刷新后一致", async () => {
  await fresh();
  await setSev(["P1", "P3"]);
  await setSearch("华东", 700);
  await clickHeader("级别");
  await wait(400);
  const before = await evaluate("({hash:location.hash, n:window.__p.liveNumber(), q:window.__p.search()?.value||'', sev:[...document.querySelectorAll('input.sev,[data-sev],[data-severity],input[type=checkbox][value^=P]')].filter(x=>/^P[1-4]$/.test(x.value||'')).map(b=>b.checked)})");
  if (!before.hash || before.hash.length < 4) return { ok: false, detail: before };
  await cdp("Page.reload", { ignoreCache: false });
  await wait(1200);
  await evaluate(H);
  const after = await evaluate("({hash:location.hash, n:window.__p.liveNumber(), q:window.__p.search()?.value||'', sev:[...document.querySelectorAll('input.sev,[data-sev],[data-severity],input[type=checkbox][value^=P]')].filter(x=>/^P[1-4]$/.test(x.value||'')).map(b=>b.checked)})");
  return {
    ok: before.n === after.n && before.q === after.q && JSON.stringify(before.sev) === JSON.stringify(after.sev) && before.hash === after.hash,
    detail: { before: { ...before, sev: before.sev.join() }, after: { ...after, sev: after.sev.join() } },
  };
});
await guard("11.2 无 hash 时用 localStorage 恢复视图", async () => {
  await fresh();
  const ch = await liveAfterChange(() => setSearch("INC-005000", 700));
  const q = await evaluate("window.__p.search()?.value||''");
  if (ch.n !== 1 || q !== "INC-005000") return { ok: false, detail: { err: "前置唯一视图没有建立", n: ch.n, q } };
  await goto(BASE, 1400);
  await evaluate(H);
  // 刷新时没有 hash，只能靠 localStorage；用「关键词回来了 + 行数随之为 1」判恢复，别读可能写死的总数。
  const after = await evaluate("({q:window.__p.search()?.value||'', hash:location.hash, rows:window.__p.rows().length})");
  return { ok: after.q === "INC-005000" && after.rows >= 1 && after.rows <= 3, detail: { before: 1, q, after } };
});

/* 12. 390px */
await guard("12.1 390px 无横向滚动、单列卡片、字号 >= 14px", async () => {
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await cdp("Page.reload", { ignoreCache: false });
  await wait(1300);
  await evaluate(H);
  await shot("mobile-390");
  // 桌面 7 列布局被 .table-wrap{overflow:hidden} 剪掉横向溢出，scrollWidth 不会变大；
  // 「每个格子各有一个 top」也会被不同高度的格子偶然满足。所以改判用户真正看到的形状：
  // 标题必须堆到编号下方，且没有任何格子的右边界越过行右边界（桌面网格在 390px 下必然越界）。
  const r = await evaluate(`(()=>{
    const doc=document.documentElement;
    const rows=window.__p.rows().slice(0,3);
    const data=rows.map(row=>{
      const box=row.getBoundingClientRect();
      const cells=[...row.children].filter(c=>window.__p.vis(c)&&window.__p.text(c).trim());
      const idCell=cells.find(c=>/INC-\\d+/i.test(window.__p.text(c)))||null;
      const titleCell=cells.length?cells.reduce((a,c)=>window.__p.text(c).length>window.__p.text(a).length?c:a):null;
      let minFont=99;
      cells.forEach(c=>{ minFont=Math.min(minFont, parseFloat(getComputedStyle(c).fontSize)); });
      const bleed=Math.round(Math.max(0,...cells.map(c=>c.getBoundingClientRect().right-box.right)));
      const stacked=Boolean(idCell&&titleCell&&idCell!==titleCell&&
        titleCell.getBoundingClientRect().top>idCell.getBoundingClientRect().top+6);
      return {width:Math.round(box.width), stacked, bleed, minFont};
    });
    const width=doc.clientWidth;
    return {scrollW:doc.scrollWidth, clientW:width, overflow:doc.scrollWidth-width, rows:data,
      minFont:data.length?Math.min(...data.map(d=>d.minFont)):99};
  })()`);
  const single = r.rows.length > 0 && r.rows.every((d) => d.stacked && d.bleed <= 1 && d.width >= r.clientW * 0.85);
  return { ok: r.overflow <= 1 && single && r.minFont >= 14, detail: r };
});
await cdp("Emulation.clearDeviceMetricsOverride");
await wait(200);

/* 13. reduced motion */
await guard("13.1 prefers-reduced-motion 下过渡时长归零", async () => {
  await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  await fresh();
  const ens = await ensureDrawer();
  if (!ens.open) { await cdp("Emulation.setEmulatedMedia", { features: [] }); return { ok: false, detail: { err: "no-drawer", ens } }; }
  const r = await evaluate(`(()=>{const d=window.__p.drawer(); if(!d) return {err:'no-drawer'};
    const s=getComputedStyle(d); const p=(v)=>Math.max(...String(v).split(',').map(x=>parseFloat(x)||0));
    return {transition:p(s.transitionDuration), animation:p(s.animationDuration), matches:matchMedia('(prefers-reduced-motion: reduce)').matches};})()`);
  await cdp("Emulation.setEmulatedMedia", { features: [] });
  return { ok: r.matches === true && r.transition <= 0.01 && r.animation <= 0.01, detail: r };
});

/* 14. print */
await guard("14.1 print 媒体下隐藏筛选区与底部工具条", async () => {
  await fresh();
  const r0 = await evaluate("(()=>{window.__torToolbar=window.__p.toolbar(); window.__torFilters=window.__p.filterBlock(); return {toolbar:!!window.__torToolbar, filters:!!window.__torFilters};})()");
  if (!r0.toolbar || !r0.filters) return { ok: false, detail: { ...r0, note: "找不到工具条或筛选区" } };
  await cdp("Emulation.setEmulatedMedia", { media: "print" });
  await wait(300);
  const r = await evaluate(`(()=>{const t=window.__torToolbar; const f=window.__torFilters;
    return {toolbarDisplay:getComputedStyle(t).display, toolbarHidden:window.__p.hiddenAncestor(t), filtersHidden:window.__p.hiddenAncestor(f)};})()`);
  await cdp("Emulation.setEmulatedMedia", { media: "" });
  return { ok: r.toolbarHidden === true && r.filtersHidden === true, detail: r };
});

/* 15. XSS */
await guard("15.1 恶意标题按纯文本渲染，未执行", async () => {
  await fresh();
  await setSearch("img src=x", 800);
  const before = await evaluate("({xss: window.__xss===undefined?null:window.__xss, img:document.querySelectorAll('img[src=\"x\"]').length, rows:window.__p.rows().length, text:window.__p.text(window.__p.rows()[0]||document.body).slice(0,120)})");
  await wait(800);
  const after = await evaluate("({xss: window.__xss===undefined?null:window.__xss})");
  const ok = before.rows >= 1 && before.xss === null && after.xss === null && before.img === 0 && before.text.includes("<img src=x onerror=");
  return { ok, detail: { before, after } };
});

/* 16. 自测 */
await guard("16.1 ?selftest=1 自测真实通过", async () => {
  await goto(BASE + "?selftest=1", 1500);
  let state = null;
  for (let i = 0; i < 25; i++) {
    state = await evaluate("({flag:document.documentElement.getAttribute('data-selftest'), text:(document.querySelector('#selftest-result')?.innerText||'').replace(/\\s+/g,' ').trim().slice(0,4000)})");
    if (state.flag) break;
    await wait(200);
  }
  await evaluate(H);
  return { ok: state && state.flag === "pass" && state.text.length > 0 && !/\bFAIL\b/.test(state.text), detail: { ...state, text: state && state.text.slice(0, 1500) } };
});

/* 17. 收尾：整场无异常 */
await guard("17.1 全程无未捕获异常", async () => {
  const bad = record.console.filter((c) => c.type === "exception" || c.type === "log.error" || c.type === "console.error");
  return { ok: bad.length === 0, detail: bad.slice(0, 6) };
});

const failed = record.steps.filter((s) => !s.ok && !s.name.startsWith("shot:"));
const shots = record.steps.filter((s) => s.name.startsWith("shot:"));
record.summary = { total: record.steps.filter((s) => !s.name.startsWith("shot:")).length, failed: failed.length, shots: shots.map((s) => s.detail) };
writeFileSync(OUT, JSON.stringify(record, null, 2), "utf8");
console.log(`\n== ${failed.length === 0 ? "ALL PASS" : failed.length + " FAILED"} ==`);
for (const f of failed) console.log("  ✗ " + f.name);
console.log("json:", OUT);

chrome.kill();
spawn("taskkill", ["/PID", String(chrome.pid), "/T", "/F"], { windowsHide: true });
server.close();
await wait(200);
try {
  rmSync(profile, { recursive: true, force: true });
} catch {
  /* 临时 profile 删不掉不影响判定 */
}
process.exit(failed.length === 0 ? 0 : 1);
