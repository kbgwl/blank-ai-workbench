# -*- coding: utf-8 -*-
"""任务单 §2.3：超时诊断本身也必须有测试，否则它会在最需要的时候坏掉。

诊断函数平时不执行（只有任务卡住才走），属于典型的「写一次、半年不碰、真出事
时抛 AttributeError」的代码。这里用一个真实任务跑一遍，把要求打印的字段逐个
点名；单独放一个文件是为了让 ``test_backend_autostart_v101.py`` 仍严格是 10 例。
"""

from __future__ import annotations

import asyncio
import contextlib

from tests.test_agent_v04 import create_project, create_task, env  # noqa: F401
from tests.test_backend_autostart_v101 import _dump_state


async def test_timeout_dump_names_every_required_fact(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["给 main.py 增加输出"])

    text = await _dump_state(task_id, "诊断自测")

    assert "任务没有到达终态：诊断自测" in text, text
    assert f"task_id={task_id}" in text, text
    for label in (
        "task.status=",
        "goal_id=",
        "goal.status=",
        "provider_id=",
        "dispatch.auto_start=",
        "dispatch.decision=",
        "run_id=",
        "run.status=",
        "run.last_beat=",
        "worker.started=",
        "inprocess_runner.started=",
        "db.connection=",
        "pending_asyncio_tasks=",
        "last_code_node=",
        "最近 ",
        "条事件",
    ):
        assert label in text, f"诊断缺 {label}：{text}"


async def test_timeout_dump_survives_a_missing_task(env, workspace):  # noqa: F811
    """任务连行都没建出来时更不能抛异常——那是最该看到诊断的时刻。"""
    text = await _dump_state("task-never-created", "创建就没落库")
    assert "task 行不存在" in text, text
    assert "run 行不存在" in text, text
    assert "run_id=（无）" in text, text
    assert "db.connection=connected" in text, text


async def test_timeout_dump_sees_a_live_inprocess_runner(env):  # noqa: F811
    """运行态字段不能恒为 no：真起一个 blank-run-* 任务时必须看得见（§1.6）。"""

    async def idle() -> None:
        await asyncio.sleep(30)

    probe = asyncio.create_task(idle(), name="blank-run-diagnostics-probe")
    try:
        await asyncio.sleep(0)  # 让任务真的进事件循环，而不是只拿到一个句柄
        text = await _dump_state("task-never-created", "有活着的进程内 runner")
        assert "inprocess_runner.started=yes" in text, text
        assert "blank-run-diagnostics-probe" in text, text
        assert "db.connection=connected" in text, text
        assert "last_code_node=" in text, text
    finally:
        probe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await probe
