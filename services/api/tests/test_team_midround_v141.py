"""群轮跑到一半时改设置 / 加人：哪些立刻生效、哪些要等本轮结束。

口径（本文件五条用例钉住，改动都发生在真实审批挂起期间）：

- **发言上限是实时读的**。中途调低，当前轮在下一次判定就收口并说明；中途调高，
  当前轮之后被点名的人按新上限排得进。
- **加人走主理人工具可以在轮中完成**，但新成员不进当前轮队列（队列开轮时定格）；
  本轮有人 @ 他，他才上场。
- **用户直接改成员列表要等本轮结束**：轮次还在跑时 PATCH member_ids 回 409，
  轮次结束后同一个请求正常通过——拒绝的是这一刻，不是这个动作。
"""

from __future__ import annotations

import json

import httpx

from tests.test_agent_v04 import API, Env, call, env  # noqa: F401
from tests.test_team import _model_response, make_group, messages_of, wait_round

from blank_api.db import get_session_factory
from blank_api.models.team import TeamBot, TeamMember


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


def _identity(system: str) -> str:
    if "你的身份：" not in system:
        return ""
    return system.split("你的身份：", 1)[1].split("\n", 1)[0].strip()


async def _member_names(group_id: str) -> list[str]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                TeamMember.__table__.select().where(TeamMember.group_id == group_id)
            )
        ).mappings()
        names = []
        for row in rows:
            bot = await session.get(TeamBot, row["bot_id"])
            names.append(bot.name if bot is not None else row["bot_id"])
    return names


async def _approve(env: Env, message_id: int) -> None:
    resp = await env.client.post(f"{API}/team/messages/{message_id}/approval", json={"approve": True})
    assert resp.status_code == 202, resp.text


def _install(
    env: Env,
    *,
    pauses: dict[str, str] | None = None,
    admin_tool: tuple[str, dict] | None = None,
    admin_final: str = "大家分头做登录页。",
) -> None:
    """按身份回话；pauses 里的成员先发一条要审批的命令，批完再说那句话。"""

    paused = pauses or {}
    issued = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        system = _system_text(request)
        after_tool = _has_tool_result(request)
        if "主理人" in _identity(system):
            # 同一个动作只张一次卡：收尾那一轮别再挂第二张。
            if admin_tool is not None and not after_tool and not issued:
                issued = True
                name, arguments = admin_tool
                return _model_response("我把人拉进来。", tool_calls=[call(name, arguments)])
            return _model_response(admin_final)
        for name, final in paused.items():
            if f"你的身份：{name}" in system:
                if not after_tool:
                    return _model_response(
                        "需要先联网确认。",
                        tool_calls=[call("run_shell", {"command": "curl https://example.com"})],
                    )
                return _model_response(final)
        if _identity(system):
            return _model_response(f"{_identity(system)}完成。")
        return _model_response(admin_final)

    env.mock._handler = handler


async def test_lowering_the_speaker_cap_mid_round_stops_at_the_new_value(env, workspace):  # noqa: F811
    """上限调低后，当前轮的下一次判定就收口——不把已经排队的人再放出来。"""

    _install(env, pauses={"评审": "评审完成。"})
    _, _, group_id = await make_group(env, workspace, members=("评审", "测试", "设计"), max_speakers=5)

    await env.client.post(
        f"{API}/team/groups/{group_id}/messages",
        json={"content": "@评审 @测试 @设计 一起做登录页"},
    )
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    # 审批挂起时用户把上限压到 2（主理人开场 + 评审这一位）。
    patched = await env.client.patch(f"{API}/team/groups/{group_id}", json={"max_speakers": 2})
    assert patched.status_code == 200, patched.text
    await _approve(env, waiting["pending"][0]["message_id"])
    state = await wait_round(env, group_id, ("completed",))

    names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert names == ["小白管家", "评审"], json.dumps(names, ensure_ascii=False)
    line = next((row for row in messages_of(state, "system") if "上限" in row["content"]), None)
    assert line is not None, "收口必须说明原因，不能让排队的人静默消失"
    assert "2" in line["content"], f"说明里要念出调低后的上限：{line['content']}"


