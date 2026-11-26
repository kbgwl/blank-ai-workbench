"""会话级审批授权：一次当面批准，同类动作本会话不再逐条问。

真机现象：用户选了「完全访问」，装依赖与删除仍然一遍遍弹卡；跨轮之后
（每一轮都是新任务）连“本任务批过一次”的记忆也丢了。本文件的判据把三件事
钉住：

1. **bypass 本身不产生会话授权**：高风险动作第一次仍要人点；
2. **授权按动作类别隔离**：批准 `git push` 不会连带放行 `git push --force`，
   批准工作区内删除不会放行 `rm -rf /`；
3. **落盘按会话（thread）隔离**：同会话下一轮认得，换会话、过期、文件损坏
   一律重新问（fail-closed）。

另外钉住一个顺序缺陷：用户点过“不允许”的精确调用，下一轮必须直接拒绝，
不能因为排在不可逆闸门后面而再次弹卡。
"""

import json

import pytest
from pydantic import ValidationError

from blank_api.schemas.task import TaskApprovalIn
from blank_api.services import session_grants
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    SCOPE_SESSION,
    PermissionPolicy,
    approval_options,
    parse_decision,
    session_grant_category,
)
from blank_api.services.permission_rules import parse_settings_rules
from blank_api.services.tools.base import ToolDefinition
from blank_api.services.tools.shell import assess_risk


def _shell_tool() -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name="run_shell",
        description="d",
        input_schema={"type": "object", "required": ["command"]},
        risk_level="medium",
        risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
        handler=handler,
    )


def _policy(mode: str = "bypass") -> PermissionPolicy:
    return PermissionPolicy.build(mode, sandbox_tier="workspace")


def _decide(policy: PermissionPolicy, command: str, **extra):
    return policy.decide(_shell_tool(), {"command": command, **extra})


@pytest.fixture
def grants_file(tmp_path, monkeypatch):
    target = tmp_path / "session-grants.json"
    monkeypatch.setenv("BLANK_SESSION_GRANTS_FILE", str(target))
    return target


# --- 判定：bypass 不等于会话授权 ------------------------------------------- #


def test_bypass_alone_does_not_auto_approve_high_risk():
    decision = _decide(_policy(), "git push origin main")
    assert decision.outcome == ASK
    assert decision.rule == "v165:high_risk"


def test_session_grant_is_an_explicit_human_basis():
    policy = _policy()
    assert policy.remember_session_grant("git_push") is True
    decision = _decide(policy, "git push origin main")
    assert decision.outcome == ALLOW
    assert decision.rule == "session_grant:git_push"


def test_session_grant_does_not_cover_a_dangerous_variant():
    policy = _policy()
    policy.remember_session_grant("git_push")
    forced = _decide(policy, "git push --force origin main")
    assert forced.outcome == ASK
    assert forced.rule == "irreversible"


def test_session_grant_delete_does_not_cover_root_delete():
    policy = _policy()
    policy.remember_session_grant("delete_file")
    assert _decide(policy, "rm -rf build").outcome == ALLOW
    assert _decide(policy, "rm -rf /").outcome == ASK


def test_session_grant_migration_does_not_cover_drop_table():
    policy = _policy()
    policy.remember_session_grant("db_migration")
    assert _decide(policy, "python manage.py migrate").outcome == ALLOW
    assert _decide(policy, "drop table users").outcome == ASK


def test_session_grant_covers_piped_install():
    """带管道的安装命令记不住前缀，会话级类别必须能覆盖它。"""
    policy = _policy()
    policy.remember_session_grant("install_dependency")
    decision = _decide(policy, "pip install fastapi | tee install.log")
    assert decision.outcome == ALLOW
    assert decision.rule == "session_grant:install_dependency"


def test_ask_rule_beats_session_grant():
    """用户手写的 ask 规则就是“每次都让我看一眼”，会话授权不能顶掉它。"""
    policy = _policy()
    policy.remember_session_grant("git_push")
    policy.ruleset = parse_settings_rules({"ask": ["Bash(git push:*)"]})
    decision = _decide(policy, "git push origin main")
    assert decision.outcome == ASK
    assert decision.rule.startswith("rule:ask:")


def test_plan_mode_never_uses_session_grant():
    policy = PermissionPolicy.build("plan", sandbox_tier="workspace")
    policy.remember_session_grant("delete_file")
    decision = _decide(policy, "rm -rf build")
    assert decision.outcome == DENY
    assert decision.rule == "mode:plan"


def test_denied_exact_call_is_not_asked_again():
    """驳回是结论，不是“下一次再问一遍”。旧顺序让不可逆动作绕过这条账。"""
    policy = _policy()
    tool = _shell_tool()
    arguments = {"command": "rm -rf build"}
    policy.forget("run_shell", arguments, "once")
    decision = policy.decide(tool, dict(arguments))
    assert decision.outcome == DENY
    assert decision.rule == "action:denied"


def test_unknown_session_category_is_never_remembered():
    policy = _policy()
    assert policy.remember_session_grant("shutdown") is False
    assert policy.session_grants == set()


# --- 类别映射 --------------------------------------------------------------- #


