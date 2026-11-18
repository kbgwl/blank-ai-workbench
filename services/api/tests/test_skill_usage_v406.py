"""技能用量归因：口径必须能被解释，不能虚高。

判据只有三条，但每条都能推翻一个"看起来对"的实现：
1. 只进清单不算调用——`skill.invoked` 才算；
2. 同一任务里重复调用只算一次任务；
3. token 是"带着这条技能干完一次活"的任务总量，不是技能正文的长度。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from blank_api.db import Base
from blank_api.models.model_provider import ModelUsage
from blank_api.models.task import AgentEvent
from blank_api.services.skill_usage import skill_usage


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/usage.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _event(db, task_id: str, skill: str, *, seq: int, when: datetime) -> None:
    db.add(
        AgentEvent(
            id=str(uuid.uuid4()),
            task_id=task_id,
            run_id="run",
            origin_run_id="run",
            seq=seq,
            type="skill.invoked",
            payload={"skill": skill},
            created_at=when,
        )
    )


async def _usage_row(db, task_id: str, prompt: int, completion: int) -> None:
    db.add(
        ModelUsage(
            id=str(uuid.uuid4()),
            provider_id="p1",
            task_id=task_id,
            trace_id="t1",
            node="chat",
            model="m",
            prompt_tokens=prompt,
            completion_tokens=completion,
        )
    )


async def test_counts_calls_tasks_and_task_tokens(session) -> None:
    now = datetime.now()
    await _event(session, "task-a", "code-review", seq=1, when=now)
    # 同一任务里再调一次：算 2 次调用，但只有 1 个任务。
    await _event(session, "task-a", "code-review", seq=2, when=now)
    await _event(session, "task-b", "code-review", seq=1, when=now)
    await _event(session, "task-b", "ui-audit", seq=2, when=now)
    await _usage_row(session, "task-a", 100, 20)
    await _usage_row(session, "task-b", 300, 80)
    await session.commit()

    usage = await skill_usage(session, days=7, now=now)

    review = usage["code-review"]
    assert review.calls == 3
    assert review.tasks == 2
    # 两个任务都带着它：120 + 380。不是技能正文的字符数。
    assert review.tokens == 500
    audit = usage["ui-audit"]
    assert audit.calls == 1
    assert audit.tasks == 1
    # 同一任务同时用了两条技能时，两条都记整份任务成本——切分出来的数字是编的。
    assert audit.tokens == 380


async def test_old_events_fall_outside_the_window(session) -> None:
    now = datetime.now()
    await _event(session, "task-old", "code-review", seq=1, when=now - timedelta(days=10))
    await _usage_row(session, "task-old", 999, 1)
    await session.commit()

    assert await skill_usage(session, days=7, now=now) == {}


async def test_catalog_only_skills_are_not_counted(session) -> None:
    """只出现在清单里（没被真正加载）不该有用量：那只是名字占了一行。"""
    now = datetime.now()
    await _event(session, "task-a", "code-review", seq=1, when=now)
    await session.commit()

    usage = await skill_usage(session, days=7, now=now)
    assert set(usage) == {"code-review"}
    assert "ui-audit" not in usage


async def test_tasks_without_usage_rows_still_report_calls(session) -> None:
    """账本缺行（老任务 / 供应商已删）时不能丢掉"调用过"这个事实。"""
    now = datetime.now()
    await _event(session, "task-x", "commit-pr", seq=1, when=now)
    await session.commit()

    usage = await skill_usage(session, days=7, now=now)
    assert usage["commit-pr"].calls == 1
    assert usage["commit-pr"].tokens == 0
