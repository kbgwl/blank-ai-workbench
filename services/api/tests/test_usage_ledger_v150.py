"""用量台账：把 token 拆成「产物 / 判断 / 废纸」，口径要能对得上 scripts/token_ledger.py。

只看总额的台账没有用：900 万 token 里可能有 700 万出自一个跑废的任务。
这一组钉的是四件事：

1. 三种去向分得对（成功且有改动 = 产物；成功没改动 = 判断；失败 = 废纸）；
2. 还在跑的任务单列，不算进任何一类（钱还没结算）；
3. 聚合数字不重复计数（同一文件改三次只算一个文件）；
4. 空库不炸——新装用户第一次点开台账就是这种形状。
"""

from __future__ import annotations

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.services.usage_ledger import bucket_of, build_ledger, summarize, top_offenders


def _record(**overrides) -> dict:
    base = {
        "id": "t1",
        "title": "改首页",
        "status": "succeeded",
        "calls": 4,
        "prompt_tokens": 800,
        "completion_tokens": 200,
        "cached_tokens": 400,
        "tokens": 1_000,
        "peak_context": 900,
        "tools": 4,
        "failed_tools": 0,
        "files": 1,
        "steps": 4,
    }
    base.update(overrides)
    return base


def test_bucket_separates_product_judgement_and_waste() -> None:
    assert bucket_of(_record(status="succeeded", files=2)) == "delivered"
    assert bucket_of(_record(status="succeeded", files=0)) == "advised"
    assert bucket_of(_record(status="failed")) == "wasted"
    assert bucket_of(_record(status="cancelled")) == "wasted"


def test_a_running_task_is_not_counted_into_any_bucket() -> None:
    """钱还没结算就归到任何一类都是失真：跑飞的任务不该在失败前就算进废纸。"""
    assert bucket_of(_record(status="running")) == "running"
    assert bucket_of(_record(status="waiting_approval")) == "running"


def test_summary_reports_effective_share_and_per_file_cost() -> None:
    records = [
        _record(id="a", tokens=1_000, prompt_tokens=800, completion_tokens=200, files=1),
        _record(id="b", status="failed", tokens=3_000, prompt_tokens=3_000, completion_tokens=0, files=0),
        _record(id="c", tokens=1_000, prompt_tokens=700, completion_tokens=300, files=0),
    ]
    summary = summarize(records)

    assert summary["total_tokens"] == 5_000
    assert summary["buckets"]["delivered"]["tokens"] == 1_000
    assert summary["buckets"]["wasted"]["tokens"] == 3_000
    assert summary["buckets"]["advised"]["tokens"] == 1_000
    assert summary["effective_share"] == pytest.approx(0.2)
    # 每有效改动文件 token 只算 delivered 那一类：把废纸摊进分母会让改动看起来更贵。
    assert summary["tokens_per_file"] == 1_000


def test_cache_hit_is_a_ratio_of_prompt_tokens_not_of_the_total() -> None:
    summary = summarize([_record(prompt_tokens=1_000, completion_tokens=9_000, cached_tokens=500)])
    assert summary["cache_hit"] == pytest.approx(0.5)


def test_offenders_are_ordered_by_spend() -> None:
    records = [_record(id="cheap", tokens=10), _record(id="dear", tokens=9_000)]
    assert [row["id"] for row in top_offenders(records)] == ["dear", "cheap"]


@pytest.fixture
async def fresh_db():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="p1", name="台账"))
        await session.commit()
    return None


async def _task(task_id: str, status: str, steps: int = 3) -> None:
    async with get_session_factory()() as session:
        session.add(Task(id=task_id, project_id="p1", title=task_id, goal="x", status=status, steps_used=steps))
        await session.commit()


async def _usage(task_id: str, prompt: int, completion: int, cached: int = 0) -> None:
    async with get_session_factory()() as session:
        session.add(
            ModelUsage(
                id=f"u-{task_id}-{prompt}",
                provider_id="prov",
                task_id=task_id,
                model="mock",
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
            )
        )
        await session.commit()


