"""REPL 执行层：每个任务一个持久 JavaScript 会话（对齐 Claude 的 REPL 工具）。

Claude 的契约是「一段 JavaScript 就是一个工具调用」：支持顶层 ``await``，变量
跨调用保留，最后一条表达式就是返回值，``console.log`` / ``console.error`` 分别
捕获成 stdout / stderr。本地实现把执行本体放在一个长期存活的 Node 子进程里：

* Node 只跑一段密封的 ``vm`` 上下文：没有 ``require`` / ``process`` / ``import``，
  文件与命令只能通过宿主注入的工具函数做；
* 工具函数把调用发回 Python，走与顶层调用同一条网关（权限、场景、安全、审计、
  沙箱），REPL 不是绕过执行边界的旁路；需要审批的内层调用直接拒绝——REPL 里
  弹不出审批卡，模型必须把这一步改成顶层工具调用；
* 会话按任务存活，任务收尾由 ``aclose`` 收走进程；超时或进程崩溃会杀掉并重建，
  下一次调用从空状态开始（回执里如实说明状态已丢）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from blank_api.services.agent import workflow as workflow_module

# 与 Claude 契约同一组边界：默认 30 秒，上限 10 分钟。
REPL_DEFAULT_TIMEOUT_MS = 30_000
REPL_MAX_TIMEOUT_MS = 600_000
MAX_CODE_CHARS = 100_000
# 回执里的 stdout/stderr 各自上限；完整值仍在 data 里（再由网关统一截断/打码）。
MAX_STREAM_CHARS = 20_000
MAX_ERROR_CHARS = 4_000
START_TIMEOUT_SECONDS = 20.0
# 宿主截止时间比请求多给一点进程调度余量，真正的执行期限仍由 timeout_ms 决定。
DEADLINE_GRACE_SECONDS = 2.0

ToolRunner = Callable[[str, dict], Awaitable[dict]]


class ReplError(RuntimeError):
    """REPL 无法执行（不是脚本本身的语法/运行时错误）。"""


class ReplTimeout(ReplError):
    """一次调用超过 timeout_ms；会话已被杀掉，状态不再保留。"""


_RUNNER_JS = r"""
"use strict";
const vm = require("node:vm");
const util = require("node:util");
const readline = require("node:readline");

const LINE_LIMIT = 10000;
const TOTAL_LIMIT = 200000;
let outBuf = [];
let errBuf = [];
let outLen = 0;
let errLen = 0;
let truncated = false;

function resetBuffers() {
  outBuf = [];
  errBuf = [];
  outLen = 0;
  errLen = 0;
  truncated = false;
}

function pushLine(kind, text) {
  const raw = String(text);
  const line = raw.length > LINE_LIMIT ? raw.slice(0, LINE_LIMIT) + "\u2026" : raw;
  if (kind === "out") {
    if (outLen + line.length + 1 > TOTAL_LIMIT) { truncated = true; return; }
    outBuf.push(line);
    outLen += line.length + 1;
  } else {
    if (errLen + line.length + 1 > TOTAL_LIMIT) { truncated = true; return; }
    errBuf.push(line);
    errLen += line.length + 1;
  }
}

function fmtArgs(args) {
  return args
    .map((item) => {
      if (typeof item === "string") return item;
      try {
        return util.inspect(item, {
          depth: 5,
          breakLength: Infinity,
          maxArrayLength: 200,
          maxStringLength: 10000,
        });
      } catch (error) {
        return String(item);
      }
    })
    .join(" ");
}

const consoleProxy = {
  log: (...args) => pushLine("out", fmtArgs(args)),
  info: (...args) => pushLine("out", fmtArgs(args)),
  debug: (...args) => pushLine("out", fmtArgs(args)),
  error: (...args) => pushLine("err", fmtArgs(args)),
  warn: (...args) => pushLine("err", fmtArgs(args)),
  trace: (...args) => pushLine("err", fmtArgs(args)),
};

