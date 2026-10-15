"""阶段6 沙箱与安全加固验收。需要 Docker；没有 Docker 时整组跳过并报告受阻。"""

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from blank_api.services.sandbox import (
    AuditLog,
    AuditRecord,
    DockerSandbox,
    LocalSandbox,
    SandboxPolicy,
    SnapshotStore,
    cleanup_containers,
    get_sandbox,
)

DOCKER = shutil.which("docker")
pytestmark = pytest.mark.skipif(DOCKER is None, reason="本机没有 docker，阶段6 验收受阻")

SECRET = "sk-live-secret-never-in-sandbox"


@pytest.fixture(scope="module", autouse=True)
def sandbox_image_ready():
    """用真实 runner 参数探活：镜像缺失或参数不被本机 docker 接受时整组跳过，
    避免 docker 报错退出被误判成「拦截成功」。"""
    workspace = tempfile.mkdtemp()
    probe = asyncio.run(DockerSandbox(_policy(Path(workspace))).run("echo ok", workspace=workspace))
    if "ok" not in probe.stdout:
        pytest.skip(f"沙箱不可用：{(probe.stderr or probe.stdout)[:200]}")


def _policy(tmp_path: Path, **overrides) -> SandboxPolicy:
    policy = SandboxPolicy()
    policy.image = "blank-sandbox:dev"
    policy.enabled = True
    policy.allow_network = False
    policy.timeout_seconds = 25
    policy.memory = "64m"
    policy.cpus = 0.4
    policy.pids = 64
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


async def test_command_runs_and_writes_only_inside_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sandbox = DockerSandbox(_policy(tmp_path))
    result = await sandbox.run(
        "python3 -c \"open('inside.txt','w').write('ok')\"", workspace=str(workspace)
    )
    assert result.ok, result.output()
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "ok"

    root_out = await sandbox.run(
        "python3 -c \"open('/outside.txt','w').write('x')\"", workspace=str(workspace)
    )
    assert not root_out.ok
    assert "Read-only file system" in root_out.stderr

    escape = await sandbox.run("cp inside.txt ../../escaped.txt", workspace=str(workspace), timeout_seconds=30)
    assert not escape.ok
    assert not (workspace.parent / "escaped.txt").exists()
    assert not list(workspace.glob("*escaped*"))


async def test_network_is_disabled_by_default(tmp_path):
    sandbox = DockerSandbox(_policy(tmp_path))
    control = await sandbox.run("echo control", workspace=str(tmp_path))
    assert control.stdout.strip() == "control", control.output()
    result = await sandbox.run(
        "python3 -c \"import socket;s=socket.create_connection(('8.8.8.8',53),3);print('reached')\"",
        workspace=str(tmp_path),
    )
    assert not result.ok
    assert "reached" not in result.stdout


