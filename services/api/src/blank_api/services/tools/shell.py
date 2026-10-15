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

ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
    "PYTHONIOENCODING",
    "NODE_OPTIONS",
    "VIRTUAL_ENV",
)
DESTRUCTIVE = re.compile(
    r"(?i)(\brm\s|\bdel\b|remove-item|\brmdir\b|\bmkfs\b|\bshutdown\b|\bformat\s+[a-z]:|"
    r"\bgit\s+push\b|\bdrop\s+(table|database)\b|\btruncate\b|\bmv\s+.*\s/|\brmdir\s+/s)"
)
NETWORK = re.compile(r"(?i)(\bcurl\b|\bwget\b|\bnetworkbrowser\b|\bssh\b|\bscp\b|https?://)")
MAX_OUTPUT_CHARS = 8000


def build_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST and value}
    env["PYTHONIOENCODING"] = env.get("PYTHONIOENCODING", "utf-8")
    return env


def assess_risk(command: str) -> str:
    if DESTRUCTIVE.search(command) or NETWORK.search(command):
        return "high"
    return "medium"


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _run_blocking(command: str | list[str], root: Path, timeout: float, shell: bool) -> dict:
    argv = command if shell else [str(part) for part in command]
    try:
        completed = subprocess.run(  # noqa: S603 - 命令来自工具层白名单校验之后
            argv,
            cwd=str(root),
            env=build_env(),
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
    command: str | list[str], root: Path, timeout: float, shell: bool = False
) -> dict:
    kwargs = {
        "cwd": str(root),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": build_env(),
    }
    try:
        if shell:
            process = await asyncio.create_subprocess_shell(command, **kwargs)  # type: ignore[arg-type]
        else:
            process = await asyncio.create_subprocess_exec(*command, **kwargs)  # type: ignore[misc]
    except NotImplementedError:
        # Selector 事件循环（Windows 上 uvicorn --reload 的子进程）不支持 asyncio 子进程，退回线程执行。
        return await asyncio.to_thread(_run_blocking, command, root, timeout, shell)
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
    command: str | list[str], root: Path, timeout: float, shell: bool = True
) -> dict:
    """BLANK_SANDBOX_ENABLED 时经沙箱执行（容器隔离 + 密钥过滤），否则退回本地进程。"""
    from blank_api.services.sandbox import SandboxPolicy, get_sandbox

    policy = SandboxPolicy()
    if policy.enabled:
        script = command if isinstance(command, str) else shlex.join(command)
        result = await get_sandbox(policy).run(script, workspace=str(root), timeout_seconds=timeout)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "sandbox": result.backend,
            "notes": list(result.notes),
        }
    result = await run_process(command, root, timeout, shell=shell)
    result["sandbox"] = None
    result["notes"] = []
    return result


def runs_in_docker() -> bool:
    """工具层据此决定用宿主解释器还是容器内解释器。"""
    from blank_api.services.sandbox import SandboxPolicy, get_sandbox

    return get_sandbox(SandboxPolicy()).name == "docker"


async def _run_shell(context: ToolContext, arguments: dict) -> ToolOutcome:
    command = str(arguments["command"]).strip()
    timeout = float(arguments.get("timeout_seconds", 30))
    result = await execute_command(command, context.root, timeout)
    stdout = truncate(mask_output(result["stdout"], context.root))[0]
    stderr = truncate(mask_output(result["stderr"], context.root))[0]
    if result["timed_out"]:
        return ToolOutcome(
            ok=False,
            status="timeout",
            output=stdout,
            message=f"命令超时（{timeout:.0f}s），进程已终止：{command}",
            data={"exit_code": result["exit_code"], "timed_out": True, "sandbox": result["sandbox"]},
        )
    combined = stdout if not stderr else f"{stdout}\n[stderr]\n{stderr}".strip()
    if result["exit_code"] != 0:
        return ToolOutcome(
            ok=False,
            status="failed",
            output=combined,
            message=f"命令返回非零退出码：{result['exit_code']}",
            data={"exit_code": result["exit_code"], "command": command, "sandbox": result["sandbox"]},
        )
    return ok(
        combined or "命令执行完成，无输出",
        exit_code=result["exit_code"],
        command=command,
        stdout=stdout,
        stderr=stderr,
        sandbox=result["sandbox"],
        isolation_notes=result["notes"],
    )


def shell_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="run_shell",
            description="在工作区根目录执行 Shell 命令，支持超时并回收进程，返回退出码与标准输出/错误",
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
