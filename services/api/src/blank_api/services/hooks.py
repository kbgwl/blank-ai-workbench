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
**默认关闭** —— 只有显式配置了 `BLANK_HOOKS` 才会执行任何外部命令。
钩子失败一律 **fail-open**（坏钩子不能把任务搞挂），但每次执行都会留审计记录。
"""

from __future__ import annotations

import asyncio
import json
import locale
import os
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
    "post_compact",
    # 隔离副本的创建/删除。非 git 工作区以前只有"整树复制"一条路；
    # 有了这两个事件，用户可以自己决定副本建在哪（远端、快照、专用卷）。
    "worktree_create",
    "worktree_remove",
)
# 只有这些事件可以阻止后续动作；其余是通知性质。
BLOCKING_EVENTS = frozenset({"pre_tool_use"})
DEFAULT_TIMEOUT = 10.0
MAX_SNIPPET = 500
# additionalContext 会进模型上下文：不设上限等于让一个坏钩子把窗口占满。
MAX_CONTEXT = 4000
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
    # （pre_tool_use）例外：它的价值就在于"等结果再决定"，异步化等于放弃 veto。
    async_run: bool = False

    def applies(self, target: str) -> bool:
        return not self.match or self.match in target


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
    """杀掉钩子。shell 模式起的是 cmd/sh 再派生子进程，只 kill 外层会留下孤儿。"""
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
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


class HookRunner:
    """按事件分发钩子。线程内单实例即可，状态只用于审计。"""

    def __init__(
        self,
        hooks: dict[str, list[HookSpec]] | None = None,
        *,
        settle_timeout: float = ASYNC_SETTLE_SECONDS,
    ) -> None:
        self.hooks: dict[str, list[HookSpec]] = hooks or {}
        self.results: list[HookResult] = []
        self.settle_timeout = max(0.0, min(float(settle_timeout), MAX_SETTLE_SECONDS))
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
    def from_env(cls, raw: str | None = None) -> "HookRunner":
        """`BLANK_HOOKS` 形如：
        {"pre_tool_use":[{"match":"run_shell","command":"node hooks/guard.mjs","timeout":5}]}
        解析失败一律降级为"没有钩子"，不影响任务启动。
        """
        text = (os.environ.get("BLANK_HOOKS", "") if raw is None else raw).strip()
        if not text:
            return cls()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(parsed, dict):
            return cls()
        try:
            settle = float(os.environ.get("BLANK_HOOK_SETTLE", "") or ASYNC_SETTLE_SECONDS)
        except ValueError:
            settle = ASYNC_SETTLE_SECONDS

        hooks: dict[str, list[HookSpec]] = {}
        for event, specs in parsed.items():
            if event not in HOOK_EVENTS or not isinstance(specs, list):
                continue
            built: list[HookSpec] = []
            for spec in specs:
                if not isinstance(spec, dict) or not spec.get("command"):
                    continue
                try:
                    timeout = float(spec.get("timeout") or DEFAULT_TIMEOUT)
                except (TypeError, ValueError):
                    timeout = DEFAULT_TIMEOUT
                built.append(
                    HookSpec(
                        command=str(spec["command"]),
                        match=str(spec.get("match") or ""),
                        timeout=max(0.5, min(timeout, 60.0)),
                        async_run=bool(
                            spec.get("async")
                            or spec.get("asyncRun")
                            or spec.get("background")
                        ),
                    )
                )
            if built:
                hooks[event] = built
        return cls(hooks, settle_timeout=settle)

    async def run(self, event: str, target: str = "", payload: dict | None = None) -> list[HookResult]:
        """执行某事件下所有匹配的钩子。阻塞型事件遇到 deny 立即停止后续钩子。

        ``async_run`` 的钩子这里只登记后台任务并立即返回；它的结果留给下一次
        模型调用或收尾流程取走。阻断型事件上的 async 配置被忽略——拿不到结果的
        veto 不是 veto。
        """
        specs = [spec for spec in self.hooks.get(event, []) if spec.applies(target)]
        if not specs:
            return []

        body = {"event": event, "target": target, **(payload or {})}
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
            process = await asyncio.create_subprocess_shell(
                spec.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
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
            exit_code=exit_code,
        )


def blocking_denial(results: list[HookResult]) -> HookResult | None:
    return next((item for item in results if item.denied), None)
