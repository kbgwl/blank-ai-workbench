"""#99 → #144：无 docker 的裸 API 默认形态**不再拒绝任务**，Docker 变成可选加固档。

#99 当时钉住的产品事实是「沙箱开着 + 没有后端 ⇒ 连只读任务也在装配层 409」，并写明那条
收紧是设计认可的。用户随后当面否掉了它的前提：哪家产品要求客户先装 Docker 才能用？
#144 把默认改成：探测不到可用容器后端时退回本机 Job 受限执行（内存/进程数/进程树由内核
强制，文件与网络边界如实标注未强制）；要求「只准容器」的部署显式设置
`BLANK_SANDBOX_ALLOW_LOCAL=0`，那时才保留原来的 fail-closed。

所以这里守四条判据：

1. 默认档（不设 ALLOW_LOCAL、没有 docker）必须把只读任务跑到终态，读数是 `local`；
   界面上的文案必须说清哪几条没被系统强制，并且不许再让用户去装 Docker。
2. 严格部署（ALLOW_LOCAL=0）仍整任务 409、一步没走——#99 那条判据原样保留，
   只是从"产品默认"降级成"部署选择"。
3. 拒绝成因能在运行时安全面单独查到（#122 的开场自检读的就是这里的字段）。
4. 对照格：把沙箱整个关掉，同一条只读任务也必须能跑到终态——否则第 1 条绿了也分不清
   是"退回本机成功了"还是"只读任务本来就跑不动"。
"""

from __future__ import annotations

import time

import pytest

from blank_api.services.sandbox import docker_probe
from blank_api.services.sandbox.policy import SandboxPolicy
from tests.test_agent_v04 import create_project, create_provider, create_task, env  # noqa: F401

API = "/api/v1"
READ_ONLY_GOAL = "只列一下工作区里有哪些文件，不要修改任何内容"
NOTICE = "沙箱后端不可用"
REFUSED_CODE = "sandbox_tier_unavailable"
REASON = "sandbox_backend_unavailable"


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    """探针缓存是进程级的：不清空就会拿上一条用例的读数冒充这一条。"""
    docker_probe.invalidate()
    yield
    docker_probe.invalidate()


def _no_docker(monkeypatch) -> None:
    """把「这台机器没有可用容器后端」变成一个现场事实，而不是依赖本机装没装 docker。

    读数打在 `probe_once` 上：`ensure_fresh`（装配层）与 `probe_for_decision`（决策点）
    都经过它，所以这正是产品真正走的那一条缝；`shutil.which` 一并置空，免得
    `pending` 分支的 PATH 兜底在别的机器上把这格变成另一种读数。
    """
    probe = docker_probe.DockerProbe(
        state=docker_probe.STATE_NOT_INSTALLED,
        detail="PATH 里找不到 docker 可执行文件（用例模拟）",
        control_ok=True,
        measured_at=time.time(),
    )
    monkeypatch.setattr(docker_probe, "probe_once", lambda **_: probe)
    monkeypatch.setattr("shutil.which", lambda *_: None)


async def _readonly_task(env, workspace, monkeypatch, *, enabled: str, allow_local: str | None):
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", enabled)
    if allow_local is None:
        monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    else:
        monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", allow_local)
    project = await create_project(env, workspace)
    provider = await create_provider(env)
    task = await create_task(env, project, ["列出文件即可"], title="只读取证", goal=READ_ONLY_GOAL)
    return project, provider, task


async def test_without_docker_the_default_posture_runs_the_readonly_task(env, workspace, monkeypatch) -> None:
    """默认档 + 没有 docker ⇒ 退回本机受限执行，只读任务必须跑到终态（不再是 409）。"""
    _no_docker(monkeypatch)
    _project, provider, task = await _readonly_task(env, workspace, monkeypatch, enabled="1", allow_local=None)

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 200, resp.text
    status = (resp.json().get("task") or {}).get("status")
    assert status == "succeeded", f"没有 Docker 时默认档仍没跑到终态：{status}"

    events = (await env.client.get(f"{API}/tasks/{task}/events")).json()
    assert "task.started" in {row.get("type") for row in events}, f"任务根本没开跑：{events}"


