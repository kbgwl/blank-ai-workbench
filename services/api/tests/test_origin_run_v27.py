"""发起轮（origin_run_id）：一轮群聊里几个成员合起来花了多少，要能只问账本就知道。

REA 拆 Codex 的 `token_usage_record{turn_id}` 时，它的行上同时有"这一轮"和"这一整棵
线程树属于哪一轮"两级（docs/REA-WAVE4-CODEX-RUNTIME.md §4.1）。我们 #43 只做了前者：
`run_id` 是每人一个的键，而团队的一轮里每个发言成员各有自己的 Task 和自己的 run
（`team-{轮}-{发言}`，放行续跑再多个 `-resume`）。于是三件事在实物上成立而离线没人管：

① 「这一轮几个人合起来花了多少」答不出，能给的只有编排器自己累加的 `TeamRound.tokens`
   ——那个数只在发言正常收尾时加，中途失败的那次发言钱花了却不计；
② 全群累计的 `usage_by_bot` 是按 group 分组的 join，不按轮筛：单轮群里恰好等于本轮，
   两轮就混账（#46 用真模型量过——每轮 75,214 + 154,691，join 读出 165,552）；
③ `run_id` 声明 String(36)，实物最长 51 字符（`task:{uuid}:attempt:{n}`）。SQLite 不按
   varchar 长度检查所以本地一切正常，换 MySQL/PostgreSQL 就是当场报错。

下面每条判定都能失败，期望值全部来自表里的行、接口返回或迁移后的 DDL，不写死后比对。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from tests.test_agent_v04 import (  # noqa: F401
    API,
    call,
    create_project,
    create_provider,
    env,
)
from tests.test_team import install_team_model, make_group, messages_of, wait_round  # noqa: F401

from blank_api.api.routes.team import _BACKGROUND
from blank_api.db import Base, dispose_engine, get_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.subagent import SubAgentRequest, run_subagent
from blank_api.services.team import round_origin
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

PROVIDER_ID = "provider-origin"
TASK_ID = "task-origin-1"
# 51 字符是 #46 在真实库里量到的最长 run_id（`task:{uuid}:attempt:{n}`）。
# 这里用一个同形状的样本而不是把 51 写死：形状变了这条就该红，而不是悄悄跟着放宽。
ATTEMPT_KEY = f"task:{uuid.uuid4()}:attempt:1"


class _Silent(ModelAdapter):
    def __init__(self, name: str = "mock") -> None:
        super().__init__(
            AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name)
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _OneShot(_Silent):
    async def bind_tools(self, tools, messages, **kwargs):
        return ModelResponse(
            text="已完成", usage=Usage(prompt_tokens=200, completion_tokens=20), model=self.config.name
        )


@pytest.fixture
async def seeded():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-origin", name="发起轮"))
        session.add(
            ModelProvider(
                id=PROVIDER_ID,
                name="deepseek",
                kind="openai_compatible",
                base_url="https://example.invalid",
                default_model="mock",
                supports_tools=True,
            )
        )
        session.add(Task(id=TASK_ID, project_id="project-origin", title="发起轮", goal="发起轮", max_steps=10))
        await session.commit()


def _factory():
    return get_session_factory()


def _session(tmp_path, recorder: EventRecorder) -> AgentSession:
    factory = _factory()
    return AgentSession(
        adapter=_Silent(),
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=recorder,
        session_factory=factory,
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
        goal="发起轮",
        trace_id=recorder.trace_id,
        max_steps=10,
    )


async def _rows() -> list:
    async with _factory()() as session:
        return list((await session.execute(ModelUsage.__table__.select().order_by(ModelUsage.id))).fetchall())


async def _records() -> list[dict]:
    async with _factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
    return [row.payload for row in rows if row.type == "model.usage"]


async def _rows_for(origin: str) -> list:
    async with _factory()() as session:
        return list(
            (await session.execute(select(ModelUsage).where(ModelUsage.origin_run_id == origin))).scalars().all()
        )


# --------------------------------------------------------------------------- #
# 记录器：发起轮只有一个真源
# --------------------------------------------------------------------------- #


def test_a_run_without_an_explicit_origin_is_its_own_origin():
    """顶层运行的发起轮就是它自己——这不是兜底，是定义；给了就用给的。"""
    plain = EventRecorder(None, TASK_ID, run_id="run-a")
    assert plain.origin_run_id == "run-a"
    given = EventRecorder(None, TASK_ID, run_id="team-round-x-1", origin_run_id="team-round-x")
    assert given.origin_run_id == "team-round-x"
    assert given.run_id == "team-round-x-1", "两个键各管一级：run 是每人一个，origin 是整轮一个"
    # 装置自检：两个都没给时读作"当时没记"，不许冒充成别的键
    empty = EventRecorder(None, TASK_ID)
    assert empty.origin_run_id == "" and empty.run_id == ""


async def test_origin_lands_on_the_row_and_the_event_from_one_source(seeded, tmp_path):
    """行与事件里的发起轮必须是记录器那一份：会话自己再存一份，迟早和事件对不上。"""
    recorder = EventRecorder(_factory(), TASK_ID, trace_id="thread-root", run_id="run-a")
    session = _session(tmp_path, recorder)
    await session._record_usage("execute_action", "mock", Usage(prompt_tokens=500, completion_tokens=50), 10, "ok")
    row = (await _rows())[0]
    attribution = (await _records())[0]["attribution"]
    assert row.origin_run_id == "run-a" == attribution["origin_run_id"]
    assert row.run_id == row.origin_run_id, "顶层运行里两级同值；不同值只发生在跨任务的链上"

    recorder.origin_run_id = "team-round-late"
    await session._record_usage("execute_action", "mock", Usage(prompt_tokens=100, completion_tokens=10), 10, "ok")
    # 行是按 uuid 主键排的，不是按插入序；这里按这笔自己的金额认行，不靠顺序猜。
    rows = {row.prompt_tokens: row for row in await _rows()}
    assert sorted(rows) == [100, 500]
    second = rows[100]
    assert second.origin_run_id == "team-round-late", "改记录器两处都跟着变才算同源"
    assert second.run_id == "run-a", "发起轮变了，本轮的 run 不该跟着变"


async def test_a_row_without_an_origin_stays_unrecorded(seeded, tmp_path):
    """没记过的就是没记过：不许拿 task_id 或 trace_id 顶上一个发起轮。"""
    session = _session(tmp_path, EventRecorder(_factory(), TASK_ID, run_id=""))
    await session._record_usage("execute_action", "mock", Usage(prompt_tokens=100, completion_tokens=10), 10, "ok")
    row = (await _rows())[0]
    assert row.origin_run_id == ""
    assert TASK_ID not in (row.origin_run_id, row.run_id)


async def test_a_subagent_inherits_the_origin_it_was_handed(seeded, tmp_path):
    """子代理不认识"团队"这个东西：父辈的发起轮从记录器一路传下去，它照抄。"""
    origin = round_origin("round-1")
    parent_thread = "thread-parent"
    result = await run_subagent(
        adapter=_OneShot("kid"),
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=_factory(),
        recorder=EventRecorder(
            _factory(), TASK_ID, trace_id=parent_thread, run_id="team-round-1-9", origin_run_id=origin
        ),
        request=SubAgentRequest(goal="读 README 并复述", max_steps=2),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id=parent_thread,
        root_thread_id=parent_thread,
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
    )
    assert result.ok, result.error
    row = (await _rows())[0]
    assert row.node == "subagent"
    assert row.origin_run_id == origin, "孙代理自己编一个发起轮，整轮合计就少一笔"
    assert row.run_id == "team-round-1-9", "run 仍是派它那次发言的"


# --------------------------------------------------------------------------- #
# 列宽：声明必须容得下实物
# --------------------------------------------------------------------------- #


def test_declared_widths_fit_the_id_shapes_that_really_get_written():
    """SQLite 不检查 varchar 长度，本地永远看不出问题——这条替 MySQL 把关。

    实物来自 #46 的真实库：两处 `run_id` 当时声明 36，实际最长 51（`task:{uuid}:attempt:{n}`）。
    把声明改回 36，这条立刻红。
    """
    usage = ModelUsage.__table__.c
    events = AgentEvent.__table__.c
    assert len(ATTEMPT_KEY) == 51, f"这一条盯的是 51 字符的实物，样本长度变了要重看：{len(ATTEMPT_KEY)}"
    for label, column in (
        ("model_usage.run_id", usage.run_id),
        ("model_usage.origin_run_id", usage.origin_run_id),
        ("agent_events.run_id", events.run_id),
    ):
        assert column.type.length is not None and column.type.length >= len(ATTEMPT_KEY), (
            f"{label} 声明 {column.type.length}，装不下 51 字符的实物"
        )
    assert usage.origin_run_id.index is True, "整轮合计是按这一列筛的，没索引就是全表扫"
    assert not usage.origin_run_id.nullable


def _alembic(args: list[str], db_path: Path, cwd: Path) -> None:
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env_vars["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", *args],
        cwd=str(cwd),
        env=env_vars,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _column_ddl(db: sqlite3.Connection, table: str, column: str) -> str:
    sql = db.execute("select sql from sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
    for part in sql.replace("\n", " ").split(","):
        if part.strip().startswith(f"{column} "):
            return part.strip()
    return ""


def test_migration_0021_widens_and_adds_without_losing_rows(tmp_path):
    """0021 必须真的能跑：加列 + 建索引 + 放宽两处 run_id，老行原样留着。

    这是整个批次里唯一能失败在"改了模型忘了写迁移"上的判定——DDL 没跟上，
    现有库上按 origin 筛就是 no such column。
    """
    cwd = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "mig.db"
    _alembic(["upgrade", "0020_usage_parallel_group"], db_path, cwd)
    db = sqlite3.connect(db_path)
    assert _column_ddl(db, "model_usage", "origin_run_id") == "", "0020 上不该有这一列"
    assert "(36)" in _column_ddl(db, "model_usage", "run_id"), _column_ddl(db, "model_usage", "run_id")
    db.execute(
        "insert into model_providers (id, name, kind, base_url, api_key_encrypted, default_model,"
        " temperature, max_tokens, context_window, supports_tools, supports_vision, enabled)"
        " values ('p','p','openai_compatible','https://x','','m',0.2,100,1000,0,0,1)"
    )
    db.execute(
        "insert into model_usage (id, provider_id, task_id, trace_id, node, model,"
        " prompt_tokens, completion_tokens, latency_ms, status, run_id)"
        f" values ('u1','p','t1','th','execute_action','m',10,5,1,'ok','{ATTEMPT_KEY}')"
    )
    db.commit()
    db.close()

    _alembic(["upgrade", "head"], db_path, cwd)
    db = sqlite3.connect(db_path)
    assert "VARCHAR(120)" in _column_ddl(db, "model_usage", "run_id").upper(), _column_ddl(
        db, "model_usage", "run_id"
    )
    assert "VARCHAR(120)" in _column_ddl(db, "agent_events", "run_id").upper(), _column_ddl(
        db, "agent_events", "run_id"
    )
    origin_ddl = _column_ddl(db, "model_usage", "origin_run_id").upper()
    assert "VARCHAR(120)" in origin_ddl and "NOT NULL" in origin_ddl, origin_ddl
    assert (
        db.execute(
            "select name from sqlite_master where type='index' and name='ix_model_usage_origin_run_id'"
        ).fetchone()
        is not None
    ), "加列不建索引，按轮筛就是全表扫"
    run_id, origin = db.execute("select run_id, origin_run_id from model_usage where id='u1'").fetchone()
    assert run_id == ATTEMPT_KEY and len(run_id) == 51, "放宽列宽不许动过老数据"
    assert origin == "", "老行的发起轮是「当时没记」，不是被填成别的值"
    db.close()

    _alembic(["downgrade", "0020_usage_parallel_group"], db_path, cwd)
    db = sqlite3.connect(db_path)
    assert "(36)" in _column_ddl(db, "model_usage", "run_id")
    assert _column_ddl(db, "model_usage", "origin_run_id") == ""
    assert db.execute("select count(*) from model_usage").fetchone()[0] == 1, "往返不许丢行"
    db.close()


# --------------------------------------------------------------------------- #
# 真链路：一轮群聊的合计（走 HTTP，钱全部从账本读）
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
async def _isolate_team_engine():
    """团队后台任务与库连接绑事件循环：跑完必须停后台再释放引擎，否则下一条测试串库。"""
    yield
    pending = list(_BACKGROUND)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await dispose_engine()


def _install_model_needing_approval(env) -> None:
    """让「后端」第一次发言申请一条高风险命令：这条命令被放行时会再花一笔钱。

    命令沿用现有团队测试那一条（`curl` 判为 high，一定会停下来等确认）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        messages = payload.get("messages") or []
        system = "\n".join(
            str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        )
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        tool_calls = None
        content = "收到，@后端 你先处理。"
        if "你的身份：后端" in system:
            if has_tool_result:
                content = "后端处理完成。"
            else:
                content = "需要先联网确认。"
                tool_calls = [call("run_shell", {"command": "curl https://example.com"})]
        elif "你的身份：评审" in system:
            content = "[PASS]"
        message: dict = {"content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 30},
                "model": "mock-origin",
            },
        )

    env.mock._handler = handler


