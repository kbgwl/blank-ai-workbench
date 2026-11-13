"""AE 批：StopFailure 钩子事件（对齐上游「API 错误终结回合」的那一发）。

上游语义（dump 逐字）：``Fires instead of Stop when an API error (rate limit, auth
failure, etc.) ended the turn. Fire-and-forget — hook output and exit codes are
ignored.`` matcher 的比对对象是载荷里的 ``error`` 分类，取值十三个。

覆盖三件事，每件都钉在"能失败"的判据上：

* **事件面**：注册、PascalCase 别名、**不进**阻断集合（fire-and-forget 的落地）、
  分类映射表（本机 ModelError 的 code/状态码 → 上游分类）。
* **接线**：API 错误终结回合时发 StopFailure 且**替代** Stop（同一轮不跑 stop 钩子）、
  载荷带 error 分类与 error_details、有模型回复时带 last_assistant_message、
  matcher 按分类分流、退出码 2 不改失败结论也不注入、续跑路径同样发。
* **端到端**：上面这些在真图、真子进程钩子里各走一遍（不是只测函数）；
  没配钩子时这条新路径完全惰性。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from blank_api.services.hooks import (
    BLOCKING_EVENTS,
    EVENT_ALIASES,
    HOOK_EVENTS,
    STOP_FAILURE_ERROR_CATEGORIES,
    stop_failure_error_category,
)

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    ENV_PATCH,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


# --------------------------------------------------------------------------- #
# 1. 事件面
# --------------------------------------------------------------------------- #
def test_stop_failure_registered_with_alias_and_not_blocking():
    assert "stop_failure" in HOOK_EVENTS
    assert EVENT_ALIASES["StopFailure"] == "stop_failure"
    # fire-and-forget：上游明说钩子的输出与退出码一律忽略，所以它不能进阻断集合——
    # 进了就会被迫同步等待结果，"忽略输出"这条语义也就无从谈起。
    assert "stop_failure" not in BLOCKING_EVENTS
    assert set(EVENT_ALIASES.values()) <= set(HOOK_EVENTS)


def test_error_category_table():
    """本机 ModelError → 上游 StopFailure 的 error 分类。

    表驱动：每一格都断言**具体值**，不是"返回了某个合法分类"——后者把映射错
    （比如把 401 记成 server_error）当成通过。
    """
    cases = [
        # 状态码优先：供应商给出的那一层事实，code 猜不出来。
        ("model_http_error", 429, "rate_limit"),
        ("model_http_error", 402, "billing_error"),
        ("model_http_error", 401, "authentication_failed"),
        ("model_http_error", 403, "authentication_failed"),
        ("model_http_error", 404, "model_not_found"),
        ("model_http_error", 400, "invalid_request"),
        ("model_http_error", 503, "overloaded"),
        ("model_http_error", 500, "server_error"),
        # 状态码不在表里时退回 code。
        ("model_timeout", 0, "server_error"),
        ("model_unreachable", 0, "server_error"),
        ("model_stream_error", 0, "server_error"),
        ("model_stream_empty", 0, "server_error"),
        ("model_output_not_json", 0, "invalid_request"),
        ("model_tools_unsupported", 0, "invalid_request"),
        ("circuit_breaker_open", 0, "overloaded"),
        ("all_models_failed", 0, "unknown"),
        # 都不认：宁可 unknown，也不要编一个上游没有的分类让钩子 matcher 永远不中。
        (None, 0, "unknown"),
        ("", 0, "unknown"),
        ("not_a_real_code", 0, "unknown"),
        (None, 418, "unknown"),
        # 状态码压过 code：带 401 的传输层失败必须记成认证问题。
        ("model_timeout", 401, "authentication_failed"),
    ]
    for code, status, expected in cases:
        got = stop_failure_error_category(code, status)
        assert got == expected, f"stop_failure_error_category({code!r}, {status}) = {got!r}"
        assert got in STOP_FAILURE_ERROR_CATEGORIES

    # 分类表本身不许偏离上游：多一个少一个都会让按上游词汇写的 matcher 静默不中。
    assert STOP_FAILURE_ERROR_CATEGORIES == (
        "rate_limit",
        "overloaded",
        "authentication_failed",
        "oauth_org_not_allowed",
        "account_on_hold",
        "verification_required",
        "billing_error",
        "invalid_request",
        "model_not_found",
        "server_error",
        "max_output_tokens",
        "cloud_credential_error",
        "unknown",
    )


# --------------------------------------------------------------------------- #
# 端到端装置（沿用 AB 批的真子进程钩子写法）
# --------------------------------------------------------------------------- #
def _log_hook(tmp_path: Path, name: str, body: str = "") -> tuple[str, Path]:
    """把每次调用的 stdin 追加进 JSONL；body 可在写完之后追加输出。"""
    log = tmp_path / f"{name}.jsonl"
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "payload = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False) + '\\n')\n"
        + body,
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"', log


def _hooks_env(monkeypatch, **events: str) -> None:
    monkeypatch.setenv(
        "BLANK_HOOKS",
        json.dumps({event: [{"command": command, "timeout": 20}] for event, command in events.items()}),
    )


def _hooks_env_spec(monkeypatch, events: dict[str, list[dict]]) -> None:
    """带 matcher 的形状：一个事件下挂多条钩子，各自按分类分流。"""
    monkeypatch.setenv("BLANK_HOOKS", json.dumps(events))


def _lines(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _task(env, task_id: str) -> dict:
    return (await env.client.get(f"{API}/tasks/{task_id}")).json()


async def _events(env, task_id: str) -> list[dict]:
    return (await env.client.get(f"{API}/tasks/{task_id}/events")).json()


def _completed(events: list[dict], event: str) -> list[dict]:
    return [
        item
        for item in events
        if item["type"] == "hook.completed" and item["payload"].get("event") == event
    ]


async def _waiting_task(env, workspace: Path):
    """跑到 waiting_approval：续跑路径的前置（与审批批的夹具同形）。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["写入 .env 需人工审批"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写入 .env"},
            {"content": "写入 .env", "tool_calls": [call("apply_patch", {"path": ".env", "patch": ENV_PATCH})]},
            {"content": ".env 已写入"},
            {"content": "已完成 .env 写入"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"
    return task_id, provider_id


# --------------------------------------------------------------------------- #
# 2. 端到端：API 错误终结回合
# --------------------------------------------------------------------------- #
async def test_api_error_fires_stop_failure_instead_of_stop(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command, log = _log_hook(tmp_path, "sf")
    _hooks_env(monkeypatch, stop=command, stop_failure=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    env.mock.fail_status = 401

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    rows = _lines(log)
    fired = [row for row in rows if row["event"] == "stop_failure"]
    # 上游是"替代 Stop"：同一轮既发 Stop 又发 StopFailure，钩子就会看到两个互相
    # 矛盾的信号（一个说"收工"，一个说"模型挂了"）。
    assert len(fired) == 1, rows
    assert not [row for row in rows if row["event"] == "stop"], rows

    row = fired[0]
    assert row["hook_event_name"] == "stop_failure"
    assert row["target"] == "authentication_failed"
    assert row["error"] == "authentication_failed"
    assert "401" in row["error_details"]

    task = await _task(env, task_id)
    assert task["status"] == "failed"
    assert "401" in task["failure_reason"]

    completed = _completed(await _events(env, task_id), "stop_failure")
    assert completed, "StopFailure 跑完了却没留审计"


async def test_matcher_dispatches_on_the_error_category(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    """matcher 的比对对象是 error 分类：配错分类的钩子这一轮不该被叫醒。"""
    auth_command, auth_log = _log_hook(tmp_path, "auth")
    rate_command, rate_log = _log_hook(tmp_path, "rate")
    _hooks_env_spec(
        monkeypatch,
        {
            "stop_failure": [
                {"command": auth_command, "match": "authentication_failed", "timeout": 20},
                {"command": rate_command, "match": "rate_limit", "timeout": 20},
            ]
        },
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    env.mock.fail_status = 401

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    assert len(_lines(auth_log)) == 1, "按 authentication_failed 配的钩子没被叫到"
    # 分类不匹配的那条压根不该起进程：文件不存在就是"没被叫到"的硬证据。
    assert not rate_log.exists(), "分类不匹配的钩子被叫到了"
    assert _lines(auth_log)[0]["target"] == "authentication_failed"


async def test_stop_failure_payload_carries_last_assistant_message(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    """先留一条模型回复、再让下一次调用失败：钩子要能看见"失败发生在哪一问"。"""
    command, log = _log_hook(tmp_path, "last")
    _hooks_env(monkeypatch, stop_failure=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "先看一步", "tool_calls": [call("read_file", {"path": "main.py"})]}])
    env.mock.fail_status = 401
    env.mock.fail_after = 1

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    rows = _lines(log)
    assert len(rows) == 1, rows
    assert rows[0]["error"] == "authentication_failed"
    assert "先看一步" in rows[0]["last_assistant_message"], rows[0]


async def test_stop_failure_is_fire_and_forget(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    """钩子的输出与退出码一律忽略：只留审计，不改结论、不注入、不唤醒模型。"""
    body = (
        "sys.stdout.write(json.dumps({'decision': 'deny', 'reason': '钩子说不行'}))\n"
        "sys.exit(2)\n"
    )
    command, log = _log_hook(tmp_path, "forget", body)
    _hooks_env(monkeypatch, stop_failure=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    env.mock.fail_status = 401

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    task = await _task(env, task_id)
    # 退出码 2 + deny 都不许把失败结论改成别的，也不许再叫模型跑一轮。
    assert task["status"] == "failed", task
    assert "钩子说不行" not in (task["failure_reason"] or "")
    assert len(env.mock.requests) == 1, "钩子说 deny 之后模型又被叫了一次"

    serialised = json.dumps(env.mock.requests, ensure_ascii=False)
    assert "钩子说不行" not in serialised, "钩子的输出被注入进模型上下文了"

    events = await _events(env, task_id)
    completed = _completed(events, "stop_failure")
    assert completed, events
    # 忽略的是"作用"，不是"记录"：退出码 2 必须留在审计里，否则"跑过且被忽略了"
    # 这件事在时间线上根本看不出来。
    assert completed[0]["payload"]["results"][0]["exit_code"] == 2, completed
    assert not [item for item in events if item["type"] == "hook.turn_blocked"], events


async def test_resume_path_fires_stop_failure_when_the_model_fails_again(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    """续跑后模型又挂：同样按「API 错误终结回合」发，而不是只发 Stop。"""
    command, log = _log_hook(tmp_path, "resume")
    _hooks_env(monkeypatch, stop=command, stop_failure=command)
    task_id, provider_id = await _waiting_task(env, workspace)
    env.mock.fail_status = 401

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval", json={"approve": True, "provider_id": provider_id}
    )
    assert resp.status_code == 502, resp.text

    rows = _lines(log)
    fired = [row for row in rows if row["event"] == "stop_failure"]
    assert len(fired) == 1, rows
    assert not [row for row in rows if row["event"] == "stop"], rows
    assert fired[0]["error"] == "authentication_failed"
    # 上一轮的模型回复要带得出来：恢复出来的检查点里就有它。
    assert fired[0]["last_assistant_message"], fired[0]

    task = await _task(env, task_id)
    assert task["status"] == "failed", task


async def test_without_hooks_the_new_path_changes_nothing(env, workspace: Path):  # noqa: F811
    """没配钩子时这条新路径必须完全惰性：不发事件、不改结论。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    env.mock.fail_status = 401

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    events = await _events(env, task_id)
    assert not [item for item in events if item["type"].startswith("hook.")], events

    task = await _task(env, task_id)
    assert task["status"] == "failed"
    assert "401" in task["failure_reason"]
