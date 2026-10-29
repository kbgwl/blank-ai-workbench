"""#93/#105：fs-broker——文件工具进子进程执行边界，IPC 延迟与每一层降级都要能失败。

设计稿 `docs/SANDBOX-WINDOWS-DESIGN.md` §9.2 挂的那条「文件工具全在 API 进程里跑」由这个
模块收口：列目录/读/搜索/补丁四个动作可以放进一枚专职子进程，子进程挂进 Job Object，
每次调用记 IPC 往返延迟。判据钉五组，每一件都翻得红（"什么缺陷会让它红"写在每条 docstring 里）：

1. **默认关**：开关关着时文件工具的行为与"没有这个模块"逐字一致（数据里不许长出 broker 标记）；
2. **换了执行体，没换语义**：同一批参数在 broker 与宿主两侧跑出**相同**的 output 与结构，
   越界路径与坏正则的**失败形状**也相同——broker 不造第二套文件语义；
3. **降级必须说出来**：spawn/protocol/timeout/killed/cancelled 每一层坏掉都让本次调用**失败**，
   `fs_broker_error.layer` 指认是哪一层，`fallback` 永远写 `none_refused`；
   失败还分两种副作用口径（`side_effects`）：请求没进 broker 的写 `none`（磁盘上真的什么都没发生——
   文件没改、备份没写、`FileChange` 没入库），请求进过 broker 的写 `unknown`；
4. **随任务终止**：取消一次调用、释放一个任务、宿主进程急死，broker 都必须从 tasklist 里消失；
5. **延迟是量出来的，不是编出来的**：上界来自本机实测（见下面两个常数的注释），
   另有一条"注入 120ms 睡眠 ⇒ `last_ms` 必须 >= 120"专打"写死一个常数"或"发出去就当往返完成"。

`_pid_alive` 用 tasklist 而不是 `os.kill(pid, 0)`：Windows 上后者会 OpenProcess+TerminateProcess，
**点验脚本自己就把 broker 杀了**——那样的"已死"读数是脚本造的，不是边界给的。

失败层与延迟的期望值来自 2026-10-26 的多轮真跑（`E:/tmp/m105/probe-1.log`、`probe2.txt`、
`bench-run-1246.txt`），不是按代码读出来的想当然。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.db import get_session_factory
from blank_api.models.task import FileChange
from blank_api.services import features
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.sandbox import windows_job
from blank_api.services.tools import build_default_tools
from blank_api.services.tools import fs_broker
from blank_api.services.tools.base import ToolOutcome
from blank_api.services.tools.filesystem import _list_files, _read_file, _search_code
from blank_api.services.tools.gateway import (
    BOUNDARY_BLOCKED,
    BOUNDARY_FS_BROKER,
    BOUNDARY_IN_PROCESS,
    FS_SIDE_EFFECTS_UNKNOWN,
    ToolGateway,
    _boundary_kind,
    sandbox_backend_event_field,
)
from blank_api.services.tools.patch import _apply_patch
from blank_api.services.tools.paths import BACKUP_DIRNAME

from tests.test_agent_v04 import (  # noqa: F401  (复用夹具)
    create_project,
    create_task,
    env,
)

TASK = "task-fs-broker"
FLAG = "BLANK_FEATURE_FS_BROKER"
# 上界来源：本机实测（2026-10-26，`E:/tmp/m105/bench-run-1246.txt` 第 3 行，真网关调用 65 个样本）
# p50=0.646ms / p95=1.951ms；同日另一轮 65 样本 p50=0.717ms / p95=1.87ms。
# 取 40ms ≈ 实测 p95 的 20 倍：够挡住机器抖动，但慢一个数量级（协议退化、每次重建进程）必须被抓到。
IPC_P95_UPPER_BOUND_MS = 40.0
# 注入延迟判据用的睡眠：这一条不比对数，只比对"读数会不会跟着真实耗时走"。
IPC_DELAY_PROBE_MS = 120.0

READ_PATCH = "--- a/src/core.py\n+++ b/src/core.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"


def _pid_alive(pid: int) -> bool:
    """问内核这个 pid 还在不在：tasklist 是唯一可信的读数。

    中文 Windows 上 tasklist 的输出是 GBK（本机环境笔记里有案底），按 UTF-8 解会当场炸读取线程——
    用 bytes 收再宽松解码；pid 是纯数字，不受编码影响。
    """
    if pid <= 0:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, check=False)
    return str(pid) in (out.stdout or b"").decode("utf-8", "ignore")


# 这一组判的是**内核真的动手了**：broker 挂在 Windows Job Object 上、"没了"要靠
# `tasklist` 观察。Linux 上没有 Job Object，产品那条路会如实报 job_created=False，
# 于是这些断言在这里永远不成立——不是产品坏了，是判据要求的观察手段在这个平台不存在。
# 2026-10-29 CI 首次在 Linux 上跑全量时，这七条一起红成了 tasklist/Job Object 的形状。
needs_windows_boundary = pytest.mark.skipif(
    os.name != "nt",
    reason="这一条靠 Windows Job Object + tasklist 判「broker 是不是真的没了」，Linux 上没有这两样",
)


@pytest.fixture(autouse=True)
def _clean_broker_managers():
    """每个用例前后各收一次尸。

    换 manager 不关上一任的 Job，就会留下孤儿 broker 子进程：这些进程是产品代码起的，
    不是测试自己造的，靠 `close_all_for_exit()`（关 Job → 内核带走整棵树）收。
    """
    fs_broker.manager().close_all_for_exit()
    fs_broker.reset_manager_for_tests()
    yield
    fs_broker.manager().close_all_for_exit()
    fs_broker.reset_manager_for_tests()


async def _context(env, root: Path):
    project_id = await create_project(env, root)
    task_id = await create_task(env, project_id, ["fs-broker 点验"])
    return env.context(task_id, root)


def _enable(monkeypatch, on: bool) -> None:
    monkeypatch.setenv(FLAG, "1" if on else "0")


def _error_of(result) -> dict:
    data = result.data if isinstance(result, ToolOutcome) else result.get("data") or {}
    return (data or {}).get("fs_broker_error") or {}


def _marker_of(result) -> dict:
    data = result.data if isinstance(result, ToolOutcome) else result.get("data") or {}
    return (data or {}).get("fs_broker") or {}


def _client(root: Path, task_id: str) -> fs_broker.FsBrokerClient:
    """取 manager 手里那枚 client（只为"外部杀死它"这一件事，不用于判定）。"""
    return fs_broker.manager()._clients[(str(Path(root).resolve()), str(task_id))]


async def _wait_client(root: Path, task_id: str, seconds: float = 20.0) -> fs_broker.FsBrokerClient:
    """等 broker 起完（握手结束、client 入册）。

    子进程要解释器冷启动 + import 整个包，实测 0.5–1.5s：直接 sleep 固定值会时快时慢，
    判据就跟着抖。这里等到"确实起起来了"再继续。
    """
    key = (str(Path(root).resolve()), str(task_id))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        client = fs_broker.manager()._clients.get(key)
        if client is not None:
            return client
        await asyncio.sleep(0.1)
    raise AssertionError(f"{seconds}s 内 broker 没有起起来：{key}")


async def _kill(pid: int) -> None:
    """用系统的 taskkill 杀：与"用户/内核把进程点名"同形，不是 asyncio 自己 cancel。"""
    killer = await asyncio.create_subprocess_exec(
        "taskkill", "/PID", str(pid), "/F",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await killer.wait()
    # 让 reader 循环有机会把"管道到头"记成"broker 已关闭"——否则本次调用会走写请求那条分支。
    await asyncio.sleep(0.3)


async def _rows(task_id: str) -> list[FileChange]:
    async with get_session_factory()() as session:
        return list((await session.execute(select(FileChange).where(FileChange.task_id == task_id))).scalars())


async def _wait_gone(pid: int, seconds: float = 20.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.25)
    return False


def _live_brokers() -> list[int]:
    return [
        client.pid
        for client in fs_broker.manager()._clients.values()
        if client.pid and _pid_alive(client.pid)
    ]


# --------------------------------------------------------------------------- #
# 1. 默认关：开关不生效时不留任何 broker 痕迹
# --------------------------------------------------------------------------- #
def test_the_flag_is_registered_and_off_by_default(monkeypatch):
    """缺陷红法：注册表把 `fs_broker` 的默认值改成"开"而没做全量证明，或 stage 不再约束默认值 → 红。

    关着时文件工具走原宿主路径，行为逐字不变——这是"不破坏现有链路"那条硬约束的落点。
    """
    flag = features.FEATURES["fs_broker"]
    assert flag.stage == features.BETA
    assert flag.default is False, "fs-broker 是行为变更，转正必须由全量回归 + 用户裁决，不许默认开"
    monkeypatch.delenv(FLAG, raising=False)
    assert features.is_enabled("fs_broker") is False
    # 设置文件里也没人替它开过：注册表里的 default 就是生效值
    assert features.resolve("fs_broker")[1] == features.SRC_DEFAULT
    assert fs_broker.broker_enabled() is False


def test_the_side_effect_vocabulary_is_not_forked():
    """缺陷红法：网关为了不在热路径 import broker 而镜像了一个字面值——两处一旦漂移，
    "unknown 的失败仍记在 broker 边界上"这条判定就会一半对一半错。"""
    assert FS_SIDE_EFFECTS_UNKNOWN == fs_broker.SIDE_EFFECTS_UNKNOWN
    assert fs_broker.SIDE_EFFECTS_NONE == "none"


async def test_the_host_path_carries_no_broker_markers(env, workspace: Path, monkeypatch):  # noqa: F811
    """缺陷红法：关着开关 handler 却接了 broker（默认值失效、接线写反）→ 这里长出 broker 键就红。"""
    _enable(monkeypatch, False)
    context = await _context(env, workspace)
    outcome = await _list_files(context, {"path": ""})
    assert outcome.ok is True, outcome.message
    assert "fs_broker" not in outcome.data
    assert "fs_broker_latency" not in outcome.data
    # 边界账要如实说"在 API 进程里"，不能因为模块存在就写成功
    assert _boundary_kind("list_files", outcome, False) == BOUNDARY_IN_PROCESS
    gateway = ToolGateway(build_default_tools())
    payload = await gateway.execute("read_file", {"path": "README.md"}, context, "execute_action")
    assert payload["ok"] and payload["boundary_kind"] == BOUNDARY_IN_PROCESS
    assert "fs_broker_latency" not in payload


# --------------------------------------------------------------------------- #
# 2. 开了就在子进程里跑，并把边界事实写全
# --------------------------------------------------------------------------- #
async def test_a_broker_call_executes_in_a_child_and_books_ipc_latency(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：Job 挂载回归（create_job/assign_pid 任一坏掉却把账写成 True）、
    延迟字段缺席、或把 0 当"没说"（`or -1` 那类比较）→ 红。"""
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    outcome = await _list_files(context, {"path": ""})
    assert outcome.ok is True, outcome.message
    marker = _marker_of(outcome)
    assert marker.get("executed_by") == "fs_broker_subprocess", marker
    assert marker.get("op") == "list_tree"
    assert marker.get("protocol") == fs_broker.PROTOCOL_VERSION
    broker_pid = int(marker.get("broker_pid") or 0)
    # "在子进程里"唯一的硬证据：这个 pid 不是本进程的
    assert broker_pid > 0 and broker_pid != os.getpid(), marker
    # Job 那三格按平台各说各话：Windows 上必须真挂上，挂不上就不是"进了边界"
    if windows_job.supported():
        assert marker.get("job_created") is True, marker
        assert marker.get("job_assigned") is True, marker
        assert marker.get("job_tree_kill") is True, marker
        assert marker.get("job_memory_bytes") > 0, marker
    else:
        assert marker.get("job_created") is False, marker
        assert str(marker.get("job_error") or ""), "没挂上 Job 却不说是为什么：这一格是静默降级"
    assert _boundary_kind("list_files", outcome, False) == BOUNDARY_FS_BROKER
    latency = outcome.data["fs_broker_latency"]
    assert latency["samples"] == 1
    # 三件套必须是真数：0 不许被读成"没说"，也不许拿 -1 兜底
    assert 0.0 < latency["p50_ms"] <= latency["p95_ms"] <= IPC_P95_UPPER_BOUND_MS
    assert latency["last_ms"] > 0.0


