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
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
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

SCHEMA_VERSION = 1

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
        "python": sys.version.split()[0],
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blank doctor", description="空白格本机自检")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="逐条攻击八条执行边界，并和这次执行的账本比对",
    )
    parser.add_argument("--json", type=Path, default=None, help="把报告写到这个文件")
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
