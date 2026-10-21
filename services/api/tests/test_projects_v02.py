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


async def test_activate_workspace_switches_exactly_one(client, workspace, tmp_path):
    """切目录是"换一个"，不是"多一个"：任何时候只能有一个活动工作区。

    允许两个同时活动的话，快照回滚就不知道该往哪个根写——这是这套数据结构里
    唯一不能含糊的地方。
    """
    pid = (await _create_project(client))["id"]
    first = (await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})).json()
    second_root = tmp_path / "repo2"
    second_root.mkdir()
    second = (
        await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(second_root)})
    ).json()
    # 第二次绑定不自动抢活动位：用户可能只是想先挂上来看文件。
    assert first["is_active"] is True
    assert second["is_active"] is False

    switched = await client.post(f"/api/v1/projects/{pid}/workspaces/{second['id']}/activate")
    assert switched.status_code == 200
    flags = {w["id"]: w["is_active"] for w in switched.json()["workspaces"]}
    assert flags == {first["id"]: False, second["id"]: True}

    # 切过去之后，文件树读的就是新根。
    tree = await client.get(f"/api/v1/projects/{pid}/tree")
    assert tree.json()["entries"] == []


async def test_unbind_active_workspace_hands_over(client, workspace, tmp_path):
    """解掉活动目录要补选一个，否则界面上是"0 个活动"、任务却在按第一个兜底跑。"""
    pid = (await _create_project(client))["id"]
    first = (await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})).json()
    second_root = tmp_path / "repo2"
    second_root.mkdir()
    second = (
        await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(second_root)})
    ).json()

    assert (
        await client.delete(f"/api/v1/projects/{pid}/workspaces/{first['id']}")
    ).status_code == 204
    left = (await client.get(f"/api/v1/projects/{pid}")).json()["workspaces"]
    assert [w["id"] for w in left] == [second["id"]]
    assert left[0]["is_active"] is True


async def test_reveal_path_is_opt_in(client, workspace):
    """列表接口给打码路径，真路径只在用户主动点开时单独取。"""
    pid = (await _create_project(client))["id"]
    bound = (await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})).json()
    assert bound["path"].startswith("<workspace:")  # 默认不外泄主机路径

    revealed = await client.get(f"/api/v1/projects/{pid}/workspaces/{bound['id']}/path")
    assert revealed.status_code == 200
    assert revealed.json()["path"] == str(workspace)
