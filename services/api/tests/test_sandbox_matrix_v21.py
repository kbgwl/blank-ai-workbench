"""M2：三档 sandbox × 四审批模式的矩阵，以及四条边界探针。

设计稿 §7 的退出条件原话是「矩阵 12 格里每格至少一条可失败断言；P-NET / P-PROC /
P-ESCAPE / P-INHERIT 四条有结论」。这里逐格给出判据，并让每条判据都能翻红：

* 档位只回答"这次想让什么边界管"，审批模式只回答"要不要问"——两件事不许互相冒充。
* `off` 跑在宿主上、`workspace` 跑在 local（本机 Job）上、`workspace-net` 在测不出网络
  强制的机器上必须**拒绝**而不是降级成 off（off 的权限严格更大）。
* P-NET：job_only 下连接本机监听端口会成功；结论必须与 `network_enforced == False` 一致。
* P-PROC：pids=1 时第二个进程起不来；同一命令在 pids=128 下必须成功（阳性对照）。
* P-ESCAPE：job_only 下能写工作区之外的文件；写成即证明文件边界没被系统强制。
* P-INHERIT：父进程退出后 Job 关闭会带走仍在写心跳的子进程；off 档同一脚本的子进程
  必须继续活着（阳性对照，事后由测试自己杀掉）。

这些探针不是"证明沙箱很安全"，而是把**没被强制**的那几条钉成可复现的事实。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_PLAN,
    MODES,
    PermissionPolicy,
)
from blank_api.services.safety import backend_info
from blank_api.services.sandbox import boundary, runner
from blank_api.services.sandbox.boundary import Capability
from blank_api.services.sandbox.tiers import (
    REASON_JOB_ONLY,
    REASON_NET_UNAVAILABLE,
    REASON_OFF,
    TIER_OFF,
    TIER_WORKSPACE,
    TIER_WORKSPACE_NET,
    TIERS,
    resolve_tier,
)
from blank_api.services.settings import SettingsStore
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field
from blank_api.services.tools.shell import execute_command

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Job Object / 进程树回收只在 Windows 上有结论")


# --------------------------------------------------------------------------- #
# 装置
# --------------------------------------------------------------------------- #
@pytest.fixture
def job_only(monkeypatch):
    """把边界探测钉成这台开发机的实测形状（M0：job_only）。

    只动缓存不动探针：这些用例测的是"拿到 job_only 之后怎么记账、怎么拒绝"，
    "这台机器究竟能不能建 Job"由 M0 的探针与 v19 的真 Job 用例负责。
    """
    cap = Capability(
        platform=sys.platform,
        job=True,
        restricted_token=False,
        token_control=True,      # DuplicateTokenEx 阳性对照
        appcontainer=False,
        carrier_control=True,    # 不带属性的同载体阳性对照
        measured_at=time.time(),
    )
    monkeypatch.setattr(boundary, "_CACHE", (time.time(), cap))
    return cap


@pytest.fixture
def local_sandbox(monkeypatch, job_only):
    """开启沙箱、允许本机回退、并把 docker 探测固定成"没有"。

    固定 backend=local 是为了让矩阵在装了 Docker 的机器上也测 LocalSandbox 那一格；
    容器那一格由 test_sandbox_v07 用真镜像负责。workspace-net 在 job_only 下必须不可用。
    """
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.setenv("BLANK_SANDBOX_NETWORK", "false")
    monkeypatch.setattr(runner, "shutil_which", lambda *_: None)
    return job_only


class _TinyAdapter(ModelAdapter):
    """只为把运行时装配起来；用例断言的是档位接线，不是模型行为。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="tiny",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="tiny",
                context_window=32_000,
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs) -> ModelResponse:
        raise RuntimeError("模型不该被调用：这些用例在装配层就该给出结论")

    async def stream(self, messages, **kwargs):
        yield ""


async def _seed_task(task_id: str = "task-m2") -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-m2", name="M2 矩阵"))
        session.add(Task(id=task_id, project_id="project-m2", title="矩阵", goal="矩阵", max_steps=3))
        await session.commit()


