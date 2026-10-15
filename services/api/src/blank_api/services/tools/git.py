"""git: read-only version control inspection."""

from __future__ import annotations

import asyncio

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.shell import mask_output, truncate

READ_ONLY_SUBCOMMANDS = ("status", "diff", "log")
MAX_LOG_LINES = 50


def assess_git_risk(arguments: dict) -> str:
    return "low" if arguments.get("subcommand") in READ_ONLY_SUBCOMMANDS else "critical"


def _argv(arguments: dict) -> list[str]:
    subcommand = arguments["subcommand"]
    argv = ["git", subcommand]
    if subcommand == "log":
        limit = min(int(arguments.get("lines", 20)), MAX_LOG_LINES)
        argv += [f"--max-count={limit}", "--oneline"]
    if subcommand == "diff" and arguments.get("path"):
        argv += ["--", arguments["path"]]
    return argv


async def _git(context: ToolContext, arguments: dict) -> ToolOutcome:
    try:
        process = await asyncio.create_subprocess_exec(
            *_argv(arguments),
            cwd=str(context.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=30)
    except FileNotFoundError:
        return ToolOutcome(ok=False, status="error", message="Git 不可用：未找到 git 可执行文件")
    except asyncio.TimeoutError:
        return ToolOutcome(ok=False, status="timeout", message="Git 命令超时，已终止")
    stdout = truncate(mask_output(raw_out.decode("utf-8", errors="replace"), context.root))[0]
    stderr = truncate(mask_output(raw_err.decode("utf-8", errors="replace"), context.root))[0]
    if process.returncode != 0:
        return ToolOutcome(
            ok=False,
            status="failed",
            output=stdout,
            message=stderr or f"git {arguments['subcommand']} 返回退出码 {process.returncode}",
            data={"exit_code": process.returncode, "is_repository": False},
        )
    return ok(
        stdout or "无输出",
        subcommand=arguments["subcommand"],
        exit_code=process.returncode,
        is_repository=True,
    )


def git_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="git",
            description="查看仓库状态与改动，仅开放只读子命令：status / diff / log",
            input_schema={
                "type": "object",
                "required": ["subcommand"],
                "properties": {
                    "subcommand": {
                        "type": "string",
                        "enum": list(READ_ONLY_SUBCOMMANDS),
                        "description": "只读子命令",
                    },
                    "path": {"type": "string", "description": "diff 时限定文件"},
                    "lines": {"type": "integer", "description": "log 条数，最多 50"},
                },
            },
            risk_level="low",
            risk_for=assess_git_risk,
            handler=_git,
            timeout_seconds=40.0,
        )
    ]
