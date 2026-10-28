"""群轮中途排不上的点名：发言上限与队列深度两条路都要有人把话说清楚。

建轮时被挤掉的人由 `create_user_round` 补说明（#139 已钉）；这里管的是**发言过程中**
成员点名、但这一轮已经排不下的人——过去直接在循环里 `break`，群里看不出「我点的人没排上」。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.test_agent_v04 import API, Env, env  # noqa: F401
from tests.test_team import _model_response, make_group, messages_of, wait_round

from blank_api.db import get_session_factory
from blank_api.models.team import TeamMember


def _system_text(request: httpx.Request) -> str:
    payload = json.loads(request.content)
    return "\n".join(
        str(item.get("content") or "")
        for item in payload.get("messages") or []
        if item.get("role") == "system"
    )


async def test_mentions_dropped_at_the_speaker_cap_are_named(env, workspace):  # noqa: F811
    """发言次数排满时，被点却轮不到的人要有一句可核对说明（含上限次数与名字）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        system = _system_text(request)
        if "你的身份：评审" in system:
            return _model_response("评审通过。")
        if "你的身份：测试" in system:
            return _model_response("测试通过。")
        return _model_response("@评审 @测试 你们分头做登录页。")

    env.mock._handler = handler
    _, _, group_id = await make_group(env, workspace, members=("评审", "测试"), max_speakers=2)

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做登录页"})
    state = await wait_round(env, group_id, ("completed",))

    # 上限 2 次 = 主理人开场 + 一位成员；测试确实没发言。
    names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert names == ["小白管家", "评审"], json.dumps(names, ensure_ascii=False)
    line = next((row for row in messages_of(state, "system") if "测试" in row["content"]), None)
    assert line is not None, "被挤掉的人不许静默消失"
    assert "没排上" in line["content"], line["content"]
    assert "2" in line["content"], "说明里要写明这一轮的上限次数"
    assert line["round_id"] == state["round"]["id"], line


async def test_mentions_dropped_by_queue_depth_say_so(env, workspace):  # noqa: F811
    """待发言队列已满时，新点名的人排不进去，也要留下可核对的说明。

    API 现在最多 5 名成员（含主理人），而队列容量也是 4：从正常入口打不满这一格。
    这里直接补一条成员行，钉的是历史数据 / 日后放宽成员上限时这条兜底不许静默丢人。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        system = _system_text(request)
        payload = json.loads(request.content)
        everything = "\n".join(str(item.get("content") or "") for item in payload.get("messages") or [])
        if "你的身份：后端" in system:
            return _model_response("后端完成。")
        if "你的身份：评审" in system:
            return _model_response("评审完成。")
        if "你的身份：测试" in system:
            return _model_response("测试完成。")
        if "你的身份：设计" in system:
            return _model_response("设计完成。")
        # 一次发言内模型可能被调用多轮：按"这一轮要干什么"分，不按调用次数分。
        # 收尾轮不再点名，否则队列空出来的位置会把他塞进去。
        if "请收尾本轮讨论" in everything:
            return _model_response("这一轮就到这里。")
        return _model_response("@运维 也一起看看。")

    env.mock._handler = handler
    _, provider_id, group_id = await make_group(
        env, workspace, members=("后端", "评审", "测试", "设计"), max_speakers=8
    )
    fifth = await env.client.post(
        f"{API}/team/bots",
        json={"name": "运维", "role": "运维角色", "provider_id": provider_id},
    )
    assert fifth.status_code == 201, fifth.text
    async with get_session_factory()() as session:
        session.add(
            TeamMember(
                id=str(uuid.uuid4()),
                group_id=group_id,
                bot_id=fifth.json()["id"],
                position=5,
                enabled=True,
            )
        )
        await session.commit()

    # 用户一次点满 4 位，队列正好达到容量上限；主理人开场再点第 5 位。
    await env.client.post(
        f"{API}/team/groups/{group_id}/messages",
        json={"content": "@后端 @评审 @测试 @设计 一起做登录页"},
    )
    state = await wait_round(env, group_id, ("completed",))

    names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert "运维" not in names, json.dumps(names, ensure_ascii=False)
    line = next((row for row in messages_of(state, "system") if "运维" in row["content"]), None)
    assert line is not None, "队列满导致没排上的人不许静默消失"
    assert "没排上" in line["content"], line["content"]
    assert "4" in line["content"], "说明里要写明队列容量"
    assert line["round_id"] == state["round"]["id"], line