async def _round_state(env, group_id: str) -> dict:
    return (await env.client.get(f"{API}/team/groups/{group_id}/state")).json()


async def _any_provider_id() -> str:
    async with _factory()() as session:
        return (await session.execute(select(ModelProvider.id).limit(1))).scalar_one()


async def test_one_round_totals_every_member_across_their_own_tasks(env, workspace):  # noqa: F811
    """这一批的核心判定：一轮里几个成员、几个 Task、几个 run_id，合计只有一个数。

    原来这句话问不出：`run_id` 每人一个，能给的只有编排器自己累加的轮计数。
    """
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))
    round_id = state["round"]["id"]
    usage = state["round"]["usage"]
    origin = round_origin(round_id)

    assert usage["origin_run_id"] == origin
    speaking = [row["task_id"] for row in messages_of(state, "bot") if row["task_id"]]
    assert len(set(speaking)) >= 2, "一轮里至少两个成员各有一个任务，这才叫跨任务"

    rows = await _rows_for(origin)
    assert rows, "按发起轮筛不出一笔账，就是这一列根本没写进去"
    assert len({row.task_id for row in rows}) == usage["tasks"], "任务数要和账本一致"
    assert len({row.run_id for row in rows}) == usage["runs"], "每人一个 run：run 数应当多于 1"
    assert usage["tasks"] >= 2 and usage["runs"] >= 2
    assert usage["tokens"] == sum(row.prompt_tokens + row.completion_tokens for row in rows)
    assert usage["calls"] == len(rows)
    assert usage["subagent_calls"] == sum(1 for row in rows if row.node == "subagent")
    # 装置自检：每人一档加起来必须等于整轮，加起来不等就是有一档漏了
    assert sum(item["tokens"] for item in usage["by_bot"]) == usage["tokens"]
    assert sum(item["calls"] for item in usage["by_bot"]) == usage["calls"]
    # 干净收尾的一轮里，账本合计应与轮计数相等；不等说明有一头口径漏了
    assert usage["tokens"] == state["round"]["tokens"], json.dumps(
        {"ledger": usage["tokens"], "counter": state["round"]["tokens"]}, ensure_ascii=False
    )


