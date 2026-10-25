"""`blank doctor --sandbox`：在本机把八条执行边界逐条攻击一遍，再和账本对质。

设计稿（`docs/SANDBOX-WINDOWS-DESIGN.md` §5）要求每条边界都有一次**主动逃逸尝试**，
并且装置自检：先证明脚本真的能跑、监听端真的能收到连接，再拿结果说话。这里不做
"看配置猜边界"的事——探针在 `workspace` 档里真执行，行为结果和这次执行自己的
`enforcement` 账逐条比对：行为说没拦住、账本也说没强制，才算一致；任何一边说了
另一边没说的话，doctor 退出码就是 1。

状态只用四个词：
- `enforced`：攻击被系统拒绝（或限额被内核执行）；
- `not_enforced`：攻击成功，账本也必须承认没有强制；
- `unknown`：装置自己坏了或结果无法归因，**不冒充结论**；
- `not_tested`：这台机器/这个后端测不了（例如容器后端、未给任务号），写明原因。

用法：
    blank doctor --sandbox
    blank doctor --sandbox --json E:/tmp/doctor.json
    blank doctor --sandbox --task <task-id> --api http://127.0.0.1:8000/api/v1
    blank doctor --compare E:/tmp/a.json E:/tmp/b.json   # 两份报告的跨机对账（§78）
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import platform
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from blank_api.services.sandbox import boundary
from blank_api.services.sandbox.tiers import (
    TIER_OFF,
    TIER_WORKSPACE,
    TIER_WORKSPACE_NET,
    resolve_tier,
)

# 2 = 报告里带上了机器指纹（`host`）与 P-AUDIT 的跑法（`audit_task_id`）：跨机对账要靠这两处
# 判断"这是两台机器"还是"同一台跑了两次"。schema 1 的报告没有它们，对账必须拒绝而不是补一份。
SCHEMA_VERSION = 2

ENFORCED = "enforced"
NOT_ENFORCED = "not_enforced"
UNKNOWN = "unknown"
NOT_TESTED = "not_tested"
VERIFIED = "verified"  # 只给 P-AUDIT：证据链核对通过，不是"边界拦住了"

BOUNDARY_IDS = (
    "P-READ",
    "P-WRITE",
    "P-ENV",
    "P-NET",
    "P-PROC",
    "P-ESCAPE",
    "P-INHERIT",
    "P-AUDIT",
)

# Windows 的拒绝会以不同形状出现：Python 异常、Win32 错误码、中文系统文案。
# 只把"明确的拒绝"算成 enforced；其余非零退出都归 unknown，免得把"脚本写坏了"读成"拦住了"。
_DENIED_MARKERS = (
    "permissionerror",
    "winerror 5",
    "access is denied",
    "拒绝访问",
    "errno 13",
    "privilege not held",
)

_Runner = Callable[..., Awaitable[dict[str, Any]]]


def _denied(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DENIED_MARKERS)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def access_verdict(result: dict[str, Any], verb: str) -> tuple[str, str]:
    """把一次越界尝试翻成状态。成功 = 没拦住；明确拒绝 = 拦住了；其余不猜。"""
    output = f"{result.get('stderr') or ''}\n{result.get('stdout') or ''}"
    if result.get("exit_code") == 0:
        return NOT_ENFORCED, f"{verb}成功：系统没有拒绝"
    if _denied(output):
        return ENFORCED, f"{verb}被拒：{_first_line(output)}"
    if result.get("timed_out"):
        return UNKNOWN, f"{verb}超时：无法归因"
    return UNKNOWN, f"{verb}没有明确结论：{_first_line(output) or 'exit=' + str(result.get('exit_code'))}"


def pids_verdict(control: dict[str, Any], limited: dict[str, Any]) -> tuple[str, str]:
    """P-PROC：同一脚本先跑 128 个进程的额度，再跑 1 个。对照不通过就整条作废。"""
    if control.get("exit_code") != 0 or "pids-ok" not in (control.get("stdout") or ""):
        return UNKNOWN, "阳性对照没跑通：限额结论无从谈起"
    if limited.get("exit_code") == 0 and "pids-ok" in (limited.get("stdout") or ""):
        return NOT_ENFORCED, "pids=1 时第二个进程仍然起得来：限额没被内核执行"
    return ENFORCED, "pids=1 时第二个进程起不来：限额由内核执行"


def inherit_verdict(control_grew: bool, wrote: bool, stopped: bool) -> tuple[str, str]:
    if not control_grew:
        return UNKNOWN, "无 Job 的对照组孙进程没继续写：装置本身是坏的"
    if not wrote:
        return UNKNOWN, "Job 里的孙进程一次都没写成功：'被杀'与'没跑起来'分不清"
    if stopped:
        return ENFORCED, "父进程退出、Job 关闭后孙进程停止写心跳"
    return NOT_ENFORCED, "父进程退出、Job 已关闭，孙进程还在写：KILL_ON_JOB_CLOSE 没生效"


def row(
    probe_id: str,
    title: str,
    status: str,
    observed: str,
    *,
    account_key: str = "",
    account_value: Any = None,
    reason: str = "",
) -> dict[str, Any]:
    consistent: bool | None = None
    if status in (ENFORCED, NOT_ENFORCED) and account_value is not None:
        consistent = (status == ENFORCED) is bool(account_value)
    return {
        "id": probe_id,
        "title": title,
        "status": status,
        "observed": observed,
        "account_key": account_key,
        "account_value": account_value,
        "consistent": consistent,
        "reason": reason,
    }


def _write_script(workspace: Path, name: str, body: str) -> str:
    (workspace / name).write_text(body, encoding="utf-8")
    return name


@contextlib.contextmanager
def _temporary_env(name: str, value: str):
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


async def _probe_read(runner: _Runner, workspace: Path, outside: Path) -> tuple[str, str]:
    target = outside / "secret.txt"
    target.write_text("doctor-read-secret", encoding="utf-8")
    script = _write_script(
        workspace,
        "read_outside.py",
        "from pathlib import Path\n"
        f"print(Path(r'{target}').read_text(encoding='utf-8'))\n",
    )
    result = await runner(f'"{sys.executable}" {script}', workspace, 20, tier=TIER_WORKSPACE)
    return access_verdict(result, "读取工作区外文件")


async def _probe_write(runner: _Runner, workspace: Path, outside: Path) -> tuple[str, str]:
    target = outside / "escaped.txt"
    script = _write_script(
        workspace,
        "write_outside.py",
        "from pathlib import Path\n"
        f"Path(r'{target}').write_text('doctor-escaped', encoding='utf-8')\n"
        "print('wrote')\n",
    )
    result = await runner(f'"{sys.executable}" {script}', workspace, 20, tier=TIER_WORKSPACE)
    status, detail = access_verdict(result, "写入工作区外文件")
    if status == NOT_ENFORCED and target.read_text(encoding="utf-8") != "doctor-escaped":
        return UNKNOWN, "命令自称写入成功，但文件不在：结果无法归因"
    return status, detail


async def _probe_env(runner: _Runner, workspace: Path) -> tuple[str, str]:
    if sys.platform != "win32":
        return NOT_TESTED, "非 Windows：没有 HKCU\\Environment 这一条边界"
    script = _write_script(
        workspace,
        "read_env_key.py",
        "import winreg\n"
        "key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Environment')\n"
        "count = 0\n"
        "try:\n"
        "    while True:\n"
        "        winreg.EnumValue(key, count)\n"
        "        count += 1\n"
        "except OSError:\n"
        "    pass\n"
        "print('env-values', count)\n",
    )
    result = await runner(f'"{sys.executable}" {script}', workspace, 20, tier=TIER_WORKSPACE)
    return access_verdict(result, "读取 HKCU\\Environment")


async def _probe_escape(runner: _Runner, workspace: Path, outside: Path) -> tuple[str, str]:
    """P-ESCAPE：沙箱内进程试图给自己加一条 ACE，把边界放大。"""
    if sys.platform != "win32":
        return NOT_TESTED, "非 Windows：没有 icacls 这条自放大路径"
    target = outside / "secret.txt"
    if not target.exists():
        target.write_text("doctor-read-secret", encoding="utf-8")
    script = _write_script(
        workspace,
        "widen_acl.py",
        "import subprocess, sys\n"
        f"done = subprocess.run(['icacls', r'{target}', '/grant', '*S-1-1-0:F'],"
        " capture_output=True, text=True)\n"
        "sys.stdout.write(done.stdout)\n"
        "sys.stderr.write(done.stderr)\n"
        "sys.exit(done.returncode)\n",
    )
    result = await runner(f'"{sys.executable}" {script}', workspace, 20, tier=TIER_WORKSPACE)
    return access_verdict(result, "沙箱内给自己加文件 ACE（自放大）")


async def _probe_net(runner: _Runner, workspace: Path) -> tuple[str, str]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    server.settimeout(0.2)
    port = server.getsockname()[1]
    accepted: list[bytes] = []

    def serve() -> None:
        deadline = time.time() + 8
        while len(accepted) < 2 and time.time() < deadline:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                accepted.append(conn.recv(16))
            except OSError:
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        # 阳性对照：doctor 自己先连一次。监听端收不到任何数据时，下面的失败不能当成"网络被拦"。
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as control:
                control.sendall(b"doctor-host")
        except OSError as error:
            return UNKNOWN, f"阳性对照连不上本机监听端：{error}"

        script = _write_script(
            workspace,
            "connect_local.py",
            "import socket, sys\n"
            "s = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=3)\n"
            "s.sendall(b'doctor-child')\n"
            "s.close()\n"
            "print('connected')\n",
        )
        result = await runner(f'"{sys.executable}" {script} {port}', workspace, 20, tier=TIER_WORKSPACE)
        for _ in range(30):
            if b"doctor-child" in accepted:
                break
            time.sleep(0.05)
    finally:
        server.close()

    if b"doctor-host" not in accepted:
        return UNKNOWN, "阳性对照的数据没到监听端：装置本身不可信"
    if b"doctor-child" in accepted:
        return NOT_ENFORCED, "沙箱内 Python 连上了本机监听端：出网没被系统拦"
    if _denied(f"{result.get('stderr') or ''}{result.get('stdout') or ''}"):
        return ENFORCED, "沙箱内连接被拒：网络边界由系统强制"
    return UNKNOWN, f"连接没成功也没被明确拒绝：{_first_line(result.get('stderr') or '')}"


async def _probe_pids(runner: _Runner, workspace: Path) -> tuple[str, str]:
    if sys.platform != "win32":
        return NOT_TESTED, "非 Windows：没有 Job 活跃进程限额"
    script = _write_script(
        workspace,
        "pids_probe.py",
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', \"print('child-ok')\"], check=True)\n"
        "print('pids-ok')\n",
    )
    command = f'"{sys.executable}" {script}'
    with _temporary_env("BLANK_SANDBOX_PIDS", "128"):
        control = await runner(command, workspace, 20, tier=TIER_WORKSPACE)
    with _temporary_env("BLANK_SANDBOX_PIDS", "1"):
        limited = await runner(command, workspace, 20, tier=TIER_WORKSPACE)
    return pids_verdict(control, limited)


_BEAT_SCRIPT = """import os, sys, time
path = sys.argv[1]
with open(path, "a", encoding="utf-8") as fh:
    for _ in range(400):
        fh.write("x")
        fh.flush()
        os.fsync(fh.fileno())
        time.sleep(0.02)
