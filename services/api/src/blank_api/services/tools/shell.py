"""run_shell: workspace-pinned command execution with timeout, capture and process kill."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.workspace import mask_path

DESTRUCTIVE = re.compile(
    r"(?i)(\brm\s|\bdel\b|remove-item|\brmdir\b|\bmkfs\b|\bshutdown\b|\bformat\s+[a-z]:|"
    r"\bgit\s+push\b|\bdrop\s+(table|database)\b|\btruncate\b|\bmv\s+.*\s/|\brmdir\s+/s)"
)
NETWORK = re.compile(r"(?i)(\bcurl\b|\bwget\b|\bnetworkbrowser\b|\bssh\b|\bscp\b|https?://)")
MAX_OUTPUT_CHARS = 8000


def build_env(policy: str | None = None) -> dict[str, str]:
    """子进程环境：allowlist + 密钥名过滤，策略见 services/shell_env.py。

    没有快照时（单测、外部集成）退回同一个过滤函数，口径与运行时一致，
    不会出现"测试能跑、线上把 OPENAI_API_KEY 漏给子进程"这种漂移。
    """
    from blank_api.services.shell_env import filter_env
    from blank_api.services.shell_env import _policy_from_env

    env = filter_env(os.environ, policy=policy or _policy_from_env())
    env["PYTHONIOENCODING"] = env.get("PYTHONIOENCODING", "utf-8")
    return env


def assess_risk(command: str) -> str:
    if DESTRUCTIVE.search(command) or NETWORK.search(command):
        return "high"
    return "medium"


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _run_blocking(
    command: str | list[str],
    root: Path,
    timeout: float,
    shell: bool,
    env: dict[str, str] | None = None,
) -> dict:
    argv = command if shell else [str(part) for part in command]
    try:
        completed = subprocess.run(  # noqa: S603 - 命令来自工具层白名单校验之后
            argv,
            cwd=str(root),
            env=env or build_env(),
            capture_output=True,
            timeout=timeout,
            shell=shell,  # noqa: S602 - shell=True 时 argv 已是完整命令字符串
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "stdout": _decode(exc.stdout or b""),
            "stderr": _decode(exc.stderr or b""),
            "timed_out": True,
        }
    return {
        "exit_code": completed.returncode,
        "stdout": _decode(completed.stdout),
        "stderr": _decode(completed.stderr),
        "timed_out": False,
    }


async def run_process(
    command: str | list[str],
    root: Path,
    timeout: float,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> dict:
    kwargs = {
        "cwd": str(root),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": env or build_env(),
    }
    try:
        if shell:
            process = await asyncio.create_subprocess_shell(command, **kwargs)  # type: ignore[arg-type]
        else:
            process = await asyncio.create_subprocess_exec(*command, **kwargs)  # type: ignore[misc]
    except NotImplementedError:
        # Selector 事件循环（Windows 上 uvicorn --reload 的子进程）不支持 asyncio 子进程，退回线程执行。
        return await asyncio.to_thread(_run_blocking, command, root, timeout, shell, env)
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            process.terminate()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        except (asyncio.TimeoutError, ProcessLookupError):
            process.kill()
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                stdout, stderr = b"", b""
    return {
        "exit_code": process.returncode,
        "stdout": _decode(stdout),
        "stderr": _decode(stderr),
        "timed_out": timed_out,
    }


def mask_output(text: str, root: Path) -> str:
    return text.replace(str(root), mask_path(root)).replace(
        str(Path(root).resolve()), mask_path(root)
    )


def truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS] + "\n…（输出已截断）", True


async def execute_command(
    command: str | list[str],
    root: Path,
    timeout: float,
    shell: bool = True,
    env: dict[str, str] | None = None,
    safety=None,
) -> dict:
    """BLANK_SANDBOX_ENABLED 时经沙箱执行（容器隔离 + 密钥过滤），否则退回本地进程。

    ``safety`` 是能力边界；这里只用它的超时上限，允许/拒绝由网关与工具入口判定。
    把上限放在这一层，是因为它必须对 run_shell 与 run_tests 同时生效 ——
    只在其中一个工具里限时，另一个就是旁路。
    """
    from blank_api.services.sandbox import SandboxPolicy, get_sandbox

    policy = SandboxPolicy()
    budget = float(timeout)
    if safety is not None:
        budget = min(budget, float(getattr(safety, "timeout_seconds", budget) or budget))
    if policy.enabled:
        script = command if isinstance(command, str) else shlex.join(command)
        result = await get_sandbox(policy).run(script, workspace=str(root), timeout_seconds=budget)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "sandbox": result.backend,
            "notes": list(result.notes),
            "timeout_budget": budget,
        }
    result = await run_process(command, root, budget, shell=shell, env=env)
    result["sandbox"] = None
    result["notes"] = []
    result["timeout_budget"] = budget
    return result


def policy_gate(context: ToolContext, tool_name: str, arguments: dict):
    """工具入口的第二道闸：即使有人绕过网关直接调 handler，能力边界依然生效。

    返回 ToolOutcome 表示要拒绝；返回 None 表示放行（要不要再问一遍由网关决定）。
    """
    from blank_api.services.safety import SafetyPolicy

    policy = getattr(context, "safety", None) or SafetyPolicy.load()
    permissions = getattr(context, "permissions", None)
    mode = getattr(permissions, "mode", "default") if permissions is not None else "default"
    verdict = policy.gate(tool_name, arguments, context.root, mode=mode)
    if not verdict.denied:
        return None
    return ToolOutcome(
        ok=False,
        status="denied",
        message=verdict.reason,
        data={
            "policy": "safety",
            "rule": verdict.rule,
            "command_prefix": verdict.prefix,
            "hint": "security_settings",
        },
    )


def runs_in_docker() -> bool:
    """工具层据此决定用宿主解释器还是容器内解释器。"""
    from blank_api.services.sandbox import SandboxPolicy, get_sandbox

    return get_sandbox(SandboxPolicy()).name == "docker"


async def _run_shell(context: ToolContext, arguments: dict) -> ToolOutcome:
    blocked = policy_gate(context, "run_shell", arguments)
    if blocked is not None:
        return blocked
    command = str(arguments["command"]).strip()
    timeout = float(arguments.get("timeout_seconds", 30))
    # 有 shell 快照就用快照里的环境（含被前置的项目 venv），保证多步命令解析到同一个解释器
    snapshot = getattr(context, "shell", None)
    env = snapshot.env_for() if hasattr(snapshot, "env_for") else None
    safety = getattr(context, "safety", None)
    result = await execute_command(command, context.root, timeout, env=env, safety=safety)
    timeout = result.get("timeout_budget", timeout)
    stdout = truncate(mask_output(result["stdout"], context.root))[0]
    stderr = truncate(mask_output(result["stderr"], context.root))[0]
    if result["timed_out"]:
        return ToolOutcome(
            ok=False,
            status="timeout",
            output=stdout,
            message=f"命令超时（{timeout:.0f}s），进程已终止：{command}",
            data={
                "exit_code": result["exit_code"],
                "timed_out": True,
                "sandbox": result["sandbox"],
                "isolation": result["sandbox"] or "local",
            },
        )
    combined = stdout if not stderr else f"{stdout}\n[stderr]\n{stderr}".strip()
    if result["exit_code"] != 0:
        # 用错 shell 方言是最常见的失败原因，且一次提示就能改对，值得单独说清楚。
        hint = shell_mismatch_hint(command, stderr)
        message = f"命令返回非零退出码：{result['exit_code']}"
        if hint:
            message = f"{message}。{hint}"
        return ToolOutcome(
            ok=False,
            status="failed",
            output=combined,
            message=message,
            data={
                "exit_code": result["exit_code"],
                "command": command,
                "sandbox": result["sandbox"],
                "isolation": result["sandbox"] or "local",
                **({"hint": "windows_cmd_syntax"} if hint else {}),
            },
        )
    return ok(
        combined or "命令执行完成，无输出",
        exit_code=result["exit_code"],
        command=command,
        stdout=stdout,
        stderr=stderr,
        sandbox=result["sandbox"],
        isolation=result["sandbox"] or "local",
        isolation_notes=result["notes"],
    )


def shell_dialect() -> str:
    """这台机器上 run_shell 实际用的是哪种 shell。

    Windows 上是 cmd.exe。模型默认按 POSIX 写（pwd / ls / cat / grep），
    在这台机器上全是"不是内部或外部命令"——实测 83 次 shell 调用里 21 次失败，
    大头就是这个。错一次就重试一次，既慢又贵，而且完全没必要：让它一开始
    就知道该用哪套命令即可。
    """
    from blank_api.services.sandbox import SandboxPolicy, get_sandbox

    try:
        if get_sandbox(SandboxPolicy()).name == "docker":
            return "sh"
    except Exception:  # noqa: BLE001 - 探测沙箱失败不该影响工具定义
        pass
    return "cmd" if os.name == "nt" else "sh"


# cmd 认不出 POSIX 命令时的固定说法（中英文两套 Windows）
_UNKNOWN_COMMAND_MARKS = (
    "不是内部或外部命令",
    "is not recognized as an internal or external command",
    "CommandNotFoundException",
    "系统找不到指定的路径",
)
# 常见的 POSIX 命令 → cmd 等价写法。只在报错时用作提示，不自动改写命令：
# 自动改写会改变语义（`grep -r` 和 `findstr /s` 并不等价），猜错了更麻烦。
_POSIX_TO_CMD = {
    "pwd": "cd",
    "ls": "dir",
    "cat": "type",
    "grep": "findstr",
    "rm": "del",
    "cp": "copy",
    "mv": "move",
    "touch": "type nul > 文件名",
    "which": "where",
    "head": "more",
}


def shell_mismatch_hint(command: str, stderr: str) -> str:
    """命令失败是因为用错了 shell 方言时，给一句能立刻改对的提示。"""
    if shell_dialect() != "cmd":
        return ""
    if not any(mark in (stderr or "") for mark in _UNKNOWN_COMMAND_MARKS):
        return ""
    tokens = re.split(r"[\s;&|]+", command.strip())
    tip = next((f"{token} → {_POSIX_TO_CMD[token]}" for token in tokens if token in _POSIX_TO_CMD), "")
    base = (
        "这条命令用的是 POSIX 写法，本机 shell 是 Windows cmd.exe。"
        "请改成 cmd 命令（如 dir / type / findstr / where），或者直接改用专用工具"
        "（list_files 列目录、read_file 读文件、search_code 搜代码），不要再用 ls/cat/grep/pwd。"
    )
    return f"{base} 参考：{tip}" if tip else base


def shell_tools() -> list[ToolDefinition]:
    dialect = shell_dialect()
    syntax = (
        "当前 shell 是 Windows cmd.exe：用 dir / type / findstr / where / del，"
        "不要用 ls / cat / grep / pwd / rm。列目录、读文件、搜代码优先用专用工具。"
        if dialect == "cmd"
        else "当前 shell 是 sh：可以用 ls / cat / grep / pwd 等标准命令。"
    )
    return [
        ToolDefinition(
            name="run_shell",
            description=(
                f"在工作区根目录执行 Shell 命令，支持超时并回收进程，返回退出码与标准输出/错误。{syntax}"
            ),
            input_schema={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout_seconds": {"type": "number", "description": "超时秒数，默认 30"},
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
            handler=_run_shell,
            timeout_seconds=330.0,
        )
    ]


def python_test_command() -> list[str]:
    return [sys.executable, "-m", "pytest", "-q"]
