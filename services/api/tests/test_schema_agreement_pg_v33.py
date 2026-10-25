"""两条建库路径在 PostgreSQL 上必须给出同一个 schema（#61、#62）。

#49 的对账（`test_schema_agreement_v29.py`）只在 SQLite 上真跑两条路径，PG 只有
迁移文件里的 `else` 分支；#60 把整条历史和 83 步 HTTP 跑通了，但**逐列对账仍欠账**——
SQLite 对列宽、可空性、默认值照收不误，只有真 PG 会把它们当回事。

这里在真 PG 上开两个库：一个跑 `alembic upgrade head`，一个跑 `Base.metadata.create_all()`，
然后逐表 / 逐列 / 可空性 / 外键 / 服务端默认值（#61）以及**列类型与长度、索引、唯一约束**（#62）对账。
默认值只比**两个库反射出来的结果**，不比 ORM 里的 Python 字面量：PG 会把自己的写法规范化
（`'[]'` → `'[]'::json`），两条 DDL 写法只要等价，反射结果就该一字不差。

索引与唯一约束连**名字**一起比：迁移和运维脚本会按名字引用它们。`#62` 实测到第一处分歧正是名字——
迁移 0003 写的是 `uq_model_providers_name`，而 ORM 的 `unique=True` 在 PG 上自动命名成
`model_providers_name_key`；两条路建出来的库语义相同、按名字 DROP 却会一个中一个空。

CHECK 约束**不做对账**：当前 20 张表两条路径都是 0 条，空对空的相等是一条永不失败的装饰。
触发条件：一旦有任何模型/迁移加 `CheckConstraint`，先让本用例在两边各反射出一条非空 CHECK，
再把它纳入对账。

没给 `BLANK_TEST_POSTGRES_URL` 时整份跳过（默认测试入口与 CI 的 api job 没有 PG）；
给了却连不上是**失败**，不是跳过——装置错不许伪装成"今天没空跑"。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from blank_api import models  # noqa: F401  create_all 的 oracle 来自已注册的表，别依赖 conftest 的导入顺序
from blank_api.db import Base

CWD = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("BLANK_TEST_POSTGRES_URL", "").strip()
# 当前 schema 有 20 张应用表（#60 实测 21 张含 alembic_version）。
# 用下界而不是等号：以后加表不该让这条守卫变红，但"只建出两三张"必须红。
MIN_TABLES = 20

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="没给 BLANK_TEST_POSTGRES_URL：PG 两条建库路径对账只在有真库时跑",
)


def _url_for(database: str) -> URL:
    parsed = make_url(POSTGRES_URL)
    if not parsed.drivername.startswith("postgresql"):
        raise AssertionError(
            f"BLANK_TEST_POSTGRES_URL 不是 PostgreSQL URL：{parsed.render_as_string(hide_password=True)}"
        )
    return parsed.set(database=database)


def _connection_kwargs(database: str) -> dict:
    parsed = make_url(POSTGRES_URL)
    return {
        "host": parsed.host or "127.0.0.1",
        "port": int(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "password": parsed.password or None,
        "database": database,
        "timeout": 10,
    }


def _admin(statements: list[str]) -> None:
    """连 maintenance 库执行建/删库；连不上就是装置错，抛 AssertionError 而不是 skip。"""
    try:
        import asyncpg
    except ImportError as error:  # pragma: no cover - asyncpg 是默认依赖
        raise AssertionError("装了默认依赖却没有 asyncpg，PG 对账装置起不来") from error

    async def run() -> None:
        try:
            connection = await asyncpg.connect(**_connection_kwargs("postgres"))
        except Exception as error:
            parsed = make_url(POSTGRES_URL)
            raise AssertionError(
                "BLANK_TEST_POSTGRES_URL 已设置但连不上 PostgreSQL"
                f"（{parsed.host}:{parsed.port}）：{error}"
            ) from error
        try:
            for statement in statements:
                await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run())


def _defaults_of(column) -> str | None:
    default = column.server_default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    raw = getattr(arg, "text", None) or str(arg)
    return " ".join(str(raw).split())


def _reflect_sync(connection) -> dict[str, dict]:
    meta = sa.MetaData()
    meta.reflect(bind=connection)
    inspector = sa.inspect(connection)
    result: dict[str, dict] = {}
    for table in meta.tables.values():
        # 名字一起比：迁移与运维脚本按名字引用索引/约束，语义相同但名字不同的库并不等价。
        indexes = {
            (index.get("name"), tuple(index.get("column_names") or []), bool(index.get("unique")))
            for index in inspector.get_indexes(table.name)
        }
        uniques = {
            (unique.get("name"), tuple(unique.get("column_names") or []))
            for unique in inspector.get_unique_constraints(table.name)
        }
        # 外键的名字与 ON DELETE 动作也算身份：自动命名在 MySQL/PG 上各起各的，
        # 而运维脚本按名字 DROP；#63 在 MySQL 上量出 tasks.thread_id 一处真分歧。
        fks = {
            (
                fk.get("name"),
                tuple(fk.get("constrained_columns") or []),
                fk.get("referred_table"),
                tuple(fk.get("referred_columns") or []),
                (fk.get("options") or {}).get("ondelete"),
                (fk.get("options") or {}).get("onupdate"),
            )
            for fk in inspector.get_foreign_keys(table.name)
        }
        result[table.name] = {
            "columns": {column.name: bool(column.nullable) for column in table.columns},
            "types": {column.name: str(column.type) for column in table.columns},
            "lengths": {column.name: getattr(column.type, "length", None) for column in table.columns},
            "fks": fks,
            "defaults": {column.name: _defaults_of(column) for column in table.columns},
            "indexes": indexes,
            "uniques": uniques,
        }
    return result


async def _reflect(url: str) -> dict[str, dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_reflect_sync)
    finally:
        await engine.dispose()


async def _create_all(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _run_alembic(url: str) -> None:
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = url
    env_vars["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", "upgrade", "head"],
        cwd=str(CWD),
        env=env_vars,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert done.returncode == 0, (
        "整条迁移历史在 PostgreSQL 上跑不通：\n" + done.stdout[-3000:] + done.stderr[-3000:]
    )


@pytest.fixture(scope="module")
def pg_pair() -> dict:
    suffix = str(os.getpid())
    names = [f"kbgai_agree_mig_{suffix}", f"kbgai_agree_orm_{suffix}"]
    _admin([f"drop database if exists {name} with (force)" for name in names])
    _admin([f"create database {name}" for name in names])
    migrated_url = _url_for(names[0]).render_as_string(hide_password=False)
    orm_url = _url_for(names[1]).render_as_string(hide_password=False)
    try:
        _run_alembic(migrated_url)
        asyncio.run(_create_all(orm_url))
        yield {
            "migrated": asyncio.run(_reflect(migrated_url)),
            "orm": asyncio.run(_reflect(orm_url)),
            "migrated_url": migrated_url,
        }
    finally:
        _admin([f"drop database if exists {name} with (force)" for name in names])


def test_both_provisioning_paths_agree_on_postgres(pg_pair: dict) -> None:
    migrated = dict(pg_pair["migrated"])
    orm = pg_pair["orm"]
    assert "alembic_version" in migrated, "迁移库连 alembic_version 都没有，整条历史等于没跑"
    migrated.pop("alembic_version")

    assert len(orm) >= MIN_TABLES, (
        f"ORM 只声明了 {len(orm)} 张表（期望至少 {MIN_TABLES}）——oracle 本身不完整，对账无意义"
    )
    assert set(migrated) == set(orm), (
        f"迁移少建 {sorted(set(orm) - set(migrated))} / ORM 多声明 {sorted(set(migrated) - set(orm))}"
    )

    differences: list[str] = []
    for name in sorted(migrated):
        left, right = migrated[name], orm[name]
        if set(left["columns"]) != set(right["columns"]):
            differences.append(
                f"{name}: 迁移缺列 {sorted(set(right['columns']) - set(left['columns']))} / "
                f"ORM 缺列 {sorted(set(left['columns']) - set(right['columns']))}"
            )
            continue
        for column in sorted(left["columns"]):
            if left["columns"][column] != right["columns"][column]:
                differences.append(
                    f"{name}.{column}: 迁移 nullable={left['columns'][column]} "
                    f"但 ORM nullable={right['columns'][column]}"
                )
        if left["fks"] != right["fks"]:
            differences.append(
                f"{name} 的外键集合分歧：迁移={sorted(left['fks'])} ORM={sorted(right['fks'])}"
            )
    assert not differences, "两条建库路径在 PG 上给出不同 schema：\n" + "\n".join(differences)


def test_server_defaults_agree_on_postgres(pg_pair: dict) -> None:
    migrated, orm = pg_pair["migrated"], pg_pair["orm"]
    differences: list[str] = []
    counted = 0
    for name in sorted(set(migrated) & set(orm)):
        left, right = migrated[name]["defaults"], orm[name]["defaults"]
        for column in sorted(set(left) & set(right)):
            if left[column] is not None:
                counted += 1
            if left[column] != right[column]:
                differences.append(
                    f"{name}.{column}: 迁移={left[column]!r} create_all={right[column]!r}"
                )

    # 防空绿：若反射没拿到默认值，两边会"全 None 相等"，这条判定就什么也没守。
    assert counted >= 17, f"只数到 {counted} 个非空服务端默认值——两边全空也会相等，判定等于没测"
    assert "[]" in (migrated["tasks"]["defaults"].get("attachments") or ""), (
        f"tasks.attachments 的服务端默认值不是 []：{migrated['tasks']['defaults'].get('attachments')!r}"
    )
    assert "{}" in (migrated["agent_runs"]["defaults"].get("request_payload") or ""), (
        "agent_runs.request_payload 的服务端默认值不是 {}："
        f"{migrated['agent_runs']['defaults'].get('request_payload')!r}"
    )
    assert not differences, "两条建库路径在 PG 上给出不同服务端默认值：\n" + "\n".join(differences)


def test_column_types_and_lengths_agree_on_postgres(pg_pair: dict) -> None:
    """列类型与长度（#62）：SQLite 对 `VARCHAR(n)` 的 n 照收不误，只有 PG/MySQL 会拒。"""
    migrated, orm = pg_pair["migrated"], pg_pair["orm"]
    differences: list[str] = []
    compared = 0
    with_length = 0
    for name in sorted(set(migrated) & set(orm)):
        left, right = migrated[name], orm[name]
        for column in sorted(set(left["types"]) & set(right["types"])):
            compared += 1
            if left["lengths"][column] is not None:
                with_length += 1
            if left["types"][column] != right["types"][column]:
                differences.append(
                    f"{name}.{column}: 迁移={left['types'][column]} ORM={right['types'][column]}"
                )
            elif left["lengths"][column] != right["lengths"][column]:
                differences.append(
                    f"{name}.{column}: 迁移 length={left['lengths'][column]} "
                    f"ORM length={right['lengths'][column]}"
                )

    # 防空绿：两边如果只反射出几张表/几列，相等也什么都没证明。
    assert compared >= 200, f"只比了 {compared} 列（当前 schema 有 213 列）——oracle 不完整"
    assert with_length >= 100, f"只有 {with_length} 列带长度——长度这一段等于没测"
    # 具体锚点：#47 把这三列的列宽从 36 放宽到 120，丢了说明对账读错了列。
    for table, column in (("model_usage", "run_id"), ("agent_events", "run_id"), ("model_usage", "origin_run_id")):
        assert migrated[table]["types"].get(column) == "VARCHAR(120)", (
            f"{table}.{column} 在迁移库上不是 VARCHAR(120)：{migrated[table]['types'].get(column)!r}"
        )
        assert migrated[table]["lengths"].get(column) == 120
    assert migrated["model_providers"]["lengths"].get("name") == 100

    assert not differences, "两条建库路径在 PG 上给出不同列类型/长度：\n" + "\n".join(differences)


