"""UP-04：本机模式网络未隔离必须可见（启动事件 + 创建响应），且不阻断任务。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.sandbox import tiers
from blank_api.services.sandbox.tiers import TierResolution, policy_for


class _StubAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="stub",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="stub",
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover - 装配不需要真调用
        return ModelResponse(text="{}", usage=Usage(1, 1), model="stub")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="stub")


def _tier(network: bool) -> TierResolution:
    return TierResolution(
        requested="workspace",
        tier="workspace",
        strength="job_only",
        label="内核限额",
        backend="local",
        global_enabled=True,
        available=True,
        degraded=False,
        limits_enforced=True,
        filesystem_enforced=False,
        network_enforced=network,
        notice="本机模式：文件与网络未强制",
        reason="",
        policy=policy_for("workspace"),
    )


def test_default_constraints_reports_the_tier_truth(monkeypatch) -> None:
    monkeypatch.setattr(tiers, "resolve_tier", lambda *args, **kwargs: _tier(False))
    monkeypatch.setattr(tiers, "_CONSTRAINTS_CACHE", None)

    value = tiers.default_constraints()

    assert value["network_isolated"] is False
    assert value["filesystem_isolated"] is False
    assert value["tier"] == "workspace"
    assert value["backend"] == "local"


async def test_runtime_warns_when_network_is_not_enforced(tmp_path: Path, workspace: Path, monkeypatch):
    import blank_api.services.agent.runtime as runtime_module
    from blank_api.services.sandbox import docker_probe

    async with get_session_factory()() as session:
        if await session.get(Task, "task-net-warning") is None:
            session.add(Project(id="p-task-net-warning", name="p"))
            await session.flush()
            session.add(
                Task(
                    id="task-net-warning",
                    project_id="p-task-net-warning",
                    title="t",
                    goal="g",
                )
            )
            await session.commit()

    async def fake_fresh(**kwargs):
        return SimpleNamespace(available=False, reason="test", notice="")

    monkeypatch.setattr(runtime_module, "resolve_tier", lambda *args, **kwargs: _tier(False))
    monkeypatch.setattr(docker_probe, "ensure_fresh", fake_fresh)

    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id="task-net-warning",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="看看网络隔离提示",
    )
    scope = await runtime._prepare(request, "task-net-warning:1")
    try:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.task_id == "task-net-warning")
                )
            ).scalars().all()
        warnings = [row for row in rows if row.type == "sandbox.warning"]
        assert warnings, [row.type for row in rows]
        payload = warnings[0].payload or {}
        assert payload["level"] == "warning"
        assert payload["tier"] == "workspace"
        assert payload["backend"] == "local"
        assert "网络访问未受限" in str(payload.get("message") or "")
    finally:
        lsp = getattr(scope.session.context, "lsp", None)
        if lsp is not None:
            await lsp.close()


async def test_runtime_does_not_warn_when_network_is_enforced(tmp_path: Path, workspace: Path, monkeypatch):
    import blank_api.services.agent.runtime as runtime_module
    from blank_api.services.sandbox import docker_probe

    async def fake_fresh(**kwargs):
        return SimpleNamespace(available=False, reason="test", notice="")

    monkeypatch.setattr(runtime_module, "resolve_tier", lambda *args, **kwargs: _tier(True))
    monkeypatch.setattr(docker_probe, "ensure_fresh", fake_fresh)

    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-net-enforced",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="网络已强制时不该报警",
    )
    scope = await runtime._prepare(request, "task-net-enforced:1")
    try:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.task_id == "task-net-enforced")
                )
            ).scalars().all()
        assert not [row for row in rows if row.type == "sandbox.warning"]
    finally:
        lsp = getattr(scope.session.context, "lsp", None)
        if lsp is not None:
            await lsp.close()