def _context(root: Path, *, tier: str, mode: str = MODE_BYPASS, task_id: str = "task-m2") -> ToolContext:
    return ToolContext(
        root=root,
        task_id=task_id,
        session_factory=get_session_factory(),
        permissions=PermissionPolicy.build(mode, sandbox_tier=tier),
        sandbox_tier=tier,
    )


# --------------------------------------------------------------------------- #
# 12 格矩阵
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tier", TIERS)
async def test_matrix_cell(tier, mode, local_sandbox, workspace):
    """一档 × 一模一格：档位决定"跑在哪/是否拒绝"，模式只决定"要不要问"。"""
    resolution = resolve_tier(tier)
    tools = {tool.name: tool for tool in build_default_tools()}
    policy = PermissionPolicy.build(mode, sandbox_tier=tier)

    # ---- 模式那一半：同一档位下四档判定的差异必须真的出现 ----
    shell_decision = policy.decide(tools["run_shell"], {"command": "echo m2"})
    patch_decision = policy.decide(tools["apply_patch"], {"path": "src/core.py", "patch": "@@ -1,1 +1,1 @@"})
    forbidden = policy.decide(tools["run_shell"], {"command": "git push --force origin main"})
    if mode == MODE_PLAN:
        assert shell_decision.outcome == DENY and shell_decision.rule == "mode:plan"
        assert patch_decision.outcome == DENY and patch_decision.rule == "mode:plan"
    else:
        assert shell_decision.outcome == ALLOW, (tier, mode, shell_decision)
        if mode == MODE_ACCEPT_EDITS:
            assert patch_decision.outcome == ALLOW and patch_decision.rule == "mode:accept_edits"
        if mode == MODE_BYPASS:
            # 全放行也不越过不可逆：这一条同时钉住"bypass 不是关掉安全边界"。
            assert forbidden.outcome == ASK and forbidden.rule == "irreversible"
        if mode == MODE_DEFAULT:
            assert shell_decision.rule == "risk:auto"

    # ---- 档位那一半：执行路由与拒绝必须与解析结果一致 ----
    result = await execute_command("echo m2", workspace, timeout=10, tier=tier)
    assert result["tier"]["tier"] == tier
    if tier == TIER_WORKSPACE_NET:
        assert resolution.available is False
        assert resolution.reason == REASON_NET_UNAVAILABLE
        assert result["tier_blocked"] is True, "workspace-net 起不来时必须拒绝，不许退回宿主"
        assert result["enforcement"]["limits_applied"] is False
        assert "不会降级成 off" in result["tier"]["notice"]
    elif tier == TIER_OFF:
        assert resolution.reason == REASON_OFF
        assert result.get("tier_blocked") is None
        assert "sandbox" in result and result["sandbox"] is None, "off 档跑在宿主上，必须留下'没有沙箱'这条事实"
        assert result["enforcement"]["limits_applied"] is False
        assert "无隔离" in result["tier"]["notice"]
        assert "m2" in result["stdout"]
    else:
        assert resolution.available is True
        assert resolution.reason == REASON_JOB_ONLY
        assert resolution.degraded is True
        assert result.get("tier_blocked") is None
        assert result["sandbox"] == "local"
        assert result["tier"]["degraded"] is True
        assert result["enforcement"]["sandbox_tier"] == TIER_WORKSPACE
        # 文件与网络在 job_only 上没有被系统强制：这两条必须一直是 False。
        assert result["enforcement"]["filesystem_enforced"] is False
        assert result["enforcement"]["network_enforced"] is False
        assert "m2" in result["stdout"]
        if sys.platform == "win32":
            assert result["enforcement"]["limits_applied"] is True