async def test_api_key_never_enters_container_or_output(tmp_path):
    sandbox = DockerSandbox(_policy(tmp_path))
    result = await sandbox.run(
        "printenv",
        workspace=str(tmp_path),
        env={"OPENAI_API_KEY": SECRET, "ANTHROPIC_API_KEY": SECRET, "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert "PATH=" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.output()
    assert "API_KEY" not in result.stdout


async def test_env_allowlist_blocks_secret_even_if_allowlisted(tmp_path):
    policy = _policy(tmp_path)
    policy.env_allowlist = ("PATH", "MY_API_TOKEN", "LANG")
    assert policy.filter_env({"PATH": "/bin", "MY_API_TOKEN": SECRET, "HOME": "/x"}) == {"PATH": "/bin"}


async def test_memory_limit_terminates_runaway(tmp_path):
    sandbox = DockerSandbox(_policy(tmp_path, memory="64m"))
    result = await sandbox.run(
        "python3 -c \"b=[]; [b.append(bytearray(8000000)) for _ in iter(int,1)]\"",
        workspace=str(tmp_path),
    )
    assert not result.ok


async def test_timeout_kills_container_and_leaves_no_residue(tmp_path):
    sandbox = DockerSandbox(_policy(tmp_path, timeout_seconds=3))
    result = await sandbox.run("python3 -c \"import time;time.sleep(60)\"", workspace=str(tmp_path))
    assert result.timed_out
    assert result.exit_code is None
    await asyncio.sleep(0.5)
    listing = subprocess.run(
        [DOCKER, "ps", "-q", "--filter", "label=blank.sandbox=1"], capture_output=True, text=True
    )
    assert listing.stdout.strip() == ""
    assert await cleanup_containers() == 0


async def test_docker_flags_encode_resource_limits(tmp_path):
    policy = _policy(tmp_path)
    flags = " ".join(policy.docker_flags("c1", str(tmp_path)))
    for needle in ("--memory 64m", "--cpus 0.4", "--pids-limit 64", "--network none", "--read-only"):
        assert needle in flags


async def test_local_sandbox_used_when_docker_disabled(tmp_path):
    policy = _policy(tmp_path)
    policy.enabled = False
    sandbox = get_sandbox(policy)
    assert isinstance(sandbox, LocalSandbox)
    result = await sandbox.run("python -V" if shutil.which("python") else "echo hi", workspace=str(tmp_path))
    assert result.backend == "local"
    assert "本地模式无隔离" in " ".join(result.notes)


async def test_audit_log_reconstructs_tool_calls(tmp_path):
    sandbox = DockerSandbox(_policy(tmp_path))
    log = AuditLog(tmp_path / "audit")
    task = "task-audit-1"
    for step in range(2):
        result = await sandbox.run(f"echo step-{step}", workspace=str(tmp_path))
        log.record(
            AuditRecord(
                action=result.command,
                tool="run_shell",
                task_id=task,
                node="act",
                workspace=str(tmp_path),
                detail={"exit_code": result.exit_code, "stdout": result.stdout, "secret": SECRET},
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                backend=result.backend,
            )
        )
    entries = log.read(task)
    assert [e["detail"]["stdout"] for e in entries] == ["step-0\n", "step-1\n"]
    assert all(e["tool"] == "run_shell" for e in entries)
    assert entries[0]["workspace"].startswith("<workspace:")
    raw = (tmp_path / "audit" / "tool-calls.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw
    assert len(log.trace(task)) == 2


def test_snapshot_restores_overwritten_and_deleted_files(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.txt").write_text("keep me\n", encoding="utf-8")

    store = SnapshotStore(tmp_path / "snapshots")
    snapshot = store.capture(str(workspace), reason="覆盖前快照", task_id="task-1")
    assert set(snapshot["files"]) == {"a.py", "sub/b.txt"}

    (workspace / "a.py").write_text("VALUE = 999\n", encoding="utf-8")
    (workspace / "sub" / "b.txt").unlink()

    assert store.content(snapshot["id"], "sub/b.txt").decode().strip() == "keep me"
    restored = store.restore(snapshot["id"], str(workspace))
    assert sorted(restored) == ["a.py", "sub/b.txt"]
    assert (workspace / "a.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace / "sub" / "b.txt").read_text(encoding="utf-8") == "keep me\n"
    assert store.list("task-1")[0]["reason"] == "覆盖前快照"


def test_snapshot_can_restore_single_file(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("one\n", encoding="utf-8")
    (workspace / "b.py").write_text("bee\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot = store.capture(str(workspace), reason="before patch")
    (workspace / "a.py").write_text("changed\n", encoding="utf-8")
    (workspace / "b.py").write_text("changed too\n", encoding="utf-8")
    assert store.restore(snapshot["id"], str(workspace), relative="a.py") == ["a.py"]
    assert (workspace / "a.py").read_text(encoding="utf-8") == "one\n"
    assert (workspace / "b.py").read_text(encoding="utf-8") == "changed too\n"


async def test_run_shell_is_routed_through_the_sandbox(client, monkeypatch, tmp_path):
    """阶段6：BLANK_SANDBOX_ENABLED=1 时 run_shell 必须在沙箱里执行，且密钥不进容器。"""
    from blank_api.db import get_session_factory
    from blank_api.services.tools import build_default_tools
    from blank_api.services.tools.base import ToolContext
    from blank_api.services.tools.gateway import ToolGateway

    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_IMAGE", "blank-sandbox:dev")
    monkeypatch.setenv("SUPER_SECRET_TOKEN", SECRET)
    context = ToolContext(
        root=tmp_path,
        task_id="shell-through-sandbox",
        session_factory=get_session_factory(),
        approval_policy=lambda *_: True,
    )
    outcome = await ToolGateway(build_default_tools()).execute(
        "run_shell", {"command": "env | sort"}, context, "test"
    )
    assert outcome["ok"] is True, outcome
    assert outcome["data"]["sandbox"] == "docker", outcome["data"]
    assert "PATH=" in outcome["output"], outcome["output"]
    assert SECRET not in outcome["output"] + str(outcome["data"])


async def test_run_tests_is_routed_through_the_sandbox(client, monkeypatch, tmp_path):
    """阶段6：测试验证环节同样在容器里执行，宿主虚拟环境的解释器不得参与。"""
    from blank_api.db import get_session_factory
    from blank_api.services.tools import build_default_tools
    from blank_api.services.tools.base import ToolContext
    from blank_api.services.tools.gateway import ToolGateway

    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_IMAGE", "blank-sandbox:dev")
    (tmp_path / "test_in_container.py").write_text(
        "import os\nimport sys\n\n\n"
        "def test_runs_inside_the_container():\n"
        "    assert os.path.exists('/.dockerenv'), sys.executable\n"
        "    assert '.venv' not in sys.executable\n",
        encoding="utf-8",
    )
    context = ToolContext(
        root=tmp_path,
        task_id="tests-through-sandbox",
        session_factory=get_session_factory(),
        approval_policy=lambda *_: True,
    )
    outcome = await ToolGateway(build_default_tools()).execute(
        "run_tests", {}, context, "run_validation"
    )
    assert outcome["ok"] is True, outcome
    assert outcome["data"]["sandbox"] == "docker", outcome["data"]
    assert "python3 -m pytest" in outcome["data"]["command"], outcome["data"]
