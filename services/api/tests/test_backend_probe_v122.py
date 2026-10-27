"""#122：执行后端到底能不能用，由现场探测给出，不看 PATH 里有没有那个文件。

`docs/COST.md` 把这条登记成「还没做」：老口径是 `shutil.which("docker") is not None`，
它只回答"这个文件名在不在 PATH 里"。装了 Docker Desktop 但守护进程没起来是常态，那种机器上
老口径一路把容器说成"可用"，界面写「可用」、任务照样起、然后每条命令各自撞一次墙。

这一批把三件事分开：未安装 / 已安装但守护进程没应答 / 真的能用，并且把读数带到
装配层（409 时就念得出成因）、带到设置页（一行读数 + 「几秒前探测」+ 重新探测）。

判据的形状沿用 `services/sandbox/boundary.py` 那三条铁律，全部用**真起的子进程**（临时目录里
放一个名为 docker 的桩）打出来，不用假读数：

* 「退 0 不等于可用」那条是本批的正身——只看退出码的实现会在这一条当场红；
* 「对照失败只能报 probe_failed」钉住铁律 1：探针自己不可信时，"这台机器没装 docker"是假事实；
* 「超时要带走进程树」钉住实测那一档：4 秒的桩 + 0.4 秒闸门，只杀直接子进程要白等 4.06s，
  杀整棵树 0.63s 返回——不杀孙进程的话探测会占着线程不放。
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from blank_api.services.sandbox import docker_probe, runner
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.safety import backend_info
from tests.test_agent_v04 import create_project, create_provider, create_task, env  # noqa: F401

API = "/api/v1"
REFUSED_CODE = "sandbox_tier_unavailable"
REASON = "sandbox_backend_unavailable"
DAEMON_PHRASE = "守护进程"


@pytest.fixture(autouse=True)
def _fresh_cache_each_test():
    """进程级缓存是这批所有判据的地雷：每条用例前后都清空，免得上一条的读数冒充这一条。"""
    docker_probe.invalidate()
    yield
    docker_probe.invalidate()


def _no_stub(monkeypatch, tmp_path) -> None:
    """PATH 只剩这个空目录 ⇒ 现场事实变成"这台机器没装 docker"。"""
    monkeypatch.setenv("PATH", str(tmp_path))


def _stub(tmp_path, monkeypatch, *, out=(), err="", rc=0, slow=False) -> None:
    """在 PATH 最前面放一个**真能跑**的 docker 桩。

    Windows 写 `docker.bat`（cmd 再起 echo/ping，正好是"孙子进程"那种载体），
    POSIX 写带 shebang 的 `docker`。两条路都是真 subprocess，不是打补丁出来的假读数。
    """
    if os.name == "nt":
        path = tmp_path / "docker.bat"
        lines = ["@echo off"]
        if slow:
            lines.append("ping -n 5 127.0.0.1 >nul")  # ≈4 秒，用来把探测的超时逼出来
        for line in out:
            lines.append(f"echo {line}")
        if err:
            lines.append(f"echo {err} 1>&2")
        lines.append(f"exit /b {rc}")
        path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii", newline="")
    else:
        path = tmp_path / "docker"
        lines = ["#!/bin/sh"]
        if slow:
            lines.append("sleep 4")
        for line in out:
            lines.append(f"echo {line}")
        if err:
            lines.append(f"echo {err} 1>&2")
        lines.append(f"exit {rc}")
        path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


# --------------------------------------------------------------------------- #
# 读数本身：四种状态 + 一条铁律
# --------------------------------------------------------------------------- #
def test_没装docker的机器读出不安装(monkeypatch, tmp_path) -> None:
    _no_stub(monkeypatch, tmp_path)
    info = docker_probe.probe(force=True)
    assert info.state == docker_probe.STATE_NOT_INSTALLED, info
    assert info.usable is False and info.known_unavailable is True, info
    # 对照成立才敢说"没装"：这一枚是装置自检，不是装饰
    assert info.control_ok is True, info


def test_装了且守护应答才算可用(monkeypatch, tmp_path) -> None:
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0", "Server Version: 99.9.9"])
    info = docker_probe.probe(force=True)
    assert info.state == docker_probe.STATE_READY, info
    assert info.usable is True, info
    assert "99.9.9" in info.detail, info


def test_退零但输出里没有守护进程那一行不许算可用(monkeypatch, tmp_path) -> None:
    """本批的正身：只看 `rc == 0` 的实现在这里当场红。

    一个立刻退 0 的空壳（或只打印客户端段的 docker）就能把系统说成"有容器"，
    于是每条命令照样撞墙——那正是这批要消灭的形状。
    """
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    info = docker_probe.probe(force=True)
    assert info.state == docker_probe.STATE_DAEMON_UNREACHABLE, info
    assert info.exit_code == 0, info  # 退出码确实是 0，读数仍然不许是 ready
    assert "Server Version" in info.detail or "守护进程" in info.detail, info


def test_非零退出要把成因原样念出来(monkeypatch, tmp_path) -> None:
    _stub(tmp_path, monkeypatch, err="Cannot connect to the Docker daemon at npipe", rc=1)
    info = docker_probe.probe(force=True)
    assert info.state == docker_probe.STATE_DAEMON_UNREACHABLE, info
    assert "Docker daemon" in info.detail, info


def test_探测超时既不算可用也要自己把进程带走(monkeypatch, tmp_path) -> None:
    """铁律 2（测不到就降级）+ 一条实测缺陷。

    实测（同一份 `docker.bat` 桩，里面 `ping -n 5` ≈4 秒，超时闸门给 0.4 秒）：
    `subprocess.run(timeout=…)` 那种写法只杀直接子进程，然后继续等管道里孙进程留下的字节，
    3 次都是 4.06–4.08s；换成杀整棵进程树（`taskkill /PID /T /F`）3 次都是 0.63–0.65s。
    这条判据钉的就是那个上界：超时以后必须贴着闸门返回，不许白等孙进程跑完。
    """
    _stub(tmp_path, monkeypatch, out=["Server Version: 1.0"], slow=True)
    started = time.perf_counter()
    info = docker_probe.probe(force=True, timeout_seconds=0.4)
    spent = time.perf_counter() - started
    assert info.state == docker_probe.STATE_TIMEOUT, info
    assert 0.3 <= spent < 2.0, f"超时以后没把进程带走，探测白等了 {spent:.2f}s"


def test_对照进程起不来时读数只能是探测装置不可信(monkeypatch) -> None:
    """铁律 1：对照失败 ⇒ 结论不许写成「没装」或「守护没应答」，那都是假事实。"""
    real_spawn = docker_probe._spawn

    def _broken_control(argv, timeout_seconds):
        if argv and argv[0] == sys.executable:
            return None, "", "", "OSError: 模拟对照进程起不来"
        return real_spawn(argv, timeout_seconds)

    monkeypatch.setattr(docker_probe, "_spawn", _broken_control)
    info = docker_probe.probe_once()
    assert info.state == docker_probe.STATE_PROBE_FAILED, info
    assert info.state not in (docker_probe.STATE_NOT_INSTALLED, docker_probe.STATE_DAEMON_UNREACHABLE)
    assert "对照" in info.detail, info
    assert info.control_ok is False, info


# --------------------------------------------------------------------------- #
# 缓存：一台机器问一次就够，但读数要报得出年龄
# --------------------------------------------------------------------------- #
def test_一次探测被后续读取复用而不是每次都起进程(monkeypatch) -> None:
    calls: list[float] = []
    real = docker_probe.probe_once

    def _counted(*, timeout_seconds: float = docker_probe.PROBE_TIMEOUT_SECONDS):
        calls.append(timeout_seconds)
        return real(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(docker_probe, "probe_once", _counted)
    for _ in range(5):
        docker_probe.probe()
    assert len(calls) == 1, f"TTL 内重复起了 {len(calls)} 次探测进程"
    docker_probe.probe(force=True)
    assert len(calls) == 2, "force 必须跳过缓存"
    docker_probe.probe(ttl=0.0)
    assert len(calls) == 3, "TTL 到点必须补探，不许把旧值当现值"
    docker_probe.peek()
    assert len(calls) == 3, "peek 只读缓存，绝不许起进程"


def test_缓存读数报得出年龄没探过的就不编一个时间(monkeypatch, tmp_path) -> None:
    _no_stub(monkeypatch, tmp_path)
    info = docker_probe.probe(force=True)
    payload = info.as_dict()
    assert payload["measured_at"] > 0
    assert isinstance(payload["age_seconds"], float | int), payload
    never = docker_probe.DockerProbe()
    assert never.as_dict()["age_seconds"] is None, "没探过的读数不许编造年龄"


async def test_在事件循环里绝不同步起探测进程(monkeypatch) -> None:
    """`search_code` 那次把整个后端占死的教训：同步子进程会独占事件循环。

    循环里的决策只能读缓存；没缓存就返 pending（并因此不做拒绝/放行的决定），
    真起进程那一下交给 `ensure_fresh` 放线程里。
    """
    calls: list[int] = []
    real = docker_probe.probe_once

    def _counted(*, timeout_seconds: float = docker_probe.PROBE_TIMEOUT_SECONDS):
        calls.append(1)
        return real(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(docker_probe, "probe_once", _counted)
    docker_probe.invalidate()
    info = docker_probe.probe_for_decision()
    assert info.state == docker_probe.STATE_PENDING, info
    assert calls == [], f"事件循环里同步起了 {len(calls)} 次探测进程"
    await docker_probe.ensure_fresh()
    assert len(calls) == 1, "补探应该由 ensure_fresh 在线程里做恰好一次"
    docker_probe.probe_for_decision()
    assert len(calls) == 1, "缓存新鲜时决策点不许再起进程"


async def test_并发读取点也只起一条进程(monkeypatch) -> None:
    """「一台机器问一次就够」要连**同时到达**的读取点也算数。

    `probe()` 里那把锁把并发读取点串行化：第一个真探，后面九个读到刚填上的缓存。
    实测 10 个并发 `ensure_fresh` 起 1 条子进程、总耗时 0.098s（单条探测本身 0.079–0.097s）。
    把缓存检查挪到锁外面（读取点各探各的）这条会红——变异电池里那条形状实测红。
    """
    import asyncio

    calls: list[int] = []
    real = docker_probe.probe_once

    def _counted(*, timeout_seconds: float = docker_probe.PROBE_TIMEOUT_SECONDS):
        calls.append(1)
        time.sleep(0.2)  # 比实测单条探测子进程的往返（0.079–0.097s）更宽，把并发窗口拉开
        return real(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(docker_probe, "probe_once", _counted)
    docker_probe.invalidate()
    results = await asyncio.gather(*(docker_probe.ensure_fresh() for _ in range(10)))
    assert len(calls) == 1, f"10 个并发读取点起了 {len(calls)} 条探测子进程"
    assert all(row is results[0] for row in results), "合用的必须是同一份读数"


# --------------------------------------------------------------------------- #
# 决策：读数怎么变成"给不给容器后端"
# --------------------------------------------------------------------------- #
def test_装了但守护没应答时不许返回容器后端(monkeypatch, tmp_path) -> None:
    """这条就是老缺陷的正面：PATH 里有那个文件 ≠ 可以往它上面派命令。"""
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    docker_probe.probe(force=True)
    with pytest.raises(runner.SandboxUnavailable) as caught:
        runner.get_sandbox(SandboxPolicy(enabled=True, allow_local=False))
    message = str(caught.value)
    assert DAEMON_PHRASE in message, f"拒绝要念得出成因：{message}"


def test_允许直连宿主的开发机仍然走本机执行(monkeypatch, tmp_path) -> None:
    """对照格：上一条的红必须能归因到"后端不可用"，而不是"这个开关把路全堵了"。"""
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    docker_probe.probe(force=True)
    assert runner.get_sandbox(SandboxPolicy(enabled=True, allow_local=True)).name == "local"


def test_真能用才给容器后端(monkeypatch, tmp_path) -> None:
    _stub(tmp_path, monkeypatch, out=["Server Version: 99.9.9"])
    docker_probe.probe(force=True)
    assert runner.get_sandbox(SandboxPolicy(enabled=True, allow_local=False)).name == "docker"


async def test_还没探过的时候不假装不可用(monkeypatch, tmp_path) -> None:
    """`pending` 的取舍钉在这里：没测过就不许宣称"不可用"而拦住正常机器。

    哪天有人把它改成 fail-closed（pending 也拒），正常装了 docker 的机器在探针还没跑完的
    那一瞬间会全线被拒——这条会先红，逼他写明这是新的产品决定。
    """
    _stub(tmp_path, monkeypatch, out=["Server Version: 99.9.9"])
    docker_probe.invalidate()
    assert runner.get_sandbox(SandboxPolicy(enabled=True, allow_local=False)).name == "docker"


# --------------------------------------------------------------------------- #
# 端点与装配层：读数要能被查到，拒绝要念得出成因
# --------------------------------------------------------------------------- #
def test_docker_available不再等于PATH里有没有那个文件(monkeypatch, tmp_path) -> None:
    import shutil

    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    assert shutil.which("docker") is not None, "桩没进 PATH：这条测的就不是"
    info = backend_info()
    assert info.docker_available is False, 'PATH 里有文件就说「可用」，正是这批要修的谎'
    assert info.docker_state == docker_probe.STATE_DAEMON_UNREACHABLE, info
    assert info.docker_age_seconds is not None, info


def test_装了但没起的人被告知去启动守护进程(monkeypatch, tmp_path) -> None:
    """指路必须跟着读数走：这机器上装是装好了的，你让他「装 Docker」是耍人。"""
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    info = backend_info()
    assert info.docker_state == docker_probe.STATE_DAEMON_UNREACHABLE, info
    assert "启动 Docker 守护进程" in info.detail, info.detail


def test_真没装的人只被告知要装什么(monkeypatch, tmp_path) -> None:
    """对照格：上一条的红句在这条里必须不出现，否则两条测的是同一句话。"""
    _no_stub(monkeypatch, tmp_path)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    info = backend_info()
    assert info.docker_state == docker_probe.STATE_NOT_INSTALLED, info
    assert "装 Docker" in info.detail, info.detail
    assert "启动 Docker 守护进程" not in info.detail, "这台机器上没有守护进程可启动"


async def test_运行时安全面把状态成因与年龄一起端出去(env, monkeypatch, tmp_path) -> None:  # noqa: F811
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    resp = await env.client.get(f"{API}/settings/safety/runtime")
    assert resp.status_code == 200, resp.text
    backend = resp.json().get("backend") or {}
    # 这一页不许回答"尚未探测"：它存在的意义就是回答"现在到底怎么样"
    assert backend.get("docker_state") != docker_probe.STATE_PENDING, backend
    assert backend.get("docker_state") == docker_probe.STATE_DAEMON_UNREACHABLE, backend
    assert backend.get("docker_available") is False, backend
    assert DAEMON_PHRASE in backend.get("notice", ""), backend
    assert "BLANK_SANDBOX_ALLOW_LOCAL" in backend.get("detail", ""), "装了的人要被指引去启动守护进程"


async def test_没开沙箱时这一页不为用不上的读数起进程(env, monkeypatch) -> None:  # noqa: F811
    """`off` 形态下 Docker 读数只是资料：为它补探等于把一次子进程卡在客户点开设置的那一下。

    实测（本机，PATH 前置一个退 1 的 `docker` 桩模拟「装了但守护没应答」，每轮全新 app）：
    开着沙箱第一次读这一页 0.112s（等了一条探测子进程），关着沙箱 0.034s（不起进程，
    读数留给启动探测去填）；TTL 内的第二次读都是 0.001s。
    """
    calls: list[int] = []

    def _counted(*, timeout_seconds: float = docker_probe.PROBE_TIMEOUT_SECONDS):
        calls.append(1)
        return docker_probe.DockerProbe(state=docker_probe.STATE_READY, detail="这条读数不该被用到")

    monkeypatch.setattr(docker_probe, "probe_once", _counted)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "0")
    resp = await env.client.get(f"{API}/settings/safety/runtime")
    assert resp.status_code == 200, resp.text
    assert calls == [], f"沙箱关着仍为读数起了 {len(calls)} 次探测进程"
    # 关掉沙箱这一句本身必须照说——不补探不等于这页没话可说
    assert "隔离" in ((resp.json().get("backend") or {}).get("label") or ""), resp.text


async def test_重新探测让装好之后的机器换到新读数(env, monkeypatch, tmp_path) -> None:  # noqa: F811
    """客户装完 Docker 不该被要求重启服务：这条把 TTL 跳过、重新起一次进程。"""
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    first = await env.client.post(f"{API}/settings/safety/runtime/reprobe")
    assert first.status_code == 200, first.text
    assert first.json()["probe"]["state"] == docker_probe.STATE_DAEMON_UNREACHABLE

    _stub(tmp_path, monkeypatch, out=["Server Version: 99.9.9"])
    second = await env.client.post(f"{API}/settings/safety/runtime/reprobe")
    assert second.json()["probe"]["state"] == docker_probe.STATE_READY, second.text
    assert second.json()["backend"]["docker_available"] is True, second.text


async def test_守护没应答时任务在装配层就被拒且一步没走(env, workspace, monkeypatch, tmp_path) -> None:
    """把 #99 那条产品事实推到新的成因上：拒绝发生在装配层，且成因念得出守护进程。"""
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    project = await create_project(env, workspace)
    provider = await create_provider(env)
    task = await create_task(env, project, ["列出文件即可"], title="探测取证", goal="只列文件不要改")

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    error = body.get("error") or {}
    assert error.get("code") == REFUSED_CODE, body
    # 拒绝必须说清"没退回宿主"：只念"后端不可用"会被读成"那大概会退回报宿主跑"
    assert "不会退回宿主执行" in error.get("message", ""), body
    assert DAEMON_PHRASE in error.get("message", ""), body

    events = (await env.client.get(f"{API}/tasks/{task}/events")).json()
    types = {row.get("type") for row in events}
    assert types <= {"task.created"}, f"整任务被拒应当一步不走，事件里却有：{types}"