# --------------------------------------------------------------------------- #
# 档位授信：换档或多走一层路径都不许继承免问
# --------------------------------------------------------------------------- #
def test_tool_grant_does_not_survive_a_tier_change():
    policy = PermissionPolicy.build(MODE_DEFAULT, approved_tools={"run_shell"}, sandbox_tier=TIER_WORKSPACE)
    shell = {tool.name: tool for tool in build_default_tools()}["run_shell"]
    # 用高风险命令：默认模式对中风险本来就会自动放行，拿它测不出"授权有没有失效"。
    command = {"command": 'python -c "print(1)"'}
    granted = policy.decide(shell, command)
    assert granted.outcome == ALLOW and granted.rule == f"tool:{TIER_WORKSPACE}:run_shell"
    # 同一份会话记忆切到 off（宿主完整权限）后必须重新问：放行按档位分桶。
    policy.sandbox_tier = TIER_OFF
    decision = policy.decide(shell, command)
    assert decision.outcome == ASK, "切到权限更大的档位仍然免问，等于一次静默提权"


def test_path_scoped_grant_does_not_cover_another_file():
    policy = PermissionPolicy.build(
        MODE_DEFAULT, approved_tools={(TIER_WORKSPACE, "apply_patch", "src/a.py")}, sandbox_tier=TIER_WORKSPACE
    )
    patch = {tool.name: tool for tool in build_default_tools()}["apply_patch"]
    # 含密钥的补丁是高风险：没有授权时必须问，否则测不出"路径前缀有没有生效"。
    secret = {"patch": "@@ -0,0 +1,1 @@\n+sk-abcdefghijklmnop123456"}
    allowed = policy.decide(patch, {"path": "src/a.py", **secret})
    other = policy.decide(patch, {"path": "src/b.py", **secret})
    assert allowed.outcome == ALLOW
    assert other.outcome == ASK, "批准了一个文件不能让同目录的另一个文件也免问"


# --------------------------------------------------------------------------- #
# 拒绝不能伪装成宿主执行（网关事件语义）
# --------------------------------------------------------------------------- #
async def test_blocked_command_is_not_recorded_as_host_execution(local_sandbox, workspace):
    await _seed_task("task-m2-blocked")
    context = _context(workspace, tier=TIER_WORKSPACE_NET, task_id="task-m2-blocked")
    payload = await ToolGateway(build_default_tools()).execute(
        "run_shell", {"command": "echo should-not-run"}, context, "act"
    )
    assert payload["status"] == "denied"
    assert payload["data"].get("tier_blocked") is True
    assert "sandbox" not in payload["data"], "被拒绝的命令不许带'跑在宿主上'的键"
    event = sandbox_backend_event_field(payload)
    assert "sandbox_blocked" in event
    assert "sandbox_backend" not in event, "把拒绝写成 sandbox_backend=null 会被读成一次宿主执行"
    assert "不会降级成 off" in event["sandbox_blocked"]

    # 对照：同一工具在 off 档真的跑起来时，sandbox_backend 必须存在且为 null。
    await _seed_task("task-m2-host")
    host_context = _context(workspace, tier=TIER_OFF, task_id="task-m2-host")
    host = await ToolGateway(build_default_tools()).execute(
        "run_shell", {"command": "echo host-run"}, host_context, "act"
    )
    assert host["status"] == "ok"
    host_event = sandbox_backend_event_field(host)
    assert "sandbox_backend" in host_event and host_event["sandbox_backend"] is None
    assert host_event["sandbox_enforcement"]["sandbox_tier"] == TIER_OFF


# --------------------------------------------------------------------------- #
# 装配层：非法档位 422、不可用档位 409、默认档位来自设置
# --------------------------------------------------------------------------- #
async def test_unknown_tier_is_rejected_before_anything_runs(workspace):
    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id="task-m2-invalid", provider_id="p", provider_name="P",
        adapter=_TinyAdapter(), root=workspace, sandbox_tier="vm",
    )
    with pytest.raises(ApiError) as caught:
        await runtime._prepare(request, "task-m2-invalid:1")
    assert caught.value.code == "sandbox_tier_invalid"
    assert caught.value.status_code == 422


