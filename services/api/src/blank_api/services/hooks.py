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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

HOOK_EVENTS = (
    "session_start",
    "session_end",
    "user_prompt_submit",
    # 斜杠命令展开成提示词的那一刻（上游 UserPromptExpansion）。与 user_prompt_submit
    # 同族、但在它之前：载荷带 expansion_type / command_name / command_args /
    # command_source / 原文，钩子可以补上下文、要求抑制原文，或直接否决这次展开。
    "user_prompt_expansion",
    "pre_tool_use",
    "post_tool_use",
    # 失败与成功分开：Claude Code 的同名事件让钩子作者不必靠 ok 字段分流。
    # 旧的 post_tool_use 在失败时仍会跑一次（兼容），这里是额外的一次。
    "post_tool_use_failure",
    # 任务清单的创建/完成。两家客户端都有同名钩子，价值在"创建前拦一次、完成前拦一次"：
    # 被拒的创建要把刚建的任务删回去，被拒的完成不许落盘（见 tools/tasks.py）。
    "task_created",
    "task_completed",
    # 成员即将转入空闲的那一刻（上游 TeammateIdle）。载荷只有 teammate_name /
    # team_name 两个键（上游的 team_name 已标 deprecated，仍是会话推导出来的那份）；
    # 钩子给 preventContinuation 就阻止它继续，理由取 stopReason，缺省文案
    # "TeammateIdle hook prevented continuation"。退出码 2 按上游同口径只算一条
    # 非阻断错误（不进 BLOCKING_EVENTS）。
    "teammate_idle",
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
        # 与 user_prompt_submit 同族：上游 UserPromptExpansion 的退出码 2 就是
        # "这次展开作废"，不进集合的话退出码 2 会被当成一条非阻断错误，否决直接失效。
        "user_prompt_expansion",
        "task_created",
        "task_completed",
        "permission_request",
        "stop",
        "subagent_stop",
    }
)
# 上游的六种可执行钩子类型（AF3c 补齐）。command 是本仓库原来唯一支持的形态；
# prompt/agent 需要会话上下文（上游在无上下文时直接抛错），http 直接 POST，
# mcp_tool 调一个 MCP 工具，script 走脚本文件——但上游这个构建里 ``Jst`` 直接抛
# "script hooks are not available in this build"，所以 script 认出来、执行时按上游
# 同口径报不可用。四类可执行类型的输出判定与退出码语义与 command 共用。
HOOK_KINDS = frozenset({"command", "prompt", "agent", "http", "mcp_tool", "script"})
# Claude settings.json 用的是 PascalCase 事件名，BLANK_HOOKS 用蛇形。两种都收，
# 统一折成 HOOK_EVENTS 里的名字。表里没有的事件 = 我们还没移植，告警后跳过：
# 静默忽略会让用户以为自己配的钩子在跑。
EVENT_ALIASES: dict[str, str] = {
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "UserPromptExpansion": "user_prompt_expansion",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "PostToolUseFailure": "post_tool_use_failure",
    "TaskCreated": "task_created",
    "TaskCompleted": "task_completed",
    "TeammateIdle": "teammate_idle",
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


def write_transcript(messages: list[dict], path: Path) -> str:
    """把对话消息写成 JSONL transcript 文件，返回绝对路径字符串。

    上游 SubagentStop / StopFailure 的 stdin 载荷带 ``agent_transcript_path``，
    指向一份 JSONL 格式的对话记录文件，钩子可以读取它来检查整段对话。
    本机此前没有 transcript 落盘机制——钩子拿到这个路径也没东西可读。
    这里在触发点把当前对话消息写成一份 JSONL 快照：每行一条 ``{"role", "content"}``，
    与上游的 JSONL transcript 格式一致。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for msg in messages or []:
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        lines.append(json.dumps({"role": role, "content": content}, ensure_ascii=False))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return str(path)

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
    # 上游的钩子类型（command/prompt/agent/http/mcp_tool）。command 是本仓库原来
    # 唯一支持的形态；其余四类在 AF3c 补齐——它们与 command 的差别只在"怎么拿到
    # 输出"，拿到之后的判定（parse_hook_output）与退出码语义完全共用。
    kind: str = "command"
    # 审计与调用侧共用的标识串：command 型是 shell 命令，http 型是 url，
    # mcp_tool 型是 `server/tool`，prompt/agent 型是提示词正文。
    command: str = ""
    match: str = ""
    timeout: float = DEFAULT_TIMEOUT
    # 配置里写 "async": true 时，钩子跑在后台，调用方不再等它。阻断型事件
    # （pre_tool_use / user_prompt_submit）例外：它的价值就在于"等结果再决定"，
    # 异步化等于放弃 veto。
    async_run: bool = False
    # 这条钩子从哪来：env（BLANK_HOOKS）还是 settings（settings.json 的 hooks）。
    # 只用于审计，不影响执行。
    source: str = "env"
    # http 型：请求地址（上游的字段名就是 url）。
    url: str = ""
    # prompt/agent 型：交给模型的提示词正文。
    prompt: str = ""
    # mcp_tool 型：服务端名与工具名（上游序列化成 `${server}/${tool}`）。
    server: str = ""
    tool: str = ""

    def applies(self, target: str) -> bool:
        return matcher_matches(self.match, target)

    def as_dict(self) -> dict:
        """审计视图：只放影响执行的字段，不含命令正文以外的细节。"""
        body: dict = {
            "kind": self.kind,
            "command": self.command,
            "match": self.match,
            "timeout": self.timeout,
        }
        if self.async_run:
            body["async"] = True
        if self.source != "env":
            body["source"] = self.source
        if self.url:
            body["url"] = self.url
        if self.prompt:
            body["prompt_chars"] = len(self.prompt)
        if self.server or self.tool:
            body["server"] = self.server
            body["tool"] = self.tool
        return body


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
            # 上游的六种可执行钩子类型（command/prompt/agent/http/mcp_tool/script）：
            # 字段名与序列化形状都对齐上游——http 用 url（序列化时写进 command 键），
            # mcp_tool 用 server+tool（序列化成 `${server}/${tool}`），
            # prompt/agent 用 prompt，script 用 file（或内联 script）。认不出来的类型
            # 照旧跳过并告警，而不是当成 shell 命令去跑一坨提示词。
            kind = str(hook.get("type") or "command").strip().lower()
            if kind not in HOOK_KINDS:
                warnings.append(f"{event} 第 {index} 组的钩子类型 {kind} 尚未支持，已跳过")
                continue
            if kind == "command":
                primary = str(hook.get("command") or "").strip()
                if not primary:
                    warnings.append(f"{event} 第 {index} 组的钩子缺少 command，已跳过")
                    continue
            elif kind == "http":
                primary = str(hook.get("url") or hook.get("command") or "").strip()
                if not primary:
                    warnings.append(f"{event} 第 {index} 组的 http 钩子缺少 url，已跳过")
                    continue
            elif kind in ("prompt", "agent"):
                primary = str(hook.get("prompt") or "").strip()
                if not primary:
                    warnings.append(f"{event} 第 {index} 组的 {kind} 钩子缺少 prompt，已跳过")
                    continue
            elif kind == "script":
                # 上游 script 型要么给 file（脚本文件路径），要么给内联 script 正文。
                # 内联正文不落进审计的 command 字段（mask_path 只打码路径、不打码正文），
                # 用固定标记代替，免得把脚本原文带进事件。
                file = str(hook.get("file") or "").strip()
                if not file and not str(hook.get("script") or "").strip():
                    warnings.append(f"{event} 第 {index} 组的 script 钩子缺少 file/script，已跳过")
                    continue
                primary = file or "<inline-script>"
            else:  # mcp_tool
                server = str(hook.get("server") or "").strip()
                tool = str(hook.get("tool") or "").strip()
                if not server or not tool:
                    warnings.append(f"{event} 第 {index} 组的 mcp_tool 钩子缺少 server/tool，已跳过")
                    continue
                primary = f"{server}/{tool}"
            specs.append(
                HookSpec(
                    kind=kind,
                    command=primary,
                    match=matcher,
                    timeout=_clamp_timeout(hook.get("timeout")),
                    async_run=bool(
                        hook.get("async") or hook.get("asyncRun") or hook.get("background")
                    ),
                    source=source,
                    url=primary if kind == "http" else "",
                    prompt=primary if kind in ("prompt", "agent") else "",
                    server=str(hook.get("server") or "").strip() if kind == "mcp_tool" else "",
                    tool=str(hook.get("tool") or "").strip() if kind == "mcp_tool" else "",
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
    # preventContinuation / stopReason：上游 TeammateIdle（与 TaskCompleted、Stop 同族）
    # 的契约——钩子要求"别继续"，并给出理由；上游的默认文案是
    # "TeammateIdle hook prevented continuation"。只认真布尔 true，理由只取字符串。
    prevent_continuation: bool = False
    stop_reason: str = ""
    # SessionStart 钩子输出四字段（对齐上游 Sn() 校验函数）。
    # sessionTitle（string）：更新会话标题（Thread.title）。
    session_title: str = ""
    # initialUserMessage（string）：作为会话第一条 user 消息注入模型上下文。
    initial_user_message: str = ""
    # watchPaths（string[]）：写入 .blank/context/watch.json。
    watch_paths: list[str] = field(default_factory=list)
    # reloadSkills（bool，只认 true）：重新发现 Skill 目录并刷新会话目录。
    reload_skills: bool = False
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
        if self.prevent_continuation:
            # 阻止继续的理由要落账：它是给用户看的那句话（上游渲染成
            # hook_stopped_continuation 的消息），只记钩子自己的那份。
            body["prevent_continuation"] = True
            if self.stop_reason:
                body["stop_reason"] = self.stop_reason
        if self.session_title:
            body["session_title"] = self.session_title
        if self.initial_user_message:
            body["initial_user_message_chars"] = len(self.initial_user_message)
        if self.watch_paths:
            body["watch_paths_count"] = len(self.watch_paths)
        if self.reload_skills:
            body["reload_skills"] = True
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
    # preventContinuation / stopReason（TeammateIdle、TaskCompleted、Stop 同族）：
    # 同样只认真布尔 true——字符串 "true" 与 1 是写错的钩子，把它读成"要阻止继续"
    # 等于让一次错型变成一次静默的硬停，比放行更难查。
    prevent = body.get("preventContinuation")
    if prevent is not True:
        prevent = parsed.get("preventContinuation")
    if prevent is True:
        body_out["prevent_continuation"] = True
    stop = body.get("stopReason")
    if not isinstance(stop, str):
        stop = parsed.get("stopReason")
    if isinstance(stop, str) and stop.strip():
        body_out["stop_reason"] = stop.strip()[:300]
    # SessionStart 钩子输出四字段（对齐上游 Sn() 的类型校验）：
    # sessionTitle / initialUserMessage 只认字符串，非字符串即错型不解析；
    # watchPaths 只认字符串数组，非数组或含非字符串元素即错型不解析；
    # reloadSkills 只认真布尔 true——false、字符串 "true"、1 都是写错的钩子，
    # 把它读成"要重载"等于让一次错型变成一次静默的目录重扫。
    title = body.get("sessionTitle")
    if not isinstance(title, str):
        title = parsed.get("sessionTitle")
    if isinstance(title, str) and title.strip():
        body_out["session_title"] = title.strip()[:300]
    init_msg = body.get("initialUserMessage")
    if not isinstance(init_msg, str):
        init_msg = parsed.get("initialUserMessage")
    if isinstance(init_msg, str) and init_msg.strip():
        body_out["initial_user_message"] = init_msg.strip()[:MAX_CONTEXT]
    watch = body.get("watchPaths")
    if not isinstance(watch, list):
        watch = parsed.get("watchPaths")
    if isinstance(watch, list) and all(isinstance(item, str) for item in watch):
        body_out["watch_paths"] = [item.strip() for item in watch if item.strip()]
    reload_skills = body.get("reloadSkills")
    if reload_skills is not True:
        reload_skills = parsed.get("reloadSkills")
    if reload_skills is True:
        body_out["reload_skills"] = True
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


async def _spawn_shell(command: str, env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
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
            env=env,
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
        prompt_runner: Callable[[str, dict], Awaitable[str]] | None = None,
        mcp_runner: Callable[[str, str, dict], Awaitable[str]] | None = None,
    ) -> None:
        self.hooks: dict[str, list[HookSpec]] = hooks or {}
        self.results: list[HookResult] = []
        self.settle_timeout = max(0.0, min(float(settle_timeout), MAX_SETTLE_SECONDS))
        # 配置期发现的问题（未支持的事件/类型、坏正则、空命令）。进审计事件，
        # 让"我配了但没跑"有出处；不含命令正文，避免把路径/参数带进事件。
        self.warnings: list[str] = list(warnings or [])
        # disableAllHooks：策略级总开关。为真时一个钩子都不加载。
        self.disabled = bool(disabled)
        # prompt/agent 型钩子要把提示词交给模型（上游在无会话上下文时直接抛错）。
        # 运行时注入一个 `(prompt, payload) -> 模型正文` 的调用；没有它，这两类
        # 钩子按上游同口径报"没有会话上下文"，而不是静默跳过。
        self.prompt_runner = prompt_runner
        # mcp_tool 型钩子要调一个 MCP 工具（上游从会话上下文里取客户端）。注入
        # `(server, tool, payload) -> 工具正文`；没有它，这类钩子按上游同口径报
        # "缺 MCP 客户端"，而不是把它当命令去跑。
        self.mcp_runner = mcp_runner
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
        prompt_runner: Callable[[str, dict], Awaitable[str]] | None = None,
        mcp_runner: Callable[[str, str, dict], Awaitable[str]] | None = None,
    ) -> "HookRunner":
        """从 settings.json 的 ``hooks`` 与 ``BLANK_HOOKS`` 构建（后者形如：
        {"pre_tool_use":[{"match":"run_shell","command":"node hooks/guard.mjs","timeout":5}]}）。

        两处配置合并，同一条（事件 + 命令 + matcher + 超时）只跑一次。解析失败
        一律降级为"没有钩子"，不影响任务启动；坏条目进 ``warnings`` 进审计。
        ``disabled``（上游 disableAllHooks）为真时一个钩子都不加载。
        ``prompt_runner`` 供 prompt/agent 型钩子把提示词交给模型（见
        :attr:`HookRunner.prompt_runner`）；``mcp_runner`` 供 mcp_tool 型钩子调
        MCP 工具（见 :attr:`HookRunner.mcp_runner`）。
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
                prompt_runner=prompt_runner,
                mcp_runner=mcp_runner,
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
        return cls(
            hooks,
            settle_timeout=settle,
            warnings=warnings,
            prompt_runner=prompt_runner,
            mcp_runner=mcp_runner,
        )

    def describe(self) -> dict:
        """审计面：每个事件几条钩子、来自哪里、配置期告警。不含命令正文。"""
        return {
            "disabled": self.disabled,
            "events": {event: len(specs) for event, specs in sorted(self.hooks.items())},
            "count": sum(len(specs) for specs in self.hooks.values()),
            "sources": sorted({spec.source for specs in self.hooks.values() for spec in specs}),
            "warnings": list(self.warnings),
        }

    def register_skill_hooks(
        self,
        skill_name: str,
        hooks: dict[str, list[HookSpec]],
    ) -> int:
        """把 Skill 声明的钩子并入当前运行器。

        上游在 Skill 加载时把 hooks 注册进会话级钩子集合，同一条
        （事件 + 命令 + matcher + 超时）只跑一次。这里沿用 ``from_env``
        的指纹去重口径，新注册的条目 ``source`` 标为 ``skill``。

        返回本次实际新增的条数。
        """
        if self.disabled or not hooks:
            return 0
        seen = {
            (event, spec.command, spec.match, spec.timeout)
            for event, specs in self.hooks.items()
            for spec in specs
        }
        added = 0
        for event, specs in hooks.items():
            for spec in specs:
                fingerprint = (event, spec.command, spec.match, spec.timeout)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                # Skill 钩子带着自己的 source，不改已有条目——
                # 已有条目是 settings/env 配的，优先级不受 Skill 影响。
                self.hooks.setdefault(event, []).append(spec)
                added += 1
        if added:
            self.warnings.append(f"Skill {skill_name} 注册了 {added} 条钩子")
        return added

    async def run(
        self,
        event: str,
        target: str = "",
        payload: dict | None = None,
        *,
        effort: str = "",
    ) -> list[HookResult]:
        """执行某事件下所有匹配的钩子。阻塞型事件遇到 deny 立即停止后续钩子。

        ``async_run`` 的钩子这里只登记后台任务并立即返回；它的结果留给下一次
        模型调用或收尾流程取走。阻断型事件上的 async 配置被忽略——拿不到结果的
        veto 不是 veto。

        ``effort`` 是本轮裁决出的思考强度：上游把它作为 ``CLAUDE_EFFORT`` 暴露给
        钩子命令的子进程，钩子脚本里 ``$CLAUDE_EFFORT`` 读到的就是这一档。
        没传（会话生命周期类钩子、没有档位的场合）就一个键都不加。
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
                self._spawn(spec, event, body, effort=effort)
                continue
            result = await self._execute(spec, event, body, effort=effort)
            results.append(result)
            self.results.append(result)
            if event in BLOCKING_EVENTS and result.denied:
                break
        return results

    def _spawn(self, spec: HookSpec, event: str, body: dict, *, effort: str = "") -> None:
        task = asyncio.create_task(self._run_detached(spec, event, body, effort=effort))
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

    async def _run_detached(self, spec: HookSpec, event: str, body: dict, *, effort: str = "") -> None:
        try:
            result = await self._execute(spec, event, body, effort=effort)
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

    async def _execute(
        self, spec: HookSpec, event: str, payload: dict, *, effort: str = ""
    ) -> HookResult:
        """按钩子类型分发。四类可执行的非 command 类型（http/prompt/agent/mcp_tool）
        的判定与退出码语义与 command 共用同一套 ``parse_hook_output``，差别只在
        "怎么拿到输出正文"；script 在这个构建里不可用，按上游同口径报错。"""
        if spec.kind == "command":
            return await self._execute_command(spec, event, payload, effort=effort)
        if spec.kind == "http":
            return await self._execute_http(spec, event, payload)
        if spec.kind in ("prompt", "agent"):
            return await self._execute_prompt(spec, event, payload)
        if spec.kind == "mcp_tool":
            return await self._execute_mcp_tool(spec, event, payload)
        if spec.kind == "script":
            return await self._execute_script(spec, event, payload)
        return HookResult(
            event=event,
            command=spec.command,
            status="failed",
            reason=f"未知钩子类型 {spec.kind}",
        )

    async def _execute_command(
        self, spec: HookSpec, event: str, payload: dict, *, effort: str = ""
    ) -> HookResult:
        started = time.perf_counter()
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        # 钩子命令的子进程环境：上游把本轮档位作为 CLAUDE_EFFORT 暴露给钩子脚本
        # （Uje 的 ``n.CLAUDE_EFFORT=e.effortLevel``）。这里只加这一个键，其余照旧
        # 继承父进程环境——收窄钩子环境是另一件事，不该混在这一次改动里。
        from blank_api.services.shell_env import apply_effort

        env = apply_effort(dict(os.environ), effort) if effort else None
        try:
            process = await _spawn_shell(spec.command, env=env)
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
        return self._finish(spec, event, text, err, exit_code=process.returncode, elapsed=elapsed)

    async def _execute_http(self, spec: HookSpec, event: str, payload: dict) -> HookResult:
        """http 型：把钩子输入 JSON POST 到 ``url``，响应正文按同一套解析。

        对齐上游 ``dfe``：非 2xx 的报错文案逐字是 ``HTTP ${statusCode} from ${url}``；
        成功时上游把 ``statusCode`` 记成 exitCode、``body`` 当 stdout，而成败看的是
        ``outcome``（2xx 即 success）——退出码不等于成败，所以这里显式传 ``ok=True``，
        不能拿"非 0 即失败"反推（否则 200 会被误判成失败）。
        """
        import httpx

        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        body = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            async with httpx.AsyncClient(timeout=spec.timeout) as client:
                response = await client.post(
                    spec.url,
                    content=body.encode("utf-8"),
                    headers={"content-type": "application/json"},
                    follow_redirects=False,
                )
        except Exception as exc:  # 连不上 / 超时 / TLS：一律非阻断失败
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"HTTP 钩子请求失败：{exc.__class__.__name__}",
                duration_ms=elapsed(),
            )
        status_code = response.status_code
        text = _decode(response.content).strip()
        if not 200 <= status_code < 300:
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"HTTP {status_code} from {spec.url}"[:300],
                duration_ms=elapsed(),
                output=text[:MAX_SNIPPET],
                exit_code=status_code,
            )
        return self._finish(
            spec, event, text, "", exit_code=status_code, elapsed=elapsed, ok=True
        )

    async def _execute_prompt(self, spec: HookSpec, event: str, payload: dict) -> HookResult:
        """prompt/agent 型：把提示词交给模型，模型正文按同一套解析。

        上游在没有会话上下文时直接抛 ``prompt-type hooks are not supported for
        ${event} events (no conversation context is available). Use a command-type
        hook instead.``——这里没有注入 ``prompt_runner`` 时逐字报同一句，作为一条
        非阻断错误（可见、不阻断、不注入），而不是静默跳过。
        """
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        if self.prompt_runner is None:
            label = "prompt-type" if spec.kind == "prompt" else "agent-type"
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=(
                    f"{label} hooks are not supported for {event} events "
                    "(no conversation context is available). Use a command-type hook instead."
                ),
                duration_ms=elapsed(),
            )
        try:
            text = await self.prompt_runner(spec.prompt, payload)
        except Exception as exc:
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"{spec.kind} 钩子执行失败：{exc.__class__.__name__}",
                duration_ms=elapsed(),
            )
        return self._finish(spec, event, str(text or ""), "", exit_code=0, elapsed=elapsed)

    async def _execute_mcp_tool(self, spec: HookSpec, event: str, payload: dict) -> HookResult:
        """mcp_tool 型：调用 ``server/tool``，工具正文按同一套解析。

        上游把 MCP 客户端从会话上下文里取出来再调；这里没有注入 ``mcp_runner``
        时报一条非阻断错误，指明缺的是 MCP 客户端，而不是把它当命令去跑。
        """
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        if self.mcp_runner is None:
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"mcp_tool 钩子缺少 MCP 客户端（{spec.server}/{spec.tool}），无法执行",
                duration_ms=elapsed(),
            )
        try:
            text = await self.mcp_runner(spec.server, spec.tool, payload)
        except Exception as exc:
            return HookResult(
                event=event,
                command=spec.command,
                status="failed",
                reason=f"mcp_tool 钩子执行失败：{exc.__class__.__name__}",
                duration_ms=elapsed(),
            )
        return self._finish(spec, event, str(text or ""), "", exit_code=0, elapsed=elapsed)

    async def _execute_script(self, spec: HookSpec, event: str, payload: dict) -> HookResult:
        """script 型：上游这个构建里不可用。

        上游的 ``Jst`` 直接 ``throw Error("script hooks are not available in this
        build")``——这里按同一句逐字报一条非阻断失败（可见、不阻断、不执行），
        而不是把它当命令去跑一个脚本文件。
        """
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        return HookResult(
            event=event,
            command=spec.command,
            status="failed",
            reason="script hooks are not available in this build",
            duration_ms=elapsed(),
        )

    def _finish(
        self,
        spec: HookSpec,
        event: str,
        text: str,
        err: str,
        *,
        exit_code: int | None,
        elapsed,
        ok: bool | None = None,
    ) -> HookResult:
        """四种类型共用的收尾：解析正文、套退出码语义、组装 HookResult。

        ``ok`` 显式给定成败（上游 http 型把 2xx 的状态码记成 exitCode、却仍是
        ``outcome:"success"``——退出码不等于成败，不能拿"非 0 即失败"反推）；
        不传时按 command/prompt/mcp 的口径，退出码 0 才算成功。
        """
        parsed = parse_hook_output(text)
        decision = str(parsed.get("decision") or "")
        reason = str(parsed.get("reason") or "")
        additional_context = str(parsed.get("additional_context") or "")
        if ok is None:
            status = "ok" if exit_code == 0 else "failed"
        else:
            status = "ok" if ok else "failed"
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
            prevent_continuation=bool(parsed.get("prevent_continuation")),
            stop_reason=str(parsed.get("stop_reason") or ""),
            session_title=str(parsed.get("session_title") or ""),
            initial_user_message=str(parsed.get("initial_user_message") or ""),
            watch_paths=list(parsed.get("watch_paths") or []),
            reload_skills=bool(parsed.get("reload_skills")),
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


def prompt_sha(text: str) -> str:
    """原文指纹：审计里记"改了哪一段"时用，不把原文本身落进事件。"""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""


# 上游 UserPromptExpansion 的载荷键（逐字）。钩子按这几个名字读，多一个少一个都会让
# "我配的钩子没拿到参数"这类问题查不出来。触发点用 `expansion_payload` 抽出这五键。
EXPAND_PAYLOAD_KEYS: tuple[str, ...] = (
    "expansion_type",
    "command_name",
    "command_args",
    "command_source",
    "prompt",
)


@dataclass
class ExpansionOutcome:
    """展开钩子（上游 UserPromptExpansion）对"这一次展开"的处置。

    与提交钩子的处置分开记：展开钩子否决的是"这次展开"，提交钩子否决的是"这次提交"，
    两者的阻断理由要能在同一个时间线上分得清。非阻断时它只贡献补上下文（notes）与
    "是否要求抑制原文"这两条事实。
    """

    # 钩子补的上下文，与提交钩子的补段一起进模型上下文（展开的在前）。
    notes: list[str] = field(default_factory=list)
    # 补上下文的钩子命令（掩码后）：注入事件的 sources 要能指到是谁补的，
    # 只说"注入了几段"而不说是谁，账就不全。
    sources: list[str] = field(default_factory=list)
    # 上游这条键只管阻断消息（decision 为 block 时从消息里略去原文）。非阻断时
    # 展开后的正文本来就不是原文，所以它天然成立，只记进审计。
    suppress_original: bool = False
    blocked: bool = False
    reason: str = ""


async def run_expansion_hooks(
    hooks: "HookRunner",
    recorder,
    expansion: dict | None,
    *,
    node: str = "receive_task",
) -> ExpansionOutcome:
    """斜杠命令展开那一刻的钩子（上游 UserPromptExpansion）。

    载荷就是上游那五个键，逐字对齐——钩子是按这几个名字读的，多一个少一个都会让
    "钩子没拿到参数"这类问题查不出来。``matcher`` 的比对对象取 ``expansion_type``
    （上游这张表的 fieldToMatch 是 source，取值 slash_command / register_repo_root；
    本仓库只有斜杠命令这一种展开）。

    三种处置：
    * 否决（退出码 2 / decision=deny）：整次提交作废。理由里带用户原文，钩子要求
      抑制原文时不带——与上游 ``suppressOriginalPrompt`` 同口径（"decision 为 block
      时从阻断消息里略去原文"）。
    * 补上下文：与提交钩子的补段一起进模型上下文。
    * 要求抑制原文：非阻断时展开后的正文本来就不是原文，这条天然成立，只记进审计
      让人看得见，不改送进模型的那一份。

    ``expansion`` 为空 = 这条 goal 不是斜杠命令展开来的：一个钩子都不跑，也不产生任何
    事件，与"没配钩子"同形。

    提交路径与插话（steer）路径共用这一份：两处都是"用户敲了斜杠命令、正文被展开"，
    钩子该拿到的载荷与该有的处置不该因为入口不同而分叉。
    """
    from blank_api.services.workspace import mask_path

    if not expansion:
        return ExpansionOutcome()
    payload = {key: str(expansion.get(key) or "") for key in EXPAND_PAYLOAD_KEYS}
    results = await hooks.run("user_prompt_expansion", payload["expansion_type"], payload)
    if not results:
        return ExpansionOutcome()
    await recorder.add(
        "hook.completed",
        node,
        {"event": "user_prompt_expansion", "target": payload["expansion_type"],
         "results": [item.as_dict() for item in results]},
    )
    denied = next((item for item in results if item.denied), None)
    if denied is not None:
        suppressed = any(item.suppress_original for item in results)
        original = payload["prompt"]
        reason = (denied.reason or "钩子否决了这次展开")[:300]
        if not suppressed and original:
            reason = f"{reason}\n用户原文：{original[:300]}"[:600]
        await recorder.add(
            "hook.prompt_blocked",
            node,
            {
                "event": "user_prompt_expansion",
                "reason": reason,
                "expansion_type": payload["expansion_type"],
                "command_name": payload["command_name"],
                "original_chars": len(original),
                "original_sha": prompt_sha(original),
                "original_omitted": suppressed,
                "exit_code": denied.exit_code,
                "sources": [mask_path(item.command)[:120] for item in results if item.denied],
            },
        )
        return ExpansionOutcome(blocked=True, reason=reason, suppress_original=suppressed)
    asking = next((item for item in results if item.asks), None)
    if asking is not None:
        # 与提交钩子同一口径：提交这一步没有确认界面，要求"先问一声"按否决处理。
        # 静默放行等于把钩子写下的条件丢掉，而它自己以为问过了。
        note = f"：{asking.reason}" if asking.reason else ""
        reason = f"展开钩子要求先确认（ask），提交这一步没有确认界面，按否决处理{note}"[:300]
        await recorder.add(
            "hook.prompt_blocked",
            node,
            {
                "event": "user_prompt_expansion",
                "reason": reason,
                "decision": "ask",
                "expansion_type": payload["expansion_type"],
                "command_name": payload["command_name"],
                "exit_code": asking.exit_code,
                "sources": [mask_path(item.command)[:120] for item in results if item.asks],
            },
        )
        return ExpansionOutcome(blocked=True, reason=reason)
    notes = [item.additional_context.strip() for item in results if item.additional_context.strip()]
    return ExpansionOutcome(
        notes=notes,
        sources=[mask_path(item.command)[:120] for item in results if item.additional_context.strip()],
        suppress_original=any(item.suppress_original for item in results),
    )


# 上游 TeammateIdle 阻止继续时的缺省文案（逐字）：钩子没给 stopReason 就用这句。
DEFAULT_IDLE_STOP_REASON = "TeammateIdle hook prevented continuation"


@dataclass
class IdleOutcome:
    """TeammateIdle 钩子对"成员转空闲"的处置。

    与展开/提交钩子不同，这条默认不否决：钩子不明确要求 ``preventContinuation``
    就照常继续，只贡献补上下文（notes）。
    """

    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    # 钩子要求阻止继续：调用方要停下"接下来还要继续的那一段"，成员转空闲。
    prevented: bool = False
    # 阻止的理由（上游渲染成 hook_stopped_continuation 的消息）。
    reason: str = ""


async def run_idle_hooks(
    hooks: "HookRunner",
    recorder,
    payload: dict | None,
    *,
    node: str = "team",
) -> IdleOutcome:
    """成员即将转入空闲那一刻的钩子（上游 TeammateIdle）。

    载荷逐字对齐上游的两个键：``teammate_name`` / ``team_name``（上游把 team_name
    标成 deprecated，仍按会话推导出来那一份传）；其余键是本仓库补的标识，供审计
    与 matcher 使用。``matcher`` 的比对对象取 ``teammate_name``。

    两种处置：
    * 阻止继续（``preventContinuation: true``）：理由取 ``stopReason``，缺省用上游
      那句 "TeammateIdle hook prevented continuation"。调用方据此停下接下来的一段。
    * 补上下文：成员转空闲之后没有"下一次模型调用"可注入，这份正文的去处是群记录
      （下一位发言者与用户读的都是它），审计里另记 count/chars。

    退出码 2 按上游同口径只算一条非阻断错误：可见、不注入、不阻止继续。
    """
    from blank_api.services.workspace import mask_path

    if not payload:
        return IdleOutcome()
    teammate = str(payload.get("teammate_name") or "")
    results = await hooks.run("teammate_idle", teammate, payload)
    if not results:
        return IdleOutcome()
    await recorder.add(
        "hook.completed",
        node,
        {
            "event": "teammate_idle",
            "target": teammate,
            "results": [item.as_dict() for item in results],
        },
    )
    notes = usable_context(results)
    sources = [
        mask_path(item.command)[:120] for item in results if item.additional_context.strip()
    ]
    errors = non_blocking_errors(results)
    if errors:
        await recorder.add(
            "hook.non_blocking_error",
            node,
            {
                "event": "teammate_idle",
                "target": teammate,
                "count": len(errors),
                "reasons": [
                    (item.additional_context or item.reason or "钩子以退出码 2 结束")[:300]
                    for item in errors
                ],
            },
        )
    prevented = next((item for item in results if item.prevent_continuation), None)
    if prevented is not None:
        reason = (prevented.stop_reason or DEFAULT_IDLE_STOP_REASON)[:300]
        await recorder.add(
            "hook.continuation_stopped",
            node,
            {
                "event": "teammate_idle",
                "teammate_name": teammate,
                "reason": reason,
                "exit_code": prevented.exit_code,
                "sources": [
                    mask_path(item.command)[:120] for item in results if item.prevent_continuation
                ],
            },
        )
        return IdleOutcome(notes=notes, sources=sources, prevented=True, reason=reason)
    if notes:
        await recorder.add(
            "hook.context_received",
            node,
            {
                "event": "teammate_idle",
                "count": len(notes),
                "chars": sum(len(note) for note in notes),
            },
        )
    return IdleOutcome(notes=notes, sources=sources)

