"""两条建库路径在真 MySQL 8.4 上必须给出同一个 schema（#63）。

#61/#62 把对账扩到了类型/长度/索引/唯一约束（PG），但 MySQL 侧自 #49 之后只剩
「迁移能跑通」与 HTTP 端到端，`create_all` 这条路没再逐列对过。这一批在真 MySQL 8.4
（127.0.0.1:3307）上开两个库分别跑两条路径，量出并修掉一处真分歧：

  tasks.thread_id 的外键：迁移 0014 显式命名 `fk_tasks_thread_id`，ORM 没写名字，
  MySQL 自动起成 `tasks_ibfk_2`、PG 起成 `tasks_thread_id_fkey`。按名字 DROP 的脚本
  会一个中一个空。修法是 ORM 补同名 ForeignKey；PG 侧的对账（v33）也补上名字与
  ON DELETE 动作，不再只比「从哪列指向哪列」。

另有一处**不是**分歧：12 个 `created_at/updated_at` 的默认值在迁移库里念作
`CURRENT_TIMESTAMP`，在 create_all 库里念作 `(now())`。成因是 0023 在 MySQL 上
`ALTER ... SET NOT NULL` 必须重述整列定义，Alembic 把 `existing_server_default`
写回成了前者；MySQL 视两者为同义（`NOW()` 与 `CURRENT_TIMESTAMP` 同义）。
所以这里只归一这一族写法，并用「不带该列插入一行必须真落当前时间」证明归一没有
掩盖「默认值丢了」。别的默认值仍逐字比较。

CHECK 约束在两条路径上都是 0 条（#62 已记录触发条件），所以不比较；索引的
USING/前缀长度/隐藏列同样不比较，留作已记录缺口。没给 `BLANK_TEST_MYSQL_URL`
时整份跳过；给了却连不上是**失败**，不是跳过。
"""

from __future__ import annotations

import asyncio
import json
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
MYSQL_URL = os.environ.get("BLANK_TEST_MYSQL_URL", "").strip()
MIN_TABLES = 20
MIN_COLUMNS = 200
MIN_LENGTHS = 100
MIN_INDEXES = 40
MIN_UNIQUES = 6
MIN_FKS = 20
MIN_DEFAULTS = 120

# 0023 在 MySQL 上重述默认值的那 12 列；它们是 CURRENT_TIMESTAMP / (now()) 的唯一合法分歧。
TIMESTAMP_SPELLING_SITES = {
    ("threads", "created_at"),
    ("threads", "updated_at"),
    ("team_bots", "created_at"),
    ("team_bots", "updated_at"),
    ("team_groups", "created_at"),
    ("team_groups", "updated_at"),
    ("team_members", "created_at"),
    ("team_messages", "created_at"),
    ("team_rounds", "created_at"),
    ("team_rounds", "updated_at"),
    ("search_docs", "created_at"),
    ("search_docs", "updated_at"),
}

pytestmark = pytest.mark.skipif(
    not MYSQL_URL,
    reason="没给 BLANK_TEST_MYSQL_URL：MySQL 两条建库路径对账只在有真库时跑",
)


def _url_for(database: str) -> URL:
    parsed = make_url(MYSQL_URL)
    if not parsed.drivername.startswith("mysql"):
        raise AssertionError(
            f"BLANK_TEST_MYSQL_URL 不是 MySQL URL：{parsed.render_as_string(hide_password=True)}"
        )
    return parsed.set(database=database)


