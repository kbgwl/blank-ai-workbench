"""v157 P0 回归：校验通道里的 run_tests 必须走同一张审批卡。

缺陷（benchmark coding-036 实测）：run_validation 直接调网关执行 run_tests，
拿到 awaiting_approval 只能跳过校验收场——任务照样 succeeded，证据里挂着一条
永远不会消失的「待审批」。用户批与不批都改变不了这个结局。

修复后：
1. 批准 → run_validation 的中断恢复后 run_tests 真实执行，validation 有结论；
2. 驳回 → 如实记 skipped（未获授权），绝不说测试跑过了。
"""

import json
import uuid
from pathlib import Path

import pytest

from tests.test_agent_v04 import (
    API,
    MockModel,
    add_tests_dir,
    call,
    create_project,
    create_provider,
    create_task,
    env,  # noqa: F401  (复用阶段3 的夹具)
)

PATCH = "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('patched')\n"


@pytest.fixture
def confirm_all_commands(tmp_path, monkeypatch):
    """把安全设置调回产品默认档：每条命令都要确认。

    conftest 为全局测试写的是 confirm_all_commands=False 的宽松档；本文件测的
    恰恰是「run_tests 也要审批」这条链路，必须逐用例指向自己的设置文件。
    guardian 一并关掉：审查员不该在这些用例里代批或代驳。
    """
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "guardian": {"mode": "off"},
                "safety": {
                    "shell_enabled": True,
                    "network_enabled": True,
                    "confirm_all_commands": True,
                    "restrict_to_workspace": True,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))
    return settings_file


async def _mutating_task(env, workspace: Path):
    """跑一个改了 main.py、主循环不调 run_tests 的任务，停在校验审批上。"""
    add_tests_dir(workspace)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["main.py 输出 patched", "原有测试全部通过"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 应用补丁\n2. 运行测试"},
            {"content": "开始执行", "tool_calls": [call("apply_patch", {"path": "main.py", "patch": PATCH})]},
            {"content": "补丁已应用"},
            # 校验通道的审批中断不打模型调用；恢复后接着消费这里两步。
            {
                "content": json.dumps(
                    {"approved": True, "findings": [], "summary": "实现与验收条件一致"},
                    ensure_ascii=False,
                )
            },
            {
                "content": json.dumps(
                    {
                        "summary": "已修改 main.py 并通过测试",
                        "acceptance": [
                            {"criterion": "main.py 输出 patched", "covered": True, "evidence": "apply_patch 成功"},
                            {"criterion": "原有测试全部通过", "covered": True, "evidence": "pytest 1 passed"},
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval", resp.json()
    return task_id, provider_id


async def test_validation_channel_approval_executes_run_tests(
    env, workspace: Path, confirm_all_commands
):  # noqa: F811
    task_id, provider_id = await _mutating_task(env, workspace)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id, "scope": "once"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "succeeded", body
    # 批准后测试真的跑了：validation 必须有 pytest 的结论，而不是一句 skipped。
    assert body["evidence"]["validation"]["passed"] is True, body["evidence"]["validation"]
    assert "pytest" in body["evidence"]["validation"]["command"]
    assert body["evidence"]["pending_approvals"] == []
    assert not any(
        "待审批" in risk or "未获批准" in risk for risk in body["evidence"]["unverified_risks"]
    ), body["evidence"]["unverified_risks"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "approval.granted" in types, types
    granted = [event for event in events if event["type"] == "approval.granted"]
    assert [event["payload"]["tool"] for event in granted] == ["run_tests"], granted
    # 重放安全：批准后 run_tests 只执行一次，不因节点重放跑两遍。
    run_test_events = [
        event
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "run_tests"
    ]
    assert len(run_test_events) == 1, run_test_events
    assert run_test_events[0]["payload"]["ok"] is True


async def test_validation_channel_denial_keeps_honest_records(
    env, workspace: Path, confirm_all_commands
):  # noqa: F811
    task_id, provider_id = await _mutating_task(env, workspace)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": False, "provider_id": provider_id, "scope": "once"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 驳回不是失败：代码没问题，是命令没获批。任务照常收尾，但账要如实。
    assert body["task"]["status"] == "succeeded", body
    validation = body["evidence"]["validation"]
    assert validation["passed"] is None and validation["status"] == "skipped", validation
    assert "未获授权" in validation["skipped_reason"], validation
    assert any(
        "未获授权" in risk or "未做自动化校验" in risk for risk in body["evidence"]["unverified_risks"]
    ), body["evidence"]["unverified_risks"]
    assert not any("待审批" in risk for risk in body["evidence"]["unverified_risks"])
    assert body["evidence"]["pending_approvals"] == []

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "approval.denied" in types, types
    # 没批准就没跑：不允许出现任何 run_tests 的成功执行账。
    assert not any(
        event["type"] == "tool.completed"
        and event["payload"].get("tool") == "run_tests"
        and event["payload"].get("ok") is True
        for event in events
    ), types
