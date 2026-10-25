"""`permission_profile`：把「这一步能碰哪些路径」写成逐条 ACE，且每条都说清谁在拦。

对标 Codex `turn_context.permission_profile` 的形状；差别是我们多答一个问题——
本机常常拿不到内核级文件边界（M0 实测 `job_only`），所以「策略闸门会拦」不能写成
「系统会拒」。这里每一条断言都瞄着这条差别，以及 `missing_path_behavior` 的**真实**
语义（闸门对不存在的目标跳过，不是 fail）。
"""

from __future__ import annotations

import types

from blank_api.services import safety as safety_mod
from blank_api.services.agent.evidence import boundary_report
from blank_api.services.agent.governance import digest, permission_profile, turn_context
from blank_api.services.safety import SafetyPolicy

JOB_ONLY = {
    "backend": "local",
    "enabled": True,
    "isolation": "host",
    "filesystem_enforced": False,
    "network_enforced": False,
    "network": False,
    "tier": "workspace",
}
DOCKER = {
    "backend": "docker",
    "enabled": True,
    "isolation": "container",
    "filesystem_enforced": True,
    "network_enforced": True,
    "network": False,
    "tier": "workspace",
}
OFF = {
    "backend": "none",
    "enabled": False,
    "isolation": "host",
    "filesystem_enforced": False,
    "network_enforced": False,
    "network": False,
    "tier": "off",
}


def _policy(**overrides) -> SafetyPolicy:
    base = dict(shell_enabled=True, restrict_to_workspace=True, network_enabled=False,
                confirm_all_commands=False)
    base.update(overrides)
    return SafetyPolicy(**base)


def _entries(profile: dict) -> list[dict]:
    return list(profile["file_system"]["entries"])


def test_job_only_attributes_the_gate_to_policy_not_to_the_kernel():
    profile = permission_profile(
        sandbox=JOB_ONLY,
        safety=_policy(allowed_roots=("D:\\datasets",)),
        workspace="<workspace:kbgai>",
    )
    assert profile["type"] == "managed"
    assert profile["enforced_by"] == "policy"
    entries = _entries(profile)
    assert entries[0] == {"path": "<workspace:kbgai>", "access": "write", "enforced_by": "policy"}
    # 允许根也是逐条写出来，不是一句"另有白名单"
    assert entries[1]["access"] == "write" and entries[1]["enforced_by"] == "policy"
    assert entries[1]["path"] == "<workspace:datasets>"
    assert entries[-1] == {"path": "*", "access": "deny", "enforced_by": "policy"}
    # 档位要的是不通网；真正拦着的是命令闸门的正则，不是内核——这两件事必须分开写。
    assert profile["network"] == {"requested": "none", "enforced_by": "policy", "command_gate": "deny"}


def test_filesystem_enforced_flips_the_same_entries_to_os():
    profile = permission_profile(sandbox={**JOB_ONLY, "filesystem_enforced": True},
                                 safety=_policy(), workspace="<workspace:kbgai>")
    assert profile["enforced_by"] == "os"
    assert _entries(profile)[0]["enforced_by"] == "os"
    assert _entries(profile)[-1] == {"path": "*", "access": "deny", "enforced_by": "os"}


def test_container_outside_paths_are_absent_not_denied():
    profile = permission_profile(sandbox=DOCKER, safety=_policy(), workspace="<workspace:kbgai>")
    # 容器里宿主路径根本不在挂载表里：说"拒"比说"没有"更含糊，这里要落 "none"。
    assert _entries(profile)[-1] == {"path": "*", "access": "none", "enforced_by": "os"}
    assert profile["network"] == {"requested": "none", "enforced_by": "os", "command_gate": "deny"}


def test_off_tier_still_reports_the_path_gate():
    """关档不等于没有边界：路径闸门是能力边界，不随沙箱档位开关。"""
    profile = permission_profile(sandbox=OFF, safety=_policy(), workspace="<workspace:kbgai>")
    assert profile["type"] == "managed"
    assert profile["enforced_by"] == "policy"
    assert _entries(profile)[-1]["access"] == "deny"


def test_unrestricted_is_only_said_when_no_layer_is_left():
    profile = permission_profile(
        sandbox=OFF,
        safety=_policy(restrict_to_workspace=False, network_enabled=True),
        workspace="<workspace:kbgai>",
    )
    assert profile["type"] == "unrestricted"
    assert profile["file_system"]["type"] == "unrestricted"
    assert _entries(profile)[-1] == {"path": "*", "access": "write", "enforced_by": "none"}
    # 关闭「仅限工作区」后 skip 这条语义不再适用，不许继续写 "skip"
    assert profile["missing_path_behavior"] == "not_applicable"
    assert profile["network"]["enforced_by"] == "none"


def test_missing_path_behavior_matches_what_the_gate_really_does(tmp_path):
    """四个字母必须是量出来的：存在的外部目标被拒，不存在的外部目标跳过。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    policy = _policy()

    hit = policy.gate("run_shell", {"command": f'type "{secret}"'}, workspace)
    assert hit.denied and hit.rule == "capability:path_escape"
    miss = policy.gate("run_shell", {"command": f'type "{tmp_path / "not-yet-there.txt"}"'}, workspace)
    assert miss.allowed, miss.reason
    profile = permission_profile(sandbox=JOB_ONLY, safety=policy, workspace="<workspace:ws>")
    assert profile["missing_path_behavior"] == safety_mod.MISSING_PATH_BEHAVIOR == "skip"


def test_profile_does_not_leak_the_host_path_of_allowed_roots():
    profile = permission_profile(
        sandbox=JOB_ONLY,
        safety=_policy(allowed_roots=("C:\\Users\\someone\\secrets",)),
        workspace="<workspace:kbgai>",
    )
    joined = repr(_entries(profile))
    assert "someone" not in joined and "C:\\\\Users" not in joined


def test_turn_context_carries_the_profile_and_digest_stays_stable():
    kwargs = dict(
        node="agent", step=1, role="main", model="m", provider="p",
        permission_mode="default", permissions={"mode": "default"},
        workspace_root="E:\\projects\\kbgai", sandbox_tier="off",
        safety=_policy(),
    )
    first = turn_context(**kwargs)
    second = turn_context(**kwargs)
    profile = first["permission_profile"]
    assert profile["type"] in ("managed", "unrestricted")
    assert profile["file_system"]["entries"][0]["path"] == "<workspace:kbgai>"
    assert digest(first) == digest(second)


def test_evidence_reports_unrecorded_instead_of_guessing():
    boundaries = boundary_report([], {})
    assert boundaries["permission_profile"]["type"] == "unrecorded"


def test_evidence_uses_the_last_turn_snapshot():
    old = {"type": "managed", "enforced_by": "policy", "file_system": {"entries": []}}
    new = {"type": "unrestricted", "enforced_by": "none", "file_system": {"entries": []}}
    events = [
        types.SimpleNamespace(type="turn.context", payload={"permission_profile": old}),
        types.SimpleNamespace(type="tool.completed", payload={"boundary_kind": "process"}),
        types.SimpleNamespace(type="turn.context", payload={"permission_profile": new}),
    ]
    boundaries = boundary_report(events, {})
    # 与事件逐字段相等，不是另写一份期望
    assert boundaries["permission_profile"] == new
    assert boundaries["tool_calls"]["process"] == 1