async def test_approved_resume_is_the_same_round_not_a_new_one(env, workspace):  # noqa: F811
    """放行续跑的钱算进被放行那一轮：run_id 多个 `-resume`，发起轮一个字不改。

    按 `run_id` 筛的话，一轮会被续跑切成两半——这正是 #46 量到的四种 run 形状里最难认的一种。
    """
    _install_model_needing_approval(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    posted = await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "确认公网可达"})
    assert posted.status_code == 201, posted.text

    waiting = await wait_round(env, group_id, ("waiting_approval",))
    pending = waiting["pending"]
    assert pending and pending[0]["approvals"], pending
    message_id = pending[0]["message_id"]
    task_id = pending[0]["task_id"]
    before = waiting["round"]["usage"]

    approved = await env.client.post(f"{API}/team/messages/{message_id}/approval", json={"approve": True})
    assert approved.status_code == 202, approved.text
    finished = await wait_round(env, group_id, ("completed", "failed"))
    origin = round_origin(finished["round"]["id"])
    usage = finished["round"]["usage"]
    assert usage["origin_run_id"] == origin

    async with _factory()() as session:
        member_rows = (
            await session.execute(select(ModelUsage).where(ModelUsage.task_id == task_id))
        ).scalars().all()
    runs = {row.run_id for row in member_rows}
    assert any(item.endswith(f"-{message_id}") for item in runs), f"发言那一轮的 run：{runs}"
    assert any(item.endswith(f"-{message_id}-resume") for item in runs), f"续跑的 run 另起一个：{runs}"
    assert {row.origin_run_id for row in member_rows} == {origin}, (
        "续跑那几笔的发起轮必须还是这一轮，否则一轮被切成两轮"
    )
    round_rows = await _rows_for(origin)
    assert usage["runs"] == len({row.run_id for row in round_rows})
    assert usage["calls"] > before["calls"], "放行后的花费要进同一轮的合计"


