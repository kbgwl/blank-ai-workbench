"""#321 工具钩子载荷补齐上游字段（父会话与子代理两条路径）。

上游语义（`grep -boF` 现场复核）：

* ``PreToolUse`` = ``{hook_event_name, tool_name, tool_input, tool_use_id}``
  （24337589）；
* ``PostToolUse`` 另带 ``tool_response`` / ``tool_use_id`` / ``duration_ms``
  （24337830 / 24337928），其中 ``duration_ms`` 的描述是
  *"Tool execution time in milliseconds. Excludes permission-prompt and hook time."*；
* ``PostToolUseFailure`` 带 ``error`` / ``is_interrupt`` / ``duration_ms``
  （24338072 / 24338196）；
* ``PostToolBatch`` 的 ``tool_calls`` 每项带 ``tool_name`` / ``tool_input`` /
  ``tool_use_id`` / ``tool_response``（24338196 / 24338432）。

本机此前给钩子的是 ``tool`` / ``arguments``；照着 Claude 文档写钩子的人会拿到
``undefined``。本批把上游字段名与历史字段名一起下发（旧钩子不静默失效），并补
``tool_response`` / ``duration_ms`` / ``is_interrupt``：

* ``tool_response`` 是回执投影 ``{ok, status, output, data}``，不含账本字段
  （``tool_call_id`` / ``permission`` / ``sandbox_backend``）——钩子是观察者，
  不该顺走治理内部数据；
* ``duration_ms`` 沿用网关回执（执行耗时；审批与钩子本身都在它之外）；
* ``is_interrupt`` 在本机恒为 ``False``：工具失败没有"用户中断"这一维度。

判据同时钉真图（父会话 + 真子进程钩子）与子代理循环两条路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from blank_api.services.tools.base import fail

from tests.test_agent_hooks_v316 import _Rec, _runner, _spec
from tests.test_agent_v04 import (  # noqa: F401  (env 等夹具)
    API,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_hook_events_v52 import _hooks_env, _lines, _log_hook
from tests.test_subagent_tool_hooks_v320 import _call, _probe, _run_child, _script


def _plan_and_summary(tool_call: dict | None) -> list[dict]:
    """父会话的固定三段：计划、工具调用、收尾。"""
    script = [{"content": "计划：\n1. 看一眼"}]
    if tool_call is not None:
        script.append({"content": "执行", "tool_calls": [tool_call]})
    script.append({"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)})
    return script


# --------------------------------------------------------------------------- #
# 1. 父会话：真图 + 真子进程钩子
# --------------------------------------------------------------------------- #

async def test_parent_tool_hooks_carry_upstream_fields(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    pre_command, pre_log = _log_hook(tmp_path, "parent-pre")
    post_command, post_log = _log_hook(tmp_path, "parent-post")
    batch_command, batch_log = _log_hook(tmp_path, "parent-batch")
    _hooks_env(
        monkeypatch,
        pre_tool_use=pre_command,
        post_tool_use=post_command,
        post_tool_batch=batch_command,
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        _plan_and_summary(
            {
                "id": "call-read",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "main.py"})},
            }
        )
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    pre = _lines(pre_log)[0]
    assert pre["tool_name"] == "read_file"
    assert pre["tool_input"] == {"path": "main.py"}
    assert pre["tool_use_id"] == "call-read"
    # 历史字段名继续下发，老钩子不静默失效。
    assert pre["tool"] == "read_file"
    assert pre["arguments"] == {"path": "main.py"}

    post = _lines(post_log)[0]
    assert post["tool_name"] == "read_file"
    assert post["tool_input"] == {"path": "main.py"}
    assert post["tool_use_id"] == "call-read"
    response = post["tool_response"]
    assert response["ok"] is True
    assert response["status"] == "ok"
    assert response["output"]
    assert isinstance(post["duration_ms"], int) and post["duration_ms"] >= 0
    # 回执投影不带账本字段：钩子拿不到 tool_call_id / permission 这类治理内部数据。
    assert "tool_call_id" not in response
    assert "permission" not in response

    batch = _lines(batch_log)[0]["tool_calls"][0]
    assert batch["tool_name"] == "read_file"
    assert batch["tool_input"] == {"path": "main.py"}
    assert batch["tool_use_id"] == "call-read"
    assert batch["tool_response"]["ok"] is True


async def test_parent_failure_hook_carries_is_interrupt_and_duration(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    fail_command, fail_log = _log_hook(tmp_path, "parent-fail")
    _hooks_env(monkeypatch, post_tool_use_failure=fail_command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取不存在的文件"])
    env.mock.reset(
        _plan_and_summary(
            {
                "id": "call-missing",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "no-such-file.txt"}),
                },
            }
        )
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    row = _lines(fail_log)[0]
    assert row["hook_event_name"] == "post_tool_use_failure"
    assert row["tool_name"] == "read_file"
    assert row["tool_input"] == {"path": "no-such-file.txt"}
    assert row["error"]
    assert row["is_interrupt"] is False
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0
    assert row["tool_response"]["ok"] is False


# --------------------------------------------------------------------------- #
# 2. 子代理：工具循环里同样两套字段
# --------------------------------------------------------------------------- #

async def test_child_tool_hooks_carry_upstream_fields(tmp_path: Path):
    pre_command, pre_log = _log_hook(tmp_path, "child-pre")
    post_command, post_log = _log_hook(tmp_path, "child-post")
    batch_command, batch_log = _log_hook(tmp_path, "child-batch")

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks={
            "pre_tool_use": (_spec(pre_command),),
            "post_tool_use": (_spec(post_command),),
            "post_tool_batch": (_spec(batch_command),),
        },
        adapter=_script(_call("probe", {"path": "a.txt"})),
        tools=[
            _probe(
                schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
            )
        ],
        recorder=_Rec(),
    )
    assert result.ok, result.error

    pre = _lines(pre_log)[0]
    assert pre["tool_name"] == "probe"
    assert pre["tool_input"] == {"path": "a.txt"}
    assert pre["tool_use_id"] == "call-probe"
    assert pre["tool"] == "probe" and pre["arguments"] == {"path": "a.txt"}

    post = _lines(post_log)[0]
    assert post["tool_name"] == "probe"
    assert post["tool_input"] == {"path": "a.txt"}
    assert post["tool_use_id"] == "call-probe"
    assert post["tool_response"]["ok"] is True
    assert "pong" in post["tool_response"]["output"]
    assert isinstance(post["duration_ms"], int) and post["duration_ms"] >= 0
    assert "tool_call_id" not in post["tool_response"]

    batch = _lines(batch_log)[0]["tool_calls"][0]
    assert batch["tool_name"] == "probe"
    assert batch["tool_input"] == {"path": "a.txt"}
    assert batch["tool_use_id"] == "call-probe"
    assert batch["tool_response"]["ok"] is True


async def test_child_failure_hook_carries_is_interrupt_and_duration(tmp_path: Path):
    fail_command, fail_log = _log_hook(tmp_path, "child-fail")

    async def _boom(_context, _arguments):
        return fail("error", "磁盘满了")

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks={"post_tool_use_failure": (_spec(fail_command),)},
        adapter=_script(_call("probe", {})),
        tools=[_probe(handler=_boom)],
        recorder=_Rec(),
    )
    assert result.ok, result.error

    row = _lines(fail_log)[0]
    assert row["hook_event_name"] == "post_tool_use_failure"
    assert row["tool_name"] == "probe"
    assert row["tool_input"] == {}
    assert "磁盘满了" in row["error"]
    assert row["is_interrupt"] is False
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0
    assert row["tool_response"]["ok"] is False
