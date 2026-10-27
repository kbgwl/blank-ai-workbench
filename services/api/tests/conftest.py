import json
import os
import tempfile
from pathlib import Path

# 用临时文件而不是 :memory:：内存库只能靠 StaticPool 共用一条连接，后台任务
# （团队编排、子代理）与请求处理并发时事务会互相踩。文件库每条会话拿独立连接，
# 与生产同构；路径随测试临时目录清理。
_TEST_DB = (Path(tempfile.mkdtemp(prefix="blank-db-")) / "test.db").as_posix()
os.environ.setdefault("BLANK_DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB}")
# 默认让命令在宿主机跑，保持测试夹具与 Docker 无关；沙箱路由由 tests/test_sandbox_v07.py 用真实镜像验证。
os.environ.setdefault("BLANK_SANDBOX_ENABLED", "0")
# 交接笔记与长期记忆落到临时目录：跑测试不该在仓库里留 data/notes、data/memory
os.environ.setdefault("BLANK_NOTES_DIR", tempfile.mkdtemp(prefix="blank-notes-"))
os.environ.setdefault("BLANK_MEMORY_DIR", tempfile.mkdtemp(prefix="blank-memory-"))
# 运行时设置也落临时目录：测试绝不能读到或写进仓库里的 data/settings.json
os.environ.setdefault("BLANK_SETTINGS_FILE", f"{tempfile.mkdtemp(prefix='blank-settings-')}/settings.json")
# 落盘的审批前缀规则同理：一次测试留下的 approvals.json 会改变后续用例的判定。
# 具体用例可再用 monkeypatch 指向自己的 tmp_path，做跨任务的隔离验证。
os.environ.setdefault("BLANK_APPROVALS_FILE", f"{tempfile.mkdtemp(prefix='blank-approvals-')}/approvals.json")
# 子代理的隔离副本也放测试自己的目录：不往系统临时目录里堆，跑完自动随 basetemp 清理
os.environ.setdefault("BLANK_WORKTREE_ROOT", tempfile.mkdtemp(prefix="blank-worktrees-"))
# 工作区快照同样落临时目录：默认路径是仓库里的 data/snapshots。
os.environ.setdefault("BLANK_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="blank-snapshots-"))
# 工具调用的审计账也落临时目录：每次网关调用都写一条 JSONL，留在仓库里就是脏数据
os.environ.setdefault("BLANK_AUDIT_DIR", tempfile.mkdtemp(prefix="blank-audit-"))

# 安全策略的产品默认是「禁止执行命令 + 每条命令都要确认」，那是给客户装机用的。
# 测试要验证的恰恰是命令能跑通、审批能流转，所以这里把夹具调成宽松档 ——
# 但只放宽这两项：内置禁止、路径边界、联网开关都保留原语义，
# 否则一堆测试会绕过真正要守的边界。策略本身的默认值由 test_safety_policy_v050.py 断言。
_SETTINGS_FIXTURE = os.environ["BLANK_SETTINGS_FILE"]
if not os.path.exists(_SETTINGS_FIXTURE):
    os.makedirs(os.path.dirname(_SETTINGS_FIXTURE), exist_ok=True)
    with open(_SETTINGS_FIXTURE, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "safety": {
                    "shell_enabled": True,
                    "network_enabled": True,
                    "confirm_all_commands": False,
                    "restrict_to_workspace": True,
                }
            },
            handle,
        )

import httpx  # noqa: E402
import pytest  # noqa: E402

from blank_api.app import create_app  # noqa: E402
from blank_api.db import Base, get_engine  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_approvals_file(tmp_path, monkeypatch):
    """每个用例一份空的审批规则：上个用例批准过的前缀不该改变下个用例的判定。"""
    monkeypatch.setenv("BLANK_APPROVALS_FILE", str(tmp_path / "approvals.json"))


@pytest.fixture(autouse=True)
def _isolate_snapshot_dir(tmp_path, monkeypatch):
    """每个用例一份快照目录：整树恢复会覆盖工作区，不能和别的用例共享落盘状态。"""
    monkeypatch.setenv("BLANK_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


@pytest.fixture(autouse=True)
def _isolate_permission_rules(tmp_path, monkeypatch):
    """每个用例一份手写规则文件：仓库里或上个用例留下的 allow/deny 会改变判定。"""
    monkeypatch.setenv("BLANK_PERMISSION_RULES", str(tmp_path / "permissions.rules"))


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
