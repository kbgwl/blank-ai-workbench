"""#304 SessionStart 钩子输出四字段：sessionTitle / initialUserMessage / watchPaths / reloadSkills。

上游 ``Sn()`` 校验函数定义了 SessionStart 钩子输出的全部字段，本批移植其中四个：

* ``sessionTitle`` (string) → 更新会话标题（``Thread.title``）
* ``initialUserMessage`` (string) → 作为会话第一条 user 消息注入模型上下文
* ``watchPaths`` (string[]) → 写入 ``.blank/context/watch.json``
* ``reloadSkills`` (bool, 只认 ``true``) → 重新发现 Skill 目录并刷新会话目录

判据分五层，每条都在「拿掉对应实现就会红」的位置上：

1. 纯解析：``parse_hook_output`` 认四字段，类型校验对齐上游 ``Sn()``；
2. HookResult 组装：``_finish`` 把四字段带进 ``HookResult``；
3. ``as_dict`` 序列化：审计视图可见；
4. ``initial_messages``：``initial_user_message`` 作为独立 user 消息插在 system 之后；
5. 端到端：runtime 真的把四字段落到各自消费端。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.models.thread import Thread
from blank_api.services.agent.graph import initial_messages
from blank_api.services.hooks import (
    HookResult,
    HookRunner,
    HookSpec,
    parse_hook_output,
)

from tests.test_agent_v04 import (  # noqa: F401  (复用 HTTP 夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_hook_events_v52 import _hooks_env


# --------------------------------------------------------------------------- #
# 1. 纯解析：parse_hook_output 认四字段，类型校验对齐上游 Sn()
# --------------------------------------------------------------------------- #

def test_parse_session_title_from_nested_shape():
    parsed = parse_hook_output(
        json.dumps({"hookSpecificOutput": {"sessionTitle": "钩子标题"}})
    )
    assert parsed["session_title"] == "钩子标题"


def test_parse_session_title_from_top_level():
    parsed = parse_hook_output(json.dumps({"sessionTitle": "顶层标题"}))
    assert parsed["session_title"] == "顶层标题"


def test_parse_session_title_rejects_non_string():
    # 上游 Sn() 对 sessionTitle 的类型校验是「非 string 即错型」。
    parsed = parse_hook_output(json.dumps({"sessionTitle": 123}))
    assert "session_title" not in parsed


def test_parse_initial_user_message():
    parsed = parse_hook_output(
        json.dumps({"hookSpecificOutput": {"initialUserMessage": "初始消息"}})
    )
    assert parsed["initial_user_message"] == "初始消息"


def test_parse_initial_user_message_rejects_non_string():
    parsed = parse_hook_output(json.dumps({"initialUserMessage": 456}))
    assert "initial_user_message" not in parsed


def test_parse_watch_paths_from_array():
    parsed = parse_hook_output(
        json.dumps({"hookSpecificOutput": {"watchPaths": ["/a", "/b"]}})
    )
    assert parsed["watch_paths"] == ["/a", "/b"]


def test_parse_watch_paths_rejects_non_list():
    parsed = parse_hook_output(json.dumps({"watchPaths": "/not-a-list"}))
    assert "watch_paths" not in parsed


def test_parse_watch_paths_rejects_non_string_items():
    # 上游 i1t() 要求数组元素全是字符串；混入非字符串即错型。
    parsed = parse_hook_output(json.dumps({"watchPaths": ["/ok", 42]}))
    assert "watch_paths" not in parsed


def test_parse_reload_skills_true():
    parsed = parse_hook_output(
        json.dumps({"hookSpecificOutput": {"reloadSkills": True}})
    )
    assert parsed["reload_skills"] is True


def test_parse_reload_skills_rejects_non_true():
    # 上游对 reloadSkills 的校验是「非 true 即错型」：false、字符串 "true"、1 都不认。
    for bad in (False, "true", 1, None):
        parsed = parse_hook_output(json.dumps({"reloadSkills": bad}))
        assert "reload_skills" not in parsed, bad


def test_parse_all_four_fields_together():
    parsed = parse_hook_output(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "sessionTitle": "标题",
                    "initialUserMessage": "消息",
                    "watchPaths": ["/x"],
                    "reloadSkills": True,
                }
            }
        )
    )
    assert parsed["session_title"] == "标题"
    assert parsed["initial_user_message"] == "消息"
    assert parsed["watch_paths"] == ["/x"]
    assert parsed["reload_skills"] is True


# --------------------------------------------------------------------------- #
# 2. HookResult 组装：_finish 把四字段带进 HookResult
# --------------------------------------------------------------------------- #

async def test_hook_result_carries_all_four_fields(tmp_path: Path):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'sessionTitle':'t','initialUserMessage':'m',"
        "'watchPaths':['/a'],'reloadSkills':True}))\n",
        encoding="utf-8",
    )
    runner = HookRunner(
        {"session_start": [HookSpec(command=f'"{sys.executable}" "{script}"')]}
    )
    results = await runner.run("session_start", "startup", {})
    assert len(results) == 1
    item = results[0]
    assert item.session_title == "t"
    assert item.initial_user_message == "m"
    assert item.watch_paths == ["/a"]
    assert item.reload_skills is True


async def test_hook_result_defaults_when_fields_absent(tmp_path: Path):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'decision':'allow'}))\n",
        encoding="utf-8",
    )
    runner = HookRunner(
        {"session_start": [HookSpec(command=f'"{sys.executable}" "{script}"')]}
    )
    results = await runner.run("session_start", "startup", {})
    assert results[0].session_title == ""
    assert results[0].initial_user_message == ""
    assert results[0].watch_paths == []
    assert results[0].reload_skills is False


# --------------------------------------------------------------------------- #
# 3. as_dict 序列化：审计视图可见
# --------------------------------------------------------------------------- #

def test_as_dict_serializes_four_fields():
    item = HookResult(
        event="session_start",
        command="hook.py",
        status="ok",
        session_title="钩子标题",
        initial_user_message="初始消息文本",
        watch_paths=["/a", "/b"],
        reload_skills=True,
    )
    body = item.as_dict()
    assert body["session_title"] == "钩子标题"
    assert body["initial_user_message_chars"] == len("初始消息文本")
    assert body["watch_paths_count"] == 2
    assert body["reload_skills"] is True


def test_as_dict_omits_empty_fields():
    item = HookResult(event="session_start", command="hook.py", status="ok")
    body = item.as_dict()
    assert "session_title" not in body
    assert "initial_user_message_chars" not in body
    assert "watch_paths_count" not in body
    assert "reload_skills" not in body


# --------------------------------------------------------------------------- #
# 4. initial_messages：initial_user_message 作为独立 user 消息插在 system 之后
# --------------------------------------------------------------------------- #

def test_initial_messages_prepends_initial_user_message():
    messages = initial_messages(
        goal="任务目标",
        criteria=[],
        initial_user_message="钩子初始消息",
        run_id="test-run",
    )
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "钩子初始消息" in messages[1]["content"]
    # 目标仍然在 initial_user_message 之后
    assert messages[2]["role"] == "user"
    assert "任务目标" in messages[2]["content"]


def test_initial_messages_without_initial_user_message_has_no_extra_message():
    messages = initial_messages(goal="任务目标", criteria=[], run_id="test-run")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "任务目标" in messages[1]["content"]


# --------------------------------------------------------------------------- #
# 5. 端到端：runtime 真的把四字段落到各自消费端
# --------------------------------------------------------------------------- #

def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


async def test_session_start_hook_updates_thread_title(
    env, workspace: Path, tmp_path: Path, monkeypatch
):
    command = _hook_script(
        tmp_path,
        "title",
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'sessionTitle': '钩子标题'}))\n",
    )
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "完成"}])

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text

    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        thread = (
            await session.get(Thread, task.thread_id)
            if task is not None and task.thread_id
            else None
        )
    assert thread is not None
    assert thread.title == "钩子标题"


async def test_session_start_hook_prepends_initial_user_message_to_model(
    env, workspace: Path, tmp_path: Path, monkeypatch
):
    command = _hook_script(
        tmp_path,
        "init",
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'initialUserMessage': '钩子初始消息'}))\n",
    )
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "完成"}])

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text

    messages = env.mock.requests[0]["messages"]
    # 第二条消息（index 1）是 initial_user_message，第三条（index 2）是目标
    roles = [item["role"] for item in messages]
    assert "user" in roles
    init_msg = next(
        item for item in messages
        if item["role"] == "user" and "钩子初始消息" in str(item.get("content") or "")
    )
    assert init_msg is not None
    # initial_user_message 必须在目标之前
    goal_idx = next(
        i for i, item in enumerate(messages)
        if item["role"] == "user" and "读取 main.py" in str(item.get("content") or "")
    )
    init_idx = next(
        i for i, item in enumerate(messages)
        if item["role"] == "user" and "钩子初始消息" in str(item.get("content") or "")
    )
    assert init_idx < goal_idx


async def test_session_start_hook_writes_watch_json(
    env, workspace: Path, tmp_path: Path, monkeypatch
):
    command = _hook_script(
        tmp_path,
        "watch",
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'watchPaths': ['/watch/a', '/watch/b']}))\n",
    )
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "完成"}])

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text

    watch_file = workspace / ".blank" / "context" / "watch.json"
    assert watch_file.exists(), "watch.json 没有落盘"
    data = json.loads(watch_file.read_text(encoding="utf-8"))
    assert data == ["/watch/a", "/watch/b"]


async def test_session_start_hook_reloads_skills(
    env, workspace: Path, tmp_path: Path, monkeypatch
):
    # 钩子先在磁盘上装一个 Skill，再要求 reloadSkills。初始 discover_skills 跑在
    # 钩子之前，所以这个 Skill 不在初始目录里；只有 reload 真正重扫了目录才会进
    # 系统提示。拿掉 reload 实现，系统提示里就找不到 hook-skill，判据变红。
    skill_dir = workspace / ".blank" / "skills" / "hook-skill"
    command = _hook_script(
        tmp_path,
        "reload",
        "import json, sys, os\n"
        f"skill_dir = {str(skill_dir)!r}\n"
        "os.makedirs(skill_dir, exist_ok=True)\n"
        "with open(os.path.join(skill_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:\n"
        "    f.write('---\\nname: hook-skill\\ndescription: 钩子装的技能\\n---\\n\\n正文\\n')\n"
        "sys.stdout.write(json.dumps({'reloadSkills': True}))\n",
    )
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "完成"}])

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text

    system_text = str(env.mock.requests[0]["messages"][0].get("content") or "")
    assert "hook-skill" in system_text, "reloadSkills 后新装的 Skill 没有进系统提示"


async def test_session_start_hook_no_four_fields_no_side_effects(
    env, workspace: Path, tmp_path: Path, monkeypatch
):
    command = _hook_script(
        tmp_path,
        "noop",
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'decision': 'allow'}))\n",
    )
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "完成"}])

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text

    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        thread = (
            await session.get(Thread, task.thread_id)
            if task is not None and task.thread_id
            else None
        )
    assert thread.title != "钩子标题"
    watch_file = workspace / ".blank" / "context" / "watch.json"
    assert not watch_file.exists()
