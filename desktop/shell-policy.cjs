"use strict";

/**
 * 桌面壳的「策略层」：单实例、托盘/关闭路由、全局快捷键、更新判定里
 * 所有能纯函数化的判断。
 *
 * 这个文件禁止 require("electron")：main.cjs 一旦 require('electron') 就跑不进
 * 普通 node 测试，判定规则就没法钉住（和 gpu-guard.cjs、api-base.cjs 同一个理由）。
 * main.cjs 只保留「拿 Electron 的状态 → 调这里的函数 → 执行返回的动作」这类搬运工作。
 *
 * 自动更新在本任务里刻意只是骨架：decideUpdate 的每个分支（包括找到更新的版本）
 * 输出都带 `install: false`，下载与安装不在职责内，也不许在这里发生。
 */

// ── 1. 单实例 ───────────────────────────────────────────────────────────────

/**
 * 第二实例该不该放弃启动。`gotLock` 是 requestSingleInstanceLock() 的返回值。
 * 放弃的一侧必须在建窗口、spawn 后端之前退出——否则同机两份壳会抢同一个端口、
 * 各起一个后端，还会把 gpu-state 的启动计数记花。
 */
function shouldAbortSecondaryInstance(gotLock) {
  return !gotLock;
}

/**
 * 主实例收到「第二实例启动」信号后，怎么把窗口唤回来。
 * 三种现存形态都要覆盖：正常开着（只需 focus）、最小化（restore+focus）、
 * 被「关到托盘」隐藏（show+focus，这一条正是需求点名的「最小化到托盘也要唤得回」）。
 * 输入是窗口状态快照，输出是要依次执行的动作清单，执行归 main.cjs。
 */
function planSecondInstanceReveal({ hasWindow, minimized, visible } = {}) {
  if (!hasWindow) {
    // 窗口不存在（比如上一次异常销毁）：重新建一个，其余动作不适用。
    return { create: true, restore: false, show: false, focus: false };
  }
  return {
    create: false,
    restore: Boolean(minimized),
    // 隐藏态（托盘）必须显式 show；已最小化的窗口 isVisible 也可能为真，
    // 所以 show 只看 visible 这一个事实，由 main.cjs 按 restore→show→focus 顺序执行。
    show: !visible,
    focus: true,
  };
}

// ── 2. 托盘 / 关闭路由 ──────────────────────────────────────────────────────

/**
 * 点窗口 ✕ 时到底怎么办。
 * - 显式退出（托盘「退出」/ before-quit 已经置位）→ "quit"，别让守卫复活成「关窗=退出」的反面；
 * - 托盘不可用（创建失败或被 BLANK_TRAY=0 关掉）→ 退回 "quit"：
 *   没有托盘还隐藏窗口，等于把用户关在一个既看不见又关不掉的界面里；
 * - 其余 → "hide"，进托盘，不退出。
 */
function decideWindowClose({ explicitQuit, trayAvailable } = {}) {
  if (explicitQuit) return "quit";
  return trayAvailable ? "hide" : "quit";
}

/**
 * 托盘菜单的结构（纯数据，main.cjs 拿 id 去挂 click）。
 * 「后端状态」是一条只读展示项，随守护结果刷新；退出必须走托盘菜单这一条显式路径。
 * `update` 为 decideUpdate 的返回值时追加一行只读的更新状态，null 表示还没查过/没配置。
 */
function buildTrayMenuModel({ backendOnline, update } = {}) {
  const items = [
    { id: "show", label: "显示主窗口" },
    { id: "backend", label: `后端状态：${backendOnline ? "运行中" : "未运行"}`, enabled: false },
  ];
  if (update && typeof update === "object") {
    items.push({ id: "update", label: trayUpdateLabel(update), enabled: false });
  }
  items.push({ type: "separator" });
  items.push({ id: "quit", label: "退出" });
  return items;
}