function send(message) {
  try {
    process.stdout.write(JSON.stringify(message) + "\n");
  } catch (error) {
    const fallback = {
      type: "done",
      id: message && message.id,
      stdout: "",
      stderr: "",
      error: "\u8fd4\u56de\u7ed3\u679c\u65e0\u6cd5\u5e8f\u5217\u5316\uff1a" + ((error && error.message) || String(error)),
    };
    try { process.stdout.write(JSON.stringify(fallback) + "\n"); } catch (_) { /* 协议行写不出去只能放弃 */ }
  }
}

let seq = 0;
const pending = new Map();
function callTool(name, args) {
  const id = ++seq;
  const promise = new Promise((resolve) => pending.set(id, resolve));
  let payload;
  try {
    payload = args === undefined ? {} : args;
    JSON.stringify(payload);
  } catch (error) {
    pending.delete(id);
    return Promise.resolve({ error: "\u5de5\u5177\u53c2\u6570\u65e0\u6cd5\u5e8f\u5217\u5316\uff1a" + ((error && error.message) || String(error)) });
  }
  send({ type: "tool", id, name, arguments: payload });
  return promise;
}

function onToolResult(message) {
  const resolve = pending.get(message.id);
  if (!resolve) return;
  pending.delete(message.id);
  if (message.ok) resolve(message.value === undefined ? {} : message.value);
  else resolve({ error: message.error || "\u5de5\u5177\u6267\u884c\u5931\u8d25" });
}

function makeTool(name) {
  const fn = (args) => callTool(name, args);
  Object.defineProperty(fn, "name", { value: name });
  return fn;
}

function fmtError(error) {
  if (error === null || error === undefined) return "\u629b\u51fa\u4e86\u7a7a\u503c";
  if (typeof error === "object" && error.stack) {
    const text = String(error.stack);
    return text.length > 4000 ? text.slice(0, 4000) + "\u2026" : text;
  }
  try { return String(error).slice(0, 4000); } catch (_) { return "\u65e0\u6cd5\u5c55\u793a\u7684\u9519\u8bef"; }
}

function safe(value, depth, seen) {
  depth = depth || 0;
  seen = seen || new Set();
  if (value === null) return null;
  const kind = typeof value;
  if (kind === "string" || kind === "boolean") return value;
  if (kind === "number") return Number.isFinite(value) ? value : String(value);
  if (kind === "bigint") return value.toString();
  if (kind === "undefined") return { "$undefined": true };
  if (kind === "symbol") return value.toString();
  if (kind === "function") return "[Function: " + (value.name || "anonymous") + "]";
  if (depth > 6) return "[depth limit]";
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      const items = value.slice(0, 1000).map((item) => safe(item, depth + 1, seen));
      if (value.length > 1000) items.push("[\u2026" + (value.length - 1000) + " more items]");
      return items;
    }
    if (value instanceof Error) {
      return { name: value.name, message: value.message };
    }
    const result = {};
    let count = 0;
    for (const key of Object.keys(value)) {
      if (count >= 1000) { result["\u2026"] = "more keys omitted"; break; }
      result[key] = safe(value[key], depth + 1, seen);
      count += 1;
    }
    return result;
  } catch (error) {
    try { return util.inspect(value, { depth: 3, breakLength: Infinity }).slice(0, 4000); }
    catch (_) { return "[Unserializable]"; }
  } finally {
    seen.delete(value);
  }
}

// 候选尾表达式：用 async 包一层真编译，编得过才当成返回值。
// 以语句关键字开头的直接排除——`const x = 1` 塞进括号里编不过，但先判一次更省事。
function isExpression(text) {
  const value = String(text || "").trim();
  if (!value) return false;
  if (
    /^(?:const|let|var|function|class|async\s+function|if|for|while|do|switch|try|return|throw|import|export|break|continue|debugger|with|\/\/|\/\*)/.test(
      value
    )
  ) {
    return false;
  }
  try {
    new vm.Script("(async () => (" + value + "\n))", { filename: "repl-tail.js" });
    return true;
  } catch (error) {
    return false;
  }
}

