"""S8：MCP 配置三作用域（user < project < local）与项目层批准闸门。

对齐 Claude Code 的两条语义：

* 同名服务端**整条覆盖**，不逐字段合并——半个新命令配半个旧参数没人解释得清；
* 项目层（``.mcp.json``）与本机层（``.blank/mcp.local.json``）的条目在批准前
  **不启动进程**。批准记的是整条定义的哈希：改一个字节、换一个工作区都会失效。

每一格都对应一个会真实发生的坏结局：克隆来的仓库带一条服务端就执行命令、
改完配置不生效、批准记录泄露密钥、坏掉的批准文件把一切当成已批准。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.mcp.approvals import (
    approval_key,
    approve,
    entry_digest,
    is_approved,
    load_approvals,
    revoke,
)
from blank_api.services.mcp.bridge import (
    LOCAL_MCP_FILE,
    PROJECT_MCP_FILE,
    MCPManager,
    manager,
    reset_manager,
    resolve_scoped_servers,
    scoped_entries,
    scoped_paths,
)
from tests.test_mcp_tools import _StubAdapter  # 复用装配桩，验证的是工具表不是模型

SERVER = Path(__file__).with_name("fake_mcp_server.py")
API = "/api/v1"


def _entry(mode: str = "normal", **extra) -> dict:
    return {"command": sys.executable, "args": [str(SERVER), mode], "timeout": 8, **extra}


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def approvals(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "mcp-approvals.json"
    monkeypatch.setenv("BLANK_MCP_APPROVALS_FILE", str(path))
    return path


@pytest.fixture()
def user_config(tmp_path: Path) -> Path:
    return tmp_path / "user-mcp.json"


async def _project(client, root: Path | None) -> str:
    project = (await client.post(f"{API}/projects", json={"name": "mcp-scope"})).json()
    if root is not None:
        bound = await client.post(
            f"{API}/projects/{project['id']}/workspaces", json={"path": str(root)}
        )
        assert bound.status_code == 201, bound.text
    return project["id"]


# --------------------------------------------------------------------------- #
# 1. 三层路径与整条覆盖
# --------------------------------------------------------------------------- #
def test_scoped_paths_use_claude_file_names(tmp_path: Path, user_config: Path):
    root = tmp_path / "repo"
    paths = dict(scoped_paths(root, user_config))
    assert paths["user"] == user_config
    assert paths["project"] == root / ".mcp.json"
    assert paths["local"] == root / ".blank" / "mcp.local.json"
    assert PROJECT_MCP_FILE == ".mcp.json" and LOCAL_MCP_FILE == Path(".blank") / "mcp.local.json"


def test_project_entry_overrides_whole_user_entry(
    tmp_path: Path, user_config: Path, approvals: Path
):
    """用户层 risk=low、timeout=5；项目层只给 command/args。

    整条覆盖意味着项目层赢，risk 必须回到默认 high——如果逐字段合并，
    仓库就能悄悄继承用户层「低风险、已放行」的判定，这是最危险的一种合并。
    """
    root = tmp_path / "repo"
    _write(user_config, {"fake": _entry("normal", risk="low", timeout=5)})
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("crash")})
    approve("project", root, "fake", _entry("crash"))

    servers, statuses, _ = resolve_scoped_servers(root, user_config)

    assert len(servers) == 1, "同名只能剩一条，否则工具名会打架"
    server = servers[0]
    assert server.scope == "project"
    assert server.args == (str(SERVER), "crash")
    assert server.risk == "high", "项目层没写 risk，不许继承用户层的 low"
    assert statuses == []


def test_local_layer_beats_project_layer(tmp_path: Path, user_config: Path, approvals: Path):
    root = tmp_path / "repo"
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    _write(root / LOCAL_MCP_FILE, {"fake": _entry("crash")})
    approve("local", root, "fake", _entry("crash"))

    servers, _, _ = resolve_scoped_servers(root, user_config)

    assert [server.args for server in servers] == [(str(SERVER), "crash")]
    assert servers[0].scope == "local"


def test_disabled_high_layer_shadows_lower_server(tmp_path: Path, user_config: Path):
    """高优先级层写 disabled 就是把同名服务端整条关掉，而不是"跳过这层看下一层"。"""
    root = tmp_path / "repo"
    _write(user_config, {"fake": _entry("normal")})
    _write(root / PROJECT_MCP_FILE, {"fake": {"disabled": True}})

    servers, statuses, _ = resolve_scoped_servers(root, user_config)

    assert servers == [], "项目层明确禁用，用户层同名条目不许复活"
    assert [(item.name, item.state, item.scope) for item in statuses] == [
        ("fake", "skipped", "project")
    ]
    assert "已禁用" in statuses[0].error, "这条是被显式关掉的，不是配置写错了"


# --------------------------------------------------------------------------- #
# 2. 批准闸门
# --------------------------------------------------------------------------- #
def test_unapproved_project_server_never_starts(tmp_path: Path, user_config: Path, approvals: Path):
    """用 crash 模式做反证：若闸门放行，状态会是 failed + MISSING_API_KEY，而不是 pending。"""
    root = tmp_path / "repo"
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("crash")})
    _write(user_config, {})

    servers, statuses, _ = resolve_scoped_servers(root, user_config)

    assert servers == []
    assert len(statuses) == 1
    status = statuses[0]
    assert (status.state, status.scope, status.approval) == ("skipped", "project", "pending")
    assert "还没批准" in status.error, "原因要说清是被批准闸门拦下，而不是连接失败"


def test_user_layer_never_needs_approval(tmp_path: Path, user_config: Path, approvals: Path):
    _write(user_config, {"fake": _entry("normal")})

    servers, _, _ = resolve_scoped_servers(None, user_config)

    assert len(servers) == 1 and servers[0].scope == "user"


async def test_approving_the_exact_definition_starts_it(
    tmp_path: Path, user_config: Path, approvals: Path
):
    root = tmp_path / "repo"
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    _write(user_config, {})
    approve("project", root, "fake", _entry("normal"))

    manager_ = MCPManager(user_config)
    try:
        bundle = await manager_.bundle(root)
    finally:
        await manager_.close()

    assert len(bundle.tools) == 4
    status = bundle.statuses[0]
    assert (status.state, status.scope, status.approval) == ("ready", "project", "approved")


async def test_editing_the_definition_drops_back_to_pending(
    tmp_path: Path, user_config: Path, approvals: Path
):
    """批准绑定的是整条定义：只改 args 一个元素也要重新批准。"""
    root = tmp_path / "repo"
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    _write(user_config, {})
    approve("project", root, "fake", _entry("normal"))

    manager_ = MCPManager(user_config)
    try:
        assert len((await manager_.bundle(root)).tools) == 4
        # 定义改了：同一秒内 mtime 可能不变，但 size 变了；stamp 会触发重读
        _write(root / PROJECT_MCP_FILE, {"fake": _entry("crash")})
        bundle = await manager_.bundle(root)
    finally:
        await manager_.close()

    assert bundle.tools == []
    assert bundle.statuses[0].approval == "pending"


def test_approval_is_bound_to_the_workspace(tmp_path: Path, user_config: Path, approvals: Path):
    entry = _entry("normal")
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    _write(root_a / PROJECT_MCP_FILE, {"fake": entry})
    _write(root_b / PROJECT_MCP_FILE, {"fake": entry})
    approve("project", root_a, "fake", entry)

    servers_a, _, _ = resolve_scoped_servers(root_a, user_config)
    servers_b, statuses_b, _ = resolve_scoped_servers(root_b, user_config)

    assert len(servers_a) == 1
    assert servers_b == [] and statuses_b[0].approval == "pending", "另一个仓库不能被顺带放行"


def test_approval_file_stores_hashes_not_secrets(
    tmp_path: Path, user_config: Path, approvals: Path
):
    entry = _entry("normal", env={"BLANK_TEST_SECRET": "sk-live-do-not-store-me"})
    approve("project", tmp_path / "repo", "fake", entry, path=approvals)

    text = approvals.read_text(encoding="utf-8")
    assert "sk-live-do-not-store-me" not in text, "批准文件不许复制 env/headers 里的密钥"
    stored = json.loads(text)["approvals"]
    assert stored[approval_key("project", tmp_path / "repo", "fake")]["digest"] == entry_digest(entry)


def test_approval_digest_binds_values_not_key_order():
    assert entry_digest({"b": 1, "a": 2}) == entry_digest({"a": 2, "b": 1})
    assert entry_digest({"command": "x", "args": ["a"]}) != entry_digest(
        {"command": "x", "args": ["b"]}
    )


def test_corrupt_approvals_file_means_nothing_is_approved(
    tmp_path: Path, user_config: Path, approvals: Path
):
    """fail-closed：截断的批准文件必须等价于"什么都没批准"，而不是"全都批准"。"""
    approvals.write_text("{ 这不是 json", encoding="utf-8")
    root = tmp_path / "repo"
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("normal")})

    servers, statuses, _ = resolve_scoped_servers(root, user_config)

    assert load_approvals(approvals) == {}
    assert servers == [] and statuses[0].approval == "pending"


def test_revoke_takes_effect_immediately(tmp_path: Path, user_config: Path, approvals: Path):
    root = tmp_path / "repo"
    entry = _entry("normal")
    _write(root / PROJECT_MCP_FILE, {"fake": entry})
    approve("project", root, "fake", entry)
    assert is_approved("project", root, "fake", entry)

    assert revoke("project", root, "fake") is True
    servers, statuses, _ = resolve_scoped_servers(root, user_config)
    assert servers == [] and statuses[0].approval == "pending"
    assert revoke("project", root, "fake") is False


def test_approving_a_user_entry_is_rejected(tmp_path: Path, approvals: Path):
    with pytest.raises(ValueError):
        approve("user", tmp_path, "fake", _entry())


# --------------------------------------------------------------------------- #
# 3. 坏文件与缓存
# --------------------------------------------------------------------------- #
def test_broken_project_file_does_not_take_down_user_layer(
    tmp_path: Path, user_config: Path, approvals: Path
):
    root = tmp_path / "repo"
    _write(user_config, {"fake": _entry("normal")})
    root.mkdir(parents=True, exist_ok=True)
    (root / PROJECT_MCP_FILE).write_text("{ 坏掉的 json", encoding="utf-8")

    servers, statuses, _ = resolve_scoped_servers(root, user_config)

    assert len(servers) == 1 and servers[0].scope == "user", "项目层坏了不能连用户层一起丢"
    broken = [item for item in statuses if item.state == "failed"]
    assert broken and broken[0].scope == "project" and "解析失败" in broken[0].error


async def test_approval_takes_effect_without_restart(
    tmp_path: Path, user_config: Path, approvals: Path
):
    """批准文件也是 stamp 的一部分：批准后下一次 bundle 就该看到工具。"""
    root = tmp_path / "repo"
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("normal")})

    manager_ = MCPManager(user_config)
    try:
        assert (await manager_.bundle(root)).tools == []
        approve("project", root, "fake", _entry("normal"))
        bundle = await manager_.bundle(root)
    finally:
        await manager_.close()

    assert len(bundle.tools) == 4, "批准后不必重启进程或手动 reload"


async def test_manager_keeps_one_bucket_per_workspace(
    tmp_path: Path, user_config: Path, approvals: Path
):
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    _write(root_a / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    _write(root_b / PROJECT_MCP_FILE, {"other": _entry("crash")})
    approve("project", root_a, "fake", _entry("normal"))
    approve("project", root_b, "other", _entry("crash"))

    manager_ = MCPManager(user_config)
    try:
        bundle_a = await manager_.bundle(root_a)
        bundle_b = await manager_.bundle(root_b)
        again_a = await manager_.bundle(root_a)
    finally:
        await manager_.close()

    assert len(bundle_a.tools) == 4
    assert bundle_b.tools == [] and bundle_b.statuses[0].name == "other"
    assert again_a is bundle_a, "回到原工作区应命中原来的桶，而不是被另一个仓库顶掉"


def test_manager_without_root_reads_only_user_layer(tmp_path: Path, user_config: Path):
    _write(user_config, {"fake": _entry("normal")})

    servers, _, scopes = resolve_scoped_servers(None, user_config)

    assert [server.scope for server in servers] == ["user"]
    assert set(scopes) == {"user"}, "没有工作区就没有项目层/本机层"


def test_scoped_entries_reports_winning_layer(tmp_path: Path, user_config: Path):
    root = tmp_path / "repo"
    _write(user_config, {"fake": _entry("normal")})
    _write(root / PROJECT_MCP_FILE, {"fake": _entry("crash")})

    entries = scoped_entries(root, user_config)

    assert set(entries) == {"fake"}
    scope, definition = entries["fake"]
    assert scope == "project" and definition["args"][-1] == "crash"


# --------------------------------------------------------------------------- #
# 4. HTTP：项目选择、批准、撤销、拒绝
# --------------------------------------------------------------------------- #
async def test_status_endpoint_reads_the_selected_workspace(
    client, workspace: Path, tmp_path: Path, user_config: Path, approvals, monkeypatch
):
    monkeypatch.setenv("BLANK_MCP_FILE", str(user_config))
    _write(user_config, {})
    _write(workspace / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    project_id = await _project(client, workspace)
    reset_manager()
    try:
        body = (await client.get(f"{API}/settings/mcp?project_id={project_id}")).json()
    finally:
        await manager().close()
        reset_manager()

    assert body["workspace"] is True
    assert set(body["scopes"]) == {"user", "project", "local"}
    assert body["scopes"]["project"] == {"path": ".mcp.json", "exists": True}
    assert body["tools"] == 0
    assert body["servers"][0]["scope"] == "project"
    assert body["servers"][0]["approval"] == "pending"


async def test_http_approve_and_revoke_change_the_status(
    client, workspace: Path, tmp_path: Path, user_config: Path, approvals, monkeypatch
):
    monkeypatch.setenv("BLANK_MCP_FILE", str(user_config))
    _write(user_config, {})
    _write(workspace / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    project_id = await _project(client, workspace)
    reset_manager()
    try:
        approved = await client.post(
            f"{API}/settings/mcp/approvals",
            json={"project_id": project_id, "scope": "project", "name": "fake"},
        )
        assert approved.status_code == 200, approved.text
        body = (await client.get(f"{API}/settings/mcp?project_id={project_id}")).json()
        assert body["tools"] == 4 and body["servers"][0]["state"] == "ready"
        assert body["servers"][0]["approval"] == "approved"

        revoked = await client.delete(
            f"{API}/settings/mcp/approvals/fake",
            params={"scope": "project", "project_id": project_id},
        )
        assert revoked.status_code == 200 and revoked.json()["revoked"] is True
        body = (await client.get(f"{API}/settings/mcp?project_id={project_id}")).json()
    finally:
        await manager().close()
        reset_manager()

    assert body["tools"] == 0 and body["servers"][0]["approval"] == "pending"


async def test_http_approve_rejects_user_scope_unknown_and_shadowed(
    client, workspace: Path, user_config: Path, approvals, monkeypatch
):
    monkeypatch.setenv("BLANK_MCP_FILE", str(user_config))
    _write(user_config, {"fake": _entry("normal")})
    project_id = await _project(client, workspace)
    reset_manager()
    try:
        base = f"{API}/settings/mcp/approvals"
        user_scope = await client.post(
            base, json={"project_id": project_id, "scope": "user", "name": "fake"}
        )
        unknown = await client.post(
            base, json={"project_id": project_id, "scope": "project", "name": "nope"}
        )
        shadowed = await client.post(
            base, json={"project_id": project_id, "scope": "project", "name": "fake"}
        )
    finally:
        await manager().close()
        reset_manager()

    assert user_scope.status_code == 400 and user_scope.json()["error"]["code"] == "invalid_scope"
    assert unknown.status_code == 404
    assert shadowed.status_code == 409, "用户层条目当前生效，批准项目层的同名定义不会生效"
    assert shadowed.json()["error"]["code"] == "scope_overridden"


async def test_http_project_without_workspace_has_no_repo_layers(
    client, tmp_path: Path, user_config: Path, approvals, monkeypatch
):
    monkeypatch.setenv("BLANK_MCP_FILE", str(user_config))
    _write(user_config, {})
    project_id = await _project(client, None)
    reset_manager()
    try:
        body = (await client.get(f"{API}/settings/mcp?project_id={project_id}")).json()
        approved = await client.post(
            f"{API}/settings/mcp/approvals",
            json={"project_id": project_id, "scope": "project", "name": "fake"},
        )
    finally:
        await manager().close()
        reset_manager()

    assert body["workspace"] is False and set(body["scopes"]) == {"user"}
    assert approved.status_code == 400
    assert approved.json()["error"]["code"] == "workspace_required"


# --------------------------------------------------------------------------- #
# 5. 运行时接线：任务必须按自己的工作区读配置
# --------------------------------------------------------------------------- #
async def test_runtime_sees_approved_project_tools(
    workspace: Path, tmp_path: Path, user_config: Path, approvals, monkeypatch
):
    monkeypatch.setenv("BLANK_MCP_FILE", str(user_config))
    _write(user_config, {})
    _write(workspace / PROJECT_MCP_FILE, {"fake": _entry("normal")})
    approve("project", workspace, "fake", _entry("normal"))
    reset_manager()

    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-mcp-scope",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="看看项目层工具接上没有",
    )
    try:
        scope = await runtime._prepare(request, "task-mcp-scope:1")
    finally:
        await manager().close()
        reset_manager()

    assert "mcp__fake__echo" in scope.session.gateway.tools
    assert scope.mcp["tools"] == 4
    assert scope.mcp["servers"][0]["scope"] == "project"


async def test_runtime_does_not_start_unapproved_project_server(
    workspace: Path, tmp_path: Path, user_config: Path, approvals, monkeypatch
):
    """项目层没批准时运行时拿不到任何工具；crash 模式还能反证进程根本没起。"""
    monkeypatch.setenv("BLANK_MCP_FILE", str(user_config))
    _write(user_config, {})
    _write(workspace / PROJECT_MCP_FILE, {"fake": _entry("crash")})
    reset_manager()

    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-mcp-pending",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="不该起进程",
    )
    try:
        scope = await runtime._prepare(request, "task-mcp-pending:1")
    finally:
        await manager().close()
        reset_manager()

    assert scope.mcp["tools"] == 0
    dumped = json.dumps(scope.mcp, ensure_ascii=False)
    assert "MISSING_API_KEY" not in dumped, "待批准的服务端不许被启动"
    assert scope.mcp["servers"][0]["approval"] == "pending"