/** 更新决策在托盘里的一行文案。任何分支都不承诺会安装。 */
function trayUpdateLabel(decision) {
  switch (decision && decision.action) {
    case "available":
      return `更新：发现新版本 ${decision.version}（骨架不会自动安装）`;
    case "none":
      return "更新：已是最新";
    case "unavailable":
      return "更新：不可用（原因见日志）";
    default:
      return "更新：未检查";
  }
}

// ── 3. 全局快捷键 ───────────────────────────────────────────────────────────

/** 默认唤出/隐藏组合；BLANK_HOTKEY 可覆盖（注册失败一律降级，不影响启动）。 */
const DEFAULT_HOTKEY = "Control+Alt+B";

const HOTKEY_MODIFIERS = new Map([
  ["control", "Control"],
  ["ctrl", "Control"],
  ["alt", "Alt"],
  ["option", "Alt"],
  ["shift", "Shift"],
  ["super", "Super"],
  ["win", "Super"],
  ["meta", "Super"],
  ["cmd", "Command"],
  ["command", "Command"],
]);

const HOTKEY_KEYS = new Map([
  ["space", "Space"],
  ["tab", "Tab"],
  ["enter", "Enter"],
  ["return", "Enter"],
  ["escape", "Escape"],
  ["esc", "Escape"],
  ["backspace", "Backspace"],
  ["delete", "Delete"],
  ["insert", "Insert"],
  ["home", "Home"],
  ["end", "End"],
  ["pageup", "PageUp"],
  ["pagedown", "PageDown"],
  ["up", "Up"],
  ["down", "Down"],
  ["left", "Left"],
  ["right", "Right"],
]);

/**
 * 把用户写的组合（"ctrl + alt+b"）规范成 Electron 认识的形式（"Control+Alt+B"）。
 * 故意比 Electron 更严：裸的单字符键（比如就一个 "B"）会全局吞按键、别的程序都没法用，
 * 必须带修饰键（F1–F24 例外，它们本身几乎不会被打字占用）。
 * 返回 { ok:false, reason } 时 main.cjs 要写日志并放弃注册，而不是抛异常炸主进程。
 */
function normalizeHotkey(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return { ok: false, reason: "快捷键未配置" };
  const tokens = text.split("+").map((token) => token.trim()).filter(Boolean);
  if (tokens.length < 2) {
    // 允许裸功能键（F1..F24）；其余单词一律要求修饰键。
    if (tokens.length === 1 && /^f([1-9]|1[0-9]|2[0-4])$/i.test(tokens[0])) {
      return { ok: true, accelerator: tokens[0].toUpperCase() };
    }
    return {
      ok: false,
      reason: `快捷键 "${text}" 需要至少一个修饰键（Control/Alt/Shift 等）加一个主键`,
    };
  }
  const keyToken = tokens[tokens.length - 1];
  const modifiers = tokens.slice(0, -1);
  const seen = new Set();
  const canonicalModifiers = [];
  for (const modifier of modifiers) {
    const canonical = HOTKEY_MODIFIERS.get(modifier.toLowerCase());
    if (!canonical) return { ok: false, reason: `快捷键 "${text}" 里的修饰键 "${modifier}" 不认识` };
    if (seen.has(canonical)) return { ok: false, reason: `快捷键 "${text}" 里修饰键 "${modifier}" 重复` };
    seen.add(canonical);
    canonicalModifiers.push(canonical);
  }
  let canonicalKey = HOTKEY_KEYS.get(keyToken.toLowerCase()) ?? null;
  if (!canonicalKey) {
    if (/^f([1-9]|1[0-9]|2[0-4])$/i.test(keyToken)) canonicalKey = keyToken.toUpperCase();
    else if (/^[a-z0-9]$/i.test(keyToken)) canonicalKey = keyToken.toUpperCase();
    else if (/^[a-z]-[a-z]$/i.test(keyToken)) {
      // Electron 支持 "A-Z" 这种范围键，但作为全局唤出组合基本是误配，直接拒。
      return { ok: false, reason: `快捷键 "${text}" 里的 "${keyToken}" 是键范围，不适合作唤出组合` };
    } else return { ok: false, reason: `快捷键 "${text}" 里的主键 "${keyToken}" 不认识` };
  }
  // 走到这里 canonicalKey 必非空；末位是修饰键（如 "Control+Alt"）已落在上面的"不认识"分支。
  return { ok: true, accelerator: [...canonicalModifiers, canonicalKey].join("+") };
}

