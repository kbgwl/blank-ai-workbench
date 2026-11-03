"""run_shell: workspace-pinned command execution with timeout, capture and process kill."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, approval_key, ok
from blank_api.services.workspace import mask_path

DESTRUCTIVE = re.compile(
    r"(?i)(\brm\s|\bdel\b|remove-item|\brmdir\b|\bmkfs\b|\bshutdown\b|\bformat\s+[a-z]:|"
    r"\bgit\s+push\b|\bdrop\s+(table|database)\b|\btruncate\b|\bmv\s+.*\s/|\brmdir\s+/s)"
)
NETWORK = re.compile(r"(?i)(\bcurl\b|\bwget\b|\bnetworkbrowser\b|\bssh\b|\bscp\b|https?://)")
# 内联代码：一段字符串交给解释器执行，路径/命令正则看不见正文。
# `python -c "open('x','w')…"` 过去算 medium，默认模式下按 risk:auto 静默跑。
_INTERPRETER = re.compile(
    r"(?i)^\s*(?:[^\s\"']*[\\/])?"
    r"(python[0-9.]*|py|node|perl|ruby|php|pwsh|powershell|bash|sh|zsh|cmd)(?:\.exe)?\s+(.*)$"
)
_LONG_INLINE_FLAGS = {
    "-c",
    "-e",
    "-p",
    "-r",
    "-command",
    "/c",
    "-encodedcommand",
    "-enc",
}
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|]")
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


def _has_inline_code(command: str) -> bool:
    """解释器后面跟着内联代码旗标（`-c` / `-e` / `-Command` …）就算。

    旗标必须出现在第一个非旗标参数之前：`python script.py -c foo` 的 `-c` 是脚本的
    参数，不是解释器开关；`python -I -u -c '…'` 则是。
    """
    for segment in _SEGMENT_SPLIT.split(command):
        match = _INTERPRETER.match(segment)
        if not match:
            continue
        for token in match.group(2).split():
            lowered = token.strip("\"'").lower()
            if lowered in _LONG_INLINE_FLAGS:
                return True
            # `bash -lc` / `node -pe`：短旗标并写时含 c/e 也算
            if re.fullmatch(r"-[a-z]*[ce]", lowered):
                return True
            if not lowered.startswith("-"):
                break
    return False


def assess_risk(command: str) -> str:
    if DESTRUCTIVE.search(command) or NETWORK.search(command) or _has_inline_code(command):
        return "high"
    return "medium"


# 子进程输出的编码候选。顺序有意义：先按 utf-8 严格解，解不动再试本机 ANSI 代码页。
# 反过来的顺序会把本来就正确的 utf-8 中文解成乱码——GBK 解码器对多数多字节序列
# 也是"能解"，不报错，于是错误静默通过。所以只能"严格 utf-8 失败才回退"。
_FALLBACK_ENCODINGS = ("gbk", "mbcs") if os.name == "nt" else ("gbk", "latin-1")


def _decode(raw: bytes) -> str:
    """把子进程的输出字节解成文本。

    中文 Windows 上 cmd.exe 按 GBK 输出：原来一律 ``utf-8 + replace``，于是
    ``'ls' 不是内部或外部命令`` 会变成 ``'ls' �����ڲ����ⲿ���``。模型读不懂这句话，
    ``_UNKNOWN_COMMAND_MARKS`` 里的中文标记也永远匹配不上，方言提示形同虚设。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # 两套都不成立（真乱码）：宁可带替换符，也不能抛——一个字节异常不该让工具失败。
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


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def normalize_relative_executable(command: str | list[str], root: Path) -> str | list[str]:
    """Windows 上把"工作区里的相对可执行文件"补成绝对路径。

    模型写命令习惯用 POSIX 形状：`.venv/Scripts/python.exe -m pytest -q`。这在 sh 里
    完全合法，在 cmd 里却是另一回事——`/` 被当成参数分隔，cmd 只把 `.venv` 当命令名，
    于是报「'.venv' 不是内部或外部命令」，一条测试都没跑。用户看到的是"测试失败"，
    会去改代码；真实原因只是路径写法（实测任务 d7143491：改对的文件、跑不了的命令）。

    只处理**确实存在**的相对路径首段，其余原样返回：没匹配上就不猜，宁可让 cmd
    按原样报错，也不要改坏用户自己的命令。
    """
    if os.name != "nt":
        return command

    def fix(token: str) -> str:
        head = token.strip()
        if len(head) < 3 or head[0] in "\"'":
            return token
        if "/" not in head and "\\" not in head:
            return token  # 裸命令名，交给 PATH
        if _DRIVE_PREFIX.match(head) or head.startswith("\\\\"):
            return token  # 已经是绝对路径 / UNC
        candidate = Path(root) / head.replace("/", "\\")
        try:
            exists = candidate.exists()
        except OSError:
            exists = False
        if not exists:
            return token
        return str(candidate)

    if isinstance(command, list):
        if not command:
            return command
        return [fix(str(command[0])), *[str(part) for part in command[1:]]]

    text = str(command)
    stripped = text.lstrip()
    if not stripped:
        return command
    head, sep, tail = stripped.partition(" ")
    fixed = fix(head)
    if fixed == head:
        return command
    # 绝对路径含空格时必须加引号，否则 cmd 会把后半截当成参数
    rendered = f'"{fixed}"' if " " in fixed else fixed
    return f"{rendered}{sep}{tail}"


