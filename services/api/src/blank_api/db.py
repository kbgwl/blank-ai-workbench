from collections.abc import AsyncIterator
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from blank_api.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None

T = TypeVar("T")

# 请求被取消（断流、切页）时，连接可能正处在 close() 里被中断，于是池子里留下一条
# “半关闭”的连接：下一次检出它的人会直接收到 sqlite3.OperationalError。
# 这类错误是瞬时的——换一条新连接就能成功，全部按可重试处理。
_STALE_CONNECTION_MARKERS = (
    "no active connection",
    "connection closed",
    "cannot operate on a closed database",
    "database is locked",
    "database table is locked",
)


def is_transient_db_error(error: BaseException) -> bool:
    if not isinstance(error, OperationalError):
        return False
    message = str(error).lower()
    return any(marker in message for marker in _STALE_CONNECTION_MARKERS)


async def db_retry(
    operation: Callable[[], Awaitable[T]], *, attempts: int = 3, delay: float = 0.15
) -> T:
    """跑一段写库操作，遇到半关闭连接就换新连接重试。

    收尾动作（写 task.failed、finish_run）一旦失败，任务会永远停在 queued，
    界面上就是“一直转圈”。这里宁可多试两次，也不让任务失去终态。
    """
    import asyncio

    last: BaseException | None = None
    for index in range(attempts):
        try:
            return await operation()
        except OperationalError as error:
            if not is_transient_db_error(error) or index == attempts - 1:
                raise
            last = error
            await asyncio.sleep(delay * (index + 1))
    assert last is not None
    raise last


def engine_options(database_url: str) -> dict:
    """SQLite 连池参数。单独拎出来，测试可以直接验证这套口径。"""
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        # timeout：并发写入时等锁，而不是立刻抛 database is locked。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        # pre_ping：检出前先验证连接。池子里那条被取消打断的半关闭连接会在这里被
        # 识别、丢弃并换新，不会再把 “no active connection” 甩给业务线程。
        kwargs["pool_pre_ping"] = True
        if ":memory:" in database_url:
            from sqlalchemy.pool import StaticPool

            # 内存库必须共用同一条连接，ping 与 StaticPool 互斥
            kwargs["pool_pre_ping"] = False
            kwargs["poolclass"] = StaticPool
    return kwargs


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, **engine_options(settings.database_url))
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def shielded_close(session: AsyncSession) -> None:
    """关闭会话时屏蔽取消。

    Starlette 在每个请求外面套了取消域：客户端一断流，请求任务就被取消，
    连接 teardown 正好落在取消点上就会留下半关闭连接。收尾阶段不该被取消打断。
    """
    import anyio

    with anyio.CancelScope(shield=True):
        await session.close()


async def session_scope() -> AsyncIterator:
    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
