"""最小「输入 -> 返回」冒烟脚本：不启动服务、不依赖 Docker，直接进程内打 GET /api/v1/health。

用法（在仓库根目录执行，由 uv 提供依赖环境）：
    uv run --project services/api python scripts/smoke_health.py

退出码：0 表示拿到期望的 200 + status=ok；非 0 表示失败（可直接接进 CI 或本地核查）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

# 与 tests/conftest.py 保持同一套隔离约定：内存库 + 沙箱关闭 + 笔记/记忆/设置落临时目录，
# 保证脚本不读写仓库里的 data/，也让结果可重复。
os.environ.setdefault("BLANK_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("BLANK_SANDBOX_ENABLED", "0")
os.environ.setdefault("BLANK_NOTES_DIR", tempfile.mkdtemp(prefix="blank-notes-"))
os.environ.setdefault("BLANK_MEMORY_DIR", tempfile.mkdtemp(prefix="blank-memory-"))
os.environ.setdefault(
    "BLANK_SETTINGS_FILE", f"{tempfile.mkdtemp(prefix='blank-settings-')}/settings.json"
)

import httpx  # noqa: E402

from blank_api.app import create_app  # noqa: E402
from blank_api.db import Base, get_engine  # noqa: E402


async def fetch_health() -> tuple[int, dict]:
    """进程内发一次 GET /api/v1/health，返回 (状态码, 响应体)。"""
    app = create_app()
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as client:
            resp = await client.get("/api/v1/health")
        return resp.status_code, resp.json()
    finally:
        # 不 dispose 会在解释器退出时留下 aiosqlite 的 ResourceWarning
        await engine.dispose()


async def main() -> int:
    try:
        status_code, body = await fetch_health()
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本要把失败原因原样打出来
        print(f"FAIL: 请求 /api/v1/health 时抛异常：{exc!r}", file=sys.stderr)
        return 1

    print(f"GET /api/v1/health -> {status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    if status_code != 200:
        print(f"FAIL: 期望 200，实际 {status_code}", file=sys.stderr)
        return 1
    if body.get("status") != "ok" or body.get("database") != "connected":
        print(f"FAIL: 期望 status=ok / database=connected，实际 {body}", file=sys.stderr)
        return 1

    print("OK: 输入有返回，健康检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))