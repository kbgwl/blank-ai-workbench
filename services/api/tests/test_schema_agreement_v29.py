"""两条建库路径必须给出同一个 schema：`alembic upgrade head` 与 `Base.metadata.create_all`。

这一批（#49）的起点是实测：整条迁移历史在真 MySQL 8.4 上死在 0002 的
`projects.description`（1101，`BLOB, TEXT, GEOMETRY or JSON column ... can't have a default value`），
清掉 12 处 TEXT 之后又死在 0008 的 `agent_runs.request_payload`（同类，JSON 列），共 17 处；
而 ORM 声明层自己也有 4 处（`tasks.attachments`、`team_rounds.queue/spoken`、`team_messages.meta`），
`create_all` 在 MySQL 上同样抛 1101（`E:/tmp/m48/mysql_schema_agreement.log` S1）。
把默认值写成 8.0.13+ 的**表达式默认值** `DEFAULT ('')` 是两种方言都接受的唯一写法，
实测口径不变：不带这一列插入，MySQL 和 SQLite 都落 `''` / `{}` / `[]`，不是 NULL。

SQLite 上这些差别一个字都看不见——列宽、可空性、字面量默认值全都照收不误，
所以这里做的是**跨路径对账**，而不是再加一条只测 SQLite 行为的用例：

  A1 迁移动出来的表集合/列名集合/可空性/外键集合 == ORM 声明的（#47 列宽、#48 外键、#49 默认值
      三次分歧都是这一类，只是当时按具体问题各补了一次；这一条把它们统一成一条闸门）
  A1b 类型/长度、索引/唯一约束/主键、服务端默认值也逐列/逐对象对账（#64 从 PG/MySQL 的
      #61–#63 搬回来的维度；SQLite 是主方言，生产走 alembic，测试走 create_all，两边差一点
      就可能出现"测试全绿、生产缺索引"）。实测 20 表 / 213 列（118 列带长度）/
      38 索引 / 6 唯一约束 / 21 外键 / 125 个非空默认值 / 0 个 CHECK。CHECK 两边都是 0，
      不写空对空判定；哪天某条迁移引入 CHECK，再补对账。
  A2 ORM 声明编译成 MySQL DDL 后，LOB 列不许带裸字面量 DEFAULT（能失败的守卫：改回 `server_default="[]"` 即红）
  A3 迁移文件里也不许再出现 LOB 列 + 纯字符串 `server_default`（AST 扫，认 `sa.Text()`/`Text`/`JSON` 三种写法——
      只认"函数调用"的那种扫法漏掉了 `mapped_column(JSON, ...)`，本批实测被真 MySQL 打回过一次）
  A4 迁移出来的库上，不带 `description` 插入一行必须真落空串（改写法不许把默认值口径带跑）

期望值全部来自迁移后的实际 DDL/行值，不来自注释。
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import CreateTable
from sqlalchemy.dialects import mysql

from blank_api import models  # noqa: F401  create_all 的 oracle 来自已注册的表，别依赖 conftest 的导入顺序
from blank_api.db import Base

CWD = Path(__file__).resolve().parents[1]
VERSIONS = CWD / "alembic" / "versions"
LOB_TYPES = {"Text", "JSON"}

# 实测下界（#64，迁移动出来的那个库）：表 20 / 列 213 / 带长度 118 / 索引 38 /
# 唯一约束 6 / 外键 21 / 非空默认值 125。下界只防"对账对象是空的所以全绿"。
MIN_TABLES = 20
MIN_COLUMNS = 200
MIN_LENGTHS = 100
MIN_INDEXES = 30
MIN_UNIQUES = 6
MIN_FKS = 20
MIN_DEFAULTS = 120

# 原裸字面量靶点：改完之后，这些列在迁移动出来的库里必须还带那个字面量默认值
LITERAL_SITES = [
    ("projects", "description", ""),
    ("tasks", "goal", ""),
    ("tasks", "result_summary", ""),
    ("tasks", "failure_reason", ""),
    ("tasks", "attachments", "[]"),
    ("tool_calls", "result_summary", ""),
    ("file_changes", "diff", ""),
    ("agent_runs", "request_payload", "{}"),
    ("team_bots", "instructions", ""),
    ("team_groups", "topic", ""),
    ("team_rounds", "error", ""),
    ("team_rounds", "queue", "[]"),
    ("team_rounds", "spoken", "[]"),
    ("team_messages", "content", ""),
    ("team_messages", "meta", "{}"),
    ("search_docs", "content", ""),
    ("search_docs", "tokens", ""),
]


@pytest.fixture(scope="module")
def provisioned_pair(tmp_path_factory) -> dict[str, Path]:
    """同一条历史的两种出生方式：`alembic upgrade head` 与 `create_all`，各建一个真库。

    迁移用 subprocess + `alembic` 而不是在测试进程里调 `command.upgrade`：
    env.py 走的是应用的异步引擎，同进程里跑会把事件循环和连接池搅在一起。
    create_all 是测试与本地快速起库走的那条路；两者形状不一致，测试库就与生产不是同一个东西。
    """
    base = tmp_path_factory.mktemp("schema-agreement")
    migrated_path = base / "migrated.db"
    orm_path = base / "orm.db"
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{migrated_path.as_posix()}"
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
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert migrated_path.exists(), "alembic rc=0 但库文件不在（这条历史从没被真跑过）"

    engine = sa.create_engine(f"sqlite:///{orm_path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    assert orm_path.exists(), "create_all 没建出库文件"
    return {"migrated": migrated_path, "orm": orm_path}


@pytest.fixture(scope="module")
def migrated_db(provisioned_pair: dict[str, Path]) -> Path:
    return provisioned_pair["migrated"]


def _default_of(column) -> str | None:
    default = column.server_default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    raw = getattr(arg, "text", None) or str(arg)
    return " ".join(str(raw).split())


def _reflected(db_path: Path) -> dict[str, dict]:
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        meta = sa.MetaData()
        meta.reflect(bind=engine, extend_existing=True)
        inspector = sa.inspect(engine)
        shape = {}
        for table in meta.tables.values():
            pk = inspector.get_pk_constraint(table.name)
            shape[table.name] = {
                "columns": {c.name: bool(c.nullable) for c in table.columns},
                "types": {c.name: str(c.type) for c in table.columns},
                "lengths": {c.name: getattr(c.type, "length", None) for c in table.columns},
                "defaults": {c.name: _default_of(c) for c in table.columns},
                # SQLite 只有显式命名的外键才反射得出名字（`fk_tasks_thread_id`），其余是 None；
                # 名字与 ON DELETE 都算身份：按名字 DROP 的脚本只认名字。
                "fks": {
                    (
                        fk.get("name"),
                        tuple(fk.get("constrained_columns") or []),
                        fk.get("referred_table"),
                        tuple(fk.get("referred_columns") or []),
                        (fk.get("options") or {}).get("ondelete"),
                        (fk.get("options") or {}).get("onupdate"),
                    )
                    for fk in inspector.get_foreign_keys(table.name)
                },
                "indexes": {
                    (ix.get("name"), tuple(ix.get("column_names") or []), bool(ix.get("unique")))
                    for ix in inspector.get_indexes(table.name)
                },
                "uniques": {
                    (uq.get("name"), tuple(uq.get("column_names") or []))
                    for uq in inspector.get_unique_constraints(table.name)
                },
                "checks": {
                    (ck.get("name"), ck.get("sqltext"))
                    for ck in inspector.get_check_constraints(table.name)
                },
                "pk": (pk.get("name"), tuple(pk.get("constrained_columns") or [])),
            }
        return shape
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# A1：两条建库路径对账
# --------------------------------------------------------------------------- #


def _pair(provisioned_pair: dict[str, Path]) -> tuple[dict[str, dict], dict[str, dict]]:
    migrated = dict(_reflected(provisioned_pair["migrated"]))
    assert "alembic_version" in migrated, "迁移库连 alembic_version 都没有，整条历史等于没跑"
    migrated.pop("alembic_version")
    orm = _reflected(provisioned_pair["orm"])
    assert len(orm) >= MIN_TABLES, (
        f"ORM 只建出 {len(orm)} 张表（期望至少 {MIN_TABLES}）——oracle 本身不完整，对账无意义"
    )
    return migrated, orm


def test_both_provisioning_paths_agree(provisioned_pair: dict[str, Path]) -> None:
    migrated, orm = _pair(provisioned_pair)

    assert set(migrated) == set(orm), (
        f"迁移少建 {sorted(set(orm) - set(migrated))} / ORM 多声明 {sorted(set(migrated) - set(orm))}"
    )
    column_count = sum(len(t["columns"]) for t in migrated.values())
    fk_count = sum(len(t["fks"]) for t in migrated.values())
    assert column_count >= MIN_COLUMNS, f"迁移只建出 {column_count} 列（期望至少 {MIN_COLUMNS}）"
    assert fk_count >= MIN_FKS, f"迁移只建出 {fk_count} 个外键（期望至少 {MIN_FKS}）"

    differences: list[str] = []
    for name in sorted(migrated):
        left, right = migrated[name]["columns"], orm[name]["columns"]
        if set(left) != set(right):
            differences.append(
                f"{name}: 迁移缺列 {sorted(set(right) - set(left))} / ORM 缺列 {sorted(set(left) - set(right))}"
            )
            continue
        differences.extend(
            f"{name}.{column}: 迁移 nullable={left[column]} 但 ORM nullable={right[column]}"
            for column in sorted(left)
            if left[column] != right[column]
        )
        if migrated[name]["fks"] != orm[name]["fks"]:
            differences.append(
                f"{name}: 外键集合分歧 仅迁移={sorted(migrated[name]['fks'] - orm[name]['fks'], key=repr)} "
                f"仅ORM={sorted(orm[name]['fks'] - migrated[name]['fks'], key=repr)}"
            )
    assert not differences, "两条建库路径的形状不一致：" + "; ".join(differences)


def test_column_types_and_lengths_agree(provisioned_pair: dict[str, Path]) -> None:
    migrated, orm = _pair(provisioned_pair)

    compared = with_length = 0
    differences: list[str] = []
    for name in sorted(migrated):
        left, right = migrated[name], orm[name]
        for column in sorted(set(left["types"]) & set(right["types"])):
            compared += 1
            if left["types"][column] != right["types"][column]:
                differences.append(
                    f"{name}.{column}: 类型 迁移={left['types'][column]!r} ORM={right['types'][column]!r}"
                )
            if left["lengths"][column] is not None:
                with_length += 1
            if left["lengths"][column] != right["lengths"][column]:
                differences.append(
                    f"{name}.{column}: 长度 迁移={left['lengths'][column]!r} ORM={right['lengths'][column]!r}"
                )
    assert compared >= MIN_COLUMNS, f"只比到 {compared} 列（期望至少 {MIN_COLUMNS}）——对账对象不完整"
    assert with_length >= MIN_LENGTHS, f"只比到 {with_length} 个带长度的列（期望至少 {MIN_LENGTHS}）"
    assert not differences, "类型/长度分歧：" + "; ".join(differences)

    anchors = {
        ("model_usage", "run_id"): 120,
        ("agent_events", "run_id"): 120,
        ("model_usage", "origin_run_id"): 120,
        ("model_providers", "name"): 100,
    }
    for (table, column), length in anchors.items():
        assert migrated[table]["lengths"][column] == length, (
            f"锚点 {table}.{column} 在迁移库里的长度是 {migrated[table]['lengths'][column]!r}，"
            f"不是实测的 {length}——对账锚点失效"
        )


def test_indexes_and_uniques_agree(provisioned_pair: dict[str, Path]) -> None:
    migrated, orm = _pair(provisioned_pair)

    index_count = sum(len(t["indexes"]) for t in migrated.values())
    unique_count = sum(len(t["uniques"]) for t in migrated.values())
    assert index_count >= MIN_INDEXES, f"迁移只建出 {index_count} 个索引（期望至少 {MIN_INDEXES}）"
    assert unique_count >= MIN_UNIQUES, f"迁移只建出 {unique_count} 个唯一约束（期望至少 {MIN_UNIQUES}）"

    differences: list[str] = []
    for name in sorted(migrated):
        for key, label in (("indexes", "索引"), ("uniques", "唯一约束")):
            only_migrated = migrated[name][key] - orm[name][key]
            only_orm = orm[name][key] - migrated[name][key]
            if only_migrated or only_orm:
                differences.append(
                    f"{name}: {label} 仅迁移={sorted(only_migrated, key=repr)} "
                    f"仅ORM={sorted(only_orm, key=repr)}"
                )
        if migrated[name]["pk"] != orm[name]["pk"]:
            differences.append(f"{name}: 主键 迁移={migrated[name]['pk']!r} ORM={orm[name]['pk']!r}")
    assert not differences, "索引/约束分歧：" + "; ".join(differences)

    assert ("uq_model_providers_name", ("name",)) in migrated["model_providers"]["uniques"], (
        f"model_providers 上没有实测过的命名唯一约束，实测={migrated['model_providers']['uniques']}"
    )
    assert ("ix_model_usage_parallel_group", ("parallel_group",), False) in migrated["model_usage"]["indexes"], (
        f"model_usage 上没有 parallel_group 索引，实测={migrated['model_usage']['indexes']}"
    )


def test_server_defaults_agree(provisioned_pair: dict[str, Path]) -> None:
    migrated, orm = _pair(provisioned_pair)

    default_count = sum(1 for t in migrated.values() for v in t["defaults"].values() if v is not None)
    assert default_count >= MIN_DEFAULTS, f"迁移只留下 {default_count} 个默认值（期望至少 {MIN_DEFAULTS}）"

    differences = [
        f"{name}.{column}: 迁移={migrated[name]['defaults'][column]!r} "
        f"ORM={orm[name]['defaults'].get(column, '<缺列>')!r}"
        for name in sorted(migrated)
        for column in sorted(migrated[name]["defaults"])
        if migrated[name]["defaults"][column] != orm[name]["defaults"].get(column)
    ]
    assert not differences, "默认值分歧（SQLite 上 true()/false() 与迁移写的 '1'/'0' 必须反射成同一个字面量）：" + "; ".join(differences)

    anchors = {
        ("model_providers", "supports_tools"): "0",
        ("model_providers", "supports_vision"): "0",
        ("model_providers", "enabled"): "1",
        ("team_bots", "is_admin"): "0",
        ("team_bots", "enabled"): "1",
        ("team_members", "enabled"): "1",
        ("team_rounds", "wrap_up_done"): "0",
        ("workspaces", "is_active"): "1",
        ("tasks", "attachments"): "'[]'",
        ("tasks", "goal"): "''",
        ("agent_runs", "request_payload"): "'{}'",
    }
    for (table, column), expected in anchors.items():
        assert migrated[table]["defaults"][column] == expected, (
            f"锚点 {table}.{column} 的默认值是 {migrated[table]['defaults'][column]!r}，"
            f"不是实测的 {expected!r}——对账锚点失效"
        )


# --------------------------------------------------------------------------- #
# A2 / A3：LOB 列不许带裸字面量默认值
# --------------------------------------------------------------------------- #

# DDL 行形如 `  attachments JSON NOT NULL DEFAULT '[]'`；表达式默认值是 `DEFAULT ('[]')`，
# 所以"DEFAULT 后面紧跟的不是左括号"就是会被 MySQL 1101 拒掉的那一种。
LOB_WITH_LITERAL = re.compile(
    r"^\s*\"?\w+\"?\s+(TEXT|JSON|BLOB|LONGTEXT|MEDIUMTEXT|TINYTEXT)\b[^,]*?\sDEFAULT\s+(?!\()",
    re.I | re.M,
)


def test_orm_mysql_ddl_has_no_literal_default_on_lob_columns() -> None:
    offenders = []
    for table in Base.metadata.tables.values():
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
        for line in ddl.splitlines():
            if LOB_WITH_LITERAL.match(line):
                offenders.append(f"{table.name}: {line.strip()}")
    assert not offenders, (
        "ORM 声明编译到 MySQL 会在这些 LOB 列上写字面量默认值（1101）；"
        "改成表达式默认值 server_default=text(\"('[]')\")：" + "; ".join(offenders)
    )


def _call_column_nodes(tree: ast.AST):
    """_yield_ 每个 sa.Column(...) / mapped_column(...) 调用节点。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in {"Column", "mapped_column"}:
                yield node