async def test_workspace_net_is_409_instead_of_silently_degrading(local_sandbox, workspace):
    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id="task-m2-net", provider_id="p", provider_name="P",
        adapter=_TinyAdapter(), root=workspace, sandbox_tier=TIER_WORKSPACE_NET,
    )
    with pytest.raises(ApiError) as caught:
        await runtime._prepare(request, "task-m2-net:1")
    assert caught.value.code == "sandbox_tier_unavailable"
    assert caught.value.status_code == 409
    assert "不会降级成 off" in caught.value.message


async def test_default_tier_comes_from_settings(local_sandbox, workspace, tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(path))
    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id="task-m2-default", provider_id="p", provider_name="P",
        adapter=_TinyAdapter(), root=workspace, sandbox_tier="",
    )
    SettingsStore(path).write({"sandbox": {"default_tier": TIER_OFF}})
    off = await runtime._prepare(request, "task-m2-default:1")
    assert off.sandbox["requested"] == TIER_OFF and off.sandbox["tier"] == TIER_OFF

    SettingsStore(path).write({"sandbox": {"default_tier": TIER_WORKSPACE}})
    work = await runtime._prepare(request, "task-m2-default:2")
    assert work.sandbox["requested"] == TIER_WORKSPACE
    assert work.sandbox["tier"] == TIER_WORKSPACE
    assert work.sandbox["degraded"] is True, "job_only 不是内核级文件/网络边界，必须标降级"


def test_unknown_default_tier_falls_back_to_workspace_not_off(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(path))
    store = SettingsStore(path)
    store.write({"sandbox": {"default_tier": "container"}})
    assert store.read()["sandbox"]["default_tier"] == TIER_WORKSPACE, "认不出的档位只能收紧到 workspace，绝不能落到 off"


async def test_task_started_carries_the_sandbox_facts(local_sandbox, workspace, monkeypatch):
    await _seed_task("task-m2-started")
    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id="task-m2-started", provider_id="p", provider_name="P",
        adapter=_TinyAdapter(), root=workspace, goal="记录档位", sandbox_tier=TIER_WORKSPACE,
    )
    # 只验 task.started 的接线：图一开跑就打断，模型一次都不调用。
    real_prepare = runtime._prepare

    async def prepare_then_break(req, thread_id):
        scope = await real_prepare(req, thread_id)

        async def boom(*_a, **_k):
            raise RuntimeError("停在 task.started 之后")

        scope.graph.ainvoke = boom
        return scope

    monkeypatch.setattr(runtime, "_prepare", prepare_then_break)
    with pytest.raises(RuntimeError):
        await runtime.run(request)
    async with get_session_factory()() as session:
        events = list(
            (
                await session.execute(
                    select(AgentEvent).where(
                        AgentEvent.task_id == "task-m2-started", AgentEvent.type == "task.started"
                    )
                )
            ).scalars()
        )
    assert len(events) == 1
    sandbox = events[0].payload["sandbox"]
    assert sandbox["requested"] == TIER_WORKSPACE
    assert sandbox["tier"] == TIER_WORKSPACE
    assert sandbox["reason"] == REASON_JOB_ONLY
    assert sandbox["filesystem_enforced"] is False


async def test_resume_reads_back_the_stored_tier(monkeypatch, workspace):
    """审批恢复时前端不重发档位：必须读 task.started 里的 requested。

    反向也钉住：这里存的是 workspace-net（与默认档不同），实现若漏接就会拿到空串。
    """
    await _seed_task("task-m2-resume")
    await EventRecorder(get_session_factory(), "task-m2-resume").add(
        "task.started", "", {"sandbox": {"requested": TIER_WORKSPACE_NET}}
    )
    runtime = AgentRuntime(session_factory=get_session_factory())
    captured: dict = {}

    async def fake_prepare(request, thread_id):
        captured["tier"] = request.sandbox_tier
        raise RuntimeError("停在这里：只验读回")

    async def fake_latest(*_a, **_k):
        return "task-m2-resume:1"

    monkeypatch.setattr("blank_api.services.agent.runtime.latest_thread_id", fake_latest)
    monkeypatch.setattr(runtime, "_prepare", fake_prepare)
    request = RunRequest(
        task_id="task-m2-resume", provider_id="p", provider_name="P",
        adapter=_TinyAdapter(), root=workspace, sandbox_tier="",
    )
    with pytest.raises(RuntimeError):
        await runtime.resume(request)
    assert captured["tier"] == TIER_WORKSPACE_NET