/**
 * 注册结果的三态（需求点名的三态都在这里，main.cjs 只负责把 Electron 的返回递进来）：
 * - registered：成功；
 * - occupied：返回 false，被别的程序（常见是另一个热键助手）占了 → 降级、写日志；
 * - error：register 直接抛了（Electron 对个别组合会抛）→ 同样降级，绝不让主进程崩。
 */
function describeShortcutRegistration(accelerator, outcome) {
  if (outcome === true) {
    return { status: "registered", message: `全局快捷键 ${accelerator} 已注册` };
  }
  if (outcome === false) {
    return {
      status: "occupied",
      message: `全局快捷键 ${accelerator} 已被其他程序占用（可能是别的热键助手），降级：本次不启用全局快捷键，主流程不受影响`,
    };
  }
  const detail = outcome instanceof Error ? outcome.message : String(outcome ?? "未知错误");
  return {
    status: "error",
    message: `全局快捷键 ${accelerator} 注册抛出异常（${detail}），降级：本次不启用全局快捷键，主流程不受影响`,
  };
}

/**
 * 快捷键按下后对主窗口的动作：可见且持有焦点 → 隐藏；其余任何状态
 * （隐藏、最小化、没焦点、窗口不存在）→ 唤回并聚焦。
 */
function planHotkeyToggle({ hasWindow, visible, focused } = {}) {
  if (!hasWindow) return { create: true, hide: false, show: false, focus: false };
  if (visible && focused) return { create: false, hide: true, show: false, focus: false };
  return { create: false, hide: false, show: true, focus: true };
}

// ── 4. 自动更新（仅决策骨架，永不安装）─────────────────────────────────────

/** 解析 "0.8.5" / "v0.8.5-rc.1"；不合规返回 null。 */
function parseVersion(value) {
  const text = String(value ?? "").trim().replace(/^v/i, "");
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(text);
  if (!match) return null;
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    pre: match[4] ?? null,
  };
}

/** semver 的预发布标识符序：数字段按数值比且小于字母段；短的一方更小。 */
function comparePrerelease(a, b) {
  const xs = a.split(".");
  const ys = b.split(".");
  const n = Math.max(xs.length, ys.length);
  for (let i = 0; i < n; i += 1) {
    if (xs[i] === undefined) return -1;
    if (ys[i] === undefined) return 1;
    const xn = /^\d+$/.test(xs[i]) ? Number(xs[i]) : null;
    const yn = /^\d+$/.test(ys[i]) ? Number(ys[i]) : null;
    if (xn !== null && yn !== null) {
      if (xn !== yn) return xn < yn ? -1 : 1;
      continue;
    }
    if (xn !== null) return -1;
    if (yn !== null) return 1;
    if (xs[i] !== ys[i]) return xs[i] < ys[i] ? -1 : 1;
  }
  return 0;
}

/** 返回 -1/0/1；任何一边解析不了返回 null（调用方必须把 null 当「不能更新」处理）。 */
function compareVersions(a, b) {
  const left = typeof a === "object" && a !== null ? a : parseVersion(a);
  const right = typeof b === "object" && b !== null ? b : parseVersion(b);
  if (!left || !right) return null;
  for (const field of ["major", "minor", "patch"]) {
    if (left[field] !== right[field]) return left[field] < right[field] ? -1 : 1;
  }
  if (left.pre === right.pre) return 0;
  if (left.pre === null) return 1; // 正式版高于同号预发布版
  if (right.pre === null) return -1;
  return comparePrerelease(left.pre, right.pre);
}

/**
 * feed 的 JSON 载荷 → 候选条目数组。接受三种形态：条目数组、
 * `{ entries: [...] }`、单个条目对象；其余形状返回 null（元数据不可读）。
 */
