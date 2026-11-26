"""完全访问下的跨盘：只读自动盘点，写/删必须逐次确认。

用户真机场景：工作区在 E 盘，要清理 C 盘。旧行为在安全层直接拒绝 C 路径，
连只读盘点都做不了；新口径只放宽到：
- 完全访问 + 只读命令 + 工作区外路径 → 自动放行；
- 完全访问 + 写/删 + 工作区外路径 → 审批卡（模式放行不能代替这一次确认）；
- 默认/计划/接受编辑等模式 → 仍按原边界拒绝。
"""

from __future__ import annotations

from types import SimpleNamespace

from blank_api.services.authorization import authorize
from blank_api.services.permissions import PermissionPolicy, is_read_only_command
from blank_api.services.safety import SafetyPolicy
from blank_api.services.tools.shell import slow_scan_hint


class _Tool:
    name = "run_shell"

    def resolve_risk(self, arguments: dict) -> str:
        return "high"


def _policy() -> SafetyPolicy:
    return SafetyPolicy(
        shell_enabled=True,
        network_enabled=False,
        confirm_all_commands=False,
        restrict_to_workspace=True,
        timeout_seconds=120.0,
    )


def _paths(tmp_path):
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    source = outside / "src.txt"
    source.write_text("x", encoding="utf-8")
    return workspace, outside, source


def test_read_only_command_can_allow_absolute_paths_for_cross_drive_scan(tmp_path) -> None:
    _, outside, _ = _paths(tmp_path)
    command = f'dir "{outside}"'
    assert is_read_only_command(command) is False
    assert is_read_only_command(command, allow_absolute_paths=True) is True
    scan = f'powershell -Command "Get-ChildItem \\"{outside}\\" -Recurse -File | Measure-Object -Property Length -Sum"'
    assert is_read_only_command(scan, allow_absolute_paths=True) is True
    delete = f'powershell -Command "Remove-Item \\"{outside}\\" -Recurse -Force"'
    assert is_read_only_command(delete, allow_absolute_paths=True) is False


def test_slow_scan_hint_stops_broad_c_drive_recursion() -> None:
    assert slow_scan_hint('dir /s /a C:\\Users 2>&1 | findstr /c:"File(s)"')
    assert slow_scan_hint('Get-ChildItem "C:\\Users" -Recurse -File')
    assert slow_scan_hint('Get-ChildItem "C:\\" -Recurse -File')
    # 系统盘根变量同样不能递归扫：T09 真机用 dir /s /b "%SystemDrive%" 扫了 30 秒。
    assert slow_scan_hint('dir /s /b "%SystemDrive%" 2>nul | findstr /i /c:".md"')
    assert slow_scan_hint('dir /s "%SystemRoot%"')
    assert slow_scan_hint('Get-ChildItem "$env:SystemDrive" -Recurse -File')
    # 窄目录仍允许，别把正常的按子目录统计也挡掉。
    assert slow_scan_hint('Get-ChildItem "C:\\Users\\22103\\AppData\\Local\\Temp" -Recurse -File') == ""
    assert slow_scan_hint('dir /s "C:\\Windows\\Temp"') == ""


def test_bypass_allows_read_only_cross_drive_but_not_write(tmp_path) -> None:
    workspace, outside, source = _paths(tmp_path)
    safety = _policy()

    read = safety.gate("run_shell", {"command": f'dir "{outside}"'}, workspace, mode="bypass")
    assert read.allowed and read.rule == "scope:outside_read"

    write = safety.gate(
        "run_shell",
        {"command": f'copy "{source}" "{outside / "dst.txt"}"'},
        workspace,
        mode="bypass",
    )
    assert write.asks and write.rule == "scope:outside_write"


def test_default_mode_still_denies_cross_drive_reads(tmp_path) -> None:
    workspace, outside, _ = _paths(tmp_path)
    verdict = _policy().gate(
        "run_shell", {"command": f'dir "{outside}"'}, workspace, mode="default"
    )
    assert verdict.denied and verdict.rule == "capability:path_escape"


def test_authorize_forces_one_confirmation_for_cross_drive_write(tmp_path) -> None:
    workspace, outside, source = _paths(tmp_path)
    permissions = PermissionPolicy(mode="bypass")
    context = SimpleNamespace(
        root=str(workspace), safety=_policy(), permissions=permissions
    )
    args = {"command": f'copy "{source}" "{outside / "dst.txt"}"'}

    first = authorize(_Tool(), args, context)
    assert first.asks and first.rule == "scope:outside_write"

    # 用户批准这一次之后，同一动作才允许执行。
    permissions.remember("run_shell", args, scope="once")
    second = authorize(_Tool(), args, context)
    assert second.allowed


def test_approved_outside_delete_is_not_blocked_by_irreversible_gate(tmp_path) -> None:
    workspace, outside, source = _paths(tmp_path)
    permissions = PermissionPolicy(mode="bypass")
    context = SimpleNamespace(
        root=str(workspace), safety=_policy(), permissions=permissions
    )
    args = {"command": f'del /f /q "{source}"'}

    first = authorize(_Tool(), args, context)
    assert first.asks and first.rule == "scope:outside_write"

    permissions.remember("run_shell", args, scope="once")
    second = authorize(_Tool(), args, context)
    assert second.allowed, second
