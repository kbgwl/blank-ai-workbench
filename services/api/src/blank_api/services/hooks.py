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
import os
import time
from dataclasses import dataclass

HOOK_EVENTS = (
    "session_start",
    "session_end",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "post_compact",
)
# 只有这些事件可以阻止后续动作；其余是通知性质。
BLOCKING_EVENTS = frozenset({"pre_tool_use"})
DEFAULT_TIMEOUT = 10.0
MAX_SNIPPET = 500


@dataclass(frozen=True)
class HookSpec:
    command: str
    match: str = ""
    timeout: float = DEFAULT_TIMEOUT

    def applies(self, target: str) -> bool:
        return not self.match or self.match in target


@dataclass
class HookResult:
    event: str
    command: str
    status: str  # ok / failed / timeout / skipped
    decision: str = ""  # allow / deny
    reason: str = ""
    duration_ms: int = 0
    output: str = ""

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

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
        return body


def parse_decision(text: str) -> tuple[str, str]:
    """从钩子 stdout 里容错解析决定。找不到就当作"不表态"。"""
    stripped = (text or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return "", ""
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    raw = parsed.get("decision") or parsed.get("permissionDecision") or parsed.get("permission")
    decision = str(raw).strip().lower()
    if decision in {"block", "deny", "denied", "reject"}:
        decision = "deny"
    elif decision in {"allow", "allowed", "approve", "approved", "continue"}:
        decision = "allow"
    else:
        decision = ""
    return decision, str(parsed.get("reason") or parsed.get("message") or "")[:300]


class HookRunner:
    """按事件分发钩子。线程内单实例即可，状态只用于审计。"""

    def __init__(self, hooks: dict[str, list[HookSpec]] | None = None) -> None:
        self.hooks: dict[str, list[HookSpec]] = hooks or {}
        self.results: list[HookResult] = []

    @property
    def enabled(self) -> bool:
        return bool(self.hooks)

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
                    )
                )
            if built:
                hooks[event] = built
        return cls(hooks)

    async def run(self, event: str, target: str = "", payload: dict | None = None) -> list[HookResult]:
        """执行某事件下所有匹配的钩子。阻塞型事件遇到 deny 立即停止后续钩子。"""
        specs = [spec for spec in self.hooks.get(event, []) if spec.applies(target)]
        if not specs:
            return []

        body = {"event": event, "target": target, **(payload or {})}
        results: list[HookResult] = []
        for spec in specs:
            result = await self._execute(spec, event, body)
            results.append(result)
            self.results.append(result)
            if event in BLOCKING_EVENTS and result.denied:
                break
        return results

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
            process.kill()
            await process.wait()
            # 超时不阻断：宁可放过，也不让坏钩子卡死任务
            return HookResult(
                event=event,
                command=spec.command,
                status="timeout",
                reason=f"钩子超时（{spec.timeout:g}s）",
                duration_ms=elapsed(),
            )

        text = (stdout or b"").decode("utf-8", "ignore").strip()
        err = (stderr or b"").decode("utf-8", "ignore").strip()
        decision, reason = parse_decision(text)
        status = "ok" if process.returncode == 0 else "failed"
        if status == "failed" and not reason:
            reason = (err or text)[:300] or f"退出码 {process.returncode}"
        return HookResult(
            event=event,
            command=spec.command,
            status=status,
            decision=decision,
            reason=reason,
            duration_ms=elapsed(),
            output=text[:MAX_SNIPPET],
        )


def blocking_denial(results: list[HookResult]) -> HookResult | None:
    return next((item for item in results if item.denied), None)
