"""AF3a / AF3b：两个事件的触发点——`UserPromptExpansion`（斜杠命令展开那一刻）与
`TeammateIdle`（成员即将转入空闲）。

对标 Claude Code 的同名事件契约，逐条取证自上游实物（`claude.uniq.txt`）：
* `UserPromptExpansion` 的载荷是上游那五个键（`expansion_type` / `command_name` /
  `command_args` / `command_source` / `prompt`），`matcher` 比的是 `expansion_type`；
  它是阻断事件（与 `UserPromptSubmit` 同族）：退出码 2 / decision=deny = 这次展开作废，
  理由里带用户原文，除非钩子要求 `suppressOriginalPrompt`（那一条只管阻断消息，
  "decision 为 block 时从消息里略去原文"）；ask 档在提交这一步没有确认界面，按否决处理。
* `TeammateIdle` 的载荷只有 `teammate_name` / `team_name` 两个键（上游把 team_name
  标成 deprecated，仍按会话推导传），`matcher` 比 `teammate_name`；它**不阻断**：
  钩子要 `preventContinuation` 才阻止继续，理由取 `stopReason`，缺省文案逐字是
  `TeammateIdle hook prevented continuation`；退出码 2 只算一条非阻断错误（可见、
  不注入、不阻止）。

判据分三层，每条都能失败：
* 事件面与载荷：两个事件注册/别名/阻断归属、五键逐字、两份载荷键常量不漂移；
* 触发处置（真子进程钩子）：matcher 分流、否决/抑制/ask、补上下文与掩码、阻止继续
  与缺省文案、退出码 2 的非阻断分流、stdin 载荷；
* 审计：`hook.completed` / `hook.prompt_blocked` / `hook.continuation_stopped` /
  `hook.non_blocking_error` / `hook.context_received` 各就各位。
"""

from __future__ import annotations

import json
from pathlib import Path

from blank_api.services.commands_defs import EXPANSION_PAYLOAD_KEYS, expansion_payload
from blank_api.services.hooks import (
    BLOCKING_EVENTS,
    EVENT_ALIASES,
    EXPAND_PAYLOAD_KEYS,
    HOOK_EVENTS,
    ExpansionOutcome,
    HookRunner,
    IdleOutcome,
    prompt_sha,
    run_expansion_hooks,
    run_idle_hooks,
)
from tests.test_agent_v04 import env  # noqa: F401  （asyncio_mode=auto，沿用装置约定）
from tests.test_hook_events_v52 import _log_hook, _lines

DEFAULT_IDLE_STOP_REASON = "TeammateIdle hook prevented continuation"

EXP_PAYLOAD = {
    "expansion_type": "slash_command",
    "command_name": "review",
    "command_args": "main.py",
    "command_source": "project",
    "prompt": "帮我评审 main.py",
}