async def _admin(statements: list[str]) -> None:
    """连 `mysql` 系统库执行建/删库；连不上是装置错，抛 AssertionError 而不是 skip。"""
    engine = create_async_engine(_url_for("mysql"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(sa.text(statement))
    except Exception as error:
        parsed = make_url(MYSQL_URL)
        raise AssertionError(
            f"BLANK_TEST_MYSQL_URL 已设置但连不上 MySQL（{parsed.host}:{parsed.port}）：{error}"
        ) from error
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
        timeout=900,
    )
    assert done.returncode == 0, (
        "整条迁移历史在 MySQL 上跑不通：\n" + done.stdout[-3000:] + done.stderr[-3000:]
    )


async def _create_all(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _default_of(column) -> str | None:
    default = column.server_default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    raw = getattr(arg, "text", None) or str(arg)
    return " ".join(str(raw).split())


def _canonical_default(value: str | None) -> str | None:
    if value is None:
        return None
    compact = value.strip().lower().replace(" ", "")
    if compact in {
        "current_timestamp",
        "current_timestamp()",
        "now()",
        "(now())",
        "(current_timestamp)",
        "(current_timestamp())",
    }:
        return "<CURRENT_TIMESTAMP>"
    return value


def _reflect_sync(connection) -> dict[str, dict]:
    inspector = sa.inspect(connection)
    meta = sa.MetaData()
    meta.reflect(bind=connection)
    result: dict[str, dict] = {}
    for table in meta.tables.values():
        fks = inspector.get_foreign_keys(table.name)
        indexes = inspector.get_indexes(table.name)
        uniques = inspector.get_unique_constraints(table.name)
        checks = inspector.get_check_constraints(table.name)
        pk = inspector.get_pk_constraint(table.name)
        result[table.name] = {
            "columns": {column.name: bool(column.nullable) for column in table.columns},
            "types": {column.name: str(column.type) for column in table.columns},
            "lengths": {column.name: getattr(column.type, "length", None) for column in table.columns},
            "defaults": {column.name: _default_of(column) for column in table.columns},
            # 名字与动作都算身份：按名字 DROP 的脚本只认名字，ON DELETE 决定删父行时孩子留不留。
            "fks": sorted(
                (
                    fk.get("name"),
                    tuple(fk.get("constrained_columns") or []),
                    fk.get("referred_table"),
                    tuple(fk.get("referred_columns") or []),
                    (fk.get("options") or {}).get("ondelete"),
                    (fk.get("options") or {}).get("onupdate"),
                )
                for fk in fks
            ),
            # MySQL 的 get_indexes 会把唯一约束背后的索引也列一遍（unique=True），
            # 与 get_unique_constraints 重复；两边同源即可，唯一性由 uniques 集合单独钉住。
            "indexes": {
                (index.get("name"), tuple(index.get("column_names") or []), bool(index.get("unique")))
                for index in indexes
            },
            "uniques": {
                (unique.get("name"), tuple(unique.get("column_names") or []))
                for unique in uniques
            },
            "checks": {(check.get("name"), check.get("sqltext")) for check in checks},
            "pk": (pk.get("name"), tuple(pk.get("constrained_columns") or [])),
        }
    return result


async def _reflect(url: str) -> dict[str, dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_reflect_sync)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def mysql_pair() -> dict:
    suffix = str(os.getpid())
    names = [f"kbgai_agree_mig_{suffix}", f"kbgai_agree_orm_{suffix}"]
    asyncio.run(_admin([f"drop database if exists `{name}`" for name in names]))
    asyncio.run(_admin([f"create database `{name}` character set utf8mb4 collate utf8mb4_0900_ai_ci" for name in names]))
    urls = [_url_for(name).render_as_string(hide_password=False) for name in names]
    try:
        _run_alembic(urls[0])
        asyncio.run(_create_all(urls[1]))
        yield {
            "migrated": asyncio.run(_reflect(urls[0])),
            "orm": asyncio.run(_reflect(urls[1])),
            "urls": urls,
        }
    finally:
        asyncio.run(_admin([f"drop database if exists `{name}`" for name in names]))


def test_both_provisioning_paths_agree_on_mysql(mysql_pair: dict) -> None:
    migrated = dict(mysql_pair["migrated"])
    orm = mysql_pair["orm"]
    assert "alembic_version" in migrated, "迁移库连 alembic_version 都没有，整条历史等于没跑"
    migrated.pop("alembic_version")

    assert len(orm) >= MIN_TABLES, (
        f"ORM 只声明了 {len(orm)} 张表（期望至少 {MIN_TABLES}）——oracle 本身不完整，对账无意义"
    )
    assert set(migrated) == set(orm), (
        f"迁移少建 {sorted(set(orm) - set(migrated))} / ORM 多声明 {sorted(set(migrated) - set(orm))}"
    )

    columns = sum(len(table["columns"]) for table in migrated.values())
    lengths = sum(1 for table in migrated.values() for value in table["lengths"].values() if value is not None)
    indexes = sum(len(table["indexes"]) for table in migrated.values())
    uniques = sum(len(table["uniques"]) for table in migrated.values())
    fks = sum(len(table["fks"]) for table in migrated.values())
    defaults = sum(1 for table in migrated.values() for value in table["defaults"].values() if value is not None)
    assert columns >= MIN_COLUMNS, f"只数到 {columns} 列（期望至少 {MIN_COLUMNS}）——对账可能只覆盖了几张表"
    assert lengths >= MIN_LENGTHS, f"只数到 {lengths} 个带长度的列（期望至少 {MIN_LENGTHS}）——类型维度等于没比"
    assert indexes >= MIN_INDEXES, f"只数到 {indexes} 个索引（期望至少 {MIN_INDEXES}）——索引维度等于没比"
    assert uniques >= MIN_UNIQUES, f"只数到 {uniques} 个唯一约束（期望至少 {MIN_UNIQUES}）——唯一维度等于没比"
    assert fks >= MIN_FKS, f"只数到 {fks} 个外键（期望至少 {MIN_FKS}）——外键维度等于没比"
    assert defaults >= MIN_DEFAULTS, (
        f"只数到 {defaults} 个非空服务端默认值（期望至少 {MIN_DEFAULTS}）——两边全空也会相等，判定等于没测"
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
            if left["types"][column] != right["types"][column]:
                differences.append(
                    f"{name}.{column}: 类型 迁移={left['types'][column]!r} 但 ORM={right['types'][column]!r}"
                )
            if left["lengths"][column] != right["lengths"][column]:
                differences.append(
                    f"{name}.{column}: 长度 迁移={left['lengths'][column]} 但 ORM={right['lengths'][column]}"
                )
            if _canonical_default(left["defaults"][column]) != _canonical_default(right["defaults"][column]):
                differences.append(
                    f"{name}.{column}: 默认值 迁移={left['defaults'][column]!r} "
                    f"但 ORM={right['defaults'][column]!r}"
                )
        for key, label in (
            ("fks", "外键"),
            ("indexes", "索引"),
            ("uniques", "唯一约束"),
            ("checks", "CHECK"),
            ("pk", "主键"),
        ):
            if left[key] != right[key]:
                only_left = sorted((item for item in left[key] if item not in right[key]), key=repr)
                only_right = sorted((item for item in right[key] if item not in left[key]), key=repr)
                differences.append(f"{name}: {label} 仅迁移={only_left} 仅 ORM={only_right}")
    assert not differences, "两条建库路径在 MySQL 上给出不同 schema：\n" + "\n".join(differences[:40])


def test_timestamp_default_spellings_are_equivalent_on_mysql(mysql_pair: dict) -> None:
    """0023 在 MySQL 上重述出的 `CURRENT_TIMESTAMP` 与 create_all 的 `(now())` 必须只差写法。

    先钉住"分歧恰好是这 12 列、且归一后相等"，再在两边真插一行不带时间列的记录，
    证明这个归一没有掩盖"默认值丢了"。
    """
    migrated, orm = mysql_pair["migrated"], mysql_pair["orm"]
    spelling_sites = set()
    for name in sorted(set(migrated) & set(orm)):
        for column in sorted(set(migrated[name]["defaults"]) & set(orm[name]["defaults"])):
            left = migrated[name]["defaults"][column]
            right = orm[name]["defaults"][column]
            if left == right:
                continue
            if left is None or right is None:
                continue  # 默认值有没有，归两条对账用例管，这里只管写法
            if _canonical_default(left) != _canonical_default(right):
                continue  # 真换了表达式（比如 UTC_TIMESTAMP），同样归对账用例管
            assert _canonical_default(left) == "<CURRENT_TIMESTAMP>", (
                f"{name}.{column} 不是时间同义写法却被归一了：迁移={left!r} ORM={right!r}"
            )
            spelling_sites.add((name, column))
    assert spelling_sites == TIMESTAMP_SPELLING_SITES, (
        "时间默认值的写法分歧变了："
        f"多出 {sorted(spelling_sites - TIMESTAMP_SPELLING_SITES)} / "
        f"少了 {sorted(TIMESTAMP_SPELLING_SITES - spelling_sites)}"
    )

    async def probe(url: str) -> dict:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("insert into projects (id, name) values ('p-ts', '时间默认值')")
                )
                await connection.execute(
                    sa.text("insert into threads (id, project_id, title) values ('t-ts', 'p-ts', '对账')")
                )
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        sa.text(
                            "select (created_at is not null) as has_created, "
                            "(updated_at is not null) as has_updated from threads where id='t-ts'"
                        )
                    )
                ).mappings().one()
            return dict(row)
        finally:
            await engine.dispose()

    for label, url in zip(("迁移", "create_all"), mysql_pair["urls"]):
        row = asyncio.run(probe(url))
        assert row["has_created"] and row["has_updated"], (
            f"{label}库不带时间列插入后 created_at/updated_at 有空值：{row}——默认值等于没有"
        )


