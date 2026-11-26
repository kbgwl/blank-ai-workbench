"""NEXT-DEV-v4 1b：不同文件的独立写/改在完全访问档并行落盘。

判据只有三条：
1. 不同文件 + bypass → 真的并发（峰值 > 1）；
2. 同一文件 → 串行（峰值 = 1），避免备份/读后写状态互相踩；
3. 非 bypass → 串行，审批卡不能被 gather 打乱。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from blank_api.services.agent import graph
from blank_api.services.agent.graph import _PlannedCall


def _call(name: str, path: str) -> _PlannedCall:
    return _PlannedCall(
        call={"id": f"{name}-{path}"},
        name=name,
        arguments={"path": path},
        mode="execute",
    )


def _patched_dispatch(monkeypatch):
    active = 0
    peak = 0

    async def fake_dispatch(session, node, item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"tool": item.name, "status": "ok"}

    monkeypatch.setattr(graph, "_dispatch", fake_dispatch)
    return lambda: peak


@pytest.mark.asyncio
async def test_different_file_writes_run_concurrently_in_bypass(monkeypatch):
    monkeypatch.setattr(graph, "feature_enabled", lambda name: True)
    peak = _patched_dispatch(monkeypatch)
    session = SimpleNamespace(permission_mode="bypass", effects=0)

    out = await graph._execute_all(
        session,
        "execute_action",
        [_call("write_file", "a.py"), _call("edit_file", "b.py")],
    )

    assert peak() == 2
    assert [item["tool"] for item in out] == ["write_file", "edit_file"]


@pytest.mark.asyncio
async def test_same_file_writes_stay_sequential(monkeypatch):
    monkeypatch.setattr(graph, "feature_enabled", lambda name: True)
    peak = _patched_dispatch(monkeypatch)
    session = SimpleNamespace(permission_mode="bypass", effects=0)

    await graph._execute_all(
        session,
        "execute_action",
        [_call("write_file", "a.py"), _call("edit_file", "a.py")],
    )

    assert peak() == 1


@pytest.mark.asyncio
async def test_non_bypass_mode_writes_stay_sequential(monkeypatch):
    monkeypatch.setattr(graph, "feature_enabled", lambda name: True)
    peak = _patched_dispatch(monkeypatch)
    session = SimpleNamespace(permission_mode="default", effects=0)

    await graph._execute_all(
        session,
        "execute_action",
        [_call("write_file", "a.py"), _call("edit_file", "b.py")],
    )

    assert peak() == 1
