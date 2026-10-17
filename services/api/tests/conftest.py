import os
import tempfile

os.environ.setdefault("BLANK_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
# 默认让命令在宿主机跑，保持测试夹具与 Docker 无关；沙箱路由由 tests/test_sandbox_v07.py 用真实镜像验证。
os.environ.setdefault("BLANK_SANDBOX_ENABLED", "0")
# 交接笔记与长期记忆落到临时目录：跑测试不该在仓库里留 data/notes、data/memory
os.environ.setdefault("BLANK_NOTES_DIR", tempfile.mkdtemp(prefix="blank-notes-"))
os.environ.setdefault("BLANK_MEMORY_DIR", tempfile.mkdtemp(prefix="blank-memory-"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from blank_api.app import create_app  # noqa: E402
from blank_api.db import Base, get_engine  # noqa: E402


@pytest.fixture
async def client():
    app = create_app()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# demo project\n", encoding="utf-8")
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    pkg = root / "src"
    pkg.mkdir()
    (pkg / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("hidden\n", encoding="utf-8")
    return root