async def run_process(
    command: str | list[str],
    root: Path,
    timeout: float,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> dict:
    # 命令首段是工作区里的相对可执行文件时要先补成绝对路径（见函数注释）。
    # 放在这里而不是各个工具里：run_shell 与 run_tests、宿主直连与沙箱执行都经过它。
    command = normalize_relative_executable(command, root)
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


def _tier_enforcement(facts: dict, base: dict | None = None) -> dict:
    """把档位事实并进 enforcement：两处口径都在这里合，读的一侧只认一个形状。

    `network_enforced` 取交集（都真才算真）：Job 那条账说真、档位说没强制时，
    宁可报「没强制」，也不让一个更乐观的来源把另一处压掉。
    """
    out = dict(base or {})
    for key in ("filesystem_enforced", "network_enforced"):
        out[key] = bool(out.get(key)) and bool(facts.get(key))
    out.update(
        {
            "sandbox_tier": facts.get("tier", ""),
            "sandbox_tier_requested": facts.get("requested", ""),
            "sandbox_tier_degraded": bool(facts.get("degraded")),
            "tier_available": bool(facts.get("available", True)),
            "tier_reason": facts.get("reason", ""),
            "tier_notice": facts.get("notice", ""),
        }
    )
    return out


def _engine_enforcement(facts: dict) -> dict:
    """容器后端的账：DockerSandbox 不建 Job，限额来自容器参数。"""
    limits = bool(facts.get("limits_enforced"))
    return {
        "boundary_strength": facts.get("strength", "none"),
        "boundary_strength_label": facts.get("label", ""),
        "limits_applied": limits,
        "job_created": False,
        "job_assigned": False,
        "job_error": "容器后端不建 Windows Job Object",
        "memory_enforced": limits,
        "memory_bytes": 0,
        "pids_enforced": limits,
        "pids_limit": 0,
        "tree_kill_enforced": limits,
        "filesystem_enforced": bool(facts.get("filesystem_enforced")),
        "network_enforced": bool(facts.get("network_enforced")),
        "cpu_enforced": limits,
        "disk_enforced": False,
    }


async def execute_command(
    command: str | list[str],
    root: Path,
    timeout: float,
    shell: bool = True,
    env: dict[str, str] | None = None,
    safety=None,
    tier: str = "",
    host_bypass: bool = False,
) -> dict:
    """按任务档位执行命令：沙箱 / 宿主直连 / 拒绝，三种结果都带同一本账。

    ``safety`` 是能力边界；这里只用它的超时上限，允许/拒绝由网关与工具入口判定。
    把上限放在这一层，是因为它必须对 run_shell 与 run_tests 同时生效 ——
    只在其中一个工具里限时，另一个就是旁路。

    ``tier`` 只允许收紧，不可能把部署开关重新打开：`resolve_tier` 在开关关闭时
    会把它落成 off 并写明原因。`workspace-net` 在测不出网络强制的机器上直接拒绝，
    不会退回宿主执行。

    ``host_bypass`` 是**已获批准**的单次越界：它只对这一条命令生效，不改任务档位，
    也不会让“沙箱不可用”之外的路径悄悄放宽。调用方必须先过部署开关与审批，
    这里只负责执行与记账；两个标记必须同时出现——「跑在宿主上」和「是不是越界」。
    """
    from blank_api.services.sandbox import get_sandbox
    from blank_api.services.sandbox.runner import limits_not_applied
    from blank_api.services.sandbox.tiers import TIER_OFF, resolve_tier

    resolution = resolve_tier(tier)
    facts = resolution.as_dict()
    budget = float(timeout)
    if safety is not None:
        budget = min(budget, float(getattr(safety, "timeout_seconds", budget) or budget))
    if host_bypass:
        # 被绕过的不是“policy 对象”，而是此刻真正会生效的档位：off / 部署关闭时
        # 本来就没有沙箱可绕，此时 sandbox_bypass 必须是 False，不能借参数造一次假越界。
        active = resolution.policy.enabled and resolution.tier != TIER_OFF
        if active:
            reason = f"经人工批准单次绕过沙箱（原档位 {resolution.tier}），命令在宿主上执行"
        else:
            reason = "当前没有生效的沙箱（档位 off 或部署关闭），命令本就在宿主上执行"
        result = await run_process(command, root, budget, shell=shell, env=env)
        result["sandbox"] = None
        result["tier"] = facts
        result["sandbox_bypass"] = active
        result["bypass_reason"] = reason
        result["notes"] = [reason] + ([resolution.notice] if resolution.notice else [])
        result["timeout_budget"] = budget
        result["enforcement"] = _tier_enforcement(
            facts,
            {
                **limits_not_applied(reason),
                "host_execution": True,
                "sandbox_bypassed": active,
                "sandbox_bypass_reason": reason,
            },
        )
        return result
    if not resolution.available:
        # 拒绝也是一种结果，而且必须与"跑在宿主上"长得不一样：起进程的键一个都不写。
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": resolution.notice,
            "timed_out": False,
            "sandbox": None,
            "tier": facts,
            "tier_blocked": True,
            "notes": [resolution.notice],
            "enforcement": _tier_enforcement(facts, limits_not_applied(resolution.notice)),
            "timeout_budget": budget,
        }
    policy = resolution.policy
    if policy.enabled:
        # 不在这一层拼串：容器档跑在 `sh -c` 里（POSIX 引号），本机档跑在 cmd.exe 里
        # （CreateProcess 规则）。谁知道自己跑在哪种壳里，谁就自己拼——调用方提前
        # 用 shlex.join 拼好，等于把 POSIX 规则硬塞给 cmd（#145 的真缺陷）。
        result = await get_sandbox(policy).run(command, workspace=str(root), timeout_seconds=budget)
        enforcement = getattr(result, "enforcement", {}) or {}
        if not enforcement:
            enforcement = _engine_enforcement(facts)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "sandbox": result.backend,
            "tier": facts,
            "notes": [*result.notes, resolution.notice],
            # 这一次执行实际被什么强制（Job 限额 / 边界强度）。界面读它，不读 notes。
            "enforcement": _tier_enforcement(facts, enforcement),
            "timeout_budget": budget,
        }
    result = await run_process(command, root, budget, shell=shell, env=env)
    result["sandbox"] = None
    result["tier"] = facts
    result["notes"] = [resolution.notice]
    result["timeout_budget"] = budget
    # 关档也得有一本账：不写这一行，界面就只能拿 policy 的配置值当生效值展示。
    result["enforcement"] = _tier_enforcement(
        facts,
        limits_not_applied(f"{resolution.notice}；policy 里的限额没有对它生效"),
    )
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