async def test_raising_the_speaker_cap_mid_round_lets_later_mentions_in(env, workspace):  # noqa: F811
    """上限调高后，当前轮后续的点名按新上限排得进去（不是等下一轮才认）。"""

    _install(env, pauses={"评审": "@测试 @设计 一起补上。"})
    _, _, group_id = await make_group(env, workspace, members=("评审", "测试", "设计"), max_speakers=2)

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "@评审 先看一下"})
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    # 开轮时上限是 2，评审点两个人本该排不下；挂起时用户把它调到 4。
    patched = await env.client.patch(f"{API}/team/groups/{group_id}", json={"max_speakers": 4})
    assert patched.status_code == 200, patched.text
    await _approve(env, waiting["pending"][0]["message_id"])
    state = await wait_round(env, group_id, ("completed",))

    names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert names == ["小白管家", "评审", "测试", "设计"], json.dumps(names, ensure_ascii=False)
    dropped = [row["content"] for row in messages_of(state, "system") if "没排上" in row["content"]]
    assert dropped == [], f"上限已经调高了，不该再说有人排不上：{dropped}"


async def test_a_member_added_mid_round_is_not_queued_without_a_mention(env, workspace):  # noqa: F811
    """主理人在轮中把人拉进群：人真的进来了，但不会被自动安排发言。"""

    _install(env, admin_tool=("team_add_member", {"bot": "运维"}), admin_final="收到。")
    _, provider_id, group_id = await make_group(env, workspace, members=("评审",), max_speakers=5)
    created = await env.client.post(
        f"{API}/team/bots",
        json={"name": "运维", "role": "运维角色", "provider_id": provider_id},
    )
    assert created.status_code == 201, created.text

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做登录页"})
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    assert waiting["pending"][0]["approvals"][0]["tool"] == "team_add_member", waiting["pending"]
    await _approve(env, waiting["pending"][0]["message_id"])
    state = await wait_round(env, group_id, ("completed",))

    assert "运维" in await _member_names(group_id), "装置自检：人必须真的在轮次中途进了群"
    names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert "运维" not in names, json.dumps(names, ensure_ascii=False)


async def test_a_member_added_mid_round_joins_when_named(env, workspace):  # noqa: F811
    """中途拉进来的人本轮被点名时可以上场（成员表是实时的，队列不是）。"""

    _install(
        env,
        admin_tool=("team_add_member", {"bot": "运维"}),
        admin_final="@运维 也一起看看。",
    )
    _, provider_id, group_id = await make_group(env, workspace, members=("评审",), max_speakers=5)
    created = await env.client.post(
        f"{API}/team/bots",
        json={"name": "运维", "role": "运维角色", "provider_id": provider_id},
    )
    assert created.status_code == 201, created.text

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做登录页"})
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    assert waiting["pending"][0]["approvals"][0]["tool"] == "team_add_member", waiting["pending"]
    await _approve(env, waiting["pending"][0]["message_id"])
    state = await wait_round(env, group_id, ("completed",))

    assert "运维" in await _member_names(group_id), "装置自检：人必须真的在轮次中途进了群"
    names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert "运维" in names, f"被点名的人在同一个群里可以上场：{json.dumps(names, ensure_ascii=False)}"


async def test_member_list_cannot_change_while_a_round_is_running(env, workspace):  # noqa: F811
    """用户改成员列表要等本轮结束：轮中点回 409，轮次结束后同一个请求通过。"""

    _install(env, pauses={"评审": "评审完成。"}, admin_final="评审先看一下。")
    _, _, group_id = await make_group(env, workspace, members=("评审",), max_speakers=5)
    async with get_session_factory()() as session:
        member_rows = list(
            (
                await session.execute(
                    TeamMember.__table__.select().where(TeamMember.group_id == group_id)
                )
            ).mappings()
        )
    member_ids = [row["bot_id"] for row in member_rows]

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "@评审 做登录页"})
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    blocked = await env.client.patch(f"{API}/team/groups/{group_id}", json={"member_ids": member_ids})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "team_group_busy", blocked.text

    await _approve(env, waiting["pending"][0]["message_id"])
    await wait_round(env, group_id, ("completed",))
    # 同一个请求、同一份名单：拒绝的是"这一轮还在跑"这一刻，不是这个动作本身。
    allowed = await env.client.patch(f"{API}/team/groups/{group_id}", json={"member_ids": member_ids})
    assert allowed.status_code == 200, allowed.text
