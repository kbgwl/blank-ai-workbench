"""连接池健壮性回归测试。

背景（2026-10-18）：桌面版在“任务启动一步就失败”和“永远停在排队中”两种表现之间
来回跳，真正的原因是一条被取消打断的连接留在了池子里 —— 下一次检出它的人立刻收到
``sqlite3.OperationalError: no active connection``。

两处修复：
1. ``db.engine_options`` 打开 ``pool_pre_ping``，坏连接在检出阶段就被换掉；
2. ``deps.get_db`` 在收尾时屏蔽取消，从源头少造坏连接。

这里把“池子里有一条半关闭连接”直接造出来，验证下一次查询仍然能成功。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from blank_api.db import engine_options, is_transient_db_error


def test_file_sqlite_enables_ping(tmp_path) -> None:
    options = engine_options(f"sqlite+aiosqlite:///{tmp_path}/x.db")
    assert options["pool_pre_ping"] is True
    assert options["connect_args"]["timeout"] >= 10


def test_memory_sqlite_keeps_static_pool() -> None:
    options = engine_options("sqlite+aiosqlite:///:memory:")
    # 内存库只能有一条连接，开了 ping 反而会把库 ping 没
    assert options["pool_pre_ping"] is False
    assert "poolclass" in options


async def test_dead_pooled_connection_is_replaced(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/pool.db"
    engine = create_async_engine(url, **engine_options(url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("create table t (v integer)"))

        # 造一条“半关闭”连接：模拟请求被取消时连接正处在 close() 里
        conn = await engine.connect()
        await conn.execute(text("select 1"))
        raw = (await conn.get_raw_connection()).driver_connection
        await raw.close()
        try:
            await conn.close()
        except Exception:
            pass  # 死连接归还时报错是预期的，池子会把它作废

        async with sessions() as session:
            assert (await session.execute(text("select 1"))).scalar() == 1
    finally:
        await engine.dispose()


def test_stale_connection_errors_are_classified_as_transient() -> None:
    from sqlalchemy.exc import OperationalError

    stale = OperationalError("SELECT 1", {}, Exception("no active connection"))
    locked = OperationalError("SELECT 1", {}, Exception("database is locked"))
    broken = OperationalError("SELECT 1", {}, Exception("no such table: t"))

    assert is_transient_db_error(stale) is True
    assert is_transient_db_error(locked) is True
    # 语法/结构错误不该被无脑重试
    assert is_transient_db_error(broken) is False


def test_plan_that_is_really_an_answer_is_not_treated_as_plan() -> None:
    from blank_api.services.agent.graph import _parse_plan, _plan_is_really_answer

    answer = (
        "我具备这些能力：\n"
        "* **了解项目**：查看目录树、读取工作区内的文件、按文本或正则搜索代码。\n"
        "* **修改代码**：编辑或新建文件，统一使用带上下文补丁的方式。\n"
    )
    plan = _parse_plan(answer)
    assert plan, "前提：这段文本会被当成项目符号"
    assert _plan_is_really_answer(plan, answer) is True

    real_plan = "1. 读取 pyproject.toml\n2. 修改依赖版本\n3. 运行 pytest 验证"
    steps = _parse_plan(real_plan)
    assert _plan_is_really_answer(steps, real_plan) is False


@pytest.mark.parametrize("text", ["", "任务执行完成"])
def test_empty_answers_do_not_crash(text: str) -> None:
    from blank_api.services.agent.graph import _first_answer_text, _last_assistant_text

    assert _first_answer_text([{"role": "assistant", "content": text}]) in ("", "任务执行完成")
    assert _last_assistant_text([{"role": "assistant", "content": text}]) in ("", "任务执行完成")