async def test_the_latency_reading_moves_with_the_time_the_broker_wastes(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """让 broker 每条响应前睡 120ms：`last_ms` 必须跟着涨。

    把延迟写成常数、或只量"发出去没发出去"而不等回话，都会在这一条上红。
    """
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_TEST_DELAY_MS", str(int(IPC_DELAY_PROBE_MS)))
    context = await _context(env, workspace)
    first = await _list_files(context, {"path": ""})
    assert first.ok is True, first.message
    one = first.data["fs_broker_latency"]
    assert one["samples"] == 1
    assert one["last_ms"] >= IPC_DELAY_PROBE_MS, one
    second = await _read_file(context, {"path": "main.py"})
    two = second.data["fs_broker_latency"]
    # 窗口按调用累计：第二次必须把两次都算进去，样本数是最便宜的实现偷懒处
    assert two["samples"] == 2, two
    assert two["last_ms"] >= IPC_DELAY_PROBE_MS, two
    assert two["p50_ms"] <= two["p95_ms"], two


async def test_ipc_latency_stays_under_the_measured_bound(env, workspace: Path, monkeypatch):  # noqa: F811
    """先量后写的上界断言（40 次真调用）。

    缺陷红法：broker 变慢一个数量级——协议退化（每帧多一次协商）、每次调用重建进程、
    或延迟字段是编出来的常数——都会在这里红。常数写死成 0 或 -1 的实现在上一条就红了。
    """
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    gateway = ToolGateway(build_default_tools())
    payload = None
    for _ in range(40):
        payload = await gateway.execute("read_file", {"path": "README.md"}, context, "execute_action")
        assert payload["ok"], payload
    stats = payload["fs_broker_latency"]
    assert stats["samples"] >= 40, stats
    assert 0.0 < stats["p50_ms"] <= stats["p95_ms"] <= IPC_P95_UPPER_BOUND_MS, stats
    # 分位数来自滚动窗口，不是单点噪声冒充
    assert stats["p95_ms"] >= stats["p50_ms"]


# --------------------------------------------------------------------------- #
# 3. 换了执行体，不许换语义
# --------------------------------------------------------------------------- #
async def test_the_broker_and_the_host_run_the_same_file_semantics(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：broker 侧另写一份渲染/汇总逻辑（output 或结构漂移）→ 红。"""
    _enable(monkeypatch, False)
    context = await _context(env, workspace)
    host_list = await _list_files(context, {"path": ""})
    host_search = await _search_code(context, {"pattern": "VALUE", "mode": "literal"})
    _enable(monkeypatch, True)
    broker_list = await _list_files(context, {"path": ""})
    broker_search = await _search_code(context, {"pattern": "VALUE", "mode": "literal"})
    assert broker_list.output == host_list.output
    assert broker_list.data["files"] == host_list.data["files"]
    assert broker_list.data["count"] == host_list.data["count"] == len(host_list.data["files"])
    assert broker_search.data["matches"] == host_search.data["matches"]
    assert broker_search.output == host_search.output
    # 搜索/列目录在 broker 侧也是同一条实现：marker 只多说"在哪跑的"，不少说结果
    assert _marker_of(broker_search)["op"] == "search"
    assert broker_search.data["count"] == host_search.data["count"]


async def test_an_escape_reds_the_same_way_on_both_sides_and_does_not_poison_the_broker(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """越界是**参数**被拒，不是边界坏了：broker 侧必须原样重建 ApiError，且下一句照常能用。

    实测（`E:/tmp/m105/probe2.txt` 第 4–5 行）：宿主与 broker 都抛
    `ApiError(code=path_escape_denied, status_code=403)`，越界之后 pid 不变。
    缺陷红法：broker 里 `resolve_inside/resolve_for_write` 被绕过（越界读成功）→ 红——
    这正是边界存在的意义；把越界误判成"broker 中毒"（下次调用白重生一回）也红。
    """
    _enable(monkeypatch, False)
    context = await _context(env, workspace)
    (workspace.parent / "outside.txt").write_text("SyntheticNotARealKey0001\n", encoding="utf-8")
    with pytest.raises(ApiError) as host_exc:
        await _read_file(context, {"path": "../outside.txt"})

    _enable(monkeypatch, True)
    warm = await _list_files(context, {"path": ""})
    pid_before = _marker_of(warm)["broker_pid"]
    with pytest.raises(ApiError) as broker_exc:
        await _read_file(context, {"path": "../outside.txt"})
    assert broker_exc.value.code == host_exc.value.code == "path_escape_denied"
    assert broker_exc.value.status_code == host_exc.value.status_code == 403
    assert "SyntheticNotARealKey0001" not in str(broker_exc.value.message)
    after = await _read_file(context, {"path": "main.py"})
    assert after.ok is True, after.message
    assert _marker_of(after)["broker_pid"] == pid_before, "越界被当成 broker 中毒：下次调用白重生一回"
    # 补丁侧同样被闸住（走 resolve_for_write），而且磁盘上什么都没留下：
    # ApiError(403) 经网关读成 denied——broker 抛出的是同一个 code/status，不是另一种拒绝形状。
    gateway = ToolGateway([tool for tool in build_default_tools() if tool.name == "apply_patch"])
    denied = await gateway.execute(
        "apply_patch", {"path": "../escaped.py", "patch": "@@ -0,0 +0,1 @@\n+x = 1\n"},
        context, "execute_action",
    )
    assert not denied["ok"] and denied["status"] == "denied", denied
    assert denied["data"]["error_code"] == "path_escape_denied", denied
    assert not (workspace.parent / "escaped.py").exists()
    assert await _rows(context.task_id) == [], "被拒的补丁进了变更账"


async def test_a_bad_regex_fails_with_the_host_message_not_a_second_wording(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：正则编译挪进 broker 后自己写了一句错误文案（两侧口径分叉）→ 红。"""
    _enable(monkeypatch, False)
    context = await _context(env, workspace)
    with pytest.raises(ValueError) as host_exc:
        await _search_code(context, {"pattern": "(", "mode": "regex"})
    _enable(monkeypatch, True)
    with pytest.raises(ValueError) as broker_exc:
        await _search_code(context, {"pattern": "(", "mode": "regex"})
    assert str(broker_exc.value) == str(host_exc.value)
    assert str(broker_exc.value).startswith("正则表达式无效：")
    # 同一份实现也保住"坏正则不毒 broker"：坏的是参数
    ok = await _search_code(context, {"pattern": r"VALUE\s*=\s*\d", "mode": "regex"})
    assert ok.ok is True, ok.message
    # 二进制拒读的文案也一致（read_text_file 在同一份实现里判二进制）
    host_binary = None
    _enable(monkeypatch, False)
    host_binary = await _read_file(context, {"path": "logo.png"})
    _enable(monkeypatch, True)
    broker_binary = await _read_file(context, {"path": "logo.png"})
    assert host_binary.message == broker_binary.message
    assert "二进制" in broker_binary.message


async def test_a_patch_through_the_broker_writes_once_with_backup(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：broker 与宿主的补丁语义漂移（备份没写、diff 不对、写了两遍、变更账没记）→ 红。"""
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    gateway = ToolGateway(build_default_tools())
    payload = await gateway.execute("apply_patch", {"path": "src/core.py", "patch": READ_PATCH},
                                    context, "execute_action")
    assert payload["ok"], payload
    assert (workspace / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert payload["boundary_kind"] == BOUNDARY_FS_BROKER
    assert payload["data"]["fs_broker"]["op"] == "patch"
    assert payload["data"]["backup_path"].startswith(f"{BACKUP_DIRNAME}/")
    assert (workspace / payload["data"]["backup_path"]).read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "+VALUE = 2" in payload["data"]["diff"] and "-VALUE = 1" in payload["data"]["diff"]
    rows = await _rows(context.task_id)
    assert len(rows) == 1, f"一次补丁记了 {len(rows)} 条变更账"
    assert rows[0].path == "src/core.py"


# --------------------------------------------------------------------------- #
# 4. 每一层坏掉都说得清是哪一层，而且绝不回退宿主
# --------------------------------------------------------------------------- #
async def test_a_broker_that_will_not_start_refuses_the_patch_and_touches_nothing(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：spawn 失败被吞、或失败后 handler 回宿主再跑一遍 → 磁盘会变、账会多一条。"""
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_PYTHON", "E:/definitely/not/here/python.exe")
    context = await _context(env, workspace)
    target = workspace / "src" / "core.py"
    before = target.read_bytes()
    outcome = await _apply_patch(context, {"path": "src/core.py", "patch": READ_PATCH})
    error = _error_of(outcome)
    assert outcome.ok is False and outcome.status == "error", outcome
    assert error.get("layer") == fs_broker.LAYER_SPAWN, error
    assert error.get("fallback") == "none_refused", error
    assert error.get("op") == "patch", error
    assert error.get("side_effects") == fs_broker.SIDE_EFFECTS_NONE, error
    assert "起不来" in error.get("reason", ""), error
    # 红线判据：不是"报了错"就算数，是这次动作**根本没发生**
    assert target.read_bytes() == before, "broker 起不来却把补丁写进了宿主——静默回退"
    assert not (workspace / BACKUP_DIRNAME).exists(), "宿主回退连备份都写了：这次动作确实发生过"
    assert await _rows(context.task_id) == [], "失败的调用留下一条变更账"
    assert _boundary_kind("apply_patch", outcome, False) == BOUNDARY_BLOCKED


async def test_a_protocol_mismatch_names_both_sides_and_refuses_to_fall_back(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：握手不比对版本（新旧两端各说各话还"成功"）→ 红。"""
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_TEST_PROTOCOL", "999")
    context = await _context(env, workspace)
    target = workspace / "src" / "core.py"
    before = target.read_bytes()
    outcome = await _apply_patch(context, {"path": "src/core.py", "patch": READ_PATCH})
    error = _error_of(outcome)
    assert outcome.ok is False, outcome
    assert error.get("layer") == fs_broker.LAYER_PROTOCOL, error
    assert f"v{fs_broker.PROTOCOL_VERSION}" in error.get("reason", ""), error
    assert "v999" in error.get("reason", ""), error
    assert error.get("fallback") == "none_refused", error
    assert error.get("side_effects") == fs_broker.SIDE_EFFECTS_NONE, error
    assert target.read_bytes() == before
    assert _boundary_kind("apply_patch", outcome, False) == BOUNDARY_BLOCKED


@needs_windows_boundary
async def test_a_killed_broker_fails_the_call_and_reincubates_in_a_child_never_the_host(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """任务要求的那条真实致红读数：broker 被杀 ⇒ 本次调用失败且留痕，磁盘上什么都没发生。

    这条杀在**两次调用之间**，走的是"调用到达时进程已经没了"那条短路分支（side_effects=none）。
    缺陷红法：只要存在"broker 死了就回宿主执行"的分支，补丁会真的写进磁盘、状态会是 ok → 红。
    "请求已经在飞行途中被杀"是另一条分支（层的名字会不会退化成 timeout），由
    `test_a_kill_while_a_request_is_in_flight_is_named_killed_not_timeout` 单独守。
    """
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    first = await _list_files(context, {"path": ""})
    old_pid = int(_marker_of(first)["broker_pid"])
    assert _pid_alive(old_pid)
    await _kill(old_pid)
    assert not _pid_alive(old_pid)

    target = workspace / "src" / "core.py"
    before = target.read_bytes()
    outcome = await _apply_patch(context, {"path": "src/core.py", "patch": READ_PATCH})
    error = _error_of(outcome)
    assert outcome.ok is False, f"broker 死了调用却成功——必然走了宿主回退：{outcome}"
    assert outcome.status == "error"
    assert error.get("layer") == fs_broker.LAYER_KILLED, error
    assert "未回退宿主执行" in error.get("reason", ""), error
    assert error.get("fallback") == "none_refused", error
    assert error.get("side_effects") == fs_broker.SIDE_EFFECTS_NONE, error
    assert "退出码" in error.get("reason", ""), error
    assert _boundary_kind("apply_patch", outcome, False) == BOUNDARY_BLOCKED
    assert target.read_bytes() == before, "磁盘被宿主路径写过——红线破了"
    assert await _rows(context.task_id) == [], "被杀的调用留下一条变更账"

    # 下一次调用重生（还是 broker，不许"算了直接用宿主"）
    revived = await _list_files(context, {"path": ""})
    assert revived.ok is True, revived.message
    assert _boundary_kind("list_files", revived, False) == BOUNDARY_FS_BROKER
    new_pid = int(_marker_of(revived)["broker_pid"])
    assert new_pid != old_pid and new_pid > 0, (new_pid, old_pid)


@needs_windows_boundary
async def test_a_timeout_says_which_layer_and_refuses_to_claim_nothing_happened(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """超时这一层的诚实话：调用失败、broker 已被收尸，但**磁盘动作不可知**。

    实测（2026-10-26，`E:/tmp/m105/v93-1.log` 第 26–30 行）：注入 800ms 睡眠、0.2s 超时，
    `main.py` 真的被 broker 改成了新内容（补丁在 broker 里执行完才睡），而调用方只看得见"超时"。
    把这条失败念成"没执行"（`side_effects=none` + `blocked`）就是说假话——本条把实测读数钉住：
    磁盘确实变了，而库里一条变更账都没有，这正是 unknown 的含义。
    缺陷红法：把超时写成"什么都没发生"、或超时后还复用同一枚 broker → 红。
    """
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_TEST_DELAY_MS", "800")
    monkeypatch.setenv("BLANK_FS_BROKER_TIMEOUT", "0.2")
    context = await _context(env, workspace)
    target = workspace / "main.py"
    before = target.read_bytes()
    outcome = await _apply_patch(
        context, {"path": "main.py", "patch": "@@ -1,1 +1,1 @@\n-print('hi')\n+print('bye')\n"}
    )
    error = _error_of(outcome)
    assert outcome.ok is False and outcome.status == "error", outcome
    assert error.get("layer") == fs_broker.LAYER_TIMEOUT, error
    assert "op=patch" in error.get("reason", ""), error
    assert "未回退宿主执行" in error.get("reason", ""), error
    assert error.get("fallback") == "none_refused", error
    assert error.get("side_effects") == fs_broker.SIDE_EFFECTS_UNKNOWN, error
    # 边界账不能替这次动作开脱：它落在 broker 里，不是"根本没执行"（blocked）
    assert _boundary_kind("apply_patch", outcome, False) == BOUNDARY_FS_BROKER
    await asyncio.sleep(1.0)  # 等 broker 那一侧把已执行的写入落定、收尸完成
    assert target.read_bytes() != before, "实测里 broker 已经把补丁落盘；改成'没发生'的实现会在这红"
    assert target.read_bytes() == b"print('bye')\r\n", target.read_bytes()
    assert await _rows(context.task_id) == [], "超时了却把变更账写了：主进程根本没拿到结果"
    assert _live_brokers() == [], "中毒的 broker 还被留在手里（下一个调用会撞上半截响应）"


@needs_windows_boundary
async def test_a_cancelled_call_raises_cancellation_and_takes_the_broker_with_it(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：取消路径不收尸（broker 活着继续接单）→ 红；取消被吞成"没取消" → 红。"""
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_TEST_DELAY_MS", "4000")
    context = await _context(env, workspace)
    task = asyncio.create_task(
        fs_broker.call_broker(workspace, context.task_id, "read_text_file", {"path": "main.py"})
    )
    client = await _wait_client(workspace, context.task_id)  # 握手完成，业务请求正在 4s 的睡眠里
    pid = client.pid
    assert pid and _pid_alive(pid)
    task.cancel()
    with pytest.raises(fs_broker.FsBrokerError) as excinfo:
        await task
    assert excinfo.value.layer == fs_broker.LAYER_CANCELLED, excinfo.value
    assert "op=read_text_file" in excinfo.value.reason, excinfo.value
    assert excinfo.value.side_effects == fs_broker.SIDE_EFFECTS_UNKNOWN, excinfo.value
    assert await _wait_gone(pid, 10.0), f"取消后 broker pid={pid} 还活着——它没随任务终止"
    assert _live_brokers() == []


@needs_windows_boundary
async def test_a_kill_while_a_request_is_in_flight_is_named_killed_not_timeout(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """请求已经在管道里、broker 正在干活时被点名 ⇒ 层的名字必须是 killed。

    上面那条「被杀」用例杀在两次调用之间，走的是"调用开始前进程已经没了"那条短路分支；
    这一条杀在**飞行途中**，量的是 `_roundtrip` 同时等"响应到达"和"进程死亡"那条边。
    只等响应的话，broker 被 taskkill 之后请求静静躺在管道缓冲里，超时才把"被杀"读成
    "超时"——层的名字错了，指认就废了（2026-10-26 变异电池量出：把 `death` 换成一个永不
    完成的等待，原 20 条全绿，说明这条分支上压根没有判据）。
    缺陷红法：不等进程死亡 → 报成 timeout；把飞行途中的死亡说成"什么都没发生" → 副作用口径红。
    """
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_TEST_DELAY_MS", "5000")
    monkeypatch.setenv("BLANK_FS_BROKER_TIMEOUT", "30")
    context = await _context(env, workspace)
    task = asyncio.create_task(_read_file(context, {"path": "main.py"}))
    client = await _wait_client(workspace, context.task_id)
    pid = client.pid
    assert pid and _pid_alive(pid)
    for _ in range(200):  # 等到请求真的进了管道，否则量的还是"调用前就死了"那条分支
        if client._pending:
            break
        await asyncio.sleep(0.05)
    assert client._pending, "装置失效：请求没进 broker，这一格量的飞行途中死亡压根没发生"
    await _kill(pid)
    outcome = await task
    error = _error_of(outcome)
    assert outcome.ok is False and outcome.status == "error", outcome
    assert error.get("layer") == fs_broker.LAYER_KILLED, f"被杀读成了 {error.get('layer')}：{error}"
    assert "退出码" in error.get("reason", ""), error
    assert error.get("side_effects") == fs_broker.SIDE_EFFECTS_UNKNOWN, (
        f"请求已经进过 broker，磁盘动作不可知却报成 {error.get('side_effects')}"
    )
    assert _boundary_kind("read_file", outcome, False) == BOUNDARY_FS_BROKER, error
    assert _live_brokers() == []


# --------------------------------------------------------------------------- #
# 5. 随任务终止：Job 的 KILL_ON_JOB_CLOSE 不是文案，是 tasklist 读数
# --------------------------------------------------------------------------- #
@needs_windows_boundary
async def test_releasing_a_task_takes_its_broker_with_it(env, workspace: Path, monkeypatch):  # noqa: F811
    """任务终止后 broker 不该还活着：KILL_ON_JOB_CLOSE 是这条边界的收尾承诺。

    缺陷红法：`release_task` 只 pop 字典不关 Job、也不 kill → pid 还在 tasklist 里 → 红。
    注意这一格只问"人没了没有"，分不清是被谁带走的（`hard_stop` 里还有一刀兜底 kill），
    "关 Job 这件事本身"由下一格 `test_the_kernel_kills_the_broker_not_our_own_fallback_kill` 钉。
    """
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    outcome = await _list_files(context, {"path": ""})
    pid = int(_marker_of(outcome)["broker_pid"])
    assert _pid_alive(pid)
    await fs_broker.manager().release_task(context.task_id)
    assert await _wait_gone(pid, 10.0), f"broker pid={pid} 在任务释放后仍存活"
    assert fs_broker.manager()._clients == {}, "释放任务后 manager 还留着这枚 client"


@needs_windows_boundary
async def test_the_kernel_kills_the_broker_not_our_own_fallback_kill(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """收尸的正身是 Job 的 KILL_ON_JOB_CLOSE，`proc.kill()` 只是兜底——这句必须能被证伪。

    上一格只问"pid 还活着吗"，而 `hard_stop` 里等不到就先自己 kill 一刀：两种实现都能让
    tasklist 变空，于是"Job 真的接管了这个进程树"这句话没人守（2026-10-26 变异电池量出：
    把 `close_job` 那句删掉，原 20 条全绿）。这一格把兜底那刀装上探针——
    **它一次都不该被调用**，因为内核已经先把人带走了。
    缺陷红法：收尸不关 Job、只剩自己 kill → 探针记到名字 → 红（这条区分了"内核带走整棵树"
    与"只杀了直接子进程"，broker 一旦派生过东西，两者就不是同一件事）。
    """
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    outcome = await _list_files(context, {"path": ""})
    assert outcome.ok, outcome
    client = await _wait_client(workspace, context.task_id)
    pid = client.pid
    assert client.job_facts["job_tree_kill"] is True, client.job_facts
    proc = client.proc
    assert proc is not None
    real_kill = proc.kill
    called: list[str] = []

    def _spy_kill() -> None:
        called.append("proc.kill")
        real_kill()

    proc.kill = _spy_kill  # 只装探针，不改变语义：真被调用时仍然杀得掉
    await fs_broker.manager().release_task(context.task_id)
    assert await _wait_gone(pid, 10.0), f"broker pid={pid} 没随任务消失"
    assert called == [], f"是我们自己 kill 掉的（{called}），不是 Job 的 KILL_ON_JOB_CLOSE"


@needs_windows_boundary
async def test_the_broker_dies_when_the_owning_process_dies_without_cleanup(workspace: Path, tmp_path):
    """宿主急死（os._exit：没有 finally、没有 atexit、没有 GC）之后 broker 也必须消失。

    两段读数缺一不可：宿主还活着时 broker **在** tasklist 里（否则第二条就是"根本没起过"的假绿），
    宿主 os._exit 之后 20 秒内它**不在**。
    缺陷红法：收尸只剩"管道 EOF 自退"这一层、Job 的 KILL_ON_JOB_CLOSE 没挂上 → 第二条红；
    create_subprocess_exec 没带 Job 句柄、退出时不关 → 也红。
    """
    src = str(Path(fs_broker.blank_api_dir()).parent)
    script = tmp_path / "owner.py"
    marker = tmp_path / "broker-pid.txt"
    script.write_text(
        """
import asyncio, os, sys, time
sys.path.insert(0, os.environ["BLANK_SRC"])
from blank_api.services.tools import fs_broker

async def main():
    envelope = await fs_broker.call_broker(sys.argv[1], "owner-exit-task", "hello", {})
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        fh.write(str(envelope["status"]["broker_pid"]))
        fh.flush()
    time.sleep(float(os.environ.get("BLANK_OWNER_ALIVE_SECONDS", "6")))
    os._exit(0)  # 急死：atexit / finally / GC 一律不跑

asyncio.run(main())
""".strip(),
        encoding="utf-8",
    )
    owner = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(script), str(workspace), str(marker)],
        env={**os.environ, "BLANK_SRC": src, "PYTHONPATH": src, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 90
    while not marker.exists() and loop.time() < deadline:
        await asyncio.sleep(0.2)
    assert marker.exists(), "宿主没跑到写 pid 那步（broker 没起来或握手失败）"
    pid = int(marker.read_text(encoding="utf-8"))
    assert _pid_alive(pid), f"宿主还活着时 broker pid={pid} 就该在 tasklist 里"
    # 等到宿主自己走完 os._exit（communicate 在线程里跑，不阻塞本测试的事件循环）
    await asyncio.to_thread(owner.communicate, 180)
    assert await _wait_gone(pid, 20.0), f"宿主已急死 20 秒后 broker(pid={pid}) 还在——没有随任务终止"


# --------------------------------------------------------------------------- #
# 6. 两笔账要进事件与证据包，不然界面看不见
# --------------------------------------------------------------------------- #
async def test_the_gateway_event_payload_carries_the_two_broker_accounts(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：事件白名单没放行这两组字段（证据包只能靠反推）、或失败的调用也带延迟 → 红。"""
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    gateway = ToolGateway(
        [tool for tool in build_default_tools() if tool.name in {"list_files", "read_file"}]
    )
    payload = await gateway.execute("list_files", {"path": ""}, context, "execute_action")
    assert payload["boundary_kind"] == BOUNDARY_FS_BROKER, payload
    assert isinstance(payload.get("fs_broker_latency"), dict), payload
    assert payload["fs_broker_latency"]["samples"] == 1
    event = sandbox_backend_event_field(payload)
    assert event["boundary_kind"] == BOUNDARY_FS_BROKER
    assert event["sandbox_mode"] == str(context.sandbox_tier or event["sandbox_mode"])
    assert event["fs_broker_latency"] == payload["fs_broker_latency"]
    assert "fs_broker_error" not in event


async def test_the_failure_reaches_the_event_as_a_layer_not_a_silent_denial(
    env, workspace: Path, monkeypatch  # noqa: F811
):
    """缺陷红法：失败只写一句 message 而不带 layer（界面与证据包看不出坏在哪层）→ 红。"""
    _enable(monkeypatch, True)
    monkeypatch.setenv("BLANK_FS_BROKER_PYTHON", "E:/definitely/not/here/python.exe")
    context = await _context(env, workspace)
    gateway = ToolGateway([tool for tool in build_default_tools() if tool.name == "read_file"])
    payload = await gateway.execute("read_file", {"path": "README.md"}, context, "execute_action")
    assert not payload["ok"], payload
    assert payload["boundary_kind"] == BOUNDARY_BLOCKED, payload
    assert payload["fs_broker_error"]["layer"] == fs_broker.LAYER_SPAWN, payload
    event = sandbox_backend_event_field(payload)
    assert event["boundary_kind"] == BOUNDARY_BLOCKED
    assert event["fs_broker_error"]["fallback"] == "none_refused"
    assert event["fs_broker_error"]["side_effects"] == fs_broker.SIDE_EFFECTS_NONE
    assert "fs_broker_latency" not in event, "失败的调用没有往返延迟可记"
    assert "README.md" not in str(payload.get("output", "")), "读到底的内容=宿主回退执行了"


async def test_the_evidence_pack_rolls_up_broker_facts(env, workspace: Path, monkeypatch):  # noqa: F811
    """证据包要能一眼回答：几次走 broker、慢不慢、坏过几次坏在哪层、其中有几次"结果不可知"。

    缺陷红法：`boundary_report` 的 counts/汇总丢字段（新边界种类没进计数）、或延迟取不到最大值 → 红。
    """
    _enable(monkeypatch, True)
    context = await _context(env, workspace)
    gateway = ToolGateway(build_default_tools())
    recorder = EventRecorder(get_session_factory(), context.task_id)
    good = await gateway.execute("list_files", {"path": ""}, context, "execute_action")
    assert good["ok"], good
    await recorder.add(
        "tool.completed", "execute_action",
        {"tool": "list_files", "tool_call_id": good["tool_call_id"], **sandbox_backend_event_field(good)},
    )
    for layer, effects, kind in (
        ("killed", fs_broker.SIDE_EFFECTS_NONE, BOUNDARY_BLOCKED),
        ("timeout", fs_broker.SIDE_EFFECTS_UNKNOWN, BOUNDARY_FS_BROKER),
    ):
        await recorder.add("tool.completed", "execute_action", {
            "tool": f"read_file_{layer}", "tool_call_id": f"call-{layer}",
            "boundary_kind": kind, "sandbox_mode": "workspace",
            "fs_broker_error": {"layer": layer, "reason": "点验桩", "op": "read_text_file",
                                "fallback": "none_refused", "side_effects": effects},
        })
    evidence = await build_evidence(get_session_factory(), context.task_id)
    boundaries = evidence["boundaries"]
    assert boundaries["tool_calls"]["fs_broker"] == 2, boundaries["tool_calls"]
    assert boundaries["tool_calls"]["blocked"] == 1, boundaries["tool_calls"]
    roll = boundaries["fs_broker"]
    assert roll["calls"] == 1, roll  # 只有成功的那次有往返延迟可算
    assert roll["failures_by_layer"] == {"killed": 1, "timeout": 1}, roll
    assert roll["failures_with_unknown_side_effects"] == 1, roll
    assert roll["latency"]["samples"] >= 1
    assert 0.0 < roll["latency"]["max_p95_ms"] <= IPC_P95_UPPER_BOUND_MS
    by_call = {row.get("name"): row for row in evidence["tools"]}
    assert by_call["list_files"]["fs_broker_latency"]["samples"] >= 1
