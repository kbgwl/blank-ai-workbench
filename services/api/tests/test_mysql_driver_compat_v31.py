"""MySQL 运行时方言分歧之二：bytes 参数在 aiomysql × PyMySQL ≥1.2 上没人转义。

真红是端到端量出来的（任务 #50，真 MySQL 8.4 + 本机 OpenAI 兼容桩）：
`[FAIL] 19 任务运行到终态 AssertionError: HTTP 500`，api.log 尾部——

    File ".../aiomysql/cursors.py", line 197, in _escape_args
        return tuple(conn.escape(arg) for arg in args)
    File ".../aiomysql/connection.py", line 418, in escape
        return escape_bytes_prefixed(obj)
    TypeError: 'str' object is not callable

调用点是 `services/agent/checkpoints.py` 的 `aput`：LangGraph 检查点把 msgpack
字节写进 LONGBLOB，**任何一次真跑都要写它**，所以在 MySQL 上整条运行链路是死的。
根因不在我们代码里：aiomysql 0.3.2（PyPI 上已发布的最新）转义 bytes 时调的是
`pymysql.converters.escape_bytes_prefixed`，而 PyMySQL 从 1.2 起把这个名字换成了
哨兵字符串 "DO NOT IMPORT THIS!!!"（上游 PyMySQL/PyMySQL#1274，1.3.0 计划直接删）。

`blank_api.db.ensure_mysql_driver_compat()` 把那个引用指回 pymysql 自己给 bytes
用的编码器 `escape_bytes`。补完是否真的对，CI 里量不到（本机才有 MySQL），
所以红/绿与"逐字节回读相等"是在 3307 上用 `E:/tmp/m50/f_bytes.py` 实测的：
补之前 TypeError，补之后 `_binary X'…'` 落库、回读 `b'\\x89PNG…'` 逐字节一致。
这个文件守的是能在 CI 里成立的那三件事：补的是不是同一个编码器、
SQLite 那条路有没有被碰、以及创建引擎时到底调没调它。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blank_api import db
from blank_api.db import ensure_mysql_driver_compat

SENTINEL = "DO NOT IMPORT THIS!!!"

# 两个驱动都在 `mysql` extra 里，默认 `uv sync` 不装（CI 的 api job 就是这种环境）。
# 所以 import 本身要能失败——否则 skipif 还没来得及生效，收集阶段就因
# ModuleNotFoundError 把整个 pytest 打断（2026-10-29 CI 首次真跑就这么红的）。
try:
    from pymysql import converters

    HAS_PYMYSQL = True
except ImportError:
    converters = None
    HAS_PYMYSQL = False

try:
    import aiomysql.connection

    HAS_AIOMYSQL = True
except ImportError:  # 没装 MySQL 驱动的环境（CI 只跑 SQLite）没什么可验
    HAS_AIOMYSQL = False

pytestmark = pytest.mark.skipif(
    not (HAS_AIOMYSQL and HAS_PYMYSQL), reason="本机没装 pymysql/aiomysql（mysql extra 未装），MySQL 驱动兼容不适用"
)


@pytest.fixture
def sentinel_reference():
    """把 aiomysql 里那个名字摆成 PyMySQL ≥1.2 的哨兵，用例结束原样还原。"""
    saved = aiomysql.connection.escape_bytes_prefixed
    aiomysql.connection.escape_bytes_prefixed = SENTINEL
    try:
        yield
    finally:
        aiomysql.connection.escape_bytes_prefixed = saved


def test_the_repair_binds_pymy_sql_s_own_bytes_encoder(sentinel_reference) -> None:
    assert ensure_mysql_driver_compat("mysql+aiomysql://root@127.0.0.1:3307/x") is True
    repaired = aiomysql.connection.escape_bytes_prefixed
    # 不是随手找个能调用的东西顶上去：必须是 PyMySQL 自己给 bytes 用的那个编码器，
    # 同步 pymysql 驱动天天用它，服务端认不认这件事早就被千万次真实写入验过。
    assert repaired is converters.escape_bytes
    assert converters.encoders[bytes] is converters.escape_bytes
    assert repaired(b"ab\x00\xff") == "_binary X'616200ff'"
    # 幂等：第二次已经没有可修的东西
    assert ensure_mysql_driver_compat("mysql+aiomysql://root@127.0.0.1:3307/x") is False


def test_other_drivers_are_left_alone(sentinel_reference) -> None:
    for url in (
        "sqlite+aiosqlite:///./data/blank_dev.db",
        "mysql+pymysql://root@127.0.0.1:3307/x",
        "postgresql+asyncpg://u@h/x",
        "",
    ):
        assert ensure_mysql_driver_compat(url) is False, url
    # 没用到 aiomysql 就不该改别人的进程状态
    assert aiomysql.connection.escape_bytes_prefixed == SENTINEL


async def test_engine_creation_applies_the_compat(sentinel_reference, monkeypatch) -> None:
    """兼容必须发生在建引擎那一刻，不是等谁来手动调用。

    连不上服务端也无所谓：create_async_engine 是惰性的，这里只验"建引擎这条路
    把转义补上了"，真连库的字节往返在 E:/tmp/m50/f_bytes.py 里实测过。
    """
    saved_engine, saved_factory = db._engine, db._session_factory
    monkeypatch.setattr(
        db, "get_settings", lambda: SimpleNamespace(database_url="mysql+aiomysql://root@127.0.0.1:3307/x")
    )
    db._engine = db._session_factory = None
    engine = None
    try:
        engine = db.get_engine()
        assert callable(aiomysql.connection.escape_bytes_prefixed), "建引擎没有补上 bytes 转义"
        assert aiomysql.connection.escape_bytes_prefixed is converters.escape_bytes
    finally:
        if engine is not None:
            await engine.dispose()
        db._engine, db._session_factory = saved_engine, saved_factory