// 两行之间是否不能切：上一行以运算符/开括号/逗号收尾，或这一行以闭包/点号等开头。
function continues(prev, next) {
  const tailChar = String(prev || "").replace(/\s+$/, "").slice(-1);
  const headChar = String(next || "").replace(/^\s+/, "").charAt(0);
  if ("+-*/%&|^=<>!?.,:([{".includes(tailChar)) return true;
  if (")]}.?:,+-*/%&|^=<>".includes(headChar)) return true;
  return false;
}

// 扫描顶层语句：只关心深度 0、且行首在普通代码状态的位置。字符串/注释/模板
// 里的内容不算声明，嵌套块里的 let/const 也不是顶层变量。
function analyze(code) {
  const lines = [];
  const separators = [];
  let depth = 0;
  let state = "code";
  let escape = false;
  let lineText = "";
  let lineDepth = 0;
  let lineState = "code";
  const pushLine = () => {
    lines.push({ text: lineText, depth: lineDepth, state: lineState });
    lineText = "";
    lineDepth = depth;
    lineState = state;
  };
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i];
    if (state === "lineComment") {
      if (ch === "\n") { state = "code"; pushLine(); continue; }
      lineText += ch;
      continue;
    }
    if (state === "blockComment") {
      if (ch === "*" && code[i + 1] === "/") { state = "code"; i += 1; continue; }
      if (ch === "\n") pushLine();
      else lineText += ch;
      continue;
    }
    if (state === "sq" || state === "dq" || state === "tpl") {
      if (escape) { escape = false; lineText += ch; continue; }
      if (ch === "\\") { escape = true; lineText += ch; continue; }
      const close = state === "sq" ? "'" : state === "dq" ? '"' : "`";
      if (ch === close) state = "code";
      if (ch === "\n") pushLine();
      else lineText += ch;
      continue;
    }
    if (ch === "/" && code[i + 1] === "/") { state = "lineComment"; i += 1; continue; }
    if (ch === "/" && code[i + 1] === "*") { state = "blockComment"; i += 1; continue; }
    if (ch === "'") { state = "sq"; lineText += ch; continue; }
    if (ch === '"') { state = "dq"; lineText += ch; continue; }
    if (ch === "`") { state = "tpl"; lineText += ch; continue; }
    if (ch === ";" && depth === 0) separators.push(i);
    if ("([{".includes(ch)) depth += 1;
    else if (")]}".includes(ch)) depth = Math.max(0, depth - 1);
    if (ch === "\n") pushLine();
    else lineText += ch;
  }
  pushLine();

  const names = [];
  const seen = new Set();
  const addName = (name) => { if (name && !seen.has(name)) { seen.add(name); names.push(name); } };
  for (const line of lines) {
    if (line.depth !== 0 || line.state !== "code") continue;
    const text = line.text.trim();
    let match = /^(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=/.exec(text);
    if (match) { addName(match[1]); continue; }
    match = /^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/.exec(text);
    if (match) { addName(match[1]); continue; }
    match = /^class\s+([A-Za-z_$][\w$]*)/.exec(text);
    if (match) { addName(match[1]); }
  }

  // 最后一条顶层语句作为返回值（Claude 的「最后一条表达式即结果」）。
  // 先按顶层的 `;` 切（`const r = await f(); r` 这种一行两段最常见）；没有分号时
  // 再按行切，并用「上一行以运算符/开括号结尾、或这一行以闭包/点号开头」判断续行，
  // 避免把 `1 +\n2` 拆成返回值 2。候选一律用 async 包一层真编译验证，编不过就不返回。
  let tail = null;
  let tailStart = code.length;
  if (separators.length) {
    let start = 0;
    const segments = [];
    for (const pos of separators) {
      segments.push({ start, text: code.slice(start, pos) });
      start = pos + 1;
    }
    segments.push({ start, text: code.slice(start) });
    for (let i = segments.length - 1; i >= 0; i -= 1) {
      const seg = segments[i];
      const text = seg.text.trim();
      if (!text) continue;
      if (isExpression(text)) {
        tail = text;
        tailStart = seg.start + seg.text.indexOf(text);
      }
      break;
    }
  } else {
    const starts = [];
    let offset = 0;
    for (const raw of code.split("\n")) {
      starts.push(offset);
      offset += raw.length + 1;
    }
    const rawLines = code.split("\n");
    let last = rawLines.length - 1;
    while (last >= 0 && !rawLines[last].trim()) last -= 1;
    if (last >= 0) {
      let begin = last;
      let candidate = rawLines.slice(begin).join("\n");
      while (
        begin > 0 &&
        (!isExpression(candidate) || continues(rawLines[begin - 1], rawLines[begin]))
      ) {
        begin -= 1;
        candidate = rawLines.slice(begin).join("\n");
      }
      if (isExpression(candidate)) {
        tail = candidate.trim();
        const line = rawLines[begin];
        tailStart = starts[begin] + line.indexOf(line.trimStart());
      }
    }
  }
  return { names, tail, tailStart };
}