async def test_empty_database_gives_a_zero_ledger_instead_of_crashing(fresh_db) -> None:
    async with get_session_factory()() as session:
        ledger = await build_ledger(session, days=7)

    assert ledger["summary"]["total_tokens"] == 0
    assert ledger["summary"]["effective_share"] == 0
    assert ledger["tasks"] == []
    assert ledger["offenders"] == []


async def test_the_same_file_edited_three_times_is_one_file(fresh_db) -> None:
    """要的是「动了几个文件」，不是「打了几次补丁」。"""
    await _task("t-deliver", "succeeded")
    await _usage("t-deliver", 800, 200)
    async with get_session_factory()() as session:
        for index in range(3):
            session.add(
                FileChange(
                    id=f"f{index}",
                    task_id="t-deliver",
                    path="index.html",
                    change_type="modified",
                    diff="+x",
                )
            )
        await session.commit()
    async with get_session_factory()() as session:
        ledger = await build_ledger(session, days=7)

    row = ledger["tasks"][0]
    assert row["files"] == 1
    assert row["bucket"] == "delivered"
    assert row["tokens"] == 1_000


async def test_tool_failures_are_counted_per_task(fresh_db) -> None:
    await _task("t-tools", "failed")
    await _usage("t-tools", 500, 0)
    async with get_session_factory()() as session:
        session.add(ToolCall(id="c1", task_id="t-tools", name="run_shell", status="error"))
        session.add(ToolCall(id="c2", task_id="t-tools", name="read_file", status="ok"))
        await session.commit()
    async with get_session_factory()() as session:
        ledger = await build_ledger(session, days=7)

    row = ledger["tasks"][0]
    assert row["tools"] == 2
    assert row["failed_tools"] == 1
    assert row["bucket"] == "wasted"
    assert ledger["summary"]["failed_tool_rate"] == pytest.approx(0.5)


async def test_blocked_calls_are_counted_from_the_event_stream(fresh_db) -> None:
    """空转拦截数只认事件流里的 `tool.blocked`。

    不重算判定：判定发生在执行那一刻，事后按参数重算会得出一个"当时并没有发生"的数。
    这组还钉住"别的任务不被算进来"——按 task 分组，不是全局 count。
    """
    await _task("t-spin", "failed")
    await _usage("t-spin", 500, 100)
    await _task("t-clean", "succeeded")
    await _usage("t-clean", 200, 50)
    async with get_session_factory()() as session:
        # 两条拦截都落在 t-spin 上
        session.add(
            AgentEvent(id="e1", task_id="t-spin", seq=1, node="execute_action", type="tool.blocked", payload={})
        )
        session.add(
            AgentEvent(id="e2", task_id="t-spin", seq=2, node="execute_action", type="tool.blocked", payload={})
        )
        # 别的事件不该被数进来
        session.add(
            AgentEvent(id="e3", task_id="t-spin", seq=3, node="execute_action", type="tool.completed", payload={})
        )
        # 被拦的那次没有执行：既不算一个工具动作，也不算一次失败。
        # 算成失败会让"这个工具不可靠"和"我们没让它重复"混成一句话。
        session.add(ToolCall(id="c-blocked", task_id="t-spin", name="apply_patch", status="blocked"))
        session.add(ToolCall(id="c-ok", task_id="t-spin", name="read_file", status="ok"))
        await session.commit()
    async with get_session_factory()() as session:
        ledger = await build_ledger(session, days=7)

    rows = {row["id"]: row for row in ledger["tasks"]}
    assert rows["t-spin"]["blocked_calls"] == 2
    assert rows["t-clean"]["blocked_calls"] == 0
    assert ledger["summary"]["blocked_calls"] == 2
    # 拦截不算动作、也不算失败
    assert rows["t-spin"]["tools"] == 1
    assert rows["t-spin"]["failed_tools"] == 0
    assert ledger["summary"]["failed_tool_rate"] == 0