def test_category_mapping_keeps_dangerous_variants_apart():
    assert session_grant_category("run_shell", {"command": "git push origin main"}) == "git_push"
    assert (
        session_grant_category("run_shell", {"command": "git push --force origin main"})
        == "git_push_force"
    )
    assert session_grant_category("run_shell", {"command": "rm -rf build"}) == "delete_file"
    assert session_grant_category("run_shell", {"command": "rm -rf /"}) == "delete_irreversible"
    assert session_grant_category("run_shell", {"command": "drop table users"}) == "db_destructive"
    assert (
        session_grant_category("run_shell", {"command": "python manage.py migrate"})
        == "db_migration"
    )
    assert session_grant_category("run_shell", {"command": "deploy --prod"}) == "deploy"
    assert (
        session_grant_category("run_shell", {"command": "pip install fastapi | tee log"})
        == "install_dependency"
    )
    assert (
        session_grant_category("run_shell", {"command": "git push origin main | tee push.log"})
        == "git_push"
    )
    assert (
        session_grant_category("run_shell", {"command": "rm -rf build && echo done"})
        == "delete_file"
    )
    # 混合类别不给会话档：一段批准不能顺手覆盖另一段。
    assert (
        session_grant_category("run_shell", {"command": "pip install fastapi && rm -rf build"})
        == ""
    )
    assert (
        session_grant_category("run_shell", {"command": "rm -rf build | curl https://x"})
        == ""
    )
    # 正文里出现关键词不算类别：只认命令位置。
    assert session_grant_category("run_shell", {"command": 'git commit -m "migrate"'}) == ""
    assert session_grant_category("run_shell", {"command": "echo deploy"}) == ""
    # 越界执行不提供会话档：沙箱边界只能一条一条批，不能"本会话都出界"。
    assert session_grant_category("run_shell", {"command": "ls", "sandbox": "host"}) == ""
    # 越界叠加高危/不可逆命令时不给会话档：两件事的授权叠加太宽。
    assert session_grant_category("run_shell", {"command": "rm -rf /", "sandbox": "host"}) == ""
    assert (
        session_grant_category(
            "run_shell", {"command": "git push origin main", "sandbox": "host"}
        )
        == ""
    )
    # 通用不可逆命令不提供会话档，仍然逐条确认。
    assert session_grant_category("run_shell", {"command": "shutdown -h now"}) == ""


# --- 审批卡档位 ------------------------------------------------------------- #


def test_approval_options_offer_session_scope_for_known_category():
    options = approval_options("run_shell", "", tool_scope=False, category="delete_file")
    scopes = [item["scope"] for item in options]
    assert scopes == ["once", SCOPE_SESSION]
    assert "删除文件" in options[1]["label"]
    assert "本会话" in options[1]["warning"]


def test_approval_options_hide_session_scope_once_granted():
    options = approval_options(
        "run_shell", "", tool_scope=False, category="delete_file", session_granted=True
    )
    assert SCOPE_SESSION not in [item["scope"] for item in options]


def test_unknown_category_gets_no_session_option():
    options = approval_options("run_shell", "", tool_scope=False, category="shutdown")
    assert SCOPE_SESSION not in [item["scope"] for item in options]


# --- 落盘：按会话隔离、过期与损坏 fail-closed --------------------------------- #


def test_store_persists_per_thread(grants_file):
    assert session_grants.grant("thread-a", "git_push", source_task="t1") is True
    assert session_grants.load_categories("thread-a") == {"git_push"}
    assert session_grants.load_categories("thread-b") == set()

    policy = _policy()
    policy.session_grants = session_grants.load_categories("thread-a")
    assert _decide(policy, "git push origin main").outcome == ALLOW


def test_store_rejects_unknown_category(grants_file):
    assert session_grants.grant("thread-a", "shutdown") is False
    assert session_grants.load_categories("thread-a") == set()


def test_store_ignores_expired_grants(grants_file):
    assert session_grants.grant("thread-a", "git_push")
    data = json.loads(grants_file.read_text(encoding="utf-8"))
    data["grants"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    grants_file.write_text(json.dumps(data), encoding="utf-8")
    assert session_grants.load_categories("thread-a") == set()


def test_store_corrupt_file_fails_closed(grants_file):
    grants_file.write_text("{not json", encoding="utf-8")
    assert session_grants.load_categories("thread-a") == set()


def test_store_revoke_and_purge(grants_file):
    assert session_grants.grant("thread-a", "git_push")
    assert session_grants.grant("thread-a", "delete_file")
    assert session_grants.revoke("thread-a", "git_push") is True
    assert session_grants.load_categories("thread-a") == {"delete_file"}
    assert session_grants.purge_thread("thread-a") is True
    assert session_grants.load_categories("thread-a") == set()


# --- 接口入参 --------------------------------------------------------------- #


def test_parse_decision_and_schema_accept_session():
    assert parse_decision({"approve": True, "scope": "session"}) == (True, SCOPE_SESSION)
    assert TaskApprovalIn(approve=True, scope="session").scope == "session"
    with pytest.raises(ValidationError):
        TaskApprovalIn(approve=True, scope="forever")
