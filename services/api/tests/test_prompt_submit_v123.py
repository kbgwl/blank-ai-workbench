"""#123 提交那一刻的三道决定：补上下文、抑制原文、否决提交。

对标 Claude Code 的 `UserPromptSubmit` / `UserPromptExpansion` 契约（字符串实物
`hookSpecificOutput.suppressOriginalPrompt`、`... .additionalContext`、
`UserPromptSubmit operation blocked by hook:`、阻断事件集合里含 `UserPromptSubmit`）。

判据分三层，每条都能失败：
* 纯解析：`suppressOriginalPrompt` 只认布尔真（Claude 自己的类型校验就是「非 true 即错型」），
  坏输出不许变成隐式抑制；
* HookRunner：提交事件上退出码 2 = 拦下而不是补上下文；配置成后台的提交钩子必须当场跑完，
  拿不到结果的否决不是否决；
* 真链路（HTTP + MockModel 真请求）：钩子给的上下文必须真的出现在发给模型的报文里；
  抑制时模型读到的每一个字节都不含原文（含 system 里上下文管家挑出来的候选文件名），
  而库里的 `tasks.goal` 一个字都不许丢——抑制的是上下文，不是账。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services.hooks import HookRunner, HookSpec, parse_hook_output
from tests.test_agent_v04 import API, call, create_project, create_provider, create_task, env  # noqa: F401

MARKER = "青龙石机密-原文标记"
REPLACEMENT = "按工作区里已有的资料给出结论，不要读任何名字里带机密的文件。"


# --------------------------------------------------------------------------- #
# 层 1：纯解析
# --------------------------------------------------------------------------- #
def test_抑制原文的开关只认布尔真() -> None:
    on = parse_hook_output(
        json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}})
    )
    assert on.get("suppress_original_prompt") is True
    # 字符串 "true" 与 1 都不算：它们来自写错的钩子，静默当真是隐式放行的一种。
    for bad in ("true", 1, "false", 0, None):
        parsed = parse_hook_output(json.dumps({"suppressOriginalPrompt": bad}))
        assert "suppress_original_prompt" not in parsed, repr(bad)
    assert "suppress_original_prompt" not in parse_hook_output(json.dumps({}))


def test_嵌套与顶层两种写法都认() -> None:
    nested = parse_hook_output(
        json.dumps({"hookSpecificOutput": {"additionalContext": REPLACEMENT, "suppressOriginalPrompt": True}})
    )
    top = parse_hook_output(json.dumps({"additionalContext": REPLACEMENT, "suppressOriginalPrompt": True}))
    assert nested["additional_context"] == top["additional_context"] == REPLACEMENT
    assert nested["suppress_original_prompt"] is top["suppress_original_prompt"] is True


def test_输出解不出来时既不算抑制也不算放行() -> None:
    assert parse_hook_output("not json at all") == {}
    assert parse_hook_output("") == {}
    assert parse_hook_output("[1, 2, 3]") == {}


# --------------------------------------------------------------------------- #
# 层 2：HookRunner
# --------------------------------------------------------------------------- #
def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(
        "import json, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "payload = json.load(sys.stdin)\n"
        "goal = payload.get('goal') or ''\n" + body,
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{path}"'


async def test_提交事件上退出码二是拦下而不是补上下文(tmp_path: Path) -> None:
    command = _script(tmp_path, "exit2.py", "sys.stderr.write('这条提交不该进模型')\nsys.exit(2)\n")
    results = await HookRunner({"user_prompt_submit": [HookSpec(command=command)]}).run(
        "user_prompt_submit", "", {"goal": "无所谓"}
    )
    assert results[0].decision == "deny", results[0].as_dict()
    assert "这条提交不该进模型" in results[0].reason
    assert results[0].exit_code == 2


async def test_提交事件上的后台钩子当场跑完(tmp_path: Path) -> None:
    """否决权不能交给一个还没跑完的进程：配置里写 async 也要拿到结果再往下走。"""
    command = _script(
        tmp_path,
        "async_deny.py",
        "sys.stdout.write(json.dumps({'decision': 'deny', 'reason': '后台也要算数'}))\n",
    )
    runner = HookRunner({"user_prompt_submit": [HookSpec(command=command, async_run=True)]})
    results = await runner.run("user_prompt_submit", "", {"goal": "无所谓"})
    assert len(results) == 1, "提交钩子被后台化，否决结果当场拿不到"
    assert results[0].decision == "deny"
    assert results[0].background is False


# --------------------------------------------------------------------------- #
# 层 3：真链路
# --------------------------------------------------------------------------- #
def _hooks(monkeypatch, *commands: str) -> None:
    monkeypatch.setenv(
        "BLANK_HOOKS",
        json.dumps({"user_prompt_submit": [{"command": c, "timeout": 20} for c in commands]}),
    )


def _emit(body: str, suppress: bool = False) -> str:
    """生成钩子正文：stdout 只用 ASCII 转义，免得解码这一层混进判据里。"""
    script = "import json, sys\n"
    script += "out = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': "
    script += repr(body) + "}}\n"
    if suppress:
        script += "out['hookSpecificOutput']['suppressOriginalPrompt'] = True\n"
    script += "sys.stdout.write(json.dumps(out))\n"
    return script


def _all_texts(mock) -> list[str]:
    """把真发出去的报文里所有可能出现正文的地方摊平：系统提示、用户消息、多模态 block。"""
    texts: list[str] = []
    for request in mock.requests:
        for message in request.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(str(block.get("text") or "") for block in content if isinstance(block, dict))
    return texts


def _steps() -> list[dict]:
    return [
        {"content": "计划：\n1. 读 README"},
        {"content": "读取", "tool_calls": [call("read_file", {"path": "README.md"})]},
        {"content": json.dumps({"approved": True, "findings": [], "summary": "ok"}, ensure_ascii=False)},
        {"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)},
    ]


async def _events(env, task_id: str) -> list[dict]:  # noqa: F811
    return (await env.client.get(f"{API}/tasks/{task_id}/events")).json()


async def test_提交钩子补的上下文真的进了模型请求(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command = _script(tmp_path, "note.py", _emit(REPLACEMENT))
    _hooks(monkeypatch, command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["能读到 README"], goal=f"请先把 {MARKER} 那份文件读一遍")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    texts = _all_texts(env.mock)
    # 这条是本批的起点：过去 _fire_hooks 只看审计，钩子给的正文被静默丢掉。
    assert any(REPLACEMENT in text for text in texts), "钩子补的上下文没有进模型请求"
    assert env.mock.requests, "一次模型调用都没发生"


async def test_抑制原文时模型读到的每个字节都不含原文(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    # 工作区里放一个名字含原文标记的文件：上下文管家按关键词挑候选文件，
    # 只要它把这个名字抄进 system，下面的全报文断言就会红——抑制不许留侧门。
    (workspace / f"{MARKER}.md").write_text("无关内容\n", encoding="utf-8")
    command = _script(tmp_path, "suppress.py", _emit(REPLACEMENT, suppress=True))
    _hooks(monkeypatch, command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["给出结论"], goal=f"请围绕 {MARKER} 写一份说明")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert env.mock.requests, "一次模型调用都没发生"

    texts = _all_texts(env.mock)
    assert all(MARKER not in text for text in texts), [text for text in texts if MARKER in text][:1]
    assert any(REPLACEMENT in text for text in texts)

    # 抑制的是上下文，不是账：库里那份原文一个字都不许丢。
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert MARKER in (row.goal or "")

    events = await _events(env, task_id)
    substituted = [item for item in events if item["type"] == "hook.prompt_substituted"]
    assert substituted, events
    payload = substituted[0]["payload"]
    assert payload["suppressed"] is True
    assert payload["original_chars"] == len(f"请围绕 {MARKER} 写一份说明")
    assert payload["replacement_chars"] == len(REPLACEMENT)
    # 正文不落事件账（事件对项目成员可见、还被全文索引），只落可核对的指纹。
    assert MARKER not in json.dumps(payload, ensure_ascii=False)
    assert len(payload["original_sha"]) == 12


async def test_要求抑制却没给替代正文时原文照送并说明(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command = _script(
        tmp_path,
        "suppress_empty.py",
        "sys.stdout.write(json.dumps({'hookSpecificOutput': {'suppressOriginalPrompt': True}}))\n",
    )
    _hooks(monkeypatch, command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["给出结论"], goal=f"请围绕 {MARKER} 写一份说明")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # 没有替代正文就抑制 = 给模型一个空目标，比泄露原文更糟：宁可照送并留下说明。
    assert any(MARKER in text for text in _all_texts(env.mock))
    events = await _events(env, task_id)
    substituted = [item for item in events if item["type"] == "hook.prompt_substituted"]
    assert substituted and substituted[0]["payload"]["suppressed"] is False
    assert substituted[0]["payload"].get("skipped_reason")


async def test_钩子拦下提交时一次模型调用都不发起(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command = _script(tmp_path, "block.py", "sys.stderr.write('这类请求一律不进模型')\nsys.exit(2)\n")
    _hooks(monkeypatch, command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["给出结论"], goal=f"请围绕 {MARKER} 写一份说明")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert env.mock.requests == [], "提交被拦下之后仍然去调了模型"

    task = (await env.client.get(f"{API}/tasks/{task_id}")).json()
    assert task["status"] == "failed", task
    assert "这类请求一律不进模型" in (task.get("failure_reason") or "")

    events = await _events(env, task_id)
    types = [item["type"] for item in events]
    blocked = [item for item in events if item["type"] == "hook.prompt_blocked"]
    assert blocked, events
    assert blocked[0]["payload"]["original_chars"] == len(f"请围绕 {MARKER} 写一份说明")
    # 拦在提交那一刻：这条任务从未"开始跑"，也不该有任何一步。
    assert "task.started" not in types, types
    assert "task.failed" in types
    assert task.get("steps_used", 0) == 0


async def test_钩子跑坏了不拦提交也不冒充拦下(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    """fail-open 的边界：退出码 1 / 起不来 = 坏钩子，不能把它读成"它否决了这次提交"。

    只有明确说"不"（deny 或退出码 2）才拦。这条与上一条是一对：把两者混起来的实现
    会让一个坏掉的钩子静默堵死所有任务。
    """
    command = _script(tmp_path, "crash.py", "sys.stderr.write('这个钩子挂了')\nsys.exit(1)\n")
    _hooks(monkeypatch, command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["能读到 README"], goal=f"请先把 {MARKER} 那份文件读一遍")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert env.mock.requests, "坏钩子把提交拦下了"
    assert any(MARKER in text for text in _all_texts(env.mock))
    events = await _events(env, task_id)
    assert [item for item in events if item["type"] == "hook.prompt_blocked"] == []


async def test_没配钩子时提交链路一个字节都不变(env, workspace: Path):  # noqa: F811
    """负基线：本批的所有新行为都必须由钩子触发，没钩子时不许冒出新的读数。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["能读到 README"], goal=f"请先把 {MARKER} 那份文件读一遍")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert any(MARKER in text for text in _all_texts(env.mock))
    events = await _events(env, task_id)
    assert [item for item in events if item["type"].startswith("hook.prompt")] == []


