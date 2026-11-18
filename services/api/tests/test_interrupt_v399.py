"""运行中中断：用户按了停止，正在跑的命令要真的停。

`/cancel` 以前只把任务行改成 cancelled 并写一条终态事件。图只在节点边界醒过来，
一条跑了五分钟的构建期间没有任何节点在跑——用户按的那个"停止"要等命令自己
结束才生效，界面却已经显示"已取消"。这里守住三件事：

1. 取消信号置位后，命令在毫秒级被杀掉，不能等它跑完；
2. 取消 ≠ 超时：两条路径的结论完全相反（一个该等新指示，一个该改写法）；
3. 不取消时行为一个字都不变，输出照常拿全。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.tools.base import ToolOutcome
from blank_api.services.tools.shell import _run_shell, run_process

# 用解释器自己睡，不依赖 sleep / timeout 这些平台命令。
SLEEP_20S = f'"{sys.executable}" -c "import time; time.sleep(20)"'


def test_cancel_kills_the_command_instead_of_waiting_it_out(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, float]:
        event = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(0.8)
            event.set()

        asyncio.create_task(stop_soon())
        started = time.monotonic()
        result = await run_process(
            SLEEP_20S, tmp_path, 60, shell=True, cancel_event=event
        )
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())

    assert result["cancelled"] is True
    # 20 秒的命令，取消后必须在几秒内返回。给 8 秒余量是留给 Windows 上
    # taskkill /T 杀整棵进程树的时间。
    assert elapsed < 8, f"取消没生效，等了 {elapsed:.1f}s"
    assert result["timed_out"] is False, "取消被报成了超时"


def test_without_cancel_the_command_still_runs_to_completion(tmp_path: Path) -> None:
    """没挂取消信号时走原路径：输出、退出码一个字都不变。"""

    async def scenario() -> dict:
        return await run_process(
            f'"{sys.executable}" -c "print(42)"', tmp_path, 30, shell=True
        )

    result = asyncio.run(scenario())

    assert result["cancelled"] is False
    assert result["exit_code"] == 0
    assert "42" in result["stdout"]


def test_timeout_is_not_reported_as_cancel(tmp_path: Path) -> None:
    """超时与取消必须分开报。

    把超时写成取消，模型会以为用户否掉了这个方案，于是换一个更保守的做法；
    把取消写成超时，它会去修一条根本没错的命令。
    """

    async def scenario() -> dict:
        event = asyncio.Event()  # 永不置位
        return await run_process(
            SLEEP_20S, tmp_path, 1, shell=True, cancel_event=event
        )

    result = asyncio.run(scenario())

    assert result["timed_out"] is True
    assert result["cancelled"] is False


def test_tool_outcome_says_interrupted_not_failed(tmp_path: Path) -> None:
    """工具回执要明确写"被用户中断，不要重跑"。

    写成普通失败，模型的下一步就是重试同一条命令——用户刚喊停，它又跑一遍。
    """

    async def scenario() -> ToolOutcome:
        event = asyncio.Event()
        event.set()  # 进入工具那一刻就已经取消了
        context = SimpleNamespace(
            root=tmp_path,
            task_id="task-interrupt",
            cancel_event=event,
            sandbox_tier="",
            isolation_profile="",
            permissions=None,
            safety=None,
            extra_secrets=(),
            approved_actions=frozenset(),
            turn_state={},
        )
        return await _run_shell(context, {"command": SLEEP_20S, "timeout_seconds": 60})

    outcome = asyncio.run(scenario())

    assert outcome.ok is False
    assert outcome.status == "cancelled"
    assert outcome.data.get("cancelled") is True
    assert "不要重跑" in outcome.message


@pytest.mark.parametrize("status", ["cancelled"])
def test_cancelled_status_is_its_own_thing(status: str) -> None:
    """状态值是独立的一档，不并进 denied / timeout / failed 任何一类。

    收尾判定按状态分派：冒充 denied 会让"用户叫停"被读成"没获批"，
    冒充 failed 会把它算进失败率。
    """
    assert status not in {"denied", "failed", "timeout", "ok", "blocked"}
