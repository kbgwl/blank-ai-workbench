"""#99：无 docker 的裸 API 默认形态会拒绝**每一个**任务——这条产品事实钉进仓库。

起因：2026-10-26 那批方言端到端 85 步红了 26 步，症状写的是「status=draft」。真因不在产品，
在装置没钉自己的沙箱档位；但顺着它量出来一条**产品事实**，一直没有判据守着：

* `SandboxPolicy` 的产品默认是「沙箱开着」，设置页默认档 `workspace`；
* 没有可用后端（本机没装 docker）时 `resolve_tier(strict=True)` 得到 `backend="none"`、
  `available=False`；
* 装配层（`runtime._prepare` 与 `team.py` 同一处）据此**整任务 409**——连一条不会执行
  任何命令的只读任务也不例外，界面上就是"任务起了但一步没走"。

这条收紧是设计文档认可的（SANDBOX-WINDOWS-DESIGN.md：不给"先跑起来再说"的机会），
所以这里守的是**它必须继续成立、且拒绝必须念得出成因**；哪天有人把它放宽成"退回报宿主"，
第 2、3 条会当场红。第 4 条是对照格：把沙箱关掉后同一条只读任务必须能跑到终态——
否则第 2 条的红分不清是"档位拒绝"还是"只读任务压根跑不动"。
"""

from __future__ import annotations

import pytest

from tests.test_agent_v04 import create_project, create_provider, create_task, env  # noqa: F401

API = "/api/v1"
READ_ONLY_GOAL = "只列一下工作区里有哪些文件，不要修改任何内容"
NOTICE = "沙箱后端不可用"
REFUSED_CODE = "sandbox_tier_unavailable"
REASON = "sandbox_backend_unavailable"


def _no_backend(monkeypatch) -> None:
    """把"这台机器没有可用沙箱后端"变成一个现场事实，而不是依赖本机装没装 docker。

    `_backend()` 用函数内 import 取 `get_sandbox`，所以补丁打在 runner 的模块属性上就是
    装配层真正走的那一条缝；换成 docker 真在的机器上跑，这条判据仍然成立。
    """
    from blank_api.services.sandbox import runner

    def _raise(policy):
        raise runner.SandboxUnavailable("模拟：本机探测不到 docker")

    monkeypatch.setattr(runner, "get_sandbox", _raise)


async def _readonly_task(env, workspace, monkeypatch, *, enabled: str):
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", enabled)
    project = await create_project(env, workspace)
    provider = await create_provider(env)
    task = await create_task(env, project, ["列出文件即可"], title="只读取证", goal=READ_ONLY_GOAL)
    return project, provider, task


async def test_the_default_posture_refuses_even_a_readonly_task(env, workspace, monkeypatch) -> None:
    """沙箱开着 + 没有后端 ⇒ 只读任务也在装配层 409，且念得出成因、一步没走。"""
    _no_backend(monkeypatch)
    _project, provider, task = await _readonly_task(env, workspace, monkeypatch, enabled="1")

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert (body.get("error") or {}).get("code") == REFUSED_CODE, body
    # 只有码没有话不行：操作者在界面上读的是 message，它必须说明"被拒了、没退回宿主"
    assert NOTICE in (body.get("error") or {}).get("message", ""), body

    events = (await env.client.get(f"{API}/tasks/{task}/events")).json()
    assert isinstance(events, list), events
    types = {row.get("type") for row in events}
    assert "task.started" not in types, f"409 之前就已经开跑了：{types}"
    # `task.created` 是建任务时就写的，与本轮无关；除此之外一条都不许有——
    # 装配层拒绝的意义就是"这一步都没走"，多一条事件就说明有人放宽成了"先跑起来再说"。
    assert types <= {"task.created"}, f"整任务被拒应当一步不走，事件里却有：{types}"


async def test_the_refusal_reason_is_queryable_without_running_a_task(env, monkeypatch) -> None:
    """拒绝的成因必须能单独查到：409 只带文案，`reason` 住在运行时安全面。

    这一条守的是"装置/前端能不能不问就判"：#96 的开场姿态自检读的就是这里的
    `available` 与 `reason`，字段哪天改名或不再暴露，自检会瞎，这里先红。
    """
    _no_backend(monkeypatch)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    resp = await env.client.get(f"{API}/settings/safety/runtime")
    assert resp.status_code == 200, resp.text
    backend = resp.json().get("backend") or {}
    assert backend.get("available") is False, backend
    assert backend.get("reason") == REASON, backend
    assert backend.get("docker_available") is False, backend
    assert NOTICE in backend.get("notice", ""), backend


async def test_turning_the_sandbox_off_lets_the_same_readonly_task_finish(env, workspace, monkeypatch) -> None:
    """对照格：同一条只读任务，关掉沙箱就必须能跑到终态。

    没有这一格，上面那条 409 的红分不清是"档位拒绝"还是"只读任务本来就跑不动"。
    """
    _no_backend(monkeypatch)
    _project, provider, task = await _readonly_task(env, workspace, monkeypatch, enabled="0")

    resp = await env.client.post(f"{API}/tasks/{task}/run", json={"provider_id": provider})
    assert resp.status_code == 200, resp.text
    status = (resp.json().get("task") or {}).get("status")
    assert status == "succeeded", f"关掉沙箱后仍没跑到终态：{status}"


async def test_the_product_default_keeps_the_sandbox_on(monkeypatch) -> None:
    """默认值本身就是判据：谁把产品默认调成"沙箱关"，这条立刻红。

    裸 API 的用户面对的就是这个默认；它一旦变松，前面三条量的就不再是真实形态。
    """
    from blank_api.services.sandbox.policy import SandboxPolicy

    monkeypatch.delenv("BLANK_SANDBOX_ENABLED", raising=False)
    assert SandboxPolicy().enabled is True, "产品默认被改松了：裸 API 用户会以为命令在沙箱里跑"