# --------------------------------------------------------------------------- #
# 设置页的后端文案：off 下必须直说「无隔离」
# --------------------------------------------------------------------------- #
def test_backend_info_never_calls_off_a_restricted_execution(monkeypatch):
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "0")
    info = backend_info()
    assert info.backend == "none" and info.isolation == "none"
    assert "无隔离" in info.label and "宿主完整权限" in info.label
    assert "本机受限执行" not in info.label
    assert info.reason == "sandbox_disabled"

    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "0")
    monkeypatch.setattr("shutil.which", lambda *_: None)
    unavailable = backend_info()
    assert unavailable.available is False and unavailable.isolation == "none"
    assert "不可用" in unavailable.label and "Docker" in unavailable.detail


@pytest.mark.skipif(sys.platform != "win32", reason="job_only 是本机实测档位")
def test_backend_info_reports_the_measured_local_strength(monkeypatch, local_sandbox):
    monkeypatch.setattr("shutil.which", lambda *_: None)
    info = backend_info()
    assert info.backend == "local"
    assert info.isolation == "job_only"
    assert "文件、网络边界未由系统强制" in info.label


# --------------------------------------------------------------------------- #
# P-NET：job_only 下网络没有被强制
# --------------------------------------------------------------------------- #
def _start_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted: list[bytes] = []

    def _accept():
        try:
            conn, _ = server.accept()
            accepted.append(conn.recv(16))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_accept, daemon=True).start()
    return server, server.getsockname()[1], accepted


async def test_p_net_connect_succeeds_and_the_account_says_unenforced(local_sandbox, workspace):
    """P-NET 结论：job_only 不拦出网。判据同时要求"连上了"和"账上写着没强制"。

    这条能失败的方向是双向的：若哪天系统真拦了网，连接会失败而账仍写 False；
    若账改成了 True 而实际还能连，断言同样失败——两边必须一致。
    """
    script = workspace / "net_probe.py"
    script.write_text(
        "import socket, sys\n"
        "s = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=3)\n"
        "s.sendall(b'ping')\n"
        "s.close()\n"
        "print('connected')\n",
        encoding="utf-8",
    )
    server, port, accepted = _start_listener()
    try:
        result = await execute_command(f'"{sys.executable}" net_probe.py {port}', workspace, 15, tier=TIER_WORKSPACE)
        # 客户端已经返回时监听线程可能还没被调度；给它一个短窗口，避免把时序当成结论。
        for _ in range(40):
            if accepted:
                break
            time.sleep(0.05)
    finally:
        server.close()
    assert result["sandbox"] == "local"
    connected = result["exit_code"] == 0 and "connected" in result["stdout"]
    assert accepted and accepted[0] == b"ping", "监听端没收到连接：探针本身没跑通"
    assert result["enforcement"]["network_enforced"] is False
    assert connected is (not result["enforcement"]["network_enforced"])


# --------------------------------------------------------------------------- #
# P-PROC：pids 上限真的由 Job 执行
# --------------------------------------------------------------------------- #
@WIN_ONLY
async def test_p_proc_pids_limit_blocks_the_second_process(local_sandbox, workspace, monkeypatch):
    """P-PROC 结论：pids=1 时连解释器都起不来；同一命令在 pids=128 下正常（阳性对照）。"""
    script = workspace / "pids_ok.py"
    script.write_text("print('pids-ok')\n", encoding="utf-8")
    command = f'"{sys.executable}" pids_ok.py'

    monkeypatch.setenv("BLANK_SANDBOX_PIDS", "128")
    control = await execute_command(command, workspace, 15, tier=TIER_WORKSPACE)
    assert control["exit_code"] == 0 and "pids-ok" in control["stdout"], "对照没跑通，下面的红说明不了 Job"
    assert control["enforcement"]["pids_enforced"] is True
    assert control["enforcement"]["pids_limit"] == 128

    monkeypatch.setenv("BLANK_SANDBOX_PIDS", "1")
    limited = await execute_command(command, workspace, 15, tier=TIER_WORKSPACE)
    assert limited["enforcement"]["pids_enforced"] is True
    assert limited["enforcement"]["pids_limit"] == 1
    assert limited["exit_code"] != 0, "pids=1 时第二个进程仍然起得来，说明限额没被内核执行"
    assert "pids-ok" not in limited["stdout"]