def _declared_type(call: ast.Call) -> str | None:
    """类型参数既可能是 `sa.Text()`（调用），也可能是 `Text`/`JSON`（直接导入的名字）。

    上一批按"必须是 Call"扫，报 0 处，而真 MySQL 在 `tasks.attachments` 上抛了 1101——
    那一条写的就是 `mapped_column(JSON, ...)`。
    """
    keywords = {kw.arg: kw.value for kw in call.keywords}
    candidate = keywords.get("type_")
    if candidate is None:
        candidate = call.args[1] if len(call.args) > 1 else (call.args[0] if call.args else None)
    if isinstance(candidate, ast.Call):
        return getattr(candidate.func, "attr", None) or getattr(candidate.func, "id", None)
    if isinstance(candidate, ast.Name):
        return candidate.id
    return None


def test_migration_files_have_no_literal_default_on_lob_columns() -> None:
    offenders = []
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
        for call in _call_column_nodes(tree):
            if _declared_type(call) not in LOB_TYPES:
                continue
            for keyword in call.keywords:
                if keyword.arg == "server_default" and isinstance(keyword.value, ast.Constant):
                    if isinstance(keyword.value.value, str):
                        offenders.append(f"{path.name}:{call.lineno} 裸字面量 {keyword.value.value!r}")
    assert not offenders, "迁移里 LOB 列不许用纯字符串 server_default（MySQL 8.4 会 1101）：" + "; ".join(offenders)