def runs_in_docker(tier: str = "") -> bool:
    """工具层据此决定用宿主解释器还是容器内解释器。

    `off` 档不建容器，所以即使部署里装了 Docker 也必须用宿主解释器；
    档位解析在这里与执行层走同一个函数。
    """
    from blank_api.services.sandbox import get_sandbox
    from blank_api.services.sandbox.tiers import resolve_tier

    resolution = resolve_tier(tier)
    if not resolution.available or not resolution.policy.enabled:
        return False
    return get_sandbox(resolution.policy).name == "docker"


async def _record_consumed(context: ToolContext, key: str) -> None:
    """把「这次一次性批准已经被用掉」写进事件账。

    只 discard 内存里的 approved_actions 是不够的：审批恢复会从 `approval.granted`
    事件回放，把同一枚 key 重新写回来。于是「批准一次 → 执行一次 → 同样的命令再挂卡」
    在跨 resume 后会变成「批准一次 → 执行两次」，用户的驳回也被回放掉的批准顶掉。
    事件按 seq 回放，消费排在授权之后，先记后用，重启/恢复都不丢。
    """
    from blank_api.services.agent.events import EventRecorder

    factory = getattr(context, "session_factory", None)
    task_id = str(getattr(context, "task_id", "") or "")
    if factory is None or not task_id:
        return
    recorder = EventRecorder(factory, task_id, trace_id=str(getattr(context, "trace_id", "") or ""))
    await recorder.add(
        "approval.consumed",
        "execute_action",
        {"tool": "run_shell", "approval_key": key, "reason": "sandbox:host_bypass"},
    )