function wrapAsync(code) {
  const info = analyze(code);
  let body = code;
  let ret = "";
  if (info.tail !== null) {
    body = code.slice(0, info.tailStart);
    ret = "\nreturn (" + info.tail + ");";
  }
  const assign = info.names.length
    ? "\n;try{Object.assign(globalThis, {" + info.names.join(", ") + "})}catch(__e){}"
    : "";
  return "(async () => {\n" + body + "\n" + assign + ret + "\n})()";
}

let sandbox = null;
let ready = false;

function initContext(tools) {
  const base = { console: consoleProxy, o: {} };
  for (const name of tools) base[name] = makeTool(name);
  base.setTimeout = (fn, ms, ...args) => {
    const delay = Math.max(0, Math.min(Number(ms) || 0, 600000));
    return setTimeout(fn, delay, ...args);
  };
  base.clearTimeout = clearTimeout;
  base.setInterval = (fn, ms, ...args) => {
    const delay = Math.max(1, Math.min(Number(ms) || 1, 600000));
    return setInterval(fn, delay, ...args);
  };
  base.clearInterval = clearInterval;
  base.queueMicrotask = queueMicrotask;
  base.structuredClone = structuredClone;
  base.TextEncoder = TextEncoder;
  base.TextDecoder = TextDecoder;
  base.URL = URL;
  base.URLSearchParams = URLSearchParams;
  sandbox = vm.createContext(base, { name: "blank-repl" });
  ready = true;
}

function isTopLevelAwait(error) {
  return (
    error &&
    error.name === "SyntaxError" &&
    /await is only valid in async functions/.test(String(error.message || ""))
  );
}

async function evaluate(message) {
  resetBuffers();
  currentEval = Number(message.id) || 0;
  const code = String(message.code === undefined ? "" : message.code);
  const timeoutMs = Math.max(1, Number(message.timeout_ms) || 30000);
  let value;
  let errorText;
  let vmTimedOut = false;
  try {
    let script;
    try {
      script = new vm.Script(code, { filename: "repl.js" });
    } catch (compileError) {
      if (!isTopLevelAwait(compileError)) throw compileError;
      script = new vm.Script(wrapAsync(code), { filename: "repl-await.js" });
    }
    value = script.runInContext(sandbox, { timeout: timeoutMs });
    if (value && typeof value.then === "function") value = await value;
  } catch (error) {
    errorText = fmtError(error);
    vmTimedOut =
      !!error &&
      (error.code === "ERR_SCRIPT_EXECUTION_TIMEOUT" ||
        /Script execution timed out/.test(String(error.message || "")));
  }
  currentEval = 0;
  const payload = {
    type: "done",
    id: message.id,
    stdout: outBuf.join("\n"),
    stderr: errBuf.join("\n"),
  };
  if (truncated) payload.truncated = true;
  if (vmTimedOut) payload.timed_out = true;
  if (errorText !== undefined) payload.error = errorText;
  else if (value !== undefined) payload.result = safe(value);
  return payload;
}

