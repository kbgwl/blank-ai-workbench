"""阶段7 关键权限路径的自动化测试：令牌、路径越权、主机路径遮蔽、密钥不外泄。"""

import httpx
import pytest

from blank_api.app import create_app
from blank_api.config import get_settings
from blank_api.db import Base, get_engine

PUBLIC_PATHS = [
    ("GET", "/api/v1/projects"),
    ("POST", "/api/v1/projects"),
    ("GET", "/api/v1/projects/does-not-exist"),
    ("GET", "/api/v1/projects/does-not-exist/tree"),
    ("GET", "/api/v1/projects/does-not-exist/file?path=a.py"),
    ("GET", "/api/v1/providers"),
    ("POST", "/api/v1/providers"),
    ("GET", "/api/v1/providers/presets"),
    ("GET", "/api/v1/usage/summary"),
]


async def _tokened_app(monkeypatch, token: str):
    get_settings.cache_clear()
    monkeypatch.setenv("BLANK_API_TOKEN", token)
    monkeypatch.setenv("BLANK_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    app = create_app()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return app


@pytest.fixture
async def auth_client(monkeypatch):
    app = await _tokened_app(monkeypatch, "matrix-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer matrix-token"},
    ) as ac:
        yield ac
    get_settings.cache_clear()
    monkeypatch.delenv("BLANK_API_TOKEN", raising=False)
    monkeypatch.delenv("BLANK_DATABASE_URL", raising=False)
    get_settings.cache_clear()


