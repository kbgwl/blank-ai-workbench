"""工具与生命周期钩子。

REA 拆解证据（docs/REA-CODEX-CLAUDE.md）：Codex 与 Claude Code 都有几乎同名的一套事件 ——

    Codex        session_start / session_end / user_prompt_submit
                 pre_tool_use / post_tool_use / pre_compact / post_compact
                 subagent_start / subagent_stop
    Claude Code  SessionStart / SessionEnd / UserPromptSubmit
                 PreToolUse / PostToolUse / PreCompact / PostCompact
                 Notification / Stop / SubagentStop

这是"可扩展"的地基：没有它，任何可插拔能力都只能改内核代码。

落地方式与两家一致：把事件以 JSON 写到子进程 stdin，读 stdout 的 JSON 决定是否干预。
**默认关闭** —— 只有显式配置了 `BLANK_HOOKS` 或 settings.json 的 ``hooks`` 才会执行
任何外部命令。钩子失败一律 **fail-open**（坏钩子不能把任务搞挂），但每次执行都会
留审计记录。
例外是否决本身：阻断型事件（pre_tool_use / user_prompt_submit）上明确说"不"
（deny 或退出码 2）就不许放行，跑坏、超时与非阻断型事件的"不表态"一样不拦。
"""

from __future__ import annotations

import asyncio
import json
import locale
import os
import re
import signal
import time
from dataclasses import dataclass

HOOK_EVENTS = (
    "session_start",
    "session_end",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    # 失败与成功分开：Claude Code 的同名事件让钩子作者不必靠 ok 字段分流。
    # 旧的 post_tool_use 在失败时仍会跑一次（兼容），这里是额外的一次。
    "post_tool_use_failure",
    # 任务清单的创建/完成。两家客户端都有同名钩子，价值在"创建前拦一次、完成前拦一次"：
    # 被拒的创建要把刚建的任务删回去，被拒的完成不许落盘（见 tools/tasks.py）。
    "task_created",
    "task_completed",
    "post_compact",
    # 压缩前：钩子可以给摘要器补指令（上游把输出拼进压缩提示词）。
    # 压缩是不可逆的上下文改写，机会只有一次，所以它必须排在折叠之前。
    "pre_compact",
    # 通知：上游按 notification_type 匹配（permission_prompt / agent_completed …）。
    # 它是"告诉外部世界发生了什么"，不改变执行，永远是通知性质。
    "notification",
    # 权限三件套：请求前的决定（可 allow/deny 自动答复）、被拒之后的回执。
    # PermissionRequest 能替用户作答，是阻断型；PermissionDenied 是事后通知。
    "permission_request",
    "permission_denied",
    # 一批工具全跑完之后一次（上游 PostToolBatch，载荷是这一批的 tool_calls）。
    # 与逐条的 post_tool_use 不互斥：那个看单次，这个看"这一批做了什么"。
    "post_tool_batch",
    # 回合结束：模型准备交付时触发。阻断 = 不让这一轮结束，把理由交回模型继续干活。
    # 子代理的同一时刻叫 SubagentStop（上游会把 agent 定义里写的 Stop 转成它）。
    "stop",
    # 回合**因为 API 错误**结束：上游在这时替代 Stop 触发，而不是既发 Stop 又发它。
    # fire-and-forget —— 上游明说钩子的输出与退出码一律忽略，所以它不进 BLOCKING_EVENTS；
    # 匹配面是载荷里的 error 分类（见 stop_failure_error_category）。
    "stop_failure",
    "subagent_stop",
    # 子代理刚要开始跑：上游按 agent_type 匹配，附加上下文注入子代理的启动上下文。
    # 它不是阻断事件——上游把它的退出码 2 渲染成父会话里一条"钩子报错"提示，
    # 子代理照常启动；这里同口径（见 subagent.py 的派发点）。
    "subagent_start",
    # 隔离副本的创建/删除。非 git 工作区以前只有"整树复制"一条路；
    # 有了这两个事件，用户可以自己决定副本建在哪（远端、快照、专用卷）。
    "worktree_create",
    "worktree_remove",
)
# 只有这些事件可以阻止后续动作；其余是通知性质。
# 提交事件在 Claude 的阻断集合里（UserPromptSubmit/UserPromptExpansion 同一族）：
# 它的价值就是"在送进模型之前拦下来或改写掉"，异步化等于放弃这个决定。
# 权限请求与回合结束同理：它们的返回值就是"这一次放不放行 / 这一轮能不能收"，
# 后台跑完再回话时那个决定已经没有落点了。
BLOCKING_EVENTS = frozenset(
    {
        "pre_tool_use",
        "user_prompt_submit",
        "task_created",
        "task_completed",
        "permission_request",
        "stop",
        "subagent_stop",
    }
)
# Claude settings.json 用的是 PascalCase 事件名，BLANK_HOOKS 用蛇形。两种都收，
# 统一折成 HOOK_EVENTS 里的名字。表里没有的事件 = 我们还没移植，告警后跳过：
# 静默忽略会让用户以为自己配的钩子在跑。
EVENT_ALIASES: dict[str, str] = {
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "PostToolUseFailure": "post_tool_use_failure",
    "TaskCreated": "task_created",
    "TaskCompleted": "task_completed",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "Notification": "notification",
    "PermissionRequest": "permission_request",
    "PermissionDenied": "permission_denied",
    "PostToolBatch": "post_tool_batch",
    "Stop": "stop",
    "StopFailure": "stop_failure",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "WorktreeCreate": "worktree_create",
    "WorktreeRemove": "worktree_remove",
}