async def test_the_ledger_counts_money_the_round_counter_missed(env, workspace):  # noqa: F811
    """账本口径比轮计数宽：一笔"发过言但没有对应群消息"的钱，只有按发起轮筛才看得见。

    这一列存在的理由就在这里——`TeamRound.tokens` 只在发言收尾时累加，钱花了却没收尾的那次
    不进它的数；按发起轮筛账本就没有这个盲区。
    """
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))
    round_id = state["round"]["id"]
    before = dict(state["round"]["usage"])
    provider_id = await _any_provider_id()

    async with _factory()() as session:
        session.add(
            ModelUsage(
                id="usage-ghost-1",
                provider_id=provider_id,
                task_id="task-not-in-the-round",
                trace_id="thread-ghost",
                node="execute_action",
                model="mock",
                prompt_tokens=400,
                completion_tokens=40,
                origin_run_id=round_origin(round_id),
            )
        )
        await session.commit()

    after = (await _round_state(env, group_id))["round"]["usage"]
    assert after["tokens"] == before["tokens"] + 440, "按发起轮筛就必须连没对应发言的这笔一起算"
    assert after["calls"] == before["calls"] + 1
    assert state["round"]["tokens"] == before["tokens"], "轮计数不认识这笔——它只加收尾完成的发言"
    ghost = next(item for item in after["by_bot"] if item["bot_id"] == "")
    assert ghost["bot_name"] == "未对应发言" and ghost["tokens"] == 440
    assert sum(item["tokens"] for item in after["by_bot"]) == after["tokens"]


