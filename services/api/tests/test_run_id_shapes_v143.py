"""#141 `run_id` 四形状：写侧真实产出与读侧命名对齐（设计裁决：不统一写侧）。

裁决与理由：`run_id` 的四种形状是**出处**，不是同一种东西的四种写法——
空串=回合归属上线前没记；`team-<轮>-<消息>[-resume]`=群聊某个成员的这次发言；
`task:<uuid>:attempt:N`=同步入口的兜底键；`agent_runs.id`=排队入口的真实运行。
把它们统一成一种格式会丢掉"这一笔是谁引起的"，而读侧 `run_id_kind()` 已经在
`usage.turns[]` 上给出了单一命名。因此写侧保持自然键，读侧是唯一的命名口。

这里钉的是**真实产出**，不是构造函数：群聊（含审批续跑的 `-resume`）、同步运行、
排队入口三条写路径各跑一遍，产出的值必须被读侧认成对应形状。
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from tests.test_agent_v04 import (  # noqa: F401
    API,
    Env,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_team import (  # noqa: F401
    _isolate_team_engine,
    _model_response,
    make_group,
    messages_of,
    wait_round,
)

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelUsage
from blank_api.models.run import AgentRun
from blank_api.services.agent.evidence import run_id_kind


def _system_text(request: httpx.Request) -> str:
    payload = json.loads(request.content)
    return "\n".join(
        str(item.get("content") or "")
        for item in payload.get("messages") or []
        if item.get("role") == "system"
    )


def _has_tool_result(request: httpx.Request) -> bool:
    payload = json.loads(request.content)
    return any(item.get("role") == "tool" for item in payload.get("messages") or [])


async def _usage_rows(task_ids: list[str]) -> list[ModelUsage]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(ModelUsage).where(ModelUsage.task_id.in_(task_ids)))
        ).scalars().all()
    return list(rows)


async def test_a_team_turn_and_its_resume_are_both_named_team(env: Env, workspace):  # noqa: F811
    """群聊发言写 `team-<轮>-<消息>`；审批放行后的续跑写同一个键的 `-resume`。"""

    _, _, group_id = await make_group(env, workspace, members=("评审",), max_speakers=5)

    def handler(request: httpx.Request) -> httpx.Response:
        system = _system_text(request)
        if "你的身份：评审" in system:
            if not _has_tool_result(request):
                return _model_response(
                    "需要先联网确认。",
                    tool_calls=[call("run_shell", {"command": "curl https://example.com"})],
                )
            return _model_response("评审完成。")
        return _model_response("先请评审看一下。")

    env.mock._handler = handler
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "@评审 先看一下"})
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    pending = next(row for row in messages_of(waiting, "bot") if row["status"] == "waiting_approval")
    approved = await env.client.post(
        f"{API}/team/messages/{pending['id']}/approval", json={"approve": True}
    )
    assert approved.status_code == 202, approved.text
    state = await wait_round(env, group_id, ("completed",))

    round_id = state["round"]["id"]
    task_ids = [row["task_id"] for row in messages_of(state, "bot") if row.get("task_id")]
    assert task_ids, "装置自检：这轮没有任何成员任务，账本无从谈起"
    rows = await _usage_rows(task_ids)
    assert rows, "装置自检：这一轮没有写用量账"

    run_ids = sorted({row.run_id for row in rows})
    assert all(run_id_kind(value) == "team" for value in run_ids), run_ids
    assert all(value.startswith(f"team-{round_id}-") for value in run_ids), run_ids

    resumed = [value for value in run_ids if value.endswith("-resume")]
    assert resumed == [f"team-{round_id}-{pending['id']}-resume"], (
        f"续跑键必须是同一条消息的 -resume：{resumed}"
    )
    plain = [value for value in run_ids if not value.endswith("-resume")]
    assert len(plain) >= 2, f"主理人与成员各该有一条普通发言键：{plain}"
    message_keys = {f"team-{round_id}-{row['id']}" for row in messages_of(state, "bot")}
    assert set(plain) <= message_keys, f"普通键必须指向真实消息：{plain} vs {message_keys}"

    # 团队路径不允许出现"没记"的行：这是回合归属上线后的写路径。
    assert all(run_id_kind(row.run_id) != "unrecorded" for row in rows)


async def test_a_synchronous_run_is_named_attempt(env: Env, workspace):  # noqa: F811
    """同步 `POST /tasks/{id}/run` 的兜底键是 `task:<uuid>:attempt:N`。"""

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    criteria = ["读一次 main.py", "不修改任何文件"]
    task_id = await create_task(env, project_id, criteria)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读取 main.py"},
            {"content": "开始执行", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {
                "content": json.dumps(
                    {"approved": True, "findings": [], "summary": "与验收条件一致"},
                    ensure_ascii=False,
                )
            },
            {
                "content": json.dumps(
                    {
                        "summary": "已读取 main.py",
                        "acceptance": [
                            {"criterion": item, "covered": True, "evidence": "read_file 成功"}
                            for item in criteria
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded", resp.text

    rows = await _usage_rows([task_id])
    assert rows, "装置自检：同步运行没有写用量账"
    expected = f"task:{task_id}:attempt:1"
    assert {row.run_id for row in rows} == {expected}, [row.run_id for row in rows]
    assert run_id_kind(expected) == "attempt"


async def test_a_queued_run_is_named_run(env: Env, workspace, monkeypatch):  # noqa: F811
    """排队入口的 `run_id` 就是 `agent_runs.id`（uuid），读侧念成 `run`。"""

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["不执行，只看入队"])
    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    resp = await env.client.post(f"{API}/tasks/{task_id}/start", json={"provider_id": provider_id})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    run_id = body["run_id"]
    assert body["status"] == "queued"

    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
    assert run is not None and run.status == "queued", run
    assert run_id_kind(run_id) == "run", run_id
    assert not run_id.startswith("team-") and not run_id.startswith("task:"), run_id
