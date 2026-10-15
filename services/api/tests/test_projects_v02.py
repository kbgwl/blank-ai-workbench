import uuid

from sqlalchemy import func, select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task, ToolCall


async def _create_project(client, name="demo"):
    resp = await client.post("/api/v1/projects", json={"name": name, "description": "d"})
    assert resp.status_code == 201
    return resp.json()


async def test_project_crud(client, workspace):
    created = await _create_project(client, "首个项目")
    pid = created["id"]

    listed = (await client.get("/api/v1/projects")).json()
    assert [p["id"] for p in listed] == [pid]
    assert listed[0]["name"] == "首个项目"

    patched = await client.patch(f"/api/v1/projects/{pid}", json={"name": "改名后"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "改名后"

    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    assert (await client.get(f"/api/v1/projects/{pid}")).status_code == 404


async def test_bind_valid_workspace_and_tree(client, workspace):
    pid = (await _create_project(client))["id"]
    bound = await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})
    assert bound.status_code == 201
    assert bound.json()["is_active"] is True

    tree = await client.get(f"/api/v1/projects/{pid}/tree")
    assert tree.status_code == 200
    names = [e["name"] for e in tree.json()["entries"]]
    assert "README.md" in names and "src" in names and "core.py" in names
    assert ".git" not in names  # 忽略目录不展示


async def test_rejects_relative_missing_and_escape(client, workspace):
    pid = (await _create_project(client))["id"]
    bad = await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": "relative/dir"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "workspace_path_invalid"

    missing = await client.post(
        f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace / "nope")}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "workspace_not_found"

    await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})
    escape = await client.get(f"/api/v1/projects/{pid}/file", params={"path": "../README.md"})
    assert escape.status_code == 403
    assert escape.json()["error"]["code"] == "path_escape_denied"

    absolute = await client.get(f"/api/v1/projects/{pid}/file", params={"path": str(workspace / "main.py")})
    assert absolute.status_code == 403

    outside = await client.get(f"/api/v1/projects/{pid}/file", params={"path": "main.py/../../etc/passwd"})
    assert outside.status_code == 403


async def test_read_text_and_binary(client, workspace):
    pid = (await _create_project(client))["id"]
    await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})

    text = await client.get(f"/api/v1/projects/{pid}/file", params={"path": "src/core.py"})
    assert text.status_code == 200
    body = text.json()
    assert body["binary"] is False
    assert body["content"].strip() == "VALUE = 1"

    binary = await client.get(f"/api/v1/projects/{pid}/file", params={"path": "logo.png"})
    assert binary.status_code == 200
    assert binary.json()["binary"] is True
    assert binary.json()["content"] == ""
    assert "二进制" in binary.json()["message"]


async def test_unbound_project_tree_conflict(client):
    pid = (await _create_project(client))["id"]
    resp = await client.get(f"/api/v1/projects/{pid}/tree")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "workspace_not_bound"


async def test_workspace_path_not_exposed_by_default(client, workspace):
    pid = (await _create_project(client))["id"]
    bound = await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})
    assert str(workspace) not in bound.text
    assert bound.json()["path"].startswith("<workspace:")
    tree = await client.get(f"/api/v1/projects/{pid}/tree")
    assert str(workspace) not in tree.text

async def test_delete_project_removes_tasks_and_traces(client, workspace):
    pid = (await _create_project(client, "带任务的项目"))["id"]
    await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})
    task_id = (
        await client.post(
            f"/api/v1/projects/{pid}/tasks",
            json={"title": "留下痕迹", "goal": "留下痕迹", "acceptance_criteria": []},
        )
    ).json()["id"]
    async with get_session_factory()() as session:
        session.add(
            ToolCall(
                id=str(uuid.uuid4()),
                task_id=task_id,
                node="execute_action",
                name="read_file",
                arguments={"path": "README.md"},
                result_summary="ok",
                status="ok",
                risk_level="low",
                duration_ms=1,
            )
        )
        session.add(
            AgentEvent(
                id=str(uuid.uuid4()),
                task_id=task_id,
                seq=999,
                node="execute_action",
                type="tool.completed",
                payload={"tool": "read_file"},
            )
        )
        await session.commit()

    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204

    async with get_session_factory()() as session:
        assert await session.get(Task, task_id) is None
        for model in (ToolCall, AgentEvent):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.task_id == task_id)
            )
            assert count == 0, f"{model.__name__} 仍残留 {count} 条孤儿记录"
    assert (await client.get("/api/v1/tasks")).json() == []