class _Events:
    """最小事件记录器：只把 add 的三元组记下来供断言。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, dict(payload)))

    def named(self, event: str) -> list[dict]:
        return [payload for name, _node, payload in self.events if name == event]


def _runner(specs: dict) -> HookRunner:
    runner = HookRunner.from_env(raw=json.dumps(specs), settings=None)
    assert not runner.warnings, runner.warnings
    return runner


def _emit(body: str) -> str:
    """生成钩子正文：stdout 只用 JSON，避免解码这一层混进判据。"""
    return (
        "import json, sys\n"
        f"{body}\n"
    )


# --------------------------------------------------------------------------- #
# 层 1：事件面与载荷
# --------------------------------------------------------------------------- #
def test_两个事件注册别名与阻断归属() -> None:
    assert "user_prompt_expansion" in HOOK_EVENTS
    assert "teammate_idle" in HOOK_EVENTS
    assert EVENT_ALIASES["UserPromptExpansion"] == "user_prompt_expansion"
    assert EVENT_ALIASES["TeammateIdle"] == "teammate_idle"
    # 展开与提交同族：上游 UserPromptExpansion 的退出码 2 就是"这次展开作废"，
    # 不进阻断集合的话退出码 2 会被当成一条非阻断错误，否决直接失效。
    assert "user_prompt_expansion" in BLOCKING_EVENTS
    # TeammateIdle 不阻断：进集合就会把"钩子报错"变成"成员不能转空闲"。
    assert "teammate_idle" not in BLOCKING_EVENTS


def test_展开载荷五键逐字对齐上游() -> None:
    info = {**EXP_PAYLOAD, "source": "project", "kind": "command", "chars": 12}
    got = expansion_payload(info)
    assert list(got) == ["expansion_type", "command_name", "command_args", "command_source", "prompt"]
    assert got == EXP_PAYLOAD
    # 其余键（source/kind/字数）留给审计，不进钩子载荷。
    assert "source" not in got and "kind" not in got
    assert expansion_payload(None) == {}


def test_两份载荷键常量不漂移() -> None:
    """hooks.py 与 commands_defs.py 各存一份五键常量；漂了钩子就拿不到参数。"""
    assert list(EXPAND_PAYLOAD_KEYS) == list(EXPANSION_PAYLOAD_KEYS)


# --------------------------------------------------------------------------- #
# 层 2：user_prompt_expansion 的触发处置
# --------------------------------------------------------------------------- #
async def test_非斜杠展开不跑钩子也不产事件(tmp_path: Path) -> None:
    command, log = _log_hook(tmp_path, "exp-empty")
    runner = _runner({"user_prompt_expansion": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_expansion_hooks(runner, events, {})
    assert out == ExpansionOutcome(), "没有展开事实时不该有任何处置"
    assert not events.events
    assert not log.exists() or not _lines(log)


async def test_matcher按expansion_type分流(tmp_path: Path) -> None:
    command_a, log_a = _log_hook(tmp_path, "exp-slash")
    command_b, log_b = _log_hook(tmp_path, "exp-mcp")
    runner = _runner(
        {
            "user_prompt_expansion": [
                {"command": command_a, "match": "slash_command", "timeout": 20},
                {"command": command_b, "match": "mcp_prompt", "timeout": 20},
            ]
        }
    )
    await run_expansion_hooks(runner, _Events(), {**EXP_PAYLOAD, "expansion_type": "slash_command"})
    assert len(_lines(log_a)) == 1
    assert not log_b.exists() or not _lines(log_b), "matcher 不该让 mcp_prompt 的钩子跟着跑"


async def test_退出码二否决作废整次展开且理由带原文(tmp_path: Path) -> None:
    command, _log = _log_hook(tmp_path, "exp-deny", "sys.stderr.write('这条展开不该进模型')\nsys.exit(2)\n")
    runner = _runner({"user_prompt_expansion": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_expansion_hooks(runner, events, dict(EXP_PAYLOAD))
    assert out.blocked
    assert "这条展开不该进模型" in out.reason
    assert EXP_PAYLOAD["prompt"] in out.reason, "未要求抑制时原文要进理由"
    blocked = events.named("hook.prompt_blocked")
    assert blocked
    assert blocked[0]["original_omitted"] is False
    assert blocked[0]["original_sha"] == prompt_sha(EXP_PAYLOAD["prompt"])


async def test_抑制原文时否决理由不带原文(tmp_path: Path) -> None:
    body = (
        "sys.stdout.write(json.dumps({'decision':'deny','reason':'不许展开',"
        "'suppressOriginalPrompt':True}))\n"
    )
    command, _log = _log_hook(tmp_path, "exp-suppress", _emit(body))
    runner = _runner({"user_prompt_expansion": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_expansion_hooks(runner, events, dict(EXP_PAYLOAD))
    assert out.blocked
    assert "不许展开" in out.reason
    assert EXP_PAYLOAD["prompt"] not in out.reason, "要求抑制时原文不得进阻断消息"
    assert events.named("hook.prompt_blocked")[0]["original_omitted"] is True


async def test_ask档按否决处理(tmp_path: Path) -> None:
    """提交这一步没有确认界面：钩子要求"先问一声"若静默放行，等于把写下的条件丢掉。"""
    body = "sys.stdout.write(json.dumps({'decision':'ask','reason':'先问一声'}))\n"
    command, _log = _log_hook(tmp_path, "exp-ask", _emit(body))
    runner = _runner({"user_prompt_expansion": [{"command": command, "timeout": 20}]})
    out = await run_expansion_hooks(runner, _Events(), dict(EXP_PAYLOAD))
    assert out.blocked
    assert "先问一声" in out.reason


async def test_补上下文进notes且来源掩码(tmp_path: Path) -> None:
    body = (
        "sys.stdout.write(json.dumps({'hookSpecificOutput':{'additionalContext':"
        "'这条是补的上下文'}}))\n"
    )
    command, _log = _log_hook(tmp_path, "exp-note", _emit(body))
    runner = _runner({"user_prompt_expansion": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_expansion_hooks(runner, events, dict(EXP_PAYLOAD))
    assert not out.blocked
    assert out.notes == ["这条是补的上下文"]
    assert out.sources and "这条是补的上下文" not in "".join(out.sources), "来源只留掩码，不落正文"
    # 展开钩子的补段由提交/插话路径注入模型，这一层不发 context_received。
    assert not events.named("hook.context_received")
    got = events.named("hook.completed")[0]
    assert got["event"] == "user_prompt_expansion"


async def test_stdin带五键与事件名(tmp_path: Path) -> None:
    command, log = _log_hook(tmp_path, "exp-stdin")
    runner = _runner({"user_prompt_expansion": [{"command": command, "timeout": 20}]})
    await run_expansion_hooks(runner, _Events(), dict(EXP_PAYLOAD))
    row = _lines(log)[0]
    assert row["hook_event_name"] == "user_prompt_expansion"
    assert row["event"] == "user_prompt_expansion"
    assert row["target"] == "slash_command"
    for key, value in EXP_PAYLOAD.items():
        assert row[key] == value


# --------------------------------------------------------------------------- #
# 层 2：teammate_idle 的触发处置
# --------------------------------------------------------------------------- #
async def test_idle非阻断且载荷两键逐字(tmp_path: Path) -> None:
    command, log = _log_hook(tmp_path, "idle-payload")
    runner = _runner({"teammate_idle": [{"command": command, "timeout": 20}]})
    payload = {"teammate_name": "researcher", "team_name": "team-1"}
    events = _Events()
    out = await run_idle_hooks(runner, events, payload)
    assert out == IdleOutcome(), "钩子没要求阻止时默认不否决"
    row = _lines(log)[0]
    assert row["hook_event_name"] == "teammate_idle"
    assert row["teammate_name"] == "researcher"
    assert row["team_name"] == "team-1"


async def test_idle空载荷不跑钩子(tmp_path: Path) -> None:
    command, log = _log_hook(tmp_path, "idle-empty")
    runner = _runner({"teammate_idle": [{"command": command, "timeout": 20}]})
    out = await run_idle_hooks(runner, _Events(), None)
    assert out == IdleOutcome()
    assert not log.exists() or not _lines(log)


async def test_preventContinuation阻止继续并取stopReason(tmp_path: Path) -> None:
    body = (
        "sys.stdout.write(json.dumps({'preventContinuation':True,'stopReason':'先别继续'}))\n"
    )
    command, _log = _log_hook(tmp_path, "idle-prevent", _emit(body))
    runner = _runner({"teammate_idle": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_idle_hooks(runner, events, {"teammate_name": "researcher", "team_name": "t"})
    assert out.prevented
    assert out.reason == "先别继续"
    row = events.named("hook.continuation_stopped")[0]
    assert row["teammate_name"] == "researcher"
    assert row["reason"] == "先别继续"


async def test_preventContinuation缺stopReason用上游缺省文案(tmp_path: Path) -> None:
    body = "sys.stdout.write(json.dumps({'preventContinuation':True}))\n"
    command, _log = _log_hook(tmp_path, "idle-default", _emit(body))
    runner = _runner({"teammate_idle": [{"command": command, "timeout": 20}]})
    out = await run_idle_hooks(runner, _Events(), {"teammate_name": "researcher", "team_name": "t"})
    assert out.prevented
    assert out.reason == DEFAULT_IDLE_STOP_REASON


async def test_idle退出码二是非阻断错误不阻止(tmp_path: Path) -> None:
    command, _log = _log_hook(tmp_path, "idle-exit2", "sys.stderr.write('钩子报错')\nsys.exit(2)\n")
    runner = _runner({"teammate_idle": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_idle_hooks(runner, events, {"teammate_name": "researcher", "team_name": "t"})
    assert not out.prevented, "退出码 2 只算一条非阻断错误，不能变成阻止继续"
    assert not out.notes, "退出码 2 的正文是报错，不进上下文"
    errors = events.named("hook.non_blocking_error")
    assert errors and errors[0]["count"] == 1


async def test_idle补上下文进群记录可见的notes(tmp_path: Path) -> None:
    body = (
        "sys.stdout.write(json.dumps({'hookSpecificOutput':{'additionalContext':"
        "'转空闲时补一句'}}))\n"
    )
    command, _log = _log_hook(tmp_path, "idle-note", _emit(body))
    runner = _runner({"teammate_idle": [{"command": command, "timeout": 20}]})
    events = _Events()
    out = await run_idle_hooks(runner, events, {"teammate_name": "researcher", "team_name": "t"})
    assert not out.prevented
    assert out.notes == ["转空闲时补一句"]
    got = events.named("hook.context_received")[0]
    assert got["count"] == 1
    assert got["chars"] == len("转空闲时补一句")


async def test_idle_matcher按teammate_name分流(tmp_path: Path) -> None:
    command_a, log_a = _log_hook(tmp_path, "idle-a")
    command_b, log_b = _log_hook(tmp_path, "idle-b")
    runner = _runner(
        {
            "teammate_idle": [
                {"command": command_a, "match": "researcher", "timeout": 20},
                {"command": command_b, "match": "writer", "timeout": 20},
            ]
        }
    )
    await run_idle_hooks(runner, _Events(), {"teammate_name": "researcher", "team_name": "t"})
    assert len(_lines(log_a)) == 1
    assert not log_b.exists() or not _lines(log_b)
