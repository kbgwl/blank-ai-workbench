"""#119 异步钩子 + 退出码 2 唤醒（对标 Claude Code 的 asyncRewake）。

REA 原文（docs/REA-WAVE5-CLAUDE-CODE.md §8/P2 #11）：后台钩子退出码 2 时模型
收到一条系统提醒并继续；在此之前我们的钩子是同步等待的。

六条判定，每条都能失败：

1. ``async:true`` 的钩子必须立刻让出控制权（等同步执行，耗时判据就红）；
2. 退出码 2 的后台钩子必须进入唤醒队列，且文本真的来自 stderr；
3. 正常退出的后台钩子只进审计，不唤醒（见 2 就唤醒/见完成就唤醒都会红）；
4. 阻断型事件上的 async 配置必须被忽略：拿不到结果的 veto 不是 veto；
5. 收尾窗口耗尽必须真的杀掉子进程，并把命令记成 abandoned；
6. 唤醒必须真的进入下一次模型请求（只写事件不进请求 = 模型永远不知道）；
   收尾后才到的唤醒必须记 ``hook.wakeup_missed``，不许假装送达。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.hooks import HookRunner, HookSpec
from blank_api.services.models.base import ModelResponse, Usage
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway
from tests.test_agent_v04 import API, create_project, create_provider, create_task, env  # noqa: F401


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def _runner(*, settle_timeout: float = 5.0, **events) -> HookRunner:
    return HookRunner(
        {event: [HookSpec(command=command, async_run=True)] for event, command in events.items()},
        settle_timeout=settle_timeout,
    )


async def test_async_hook_does_not_block_the_caller(tmp_path: Path):
    marker = tmp_path / "late.txt"
    command = _script(
        tmp_path,
        "late.py",
        "import sys, time\n"
        "time.sleep(0.6)\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('late')\n",
    )
    runner = _runner(post_tool_use=command)

    started = time.perf_counter()
    results = await runner.run("post_tool_use", "demo", {"tool": "demo"})
    elapsed = time.perf_counter() - started

    assert results == [], "后台钩子不该在调用点返回结果"
    assert elapsed < 0.3, f"调用点被后台钩子拖住了 {elapsed:.2f}s"
    assert runner.pending == 1
    assert not marker.exists(), "调用点返回时钩子本不该已经跑完"

    completed, abandoned = await runner.close(timeout=5)
    assert abandoned == []
    assert len(completed) == 1
    assert completed[0].status == "ok"
    assert completed[0].background is True
    assert completed[0].as_dict()["async"] is True
    assert marker.exists(), "钩子并没有真的在后台跑起来"


async def test_async_exit_two_becomes_a_wakeup_carrying_stderr(tmp_path: Path):
    command = _script(
        tmp_path,
        "exit2.py",
        "import sys, time\n"
        "time.sleep(0.1)\n"
        "sys.stderr.write('lint found a real problem')\n"
        "sys.exit(2)\n",
    )
    runner = _runner(post_tool_use=command)
    assert await runner.run("post_tool_use", "run_shell", {"tool": "run_shell"}) == []

    completed, abandoned = await runner.close(timeout=5)
    assert abandoned == []
    assert completed and completed[0].exit_code == 2

    wakeups = runner.drain_wakeups()
    assert len(wakeups) == 1, "退出码 2 的后台钩子没有进唤醒队列"
    assert wakeups[0]["exit_code"] == 2
    assert wakeups[0]["event"] == "post_tool_use"
    assert wakeups[0]["tool"] == "run_shell"
    assert "lint found a real problem" in wakeups[0]["text"]
    assert runner.drain_wakeups() == [], "唤醒取走一次后不许重复投递"


async def test_async_exit_zero_is_audited_but_never_wakes(tmp_path: Path):
    command = _script(tmp_path, "ok.py", "print('fine')\n")
    runner = _runner(post_tool_use=command)
    await runner.run("post_tool_use", "demo", {})

    completed, abandoned = await runner.close(timeout=5)
    assert abandoned == []
    assert len(completed) == 1
    assert completed[0].status == "ok"
    assert runner.drain_wakeups() == [], "正常退出的后台钩子也叫醒模型，唤醒就没有意义了"


async def test_async_flag_is_ignored_on_blocking_events(tmp_path: Path):
    command = _script(
        tmp_path,
        "deny.py",
        "import json, sys, time\n"
        "time.sleep(0.2)\n"
        "sys.stdout.write(json.dumps({'decision': 'deny', 'reason': 'blocked by async-configured hook'}))\n",
    )
    runner = _runner(pre_tool_use=command)

    started = time.perf_counter()
    try:
        results = await runner.run("pre_tool_use", "run_shell", {})
    finally:
        elapsed = time.perf_counter() - started
        # 正常实现下这里没有后台任务；判据失败时也不许把子进程留给测试进程。
        await runner.close(timeout=0.01)

    assert results and results[0].denied, "阻断型事件上的 async 配置把 veto 弄丢了"
    assert elapsed >= 0.15, "pre_tool_use 被异步化了：没有等结果就放行"
    assert runner.drain_wakeups() == []


async def test_close_cancels_overdue_hooks_and_reports_abandoned(tmp_path: Path):
    marker = tmp_path / "should-not-exist.txt"
    # 子进程睡 3s 再落标记：观察窗口必须比它长，否则「机器忙、kill 晚了几百毫秒」
    # 会被读成「子进程活着」——全量回归里 0.6s/0.9s 这组就这么红过一次。
    # 加宽窗口只是把噪声挪出判据，判据本身没收窄：窗口内任何时刻出现标记就红。
    command = _script(
        tmp_path,
        "slow.py",
        "import time\n"
        "time.sleep(3.0)\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('alive')\n",
    )
    runner = _runner(settle_timeout=0.1, post_tool_use=command)
    await runner.run("post_tool_use", "demo", {})
    assert runner.pending == 1

    completed, abandoned = await runner.close(timeout=0.1)
    assert completed == []
    assert abandoned == [command], "没等到也没有被记下来，时间线上看不出这个钩子被丢下了"
    assert runner.pending == 0

    # 取消必须真的杀掉子进程：盯满整个窗口，标记一旦出现立刻判红。
    deadline = time.perf_counter() + 3.6
    while time.perf_counter() < deadline:
        assert not marker.exists(), "收尾取消只取消了任务，子进程还在后台跑"
        await asyncio.sleep(0.1)
    assert not marker.exists(), "收尾取消只取消了任务，子进程还在后台跑"


async def test_cancel_during_spawn_still_kills_the_child(tmp_path: Path, monkeypatch):
    """取消恰好落在「子进程创建途中」时，落地的那个进程照样得被杀。

    收尾窗口只有几十毫秒，机器一忙 ``create_subprocess_shell`` 就可能还没返回；
    旧实现在这里拿不到 process，CancelledError 分支只能干瞪眼，孙进程留到写标记。
    """
    marker = tmp_path / "spawn-window.txt"
    command = _script(
        tmp_path,
        "slow_spawn.py",
        "import time\n"
        "time.sleep(1.0)\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('alive')\n",
    )
    real_spawn = asyncio.create_subprocess_shell

    async def slow_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        await asyncio.sleep(0.4)  # 把「创建中」这一段拉到收尾窗口之外
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", slow_spawn)
    runner = _runner(settle_timeout=0.05, post_tool_use=command)
    await runner.run("post_tool_use", "demo", {})
    completed, abandoned = await runner.close(timeout=0.05)
    assert completed == []
    assert abandoned == [command], "取消没被记成 abandoned"

    deadline = time.perf_counter() + 1.6
    while time.perf_counter() < deadline:
        assert not marker.exists(), "取消落在创建途中，子进程成了孤儿"
        await asyncio.sleep(0.05)
    assert not marker.exists(), "取消落在创建途中，子进程成了孤儿"


class _FakeAdapter:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            name="fake",
            model="fake-model",
            reasoning_effort="",
            verbosity="",
            context_window=8000,
            supports_tools=True,
        )
        self.last_provider = "fake"
        self.calls: list[list] = []

    async def bind_tools(self, _specs, chat):
        self.calls.append(list(chat))
        return ModelResponse(
            text="ok",
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            model="fake-model",
        )


async def _seed_task(task_id: str) -> None:
    async with get_session_factory()() as session:
        session.add(Project(id=f"project-{task_id}", name="异步钩子"))
        session.add(
            Task(
                id=task_id,
                project_id=f"project-{task_id}",
                title="异步钩子",
                goal="异步钩子",
                max_steps=10,
            )
        )
        await session.commit()


def _session(task_id: str, root: Path, hooks: HookRunner) -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
        gateway=ToolGateway([]),
        context=ToolContext(root=root, task_id=task_id, session_factory=factory),
        recorder=EventRecorder(factory, task_id),
        session_factory=factory,
        provider_id="provider-1",
        provider_name="mock",
        goal="异步钩子",
        max_steps=10,
        hooks=hooks,
    )


async def _events(task_id: str, type_name: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.task_id == task_id, AgentEvent.type == type_name)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [row.payload for row in rows]


async def test_call_model_receives_the_wakeup_as_a_system_reminder(env, tmp_path):  # noqa: F811
    task_id = "task-async-wake-1"
    await _seed_task(task_id)
    command = _script(
        tmp_path,
        "wake.py",
        "import sys\nsys.stderr.write('fresh lint output: 3 errors')\nsys.exit(2)\n",
    )
    hooks = _runner(post_tool_use=command)
    session = _session(task_id, tmp_path, hooks)
    adapter = session.adapter

    await hooks.run("post_tool_use", "run_shell", {"tool": "run_shell"})
    # 等到后台任务自己结束（不走 close——收尾会把结果取走，就测不到 call_model 的取用）。
    deadline = time.monotonic() + 5
    while hooks.pending and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert hooks.pending == 0, "后台钩子没有在 5s 内结束"

    await session.call_model([{"role": "user", "content": "继续"}], "review_result")

    assert adapter.calls, "模型调用没有发生"
    texts = [str(message.content) for message in adapter.calls[0]]
    assert any(
        "fresh lint output: 3 errors" in text and "退出码 2" in text for text in texts
    ), "唤醒只写进了事件，没有进真正的模型请求"
    assert session.hook_context == [], "唤醒注入后没有清空，会在后续每一步重复出现"

    async_completed = await _events(task_id, "hook.completed")
    assert any(item.get("async") is True for item in async_completed), async_completed
    delivered = await _events(task_id, "hook.wakeup_delivered")
    assert delivered and delivered[0]["count"] == 1, delivered


async def test_wakeup_arriving_after_session_end_is_recorded_as_missed(
    env, workspace: Path, monkeypatch
):  # noqa: F811
    # 收尾后才到的叫醒没有下一次模型调用。这里用最便宜的命令（两种 shell 都认）
    # 保证它落在 close 的有界窗口里，从而走"没送到"而不是"被取消"的分支。
    monkeypatch.setenv(
        "BLANK_HOOKS",
        json.dumps({"session_end": [{"command": "exit 2", "async": True}]}),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读一下 main.py"])
    (workspace / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    env.mock.reset([{"content": "已读取 main.py，无需改动。"}])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    missed = [e for e in events if e["type"] == "hook.wakeup_missed"]
    assert missed, "收尾后到达的退出码 2 唤醒既没送达也没记账"
    assert missed[0]["payload"]["delivered"] is False
    assert missed[0]["payload"]["count"] == 1
    assert not [e for e in events if e["type"] == "hook.abandoned"], events


def test_env_config_supports_async_and_settle_budget(monkeypatch):
    runner = HookRunner.from_env(
        '{"post_tool_use":[{"command":"x","async":true}]}'
    )
    assert runner.hooks["post_tool_use"][0].async_run is True
    plain = HookRunner.from_env('{"post_tool_use":[{"command":"x"}]}')
    assert plain.hooks["post_tool_use"][0].async_run is False
    monkeypatch.setenv("BLANK_HOOK_SETTLE", "0.25")
    tuned = HookRunner.from_env('{"post_tool_use":[{"command":"x"}]}')
    assert tuned.settle_timeout == pytest.approx(0.25)


async def test_hook_console_text_survives_the_local_encoding(tmp_path: Path):
    """钩子按控制台码页（Windows 默认 cp936）写中文时，正文不许静默变形。

    真模型点验实测到：钩子没 `reconfigure` stderr，子进程写的是 GBK 字节，
    而这里按 UTF-8 解码并把解不出的字节 **丢掉**——送进模型系统消息的是乱码，
    还少了 6 个字。丢字比乱码更糟：没人能从产物里看出少过。
    """
    phrase = "后台检查说 read_file 已跑过：路径 中文"
    wake = _script(
        tmp_path,
        "gbk_wake.py",
        f"import sys\nsys.stderr.buffer.write({phrase!r}.encode('gbk'))\nsys.exit(2)\n",
    )
    blocking = _script(
        tmp_path,
        "gbk_block.py",
        f"import sys\nsys.stderr.buffer.write({phrase!r}.encode('gbk'))\nsys.stderr.flush()\nsys.exit(2)\n",
    )
    runner = HookRunner(
        {
            "post_tool_use": [HookSpec(command=wake)],
            "pre_tool_use": [HookSpec(command=blocking)],
        }
    )
    notes = await runner.run("post_tool_use", "", {"tool": "read_file"})
    assert notes[0].additional_context == phrase, repr(notes[0].additional_context)

    veto = await runner.run("pre_tool_use", "", {"tool": "run_shell"})
    assert veto[0].denied, veto[0].as_dict()
    assert veto[0].reason == phrase, repr(veto[0].reason)