async def test_a_group_wide_field_is_not_a_round_field(env, workspace):  # noqa: F811
    """`usage_by_bot` 是全群累计、`round.usage` 是本轮：用一笔对不齐的差额当证据。

    往本轮某个成员任务上补一笔**没有发起轮**的老账（上线前落下的那种）：它进得了全群累计，
    进不了任何一轮。少了这一列，两个口径就会在两轮群里混成一团。
    """
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))
    member_task = next(row["task_id"] for row in messages_of(state, "bot") if row["task_id"])
    provider_id = await _any_provider_id()

    async with _factory()() as session:
        session.add(
            ModelUsage(
                id="usage-legacy-1",
                provider_id=provider_id,
                task_id=member_task,
                trace_id="thread-legacy",
                node="execute_action",
                model="mock",
                prompt_tokens=700,
                completion_tokens=70,
            )
        )
        await session.commit()

    fresh = await _round_state(env, group_id)
    round_tokens = fresh["round"]["usage"]["tokens"]
    group_tokens = sum(item["tokens"] for item in fresh["usage_by_bot"])
    assert group_tokens - round_tokens == 770, json.dumps(
        {"round": round_tokens, "group": group_tokens}, ensure_ascii=False
    )
    assert fresh["round"]["usage"]["calls"] == state["round"]["usage"]["calls"], "没有发起轮的账不许冒充一轮"