# StopFailure 的匹配面：上游 matcher 按载荷里的 ``error`` 分类比对，取值就这十三个。
# 本机没有"供应商原始错误对象"，ModelError 只带 code + HTTP 状态，所以这里做的是翻译层：
# 先按状态码（供应商给出的那一层事实），再按本机收敛后的 code，都不认就 unknown。
STOP_FAILURE_ERROR_CATEGORIES = (
    "rate_limit",
    "overloaded",
    "authentication_failed",
    "oauth_org_not_allowed",
    "account_on_hold",
    "verification_required",
    "billing_error",
    "invalid_request",
    "model_not_found",
    "server_error",
    "max_output_tokens",
    "cloud_credential_error",
    "unknown",
)

# HTTP 状态 → 分类。状态码是供应商给出的那一层事实：429 一定是限流、402 一定是账单问题，
# 拿 code 去猜会把"带 429 的传输层失败"错记成 server_error，用户就看不到"被限流了"这一层。
_STOP_FAILURE_STATUS_CATEGORY = {
    429: "rate_limit",
    402: "billing_error",
    401: "authentication_failed",
    403: "authentication_failed",
    404: "model_not_found",
    400: "invalid_request",
    503: "overloaded",
}

# 本机 ModelError 的 code → 分类，只收本机真实会抛的 code（见 models/ 与 routing.py）。
# 传输层失败（超时、连不上、流中断）在上游没有专门分类，一律归 server_error：
# 一句"服务端出错了"比 unknown 更能让用户决定下一步（重试还是换模型）。
_STOP_FAILURE_CODE_CATEGORY = {
    "model_timeout": "server_error",
    "model_unreachable": "server_error",
    "model_stream_error": "server_error",
    "model_stream_empty": "server_error",
    "model_http_error": "server_error",
    "model_output_not_json": "invalid_request",
    "model_tools_unsupported": "invalid_request",
    "circuit_breaker_open": "overloaded",
    "all_models_failed": "unknown",
}


def stop_failure_error_category(code: str | None = None, status: int = 0) -> str:
    """把本机 ``ModelError`` 翻译成上游 StopFailure 载荷里的 ``error`` 分类。"""
    if status:
        category = _STOP_FAILURE_STATUS_CATEGORY.get(int(status))
        if category:
            return category
    if code:
        category = _STOP_FAILURE_CODE_CATEGORY.get(code)
        if category:
            return category
    return "unknown"