// 密封上下文里的动态 import() 会异步抛错（没有 import 回调）。没有这道兜底，
// Node 会按未处理异常直接杀掉整个会话，连"哪段脚本干的"都留不下来。
let currentEval = 0;
function fatal(error, prefix) {
  send({ type: "fatal", id: currentEval || 0, error: prefix + fmtError(error) });
}
process.on("unhandledRejection", (error) => fatal(error, "脚本里有未处理的 Promise 拒绝："));
process.on("uncaughtException", (error) => fatal(error, "脚本抛出了未捕获的异常："));

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let queue = Promise.resolve();
rl.on("line", (line) => {
  let message;
  try { message = JSON.parse(line); } catch (_) { return; }
  if (message.type === "init") {
    try { initContext(Array.isArray(message.tools) ? message.tools : []); send({ type: "ready" }); }
    catch (error) { send({ type: "fatal", error: fmtError(error) }); }
    return;
  }
  if (message.type === "tool_result") { onToolResult(message); return; }
  if (message.type !== "eval") return;
  queue = queue.then(async () => {
    if (!ready || !sandbox) {
      send({ type: "done", id: message.id, stdout: "", stderr: "", error: "REPL 会话没有初始化" });
      return;
    }
    const payload = await evaluate(message);
    send(payload);
  }).catch((error) => {
    send({ type: "done", id: message.id, stdout: "", stderr: "", error: fmtError(error) });
  });
});
rl.on("close", () => process.exit(0));
"""


class ReplSession:
    """一个任务一个 Node 进程；同一时刻只跑一次 eval。"""

    def __init__(self, task_id: str, node: str) -> None:
        self.task_id = str(task_id or "")
        self.node = node
        self.process: Any = None
        self.temp_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._stderr_task: asyncio.Task | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self.tool_calls = 0
        self.executions = 0

    # ---------------------------------------------------------------- lifecycle
    async def ensure_started(self, root: Path, tools: list[str]) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        await self._reset()
        self.temp_dir = Path(tempfile.mkdtemp(prefix=f"blank-repl-{_safe_name(self.task_id)}-"))
        runner = self.temp_dir / "repl_runner.js"
        runner.write_text(_RUNNER_JS, encoding="utf-8", newline="\n")
        env = {key: value for key, value in os.environ.items() if key != "NODE_OPTIONS"}
        env["NODE_NO_WARNINGS"] = "1"
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.node,
                "--permission",
                f"--allow-fs-read={self.temp_dir}",
                "--disable-proto=throw",
                "--no-warnings",
                str(runner),
                cwd=str(self.temp_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as error:
            raise ReplError(f"启动 Node REPL 失败：{error}") from error
        self._stderr_lines.clear()
        self._stderr_task = asyncio.create_task(self._pump_stderr())
        await self._send({"type": "init", "tools": tools, "cwd": str(root)})
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=START_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as error:
            await self._reset()
            raise ReplError("Node REPL 启动超时") from error
        message = _decode_line(line)
        if message is None or message.get("type") != "ready":
            detail = "；".join(self._stderr_lines) or (message or {}).get("error") or "没有收到 ready"
            await self._reset()
            raise ReplError(f"Node REPL 启动失败：{detail}")
        self.executions = 0
        self.tool_calls = 0

    async def _pump_stderr(self) -> None:
        stderr = getattr(self.process, "stderr", None)
        if stderr is None:
            return
        try:
            while True:
                chunk = await stderr.readline()
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_lines.append(text[:1000])
        except (asyncio.CancelledError, ValueError):
            return

    async def _send(self, message: dict) -> None:
        process = self.process
        stdin = getattr(process, "stdin", None)
        if stdin is None or stdin.is_closing():
            raise ReplError("REPL 进程已经退出")
        try:
            stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ReplError("REPL 进程已经退出") from error

    # -------------------------------------------------------------------- eval
    async def evaluate(
        self,
        code: str,
        timeout_ms: int,
        runner: ToolRunner,
        *,
        root: Path,
        tools: list[str],
    ) -> dict:
        async with self._lock:
            await self.ensure_started(root, tools)
            self._seq += 1
            eval_id = self._seq
            self.executions += 1
            self.tool_calls = 0
            await self._send({"type": "eval", "id": eval_id, "code": code, "timeout_ms": timeout_ms})
            deadline = time.monotonic() + timeout_ms / 1000 + DEADLINE_GRACE_SECONDS
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ReplTimeout("REPL 调用超时")
                    try:
                        line = await asyncio.wait_for(
                            self.process.stdout.readline(), timeout=remaining
                        )
                    except asyncio.TimeoutError as error:
                        raise ReplTimeout("REPL 调用超时") from error
                    message = _decode_line(line)
                    if message is None:
                        if not line:
                            detail = "；".join(self._stderr_lines) or "进程已退出"
                            raise ReplError(f"REPL 进程意外退出：{detail}")
                        continue
                    kind = str(message.get("type") or "")
                    if kind == "tool":
                        self.tool_calls += 1
                        task = asyncio.create_task(self._answer_tool(message, runner))
                        self._tool_tasks.add(task)
                        task.add_done_callback(self._tool_tasks.discard)
                        continue
                    if kind == "done" and int(message.get("id") or 0) == eval_id:
                        return message
                    if kind == "fatal":
                        fatal_id = int(message.get("id") or 0)
                        if fatal_id != eval_id:
                            # 空闲期/上一次调用遗留下来的异步异常：进程还活着，
                            # 不能把它记到这次调用的账上。
                            continue
                        raise ReplError(f"REPL 会话失败：{message.get('error') or ''}")
            except ReplTimeout:
                # 卡死的脚本杀进程是唯一可靠的中断：Node 的 vm 超时只看同步段。
                await self._reset()
                raise
            except ReplError:
                await self._reset()
                raise
            finally:
                if self._tool_tasks:
                    for task in list(self._tool_tasks):
                        task.cancel()
                    await asyncio.gather(*self._tool_tasks, return_exceptions=True)
                    self._tool_tasks.clear()

    async def _answer_tool(self, message: dict, runner: ToolRunner) -> None:
        call_id = message.get("id")
        name = str(message.get("name") or "")
        arguments = message.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            result = await runner(name, arguments)
        except Exception as error:  # noqa: BLE001 - 工具异常要回到脚本里，而不是打死会话
            result = {"ok": False, "status": "error", "message": f"内层工具调用失败：{error}"}
        payload = {
            "type": "tool_result",
            "id": call_id,
            "ok": bool(result.get("ok")),
            "value": result,
        }
        if not result.get("ok"):
            payload["error"] = str(result.get("message") or result.get("status") or "工具执行失败")
        try:
            await self._send(payload)
        except ReplError:
            # 进程已经死了：回执发不回去，脚本那份结果由 done/error 路径交代。
            return

    # ------------------------------------------------------------------ closing
    async def _reset(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None

    async def close(self) -> None:
        async with self._lock:
            await self._reset()


def _safe_name(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_")[:40]
    return cleaned or "task"


def _decode_line(line: bytes) -> dict | None:
    if not line:
        return None
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


_SESSIONS: dict[str, ReplSession] = {}
_SESSION_LOCK = asyncio.Lock()


async def get_session(task_id: str, node: str) -> ReplSession:
    key = str(task_id or "")
    async with _SESSION_LOCK:
        session = _SESSIONS.get(key)
        if session is None or session.node != node:
            if session is not None:
                await session.close()
            session = ReplSession(key, node)
            _SESSIONS[key] = session
        return session


async def aclose(task_id: str, reason: str = "") -> bool:
    """任务收尾：杀掉该任务的 REPL 进程并摘掉句柄。"""
    key = str(task_id or "")
    async with _SESSION_LOCK:
        session = _SESSIONS.pop(key, None)
    if session is None:
        return False
    await session.close()
    return True


def session_count() -> int:
    return len(_SESSIONS)


__all__ = [
    "MAX_CODE_CHARS",
    "MAX_ERROR_CHARS",
    "MAX_STREAM_CHARS",
    "REPL_DEFAULT_TIMEOUT_MS",
    "REPL_MAX_TIMEOUT_MS",
    "ReplError",
    "ReplSession",
    "ReplTimeout",
    "aclose",
    "get_session",
    "session_count",
]
