"""run_tests: detect the project's test command and execute it inside the workspace."""

from __future__ import annotations

import json
import re
import sys

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.shell import (
    execute_command,
    mask_output,
    policy_gate,
    python_test_command,
    runs_in_docker,
    truncate,
)

SUMMARY = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|error)")
PYTEST_MARKERS = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")
CONTAINER_PYTHON = "python3"


def detect_test_command(root) -> tuple[list[str], bool] | None:
    """Return (argv, use_shell) for the detected test runner, or None."""
    package_json = root / "package.json"
    if package_json.exists():
        try:
            manifest = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}
        if (manifest.get("scripts") or {}).get("test"):
            return ["npm", "test"], True
    for marker in PYTEST_MARKERS:
        if (root / marker).exists():
            return python_test_command(), False
    if (root / "tests").is_dir():
        return python_test_command(), False
    if any(root.glob("test_*.py")) or any(root.glob("*_test.py")):
        return python_test_command(), False
    return None


def summarize_exit_codes(output: str) -> dict:
    counts: dict[str, int] = {}
    for amount, kind in SUMMARY.findall(output):
        counts[kind.rstrip("s")] = counts.get(kind.rstrip("s"), 0) + int(amount)
    return counts


async def _run_tests(context: ToolContext, arguments: dict) -> ToolOutcome:
    blocked = policy_gate(context, "run_tests", arguments)
    if blocked is not None:
        return blocked
    timeout = float(arguments.get("timeout_seconds", 180))
    tier = getattr(context, "sandbox_tier", "") or ""
    override = arguments.get("command")
    if override:
        argv: list[str] | str = str(override)
        shell = True
    else:
        detected = detect_test_command(context.root)
        if detected is None:
            return ToolOutcome(
                ok=False,
                status="error",
                message="未识别到测试命令：工作区中既没有 pytest 配置，也没有 package.json 的 test 脚本",
            )
        argv, shell = detected
        if runs_in_docker(tier) and argv and argv[0] == sys.executable:
            # 宿主虚拟环境不在容器里，测试要改用镜像自带的解释器
            argv = [CONTAINER_PYTHON, *argv[1:]]
        if shell:
            argv = " ".join(argv)
    result = await execute_command(
        argv,
        context.root,
        timeout,
        shell=shell,
        safety=getattr(context, "safety", None),
        tier=tier,
    )
    timeout = result.get("timeout_budget", timeout)
    stdout = truncate(mask_output(result["stdout"], context.root))[0]
    stderr = truncate(mask_output(result["stderr"], context.root))[0]
    combined = stdout if not stderr else f"{stdout}\n[stderr]\n{stderr}".strip()
    command_text = argv if isinstance(argv, str) else " ".join(argv)
    if result.get("tier_blocked"):
        return ToolOutcome(
            ok=False,
            status="denied",
            output=combined,
            message=f"沙箱档位不可用，测试未执行：{result.get('stderr') or ''}".strip(),
            data={"tier": result.get("tier", {}), "tier_blocked": True,
                  "enforcement": result["enforcement"]},
        )
    if result["timed_out"]:
        return ToolOutcome(
            ok=False,
            status="timeout",
            output=combined,
            message=f"测试命令超时（{timeout:.0f}s），进程已终止：{command_text}",
            data={"command": command_text, "timed_out": True, "sandbox": result["sandbox"],
                  "tier": result.get("tier", {}), "enforcement": result["enforcement"]},
        )
    passed = result["exit_code"] == 0
    return ToolOutcome(
        ok=passed,
        status="ok" if passed else "failed",
        output=combined,
        message="" if passed else f"测试未通过，退出码 {result['exit_code']}",
        data={
            "command": command_text,
            "exit_code": result["exit_code"],
            "passed": passed,
            "sandbox": result["sandbox"],
            "tier": result.get("tier", {}),
            # 测试跑不动的原因之一可能是限额把子进程拦了；没有这一行，用户只能看到"退出码非零"。
            "enforcement": result["enforcement"],
            "summary": summarize_exit_codes(combined),
        },
    )


def test_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="run_tests",
            description=(
                "运行项目测试。不传 command 时自动识别：存在 pytest.ini/pyproject.toml/tests 目录时执行 pytest，"
                "存在 package.json test 脚本时执行 npm test"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "自定义测试命令，留空则自动识别"},
                    "timeout_seconds": {"type": "number", "description": "超时秒数，默认 180"},
                },
            },
            risk_level="medium",
            handler=_run_tests,
            timeout_seconds=200.0,
        )
    ]