def test_indexes_and_uniques_agree_on_postgres(pg_pair: dict) -> None:
    """索引与唯一约束（#62）：连名字一起比——按名字 DROP 的脚本只认名字。"""
    migrated, orm = pg_pair["migrated"], pg_pair["orm"]
    differences: list[str] = []
    index_count = 0
    unique_count = 0
    for name in sorted(set(migrated) & set(orm)):
        left, right = migrated[name], orm[name]
        index_count += len(left["indexes"])
        unique_count += len(left["uniques"])
        if left["indexes"] != right["indexes"]:
            differences.append(
                f"{name} 索引分歧：迁移独有={sorted(left['indexes'] - right['indexes'], key=repr)} "
                f"ORM 独有={sorted(right['indexes'] - left['indexes'], key=repr)}"
            )
        if left["uniques"] != right["uniques"]:
            differences.append(
                f"{name} 唯一约束分歧：迁移独有={sorted(left['uniques'] - right['uniques'], key=repr)} "
                f"ORM 独有={sorted(right['uniques'] - left['uniques'], key=repr)}"
            )

    # 防空绿：当前 20 张表共 44 个索引、6 个唯一约束；两边全空也会相等。
    assert index_count >= 40, f"只反射到 {index_count} 个索引——索引对账等于没测"
    assert unique_count >= 6, f"只反射到 {unique_count} 个唯一约束——唯一对账等于没测"
    # #62 实测到的那处分歧：迁移 0003 显式命名，ORM 的 unique=True 会被 PG 自动命名。
    assert ("uq_model_providers_name", ("name",)) in migrated["model_providers"]["uniques"], (
        f"model_providers 的唯一约束名字变了：{sorted(migrated['model_providers']['uniques'], key=repr)}"
    )

    assert not differences, "两条建库路径在 PG 上给出不同索引/唯一约束：\n" + "\n".join(differences)


def test_expression_defaults_land_values_on_postgres(pg_pair: dict) -> None:
    """DDL 里写着默认值不等于真落值：不带这两列插一行，PG 必须真给空串与时间。"""
    import asyncpg

    parsed = make_url(pg_pair["migrated_url"])

    async def probe():
        connection = await asyncpg.connect(**_connection_kwargs(parsed.database))
        try:
            await connection.execute(
                "insert into projects (id, name) values ($1, $2)", "p-agree", "默认值对账"
            )
            return await connection.fetchrow(
                "select description, (created_at is not null) as has_created, "
                "(updated_at is not null) as has_updated from projects where id = $1",
                "p-agree",
            )
        finally:
            await connection.close()

    row = asyncio.run(probe())
    assert row["description"] == "", f"projects.description 的默认值落成 {row['description']!r}，不是空串"
    assert row["has_created"] and row["has_updated"], (
        "created_at / updated_at 的 server_default=now() 在 PG 上没生效："
        f"created={row['has_created']} updated={row['has_updated']}"
    )
