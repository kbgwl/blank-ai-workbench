"""0.5.0 安全与权限：客户决定 Agent 在这台机器上「能做什么」。

分两层断言：

* **能力边界** —— 关掉就是关掉，审批也放不过去；要放行只能改设置。
* **审批节奏** —— 只决定「要不要先问」，不改边界。

这两件事混在一起时，用户最容易踩的坑是「点了允许还是失败」，所以这里把每一条
硬规则单独钉住，任何一条退回成"可审批"都会立刻红。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.permissions import PermissionPolicy
from blank_api.services.safety import SafetyPolicy
from blank_api.services.settings import normalize
from blank_api.services.tools.base import ToolDefinition
from blank_api.services.tools.gateway import ToolGateway


def policy(**overrides) -> SafetyPolicy:
    """绝大多数用例都在「命令已开、确认已关」的基线上只改一项。"""
    base = dict(
        shell_enabled=True,
        network_enabled=False,
        confirm_all_commands=False,
        restrict_to_workspace=True,
        timeout_seconds=120.0,
    )
    base.update(overrides)
    return SafetyPolicy(**base)


# ── 1. 能力开关：默认关，关着就是硬拒绝 ────────────────────────────


def test_shell_is_off_until_the_customer_turns_it_on() -> None:
    default = SafetyPolicy()
    assert default.shell_enabled is False
    verdict = default.gate("run_shell", {"command": "ls"}, Path.cwd())
    assert verdict.denied
    assert verdict.rule == "capability:shell_disabled"
    # 理由必须告诉用户下一步去哪，否则只会在界面里反复重试
    assert "安全与权限" in verdict.reason


def test_non_command_tools_are_not_part_of_this_gate() -> None:
    # 文件读写走工作区路径约束，不该被"禁止执行命令"连带拦掉
    assert SafetyPolicy().gate("apply_patch", {}, Path.cwd()).allowed


@pytest.mark.parametrize(
    "command",
    [
        "format c: /y",
        "mkfs.ext4 /dev/sda",
        "shutdown /s /t 0",
        "reg delete HKLM\\Software\\Evil /f",
        "diskpart /s script.txt",
    ],
)
def test_host_destroying_commands_are_never_allowed(command: str) -> None:
    # 这一组连"要不要问"都不谈：问了也没意义，人不会希望 Agent 能格式化磁盘
    verdict = policy(confirm_all_commands=True).gate(
        "run_shell", {"command": command}, Path.cwd(), mode="bypass"
    )
    assert verdict.denied, command
    assert verdict.rule == "builtin:destructive"


# ── 2. 网络：默认关，开了才放 ─────────────────────────────────────


def test_network_command_is_blocked_until_network_is_enabled() -> None:
    blocked = policy().gate("run_shell", {"command": "curl https://example.test/a.sh"}, Path.cwd())
    assert blocked.denied
    assert blocked.rule == "capability:network_disabled"

    allowed = policy(network_enabled=True).gate(
        "run_shell", {"command": "curl https://example.test/a.sh"}, Path.cwd()
    )
    assert allowed.allowed


@pytest.mark.parametrize(
    "command",
    ["git clone https://x.test/r.git", "pip install requests", "npm install", "wget http://a.test/b"],
)
def test_package_and_git_transports_count_as_network(command: str) -> None:
    assert policy().gate("run_shell", {"command": command}, Path.cwd()).denied


def test_user_denied_prefix_wins_over_enabled_capabilities() -> None:
    strict = policy(network_enabled=True, denied_commands=["curl"])
    verdict = strict.gate("run_shell", {"command": "curl https://example.test"}, Path.cwd())
    assert verdict.denied
    assert verdict.rule == "user:denied_prefix"


# ── 3. 路径：工作区是边界，允许列表是唯一的例外 ────────────────────


def test_path_outside_workspace_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "notes.txt"
    target.write_text("secret", encoding="utf-8")

    verdict = policy().gate("run_shell", {"command": f"type {target}"}, workspace)
    assert verdict.denied
    assert verdict.rule == "capability:path_escape"


def test_allowed_root_opens_exactly_that_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "data.csv").write_text("a,b", encoding="utf-8")

    opened = policy(allowed_roots=(str(shared),)).gate(
        "run_shell", {"command": f"type {shared / 'data.csv'}"}, workspace
    )
    assert opened.allowed


def test_parent_relative_escape_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")

    verdict = policy().gate("run_shell", {"command": "type ../secret.txt"}, workspace)
    assert verdict.denied
    assert verdict.rule == "capability:path_escape"


def test_relative_path_inside_workspace_is_fine(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print(1)", encoding="utf-8")
    assert policy().gate("run_shell", {"command": "type app.py"}, tmp_path).allowed


def test_nonexistent_absolute_path_is_not_treated_as_escape(tmp_path: Path) -> None:
    # echo 里写字面量很常见，把不存在的路径判成越界会让人直接关掉整条策略
    assert policy().gate(
        "run_shell", {"command": 'echo "C:\\no\\such\\place"'},
        tmp_path,
    ).allowed


# ── 4. 审批节奏：抬高风险，复用同一套记忆与白名单 ──────────────────


def test_confirm_all_turns_every_command_into_an_ask() -> None:
    strict = policy(confirm_all_commands=True)
    verdict = strict.gate("run_shell", {"command": "ls"}, Path.cwd())
    assert verdict.asks
    assert verdict.rule == "policy:confirm_all"


def test_bypass_mode_is_the_customers_explicit_choice() -> None:
    strict = policy(confirm_all_commands=True)
    assert strict.gate("run_shell", {"command": "ls"}, Path.cwd(), mode="bypass").allowed


def test_plan_mode_falls_through_to_the_permission_layer() -> None:
    # 计划模式下命令会由 permissions 统一拒绝；这里抢先问一遍会出现两条矛盾的理由
    strict = policy(confirm_all_commands=True)
    assert strict.gate("run_shell", {"command": "ls"}, Path.cwd(), mode="plan").allowed


# ── 5. 网关：先判能不能做，再判要不要问 ────────────────────────────


async def _never(context, arguments):  # pragma: no cover - 被调用即失败
    raise AssertionError("被策略拒绝的工具不应该进入 handler")


SHELL_TOOL = ToolDefinition(
    name="run_shell",
    description="",
    input_schema={
        "type": "object",
        "required": ["command"],
        "properties": {"command": {"type": "string"}},
    },
    risk_level="medium",
    handler=_never,
)


def _context(workspace: Path, safety: SafetyPolicy, mode: str = "default") -> SimpleNamespace:
    return SimpleNamespace(
        root=workspace,
        permissions=PermissionPolicy.build(mode),
        safety=safety,
    )


def test_gateway_returns_deny_before_generating_an_approval(tmp_path: Path) -> None:
    gateway = ToolGateway([SHELL_TOOL], defer=False)
    decision = gateway.decide(
        SHELL_TOOL, {"command": "ls"}, _context(tmp_path, SafetyPolicy())
    )
    # 必须是 deny 而不是 ask：否则用户点了允许还是失败
    assert decision.denied
    assert decision.rule == "capability:shell_disabled"


def test_gateway_escalates_risk_when_confirmation_is_required(tmp_path: Path) -> None:
    gateway = ToolGateway([SHELL_TOOL], defer=False)
    decision = gateway.decide(
        SHELL_TOOL,
        {"command": "ls"},
        _context(tmp_path, policy(confirm_all_commands=True)),
    )
    assert decision.asks
    # medium 被抬到 high，审批与"总是允许这个前缀"的记忆都复用既有逻辑
    assert decision.risk == "high"


def test_gateway_lets_approved_prefix_through_on_second_pass(tmp_path: Path) -> None:
    gateway = ToolGateway([SHELL_TOOL], defer=False)
    context = _context(tmp_path, policy(confirm_all_commands=True))
    first = gateway.decide(SHELL_TOOL, {"command": "ls -la"}, context)
    assert first.asks

    context.permissions.remember("run_shell", {"command": "ls -la"}, scope="prefix")
    second = gateway.decide(SHELL_TOOL, {"command": "ls -la"}, context)
    assert second.allowed


# ── 6. 设置持久化：少写一个字段不能变成放行 ────────────────────────


def test_missing_safety_section_keeps_the_capabilities_off() -> None:
    safety = normalize({})["safety"]
    assert safety["shell_enabled"] is False
    assert safety["network_enabled"] is False
    assert safety["confirm_all_commands"] is True
    assert safety["restrict_to_workspace"] is True


def test_safety_values_are_clamped_and_deduped() -> None:
    safety = normalize(
        {
            "safety": {
                "shell_enabled": "yes",
                "timeout_seconds": 99999,
                "allowed_roots": ["E:/a", "E:/a", "  ", "E:/b"],
                "denied_commands": "git push, git push",
            }
        }
    )["safety"]
    assert safety["shell_enabled"] is True
    assert safety["timeout_seconds"] == 600.0
    assert safety["allowed_roots"] == ["E:/a", "E:/b"]
    assert safety["denied_commands"] == ["git push"]


def test_partial_update_does_not_reset_other_safety_fields(tmp_path: Path) -> None:
    from blank_api.services.settings import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"safety": {"shell_enabled": True, "timeout_seconds": 60}})
    store.update({"safety": {"network_enabled": True}})
    safety = store.read()["safety"]
    assert safety["shell_enabled"] is True
    assert safety["network_enabled"] is True
    assert safety["timeout_seconds"] == 60.0


def test_policy_reads_only_what_settings_say(tmp_path: Path) -> None:
    from blank_api.services.settings import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"safety": {"shell_enabled": True, "denied_commands": ["rm"]}})
    loaded = SafetyPolicy.from_settings(store.read())
    assert loaded.shell_enabled is True
    assert loaded.denied_commands == ("rm",)
    assert loaded.gate("run_shell", {"command": "rm -rf build"}, tmp_path).rule == "user:denied_prefix"
