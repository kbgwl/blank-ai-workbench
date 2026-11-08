import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

from blank_api.api.auth import require_token
from blank_api.api.exception_handlers import register_error_handlers
from blank_api.api.routes import api_router
from blank_api.config import get_settings
from blank_api.helpers.middleware import RateLimitMiddleware
from blank_api.services.agent.manager import run_manager
from blank_api.services.channels.stream import StreamSupervisor


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    from blank_api.api.routes.tasks import recover_pending_runs

    # 团队轮次的推进器活在进程内存里：上次进程没收尾的轮次不会有别人接着推。
    # 迁移没跑过的旧库会让这里报 missing table，不能因此挡住 API 启动。
    try:
        from blank_api.services.team import recover_interrupted_rounds

        await recover_interrupted_rounds()
    except Exception:
        pass

    await recover_pending_runs()
    # 存量供应商的输出上限兜底：<=2048 会静默截断输出（表现为"工具参数偶尔是空的"）。
    # 迁移 0027 已经改过一遍，这里防的是"有人把它改回去"和"建库走的是 create_all"。
    try:
        from blank_api.services.models.registry import upgrade_truncating_providers

        await upgrade_truncating_providers()
    except Exception:
        pass
    # 全新空库：自动写入国产默认平台（幂等：已有配置不覆盖）
    try:
        from blank_api.api.routes.providers import seed_empty_providers
        await seed_empty_providers()
    except Exception:
        pass
    # 0017 之前的群消息没有索引行：启动时补一次，让升级后的历史也能被搜到。
    # 表不存在（旧库还没迁移）或补索引失败都不能挡住 API 启动。
    try:
        from blank_api.services.chat_index import backfill

        await backfill()
    except Exception:
        pass

    async def recovery_loop() -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await recover_pending_runs()
            except Exception:
                # A transient database outage must not kill the API lifecycle;
                # the next tick will retry the durable queue scan.
                continue

    recovery_task = asyncio.create_task(recovery_loop(), name="blank-run-recovery")
    # v165 Phase 3：关窗后 / 独立 worker 不在时，触发器、Watchdog、过期审批
    # 仍要有人巡。桌面形态只有 API 这一个进程，这些动作以前只挂在独立
    # worker 的 run_once() 里，真机上等于从不执行。
    supervisor_task = asyncio.create_task(background_supervisor(), name="blank-background-supervisor")
    # 附件清理单独一个任务、而且不阻断启动：它要扫盘 + 查一遍引用，
    # 让 /health 等着它没有道理。
    sweep_task = asyncio.create_task(sweep_orphan_uploads(), name="blank-upload-sweep")
    # 这台机器到底能不能真的起容器，是开机就该说清的事实：不能等每个任务各自撞一次墙。
    # 单独一个任务，探测要起子进程（最坏等满超时），让 /health 陪它等没有道理。
    probe_task = asyncio.create_task(probe_execution_backend(), name="blank-backend-probe")
    # 消息通道的长连接（v0.10.3）。钉钉 Stream 是本机唯一"不折腾公网"的收消息方式：
    # 客户在设置页填完凭据，这里每 20 秒对一次账，起停连接。没有配置时不建任何连接。
    stream_supervisor = StreamSupervisor()

    async def stream_loop() -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await stream_supervisor.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 连不上不该带走 API 生命周期；下一拍重试。
                continue

    stream_task = asyncio.create_task(stream_loop(), name="blank-channel-stream")
    tasks = (recovery_task, supervisor_task, sweep_task, probe_task, stream_task)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream_supervisor.shutdown()
        orchestrator = getattr(app.state, "team_orchestrator", None)
        if orchestrator is not None:
            await orchestrator.shutdown()
        await run_manager.shutdown()
        # 外部工具服务端是我们起的子进程：不在这里收掉，退出后会留下孤儿进程
        from blank_api.services.mcp import manager as mcp_manager

        await mcp_manager().close()
        # REA 那条线用的是自己的连接表（不进 mcp.json 的账），所以也要单独收
        from blank_api.services.rea import adapter as rea_adapter

        await rea_adapter.close_all()
        # 模型调用的共享连接池：进程退出前关掉，别把半开连接留给下一次启动。
        from blank_api.services.models.base import aclose_pooled_clients

        await aclose_pooled_clients()