async def test_多条钩子时按配置顺序拼接且一条要求抑制就抑制(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    first = _script(tmp_path, "first.py", _emit("第一段补充。"))
    second = _script(tmp_path, "second.py", _emit("第二段补充。", suppress=True))
    _hooks(monkeypatch, first, second)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["给出结论"], goal=f"请围绕 {MARKER} 写一份说明")
    env.mock.reset(_steps())

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    texts = _all_texts(env.mock)
    joined = [text for text in texts if "第一段补充。" in text and "第二段补充。" in text]
    assert joined, texts
    assert joined[0].index("第一段补充。") < joined[0].index("第二段补充。"), "替代正文的顺序应当是配置顺序"
    assert all(MARKER not in text for text in texts)


async def test_团队入口的成员任务也过提交钩子(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    """成员任务的 goal 直接来自用户发言（services/team.py），它是独立于 /tasks 的第二条入口。"""
    from tests.test_team import make_group, wait_round

    command = _script(tmp_path, "team_suppress.py", _emit(REPLACEMENT, suppress=True))
    _hooks(monkeypatch, command)
    project_id, _, group_id = await make_group(env, workspace, members=("后端",))

    resp = await env.client.post(
        f"{API}/team/groups/{group_id}/messages",
        json={"content": f"请把 {MARKER} 的事情办一下"},
    )
    assert resp.status_code in (200, 201), resp.text
    await wait_round(env, group_id, ("completed", "failed", "waiting_approval"), timeout=40.0)

    async with get_session_factory()() as session:
        rows = (await session.execute(select(Task).where(Task.project_id == project_id))).scalars().all()
    assert rows, "这一轮没有产生任何成员/主理人任务"
    hits: list[dict] = []
    for task in rows:
        for item in await _events(env, task.id):
            if item["type"] == "hook.prompt_substituted":
                hits.append(item["payload"])
    assert hits, "团队入口没有触发提交钩子"
    assert all(payload["suppressed"] is True for payload in hits)