async def test_group_cumulative_counts_money_without_a_speaking_row(env, workspace):  # noqa: F811
    """全群累计不能拿发言行当筛子：发起轮在群里、却没挂到任何消息的账也要进这一档。

    旧实现是 `TeamMessage JOIN ModelUsage ON task_id`——只认"有发言行"的钱。这一笔
    （task_id 不属于任何消息、origin 是本轮）在 `round.usage` 里算得出来，在全群累计里却被
    inner join 悄悄吃掉，而且没有任何"未对应发言"档可以摆它，于是两个口径都说不清它去哪了。
    """
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))
    before = state["usage"]
    provider_id = await _any_provider_id()

    async with _factory()() as session:
        session.add(
            ModelUsage(
                id="usage-group-ghost-1",
                provider_id=provider_id,
                task_id="task-without-any-message",
                trace_id="thread-group-ghost",
                node="subagent",
                model="mock",
                prompt_tokens=400,
                completion_tokens=40,
                origin_run_id=round_origin(state["round"]["id"]),
            )
        )
        await session.commit()

    after = await _round_state(env, group_id)
    usage = after["usage"]
    assert usage["tokens"] == before["tokens"] + 440, json.dumps(
        {"before": before["tokens"], "after": usage["tokens"]}, ensure_ascii=False
    )
    assert usage["calls"] == before["calls"] + 1
    assert usage["subagent_calls"] == before["subagent_calls"] + 1
    ghost = next(item for item in usage["by_bot"] if item["bot_id"] == "")
    assert ghost["bot_name"] == "未对应发言" and ghost["tokens"] == 440
    assert ghost["calls"] == 1 and ghost["subagent_calls"] == 1, ghost
    assert sum(item["tokens"] for item in usage["by_bot"]) == usage["tokens"]
    assert sum(item["calls"] for item in usage["by_bot"]) == usage["calls"]
    assert sum(item["subagent_calls"] for item in usage["by_bot"]) == usage["subagent_calls"]
    assert after["usage_by_bot"] == usage["by_bot"], "兼容字段必须与账本合计同源，不许另算一遍"


async def test_group_cumulative_counts_a_row_matching_both_keys_once(env, workspace):  # noqa: F811
    """既挂在成员任务上、又带着本轮 origin 的账只能加一次。

    两个来源（task_id 属于群内消息 / origin_run_id 属于群内轮次）取并集；不去重的话，
    每一笔正常的发言账都会命中两次，全群合计凭空翻倍。
    """
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))
    member_task = next(row["task_id"] for row in messages_of(state, "bot") if row["task_id"])
    before = state["usage"]
    provider_id = await _any_provider_id()

    async with _factory()() as session:
        session.add(
            ModelUsage(
                id="usage-both-keys-1",
                provider_id=provider_id,
                task_id=member_task,
                trace_id="thread-both-keys",
                node="execute_action",
                model="mock",
                prompt_tokens=700,
                completion_tokens=70,
                origin_run_id=round_origin(state["round"]["id"]),
            )
        )
        await session.commit()

    usage = (await _round_state(env, group_id))["usage"]
    assert usage["tokens"] == before["tokens"] + 770, json.dumps(
        {"before": before["tokens"], "after": usage["tokens"]}, ensure_ascii=False
    )
    assert usage["calls"] == before["calls"] + 1
