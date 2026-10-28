"""团队群聊的边缘路径：轮次进行中的追加发言、队列中途失配与停止。

这一批要钉的四件事都是"用户真的会撞上"的形状：

1. 一轮还在跑时再发一句话——现在按 fail-closed 拒绝（409），**被拒绝的那句不许留下半个痕迹**，
   轮次结束后同一句话必须能正常发出去；
2. 队列里的成员在这一轮中途被停用/移出，轮次仍要走到收尾，而且群聊里要有一句可核对的系统说明；
3. 某个成员的模型调用失败，不能把整轮拖死（其他人照常发言、主理人照常收尾）；
4. 停止按钮落在"等待审批"这一刻：正在等的那条记为已停止，队列里还没轮到的人一个任务都不许建，
   而且停完之后这个群不能永远"上一轮还没说完"。

模型全部走脚本化的 MockTransport；这里判的是编排器的状态翻译，不是模型能力。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from tests.test_agent_v04 import API, Env, call, env  # noqa: F401
from tests.test_team import install_team_model, make_group, messages_of, wait_round

from blank_api.api.routes.team import _BACKGROUND
from blank_api.db import dispose_engine, get_session_factory
from blank_api.models.task import Task
from blank_api.models.team import TeamGroup, TeamMessage, TeamRound
from blank_api.services.team import TeamOrchestrator, create_user_round, ensure_admin, latest_round


@pytest.fixture(autouse=True)
async def _isolate_team_engine(env):  # noqa: F811
    """后台推进器与库连接都绑在事件循环上：跑完先停后台、再释放引擎，否则下一条测试串库。"""
    yield
    app = getattr(getattr(env.client, "_transport", None), "app", None)
    orchestrator = getattr(getattr(app, "state", None), "team_orchestrator", None) if app else None
    if orchestrator is not None:
        await orchestrator.shutdown()
    pending = list(_BACKGROUND)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await dispose_engine()


def _orchestrator(env: Env):  # noqa: F811
    """与 `routes/team.py::_orchestrator` 同一份接线：进程内单例 + 现读 mock 的 factory。"""
    app = getattr(getattr(env.client, "_transport", None), "app", None)
    assert app is not None, "拿不到应用实例"
    state = app.state
    orchestrator = getattr(state, "team_orchestrator", None)
    if orchestrator is None:
        orchestrator = TeamOrchestrator()
        state.team_orchestrator = orchestrator
    orchestrator.client_factory = getattr(state, "model_client_factory", None)
    return orchestrator


def _response(content: str, *, prompt: int = 120, completion: int = 30, tool_calls=None) -> httpx.Response:
    message: dict = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}
            ],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            "model": "mock-team",
        },
    )


def _system_text(request: httpx.Request) -> str:
    payload = json.loads(request.content)
    return "\n".join(
        str(item.get("content") or "")
        for item in payload.get("messages") or []
        if item.get("role") == "system"
    )


async def _bot_by_name(env: Env, name: str) -> dict:  # noqa: F811
    bots = (await env.client.get(f"{API}/team/bots")).json()
    return next(bot for bot in bots if bot["name"] == name)


async def _make_running_round(env: Env, group_id: str, content: str) -> tuple[int, str]:  # noqa: F811
    """手工放一轮进 running 且不接着跑：这是确定状态，不是"模型慢"的时序赌局。"""
    async with get_session_factory()() as session:
        group = await session.get(TeamGroup, group_id)
        admin = await ensure_admin(session)
        message, round_row = await create_user_round(session, group, content, admin)
        return message.id, round_row.id


async def _count(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def test_second_message_during_a_running_round_is_refused_without_trace(env, workspace):  # noqa: F811
    """追加发言 fail-closed：拒得干净，等轮次结束后同一句话必须能发出去。"""
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    first = "先做登录页的骨架"
    second = "顺手把注册页也带上"
    user_id, round_id = await _make_running_round(env, group_id, first)

    refused = await env.client.post(
        f"{API}/team/groups/{group_id}/messages", json={"content": second}
    )
    try:
        assert refused.status_code == 409, refused.text
        body = refused.json()["error"]
        assert body["code"] == "team_group_busy", body
        assert "停止" in body["message"], "拒绝文案要让用户知道出路（等它结束或先停止）"

        async with get_session_factory()() as session:
            # 被拒绝的那句话一个字都不许落库：没有消息行、没有第二轮、搜索索引里也不许有。
            assert await _count(session, TeamMessage, group_id=group_id, content=second) == 0
            assert await _count(session, TeamMessage, group_id=group_id, role="user") == 1
            assert await _count(session, TeamRound, group_id=group_id) == 1
            assert await session.get(TeamRound, round_id) is not None
            assert (await session.get(TeamMessage, user_id)).content == first

        # 轮次自己走完；结束后同一句话必须被正常接受（拒绝的是这一刻，不是这句话）。
        await _orchestrator(env).schedule(round_id)
        await wait_round(env, group_id, ("completed",))

        accepted = await env.client.post(
            f"{API}/team/groups/{group_id}/messages", json={"content": second}
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["accepted"]["round_id"] != round_id
        finished = await wait_round(env, group_id, ("completed",))
        assert [row["content"] for row in messages_of(finished, "user")] == [first, second]
    finally:
        # 判定失败时（比如未来的实现把 busy 闸门拆了）群里可能还压着一轮在推进；
        # 测试库是整轮共享的文件，收个尾免得把后台任务漏给下一条用例。
        await env.client.post(f"{API}/team/groups/{group_id}/stop")


async def test_queued_member_disabled_mid_round_is_skipped_with_a_visible_line(env, workspace):  # noqa: F811
    """队列里的成员中途被停用：跳过要留一句话，轮次不能因此卡住。"""
    install_team_model(env)
    project_id, _, group_id = await make_group(env, workspace, members=("后端",))
    backend = await _bot_by_name(env, "后端")
    _, round_id = await _make_running_round(env, group_id, "@后端 先做登录页")

    async with get_session_factory()() as session:
        round_row = await session.get(TeamRound, round_id)
        assert list(round_row.queue or [])[1:] == [backend["id"]], round_row.queue

    # 直接从设置里关掉（不是主理人调工具那条路）：用户在设置页就能做这个动作。
    patched = await env.client.patch(f"{API}/team/bots/{backend['id']}", json={"enabled": False})
    assert patched.status_code == 200, patched.text

    await _orchestrator(env).schedule(round_id)
    state = await wait_round(env, group_id, ("completed",))

    system_rows = messages_of(state, "system")
    assert any("已停用" in row["content"] for row in system_rows), json.dumps(
        system_rows, ensure_ascii=False
    )
    bot_names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert bot_names == ["小白管家"], bot_names
    async with get_session_factory()() as session:
        # 被跳过的成员一个任务都不许建：群聊里没他的气泡，账上也不该有他的钱。
        assert await _count(session, Task, project_id=project_id) == 1


async def test_one_member_model_failure_does_not_stall_the_round(env, workspace):  # noqa: F811
    """某个成员的模型挂了：那一条如实记失败，其余人照常发言、主理人照常收尾。"""

    def handler(request: httpx.Request) -> httpx.Response:
        system = _system_text(request)
        if "你的身份：后端" in system:
            return httpx.Response(500, json={"error": {"message": "上游炸了"}})
        return _response("收到，@后端 你先处理。")

    env.mock._handler = handler
    project_id, _, group_id = await make_group(env, workspace, members=("后端",))

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))

    bots = messages_of(state, "bot")
    assert [row["bot_name"] for row in bots] == ["小白管家", "后端", "小白管家"], json.dumps(
        bots, ensure_ascii=False
    )
    assert bots[1]["status"] == "failed", bots[1]
    assert bots[1]["content"].strip(), "失败的发言也要有原因，不能是空白气泡"
    assert bots[2]["meta"]["wrap_up"] is True
    assert state["round"]["status"] == "completed"
    async with get_session_factory()() as session:
        statuses = (
            await session.execute(select(Task.status).where(Task.project_id == project_id))
        ).scalars().all()
    assert sorted(statuses) == ["failed", "succeeded", "succeeded"], statuses


async def test_stop_while_waiting_approval_drops_the_queue_and_frees_the_group(env, workspace):  # noqa: F811
    """停止落在"等待审批"这一刻：在等的记已停止，队列里的人一个任务都不建，群立刻能再开会。"""
    calls: dict[str, int] = {"后端": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        system = _system_text(request)
        if "你的身份：后端" in system:
            calls["后端"] += 1
            # 只让第一轮的「后端」申请审批：停止之后的那一轮要能自己走完，
            # 否则"群立刻能再开会"这条判定会被同一个审批卡第二次挡住。
            if calls["后端"] == 1:
                return _response(
                    "需要先联网确认。",
                    tool_calls=[call("run_shell", {"command": "curl https://example.com"})],
                )
            return _response("后端处理完成。")
        if "你的身份：评审" in system:
            return _response("我在，等你分配。")
        return _response("@后端 @评审 一起看看这个登录页。")

    env.mock._handler = handler
    project_id, _, group_id = await make_group(env, workspace, members=("后端", "评审"))

    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "一起做登录页"})
    waiting = await wait_round(env, group_id, ("waiting_approval",))
    assert waiting["pending"], waiting
    round_id = waiting["round"]["id"]
    assert list(waiting["round"]["queue"]), "停止时队列里还有没轮到的成员，这条判定才有意义"

    stopped = await env.client.post(f"{API}/team/groups/{group_id}/stop")
    assert stopped.status_code == 200, stopped.text
    state = (await env.client.get(f"{API}/team/groups/{group_id}/state")).json()
    assert state["round"]["status"] == "cancelled", state["round"]
    stopped_row = next(
        row for row in state["messages"] if row["round_id"] == round_id and row["status"] == "cancelled"
    )
    assert stopped_row["content"] == "已停止", stopped_row
    assert not [row for row in messages_of(state, "bot") if row["bot_name"] == "评审"]
    assert not [row for row in messages_of(state, "bot") if row["meta"].get("wrap_up")], "停止后不许再收尾"
    async with get_session_factory()() as session:
        assert await _count(session, Task, project_id=project_id) == 2

    # 停完之后这个群不许永远"上一轮还没说完"：下一句话必须能开新一轮。
    again = await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "那先只做登录页"})
    assert again.status_code == 201, again.text
    fresh = await wait_round(env, group_id, ("completed", "waiting_approval"))
    assert fresh["round"]["id"] != round_id
    assert fresh["round"]["status"] == "completed", fresh["round"]


async def test_mentions_beyond_the_speaker_cap_say_who_missed_out(env, workspace):  # noqa: F811
    """点名的人多于发言上限时，被挤掉的那位要有一句可核对的说明，不能静默消失。"""
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端", "评审"), max_speakers=2)

    posted = await env.client.post(
        f"{API}/team/groups/{group_id}/messages", json={"content": "@后端 @评审 一起做登录页"}
    )
    assert posted.status_code == 201, posted.text
    state = await wait_round(env, group_id, ("completed",))

    # 上限 2 次：主理人开场 + 一位成员。评审确实没发言（这条不是本用例要改的）。
    bot_names = [row["bot_name"] for row in messages_of(state, "bot")]
    assert bot_names == ["小白管家", "后端"], json.dumps(bot_names, ensure_ascii=False)
    # 但群里必须有人把"为什么没轮到"说清楚：上限几次、谁没排上。
    system_rows = messages_of(state, "system")
    line = next((row for row in system_rows if "评审" in row["content"]), None)
    assert line is not None, json.dumps(system_rows, ensure_ascii=False)
    assert "2" in line["content"], line["content"]
    assert line["round_id"] == state["round"]["id"], line


async def test_latest_round_breaks_the_same_second_tie_by_message_order(env, workspace):  # noqa: F811
    """两轮的时间戳逐字相同（SQLite 的 CURRENT_TIMESTAMP 只到秒）时，最新一轮不能被读成旧轮。"""
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    async with get_session_factory()() as session:
        group = await session.get(TeamGroup, group_id)
        admin = await ensure_admin(session)
        _, first = await create_user_round(session, group, "第一轮", admin)
        first.status = "cancelled"
        await session.commit()
        _, second = await create_user_round(session, group, "第二轮", admin)
        # 显式做成并列：不依赖"跑得够快刚好同一秒"，这条判定每次都得撞上同一个坑。
        second.created_at = first.created_at
        await session.commit()

        latest = await latest_round(session, group_id)
        assert latest is not None
        assert latest.id == second.id, f"最新一轮读成了 {latest.id}（第一轮是 {first.id}）"