# 上游的 matcher 三种形态：空串/"*"/".*" 全中；纯名单按 `|` 或 `,` 拆分后**精确**
# 比对；其余当正则（未锚定）搜。纯名单判定沿用上游那张字符表 —— 名单里不可能
# 出现正则元字符，所以两种解释不会歧义。
_MATCHER_LIST_RE = re.compile(r"^[A-Za-z0-9_|, -]+$")
_MATCH_ALL = ("", "*", ".*")


def matcher_matches(matcher: str, target: str) -> bool:
    """钩子的 matcher 是否命中。语义对齐上游 ``_zr``：

    * 空 / ``*`` / ``.*`` → 全中；
    * 纯名单（字母数字下划线、``|``、逗号、空格、连字符）→ 拆分后精确比对；
    * 其余 → 当正则搜（未锚定）；
    * 正则非法 → 不匹配（上游同样告警后当作不命中）。
    """
    text = str(matcher or "")
    if text in _MATCH_ALL:
        return True
    if _MATCHER_LIST_RE.match(text):
        names = {item.strip() for item in text.replace(",", "|").split("|")}
        names.discard("")
        return target in names
    try:
        return re.search(text, target) is not None
    except re.error:
        return False


def _matcher_ok(matcher: str) -> bool:
    """配置期校验：非法正则在这里就丢掉，别等到每次调用才静默不匹配。"""
    text = str(matcher or "")
    if text in _MATCH_ALL or _MATCHER_LIST_RE.match(text):
        return True
    try:
        re.compile(text)
    except re.error:
        return False
    return True


DEFAULT_TIMEOUT = 10.0
MAX_SNIPPET = 500
# additionalContext 会进模型上下文：不设上限等于让一个坏钩子把窗口占满。
MAX_CONTEXT = 4000
# 回合结束钩子的连续阻断上限（上游 CLAUDE_CODE_STOP_HOOK_BLOCK_CAP，默认同为 8）。
# 一个永远说"不"的钩子不许把一轮对话拖成死循环；超限告警并放行。
STOP_HOOK_BLOCK_CAP = 8


def stop_hook_block_cap() -> int:
    try:
        value = int(os.environ.get("BLANK_STOP_HOOK_BLOCK_CAP", "") or STOP_HOOK_BLOCK_CAP)
    except ValueError:
        value = STOP_HOOK_BLOCK_CAP
    return max(0, value)
# 后台钩子在任务收尾时最多等这么久：等到的东西进审计，等不到的在事件里记
# abandoned。一个慢钩子不许把任务结束拖成无上限等待。
ASYNC_SETTLE_SECONDS = 1.0
MAX_SETTLE_SECONDS = 10.0


@dataclass(frozen=True)
class HookSpec:
    command: str
    match: str = ""
    timeout: float = DEFAULT_TIMEOUT
    # 配置里写 "async": true 时，钩子跑在后台，调用方不再等它。阻断型事件
    # （pre_tool_use / user_prompt_submit）例外：它的价值就在于"等结果再决定"，
    # 异步化等于放弃 veto。
    async_run: bool = False
    # 这条钩子从哪来：env（BLANK_HOOKS）还是 settings（settings.json 的 hooks）。
    # 只用于审计，不影响执行。
    source: str = "env"

    def applies(self, target: str) -> bool:
        return matcher_matches(self.match, target)


def _clamp_timeout(raw: object) -> float:
    try:
        value = float(raw) if raw is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(0.5, min(value, 60.0))