async def background_supervisor() -> None:
    """v165 §14/§15：让 ``settings.background`` 在桌面默认链路里真的被消费。

    三个在此之前的真空：

    * ``background.enabled`` 关掉时，触发器 / Watchdog / 过期审批照跑不误；
    * ``automation_mode = manual`` 时后台仍然自动推进；
    * 独立 worker 形态与 API 进程形态会同时巡同一批 run（``autonomous_enabled``
      按 ``inprocess`` 二选一，避免双执行者）。

    免打扰时段不在这里重复实现：``_record_notify_decision`` 已经把
    ``quiet_hours`` 折进 ``notify.decided``，这里做的是状态维护而不是发通知，
    把它也压掉会让「审批过期」在夜里无人收敛。
    """
    from blank_api.services.agent.automation import resolve_mode as resolve_automation_mode
    from blank_api.services.agent.background_policy import (
        AUTONOMOUS_TICK_SECONDS,
        autonomous_enabled,
        resolve_background,
    )
    from blank_api.services.settings import SettingsStore

    while True:
        await asyncio.sleep(AUTONOMOUS_TICK_SECONDS)
        try:
            settings = SettingsStore().read()
            policy = resolve_background(settings)
            from blank_api.api.routes.tasks import inprocess_runner_enabled

            if not autonomous_enabled(policy, inprocess=inprocess_runner_enabled()):
                continue
            # 手动模式的承诺是「我点执行才动」：后台也不许替他推进定时任务。
            if resolve_automation_mode(settings) == "manual":
                continue
            from blank_api.worker import supervise_once

            await supervise_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 后台巡检不能把 API 生命周期带走；下一拍重试。
            continue


async def sweep_orphan_uploads() -> None:
    """启动时清一次孤儿附件。

    三处会留下没人认领的文件：删掉的任务、选了图又没发出去的草稿、以及
    早期版本删项目时没清干净的残留。规则是"够老 + 没有任何任务引用"，
    这样刚选完还在写字的草稿不会被误删。
    """
    from sqlalchemy import select

    from blank_api.db import get_session_factory
    from blank_api.models.task import Task
    from blank_api.services.uploads import UploadStore

    async def collect() -> set[str]:
        async with get_session_factory()() as session:
            rows = (await session.execute(select(Task.attachments))).scalars().all()
        keep: set[str] = set()
        for row in rows:
            for item in row or []:
                keep.add(str(item.get("id") or ""))
        return keep

    try:
        keep = await collect()
        removed = await asyncio.to_thread(UploadStore().sweep, keep)
        if removed:
            print(f"[uploads] 清理孤儿附件 {removed} 个")
    except Exception:
        # 清理失败不该影响启动：磁盘上多几个文件，远好过服务起不来。
        return


async def probe_execution_backend() -> None:
    """开机探一次执行后端并把读数念出来（`docs/COST.md` 记的那条「还没做」）。

    只探一次不是"以后就不管了"：TTL 到点后任何一次装配或 `--reprobe` 都会补探。
    探测本身坏掉（连对照进程都起不来）也不能挡住 API 启动——那时界面上看到的是
    `probe_failed`，比服务起不来诚实得多。
    """
    from blank_api.services.sandbox import docker_probe

    try:
        info = await docker_probe.ensure_fresh()
        # flush=True：stdout 被重定向到文件时是块缓冲的，实测这行在探测跑完后约 30 秒才落进日志。
        # 开机这一行是给"服务起来了但沙箱用不上"的人看的，压着不放等于没有说。
        print(f"[sandbox] 执行后端探测：{info.state}｜{info.detail}", flush=True)
    except Exception:
        return


def create_app() -> FastAPI:
    settings = get_settings()
    # 配了令牌 = 这不是"本机单用户"形态，那么接口面本身也不该匿名可读。
    # FastAPI 默认把 /openapi.json 与两份交互文档挂在鉴权之外，而 include_router 上那条
    # `dependencies=[Depends(require_token)]` 管不到它们：匿名就能拿到整份路由表、
    # 参数形状与错误码，Swagger UI 更是一份照着点的清单（方言端到端 #53 那一格量到的）。
    guarded = bool(settings.api_token)
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=app_lifespan,
        docs_url=None if guarded else "/docs",
        redoc_url=None if guarded else "/redoc",
        openapi_url=None if guarded else "/openapi.json",
    )
    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 上限可配（`BLANK_RATE_LIMIT_RPM`，默认仍是 120）。批量客户端（评测装置、迁移脚本）
    # 会在几十秒内把窗口打满，然后看到一片 429——那不是"服务挂了"，是装置和限流在打架。
    # 0/负数 = 关掉，只该用在受控的批处理实例上；产品默认值一个字没动。
    if settings.rate_limit_rpm > 0:
        app.add_middleware(RateLimitMiddleware, rpm=settings.rate_limit_rpm)
    app.include_router(api_router, prefix="/api/v1")
    if guarded:
        _mount_guarded_docs(app)

    return app


def _mount_guarded_docs(app: FastAPI) -> None:
    """把上面关掉的三条挂回来，但走同一个 `require_token`。

    直接删掉是最省事的，可桌面壳与运维排障要看文档——要收的是"谁能看"，不是"有没有"。
    """
    auth = [Depends(require_token)]

    @app.get("/openapi.json", include_in_schema=False, dependencies=auth)
    async def guarded_openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=auth)
    async def guarded_swagger():
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")

    @app.get("/redoc", include_in_schema=False, dependencies=auth)
    async def guarded_redoc():
        return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


app = create_app()