# --------------------------------------------------------------------------- #
# A4：换写法以后默认值落的东西没变
# --------------------------------------------------------------------------- #


def test_expression_default_still_lands_empty_string(migrated_db: Path) -> None:
    conn = sqlite3.connect(migrated_db)
    try:
        conn.execute("insert into projects (id, name) values ('p-1', '账本对账')")
        conn.commit()
        row = conn.execute("select description, created_at is null from projects where id='p-1'").fetchone()
    finally:
        conn.close()
    assert row[0] == "", f"`projects.description` 的默认值不再是空串，而是 {row[0]!r}"
    assert row[1] == 0, "created_at 的 server_default=now() 在迁移动出来的库上不再生效"


def test_all_literal_default_sites_kept_their_value(migrated_db: Path) -> None:
    """17 处改写的列，在迁移动出来的 SQLite 库里 DDL 必须还写着那个字面量。

    取 `sqlite_master` 原文而不是 reflected 值：反射会把 `DEFAULT ('')` 与 `DEFAULT ''`
    念成同一个 `''`，那样这条守卫就永远不会红——它守的是"这一列没把默认值丢掉"。
    """
    conn = sqlite3.connect(migrated_db)
    try:
        ddl = {
            name: sql
            for name, sql in conn.execute("select name, sql from sqlite_master where type='table'").fetchall()
        }
    finally:
        conn.close()
    missing = []
    for table, column, expected in LITERAL_SITES:
        assert table in ddl, f"迁移没建出 {table}，无法核对 {column} 的默认值"
        pattern = re.compile(
            rf'["\']?{column}["\']?\s+\w+(?:\([\w,]+\))?[^,)]*DEFAULT\s+\(?\s*[\'"]([^\'"]*)[\'"]',
            re.I,
        )
        match = pattern.search(ddl[table])
        if match is None or match.group(1) != expected:
            missing.append(f"{table}.{column}: 实测 {match.group(0) if match else '没匹配到 DEFAULT'}（期望字面量 {expected!r}）")
    assert not missing, "这些列的默认值不见了或变了：" + "; ".join(missing)