def _build_specs(event: str, items: list, *, source: str, warnings: list[str]) -> list[HookSpec]:
    """拍平一个事件下的配置。两种形状都收：

    * 扁平：``[{"command": "...", "match": "Write", "timeout": 5}]``（BLANK_HOOKS）
    * Claude 嵌套：``[{"matcher": "Write|Edit", "hooks": [{"type": "command", ...}]}]``
    """
    specs: list[HookSpec] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            warnings.append(f"{event} 第 {index} 条配置不是对象，已跳过")
            continue
        group = item.get("hooks")
        matcher = str(item.get("matcher") or item.get("match") or "").strip()
        candidates = group if isinstance(group, list) else [item]
        if not _matcher_ok(matcher):
            warnings.append(f"{event} 第 {index} 条的 matcher 不是合法正则，已跳过")
            continue
        for hook in candidates:
            if not isinstance(hook, dict):
                warnings.append(f"{event} 第 {index} 组里的钩子不是对象，已跳过")
                continue
            # 上游的钩子还有其他 type（prompt/agent/http/…）：还没有对应实现，
            # 跳过并告警，而不是当成 shell 命令去跑一坨提示词。
            kind = str(hook.get("type") or "command").strip().lower()
            if kind != "command":
                warnings.append(f"{event} 第 {index} 组的钩子类型 {kind} 尚未支持，已跳过")
                continue
            command = str(hook.get("command") or "").strip()
            if not command:
                warnings.append(f"{event} 第 {index} 组的钩子缺少 command，已跳过")
                continue
            specs.append(
                HookSpec(
                    command=command,
                    match=matcher,
                    timeout=_clamp_timeout(hook.get("timeout")),
                    async_run=bool(
                        hook.get("async") or hook.get("asyncRun") or hook.get("background")
                    ),
                    source=source,
                )
            )
    return specs


@dataclass
class HookResult:
    event: str
    command: str
    status: str  # ok / failed / timeout / skipped
    decision: str = ""  # allow / deny / ask
    reason: str = ""
    duration_ms: int = 0
    output: str = ""
    # Claude 的 updatedInput：钩子可以在不放宽权限的前提下改写这次调用。
    updated_input: dict | None = None
    # additionalContext：由调用方注入下一次模型调用，不改变工具结果。
    additional_context: str = ""
    # WorktreeCreate 的返回值：钩子自己建好隔离副本，把路径告回来。
    # 没有这个字段时退回"git worktree / 整树复制"的默认路径。
    worktree_path: str = ""
    # suppressOriginalPrompt：钩子要求"原文不进模型上下文"（#123）。
    # 抑制的是上下文，不是账——tasks.goal 里那份原文一个字都不动。
    suppress_original: bool = False
    # 这条结果是后台跑完的（配置里 async:true）。审计时读它，
    # 否则"钩子跑完了、但模型没等它"这件事在时间线上看不出来。
    background: bool = False
    exit_code: int | None = None

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def asks(self) -> bool:
        return self.decision == "ask"

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict:
        body = {
            "event": self.event,
            "command": self.command,
            "status": self.status,
            "duration_ms": self.duration_ms,
        }
        if self.decision:
            body["decision"] = self.decision
        if self.reason:
            body["reason"] = self.reason
        if self.output:
            body["output"] = self.output
        if self.updated_input is not None:
            body["updated_input"] = self.updated_input
        if self.additional_context:
            body["additional_context_chars"] = len(self.additional_context)
        if self.worktree_path:
            body["worktree_path"] = self.worktree_path
        if self.suppress_original:
            body["suppress_original_prompt"] = True
        if self.background:
            body["async"] = True
        if self.exit_code is not None:
            body["exit_code"] = self.exit_code
        return body


_DECISION_WORDS = {
    "deny": {"block", "deny", "denied", "reject"},
    "allow": {"allow", "allowed", "approve", "approved", "continue"},
    # Claude 用 ask 表示“升级给人决定”；kbgai 以前没有这一档，读到就当没表态。
    "ask": {"ask", "escalate", "require_approval", "require-approval"},
}


def _normalize_decision(raw: object) -> str:
    value = str(raw or "").strip().lower()
    for decision, words in _DECISION_WORDS.items():
        if value in words:
            return decision
    return ""