async def test_the_default_readout_is_local_and_says_what_is_not_enforced(env, monkeypatch) -> None:
    """界面读数：后端是 local、可用，且必须说清文件和网络没被系统强制，不许让人去装 Docker。"""
    _no_docker(monkeypatch)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)

    resp = await env.client.get(f"{API}/settings/safety/runtime")
    assert resp.status_code == 200, resp.text
    backend = resp.json().get("backend") or {}
    assert backend.get("available") is True, backend
    assert backend.get("backend") == "local", backend
    assert backend.get("tier") == "workspace", backend
    assert backend.get("docker_available") is False, backend
    # reason 是稳定判据（界面/自检读它，不读文案）：本机能力测出来是什么就报什么。
    assert str(backend.get("reason", "")).startswith("measured_"), backend
    # 诚实文案：文件和网络没被系统强制时，notice 必须这么说，不许写成"已隔离"
    if not backend.get("filesystem_enforced"):
        assert "未由系统强制" in backend.get("notice", ""), backend
    # Docker 是可选升级项，不是安装前提：这页不许再出现"去装 Docker"这种补救指引
    assert "自动改用容器隔离" in backend.get("detail", ""), backend
    assert "或装 Docker" not in backend.get("detail", ""), backend


async def test_strict_container_only_deployments_still_refuse_every_task(env, workspace, monkeypatch) -> None:
    """`BLANK_SANDBOX_ALLOW_LOCAL=0` = 只准容器：没有后端时仍整任务 409、一步没走。

    这是 #99 那条判据的去处：它没有消失，只是从产品默认降级成部署选择。
    """
    _no_docker(monkeypatch)
    _project, provider, task = await _readonly_task(env, workspace, monkeypatch, enabled="1", allow_local="0")

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert (body.get("error") or {}).get("code") == REFUSED_CODE, body
    message = (body.get("error") or {}).get("message", "")
    # 只有码没有话不行：操作者在界面上读的是 message，它必须说明"被拒了、没退回宿主"
    assert NOTICE in message, body
    assert "不会退回宿主执行" in message, body
    assert "BLANK_SANDBOX_ALLOW_LOCAL=0" in message, body

    events = (await env.client.get(f"{API}/tasks/{task}/events")).json()
    types = {row.get("type") for row in events}
    assert "task.started" not in types, f"409 之前就已经开跑了：{types}"
    # `task.created` 是建任务时就写的，与本轮无关；除此之外一条都不许有——
    # 装配层拒绝的意义就是"这一步都没走"，多一条事件就说明有人放宽成了"先跑起来再说"。
    assert types <= {"task.created"}, f"整任务被拒应当一步不走，事件里却有：{types}"


async def test_the_refusal_reason_is_queryable_without_running_a_task(env, monkeypatch) -> None:
    """严格部署下拒绝的成因必须能单独查到：409 只带文案，`reason` 住在运行时安全面。

    这一条守的是"装置/前端能不能不问就判"：#96 的开场姿态自检读的就是这里的
    `available` 与 `reason`，字段哪天改名或不再暴露，自检会瞎，这里先红。
    """
    _no_docker(monkeypatch)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "0")
    resp = await env.client.get(f"{API}/settings/safety/runtime")
    assert resp.status_code == 200, resp.text
    backend = resp.json().get("backend") or {}
    assert backend.get("available") is False, backend
    assert backend.get("reason") == REASON, backend
    assert backend.get("docker_available") is False, backend
    assert NOTICE in backend.get("notice", ""), backend
    # 补救路径必须在页面上念得出来：否则操作者只知道"被拒"，不知道是自己的选择造成的
    assert "BLANK_SANDBOX_ALLOW_LOCAL" in backend.get("detail", ""), backend


async def test_turning_the_sandbox_off_lets_the_same_readonly_task_finish(env, workspace, monkeypatch) -> None:
    """对照格：同一条只读任务，关掉沙箱就必须能跑到终态。

    没有这一格，第 1 条的红分不清是"退回本机失败"还是"只读任务本来就跑不动"。
    """
    _no_docker(monkeypatch)
    _project, provider, task = await _readonly_task(env, workspace, monkeypatch, enabled="0", allow_local=None)

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 200, resp.text
    status = (resp.json().get("task") or {}).get("status")
    assert status == "succeeded", f"关掉沙箱后仍没跑到终态：{status}"


def test_the_product_default_is_local_fallback_not_docker_only(monkeypatch) -> None:
    """默认值本身就是判据：谁把产品默认调回"只准容器"，这条立刻红。

    裸 API 的用户面对的就是这个默认；它一旦变回 fail-closed，没装 Docker 的机器上
    每一个任务都会在装配层被拒——那正是用户否掉的产品形态。
    """
    monkeypatch.delenv("BLANK_SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_LOCAL", raising=False)
    policy = SandboxPolicy()
    assert policy.enabled is True, "沙箱默认被关掉了：内核限额白做"
    assert policy.allow_local is True, "默认被改成「只准容器」：没装 Docker 的用户又用不了"