async def test_every_route_group_requires_token(monkeypatch):
    app = await _tokened_app(monkeypatch, "matrix-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        for method, path in PUBLIC_PATHS:
            response = await ac.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 401, f"{method} {path} 未强制鉴权"
            assert response.json()["error"]["code"] == "unauthorized"
        health = await ac.get("/api/v1/health")
        assert health.status_code == 200


async def _bound_project(auth_client, workspace):
    project = (await auth_client.post("/api/v1/projects", json={"name": "perm"})).json()
    bound = await auth_client.post(
        f"/api/v1/projects/{project['id']}/workspaces", json={"path": str(workspace)}
    )
    assert bound.status_code == 201, bound.text
    return project


async def test_relative_traversal_denied(auth_client, workspace):
    project = await _bound_project(auth_client, workspace)
    for payload in ("../secret.txt", "src/../../secret.txt", "src/.././../../x"):
        response = await auth_client.get(
            f"/api/v1/projects/{project['id']}/tree", params={"path": payload}
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "path_escape_denied"


async def test_absolute_path_denied_on_files_and_bind(auth_client, workspace):
    project = await _bound_project(auth_client, workspace)
    absolute = str(workspace.parent / "outside.txt")
    file_response = await auth_client.get(
        f"/api/v1/projects/{project['id']}/file", params={"path": absolute}
    )
    assert file_response.status_code == 403
    assert file_response.json()["error"]["code"] == "path_escape_denied"

    bind = await auth_client.post(
        f"/api/v1/projects/{project['id']}/workspaces", json={"path": "relative/path"}
    )
    assert bind.status_code == 422
    assert bind.json()["error"]["code"] == "workspace_path_invalid"

    missing = await auth_client.post(
        f"/api/v1/projects/{project['id']}/workspaces", json={"path": str(workspace.parent / "nope")}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "workspace_not_found"


async def test_host_paths_stay_masked(auth_client, workspace):
    project = await _bound_project(auth_client, workspace)
    tree = (await auth_client.get(f"/api/v1/projects/{project['id']}/tree")).json()
    assert str(workspace) not in tree["root"]
    assert tree["root"].startswith("<workspace:")
    for entry in tree["entries"]:
        assert str(workspace) not in entry["path"]

    listed = (await auth_client.get("/api/v1/projects")).json()
    assert str(workspace) not in httpx.Response(200, json=listed).text


async def test_binary_and_oversized_reads_refused(auth_client, workspace):
    project = await _bound_project(auth_client, workspace)
    binary = (
        await auth_client.get(f"/api/v1/projects/{project['id']}/file", params={"path": "logo.png"})
    ).json()
    assert binary["binary"] is True
    assert binary["content"] == ""
    assert "二进制" in binary["message"]


async def test_api_keys_never_leave_provider_endpoints(auth_client):
    created = (
        await auth_client.post(
            "/api/v1/providers",
            json={
                "name": "leak-check",
                "kind": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "api_key": "sk-permission-matrix-secret",
            },
        )
    ).json()
    assert "sk-permission-matrix-secret" not in created["api_key_hint"]
    for method, path in (
        ("GET", "/api/v1/providers"),
        ("GET", f"/api/v1/providers/{created['id']}"),
        ("PATCH", f"/api/v1/providers/{created['id']}"),
        ("GET", "/api/v1/usage/summary"),
    ):
        response = await auth_client.request(method, path, json={} if method == "PATCH" else None)
        assert "sk-permission-matrix-secret" not in response.text, path


async def test_unknown_provider_kind_and_missing_resources(auth_client):
    bad_kind = await auth_client.post(
        "/api/v1/providers",
        json={"name": "x", "kind": "midjourney", "base_url": "https://x", "model": "m", "api_key": ""},
    )
    assert bad_kind.status_code == 422
    assert bad_kind.json()["error"]["code"] == "provider_kind_unsupported"

    missing_provider = await auth_client.get("/api/v1/providers/none")
    assert missing_provider.status_code == 404
    assert missing_provider.json()["error"]["code"] == "provider_not_found"

    missing_project = await auth_client.get("/api/v1/projects/none")
    assert missing_project.status_code == 404
    assert missing_project.json()["error"]["code"] == "project_not_found"

    unbound = await auth_client.post("/api/v1/projects", json={"name": "unbound"})
    tree = await auth_client.get(f"/api/v1/projects/{unbound.json()['id']}/tree")
    assert tree.status_code == 409
    assert tree.json()["error"]["code"] == "workspace_not_bound"


async def test_error_envelope_is_always_the_same_shape(auth_client):
    cases = [
        await auth_client.get("/api/v1/projects/none"),
        await auth_client.post("/api/v1/projects", json={"name": ""}),
        await auth_client.get("/api/v1/providers/none/test"),
    ]
    for response in cases:
        body = response.json()
        assert set(body) == {"error"}, body
        assert set(body["error"]) == {"code", "message"}
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]


async def test_secret_bearing_patch_needs_approval_and_stays_redacted(client, workspace):  # noqa: F811
    """阶段6/7：往源码里写明文密钥必须升级为高风险，且入库的 diff 不能带出密钥。"""
    from sqlalchemy import select

    from blank_api.db import get_session_factory
    from blank_api.models.task import FileChange
    from blank_api.services.tools import build_default_tools
    from blank_api.services.tools.base import ToolContext
    from blank_api.services.tools.gateway import ToolGateway

    secret = "sk-live-secret-1234567890"
    patch = f"--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@\n # demo project\n+API_KEY={secret}\n"
    gateway = ToolGateway(build_default_tools())
    assert gateway.get("apply_patch").resolve_risk({"path": "README.md", "patch": patch}) == "high"

    def deny(*_args):
        return False

    blocked = await gateway.execute(
        "apply_patch",
        {"path": "README.md", "patch": patch},
        ToolContext(
            root=workspace, task_id="secret-denied", session_factory=get_session_factory(), approval_policy=deny
        ),
        "execute_action",
    )
    assert blocked["status"] == "awaiting_approval", blocked
    assert secret not in (workspace / "README.md").read_text(encoding="utf-8"), "驳回后不得写入密钥"

    def allow(*_args):
        return True

    granted = await gateway.execute(
        "apply_patch",
        {"path": "README.md", "patch": patch},
        ToolContext(
            root=workspace, task_id="secret-allowed", session_factory=get_session_factory(), approval_policy=allow
        ),
        "execute_action",
    )
    assert granted["ok"] is True, granted
    assert secret not in str(granted["output"]) + str(granted["data"]), "工具回显与事件负载必须脱敏"
    assert secret in (workspace / "README.md").read_text(encoding="utf-8"), "批准写入的内容本身不改写"
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(FileChange).where(FileChange.task_id == "secret-allowed")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert secret not in rows[0].diff, "入库 diff 必须脱敏"
