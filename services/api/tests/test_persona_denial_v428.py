"""驳回反馈 → 使用者模型：这条链路必须是真的通的。

单独一个文件而不是塞进 `test_persona_v428`：那条测的是"模型本身怎么判断"，
这条测的是"运行时有没有把它接上"。两者会以不同方式坏——模型逻辑对但没接线，
判据全绿而功能是死的（`hits/misses`、`expires_after_days` 都是这么坏过一次）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.services.agent import graph as graph_module  # noqa: E402
from blank_api.services.agent.persona import PersonaStore  # noqa: E402
from blank_api.services.tools.gateway import ToolGateway  # noqa: E402
from tests.test_guardian_trail_v51 import (  # noqa: E402
    _high_risk_tool,
    _session,
    seeded,  # noqa: F401 - 建表夹具，pytest 按模块命名空间找它
)


class _Answer:
    """替掉 LangGraph 的 interrupt：直接给出用户的决定。"""

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self, payload, **kwargs):
        self.calls += 1
        return self.answer


def _call():
    return {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "run_shell",
            "arguments": '{"command": "rm -rf build"}',
        },
    }


@pytest.mark.asyncio
async def test_a_denial_with_words_teaches_the_persona(seeded, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    session = _session(tmp_path, adapter=None)
    # `_session` 给的网关是空的，没有风险分级就没有审批可触发——
    # 第一版就是这么空跑的（`interrupt` 一次都没被调用，判据却"通过"了）。
    session.gateway = ToolGateway([_high_risk_tool()])
    answer = _Answer({"approve": False, "feedback": "先别列目录，直接用 list_files"})
    monkeypatch.setattr(graph_module, "interrupt", answer)

    denied, _blocked, _hooks, denials = await graph_module._approval_gate(
        session, "agent", [_call()]
    )

    assert answer.calls == 1, "审批没真的被触发——这条判据就是空跑"
    assert "run_shell" in denied
    assert denials["run_shell"] == "先别列目录，直接用 list_files"

    entries = PersonaStore(tmp_path / "persona").load(tmp_path)
    assert entries, "驳回时说的那句话应该被记下来"
    assert any("list_files" in item.rule for item in entries)
    assert entries[0].origin == "explicit"
    # 一次纠正还不够"一贯"——不能立刻改变行为
    assert all(item.status != "active" for item in entries)


@pytest.mark.asyncio
async def test_a_silent_denial_teaches_nothing(seeded, tmp_path, monkeypatch) -> None:
    """他什么都没说，就不能编一条"他可能不喜欢 dir"。"""
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    session = _session(tmp_path, adapter=None)
    session.gateway = ToolGateway([_high_risk_tool()])

    answer = _Answer({"approve": False})
    monkeypatch.setattr(graph_module, "interrupt", answer)
    await graph_module._approval_gate(session, "agent", [_call()])

    assert answer.calls == 1, "审批没真的被触发——这条判据就是空跑"
    assert PersonaStore(tmp_path / "persona").load(tmp_path) == []