"""

_SPAWN_SCRIPT = """import os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
child = subprocess.Popen(
    [sys.executable, os.path.join(here, "beat.py"), sys.argv[1]],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
time.sleep(0.6)
"""


def _kill_tree(pid: int | None) -> None:
    if pid is None or sys.platform != "win32":
        return
    import subprocess

    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


async def _probe_inherit(runner: _Runner, workspace: Path) -> tuple[str, str]:
    if sys.platform != "win32":
        return NOT_TESTED, "非 Windows：没有 Job 生命周期继承"
    (workspace / "beat.py").write_text(_BEAT_SCRIPT, encoding="utf-8")
    (workspace / "spawn.py").write_text(_SPAWN_SCRIPT, encoding="utf-8")
    beat = workspace / "beat.txt"
    command = f'"{sys.executable}" spawn.py beat.txt'

    control = await runner(command, workspace, 20, tier=TIER_OFF)
    control_pid: int | None = None
    try:
        control_pid = int((control.get("stdout") or "").strip().splitlines()[0])
        first = beat.stat().st_size
        time.sleep(0.8)
        control_grew = beat.stat().st_size > first
    except (ValueError, IndexError, OSError):
        control_grew = False
    finally:
        _kill_tree(control_pid)

    beat.write_text("", encoding="utf-8")
    result = await runner(command, workspace, 20, tier=TIER_WORKSPACE)
    pid: int | None = None
    try:
        pid = int((result.get("stdout") or "").strip().splitlines()[0])
        within_job = beat.stat().st_size
        time.sleep(0.8)
        stopped = beat.stat().st_size == within_job
    except (ValueError, IndexError, OSError):
        within_job, stopped = 0, False
    finally:
        _kill_tree(pid)
    return inherit_verdict(control_grew, within_job > 0, stopped)


AUDIT_KINDS = ("blocked", "mcp_host_process", "process", "in_process")


def audit_evidence(evidence: dict[str, Any]) -> tuple[str, str]:
    """P-AUDIT：证据包里每条工具调用都要有边界，且边界计数不能漏人。"""
    if not isinstance(evidence, dict):
        return UNKNOWN, "证据包不是对象"
    boundaries = evidence.get("boundaries")
    if not isinstance(boundaries, dict):
        return UNKNOWN, "证据包没有 boundaries：边界报告没产出"
    tools = evidence.get("tools") or []
    problems: list[str] = []
    for item in tools:
        name = item.get("name") or item.get("tool") or "?"
        kind = item.get("boundary_kind")
        if not kind:
            problems.append(f"{name} 没有 boundary_kind")
        elif kind not in (*AUDIT_KINDS, "unattributed"):
            problems.append(f"{name} 的 boundary_kind 不认识：{kind}")
        if "sandbox_mode" not in item:
            problems.append(f"{name} 没有 sandbox_mode")
    counts = boundaries.get("tool_calls") or {}
    total = sum(int(counts.get(kind, 0) or 0) for kind in (*AUDIT_KINDS, "unattributed"))
    if tools and total < len(tools):
        problems.append(f"边界计数 {total} 少于工具调用 {len(tools)}")
    mcp = boundaries.get("mcp") or {}
    if int(counts.get("mcp_host_process", 0) or 0) and not mcp.get("boundary"):
        problems.append("有 MCP 调用，但证据包没写 MCP 边界")
    if tools and not boundaries.get("sandbox_modes"):
        problems.append("有工具调用，但没有任何 sandbox_mode")
    if problems:
        return UNKNOWN, "证据链不成立：" + "；".join(problems[:5])
    return VERIFIED, f"{len(tools)} 条工具调用都带边界字段，计数一致"


def _fetch_evidence(api: str, task_id: str) -> dict[str, Any]:
    import httpx

    base = api.rstrip("/")
    with httpx.Client(trust_env=False, timeout=20.0) as client:
        response = client.get(f"{base}/tasks/{task_id}/evidence")
        response.raise_for_status()
        return response.json()


def _is_elevated() -> bool | None:
    """当前进程有没有提权。拿不到就是 `None`——"不知道"不许写成"没有"。"""
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001 - API 拿不到就不承诺结论
            return None
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    try:
        return bool(geteuid() == 0)
    except Exception:  # noqa: BLE001
        return None


def host_identity() -> dict[str, Any]:
    """报告的机器指纹。主机名不落明文（报告要能贴给别人看），只留哈希前 12 位；
    权限形状相关的字段（平台/架构/系统版本/是否提权）原样带着——跨机对账归因靠它们。"""
    node = platform.node() or ""
    machine = platform.machine() or ""
    os_version = platform.version() or ""
    digest = hashlib.sha256(f"{node}|{sys.platform}|{machine}|{os_version}".encode("utf-8")).hexdigest()
    return {
        "host_id": digest[:12],
        "platform": sys.platform,
        "machine": machine,
        "os_release": platform.release() or "",
        "os_version": os_version,
        "elevated": _is_elevated(),
    }


async def collect(
    *,
    runner: _Runner | None = None,
    task_id: str = "",
    api: str = "",
) -> dict[str, Any]:
    """跑完八条边界，返回可落盘的报告。`runner` 只为测试注入，生产用 execute_command。"""
    if runner is None:
        from blank_api.services.tools.shell import execute_command

        runner = execute_command
    cap = boundary.measure(force=True)
    workspace_tier = resolve_tier(TIER_WORKSPACE, cap=cap)
    seen: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    async def tracked(*call, **kwargs):
        result = await runner(*call, **kwargs)
        if isinstance(result.get("enforcement"), dict):
            seen.append(result["enforcement"])
        return result

    base = Path(tempfile.mkdtemp(prefix="blank-doctor-"))
    workspace = base / "workspace"
    outside = base / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        if not workspace_tier.available or workspace_tier.backend == "docker":
            if workspace_tier.backend == "docker":
                reason = "容器后端：本机 doctor 不越界探测容器内路径"
            else:
                reason = f"workspace 档不可用：{workspace_tier.notice}"
                if workspace_tier.backend == "none" and shutil.which("docker") is None:
                    reason += "；本机没有 Docker，要测 job_only 边界请设置 BLANK_SANDBOX_ALLOW_LOCAL=1 后重跑"
            for probe_id, title in (
                ("P-READ", "读取工作区外文件"),
                ("P-WRITE", "写入工作区外文件"),
                ("P-ENV", "读取 HKCU\\Environment"),
                ("P-NET", "连本机监听端口"),
                ("P-PROC", "活跃进程数上限"),
                ("P-ESCAPE", "沙箱内自放大文件 ACE"),
                ("P-INHERIT", "Job 关闭带走孙进程"),
            ):
                rows.append(row(probe_id, title, NOT_TESTED, reason, reason=reason))
        else:
            status, detail = await _probe_read(tracked, workspace, outside)
            rows.append(row("P-READ", "读取工作区外文件", status, detail,
                            account_key="filesystem_enforced"))
            status, detail = await _probe_write(tracked, workspace, outside)
            rows.append(row("P-WRITE", "写入工作区外文件", status, detail,
                            account_key="filesystem_enforced"))
            status, detail = await _probe_env(tracked, workspace)
            rows.append(row("P-ENV", "读取 HKCU\\Environment", status, detail,
                            account_key="filesystem_enforced"))
            status, detail = await _probe_net(tracked, workspace)
            rows.append(row("P-NET", "连本机监听端口", status, detail,
                            account_key="network_enforced"))
            status, detail = await _probe_pids(tracked, workspace)
            rows.append(row("P-PROC", "活跃进程数上限", status, detail,
                            account_key="pids_enforced"))
            status, detail = await _probe_escape(tracked, workspace, outside)
            rows.append(row("P-ESCAPE", "沙箱内自放大文件 ACE", status, detail,
                            account_key="filesystem_enforced"))
            status, detail = await _probe_inherit(tracked, workspace)
            rows.append(row("P-INHERIT", "Job 关闭带走孙进程", status, detail,
                            account_key="tree_kill_enforced"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # 账本取探针真实返回的最后一条：这些探针走的就是 execute_command，账与命令同源。
    probe_enforcement = dict(seen[-1]) if seen else {}

    for item in rows:
        key = item.get("account_key") or ""
        if key and key in probe_enforcement:
            item["account_value"] = probe_enforcement.get(key)
            item["consistent"] = (
                (item["status"] == ENFORCED) is bool(probe_enforcement.get(key))
                if item["status"] in (ENFORCED, NOT_ENFORCED)
                else None
            )
        elif key:
            item["consistent"] = None

    if task_id:
        try:
            evidence = await asyncio.to_thread(_fetch_evidence, api, task_id)
            status, detail = audit_evidence(evidence)
        except Exception as error:  # noqa: BLE001 - 拉不到证据也是"没有结论"
            status, detail = UNKNOWN, f"证据包取不到：{type(error).__name__}: {error}"
    else:
        status, detail = NOT_TESTED, "传 --task <任务号> 才能核对一条真跑任务的证据包"
    rows.append(row("P-AUDIT", "证据包三处同源", status, detail,
                    reason="" if task_id else "未指定任务号"))

    order = {probe_id: index for index, probe_id in enumerate(BOUNDARY_IDS)}
    rows.sort(key=lambda item: order.get(item["id"], 99))
    summary = {
        "enforced": sum(1 for item in rows if item["status"] == ENFORCED),
        "not_enforced": sum(1 for item in rows if item["status"] == NOT_ENFORCED),
        "verified": sum(1 for item in rows if item["status"] == VERIFIED),
        "unknown": sum(1 for item in rows if item["status"] == UNKNOWN),
        "not_tested": sum(1 for item in rows if item["status"] == NOT_TESTED),
        "inconsistent": sum(1 for item in rows if item.get("consistent") is False),
    }
    report = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": sys.platform,
        "host": host_identity(),
        "python": sys.version.split()[0],
        # P-AUDIT 的结论取决于跑的时候有没有传 --task：跨机对账要把"跑法不同"和
        # "机器不同"分开，不然一份传了、一份没传就会被读成权限差异。
        "audit_task_id": task_id,
        "capability": cap.as_dict(),
        "tiers": {
            TIER_OFF: resolve_tier(TIER_OFF, cap=cap).as_dict(),
            TIER_WORKSPACE: workspace_tier.as_dict(),
            TIER_WORKSPACE_NET: resolve_tier(TIER_WORKSPACE_NET, cap=cap).as_dict(),
        },
        "probe_enforcement": probe_enforcement,
        "boundaries": rows,
        "summary": summary,
    }
    report["exit_code"] = exit_code_for(report)
    return report


def exit_code_for(report: dict[str, Any]) -> int:
    """有不确定或不一致就返回 1；`not_tested` 只是"测不了"，不算失败。"""
    summary = report.get("summary") or {}
    if summary.get("unknown") or summary.get("inconsistent"):
        return 1
    return 0


_STATUS_LABEL = {
    ENFORCED: "拦住",
    NOT_ENFORCED: "未强制",
    VERIFIED: "已核",
    UNKNOWN: "不确定",
    NOT_TESTED: "未测",
}


def render(report: dict[str, Any]) -> str:
    cap = report.get("capability") or {}
    lines = [
        "空白格边界自检（blank doctor --sandbox）",
        f"平台 {report.get('platform')}  Python {report.get('python')}  "
        f"边界强度 {cap.get('strength')}  装置可信 {'是' if cap.get('apparatus_ok') else '否'}",
        f"判断依据：{cap.get('strength_label')}",
        "",
    ]
    for item in report.get("boundaries") or []:
        label = _STATUS_LABEL.get(item["status"], item["status"])
        account = ""
        if item.get("account_key"):
            account = f"  账本 {item['account_key']}={item.get('account_value')}"
        verdict = ""
        if item.get("consistent") is True:
            verdict = "  一致"
        elif item.get("consistent") is False:
            verdict = "  **不一致**"
        lines.append(f"{item['id']:<10} {label:<4} {item['observed']}{account}{verdict}")
    summary = report.get("summary") or {}
    lines.extend(
        [
            "",
            "八条边界：拦住 {enforced} / 未强制 {not_enforced} / 已核 {verified} / 不确定 {unknown} / 未测 {not_tested}".format(
                **summary
            ),
        ]
    )
    if summary.get("inconsistent"):
        lines.append("账本与行为不一致的条数：{}（doctor 退出码 1）".format(summary["inconsistent"]))
    if summary.get("unknown"):
        lines.append(
            "没有确定结论的条数：{}（装置不可信或证据链不成立，doctor 退出码 1）".format(
                summary["unknown"]
            )
        )
    lines.append("“未强制”不是失败，是事实：界面与证据包必须照这个写，不许说成已隔离。")
    return "\n".join(lines)


# --- 跨机对账：M4 退出判据里「输出与本机差异可解释」那半句 ------------------

CONSISTENT = "consistent"
EXPLAINED = "differences_explained"
UNEXPLAINED = "unexplained_difference"
SAME_HOST = "same_host"
INCOMPATIBLE = "incompatible_reports"
UNTRUSTED = "apparatus_untrusted"

_FILE_PROBES = frozenset({"P-READ", "P-WRITE", "P-ENV", "P-ESCAPE"})
_NETWORK_PROBES = frozenset({"P-NET"})
_JOB_PROBES = frozenset({"P-PROC", "P-INHERIT"})
_IDENTITY_PROBES = _FILE_PROBES | _NETWORK_PROBES
_SANDBOXED_PROBES = _IDENTITY_PROBES | _JOB_PROBES

# 「哪条机器维度能解释哪些探针」是**实测关系**，不是"机器不同所以什么都可能"：
# 设计稿 §2.0 量过——非管理员拿不到 AppContainer 与受限令牌，身份层差异只可能改变文件与
# 网络的结论；Job 是另一套原语，它管进程数与生命周期。P-AUDIT 从来不由机器决定。
_ATTRIBUTION: tuple[tuple[str, tuple[str, ...], frozenset[str], str], ...] = (
    ("boundary_strength", ("capability", "strength"), _IDENTITY_PROBES, "边界强度"),
    ("appcontainer", ("capability", "appcontainer"), _IDENTITY_PROBES, "AppContainer 可进"),
    ("restricted_token", ("capability", "restricted_token"), _IDENTITY_PROBES, "受限令牌可建"),
    ("elevated", ("host", "elevated"), _IDENTITY_PROBES, "进程提权"),
    ("job", ("capability", "job"), _JOB_PROBES, "Job Object 可用"),
    ("fs_enforced", ("probe_enforcement", "filesystem_enforced"), _FILE_PROBES, "账本：文件边界被强制"),
    ("net_enforced", ("probe_enforcement", "network_enforced"), _NETWORK_PROBES, "账本：网络被强制"),
    ("pids_enforced", ("probe_enforcement", "pids_enforced"), _JOB_PROBES, "账本：进程数被强制"),
    ("backend", ("tiers", TIER_WORKSPACE, "backend"), _SANDBOXED_PROBES, "沙箱后端"),
    ("tier_available", ("tiers", TIER_WORKSPACE, "available"), _SANDBOXED_PROBES, "workspace 档可用"),
)

# 这些字段不同**不解释任何探针**：我们没实测"换个 Python/系统版本结论就会变"，
# 所以宁可让差异归因不出、逼一次实测，也不把它写成理由。只列出来供人看。
_CONTEXT_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("python",)),
    ("machine", ("host", "machine")),
    ("os_release", ("host", "os_release")),
    ("os_version", ("host", "os_version")),
)

_MISSING = object()


def _dig(report: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = report
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _statuses(report: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("id") or ""): str(item.get("status") or "")
        for item in (report.get("boundaries") or [])
        if isinstance(item, dict)
    }


def compare_reports(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """两份报告对账。三条先钉死，否则这个装置会被拿去粉饰：

    1. 没有机器指纹的报告（schema 1 或手写的）拒绝对账，不按档位补一份假指纹；
    2. `host_id` 相同就是**同一台机器跑了两次**，M4 要的「第二台机器」没有满足——返回
       `same_host`、退出码 1，本机重跑多少次都不能冒充跨机验证；
    3. 任一侧装置不可信（`apparatus_ok` 不是 True）时那份报告本身没有结论资格，对账不作数。

    剩下的差异逐条找归因：能在上面那张实测表里找到机器维度才算"可解释"，找不到就红。
    """
    base: dict[str, Any] = {
        "verdict": "",
        "note": "",
        "exit_code": 1,
        "same_host": False,
        # 逐条比较有没有真发生过：不兼容/不可信时是提前返回的，这时候渲染层不许
        # 因为"差异列表是空的"就说「八条结论逐条相同」——那等于没比却报平安。
        "compared": False,
        "left_host": left.get("host") if isinstance(left.get("host"), dict) else None,
        "right_host": right.get("host") if isinstance(right.get("host"), dict) else None,
        "dimensions": [],
        "differences": [],
        "context": [],
    }

    def fail(verdict: str, note: str) -> dict[str, Any]:
        base["verdict"] = verdict
        base["note"] = note
        return base

    for label, report in (("左", left), ("右", right)):
        if not isinstance(report.get("host"), dict) or "audit_task_id" not in report:
            return fail(
                INCOMPATIBLE,
                f"{label}侧报告没有机器指纹（schema={report.get('schema')}），"
                "跨机对账要求 schema 2 起的报告；不按档位补一份假指纹",
            )
    if left.get("schema") != right.get("schema"):
        return fail(
            INCOMPATIBLE,
            f"报告版本不同（左 {left.get('schema')} / 右 {right.get('schema')}），探针语义不可比",
        )
    if _dig(left, ("host", "platform")) != _dig(right, ("host", "platform")):
        return fail(
            INCOMPATIBLE,
            "平台不同：这八条探针是 Windows 语义（Job/AC/HKCU/icacls），跨平台比不出结论",
        )
    for label, report in (("左", left), ("右", right)):
        if _dig(report, ("capability", "apparatus_ok")) is not True:
            return fail(
                UNTRUSTED,
                f"{label}侧探测装置不可信（capability.apparatus_ok 不是 True），那份报告没有结论资格",
            )

    base["compared"] = True
    dimensions = []
    causes_by_probe: dict[str, list[str]] = {}
    for name, path, probes, label in _ATTRIBUTION:
        a, b = _dig(left, path), _dig(right, path)
        if a is _MISSING or b is _MISSING or a == b:
            continue
        dimensions.append({"name": name, "label": label, "left": a, "right": b,
                           "covers": sorted(probes)})
        for probe in probes:
            causes_by_probe.setdefault(probe, []).append(name)
    for name, path in _CONTEXT_FIELDS:
        a, b = _dig(left, path), _dig(right, path)
        if a is _MISSING or b is _MISSING or a == b:
            continue
        base["context"].append({"name": name, "label": f"{name}（不解释任何探针）",
                                "left": a, "right": b})

    left_status, right_status = _statuses(left), _statuses(right)
    run_flags_differ = bool(left.get("audit_task_id")) != bool(right.get("audit_task_id"))
    for probe_id in BOUNDARY_IDS:
        a = left_status.get(probe_id, "absent")
        b = right_status.get(probe_id, "absent")
        if a == b:
            continue
        causes = list(causes_by_probe.get(probe_id, []))
        if probe_id == "P-AUDIT" and run_flags_differ:
            # 一份传了 --task、一份没传：这是跑法不同，不是机器权限不同。
            causes.append("run_flags")
        base["differences"].append({
            "probe": probe_id,
            "left": a,
            "right": b,
            "causes": causes,
            "explained": bool(causes),
        })

    base["same_host"] = bool(
        _dig(left, ("host", "host_id")) == _dig(right, ("host", "host_id"))
    )
    unexplained = [item for item in base["differences"] if not item["explained"]]
    if base["same_host"]:
        # 同一台机器的两份报告：既不能算跨机验证，也可能暴露探针不确定——都别写成"通过"。
        base["verdict"] = SAME_HOST
        base["note"] = (
            "两份报告的机器指纹相同：这是同一台机器跑了两次，M4 的「第二台机器」没有满足。"
            + (f"另有 {len(unexplained)} 条差异归因不出。" if unexplained else "")
        )
        return base
    if unexplained:
        base["verdict"] = UNEXPLAINED
        base["note"] = (
            f"{len(unexplained)} 条差异找不到实测归因："
            + "、".join(f"{item['probe']}（{item['left']}→{item['right']}）" for item in unexplained)
            + "。要么补一次实测把关系钉进归因表，要么这就是不可解释的漂移——都不许写成通过。"
        )
        return base
    if base["differences"]:
        base["verdict"] = EXPLAINED
        base["note"] = f"{len(base['differences'])} 条差异逐条归因到机器维度，退出码 0。"
        base["exit_code"] = 0
        return base
    base["verdict"] = CONSISTENT
    base["note"] = "两台机器的八条结论一致。"
    base["exit_code"] = 0
    return base


def render_compare(result: dict[str, Any]) -> str:
    labels = {"left": (result.get("left_host") or {}).get("host_id") or "?",
              "right": (result.get("right_host") or {}).get("host_id") or "?"}
    lines = [
        f"跨机对账：左 {labels['left']}  右 {labels['right']}",
        f"结论：{result.get('verdict')}",
        f"说明：{result.get('note')}",
        "",
    ]
    if result.get("dimensions"):
        lines.append("参与归因的机器维度：")
        for item in result["dimensions"]:
            lines.append(f"  {item['label']}: {item['left']} → {item['right']}"
                         f"（可解释 {','.join(item['covers'])}）")
        lines.append("")
    if result.get("context"):
        lines.append("只列出、不解释任何探针的字段差异：")
        for item in result["context"]:
            lines.append(f"  {item['name']}: {item['left']} → {item['right']}")
        lines.append("")
    if result.get("differences"):
        lines.append("探针差异：")
        for item in result["differences"]:
            cause = "、".join(item["causes"]) if item["causes"] else "**归因不出**"
            lines.append(f"  {item['probe']:<10} {item['left']} → {item['right']}  归因 {cause}")
    elif result.get("compared"):
        lines.append("八条探针结论逐条相同。")
    else:
        lines.append("八条探针**没有逐条比较过**（原因见上面的说明），这里不报平安。")
    lines.append("")
    lines.append("归因表只有实测过的关系：换 Python 版本、换系统小版本都不算理由。")
    return "\n".join(lines)


def compare_files(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = json.loads(Path(left_path).read_text(encoding="utf-8"))
    right = json.loads(Path(right_path).read_text(encoding="utf-8"))
    return compare_reports(left, right)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blank doctor", description="空白格本机自检")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="逐条攻击八条执行边界，并和这次执行的账本比对",
    )
    parser.add_argument("--json", type=Path, default=None, help="把报告写到这个文件")
    parser.add_argument(
        "--compare",
        type=Path,
        nargs=2,
        default=None,
        metavar=("LEFT.json", "RIGHT.json"),
        help="对账两份 --json 报告；机器指纹相同即判同机，差异归因不出即判红",
    )
    parser.add_argument("--task", default="", help="核对这条任务的证据包（P-AUDIT）")
    parser.add_argument(
        "--api",
        default=os.environ.get("BLANK_API_BASE") or "http://127.0.0.1:8000/api/v1",
        help="核对证据包时访问的 API 地址",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "doctor":
        args = args[1:]
    parsed = _parser().parse_args(args)
    if parsed.compare:
        result = compare_files(parsed.compare[0], parsed.compare[1])
        print(render_compare(result))
        return int(result.get("exit_code") or 0)
    if not parsed.sandbox:
        _parser().print_help()
        return 2
    report = asyncio.run(collect(task_id=parsed.task.strip(), api=parsed.api))
    print(render(report))
    if parsed.json:
        parsed.json.parent.mkdir(parents=True, exist_ok=True)
        parsed.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n报告已写入 {parsed.json}")
    return int(report.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