def _bypass_data(result: dict) -> dict:
    """越界执行的事实字段；没走过越界分支的调用不写，免得把"没越界"和"没记录"混在一起。"""
    if "sandbox_bypass" not in result:
        return {}
    return {
        "sandbox_bypass": bool(result.get("sandbox_bypass")),
        "bypass_reason": str(result.get("bypass_reason") or ""),
    }


async def _run_shell(context: ToolContext, arguments: dict) -> ToolOutcome:
    blocked = policy_gate(context, "run_shell", arguments)
    if blocked is not None:
        return blocked
    command = str(arguments["command"]).strip()
    timeout = float(arguments.get("timeout_seconds", 30))
    # coding-008：模型直接写 `python -m pytest` 时，把它换成工作区 venv 解释器，
    # 否则会落到系统解释器（可能没 pytest），把"跑不了测试"误判成"测试没过"。
    command = normalize_python_command(command, context.root)
    from blank_api.services.sandbox.policy import host_bypass_enabled
    from blank_api.services.sandbox.tiers import host_bypass_requested

    requested = host_bypass_requested("run_shell", arguments)
    approved = False
    if requested:
        # 这是入口的第二道闸：网关已经判过一次，但直接调 handler 的路径也必须守住。
        if not host_bypass_enabled():
            return ToolOutcome(
                ok=False,
                status="denied",
                message=(
                    "单次越界宿主执行未开启（部署开关 BLANK_SANDBOX_ALLOW_HOST_BYPASS），"
                    "命令未执行。"
                ),
                data={"sandbox_bypass": False, "permission_rule": "sandbox:host_bypass_disabled"},
            )
        permissions = getattr(context, "permissions", None)
        key = approval_key("run_shell", arguments)
        if permissions is not None and key in permissions.approved_actions:
            approved = True
            # 越界是单次授权：用掉就销账，同一条命令再跑必须重新问。
            permissions.approved_actions.discard(key)
            await _record_consumed(context, key)
    if requested and not approved:
        return ToolOutcome(
            ok=False,
            status="denied",
            message="单次越界宿主执行需要逐条人工批准，本次未获批准，命令未执行。",
            data={"sandbox_bypass": False, "permission_rule": "sandbox:host_bypass"},
        )
    # 有 shell 快照就用快照里的环境（含被前置的项目 venv），保证多步命令解析到同一个解释器
    snapshot = getattr(context, "shell", None)
    env = snapshot.env_for() if hasattr(snapshot, "env_for") else None
    safety = getattr(context, "safety", None)
    result = await execute_command(
        command,
        context.root,
        timeout,
        env=env,
        safety=safety,
        tier=getattr(context, "sandbox_tier", "") or "",
        host_bypass=approved,
    )
    timeout = result.get("timeout_budget", timeout)
    stdout = truncate(mask_output(result["stdout"], context.root))[0]
    stderr = truncate(mask_output(result["stderr"], context.root))[0]
    if result.get("tier_blocked"):
        # 档位不提供时命令根本没起：不许写成 sandbox=None（那会被读成"跑在宿主上"）。
        return ToolOutcome(
            ok=False,
            status="denied",
            output=stdout,
            message=f"沙箱档位不可用，命令未执行：{result.get('stderr') or ''}".strip(),
            data={
                "tier": result.get("tier", {}),
                "tier_blocked": True,
                "enforcement": result["enforcement"],
                **_bypass_data(result),
            },
        )
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
                "tier": result.get("tier", {}),
                "enforcement": result["enforcement"],
                **_bypass_data(result),
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
                "tier": result.get("tier", {}),
                # 限额被内核拒的时候走的正是这一支（非零退出码），账必须记在这里才看得见。
                "enforcement": result["enforcement"],
                **_bypass_data(result),
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
        tier=result.get("tier", {}),
        enforcement=result["enforcement"],
        **_bypass_data(result),
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
    from blank_api.services.sandbox.tiers import HOST_BYPASS

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
                "只有当沙箱导致命令无法完成、且确有必要时才传 sandbox=\"host\" 请求单次越界："
                "该请求默认被拒绝，开启后也必须逐条人工批准，仅在宿主上执行这一次。"
            ),
            input_schema={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout_seconds": {"type": "number", "description": "超时秒数，默认 30"},
                    "sandbox": {
                        "type": "string",
                        "enum": [HOST_BYPASS],
                        "description": (
                            "传 host 表示请求这条命令在宿主上执行（单次越界）："
                            "默认关闭，需部署开关与逐条人工批准；不传则按任务档位执行。"
                        ),
                    },
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
            handler=_run_shell,
            timeout_seconds=330.0,
        )
    ]


