"""AC 批：settings.json 的 ``hooks`` 配置面 + matcher 语义对齐上游。

三件事，每件都钉在"能失败"的判据上：

* **形状**：Claude 的 ``{事件: [{matcher, hooks:[{type,command,timeout,async}]}]}``
  与我们的扁平 ``{event:[{match,command,...}]}`` 都收；PascalCase 事件名折成内部名；
  未支持的事件/类型、坏正则、空命令告警后跳过；settings 与 BLANK_HOOKS 合并去重。
* **matcher 语义**：空/``*``/``.*`` 全中；纯名单按 ``|``/`,` 精确比对（**不是子串**）；
  其余按正则搜；非法正则不匹配。旧实现的 ``match in target`` 在这组判据上必红。
* **接线**：层级只认用户/管理层（项目/本机写了入 ignored）；``disableAllHooks``
  两种写法都认且受保护；运行时真子进程钩子跑起来并落 ``hooks.loaded``；
  总开关为真时钩子不执行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blank_api.services.hooks import (
    EVENT_ALIASES,
    HOOK_EVENTS,
    HookRunner,
    matcher_matches,
)
from blank_api.services.settings import DEFAULTS, normalize
from blank_api.services.settings_layers import (
    LAYER_PROJECT,
    load_layered_settings,
)

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


@pytest.fixture(autouse=True)
def _isolated_hook_env(monkeypatch: pytest.MonkeyPatch):
    """本机环境变量不能给判据加一层：每条用例自己决定要不要 env/管理层。"""
    monkeypatch.delenv("BLANK_HOOKS", raising=False)
    monkeypatch.delenv("BLANK_MANAGED_SETTINGS_FILE", raising=False)


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _layered(root: Path | str | None, tmp_path: Path, *, user: dict | None = None, managed: dict | None = None):
    user_path = tmp_path / "user-settings.json"
    if user is not None:
        _write(user_path, user)
    env_map: dict[str, str] = {}
    if managed is not None:
        env_map["BLANK_MANAGED_SETTINGS_FILE"] = str(_write(tmp_path / "managed-settings.json", managed))
    return load_layered_settings(root, user_path=user_path, env=env_map)


def _ignored(result) -> list[dict]:
    return [item for layer in result.layers for item in layer.ignored]


# --------------------------------------------------------------------------- #
# 1. 形状解析：Claude 嵌套 / 扁平 / 事件名映射 / 坏条目
# --------------------------------------------------------------------------- #
CLAUDE_HOOKS = {
    "PreToolUse": [
        {
            "matcher": "Write|Edit",
            "hooks": [
                {"type": "command", "command": "node guard.mjs", "timeout": 3},
                {"type": "command", "command": "node audit.mjs", "async": True},
            ],
        }
    ]
}


def test_claude_nested_shape_is_parsed():
    runner = HookRunner.from_env(settings=CLAUDE_HOOKS)
    assert runner.enabled is True
    specs = runner.hooks["pre_tool_use"]
    assert [spec.command for spec in specs] == ["node guard.mjs", "node audit.mjs"]
    assert {spec.match for spec in specs} == {"Write|Edit"}
    assert [spec.timeout for spec in specs] == [3.0, 10.0]
    assert specs[1].async_run is True
    # 来源要能分清：审计里"这条钩子从哪来"不能靠猜。
    assert {spec.source for spec in specs} == {"settings"}
    assert runner.describe()["events"] == {"pre_tool_use": 2}
    assert runner.describe()["sources"] == ["settings"]


def test_flat_shape_is_still_accepted_from_settings():
    runner = HookRunner.from_env(
        settings={"pre_tool_use": [{"command": "x", "match": "Read", "timeout": 5}]}
    )
    spec = runner.hooks["pre_tool_use"][0]
    assert (spec.command, spec.match, spec.timeout) == ("x", "Read", 5.0)


def test_event_alias_table_covers_only_known_events():
    assert set(EVENT_ALIASES.values()) <= set(HOOK_EVENTS)
    # 别名键必须是 PascalCase 且不重复（写成同一个键两次会静默丢掉一条）。
    assert len(EVENT_ALIASES) == len(set(EVENT_ALIASES))
    assert all(key[0].isupper() for key in EVENT_ALIASES)


def test_unknown_event_and_unknown_type_warn_and_skip():
    runner = HookRunner.from_env(
        settings={
            # 上游有、我们还没移植的事件：必须告警，不能静默当没写
            "SomeFutureEvent": [{"hooks": [{"type": "command", "command": "x"}]}],
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "prompt", "prompt": "你是一个守卫"},
                        {"type": "command", "command": "ok.sh"},
                        {"type": "command"},
                        {"type": "telepathy", "command": "x"},
                    ],
                }
            ],
        }
    )
    # prompt 是已支持的类型，要真的加载进来；缺正文的 command 与未知类型跳过。
    assert [(spec.kind, spec.command) for spec in runner.hooks["pre_tool_use"]] == [
        ("prompt", "你是一个守卫"),
        ("command", "ok.sh"),
    ]
    joined = "\n".join(runner.warnings)
    assert "SomeFutureEvent" in joined
    assert "telepathy" in joined
    assert "command" in joined
    assert "ok.sh" not in joined, "告警不许把命令正文带进审计"


def test_unknown_hook_type_in_known_event_warns_and_skips():
    runner = HookRunner.from_env(
        settings={
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "telepathy", "command": "never.sh"},
                        {"type": "command", "command": "ok.sh"},
                    ],
                }
            ]
        }
    )
    assert [spec.command for spec in runner.hooks["pre_tool_use"]] == ["ok.sh"]
    assert any("telepathy" in warning for warning in runner.warnings)


def test_timeout_is_clamped_and_garbage_falls_back_to_default():
    runner = HookRunner.from_env(
        settings={
            "PreToolUse": [
                {
                    "hooks": [
                        {"command": "a", "timeout": 0.01},
                        {"command": "b", "timeout": 9999},
                        {"command": "c", "timeout": "abc"},
                    ]
                }
            ]
        }
    )
    assert [spec.timeout for spec in runner.hooks["pre_tool_use"]] == [0.5, 60.0, 10.0]


def test_settings_and_env_merge_and_dedupe():
    raw = json.dumps(
        {"pre_tool_use": [{"match": "Write", "command": "echo hi", "timeout": 5}]}
    )
    same = {"PreToolUse": [{"matcher": "Write", "hooks": [{"command": "echo hi", "timeout": 5}]}]}
    runner = HookRunner.from_env(raw, settings=same)
    specs = runner.hooks["pre_tool_use"]
    assert len(specs) == 1, "同一条配置出现两次就会跑两次"
    assert specs[0].source == "settings"

    extra = json.dumps({"pre_tool_use": [{"match": "Bash", "command": "echo hi", "timeout": 5}]})
    runner = HookRunner.from_env(extra, settings=same)
    assert len(runner.hooks["pre_tool_use"]) == 2
    assert runner.describe()["sources"] == ["env", "settings"]


def test_disable_all_hooks_skips_both_sources():
    runner = HookRunner.from_env(
        json.dumps({"pre_tool_use": [{"command": "x"}]}), settings=CLAUDE_HOOKS, disabled=True
    )
    assert runner.enabled is False
    assert runner.disabled is True
    assert runner.describe()["count"] == 0
    assert any("disableAllHooks" in item for item in runner.warnings)


# --------------------------------------------------------------------------- #
# 2. matcher 语义（对齐上游 _zr）
# --------------------------------------------------------------------------- #
def test_matcher_match_all_forms():
    for matcher in ("", "*", ".*"):
        assert matcher_matches(matcher, "anything") is True


def test_matcher_plain_list_is_exact_not_substring():
    assert matcher_matches("Edit|Write", "Write") is True
    assert matcher_matches("Edit|Write", "Edit") is True
    # 旧实现的 `match in target` 会在这里全绿——这三条就是它的反证。
    assert matcher_matches("Edit|Write", "WriteFile") is False
    assert matcher_matches("run_shell", "run_shell_extra") is False
    assert matcher_matches("Edit|Write", "MultiEditWrite") is False


def test_matcher_accepts_comma_and_spaces():
    assert matcher_matches("Edit,Write", "Write") is True
    assert matcher_matches("Edit, Write", "Edit") is True
    assert matcher_matches("Edit, Write", "Writing") is False


def test_matcher_regex_is_unanchored_search():
    assert matcher_matches("mcp__.*", "mcp__github__create_issue") is True
    assert matcher_matches("^Edit", "BashEdit") is False
    assert matcher_matches("^Edit", "Edit_file") is True


def test_invalid_regex_never_matches_and_warns_at_load():
    assert matcher_matches("(", "anything") is False
    runner = HookRunner.from_env(
        settings={"PreToolUse": [{"matcher": "(", "hooks": [{"command": "x"}]}]}
    )
    assert runner.enabled is False
    assert any("matcher" in item for item in runner.warnings)


# --------------------------------------------------------------------------- #
# 3. 设置层级：用户/管理生效，项目/本机 ignored
# --------------------------------------------------------------------------- #
def test_hooks_default_to_empty():
    assert DEFAULTS["hooks"] == {}
    assert DEFAULTS["disable_all_hooks"] is False
    assert normalize({})["hooks"] == {}
    assert normalize({})["disable_all_hooks"] is False


def test_user_layer_hooks_are_honored(tmp_path):
    result = _layered(tmp_path, tmp_path, user={"hooks": CLAUDE_HOOKS})
    assert result.settings["hooks"] == CLAUDE_HOOKS
    assert result.sources.get("hooks") == "user"


def test_project_layer_hooks_are_ignored(tmp_path):
    root = tmp_path / "repo"
    _write(root / ".blank" / "settings.json", {"hooks": CLAUDE_HOOKS})
    result = _layered(root, tmp_path, user={})
    assert result.settings["hooks"] == {}
    project = next(layer for layer in result.layers if layer.name == LAYER_PROJECT)
    assert any(item["section"] == "hooks" for item in project.ignored)


def test_managed_layer_can_set_hooks_and_switch(tmp_path):
    result = _layered(
        tmp_path,
        tmp_path,
        user={"hooks": {"PreToolUse": [{"hooks": [{"command": "user.sh"}]}]}},
        managed={"hooks": CLAUDE_HOOKS, "disableAllHooks": True},
    )
    # 管理层覆盖用户层；钩子整段替换（数组语义）。
    assert result.settings["hooks"] == CLAUDE_HOOKS
    assert result.settings["disable_all_hooks"] is True
    assert result.sources.get("hooks") == "managed"


def test_project_layer_cannot_disable_hooks(tmp_path):
    root = tmp_path / "repo"
    _write(root / ".blank" / "settings.json", {"disableAllHooks": True})
    result = _layered(root, tmp_path, user={})
    assert result.settings["disable_all_hooks"] is False
    ignored = _ignored(result)
    assert any(item["section"] == "disable_all_hooks" for item in ignored)


def test_disable_alias_underscore_wins(tmp_path):
    assert normalize({"disable_all_hooks": True, "disableAllHooks": False})["disable_all_hooks"] is True
    assert normalize({"disableAllHooks": "yes"})["disable_all_hooks"] is True
    # 认不出的值 = 没配，不允许含糊成"开"。
    assert normalize({"disableAllHooks": "maybe"})["disable_all_hooks"] is False
    # 同一层两种写法：下划线后到，覆盖驼峰。
    result = _layered(
        tmp_path, tmp_path, user={"disableAllHooks": True, "disable_all_hooks": False}
    )
    assert result.settings["disable_all_hooks"] is False


# --------------------------------------------------------------------------- #
# 4. 端到端：真图 + 真子进程钩子（settings 来源）
# --------------------------------------------------------------------------- #
def _log_hook(tmp_path: Path, name: str, body: str = "") -> tuple[str, Path]:
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


def _lines(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _run_task(env, workspace: Path, task_id: str, provider_id: str) -> dict:
    env.mock.reset(
        [
            {"content": "计划：\n1. 读取 main.py"},
            {"content": "读取", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": json.dumps({"summary": "done", "acceptance": []}, ensure_ascii=False)},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_runtime_runs_settings_hooks_and_records_loaded(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command, log = _log_hook(tmp_path, "settings_hook")
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {"hooks": {"PreToolUse": [{"matcher": "read_file", "hooks": [{"command": command, "timeout": 20}]}]}},
    )
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])

    payload = await _run_task(env, workspace, task_id, provider_id)
    assert payload["task"]["status"] == "succeeded", payload

    rows = _lines(log)
    assert rows, "settings.json 里配的钩子没有执行"
    assert rows[0]["hook_event_name"] == "pre_tool_use"
    assert rows[0]["target"] == "read_file"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    loaded = [event for event in events if event["type"] == "hooks.loaded"]
    assert len(loaded) == 1, [event["type"] for event in events]
    body = loaded[0]["payload"]
    assert body["disabled"] is False
    assert body["events"] == {"pre_tool_use": 1}
    assert body["sources"] == ["settings"]


async def test_runtime_disable_all_hooks_prevents_execution(env, workspace: Path, tmp_path: Path, monkeypatch):  # noqa: F811
    command, log = _log_hook(tmp_path, "never")
    settings_file = tmp_path / "settings.json"
    _write(
        settings_file,
        {
            "hooks": {"PreToolUse": [{"matcher": "read_file", "hooks": [{"command": command, "timeout": 20}]}]},
            "disableAllHooks": True,
        },
    )
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])

    payload = await _run_task(env, workspace, task_id, provider_id)
    assert payload["task"]["status"] == "succeeded", payload
    assert not log.exists(), "disableAllHooks 为真时钩子仍然执行了"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    loaded = [event for event in events if event["type"] == "hooks.loaded"]
    assert len(loaded) == 1
    assert loaded[0]["payload"]["disabled"] is True
    assert loaded[0]["payload"]["count"] == 0