async def test_关掉沙箱后同一条只读任务仍能跑到终态(env, workspace, monkeypatch, tmp_path) -> None:
    """对照格：上一条若红，分不清是"后端不可用被拒"还是"只读任务本来就跑不动"。"""
    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "0")
    project = await create_project(env, workspace)
    provider = await create_provider(env)
    task = await create_task(env, project, ["列出文件即可"], title="对照取证", goal="只列文件不要改")

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 200, resp.text
    status = (resp.json().get("task") or {}).get("status")
    assert status == "succeeded", f"关掉沙箱后仍没到终态：{status}"


async def test_建群那一刻也先探一次(env, workspace, monkeypatch, tmp_path) -> None:
    """群档位在建群时解析一次——拿着 pending 做这个决定就是赌博：整群成员任务逐字继承这一档。

    这条钉 `team.py` 建群前那次补探。撤掉它，读数停在 pending，而 pending 在决策点按 PATH 兜底
    给容器后端：桩只答 `Client Version`（守护没应答），群照样建成功，要到成员任务跑起来才撞墙。
    对照格写在同一条里——换成真能用的桩必须回 201，红点才只可能落在"建群前没探"那一格。
    """
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    project = await create_project(env, workspace)
    body = {"project_id": project, "sandbox_tier": "workspace"}

    _stub(tmp_path, monkeypatch, out=["Client Version: 24.0.0"], rc=0)
    docker_probe.invalidate()
    refused = await env.client.post(
        f"{API}/team/groups", json={**body, "name": "探测取证群"}
    )
    assert refused.status_code == 409, refused.text
    error = refused.json().get("error") or {}
    assert error.get("code") == REFUSED_CODE, refused.text
    assert DAEMON_PHRASE in error.get("message", ""), refused.text

    _stub(tmp_path, monkeypatch, out=["Server Version: 99.9.9"])
    docker_probe.invalidate()
    created = await env.client.post(f"{API}/team/groups", json={**body, "name": "对照取证群"})
    assert created.status_code == 201, created.text