PROJECT_VENV_PYTHONS = (
    (".venv", "Scripts", "python.exe"),
    ("venv", "Scripts", "python.exe"),
    (".venv", "bin", "python"),
    ("venv", "bin", "python"),
)


def project_python(root) -> str:
    """工作区里的虚拟环境解释器；没有就空串。

    桌面版带的是一个干净的 Python 运行时，**没有 pytest**。用户的项目依赖装在自己的
    `.venv` 里，旧逻辑却一律用 `sys.executable -m pytest`，于是"这台机器上跑不了测试"，
    和"测试没通过"混成一句话（真实台账里出现过：代码已改对、断言已通过，
    只因解释器没 pytest 而被判失败）。项目里有 venv 就用它，这是唯一知道项目依赖的解释器。
    """
    if root is None:
        return ""
    try:
        base = Path(root)
    except TypeError:
        return ""
    for parts in PROJECT_VENV_PYTHONS:
        candidate = base.joinpath(*parts)
        if candidate.exists():
            return str(candidate)
    return ""


def python_test_command(root=None) -> list[str]:
    return [project_python(root) or sys.executable, "-m", "pytest", "-q"]


# 裸解释器写法（不带路径、不带扩展名）。模型爱写 `python -m pytest`，在 Windows 上
# 这个 `python` 解析到系统解释器（3.13，没装 pytest），于是"测试跑不了"被当成
# "测试没过"（真实台账 coding-008：代码改对了，却因解释器没 pytest 被判失败）。
# 工作区自己的 venv 才是知道项目依赖的解释器，优先用它。
_BARE_PYTHON = ("python", "python3", "py")