def test_foreign_key_names_and_actions_agree_on_mysql(mysql_pair: dict) -> None:
    migrated, orm = mysql_pair["migrated"], mysql_pair["orm"]
    for label, shape in (("迁移", migrated), ("create_all", orm)):
        tasks = {fk[0]: fk for fk in shape["tasks"]["fks"]}
        assert "fk_tasks_thread_id" in tasks, (
            f"{label}库里 tasks.thread_id 的外键不是显式名字：{sorted(tasks)}——"
            "自动命名在 MySQL/PG 上各不相同，按名字 DROP 会扑空"
        )
        assert tasks["fk_tasks_thread_id"][4] == "SET NULL", (
            f"{label}库 fk_tasks_thread_id 的 ON DELETE 不是 SET NULL：{tasks['fk_tasks_thread_id'][4]!r}"
        )
    assert {fk[0] for fk in migrated["tasks"]["fks"]} == {fk[0] for fk in orm["tasks"]["fks"]}, (
        f"tasks 的外键名集合分歧：迁移={sorted(fk[0] for fk in migrated['tasks']['fks'])} "
        f"ORM={sorted(fk[0] for fk in orm['tasks']['fks'])}"
    )


def test_expression_defaults_land_values_on_mysql(mysql_pair: dict) -> None:
    """#49 的表达式默认值（`('[]')` / `('')` / `('{}')`）在两种建库路径上都要真落值。"""
    async def probe(url: str) -> dict:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                # tasks.project_id 有外键，先建项目
                await connection.execute(
                    sa.text("insert into projects (id, name) values ('p-def', '默认值')")
                )
                await connection.execute(
                    sa.text(
                        "insert into tasks (id, project_id, title, acceptance_criteria, acceptance_results) "
                        "values ('t-def', 'p-def', '默认值', '[]', '[]')"
                    )
                )
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        sa.text(
                            "select goal, attachments, max_steps from tasks where id='t-def'"
                        )
                    )
                ).mappings().one()
            return dict(row)
        finally:
            await engine.dispose()

    for label, url in zip(("迁移", "create_all"), mysql_pair["urls"]):
        row = asyncio.run(probe(url))
        # 裸 SQL 不走 SQLAlchemy 的 JSON 类型反序列化，MySQL 会把 JSON 列原样念成字符串。
        attachments = json.loads(row["attachments"]) if isinstance(row["attachments"], str) else row["attachments"]
        assert row["goal"] == "", f"{label}库 tasks.goal 的默认值落成 {row['goal']!r}，不是空串"
        assert attachments == [], (
            f"{label}库 tasks.attachments 的默认值落成 {row['attachments']!r}，不是 []"
        )
        assert row["max_steps"] == 8, f"{label}库 tasks.max_steps 的默认值落成 {row['max_steps']!r}，不是 8"