# --------------------------------------------------------------------------- #
# P-ESCAPE：job_only 下文件边界没有被强制
# --------------------------------------------------------------------------- #
async def test_p_escape_writes_outside_the_workspace(local_sandbox, workspace, tmp_path):
    """P-ESCAPE 结论：job_only 挡不住脚本里的越界写。写成即证明，失败即证明真的拦住了。"""
    outside = tmp_path / "outside-workspace.txt"
    script = workspace / "escape_probe.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path(r'{outside}').write_text('escaped', encoding='utf-8')\n"
        "print('wrote')\n",
        encoding="utf-8",
    )
    result = await execute_command(f'"{sys.executable}" escape_probe.py', workspace, 15, tier=TIER_WORKSPACE)
    assert result["enforcement"]["filesystem_enforced"] is False
    assert result["exit_code"] == 0 and "wrote" in result["stdout"]
    assert outside.read_text(encoding="utf-8") == "escaped"


# --------------------------------------------------------------------------- #
# P-INHERIT：Job 关闭带走整棵进程树（off 档作阳性对照）
# --------------------------------------------------------------------------- #
_BEAT = """import os, sys, time
path = sys.argv[1]
with open(path, "a", encoding="utf-8") as fh:
    for _ in range(400):
        fh.write("x")
        fh.flush()
        os.fsync(fh.fileno())
        time.sleep(0.02)
"""

_SPAWN = """import os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
child = subprocess.Popen(
    [sys.executable, os.path.join(here, "beat.py"), sys.argv[1]],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
time.sleep(0.6)
"""


def _kill_tree(pid: int | None) -> None:
    if pid is None or sys.platform != "win32":
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


@WIN_ONLY
async def test_p_inherit_job_close_kills_a_surviving_grandchild(local_sandbox, workspace):
    """P-INHERIT 结论：挂进 Job 的孙进程在父进程退出后仍被 KILL_ON_JOB_CLOSE 带走。

    阳性对照是同一脚本跑在 off 档：没有 Job 时孙进程必须继续写心跳，否则"心跳停了"
    就证明不了任何事（可能脚本根本写不出来）。对照那一路由测试自己收尾杀进程。
    """
    (workspace / "beat.py").write_text(_BEAT, encoding="utf-8")
    (workspace / "spawn.py").write_text(_SPAWN, encoding="utf-8")
    command = f'"{sys.executable}" spawn.py beat.txt'
    beat = workspace / "beat.txt"

    # 阳性对照：off 档没有 Job，孙进程必须活着继续写。
    control = await execute_command(command, workspace, 20, tier=TIER_OFF)
    control_pid = int(control["stdout"].strip().splitlines()[0])
    try:
        first = beat.stat().st_size
        time.sleep(0.8)
        grown = beat.stat().st_size
        assert grown > first, "对照组的孙进程没有继续写心跳：这个探针装置本身是坏的"
    finally:
        _kill_tree(control_pid)

    # 实验组：workspace 档，父进程退出后 Job 关闭。
    beat.write_text("", encoding="utf-8")
    result = await execute_command(command, workspace, 20, tier=TIER_WORKSPACE)
    pid = int(result["stdout"].strip().splitlines()[0])
    try:
        within_job = beat.stat().st_size
        assert within_job > 0, "Job 里的孙进程一次都没写成功：'被杀'与'没跑起来'分不清"
        time.sleep(0.8)
        assert beat.stat().st_size == within_job, "父进程退出、Job 已关闭，孙进程还在写：KILL_ON_JOB_CLOSE 没生效"
    finally:
        _kill_tree(pid)