function extractFeedEntries(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === "object") {
    if (Array.isArray(payload.entries)) return payload.entries;
    if (typeof payload.version === "string") return [payload];
  }
  return null;
}

/** 从候选里选版本最高的一条；没有一条版本可解析时返回 null。 */
function pickLatestEntry(entries) {
  if (!Array.isArray(entries)) return null;
  let best = null;
  for (const item of entries) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    if (!parseVersion(item.version)) continue;
    if (best === null || compareVersions(item.version, best.version) > 0) best = item;
  }
  return best;
}

/** 更新源 URL 的可用性判定（空=未配置，骨架默认不碰网络）。 */
function parseUpdateFeedUrl(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return { ok: false, reason: "未配置 BLANK_UPDATE_FEED_URL：跳过更新检查" };
  if (!/^https?:\/\//i.test(text)) return { ok: false, reason: `更新源 URL 必须是 http(s)：${text}` };
  return { ok: true, url: text };
}

/**
 * 更新决策（纯函数，需求 #4 的主体）：
 * 输入 current（本机版本）、entry（feed 里挑出的条目，可缺）、fetchError（取源失败的原因），
 * 输出 { action, install:false, reason, version?, url?, sha256? }。
 *
 * 铁律：**任何分支的 install 都是 false**。网络失败、元数据缺字段、签名摘要缺失/格式不对，
 * 都要给可读 reason 并拒掉；就算一切合规，也只是 "available"（仅提示），下载与安装
 * 明确不属于这个骨架。
 */
function decideUpdate({ current, entry, fetchError } = {}) {
  const unavailable = (reason) => ({ install: false, action: "unavailable", reason });
  const upToDate = (reason) => ({ install: false, action: "none", reason });

  if (fetchError) {
    return unavailable(`更新源请求失败：${String(fetchError).trim() || "未知错误"}`);
  }
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    return unavailable("更新元数据缺失：更新源没有返回可用条目");
  }
  const versionText = typeof entry.version === "string" ? entry.version.trim() : "";
  if (!versionText) return unavailable("更新元数据缺 version 字段");
  const available = parseVersion(versionText);
  if (!available) return unavailable(`更新源版本号无法识别：${versionText}`);
  const running = parseVersion(current);
  if (!running) return unavailable(`当前版本号无法识别：${String(current ?? "")}，保守起见不更新`);
  const cmp = compareVersions(available, running);
  // cmp 为 null 在这里到不了（两边都解析过），但按「不能比就不更新」处理，不赌。
  if (cmp === null || cmp <= 0) {
    return upToDate(`当前 ${String(current).trim()} 不低于更新源 ${versionText}，无需更新`);
  }
  const url = typeof entry.url === "string" ? entry.url.trim() : "";
  if (!url) return unavailable(`新版本 ${versionText} 缺下载地址，拒绝更新`);
  if (!/^https?:\/\//i.test(url)) return unavailable(`新版本下载地址不是 http(s)，拒绝更新：${url}`);
  const digest = typeof entry.sha256 === "string" ? entry.sha256.trim() : "";
  if (!digest) return unavailable(`新版本 ${versionText} 缺签名摘要（sha256），拒绝更新`);
  if (!/^[0-9a-fA-F]{64}$/.test(digest)) {
    return unavailable(`新版本签名摘要不是 64 位十六进制（实际 ${digest.length} 字符），拒绝更新`);
  }
  return {
    install: false,
    action: "available",
    version: versionText,
    url,
    sha256: digest.toLowerCase(),
    reason: `发现新版本 ${versionText}（高于当前 ${String(current).trim()}）。骨架只记录/提示，不下载、不安装`,
  };
}

module.exports = {
  DEFAULT_HOTKEY,
  buildTrayMenuModel,
  compareVersions,
  decideUpdate,
  decideWindowClose,
  describeShortcutRegistration,
  extractFeedEntries,
  normalizeHotkey,
  parseUpdateFeedUrl,
  parseVersion,
  pickLatestEntry,
  planHotkeyToggle,
  planSecondInstanceReveal,
  shouldAbortSecondaryInstance,
  trayUpdateLabel,
};
