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


# PyMySQL 的上界（与 pyproject 的 mysql extra 同源；tests/test_mysql_driver_expiry_v81.py 钉两者一致）。
# aiomysql 0.3.2 在模块顶层 import pymysql.converters.escape_bytes_prefixed，而 PyMySQL 1.3
# 计划删掉这个别名（1.2.x 已经把它换成哨兵字符串）。上界一旦放宽，坏的是 aiomysql 的 import 本身。
PYMYSQL_UPPER_BOUND = "1.3"


def pymysql_release_version() -> str | None:
    """PyMySQL 的**发行**版本号；拿不到就返回 None，绝不拿别处的号冒充。

    不能读 ``pymysql.__version__``：1.2.3 把它硬写成 ``"2.2.8"``（为了通过 Django 的
    mysqlclient 版本检查），真实发行号在发行元数据与 ``VERSION_STRING`` 里。
    按 ``__version__`` 判 `<1.3` 这条上界，会把没到期的环境读成已过期。
    """
    from importlib import metadata

    try:
        return metadata.version("PyMySQL")
    except metadata.PackageNotFoundError:
        pass
    try:
        import pymysql

        # 有模块没元数据（源码直装）：VERSION_STRING 才是发行号
        return str(getattr(pymysql, "VERSION_STRING", "")).strip() or None
    except ImportError:
        return None


def ensure_mysql_driver_compat(database_url: str) -> bool:
    """让 aiomysql 0.3.2 在 PyMySQL ≥1.2 上还能绑 bytes 参数；只有真用到该驱动时才动。

    aiomysql 转义二进制参数时调的是 ``pymysql.converters.escape_bytes_prefixed``，
    而 PyMySQL 从 1.2 起把这个名字换成了哨兵字符串「DO NOT IMPORT THIS!!!」
    （上游 PyMySQL/PyMySQL#1274，1.3.0 打算直接删）。于是任何一次 bytes 绑定
    ——LangGraph 检查点就是把 msgpack 字节写进 LONGBLOB——都会炸成
    ``TypeError: 'str' object is not callable``，接口上表现为 500。
    PyMySQL 里语义等价、且返回服务端可插进 SQL 的字符串（``_binary X'<hex>'``）的
    现函数是 ``escape_bytes``，所以按它把引用补回去。等 aiomysql 修好上游引用，
    这个函数会因为「已经是可调用」而自动不做任何事。
    """
    if not database_url.startswith("mysql+aiomysql"):
        return False
    try:
        import aiomysql.connection
    except ImportError as error:
        # 1.3.0 真删掉那个别名时坏在这里：aiomysql 的模块顶层 import 就过不去，
        # 下面那身猴子补丁根本来不及跑。所以这一步只能换驱动或等上游修引用，
        # 把异常原样抛出去只会留下一句 cannot import name，没人知道该动哪里。
        raise ImportError(
            "aiomysql 装不起来：它 import 的 pymysql.converters.escape_bytes_prefixed 已被上游删除"
            f"（当前 PyMySQL 发行版 {pymysql_release_version() or '未知'}，"
            f"本项目声明的上界是 <{PYMYSQL_UPPER_BOUND}）。"
            "这不是能靠放宽上界解决的事——换驱动（如 asyncmy）或等 aiomysql 修引用。"
        ) from error
    from pymysql import converters

    if callable(getattr(aiomysql.connection, "escape_bytes_prefixed", None)):
        return False
    aiomysql.connection.escape_bytes_prefixed = converters.escape_bytes
    return True


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
        ensure_mysql_driver_compat(settings.database_url)
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