# --------------------------------------------------------------------------- #
# A5：0023 的 NULL 闸门必须真的能拦住
#     （MySQL 上 `ALTER TABLE ... MODIFY ... NOT NULL` 对存量 NULL 只给 1138 警告，
#      静默把它们变成 `0000-00-00 00:00:00`——所以闸门必须在改之前先数）
# --------------------------------------------------------------------------- #


def _alembic(args: list[str], db_path: Path) -> subprocess.CompletedProcess[str]:
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env_vars["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", *args],
        cwd=str(CWD),
        env=env_vars,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def test_timestamp_migration_refuses_existing_nulls(tmp_path: Path) -> None:
    db_path = tmp_path / "has-null.db"
    done = _alembic(["upgrade", "0022_ledger_no_cascade"], db_path)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]

    conn = sqlite3.connect(db_path)
    try:
        # 0022 的库允许 NULL，所以此刻这条插入必须成功——否则下面测的就不是这一条闸门
        conn.execute(
            "insert into threads (id, project_id, title, status, turn_count, kind, created_at) "
            "values ('t-null', 'p-1', '存量 NULL', 'idle', 0, 'team', NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    done = _alembic(["upgrade", "head"], db_path)
    combined = done.stdout + done.stderr
    assert done.returncode != 0, (
        f"库里有 created_at IS NULL 的行，0023 却照样跑通——闸门形同虚设：\n{combined[-1200:]}"
    )
    assert "还有 1 行是 NULL" in combined, f"失败原因不是 NULL 闸门，而是别的东西：\n{combined[-1200:]}"

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("select version_num from alembic_version").fetchone()[0]
        rows = conn.execute("select id, created_at from threads order by id").fetchall()
    finally:
        conn.close()
    assert version == "0022_ledger_no_cascade", f"整条迁移没回滚，版本停在 {version}"
    assert rows == [("t-null", None)], f"回滚后那行被改了：{rows}"
