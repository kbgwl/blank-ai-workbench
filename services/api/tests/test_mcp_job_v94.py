"""#94：MCP 的 stdio 服务端进程必须进同一个执行边界（Windows Job Object）。

这个文件只回答三件事，每一件都能失败：

1. **进没进**：客户端说"已挂 Job"不算数，用 ``IsProcessInJob`` 直接问内核，
   同时拿一个没挂的进程做阴性对照——否则"查询恒真"也能让判据变绿。
2. **限额是不是真的**：在 128MB 的 Job 里分配 256MB 必须失败，同一调用在 768MB 的
   Job 里必须成功。只有一红一绿同时出现，才能把失败归因给限额而不是工具本身。
3. **挂不进去会不会说谎**：Job 创建/赋值失败时，客户端仍要能连上（坏服务端不拦任务），
   但状态、汇总、证据包必须红字标注 ``host_process_unbounded``，不许默认成已纳入边界。

平台分工：1/2/3(清树) 只在 Windows 上有意义；4/5/6/7/8 是跨平台的账，注入 Job 失败
即可验证。判定里的期望值都是实测形状，不是照着代码推的。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from blank_api.services.agent.evidence import boundary_report
from blank_api.services.mcp import client as client_module
from blank_api.services.mcp.bridge import (
    BOUNDARY_JOB,
    BOUNDARY_REMOTE,
    BOUNDARY_UNBOUNDED,
    MCPBundle,
    MCPManager,
    MCPStatus,
    STATE_READY,
    parse_servers,
)
from blank_api.services.mcp.client import HttpClient, MCPError, ServerConfig, StdioClient
from blank_api.services.mcp.protocol import flatten_result
from blank_api.services.sandbox import windows_job

SERVER = Path(__file__).with_name("fake_mcp_server.py")
WIN = sys.platform == "win32"
needs_windows = pytest.mark.skipif(not WIN, reason="Job Object 只在 Windows 上存在")


def _stdio(mode: str = "normal", **overrides) -> ServerConfig:
    return ServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=(str(SERVER), mode),
        timeout=float(overrides.pop("timeout", 15.0)),
        **overrides,
    )


def _pid_alive(pid: int) -> bool:
    """进程还在跑吗。不用 os.kill：Windows 上它对任意 signal 都会真的去 TerminateProcess。"""
    if not WIN:  # pragma: no cover - 调用方都带 needs_windows
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    k32.GetExitCodeProcess.restype = ctypes.c_bool
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


# --------------------------------------------------------------------------- #
# 1. 真的在 Job 里：正向 + 阴性对照
# --------------------------------------------------------------------------- #
@needs_windows
async def test_stdio_server_is_really_inside_the_job():
    """红于：只创建 Job 不 assign、assign 失败却写 job_assigned=True、查询恒真。"""
    probe = StdioClient(_stdio(memory="256m", pids=32))
    await probe.start()
    try:
        facts = probe.boundary_facts()
        assert facts["boundary"] == BOUNDARY_JOB
        assert facts["job_created"] is True and facts["job_assigned"] is True
        assert facts["memory_enforced"] is True and facts["pids_enforced"] is True
        assert facts["tree_kill_enforced"] is True
        assert facts["memory_bytes"] == 256 * 1024 * 1024
        assert facts["pids_limit"] == 32

        job = probe._job
        assert job is not None and job.ok
        inside, detail = windows_job.pid_in_job(job, probe._process.pid)
        assert inside is True, detail

        # 阴性对照：同一时刻一个没挂进这枚 Job 的进程必须报 False。
        # 少了这条，"IsProcessInJob 永远返回 True" 也会让上面的断言通过。
        control = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            outside, detail = windows_job.pid_in_job(job, control.pid)
            assert outside is False, f"阴性对照也被判成在 Job 里（{detail}），判据无效"
        finally:
            control.kill()
            control.wait(timeout=10)
    finally:
        await probe.close()

    # 关句柄后事实不能跟着变成"没建成"：账上说的仍是这次真实生效的边界。
    assert probe._job is None
    assert probe.boundary_facts()["boundary"] == BOUNDARY_JOB


# --------------------------------------------------------------------------- #
# 2. 限额由内核强制：一红一绿
# --------------------------------------------------------------------------- #
@needs_windows
async def test_memory_limit_is_enforced_by_the_kernel():
    """红于：Job 没挂上（分配 256MB 在 128MB 的账里也照样成功）、限额写错单位。

    绿的那一半是阳性对照：同样的 hog 调用在 768MB 的 Job 里必须成功返回。
    没有它，"服务端自己崩了"也会把红的那一半解释成限额生效。
    """
    control = StdioClient(_stdio(mode="hog", memory="768m", pids=64, timeout=30.0))
    try:
        await control.start()
        _, text, _ = flatten_result(await control.call_tool("hog", {}))
        assert text == "hogged:268435456", "对照组没跑通，下面的失败就不能归因给内存上限"
    finally:
        await control.close()

    tight = StdioClient(_stdio(mode="hog", memory="128m", pids=64, timeout=30.0))
    try:
        await tight.start()
        with pytest.raises(MCPError):
            await tight.call_tool("hog", {})
        process = tight._process
        assert process is not None
        # call_tool 失败时 returncode 可能还没被传输层写回；等进程真正退出再判。
        await asyncio.wait_for(process.wait(), timeout=10)
        assert process.returncode is not None and process.returncode != 0
        # 服务端自己说的话才是"死于内存上限"的证据，不是靠我们猜。
        assert "MemoryError" in tight._stderr_tail, tight._stderr_tail[-300:]
    finally:
        await tight.close()


# --------------------------------------------------------------------------- #
# 3. 关连接＝整棵树一起走（孙子进程）
# --------------------------------------------------------------------------- #
@needs_windows
async def test_closing_the_client_takes_the_grandchild_down():
    """红于：只 terminate 直接子进程、不关 Job 句柄——长命孙子会变成孤儿。

    孙子在工具调用里出生（而非服务端启动时）：客户端是 spawn 之后才挂 Job 的，
    启动时派生的孙子可能落在赋值窗口外，这条用例就退化成竞态而不是判据。
    同时钉住 close 的顺序：先关 Job 再 wait 进程。反过来会被孙子握着的 stdout
    管道拖满整个 KILL_GRACE（实测 2.0s vs 0.03s）。
    """
    client = StdioClient(_stdio(mode="child"))
    closed_in = 0.0
    try:
        await client.start()
        _, text, _ = flatten_result(await client.call_tool("child_pid", {}))
        child_pid = int(text)
        assert _pid_alive(child_pid), "服务端没把子进程拉起来，判据本身无效"
        assert client._job is not None
        inside, detail = windows_job.pid_in_job(client._job, child_pid)
        assert inside is True, f"孙子进程不在 Job 里，清树判据无效：{detail}"
    finally:
        started = time.monotonic()
        await client.close()
        closed_in = time.monotonic() - started

    assert closed_in < client_module.KILL_GRACE, (
        f"close 吃满了 {closed_in:.2f}s 宽限期：多半是先等进程退出、后关 Job，"
        "孙子的管道把 wait 拖住了"
    )
    assert _wait_dead(child_pid), f"关连接后孙子进程 {child_pid} 还活着：Job 的 KILL_ON_JOB_CLOSE 没生效"


# --------------------------------------------------------------------------- #
# 4/5. 挂不进去：连接照常可用，但边界必须标红
# --------------------------------------------------------------------------- #
async def _assert_unbounded_but_usable(monkeypatch, kind: str):
    reason = f"注入：{kind} 失败"
    if kind == "create":
        monkeypatch.setattr(
            client_module.windows_job, "create_job", lambda *a, **kw: windows_job.Job(error=reason)
        )
    else:
        monkeypatch.setattr(
            client_module.windows_job, "assign_pid", lambda *a, **kw: (False, reason)
        )

    client = StdioClient(_stdio())
    await client.start()
    try:
        # 连不上才该拦任务；Job 失败不能成为"服务端不可用"的理由。
        tools = await client.list_tools()
        assert {item["name"] for item in tools} == {"echo", "boom", "run shell", "slow"}
        facts = client.boundary_facts()
        assert facts["boundary"] == BOUNDARY_UNBOUNDED
        assert facts["limits_applied"] is False
        assert reason in facts["note"]
        assert facts["note"].startswith("⚠")

        status = MCPStatus(
            "fake", "stdio", STATE_READY, tools=len(tools),
            boundary=facts["boundary"], boundary_note=facts["note"], enforcement=facts,
        )
        body = status.as_dict()
        assert body["boundary"] == BOUNDARY_UNBOUNDED
        assert body["enforcement"]["job_error"] == reason
        assert body["enforcement"]["memory_enforced"] is False

        bundle = MCPBundle(statuses=[status]).as_dict()
        assert bundle["boundary"] == BOUNDARY_UNBOUNDED
        assert bundle["unbounded_servers"] == ["fake"]
    finally:
        await client.close()
    assert client._process is None


async def test_job_creation_failure_is_marked_unbounded(monkeypatch):
    await _assert_unbounded_but_usable(monkeypatch, "create")


async def test_job_assignment_failure_is_marked_unbounded(monkeypatch):
    await _assert_unbounded_but_usable(monkeypatch, "assign")


# --------------------------------------------------------------------------- #
# 6. HTTP：进程在远端，不许写成 Job 或本机宿主
# --------------------------------------------------------------------------- #
async def test_http_server_is_reported_as_remote():
    def factory(**kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            message = json.loads(request.content)
            if message.get("id") is None:
                return httpx.Response(202)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "remote"}}
            else:
                result = {"tools": [{"name": "ping"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    client = HttpClient(
        ServerConfig(name="remote", transport="http", url="https://example.test/mcp", client_factory=factory)
    )
    await client.start()
    try:
        facts = client.boundary_facts()
        assert facts["boundary"] == BOUNDARY_REMOTE
        assert "远端" in facts["note"]
        assert facts["job_created"] is False and facts["memory_enforced"] is False

        body = MCPStatus(
            "remote", "http", STATE_READY, boundary=facts["boundary"], boundary_note=facts["note"]
        ).as_dict()
        assert body["boundary"] == BOUNDARY_REMOTE
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# 7. 汇总与证据：最坏的一条决定口径
# --------------------------------------------------------------------------- #
def test_bundle_takes_the_worst_boundary():
    statuses = [
        MCPStatus("local-ok", "stdio", STATE_READY, boundary=BOUNDARY_JOB, boundary_note="已挂 Job"),
        MCPStatus("remote", "http", STATE_READY, boundary=BOUNDARY_REMOTE, boundary_note="远端"),
    ]
    mixed = MCPBundle(statuses=statuses).as_dict()
    assert mixed["boundary"] == "mixed"
    assert mixed["by_boundary"] == {BOUNDARY_JOB: 1, BOUNDARY_REMOTE: 1}
    assert mixed["unbounded_servers"] == []

    statuses.append(MCPStatus("bad", "stdio", STATE_READY, boundary=BOUNDARY_UNBOUNDED, boundary_note="⚠ 没挂上"))
    worst = MCPBundle(statuses=statuses).as_dict()
    assert worst["boundary"] == BOUNDARY_UNBOUNDED, "有服务端没进 Job 时不许把整包说成已纳入边界"
    assert worst["unbounded_servers"] == ["bad"]
    assert "bad" in worst["boundary_note"] and "⚠" in worst["boundary_note"]


def test_evidence_pass_through_keeps_unbounded_servers():
    bundle = MCPBundle(
        statuses=[
            MCPStatus("ok", "stdio", STATE_READY, boundary=BOUNDARY_JOB),
            MCPStatus("bad", "stdio", STATE_READY, boundary=BOUNDARY_UNBOUNDED, boundary_note="⚠ 注入"),
        ]
    ).as_dict()
    report = boundary_report([], {"mcp": bundle})
    assert report["mcp"]["boundary"] == BOUNDARY_UNBOUNDED
    assert report["mcp"]["unbounded_servers"] == ["bad"]
    assert report["mcp"]["by_boundary"] == {BOUNDARY_JOB: 1, BOUNDARY_UNBOUNDED: 1}
    assert report["mcp"]["servers"][1]["boundary"] == BOUNDARY_UNBOUNDED


# --------------------------------------------------------------------------- #
# 8. mcp.json 的限额配置 → 真 Job
# --------------------------------------------------------------------------- #
def test_parse_servers_reads_limits_and_drops_garbage():
    servers, _ = parse_servers(
        {
            "mcpServers": {
                "tight": {"command": "x", "memory": "64m", "pids": 8},
                "garbage": {"command": "y", "memory": "64 megabytes"},
                "default": {"command": "z"},
            }
        }
    )
    by_name = {server.name: server for server in servers}
    assert by_name["tight"].memory == "64m" and by_name["tight"].pids == 8
    # 认不出来的单位退回空串＝用沙箱默认值，不许悄悄变成"不限"。
    assert by_name["garbage"].memory == "" and by_name["garbage"].pids == 0
    assert by_name["default"].memory == "" and by_name["default"].pids == 0


@needs_windows
async def test_manager_reports_the_job_limits_it_really_applied(tmp_path: Path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"tight": {
                "command": sys.executable,
                "args": [str(SERVER), "normal"],
                "timeout": 15,
                "memory": "320m",
                "pids": 12,
            }}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = MCPManager(config)
    try:
        bundle = await manager.bundle()
        body = bundle.as_dict()
        assert body["boundary"] == BOUNDARY_JOB
        status = body["servers"][0]
        assert status["state"] == "ready"
        assert status["boundary"] == BOUNDARY_JOB
        assert status["enforcement"]["memory_bytes"] == 320 * 1024 * 1024
        assert status["enforcement"]["pids_limit"] == 12
        assert status["enforcement"]["job_assigned"] is True
    finally:
        await manager.close()


# --------------------------------------------------------------------------- #
# 9. 既有路径回归：崩溃的服务端在 start 里就把进程与 Job 收掉
# --------------------------------------------------------------------------- #
async def test_start_failure_does_not_leave_a_job_or_process_behind():
    probe = StdioClient(_stdio(mode="crash"))
    with pytest.raises(MCPError):
        await probe.start()
    process = probe._process
    assert probe._job is None
    await probe.close()  # 幂等：调用方再 close 一次不该炸
    assert probe._process is None
    assert process is not None and process.returncode is not None