def parse_hook_output(text: str) -> dict:
    """解析钩子 stdout。同时认 Claude 的 ``hookSpecificOutput`` 与旧版顶层字段。

    返回 ``{decision, reason, updated_input, additional_context}``；找不到 JSON
    或字段不合法就返回空决定——坏输出不允许变成隐式放行。
    """
    stripped = (text or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    nested = parsed.get("hookSpecificOutput")
    body = nested if isinstance(nested, dict) else parsed
    decision = _normalize_decision(
        body.get("permissionDecision")
        or body.get("decision")
        or parsed.get("permissionDecision")
        or parsed.get("decision")
        or parsed.get("permission")
    )
    reason = str(
        body.get("permissionDecisionReason")
        or body.get("reason")
        or parsed.get("reason")
        or parsed.get("message")
        or ""
    )[:300]
    updated = body.get("updatedInput")
    if not isinstance(updated, dict):
        updated = parsed.get("updatedInput")
    context = (
        body.get("additionalContext")
        or parsed.get("additionalContext")
        or body.get("additional_context")
        or parsed.get("additional_context")
        or ""
    )
    body_out: dict = {"decision": decision, "reason": reason}
    if isinstance(updated, dict):
        body_out["updated_input"] = updated
    if isinstance(context, str) and context.strip():
        body_out["additional_context"] = context.strip()[:MAX_CONTEXT]
    # WorktreeCreate 的契约：钩子负责建副本并回报路径。相对路径按工作区解析，
    # 由调用方决定；这里只取字符串，不替它猜。
    worktree = (
        body.get("worktreePath")
        or body.get("worktree_path")
        or parsed.get("worktreePath")
        or parsed.get("worktree_path")
        or body.get("path")
        or parsed.get("path")
    )
    if isinstance(worktree, str) and worktree.strip():
        body_out["worktree_path"] = worktree.strip()[:1000]
    # Claude 的 suppressOriginalPrompt（UserPromptSubmit/UserPromptExpansion 同族）：
    # 只认真布尔 true——它自己的类型校验就是「非 true 即错型」。字符串 "true" 与 1
    # 来自写错的钩子，把它读成"要抑制"等于让错型变成隐式放行的一种。
    suppress = body.get("suppressOriginalPrompt")
    if suppress is not True:
        suppress = parsed.get("suppressOriginalPrompt")
    if suppress is True:
        body_out["suppress_original_prompt"] = True
    return body_out


def parse_decision(text: str) -> tuple[str, str]:
    """从钩子 stdout 里容错解析决定。找不到就当作"不表态"。"""
    parsed = parse_hook_output(text)
    return str(parsed.get("decision") or ""), str(parsed.get("reason") or "")


def _decode(raw: bytes) -> str:
    """钩子的输出按「UTF-8 优先，再退到本机码页」解，任何字节都不许静默消失。

    钩子是外部程序：Windows 上没显式 `reconfigure` 的 Python/命令按 cp936 写管道是常态，
    而这份正文会直接进模型的系统消息或被当成阻断原因给用户看。用
    ``decode("utf-8","ignore")`` 会把解不出的字节整段丢掉——真模型点验实测到一行中文
    少了 6 个字还留下乱码，比乱码更糟的是产物里看不出少过东西。解不干净时宁可出现
    可见的替换符（``replace``），也不要看不见的删除。
    """
    if not raw:
        return ""
    candidates = ["utf-8", locale.getpreferredencoding(False) or "", "gbk"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    """杀掉钩子。shell 模式起的是 cmd/sh 再派生子进程，只 kill 外层会留下孤儿。

    Windows 用 ``taskkill /T`` 连子孙一起收；POSIX 没有等价开关，得靠
    ``start_new_session``（见 ``_spawn_shell``）把钩子放进自己的进程组，再对整组发信号。
    只喊 ``process.kill()`` 的话杀的是 ``sh``，``sh`` 派生的 python 继续跑——2026-10-29
    Linux runner 上 ``test_async_hooks_v119`` 那两条就是这么红的：取消回来了，标记文件
    三秒后照样出现。
    """
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except Exception:  # noqa: BLE001 - taskkill 不可用时退回单进程 kill
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


async def _spawn_shell(command: str) -> asyncio.subprocess.Process:
    """起钩子进程；创建途中被取消时，落地的那个进程也要杀。

    收尾窗口只有几十毫秒，取消可能正好落在「子进程正在创建」这一段：那时
    ``_execute`` 还没拿到 process，CancelledError 分支无从下手，孙进程就留在
    机器上继续跑（全量回归里那条时序用例偶发红就是它）。用 shield 把创建
    捂完整，拿到进程先杀再抛，取消语义不变。
    """
    spawn = asyncio.ensure_future(
        asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # POSIX 上让钩子自成进程组，这样收尾取消能对整组发信号（见 _kill_process）。
            # Windows 上没有这个概念，靠 taskkill /T 收树，不传这个参数。
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
    )
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        while not spawn.done():
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - 起不来就没有进程可杀
                break
        if spawn.done() and not spawn.cancelled() and spawn.exception() is None:
            await _kill_process(spawn.result())
        raise


class HookRunner:
    """按事件分发钩子。线程内单实例即可，状态只用于审计。"""

    def __init__(
        self,
        hooks: dict[str, list[HookSpec]] | None = None,
        *,
        settle_timeout: float = ASYNC_SETTLE_SECONDS,
        warnings: list[str] | None = None,
        disabled: bool = False,
    ) -> None:
        self.hooks: dict[str, list[HookSpec]] = hooks or {}
        self.results: list[HookResult] = []
        self.settle_timeout = max(0.0, min(float(settle_timeout), MAX_SETTLE_SECONDS))
        # 配置期发现的问题（未支持的事件/类型、坏正则、空命令）。进审计事件，
        # 让"我配了但没跑"有出处；不含命令正文，避免把路径/参数带进事件。
        self.warnings: list[str] = list(warnings or [])
        # disableAllHooks：策略级总开关。为真时一个钩子都不加载。
        self.disabled = bool(disabled)
        # 后台任务：键是任务本身（防止被 GC），值保留 spec 与事件名供取消后审计。
        self._tasks: dict[asyncio.Task, tuple[HookSpec, str]] = {}
        # 完成但还没被下一次模型调用 / 收尾流程取走的结果与唤醒。
        self._completed: list[HookResult] = []
        self._wakeups: list[dict] = []

    @property
    def enabled(self) -> bool:
        return bool(self.hooks)

    @property
    def pending(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    @classmethod
    def from_env(
        cls,
        raw: str | None = None,
        *,
        settings: dict | None = None,
        disabled: bool = False,
    ) -> "HookRunner":
        """从 settings.json 的 ``hooks`` 与 ``BLANK_HOOKS`` 构建（后者形如：
        {"pre_tool_use":[{"match":"run_shell","command":"node hooks/guard.mjs","timeout":5}]}）。

        两处配置合并，同一条（事件 + 命令 + matcher + 超时）只跑一次。解析失败
        一律降级为"没有钩子"，不影响任务启动；坏条目进 ``warnings`` 进审计。
        ``disabled``（上游 disableAllHooks）为真时一个钩子都不加载。
        """
        try:
            settle = float(os.environ.get("BLANK_HOOK_SETTLE", "") or ASYNC_SETTLE_SECONDS)
        except ValueError:
            settle = ASYNC_SETTLE_SECONDS
        if disabled:
            return cls(
                settle_timeout=settle,
                warnings=["disableAllHooks 为真：所有钩子都不加载"],
                disabled=True,
            )

        hooks: dict[str, list[HookSpec]] = {}
        warnings: list[str] = []
        seen: set[tuple[str, str, str, float]] = set()

        def add(mapping: dict, source: str) -> None:
            for key, items in mapping.items():
                event = EVENT_ALIASES.get(str(key), str(key))
                if event not in HOOK_EVENTS:
                    warnings.append(f"未支持的事件 {key}，已跳过")
                    continue
                if not isinstance(items, list):
                    warnings.append(f"{key} 的配置不是数组，已跳过")
                    continue
                for spec in _build_specs(event, items, source=source, warnings=warnings):
                    fingerprint = (event, spec.command, spec.match, spec.timeout)
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    hooks.setdefault(event, []).append(spec)

        if isinstance(settings, dict):
            add(settings, "settings")
        text = (os.environ.get("BLANK_HOOKS", "") if raw is None else raw).strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
                warnings.append("BLANK_HOOKS 不是合法 JSON，已跳过")
            if isinstance(parsed, dict):
                add(parsed, "env")
        return cls(hooks, settle_timeout=settle, warnings=warnings)

    def describe(self) -> dict:
        """审计面：每个事件几条钩子、来自哪里、配置期告警。不含命令正文。"""
        return {
            "disabled": self.disabled,
            "events": {event: len(specs) for event, specs in sorted(self.hooks.items())},
            "count": sum(len(specs) for specs in self.hooks.values()),
            "sources": sorted({spec.source for specs in self.hooks.values() for spec in specs}),
            "warnings": list(self.warnings),
        }

    async def run(self, event: str, target: str = "", payload: dict | None = None) -> list[HookResult]:
        """执行某事件下所有匹配的钩子。阻塞型事件遇到 deny 立即停止后续钩子。

        ``async_run`` 的钩子这里只登记后台任务并立即返回；它的结果留给下一次
        模型调用或收尾流程取走。阻断型事件上的 async 配置被忽略——拿不到结果的
        veto 不是 veto。
        """
        specs = [spec for spec in self.hooks.get(event, []) if spec.applies(target)]
        if not specs:
            return []

        body = {
            "event": event,
            # 上游的 base hook input 里一定有 hook_event_name；有的钩子只认它
            # （而不认我们自己的 event 字段），两个都写，谁都能读。
            "hook_event_name": event,
            "target": target,
            **(payload or {}),
        }
        results: list[HookResult] = []
        for spec in specs:
            if spec.async_run and event not in BLOCKING_EVENTS:
                self._spawn(spec, event, body)
                continue
            result = await self._execute(spec, event, body)
            results.append(result)
            self.results.append(result)
            if event in BLOCKING_EVENTS and result.denied:
                break
        return results

    def _spawn(self, spec: HookSpec, event: str, body: dict) -> None:
        task = asyncio.create_task(self._run_detached(spec, event, body))
        self._tasks[task] = (spec, event)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        spec_event = self._tasks.pop(task, None)
        if task.cancelled() or spec_event is None:
            return
        error = task.exception()
        if error is None:
            return
        spec, event = spec_event
        # _run_detached 自己兜普通异常；这里接住的是连它也没接住的那类。
        result = HookResult(
            event=event,
            command=spec.command,
            status="failed",
            reason=f"后台钩子异常：{error.__class__.__name__}",
            background=True,
        )
        self.results.append(result)
        self._completed.append(result)

    async def _run_detached(self, spec: HookSpec, event: str, body: dict) -> None:
        try:
            result = await self._execute(spec, event, body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台失败绝不能冒到任务主流程
            result = HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"后台钩子异常：{exc.__class__.__name__}",
            )
        result.background = True
        self.results.append(result)
        self._completed.append(result)
        if result.exit_code == 2:
            # Claude asyncRewake 的语义：后台钩子以退出码 2 结束 = 叫醒模型。
            # 文本取 additionalContext（_execute 已把 stderr 并进去），
            # 为空时退回 reason，保证唤醒里有一句能读的话。
            self._wakeups.append(
                {
                    "event": event,
                    "tool": str(body.get("tool") or body.get("target") or ""),
                    "command": spec.command,
                    "text": result.additional_context or result.reason or "后台钩子以退出码 2 结束",
                    "exit_code": 2,
                }
            )

    def drain_completed(self) -> list[HookResult]:
        """取走后台已完成的审计结果；调用方负责写事件。"""
        items = list(self._completed)
        self._completed.clear()
        return items

    def drain_wakeups(self) -> list[dict]:
        """取走退出码 2 的唤醒；不取就会在下一次模型调用前一直等着。"""
        items = list(self._wakeups)
        self._wakeups.clear()
        return items

    async def close(self, timeout: float | None = None) -> tuple[list[HookResult], list[str]]:
        """收尾时给后台钩子一个有界窗口。

        返回 ``(新完成的审计结果, 没等到而被取消的命令)``。取消后重新抛出
        CancelledError 的路径由 ``_execute`` 负责杀掉子进程，不留孤儿。
        """
        budget = self.settle_timeout if timeout is None else max(0.0, float(timeout))
        running = list(self._tasks)
        if running:
            await asyncio.wait(running, timeout=budget)
        abandoned = [task for task in running if not task.done()]
        commands = [
            self._tasks[task][0].command
            for task in abandoned
            if task in self._tasks
        ]
        for task in abandoned:
            task.cancel()
        if abandoned:
            await asyncio.gather(*abandoned, return_exceptions=True)
        return self.drain_completed(), commands

    async def _execute(self, spec: HookSpec, event: str, payload: dict) -> HookResult:
        started = time.perf_counter()
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            process = await _spawn_shell(spec.command)
        except Exception as exc:  # 命令本身起不来
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"钩子无法启动：{exc.__class__.__name__}",
                duration_ms=elapsed(),
            )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(encoded), timeout=spec.timeout)
        except asyncio.TimeoutError:
            await _kill_process(process)
            # 超时不阻断：宁可放过，也不让坏钩子卡死任务
            return HookResult(
                event=event,
                command=spec.command,
                status="timeout",
                reason=f"钩子超时（{spec.timeout:g}s）",
                duration_ms=elapsed(),
            )
        except asyncio.CancelledError:
            # 收尾窗口耗尽被取消：子进程必须一起杀掉，否则退出后还在机器上跑。
            await _kill_process(process)
            raise

        text = _decode(stdout or b"").strip()
        err = _decode(stderr or b"").strip()
        parsed = parse_hook_output(text)
        decision = str(parsed.get("decision") or "")
        reason = str(parsed.get("reason") or "")
        exit_code = process.returncode
        additional_context = str(parsed.get("additional_context") or "")
        status = "ok" if exit_code == 0 else "failed"
        if exit_code == 2:
            # Claude 的约定：退出码 2 = 阻断，stderr 作为原因交给模型/用户。
            # 只有阻断型事件才真的阻断；其余事件把 stderr 当作给模型的上下文（#119）。
            if event in BLOCKING_EVENTS:
                decision = "deny"
                reason = reason or err or "钩子以退出码 2 阻止执行"
            else:
                additional_context = (additional_context + "\n" + (err or text)).strip()[:MAX_CONTEXT]
        elif status == "failed" and not reason:
            reason = (err or text)[:300] or f"退出码 {exit_code}"
        return HookResult(
            event=event,
            command=spec.command,
            status=status,
            decision=decision,
            reason=reason,
            duration_ms=elapsed(),
            output=text[:MAX_SNIPPET],
            updated_input=parsed.get("updated_input"),
            additional_context=additional_context,
            worktree_path=str(parsed.get("worktree_path") or ""),
            suppress_original=bool(parsed.get("suppress_original_prompt")),
            exit_code=exit_code,
        )


def blocking_denial(results: list[HookResult]) -> HookResult | None:
    return next((item for item in results if item.denied), None)


def usable_context(results: list[HookResult]) -> list[str]:
    """钩子给的附加上下文，去掉退出码 2 的那些。

    上游把 exit 2 一律渲染成 ``hook_non_blocking_error``（给人看的"钩子报错"），
    而不是模型必须遵守的输入。``_execute`` 对非阻断事件的 exit 2 会把 stderr 并进
    ``additional_context``，两个用途共用一个字段——注入前必须按退出码分开，
    否则一条报错会变成子代理的作业指令。
    """
    return [
        item.additional_context.strip()
        for item in results
        if item.additional_context.strip() and item.exit_code != 2
    ]


def non_blocking_errors(results: list[HookResult]) -> list[HookResult]:
    """退出码 2 的那些（上游 ``hook_non_blocking_error``）：可见、但不阻断、不注入。"""
    return [item for item in results if item.exit_code == 2]