async def test_启动时真的探了一次并把读数念出来(monkeypatch, tmp_path, capsys) -> None:
    from blank_api.app import probe_execution_backend

    _stub(tmp_path, monkeypatch, out=["Server Version: 99.9.9"])
    await probe_execution_backend()
    out = capsys.readouterr().out
    assert "[sandbox]" in out, out
    assert docker_probe.STATE_READY in out, out
    cached = docker_probe.peek()
    assert cached is not None and cached.usable, "启动探测的读数必须留在缓存里，别让装配层重探一次"


async def test_应用启动装配里真的挂着那一次探测(env, monkeypatch, tmp_path) -> None:  # noqa: F811
    """上面那条直接调函数，这条调的是**装配**：`create_app()` 的 lifespan 里没挂任务就红。

    `env` 用的是 `httpx.ASGITransport`，它不跑 lifespan，所以这里自己把 lifespan 开一遍。
    开机那一次是「决策点只读缓存」这个取舍的前提——把它摘掉，别的用例照样全绿，
    只有这条会一直停在 pending。
    """
    import asyncio

    from blank_api.app import create_app

    _stub(tmp_path, monkeypatch, out=["Server Version: 99.9.9"])
    docker_probe.invalidate()
    app = create_app()
    async with app.router.lifespan_context(app):
        info = None
        for _ in range(100):
            info = docker_probe.peek()
            if info is not None:
                break
            await asyncio.sleep(0.05)
    assert info is not None, "启动装配没挂探测任务：开机到现在一次都没探"
    assert info.state == docker_probe.STATE_READY, info


async def test_探针自己坏掉也不挡API启动(monkeypatch) -> None:
    from blank_api import app as app_module

    async def _boom(**_kwargs):
        raise RuntimeError("模拟探测装置炸了")

    monkeypatch.setattr(docker_probe, "ensure_fresh", _boom)
    await app_module.probe_execution_backend()  # 不许抛：起不来服务比少一行读数严重得多
