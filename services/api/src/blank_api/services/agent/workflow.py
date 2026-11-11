"""Workflow：用一段 JavaScript 确定性编排多个子代理（对齐 Claude Workflow）。

Claude 的 Workflow 是「脚本即编排」：脚本开头是纯字面量的 ``meta``，正文用
``agent() / parallel() / pipeline() / phase()`` 描述谁在什么阶段做什么。这里移植的
是同一套判据，落点分三层：

* **脚本层**：只接受 ``export const meta = {...}`` 开头的纯字面量；正文被包进一个
  async 函数，因此顶层 ``await`` 与 ``return`` 都成立（Claude 的示例就是这么写的）。
* **执行层**：脚本跑在独立的 Node 子进程里，只通过 stdout/stdin 的 JSON 行协议
  与宿主通信；``agent()`` 转成既有的后台子代理（``spawn_background`` + ``output``），
  工具白名单、隔离副本、用量账、事件流全部沿用，不另造一套执行器。
* **边界**：Node 以 ``--permission`` 启动，禁掉文件写入/子进程/worker；网络入口在
  预置层被替换成报错（权限模型本身不管网络，沙箱档位再兜一层）。脚本是模型写的，
  不是用户写的，所以这个工具按高风险处理、走审批，并且只在用户明确要求多代理
  编排时允许调用。

后台语义与后台 shell / 后台子代理一致：启动即返回 ``wf_...`` 句柄，结束由系统在
下一次模型调用前通知，结果用 ``task_output`` 取、``task_stop`` 取消；任务收尾时
仍在跑的工作流会被 ``aclose`` 收走。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.log_safety import redact

# Claude 的 script.maxLength；超过就不该把整段脚本塞进一次工具调用。
MAX_SCRIPT_CHARS = 524_288
# 结果体量的硬上限：一行 JSON 进内存，不能让它无限增长。
MAX_RESULT_CHARS = 1_000_000
# 通知/工具回执里展示的结果长度。
OUTPUT_CHARS = 20_000
# 默认「中等规模」：Claude 的会话指引是 10 个以内；这里是安全上限，不是指引。
GUIDELINE_AGENTS = 10
MAX_AGENTS = 50
# 单个子代理的等待上限与整条工作流的上限：后台跑不等于可以永远挂着。
AGENT_TIMEOUT_SECONDS = 900.0
WORKFLOW_TIMEOUT_SECONDS = 3600.0
# 同时向模型派出的子代理数：与 fan_out 的上限一致，避免一次脚本把并发打满。
MAX_INFLIGHT_AGENTS = 4
# 事件里保留的日志条数（脚本 console.log 不占模型上下文，但要能排错）。
MAX_LOG_LINES = 200

SCRIPT_DIR = Path(".blank") / "workflows"
RUNS_SUBDIR = ".runs"
EXCLUDE_LINE = "/.blank/workflows/.runs/"
_ID_PREFIX = "wf_"
RESUME_PATTERN = re.compile(r"^wf_[a-z0-9-]{6,}$")

# 用户明确要求多代理编排的说法。只在用户自己的目标里找，不从任务内容反推。
OPT_IN_WORDS = (
    "ultracode",
    "workflow",
    "工作流",
    "多代理",
    "多 agent",
    "多智能体",
    "fan out",
    "fan-out",
    "orchestrate",
    "编排",
    "子代理群",
)

_RUNS: dict[str, dict[str, "WorkflowRun"]] = {}
# 已完成运行的 agent() 结果缓存：同一进程内才能续跑，与 Claude 的 same-session 一致。
_CACHE: dict[str, dict[str, dict]] = {}


class WorkflowError(ValueError):
    """脚本或编排层面的错误：消息直接回给模型，必须能指导下一步。"""


# --------------------------------------------------------------------------- #
# meta：纯字面量解析
# --------------------------------------------------------------------------- #
class _LiteralParser:
    """只认 JS 字面量的递归下降解析器。

    ``meta`` 必须是纯字面量（Claude 原文）：变量、函数调用、模板插值一律拒绝。
    拒绝得早，比脚本跑到一半才发现名字是 ``undefined`` 有用。
    """

    def __init__(self, text: str, pos: int = 0) -> None:
        self.text = text
        self.pos = pos

    def _skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            char = text[self.pos]
            if char in " \t\r\n":
                self.pos += 1
                continue
            if text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end + 1
                continue
            if text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    self._error("注释没有结束")
                self.pos = end + 2
                continue
            break

    def _error(self, message: str) -> None:
        raise WorkflowError(f"meta 不是纯字面量：{message}（第 {self.pos + 1} 个字符）")

    def parse_value(self) -> Any:
        self._skip()
        if self.pos >= len(self.text):
            self._error("内容提前结束")
        char = self.text[self.pos]
        if char == "{":
            return self._parse_object()
        if char == "[":
            return self._parse_array()
        if char in "\"'":
            return self._parse_string()
        if char == "-" or char.isdigit():
            return self._parse_number()
        word = self._identifier()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "null":
            return None
        self.pos -= len(word)
        self._error(f"不认识的取值 {word!r}；只允许字符串/数字/布尔/null/对象/数组")

    def _identifier(self) -> str:
        match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", self.text[self.pos :])
        if match is None:
            self._error("这里需要标识符")
        self.pos += len(match.group(0))
        return match.group(0)

    def _parse_object(self) -> dict:
        result: dict[str, Any] = {}
        self.pos += 1
        while True:
            self._skip()
            if self.pos >= len(self.text):
                self._error("对象没有闭合")
            if self.text[self.pos] == "}":
                self.pos += 1
                return result
            if self.text[self.pos] in "\"'":
                key = self._parse_string()
            else:
                key = self._identifier()
            self._skip()
            if self.pos >= len(self.text) or self.text[self.pos] != ":":
                self._error(f"键 {key!r} 后面缺少冒号")
            self.pos += 1
            result[key] = self.parse_value()
            self._skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.text) and self.text[self.pos] == "}":
                continue
            self._error("对象里缺少逗号或右花括号")

    def _parse_array(self) -> list:
        result: list[Any] = []
        self.pos += 1
        while True:
            self._skip()
            if self.pos >= len(self.text):
                self._error("数组没有闭合")
            if self.text[self.pos] == "]":
                self.pos += 1
                return result
            result.append(self.parse_value())
            self._skip()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.text) and self.text[self.pos] == "]":
                continue
            self._error("数组里缺少逗号或右方括号")

    def _parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        chunks: list[str] = []
        escapes = {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "b": "\b",
            "f": "\f",
            "v": "\v",
            "0": "\0",
            "\\": "\\",
            "/": "/",
            "'": "'",
            '"': '"',
        }
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == quote:
                self.pos += 1
                return "".join(chunks)
            if char in "\r\n":
                self._error("字符串里不能直接换行")
            if char != "\\":
                chunks.append(char)
                self.pos += 1
                continue
            self.pos += 1
            if self.pos >= len(self.text):
                self._error("转义没有结束")
            code = self.text[self.pos]
            self.pos += 1
            if code in escapes:
                chunks.append(escapes[code])
            elif code == "u":
                hex_digits = self.text[self.pos : self.pos + 4]
                if len(hex_digits) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", hex_digits):
                    self._error("\\u 转义需要 4 位十六进制")
                chunks.append(chr(int(hex_digits, 16)))
                self.pos += 4
            elif code == "x":
                hex_digits = self.text[self.pos : self.pos + 2]
                if len(hex_digits) != 2 or not re.fullmatch(r"[0-9a-fA-F]{2}", hex_digits):
                    self._error("\\x 转义需要 2 位十六进制")
                chunks.append(chr(int(hex_digits, 16)))
                self.pos += 2
            else:
                self._error(f"不支持的转义 \\{code}")
        self._error("字符串没有闭合")

    def _parse_number(self) -> Any:
        match = re.match(
            r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",
            self.text[self.pos :],
        )
        if match is None:
            self._error("数字格式不对")
        raw = match.group(0)
        self.pos += len(raw)
        if any(char in raw for char in ".eE"):
            return float(raw)
        return int(raw)


_META_HEAD = re.compile(
    r"\s*(?:(?://[^\n]*\n)|(?:/\*.*?\*/\s*))*export\s+const\s+meta\s*=\s*",
    re.S,
)


def parse_meta(script: str) -> tuple[dict, str]:
    """拆出 meta 与正文。返回 ``(meta, body)``，两者都做过基本校验。"""
    if not isinstance(script, str) or not script.strip():
        raise WorkflowError("script 不能为空")
    text = script.lstrip("\ufeff")
    if "\x00" in text:
        raise WorkflowError("脚本里不能有 NUL 字符")
    if len(text) > MAX_SCRIPT_CHARS:
        raise WorkflowError(
            f"脚本超过长度上限 {MAX_SCRIPT_CHARS} 字符（当前 {len(text)}）"
        )
    match = _META_HEAD.match(text)
    if match is None:
        raise WorkflowError(
            "脚本必须以 export const meta = { name, description, phases? } 开头"
            "（纯字面量，不要变量/调用/插值）"
        )
    parser = _LiteralParser(text, match.end())
    raw_meta = parser.parse_value()
    if not isinstance(raw_meta, dict):
        raise WorkflowError("meta 必须是对象：{ name, description, phases? }")
    unknown = sorted(set(raw_meta) - {"name", "description", "phases"})
    if unknown:
        raise WorkflowError(f"meta 里不认识的字段：{', '.join(unknown)}")
    name = str(raw_meta.get("name") or "").strip()
    if not name:
        raise WorkflowError("meta.name 不能为空")
    if len(name) > 100:
        raise WorkflowError("meta.name 最多 100 个字符")
    description = str(raw_meta.get("description") or "").strip()
    if not description:
        raise WorkflowError("meta.description 不能为空（会显示在审批卡上）")
    if "\n" in description or "\r" in description:
        raise WorkflowError("meta.description 必须是一行")
    if len(description) > 240:
        raise WorkflowError("meta.description 最多 240 个字符")
    phases: list[dict] = []
    raw_phases = raw_meta.get("phases")
    if raw_phases is not None:
        if not isinstance(raw_phases, list):
            raise WorkflowError("meta.phases 必须是数组")
        if len(raw_phases) > 50:
            raise WorkflowError("meta.phases 最多 50 个阶段")
        seen: set[str] = set()
        for index, item in enumerate(raw_phases, start=1):
            if not isinstance(item, dict):
                raise WorkflowError(f"meta.phases 第 {index} 项必须是对象 {{ title, detail? }}")
            extra = sorted(set(item) - {"title", "detail"})
            if extra:
                raise WorkflowError(
                    f"meta.phases 第 {index} 项有不认识的字段：{', '.join(extra)}"
                )
            title = str(item.get("title") or "").strip()
            if not title:
                raise WorkflowError(f"meta.phases 第 {index} 项缺少 title")
            if len(title) > 80:
                raise WorkflowError(f"meta.phases 第 {index} 项的 title 最多 80 个字符")
            if title in seen:
                raise WorkflowError(f"meta.phases 里 title 重复：{title}")
            seen.add(title)
            detail = item.get("detail")
            if detail is not None:
                detail = str(detail).strip()
                if len(detail) > 240:
                    raise WorkflowError(f"meta.phases 第 {index} 项的 detail 最多 240 个字符")
            phases.append({"title": title, **({"detail": detail} if detail else {})})

    meta = {"name": name, "description": description}
    if phases:
        meta["phases"] = phases
    end = parser.pos
    tail = text[end:]
    if tail.startswith(";"):
        end += 1
        tail = text[end:]
    return meta, tail


# --------------------------------------------------------------------------- #
# Node 运行时探测（K 批口径：缺什么就说清缺什么，并给出补救方式）
# --------------------------------------------------------------------------- #
def find_node() -> str:
    """找到可执行的 Node。返回空串表示本机没有，由工具层如实说明。"""
    candidate = str(os.environ.get("BLANK_NODE_BIN") or "").strip()
    if candidate and Path(candidate).is_file():
        return candidate
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    if sys.platform == "win32":
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        extra = [
            Path(roots[0]) / "nodejs" / "node.exe",
            Path(roots[1]) / "nodejs" / "node.exe",
            Path(roots[2]) / "Programs" / "nodejs" / "node.exe" if roots[2] else None,
        ]
        for path in extra:
            if path is not None and path.is_file():
                return str(path)
    return ""


# --------------------------------------------------------------------------- #
# 脚本落盘与工作区卫生
# --------------------------------------------------------------------------- #
def _git_dir(root: Path) -> Path | None:
    dot_git = Path(root) / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            return target if target.is_absolute() else (Path(root) / target)
    return None


def ensure_excluded(root: Path) -> None:
    """把运行期脚本目录写进仓库本地 exclude：不碰用户的 .gitignore。"""
    git_dir = _git_dir(root)
    if git_dir is None:
        return
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if any(line.strip() == EXCLUDE_LINE for line in existing.splitlines()):
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8", newline="\n") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(EXCLUDE_LINE + "\n")
    except OSError:
        # exclude 写不进去只影响 git status 的干净程度，不该阻断工作流本身。
        pass


def persist_script(root: Path, workflow_id: str, script: str) -> str:
    directory = Path(root) / SCRIPT_DIR / RUNS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{workflow_id}.mjs"
    path.write_text(script, encoding="utf-8", newline="\n")
    ensure_excluded(Path(root))
    return f".blank/workflows/{RUNS_SUBDIR}/{workflow_id}.mjs"


def read_script_path(root: Path, relative: str) -> str:
    from blank_api.services.workspace import resolve_inside

    value = str(relative or "").strip()
    if not value:
        raise WorkflowError("scriptPath 不能为空")
    if Path(value).is_absolute():
        raise WorkflowError("scriptPath 必须是工作区相对路径，不接受绝对路径")
    if Path(value).suffix.lower() not in {".mjs", ".js"}:
        raise WorkflowError("scriptPath 必须是 .mjs 或 .js 文件")
    try:
        target = resolve_inside(Path(root), value)
    except Exception as error:  # noqa: BLE001 - 统一转成可读的脚本错误
        raise WorkflowError(f"scriptPath 不可用：{error}") from error
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkflowError(f"读不了 scriptPath：{error}") from error
    return text


def named_scripts(root: Path) -> list[str]:
    directory = Path(root) / SCRIPT_DIR
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.mjs") if path.is_file())


def load_named_script(root: Path, name: str) -> str:
    value = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value):
        raise WorkflowError("工作流名字只允许字母、数字、点、下划线和短横线（最多 80 字符）")
    path = Path(root) / SCRIPT_DIR / f"{value}.mjs"
    if not path.is_file():
        available = named_scripts(root)
        hint = "、".join(available) if available else f"（{SCRIPT_DIR.as_posix()}/ 下没有脚本）"
        raise WorkflowError(f"找不到预定义工作流 {value}；可用：{hint}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 脚本 → 可执行模块
# --------------------------------------------------------------------------- #
_PRELUDE = r"""
const __meta = __META__;
const meta = __meta;
const args = __ARGS__;
const __stdout = process.stdout.write.bind(process.stdout);
let __seq = 0;
const __pending = new Map();
function __emit(message) {
  try { __stdout(JSON.stringify(message) + "\n"); } catch (error) { /* 协议行写不出去时只能放弃 */ }
}
function __emitFinal(message, code) {
  const line = JSON.stringify(message) + "\n";
  __stdout(line, () => process.exit(code));
}
function __rpc(type, payload) {
  return new Promise((resolve, reject) => {
    const id = ++__seq;
    __pending.set(id, { resolve, reject });
    __emit({ type, id, ...payload });
  });
}
function agent(prompt, options = {}) {
  return __rpc("agent", {
    prompt: String(prompt),
    options: options && typeof options === "object" ? options : {},
  });
}
function phase(title) {
  const value = String(title);
  if (__phaseTitles.size > 0 && !__phaseTitles.has(value)) {
    throw new Error(`未知阶段 ${value}：meta.phases 里没有这个标题`);
  }
  __emit({ type: "phase", title: value });
}
async function parallel(items) {
  if (!Array.isArray(items)) throw new Error("parallel() 需要一个数组");
  return await Promise.all(items.map((item) => (typeof item === "function" ? item() : Promise.resolve(item))));
}
async function pipeline(items, ...stages) {
  if (!Array.isArray(items)) throw new Error("pipeline() 需要一个数组");
  if (stages.some((stage) => typeof stage !== "function")) {
    throw new Error("pipeline() 的每一段都必须是函数");
  }
  return await Promise.all(items.map(async (item) => {
    let value = item;
    for (const stage of stages) value = await stage(value);
    return value;
  }));
}
const __phaseTitles = new Set((__meta.phases || []).map((item) => item.title));
for (const name of ["log", "info", "debug", "warn", "error"]) {
  console[name] = (...parts) => {
    const text = parts.map((part) => {
      if (typeof part === "string") return part;
      try { return JSON.stringify(part); } catch (error) { return String(part); }
    }).join(" ");
    __emit({ type: "log", level: name, text: text.slice(0, 4000) });
  };
}
process.stdout.write = function (chunk) {
  const text = typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8");
  __emit({ type: "log", level: "stdout", text: text.slice(0, 4000) });
  return true;
};
for (const name of ["fetch", "WebSocket", "XMLHttpRequest", "EventSource"]) {
  try {
    Object.defineProperty(globalThis, name, {
      value: () => { throw new Error("工作流脚本不能直接联网；需要资料请交给 agent() 子代理"); },
      configurable: false,
      writable: false,
    });
  } catch (error) { /* 已存在的内建属性在部分版本里不可改写，权限模型仍是主边界 */ }
}
const __readline = await import("node:readline");
const __rl = __readline.createInterface({ input: process.stdin });
__rl.on("line", (line) => {
  let message;
  try { message = JSON.parse(line); } catch (error) { return; }
  if (message.type !== "agent.result" && message.type !== "agent.error") return;
  const entry = __pending.get(message.id);
  if (!entry) return;
  __pending.delete(message.id);
  if (message.type === "agent.result") entry.resolve(message.value);
  else entry.reject(new Error(message.message || "子代理失败"));
});
function __jsonSafe(value) {
  if (value === undefined) return null;
  try { return JSON.parse(JSON.stringify(value, (key, item) => (typeof item === "bigint" ? item.toString() : item))); }
  catch (error) { throw new Error("工作流返回值不能序列化成 JSON：" + error.message); }
}
async function __main() {
__BODY__
}
try {
  const __result = __jsonSafe(await __main());
  __emitFinal({ type: "done", result: __result === undefined ? null : __result }, 0);
} catch (error) {
  __emitFinal({
    type: "error",
    message: String((error && error.message) || error),
    stack: String((error && error.stack) || "").slice(0, 8000),
  }, 1);
}
"""


_META_TOKEN = "\x00META\x00"
_ARGS_TOKEN = "\x00ARGS\x00"
_BODY_TOKEN = "\x00BODY\x00"


def build_module(meta: dict, body: str, args: Any) -> str:
    """把已校验的 meta 与正文拼成一个 ESM 模块（正文包进 async 函数）。

    用 NUL 包住的占位符：JSON 序列化会把 NUL 转义成 ``\\u0000``，正文里又
    明确拒绝裸 NUL，所以三段内容不会互相替换到对方里面去。
    """
    template = (
        _PRELUDE.replace("__META__", _META_TOKEN)
        .replace("__ARGS__", _ARGS_TOKEN)
        .replace("__BODY__", _BODY_TOKEN)
    )
    return (
        template.replace(_META_TOKEN, json.dumps(meta, ensure_ascii=False))
        .replace(_ARGS_TOKEN, json.dumps(args, ensure_ascii=False))
        .replace(_BODY_TOKEN, body)
    )


# --------------------------------------------------------------------------- #
# 运行状态
# --------------------------------------------------------------------------- #
@dataclass
class WorkflowRun:
    workflow_id: str
    task_id: str
    name: str
    description: str
    meta: dict
    script_path: str
    status: str = "running"
    result: Any = None
    error: str = ""
    stack: str = ""
    phases: list[str] = field(default_factory=list)
    agents: int = 0
    cached: int = 0
    logs: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float | None = None
    notified: bool = False
    process: Any = None
    task: asyncio.Task | None = None
    agent_ids: set[str] = field(default_factory=set)
    finished_agents: set[str] = field(default_factory=set)
    inflight: dict[int, asyncio.Task] = field(default_factory=dict)
    cache: dict[str, dict] | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.perf_counter()
        start = self.started_at or end
        return round(max(0.0, end - start), 2)

    def result_text(self) -> str:
        if self.result is None:
            return ""
        try:
            text = json.dumps(self.result, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(self.result)
        return redact(text[:OUTPUT_CHARS])

    def snapshot(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "ok": self.status == "completed",
            "result": self.result_text(),
            "error": self.error,
            "phases": list(self.phases),
            "agents": self.agents,
            "cached": self.cached,
            "elapsed_seconds": self.elapsed,
            "script_path": self.script_path,
            "logs": list(self.logs[-20:]),
        }


def new_id() -> str:
    return _ID_PREFIX + uuid.uuid4().hex[:12]


def _bucket(task_id: str) -> dict[str, WorkflowRun]:
    return _RUNS.setdefault(str(task_id or ""), {})


def get_run(task_id: str, workflow_id: str) -> WorkflowRun | None:
    return (_RUNS.get(str(task_id or "")) or {}).get(str(workflow_id or "").strip())


async def _record(run: WorkflowRun, recorder: Any, event: str, payload: dict) -> None:
    if recorder is None:
        return
    try:
        await recorder.add(event, "workflow", {"workflow_id": run.workflow_id, **payload})
    except Exception:  # 事件是旁路，不能把工作流本身带崩
        pass


def _call_key(prompt: str, options: dict) -> str:
    raw = json.dumps(
        {"prompt": prompt, "options": options},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _agent_goal(prompt: str, schema: Any) -> str:
    if not schema:
        return prompt
    try:
        rendered = json.dumps(schema, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(schema)
    return (
        f"{prompt}\n\n"
        "完成后只输出一个符合下面 JSON Schema 的 JSON 值：不要写解释、不要加 markdown 围栏。\n"
        f"{rendered}"
    )


_SCHEMA_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _validate_schema(value: Any, schema: Any, path: str = "$") -> None:
    """按需校验结构化结果的骨架（type/required/properties）。

    不做完整 JSON Schema：工作流要的是「模型有没有按约定给形状」，
    不是把一份第三方 schema 实现搬进来。
    """
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _SCHEMA_TYPES:
        want = _SCHEMA_TYPES[expected]
        if expected == "integer" and isinstance(value, bool):
            raise WorkflowError(f"结构化结果 {path} 应为整数，实际是布尔")
        if expected == "number" and isinstance(value, bool):
            raise WorkflowError(f"结构化结果 {path} 应为数字，实际是布尔")
        if not isinstance(value, want):
            raise WorkflowError(f"结构化结果 {path} 类型不符：期望 {expected}")
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [str(key) for key in required if str(key) not in value]
            if missing:
                raise WorkflowError(f"结构化结果 {path} 缺少字段：{', '.join(missing)}")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child in properties.items():
                if key in value:
                    _validate_schema(value[key], child, f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], f"{path}[{index}]")


def parse_structured(text: str, schema: Any) -> Any:
    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    candidates = [body]
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            value = None
            for index, char in enumerate(candidate):
                if char not in "{[":
                    continue
                try:
                    value, _ = decoder.raw_decode(candidate[index:])
                except ValueError:
                    continue
                break
        if value is not None:
            _validate_schema(value, schema)
            return value
    raise WorkflowError("子代理没有返回可解析的 JSON（要求过 schema）")


def _agent_options(options: Any) -> dict:
    return options if isinstance(options, dict) else {}


def _max_steps(options: dict) -> int:
    raw = options.get("maxSteps", options.get("max_steps", 4))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 4
    return max(1, min(8, value))


async def _wait_agent(runner: Any, agent_id: str) -> dict:
    deadline = time.monotonic() + AGENT_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if hasattr(runner, "stop"):
                try:
                    await runner.stop(agent_id)
                except Exception:  # noqa: BLE001 - 超时收尸失败也要把超时报出来
                    pass
            raise WorkflowError(f"子代理 {agent_id} 超过 {int(AGENT_TIMEOUT_SECONDS)} 秒没有结束")
        snapshot = await runner.output(
            agent_id, block=True, timeout=min(30.0, max(0.0, remaining))
        )
        if str(snapshot.get("status") or "") != "running":
            return snapshot


async def _run_agent(
    run: WorkflowRun, runner: Any, recorder: Any, request_id: int, payload: dict
) -> Any:
    prompt = str(payload.get("prompt") or "")
    options = _agent_options(payload.get("options"))
    key = _call_key(prompt, options)
    if run.cache is not None and key in run.cache:
        run.cached += 1
        cached = run.cache[key]
        await _record(run, recorder, "workflow.agent_cached", {
            "label": str(options.get("label") or ""),
            "phase": str(options.get("phase") or ""),
            "chars": len(cached.get("value")) if isinstance(cached.get("value"), str) else 0,
        })
        return cached.get("value")

    if run.agents >= MAX_AGENTS:
        raise WorkflowError(
            f"工作流最多派 {MAX_AGENTS} 个子代理；需要更大规模请拆成多次运行"
        )
    run.agents += 1
    label = str(options.get("label") or f"agent-{run.agents}")
    phase = str(options.get("phase") or "")
    await _record(run, recorder, "workflow.agent_started", {
        "label": label,
        "phase": phase,
        "prompt": prompt[:300],
        "index": run.agents,
    })

    # tools 包要先加载：它反过来经 delegate 导入 subagent，直接导入 subagent 会
    # 撞上「父包初始化到一半又回头导入自己」的循环（工具或脚本独立加载时就会踩到）。
    import blank_api.services.tools  # noqa: F401
    from blank_api.services.subagent import SubAgentRequest

    request = SubAgentRequest(
        goal=_agent_goal(prompt, options.get("schema")),
        isolate=bool(options.get("isolate")),
        max_steps=_max_steps(options),
        label=label,
    )
    if hasattr(runner, "spawn_background"):
        agent_id, error_result = await runner.spawn_background(request)
        if error_result is not None:
            raise WorkflowError(
                str(getattr(error_result, "error", "") or "后台子代理启动失败")
            )
        run.agent_ids.add(agent_id)
        snapshot = await _wait_agent(runner, agent_id)
    else:
        result = await runner(request)
        snapshot = {
            "status": "completed" if getattr(result, "ok", False) else "failed",
            "ok": bool(getattr(result, "ok", False)),
            "text": getattr(result, "render", lambda: "")(),
            "error": str(getattr(result, "error", "") or ""),
            "usage": getattr(result, "usage", None),
            "agent_id": str(getattr(result, "agent_id", "") or ""),
        }
        agent_id = str(snapshot.get("agent_id") or "")
        if agent_id:
            run.agent_ids.add(agent_id)

    status = str(snapshot.get("status") or "")
    if status == "cancelled":
        raise WorkflowError(f"子代理 {agent_id} 已取消")
    if not snapshot.get("ok"):
        raise WorkflowError(
            str(snapshot.get("error") or f"子代理 {agent_id} 未能完成（{status}）")
        )
    text = str(snapshot.get("text") or snapshot.get("summary") or "")
    value: Any = text
    if options.get("schema"):
        value = parse_structured(text, options.get("schema"))
    if agent_id:
        run.finished_agents.add(agent_id)
    if run.cache is not None:
        run.cache[key] = {"value": value}
    await _record(run, recorder, "workflow.agent_completed", {
        "label": label,
        "phase": phase,
        "agent_id": agent_id,
        "ok": True,
        "chars": len(text),
        "usage": snapshot.get("usage"),
    })
    return value


async def _send(process: Any, message: dict) -> None:
    stdin = getattr(process, "stdin", None)
    if stdin is None or stdin.is_closing():
        return
    try:
        stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _pump_stderr(run: WorkflowRun, process: Any) -> None:
    stderr = getattr(process, "stderr", None)
    if stderr is None:
        return
    try:
        while True:
            chunk = await stderr.readline()
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace").rstrip()
            if text:
                run.logs.append(f"[stderr] {text[:1000]}")
                del run.logs[:-MAX_LOG_LINES]
    except (asyncio.CancelledError, ValueError):
        return


async def _run_workflow(
    run: WorkflowRun,
    *,
    body: str,
    args: Any,
    runner: Any,
    recorder: Any,
    node: str,
) -> None:
    run.started_at = time.perf_counter()
    temp_dir = Path(tempfile.mkdtemp(prefix=f"blank-wf-{run.workflow_id}-"))
    module_path = temp_dir / f"{run.workflow_id}.mjs"
    process = None
    stderr_task = None
    try:
        module_path.write_text(build_module(run.meta, body, args), encoding="utf-8", newline="\n")
        env = {key: value for key, value in os.environ.items() if key != "NODE_OPTIONS"}
        env["NODE_NO_WARNINGS"] = "1"
        process = await asyncio.create_subprocess_exec(
            node,
            "--permission",
            f"--allow-fs-read={temp_dir}",
            "--disable-proto=throw",
            str(module_path),
            cwd=str(temp_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        run.process = process
        stderr_task = asyncio.create_task(_pump_stderr(run, process))
        deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkflowError(
                    f"工作流超过最长运行时间 {int(WORKFLOW_TIMEOUT_SECONDS)} 秒"
                )
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError as error:
                raise WorkflowError(
                    f"工作流超过最长运行时间 {int(WORKFLOW_TIMEOUT_SECONDS)} 秒"
                ) from error
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                run.logs.append(text[:1000])
                del run.logs[:-MAX_LOG_LINES]
                continue
            kind = str(message.get("type") or "")
            if kind == "agent":
                request_id = int(message.get("id") or 0)
                task = asyncio.create_task(
                    _answer_agent(run, runner, recorder, process, request_id, message)
                )
                run.inflight[request_id] = task
                task.add_done_callback(
                    lambda _task, rid=request_id: run.inflight.pop(rid, None)
                )
                if len(run.inflight) > MAX_INFLIGHT_AGENTS:
                    # 脚本可以一次声明很多 agent，但真正并发的模型调用要有上限。
                    await _throttle(run)
            elif kind == "phase":
                title = str(message.get("title") or "")
                run.phases.append(title)
                await _record(run, recorder, "workflow.phase", {"title": title})
            elif kind == "log":
                run.logs.append(
                    f"[{message.get('level') or 'log'}] {str(message.get('text') or '')[:1000]}"
                )
                del run.logs[:-MAX_LOG_LINES]
            elif kind == "done":
                run.result = message.get("result")
                try:
                    encoded = json.dumps(run.result, ensure_ascii=False)
                except (TypeError, ValueError) as error:
                    raise WorkflowError(f"工作流返回值不能序列化：{error}") from error
                if len(encoded) > MAX_RESULT_CHARS:
                    raise WorkflowError(
                        f"工作流返回值超过 {MAX_RESULT_CHARS} 字符；请只返回结论，不要返回全文"
                    )
                run.status = "completed"
                await _record(run, recorder, "workflow.completed", {
                    "name": run.name,
                    "agents": run.agents,
                    "cached": run.cached,
                    "phases": list(run.phases),
                    "result_chars": len(encoded),
                })
                break
            elif kind == "error":
                run.error = redact(str(message.get("message") or "工作流脚本失败"))
                run.stack = str(message.get("stack") or "")
                run.status = "failed"
                await _record(run, recorder, "workflow.failed", {
                    "name": run.name,
                    "error": run.error,
                    "agents": run.agents,
                })
                break
        await process.wait()
        if run.status == "running":
            tail = "\n".join(run.logs[-5:])[:1000]
            run.status = "failed"
            run.error = redact(
                f"工作流脚本以退出码 {process.returncode} 结束，但没有给出结果"
                + (f"：{tail}" if tail else "")
            )
            await _record(run, recorder, "workflow.failed", {
                "name": run.name,
                "error": run.error,
                "agents": run.agents,
            })
    except asyncio.CancelledError:
        run.status = "cancelled"
        run.error = redact(run.error or "工作流已取消")
        await _record(run, recorder, "workflow.cancelled", {
            "name": run.name,
            "agents": run.agents,
        })
        raise
    except WorkflowError as error:
        run.status = "failed"
        run.error = redact(str(error))
        await _record(run, recorder, "workflow.failed", {
            "name": run.name,
            "error": run.error,
            "agents": run.agents,
        })
    except Exception as error:  # noqa: BLE001 - 后台任务的异常必须落成状态
        run.status = "failed"
        run.error = redact(f"{error.__class__.__name__}: {error}")
        await _record(run, recorder, "workflow.failed", {
            "name": run.name,
            "error": run.error,
            "agents": run.agents,
        })
    finally:
        run.finished_at = time.perf_counter()
        for task in list(run.inflight.values()):
            task.cancel()
        if run.inflight:
            await asyncio.gather(*run.inflight.values(), return_exceptions=True)
        run.inflight.clear()
        for agent_id in list(run.agent_ids):
            if agent_id in run.finished_agents:
                continue
            if hasattr(runner, "stop"):
                try:
                    await runner.stop(agent_id)
                except Exception:  # noqa: BLE001 - 收尸失败不能盖住原始状态
                    pass
        if stderr_task is not None:
            stderr_task.cancel()
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        if run.cache is not None:
            _CACHE[run.workflow_id] = run.cache
            # 同进程只留最近 100 条运行缓存：续跑是迭代便利，不是长期存储。
            while len(_CACHE) > 100:
                _CACHE.pop(next(iter(_CACHE)), None)
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _throttle(run: WorkflowRun) -> None:
    while len(run.inflight) > MAX_INFLIGHT_AGENTS:
        await asyncio.wait(
            list(run.inflight.values()), return_when=asyncio.FIRST_COMPLETED
        )


async def _answer_agent(
    run: WorkflowRun,
    runner: Any,
    recorder: Any,
    process: Any,
    request_id: int,
    message: dict,
) -> None:
    try:
        value = await _run_agent(run, runner, recorder, request_id, message)
    except asyncio.CancelledError:
        await _send(process, {"type": "agent.error", "id": request_id, "message": "工作流已取消"})
        raise
    except Exception as error:  # noqa: BLE001 - 单个子代理失败要回给脚本，而不是让协议挂死
        text = redact(str(error) or error.__class__.__name__)
        await _record(run, recorder, "workflow.agent_failed", {
            "id": request_id,
            "error": text,
        })
        await _send(process, {"type": "agent.error", "id": request_id, "message": text})
        return
    await _send(process, {"type": "agent.result", "id": request_id, "value": value})


def get_cache(workflow_id: str) -> dict[str, dict] | None:
    """取某次运行留下的 agent 结果缓存；没有就返回 None（进程重启后也会没有）。"""
    return _CACHE.get(str(workflow_id or "").strip())


def start_workflow(
    *,
    task_id: str,
    meta: dict,
    body: str,
    args: Any,
    script_path: str,
    runner: Any,
    recorder: Any,
    node: str,
    resume_cache: dict | None = None,
    workflow_id: str = "",
) -> WorkflowRun:
    workflow_id = str(workflow_id or "").strip() or new_id()
    run = WorkflowRun(
        workflow_id=workflow_id,
        task_id=str(task_id or ""),
        name=str(meta.get("name") or ""),
        description=str(meta.get("description") or ""),
        meta=meta,
        script_path=script_path,
        cache=resume_cache if resume_cache is not None else {},
    )
    _bucket(run.task_id)[workflow_id] = run
    run.task = asyncio.create_task(
        _run_workflow(
            run,
            body=body,
            args=args,
            runner=runner,
            recorder=recorder,
            node=node,
        ),
        name=f"blank-{workflow_id}",
    )
    return run


async def output(
    task_id: str, workflow_id: str, *, block: bool = True, timeout: float = 60.0
) -> dict:
    run = get_run(task_id, workflow_id)
    if run is None:
        return {
            "status": "not_found",
            "workflow_id": workflow_id,
            "message": f"没有找到工作流 {workflow_id}（后台句柄只在本进程内有效）",
        }
    if block and run.task is not None and not run.task.done():
        # 与后台子代理同一口径：等不到是"没等到"，不是"杀掉"。
        await asyncio.wait({run.task}, timeout=max(0.0, min(300.0, float(timeout or 0))))
    return run.snapshot()


async def stop(task_id: str, workflow_id: str) -> dict:
    run = get_run(task_id, workflow_id)
    if run is None:
        return {
            "status": "not_found",
            "workflow_id": workflow_id,
            "message": f"没有找到工作流 {workflow_id}（后台句柄只在本进程内有效）",
        }
    if run.task is not None and not run.task.done():
        run.task.cancel()
        try:
            await run.task
        except (asyncio.CancelledError, Exception):
            pass
    return run.snapshot()


def drain_notifications(task_id: str) -> list[dict]:
    """取走「工作流已结束」的通知，每条只投递一次。"""
    notes: list[dict] = []
    for run in (_RUNS.get(str(task_id or "")) or {}).values():
        if run.notified:
            continue
        if run.task is None or not run.task.done():
            continue
        run.notified = True
        notes.append(
            {
                "workflow_id": run.workflow_id,
                "name": run.name,
                "status": run.status,
                "ok": run.status == "completed",
                "agents": run.agents,
                "cached": run.cached,
                "result": run.result_text(),
                "error": run.error,
            }
        )
    return notes


async def aclose(task_id: str, reason: str = "") -> int:
    """任务收尾：取消仍在跑的工作流并摘掉句柄。"""
    key = str(task_id or "")
    bucket = _RUNS.get(key) or {}
    running = [run for run in bucket.values() if run.task is not None and not run.task.done()]
    for run in running:
        run.task.cancel()
    if running:
        await asyncio.gather(*(run.task for run in running if run.task), return_exceptions=True)
    if bucket:
        _RUNS.pop(key, None)
    return len(running)


def call_signature(prompt: str, options: dict) -> str:
    """续跑缓存的键：prompt + options 不变才复用。"""
    return _call_key(prompt, options)


__all__ = [
    "GUIDELINE_AGENTS",
    "MAX_AGENTS",
    "MAX_SCRIPT_CHARS",
    "WorkflowError",
    "WorkflowRun",
    "aclose",
    "build_module",
    "call_signature",
    "drain_notifications",
    "find_node",
    "get_cache",
    "get_run",
    "load_named_script",
    "named_scripts",
    "output",
    "parse_meta",
    "parse_structured",
    "persist_script",
    "read_script_path",
    "start_workflow",
    "stop",
]
