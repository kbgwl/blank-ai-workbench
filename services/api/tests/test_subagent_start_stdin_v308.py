"""第 308 批：SubagentStart 钩子 stdin 字段补齐的可失败判据。

上游 SubagentStart 钩子的 stdin 载荷除了本机已有的十个字段，还带
``session_id`` / ``transcript_path`` / ``scratchpad_dir`` / ``prompt_id`` /
``caller_session_id`` / ``effort``（``effort`` 是 ``{"level": ...}`` 对象）。

每条判据都放在"拿掉对应实现就会红"的位置：真子进程钩子把 stdin 落进 JSONL，
再逐字段断言。``served_call`` 语义不明、不在官方文档里，本批不实现、只记偏离。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from blank_api.services.hooks import HookRunner
from blank_api.services.subagent import SubAgentRequest, run_subagent

from tests.test_agent_v04 import (  # noqa: F401  (复用 HTTP 夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_claude_send_message_v30 import ScriptedAdapter
from tests.test_hook_events_v52 import _lines, _log_hook


class _Events:
    """最小事件记录器：run_subagent 的 hook.completed 落点。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.run_id = ""
        self.origin_run_id = ""

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, dict(payload)))


def _hook_runner(specs: dict[str, list[dict]]) -> HookRunner:
    runner = HookRunner.from_env(json.dumps(specs), settings=None)
    assert not runner.warnings, runner.warnings
    return runner


async def _child(adapter, *, hooks=None, agent_name: str = "", task_id: str = "task-v308",
                 root: Path | None = None):
    """跑一次 run_subagent，返回 (result, adapter)。root 不传时用 tmp_path 风格。"""
    return await run_subagent(
        adapter=adapter,
        tools=[],
        root=root or Path("."),
        task_id=task_id,
        session_factory=None,
        recorder=_Events(),
        request=SubAgentRequest(goal="读取 README 并复述", max_steps=4, agent_name=agent_name),
        agent_path="root/ad",
        depth=1,
        parent_thread_id="root-thread-v308",
        agent_id="agent-ad",
        hooks=hooks,
        history=None,
        start_hooks=True,
    )


# --------------------------------------------------------------------------- #
# 1. session_id：等于派发用的 task_id
# --------------------------------------------------------------------------- #
async def test_session_id_equals_task_id(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-session-id")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks, task_id="task-abc-123")

    rows = _lines(log)
    assert len(rows) == 1, rows
    assert rows[0]["session_id"] == "task-abc-123", rows[0]


# --------------------------------------------------------------------------- #
# 2. transcript_path：指向子代理 transcript 的 JSONL 文件
# --------------------------------------------------------------------------- #
async def test_transcript_path_points_to_subagent_transcript(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-transcript")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks)

    rows = _lines(log)
    assert len(rows) == 1, rows
    tp = rows[0]["transcript_path"]
    assert isinstance(tp, str) and tp, rows[0]
    assert tp.endswith("subagent-transcript.jsonl"), tp
    assert Path(tp).parent.name == "context", tp


# --------------------------------------------------------------------------- #
# 3. scratchpad_dir：指向子代理 scratchpad 目录，且目录已创建
# --------------------------------------------------------------------------- #
async def test_scratchpad_dir_created_and_passed(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-scratchpad")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks, root=tmp_path)

    rows = _lines(log)
    assert len(rows) == 1, rows
    sp = rows[0]["scratchpad_dir"]
    assert isinstance(sp, str) and sp, rows[0]
    assert sp.endswith("scratchpad"), sp
    assert Path(sp).is_dir(), f"scratchpad 目录应已创建: {sp}"


# --------------------------------------------------------------------------- #
# 4. prompt_id：非空 UUID hex 串
# --------------------------------------------------------------------------- #
async def test_prompt_id_is_uuid_hex(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-prompt-id")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks)

    rows = _lines(log)
    assert len(rows) == 1, rows
    pid = rows[0]["prompt_id"]
    assert isinstance(pid, str) and pid, rows[0]
    assert re.fullmatch(r"[0-9a-f]{32}", pid), pid


# --------------------------------------------------------------------------- #
# 5. caller_session_id：等于 parent_thread_id
# --------------------------------------------------------------------------- #
async def test_caller_session_id_equals_parent_thread_id(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-caller")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks)

    rows = _lines(log)
    assert len(rows) == 1, rows
    assert rows[0]["caller_session_id"] == "root-thread-v308", rows[0]


# --------------------------------------------------------------------------- #
# 6. effort：对象，含 level 键，且 level 是合法档位
# --------------------------------------------------------------------------- #
async def test_effort_is_object_with_level(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-effort")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks)

    rows = _lines(log)
    assert len(rows) == 1, rows
    effort = rows[0]["effort"]
    assert isinstance(effort, dict), rows[0]
    level = effort.get("level")
    assert level in ("low", "medium", "high", "xhigh", "max"), effort


# --------------------------------------------------------------------------- #
# 7. 全部六字段一次性到位（一次派发、一个载荷）
# --------------------------------------------------------------------------- #
async def test_all_six_fields_present_in_one_payload(tmp_path: Path, monkeypatch):
    command, log = _log_hook(tmp_path, "v308-all")
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks, task_id="task-all-v308")

    rows = _lines(log)
    assert len(rows) == 1, rows
    row = rows[0]
    for field in ("session_id", "transcript_path", "scratchpad_dir", "prompt_id",
                  "caller_session_id", "effort"):
        assert field in row, f"缺少 {field}: {row}"
    assert row["session_id"] == "task-all-v308"
    assert row["caller_session_id"] == "root-thread-v308"
    assert isinstance(row["effort"], dict) and "level" in row["effort"]


# --------------------------------------------------------------------------- #
# 8. effort 的两条出口：payload 里的 effort 对象 + 子进程的 CLAUDE_EFFORT 环境变量
#    （后者是 hooks.run(effort=...) 的独有效果，拿掉就红）
# --------------------------------------------------------------------------- #
async def test_claude_effort_env_var_set_for_subprocess(tmp_path: Path):
    log = tmp_path / "v308-effort-env.jsonl"
    script = tmp_path / "v308-effort-env.py"
    script.write_text(
        "import json, os, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "payload = json.load(sys.stdin)\n"
        "payload['_claude_effort_env'] = os.environ.get('CLAUDE_EFFORT', '')\n"
        f"open({str(log)!r}, 'a', encoding='utf-8')"
        ".write(json.dumps(payload, ensure_ascii=False) + '\\n')\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}"'
    hooks = _hook_runner({"subagent_start": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["结论"])

    await _child(adapter, hooks=hooks)

    rows = _lines(log)
    assert len(rows) == 1, rows
    env_val = rows[0].get("_claude_effort_env", "")
    assert env_val in ("low", "medium", "high", "xhigh", "max"), rows[0]
