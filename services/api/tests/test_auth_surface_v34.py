"""鉴权边界的可重放门禁：把「每一条接口都要令牌」从一条抽查变成全量判定。

`tests/test_auth_v02.py` 只盯了 `/api/v1/projects` 一条路，而护栏挂在
`include_router` 的 `dependencies` 上、共 10 个路由模块 —— 少挂一个，旧测试照样全绿。
这一格改按 `/openapi.json` 真枚举，逐条匿名打一遍。
"""

import httpx
import pytest

from blank_api.app import create_app
from blank_api.config import get_settings
from blank_api.db import Base, get_engine

# 设计上就匿名可读的公共面；任何别的路径落进「匿名不是 401」都算漏网。
PUBLIC_OPERATIONS = {"/api/v1/health"}
DOC_SURFACE = ("/openapi.json", "/docs", "/redoc")
METHODS = ("get", "post", "put", "patch", "delete")


async def _client(app):
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30.0)


def _operations(spec: dict) -> list[tuple[str, str]]:
    """展开成 (method, path) 清单。

    这里刻意不接受"空清单"：枚举不出任何 operation 就是装置失效，
    绝不能把它读成"没有需要鉴权的接口"。
    """
    ops: list[tuple[str, str]] = []
    for path, item in (spec.get("paths") or {}).items():
        for method in ("get", "post", "put", "patch", "delete"):
            if method in item:
                ops.append((method, path))
    assert ops, "openapi 里一条 operation 都没有，判定装置本身失效"
    return ops


def _concrete(path: str) -> str:
    """路径参数一律填占位值：鉴权依赖在参数校验之前抛 401，占位值不影响这一条。"""
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        out = out[:start] + "probe-id" + out[end + 1 :]
    return out


@pytest.fixture
def token_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("BLANK_API_TOKEN", "gate-token")
    try:
        yield
    finally:
        monkeypatch.delenv("BLANK_API_TOKEN", raising=False)
        get_settings.cache_clear()


async def test_every_operation_rejects_anonymous(token_env):
    """全量逐条：非公共接口匿名一律 401，漏挂 `require_token` 的路由当场现形。"""
    app = create_app()
    async with await _client(app) as ac:
        headers = {"Authorization": "Bearer gate-token"}
        spec = (await ac.get("/openapi.json", headers=headers)).json()
        operations = _operations(spec)
        # 装置自检：枚举不许漏掉任何一种方法声明，否则"全量"只是漏掉的那几种的全量
        paths = spec["paths"]
        stray = {method for item in paths.values() for method in item if method not in METHODS}
        assert not stray, f"openapi 里出现了没被枚举的方法：{sorted(stray)}"
        assert {p for _, p in operations} == set(paths), "展开出的路径集合与接口面不一致"

        holes: list[str] = []
        broken: list[str] = []
        for method, path in operations:
            if path in PUBLIC_OPERATIONS:
                continue
            response = await ac.request(method, _concrete(path))
            if response.status_code == 401:
                continue
            if response.status_code in {400, 404, 422}:
                # 说明请求穿过了鉴权：这一条是真漏网，但要与"占位值形状不对"分开念
                holes.append(f"{method.upper()} {path} -> {response.status_code}")
            else:
                broken.append(f"{method.upper()} {path} -> {response.status_code}")

        assert holes == [], f"匿名可达（漏挂鉴权）：{holes}"
        assert broken == [], f"匿名既没被拒也不是预期形状：{broken}"


async def test_public_health_stays_anonymous(token_env):
    app = create_app()
    async with await _client(app) as ac:
        assert (await ac.get("/api/v1/health")).status_code == 200


async def test_wrong_token_is_rejected_without_leaking_shape(token_env):
    """错令牌与匿名同码同形状，且绝对拿不到接口面。"""
    app = create_app()
    async with await _client(app) as ac:
        wrong = await ac.get("/api/v1/projects", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "unauthorized"
        surface = await ac.get("/openapi.json", headers={"Authorization": "Bearer nope"})
        assert surface.status_code == 401
        ok = await ac.get("/api/v1/projects", headers={"Authorization": "Bearer gate-token"})
        assert ok.status_code == 200


async def test_api_surface_is_not_anonymous_when_token_is_set(token_env):
    """这一格是 #53 量出来的真漏：FastAPI 把文档三条挂在鉴权之外。

    接口面本身等于整份路由表 + 参数形状 + 错误码，Swagger UI 还是照着点的清单，
    所以配了令牌时三条都必须走同一个 `require_token`，带令牌时又要真的能看。
    """
    app = create_app()
    async with await _client(app) as ac:
        anonymous = {path: (await ac.get(path)).status_code for path in DOC_SURFACE}
        assert anonymous == {"/openapi.json": 401, "/docs": 401, "/redoc": 401}, anonymous

        headers = {"Authorization": "Bearer gate-token"}
        with_token = {path: (await ac.get(path, headers=headers)) for path in DOC_SURFACE}
        assert all(response.status_code == 200 for response in with_token.values()), {
            path: response.status_code for path, response in with_token.items()
        }
        # 挂回来的 openapi 必须是同一份内容，不是为了挡访问而伪造一个空壳
        spec = with_token["/openapi.json"].json()
        assert _operations(spec), "带令牌读到的接口面是空的"


async def test_local_owner_shape_without_token(monkeypatch):
    """没配令牌 = 本机单用户形态：接口面照旧匿名可读，一条都不许变成 401。

    这条与上一条互为对照 —— 只写"配了令牌要挡"，很容易把桌面版做成连自己人都挡。
    """
    get_settings.cache_clear()
    monkeypatch.delenv("BLANK_API_TOKEN", raising=False)
    app = create_app()
    async with await _client(app) as ac:
        for path in DOC_SURFACE:
            assert (await ac.get(path)).status_code == 200, path
        assert (await ac.get("/api/v1/projects")).status_code == 200