def normalize_python_command(command: str | list[str], root) -> str | list[str]:
    """把命令首段的裸 `python`/`python3`/`py` 换成工作区的 venv 解释器。

    只换**确实存在**的项目 venv，其余原样返回——没匹配上就不猜。用于处理模型直接
    写 `run_shell("python -m pytest …")` 而非走 `run_tests` 的情况：那一路不经过
    `python_test_command`，会落到系统解释器。

    注意：这跟 `normalize_relative_executable` 是两件事。那个补的是
    `.venv/Scripts/python.exe` 这种相对路径首段；这个补的是裸 `python`。两条都
    在服务 `run_process` 之前，分别覆盖不同的写法。
    """
    venv = project_python(root)
    if not venv:
        return command

    def fix(token: str) -> str:
        head = token.strip().strip('"').strip("'")
        if head in _BARE_PYTHON:
            return venv
        return token

    if isinstance(command, list):
        if not command:
            return command
        return [fix(str(command[0])), *[str(part) for part in command[1:]]]

    text = str(command)
    stripped = text.lstrip()
    if not stripped:
        return command
    # 只取首个"词"（到第一个空白或引号为止）
    match = re.match(r'\s*["\']?([^\s"\']+)', text)
    if not match:
        return command
    head = match.group(1)
    tail = text[match.end():]
    fixed = fix(head)
    if fixed == head:
        return command
    # 绝对路径含空格时必须加引号（和 normalize_relative_executable 同样的原因）
    rendered = f'"{fixed}"' if " " in fixed else fixed
    return f"{rendered}{tail}"


# ── 临时验证脚本清理（v162 §7.3：coding-039）────────────────────────────────
# 模型为了"自证清白"常在工作区写一个 `verify_*.py` / `*_check.py` / `temp_*.py`，
# 验证完却忘了删。判分器按 expectedFiles 硬比对，这些遗留文件就记成 misEditedFiles。
# 这里把"显然是临时产物"的文件挑出来，交付前清场——它们不在任何任务的预期产出里。
SCRATCH_FILE_PATTERNS = (
    "verify_*.py",
    "*_verify.py",
    "*_check.py",
    "temp_*.py",
    "tmp_*.py",
    "scratch_*.py",
    "test_scratch*.py",
    "*.scratch.py",
)

SCRATCH_GLOBS = (
    "verify_*",
    "*_verify",
    "*_check",
    "temp_*",
    "tmp_*",
    "scratch_*",
)


def list_scratch_files(root) -> list[str]:
    """返回 root 下匹配临时候选模式的文件路径（相对 root）。"""
    try:
        base = Path(root)
    except TypeError:
        return []
    if not base.exists():
        return []
    found: list[str] = []
    for pattern in SCRATCH_FILE_PATTERNS:
        for path in base.glob(pattern):
            if path.is_file():
                found.append(str(path.relative_to(base)))
    return sorted(set(found))


def cleanup_scratch_files(root, keep: Iterable[str] | None = None) -> list[str]:
    """删除工作区里匹配临时候选模式的文件，但保留 `keep` 里列出的（相对路径）。

    返回实际删除的文件（相对路径）。`keep` 一般传任务声明的 expectedFiles，避免误删
    用户本来就想要的脚本。删前不递归子目录以外的随机位置，只在 root 直接一层 glob——
    临时脚本几乎都落在工作区根，深目录的不动，宁可多留不可误删。
    """
    keep_set = {str(p) for p in (keep or [])}
    removed: list[str] = []
    for rel in list_scratch_files(root):
        if rel in keep_set:
            continue
        try:
            Path(root) / rel  # 已校验存在
            (Path(root) / rel).unlink(missing_ok=True)
            removed.append(rel)
        except OSError:
            continue
    return removed
