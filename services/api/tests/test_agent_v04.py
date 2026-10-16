import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from blank_api.app import create_app
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.patch import restore_backup

API = "/api/v1"
PATCH = "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('patched')\n"
ENV_PATCH = "--- /dev/null\n+++ b/.env\n@@ -0,0 +1,1 @@\n+TOKEN=plain\n"


class MockModel:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.index = 0
        self.requests: list[dict] = []

    def reset(self, steps: list[dict]) -> None:
        self.steps = steps
        self.index = 0

    def factory(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        step = self.steps[self.index] if self.index < len(self.steps) else {"content": "已完成，无需更多操作"}
        self.index += 1
        message = {"content": step.get("content", "")}
        if step.get("tool_calls"):
            message["tool_calls"] = step["tool_calls"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                "model": "mock-coder",
            },
        )


@dataclass
class Env:
    client: httpx.AsyncClient
    mock: MockModel = field(default_factory=MockModel)

    def context(self, task_id: str, root: Path) -> ToolContext:
        return ToolContext(
            root=root,
            task_id=task_id,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: False,
        )

    def gateway(self) -> ToolGateway:
        return ToolGateway(build_default_tools())

    async def tool_calls(self, task_id: str) -> list[ToolCall]:
        async with get_session_factory()() as session:
            return list(
                (
                    await session.execute(
                        select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.created_at)
                    )
                ).scalars()
            )


@pytest.fixture
async def env():
    app = create_app()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    state = Env(client=None)  # type: ignore[arg-type]
    app.state.model_client_factory = state.mock.factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        state.client = client
        yield state


async def create_project(env: Env, root: Path) -> str:
    project = (
        await env.client.post(f"{API}/projects", json={"name": f"p-{uuid.uuid4().hex[:6]}"})
    ).json()
    bound = await env.client.post(
        f"{API}/projects/{project['id']}/workspaces", json={"path": str(root)}
    )
    assert bound.status_code == 201, bound.text
    return project["id"]


