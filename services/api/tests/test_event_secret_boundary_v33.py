"""事件流的密钥边界（方言端到端第 58 步量到的那条空子）。

产品口径一直是"明文密钥只留在用户批准的工作区文件里，库里那份 diff 必须打码"，
而过去只有 `FileChange.diff` 一处兑现了它。#52 把审批卡真的端到端跑起来之后，
`/tasks/{id}/evidence` 的整包 JSON 里仍然能捞出原件——因为 `approval.required`、
`tool.requested`、`approval.granted`、`task.waiting_approval` 这些**事件载荷**
走的是另一条写入路径，一个都没打码。前端逐条渲染时间线，等于把密钥回显给
任何能打开这个任务的人。

下面每条判定都能失败：期望值来自库里读出的行、接口返回或工作区的文件本身。
"""

from pathlib import Path

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent
from blank_api.services.log_safety import contains_secret, mask, mask_deep, redact

from tests.test_agent_v04 import (  # noqa: F401
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

SECRET = "SyntheticNotARealKey0001"  # 合成值，不是凭据
SECRET_PATCH = (
    "--- /dev/null\n+++ b/probe_secret.txt\n@@ -0,0 +1,2 @@\n"
    "+# 审批探针写出的文件（内容是合成的，别当凭据）\n"
    f'+api_key = "{SECRET}"\n'
)


def test_the_mask_fallback_covers_shapes_that_redact_walks_past():
    """redact 认的是"键=值"那一类；`password = "…"` 这类形态它一个字都不改。

    只测 redact 的话，边界上留了空子也照样全绿——所以这里先证明空子存在，
    再证明补上形态兜底之后它被堵住了。
    """
    leaky = 'password = "Sup3rSecretValue123"'
    assert contains_secret(leaky) is True
    assert redact(leaky) == leaky, "redact 本来就不管这种形态（这条断言失败＝它被改过，兜底测试要重看）"
    assert "Sup3rSecretValue123" not in mask(leaky)
    assert mask(f'api_key = "{SECRET}"') == "api_key = ****", "常见形态要保留字段名，人得知道自己在批什么"


def test_deep_masking_reaches_every_string_without_touching_anything_else():
    payload = {
        "tool": "apply_patch",
        "arguments": {"patch": SECRET_PATCH, "path": "probe_secret.txt"},
        "questions": [{"question": "放哪个目录？", "options": ["docs/"]}],
        "risk": "high",
        "answered": 2,
        "nested": [{"token": f"sk-{SECRET}"}],
    }
    masked = mask_deep(payload)
    blob = str(masked)
    assert SECRET not in blob
    assert masked["nested"][0]["token"] == "sk-****", masked["nested"][0]["token"]
    # 结构、非密钥文本、数字都必须原样留下：打码不许把证据变成另一份东西
    assert masked["tool"] == "apply_patch" and masked["arguments"]["path"] == "probe_secret.txt"
    assert masked["questions"] == [{"question": "放哪个目录？", "options": ["docs/"]}]
    assert masked["risk"] == "high" and masked["answered"] == 2
    assert masked["arguments"]["patch"].startswith("--- /dev/null")


async def _waiting_secret_task(env, workspace: Path):  # noqa: F811
    """把一张真的带着明文密钥的审批卡挂起来。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["写入带合成密钥的配置需人工审批"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写入 probe_secret.txt"},
            {"content": "写入配置", "tool_calls": [call("apply_patch", {"path": "probe_secret.txt", "patch": SECRET_PATCH})]},
            {"content": "已写入 probe_secret.txt"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"
    return task_id, provider_id


async def test_the_evidence_package_hides_the_key_while_the_file_keeps_it(env, workspace: Path):  # noqa: F811
    """两头都要对上：库里/接口一份密钥都不留，用户批准写出的那份文件必须原样保留。

    只查一头的话，"两头一起抹掉"这种假绿也能过关——那是把用户的东西删了。
    """
    task_id, provider_id = await _waiting_secret_task(env, workspace)

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert SECRET not in str(events), [event["type"] for event in events if SECRET in str(event)]
    card = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()["pending_approvals"][0]
    patch = card["arguments"]["patch"]
    assert SECRET not in patch and "api_key = ****" in patch, patch
    assert card["risk_level"] in ("high", "critical"), card  # 打码不许把高风险判没了

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    # 执行用的是检查点里那份参数，不是事件里打码后的副本——批准了就得写真东西
    assert (workspace / "probe_secret.txt").read_text(encoding="utf-8").count(SECRET) == 1

    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(AgentEvent).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq))
        ).scalars().all()
    assert rows, "时间线一条都没有，上面的比对是在跟空气较劲"
    assert SECRET not in str([row.payload for row in rows])