async def create_provider(env: Env, tools: bool = True) -> str:
    resp = await env.client.post(
        f"{API}/providers",
        json={
            "name": f"vendor-{uuid.uuid4().hex[:6]}",
            "kind": "openai_compatible",
            "base_url": "https://mock.example/v1",
            "model": "mock-coder",
            "api_key": "sk-mocksecret123456",
            "supports_tools": tools,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def create_task(env: Env, project_id: str, criteria: list[str], **kwargs) -> str:
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": kwargs.get("title", "给 main.py 增加输出"),
            "goal": kwargs.get("goal", "修改 main.py 并运行测试"),
            "acceptance_criteria": criteria,
            "max_steps": kwargs.get("max_steps", 6),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def add_tests_dir(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")


async def test_read_file_and_search_code_stay_inside_workspace(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["能读取文件"])
    gateway = env.gateway()
    context = env.context(task_id, workspace)

    read = await gateway.execute("read_file", {"path": "main.py"}, context, "execute_action")
    assert read["ok"] is True and "print(" in read["output"]

    search = await gateway.execute(
        "search_code", {"pattern": r"^VALUE", "mode": "regex"}, context, "execute_action"
    )
    assert search["ok"] and [m["path"] for m in search["data"]["matches"]] == ["src/core.py"]

    ignored = await gateway.execute(
        "search_code", {"pattern": "hidden", "mode": "literal"}, context, "execute_action"
    )
    assert ignored["ok"] and ignored["data"]["count"] == 0

    traversal = await gateway.execute(
        "read_file", {"path": "../outside.txt"}, context, "execute_action"
    )
    assert traversal["status"] == "denied" and "越出工作区" in traversal["message"]

    absolute = await gateway.execute(
        "read_file", {"path": str(workspace / "main.py")}, context, "execute_action"
    )
    assert absolute["status"] == "denied" and "绝对路径" in absolute["message"]

    rows = await env.tool_calls(task_id)
    assert [row.name for row in rows] == [
        "read_file",
        "search_code",
        "search_code",
        "read_file",
        "read_file",
    ]
    assert [row.status for row in rows] == ["ok", "ok", "ok", "denied", "denied"]
    assert all(row.duration_ms >= 0 for row in rows)
    assert rows[0].arguments["path"] == "main.py" and rows[0].result_summary
    assert rows[3].risk_level == "low"


async def test_apply_patch_modifies_file_and_keeps_reversible_backup(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["main.py 被修改"])
    gateway = env.gateway()
    context = env.context(task_id, workspace)

    result = await gateway.execute(
        "apply_patch", {"path": "main.py", "patch": PATCH}, context, "execute_action"
    )
    assert result["ok"] is True, result
    assert "patched" in (workspace / "main.py").read_text(encoding="utf-8")

    escape = await gateway.execute(
        "apply_patch", {"path": "../evil.py", "patch": PATCH}, context, "execute_action"
    )
    assert escape["status"] == "denied" and not (workspace.parent / "evil.py").exists()

    async with get_session_factory()() as session:
        change = (
            await session.execute(select(FileChange).where(FileChange.task_id == task_id))
        ).scalars().one()
    assert change.change_type == "modified" and change.backup_path
    assert "+print('patched')" in change.diff
    backup = workspace / change.backup_path
    assert backup.read_text(encoding="utf-8") == "print('hi')\n"

    restore_backup(workspace, change.backup_path)
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"

    created = await gateway.execute(
        "apply_patch", {"path": "src/new_module.py", "patch": ENV_PATCH.replace(".env", "src/new_module.py")},
        context,
        "execute_action",
    )
    assert created["ok"] and (workspace / "src" / "new_module.py").exists()


async def test_run_process_falls_back_when_the_loop_cannot_spawn(
    env: Env, workspace: Path, monkeypatch
):
    # Selector 事件循环（例如 uvicorn --reload 的子进程）里 asyncio 起不了子进程，必须退回线程执行。
    from blank_api.services.tools.shell import run_process

    async def refuse(*_args, **_kwargs):
        raise NotImplementedError

    monkeypatch.setattr("asyncio.create_subprocess_exec", refuse)
    result = await run_process([sys.executable, "-c", "print('threaded-fallback')"], workspace, 30)
    assert result["timed_out"] is False, result
    assert result["exit_code"] == 0, result
    assert "threaded-fallback" in result["stdout"]


async def test_run_shell_captures_output_and_terminates_on_timeout(
    env: Env, workspace: Path, monkeypatch
):
    # 本用例覆盖工具层的输出捕获/超时终止语义，需要在宿主机进程执行；
    # 沙箱路由由 tests/test_sandbox_v07.py 用真实 docker 验证。
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "0")
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["命令可执行"])
    gateway = env.gateway()
    context = env.context(task_id, workspace)

    hello = await gateway.execute(
        "run_shell", {"command": f'"{sys.executable}" -c "print(\'shell-ok\')"'}, context, "execute_action"
    )
    assert hello["ok"] is True and "shell-ok" in hello["output"]
    assert hello["data"]["exit_code"] == 0

    (workspace / "sleeper.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    started = time.perf_counter()
    timeout = await gateway.execute(
        "run_shell",
        {"command": f'"{sys.executable}" sleeper.py', "timeout_seconds": 2},
        context,
        "execute_action",
    )
    elapsed = time.perf_counter() - started
    assert timeout["status"] == "timeout"
    assert "超时" in timeout["message"] and "终止" in timeout["message"]
    assert elapsed < 15

    secret = await gateway.execute(
        "run_shell",
        {"command": f'"{sys.executable}" -c "print(\'API_KEY=sk-verysecretvalue\')"'},
        context,
        "execute_action",
    )
    assert secret["ok"]
    rows = await env.tool_calls(task_id)
    assert all("sk-verysecretvalue" not in row.result_summary for row in rows)
    assert any("****" in row.result_summary for row in rows)


async def test_run_tests_detects_and_executes_pytest_suite(env: Env, tmp_path: Path):
    root = tmp_path / "toproject"
    root.mkdir()
    add_tests_dir(root)
    project_id = await create_project(env, root)
    task_id = await create_task(env, project_id, ["测试通过"])
    gateway = env.gateway()
    context = env.context(task_id, root)

    passed = await gateway.execute("run_tests", {}, context, "run_validation")
    assert passed["ok"] is True, passed
    assert passed["data"]["exit_code"] == 0
    assert passed["data"]["summary"].get("passed") == 1
    assert "pytest" in passed["data"]["command"]

    empty = tmp_path / "empty"
    empty.mkdir()
    missing = await gateway.execute(
        "run_tests", {}, env.context(task_id, empty), "run_validation"
    )
    assert missing["status"] == "error" and "未识别到测试命令" in missing["message"]


async def test_full_task_run_produces_events_and_evidence(env: Env, workspace: Path):
    add_tests_dir(workspace)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    criteria = ["main.py 输出 patched", "原有测试全部通过"]
    task_id = await create_task(env, project_id, criteria)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读取 main.py\n2. 搜索 VALUE\n3. 应用补丁\n4. 运行测试"},
            {
                "content": "开始执行",
                "tool_calls": [
                    call("read_file", {"path": "main.py"}),
                    call("search_code", {"pattern": "VALUE"}),
                    call("apply_patch", {"path": "main.py", "patch": PATCH}),
                ],
            },
            {"content": "补丁已应用，文件读取与搜索完成"},
            # 审核节点插在验证与总结之间，mock 必须多给一条审核结论，
            # 否则审核拿到的是总结用的 JSON、永远判不通过，修复回路会把 steps_used 撑大。
            {
                "content": json.dumps(
                    {"approved": True, "findings": [], "summary": "实现与验收条件一致"},
                    ensure_ascii=False,
                )
            },
            {
                "content": json.dumps(
                    {
                        "summary": "已修改 main.py 并通过测试",
                        "acceptance": [
                            {"criterion": criteria[0], "covered": True, "evidence": "apply_patch 返回成功且文件包含 patched"},
                            {"criterion": criteria[1], "covered": True, "evidence": "pytest 1 passed"},
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "succeeded"
    assert body["task"]["steps_used"] == 2

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert {
        "task.created",
        "task.started",
        "plan.created",
        "tool.requested",
        "tool.completed",
        "file.changed",
        "validation.completed",
        "task.completed",
    } <= set(types)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert {event["node"] for event in events} >= {"create_plan", "execute_action", "run_validation"}
    polled = (await env.client.get(f"{API}/tasks/{task_id}/events", params={"after_seq": 2})).json()
    assert [event["seq"] for event in polled] == list(range(3, len(events) + 1))

    evidence = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()
    assert [item["criterion"] for item in evidence["acceptance"]] == criteria
    assert all(item["covered"] for item in evidence["acceptance"])
    assert "main.py" in evidence["changes"]["files"]
    assert "+print('patched')" in evidence["changes"]["diff"]
    assert evidence["changes"]["reversible"] is True
    assert evidence["validation"]["passed"] is True
    assert "pytest" in evidence["validation"]["command"]
    # 5 次模型调用：计划 / 执行 ×2 / 审核 / 总结（mock 每次固定 120+30 tokens）
    assert evidence["usage"]["prompt_tokens"] == 600 and evidence["usage"]["calls"] == 5
    assert evidence["usage"]["total_tokens"] == 750
    assert evidence["usage"]["providers"] and evidence["usage"]["models"] == ["mock-coder"]
    async with get_session_factory()() as session:
        latencies = list(
            (
                await session.execute(select(ModelUsage.latency_ms).where(ModelUsage.task_id == task_id))
            ).scalars()
        )
    # Mock 传输常快于 1ms， latency 可能是 0；这里校验的是求和口径，而不是数值大小。
    assert evidence["usage"]["latency_ms"] == sum(latencies)
    assert evidence["pending_approvals"] == []
    assert len(evidence["timeline"]) == len(events)
    assert "sk-mocksecret123456" not in json.dumps(evidence, ensure_ascii=False)

    diff = (await env.client.get(f"{API}/tasks/{task_id}/diff")).json()
    assert diff["files"] == ["main.py"] and "+print('patched')" in diff["diff"]

    tool_rows = await env.tool_calls(task_id)
    assert {row.name for row in tool_rows} >= {"read_file", "search_code", "apply_patch"}
    assert all(row.duration_ms >= 0 for row in tool_rows)

    async with get_session_factory()() as session:
        checkpoints = (
            await session.execute(select(AgentCheckpoint).where(AgentCheckpoint.task_id == task_id))
        ).scalars().all()
        usage = (
            await session.execute(select(ModelUsage).where(ModelUsage.task_id == task_id))
        ).scalars().all()
    assert checkpoints and {node.status for node in usage} == {"ok"}
    assert {node.node for node in usage} >= {"create_plan", "execute_action"}


async def test_high_risk_tool_without_approval_is_refused(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["删除临时文件"])
    (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 删除 notes.txt"},
            {"content": "执行删除", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
            {"content": "该操作被系统拒绝，等待人工审批"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    assert "待审批" in body["task"]["failure_reason"]
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "keep me\n"

    approvals = body["evidence"]["pending_approvals"]
    assert len(approvals) == 1
    assert approvals[0]["tool"] == "run_shell"
    assert approvals[0]["risk_level"] == "high"
    assert approvals[0]["approval_state"] == "待审批"
    assert any("待审批" in item for item in body["evidence"]["unverified_risks"])

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert any(event["type"] == "approval.required" for event in events)
    assert any(event["type"] == "task.waiting_approval" for event in events)
    # 图中断发生在执行之前：被审批拦下的工具不会留下任何执行记录。
    assert await env.tool_calls(task_id) == []


async def test_approval_hook_allows_the_same_high_risk_call(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["允许执行命令"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 执行命令"},
            {"content": "执行命令", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
            {"content": "命令已执行"},
            {"content": json.dumps({"summary": "已批准执行", "acceptance": []}, ensure_ascii=False)},
        ]
    )

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "approve_high_risk": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["evidence"]["pending_approvals"] == []
    rows = await env.tool_calls(task_id)
    assert rows[0].status != "awaiting_approval"
    assert rows[0].risk_level == "high"


async def test_run_reports_readable_error_when_provider_has_no_tools(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env, tools=False)
    task_id = await create_task(env, project_id, ["不应崩溃"])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text
    error = resp.json()["error"]
    assert error["code"] == "model_tools_unsupported"
    assert "工具" in error["message"] and "设置" in error["message"]

    task = (await env.client.get(f"{API}/tasks/{task_id}")).json()
    assert task["status"] == "failed"
    assert "工具" in task["failure_reason"]
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert any(event["type"] == "task.failed" for event in events)
    assert await env.tool_calls(task_id) == []


async def test_cancelled_task_stops_before_new_tool_calls(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["取消后不再执行"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读取文件"},
            {"content": "取消", "tool_calls": [call("read_file", {"path": "main.py"})]},
        ]
    )
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        task.status = "cancelled"
        await session.commit()

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await provider_row(provider_id), env.mock.factory),
            root=workspace,
            title="取消测试",
            goal="取消测试",
            acceptance_criteria=["取消后不再执行"],
            max_steps=4,
        )
    )
    assert evidence["task"]["status"] == "cancelled"
    assert evidence["tools"] == []
    assert any(event["type"] == "task.cancelled" for event in evidence["timeline"])


async def provider_row(provider_id: str) -> ModelProvider:
    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


async def test_high_risk_interrupt_resumes_from_checkpoint(env: Env, workspace: Path):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["审批后可继续"])
    # 工作区放一个可通过的测试，恢复后的验证环节才有明确的「通过」结论可判定。
    (workspace / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    adapter = build_adapter(await provider_row(provider_id), env.mock.factory)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写入 .env"},
            {"content": "写入配置", "tool_calls": [call("apply_patch", {"path": ".env", "patch": ENV_PATCH})]},
        ]
    )
    request = RunRequest(
        task_id=task_id,
        provider_id=provider_id,
        provider_name="mock-vendor",
        adapter=adapter,
        root=workspace,
        title="写入配置",
        goal="写入 .env 配置",
        acceptance_criteria=["审批后可继续"],
        max_steps=4,
    )
    runtime = AgentRuntime(get_session_factory())

    evidence = await runtime.run(request)
    assert evidence["task"]["status"] == "waiting_approval"
    assert not (workspace / ".env").exists()
    assert evidence["timeline"][-1]["type"] == "task.waiting_approval"
    async with get_session_factory()() as session:
        checkpoints = (
            await session.execute(select(AgentCheckpoint).where(AgentCheckpoint.task_id == task_id))
        ).scalars().all()
        events_before = (
            await session.execute(select(AgentEvent).where(AgentEvent.task_id == task_id))
        ).scalars().all()
    assert checkpoints and events_before

    env.mock.reset([{"content": "已按审批写入配置"}, {"content": "完成"}])
    resumed = await runtime.resume(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=adapter,
            root=workspace,
            title="写入配置",
            goal="写入 .env 配置",
            acceptance_criteria=["审批后可继续"],
            max_steps=4,
            approved_tools={"apply_patch"},
        ),
        decision=True,
    )
    assert (workspace / ".env").read_text(encoding="utf-8") == "TOKEN=plain"
    assert resumed["task"]["status"] in ("succeeded", "waiting_approval")
    assert resumed["changes"]["files"] == [".env"]
    async with get_session_factory()() as session:
        second = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()
    assert second.status == resumed["task"]["status"]
