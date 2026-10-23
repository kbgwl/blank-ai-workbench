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

from blank_api.db import Base

CWD = Path(__file__).resolve().parents[1]
VERSIONS = CWD / "alembic" / "versions"
LOB_TYPES = {"Text", "JSON"}

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
def migrated_db(tmp_path_factory) -> Path:
    """真跑一遍整条历史（0001 → head），拿到迁移出来的那个库。

    用 subprocess + `alembic` 而不是在测试进程里调 `command.upgrade`：
    env.py 走的是应用的异步引擎，同进程里跑会把事件循环和连接池搅在一起。
    """
    db_path = tmp_path_factory.mktemp("schema-agreement") / "migrated.db"
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
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
    assert db_path.exists(), "alembic rc=0 但库文件不在（这条历史从没被真跑过）"
    return db_path


def _reflected(db_path: Path) -> dict[str, dict]:
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    meta = sa.MetaData()
    meta.reflect(bind=engine, extend_existing=True)
    shape = {
        table.name: {
            "columns": {c.name: bool(c.nullable) for c in table.columns},
            "fks": {f"{fk.parent.name} -> {fk.target_fullname}" for fk in table.foreign_keys},
        }
        for table in meta.tables.values()
    }
    engine.dispose()
    return shape


def _orm_shape() -> dict[str, dict]:
    return {
        table.name: {
            "columns": {c.name: bool(c.nullable) for c in table.columns},
            "fks": {f"{fk.parent.name} -> {fk.target_fullname}" for fk in table.foreign_keys},
        }
        for table in Base.metadata.tables.values()
    }


# --------------------------------------------------------------------------- #
# A1：两条建库路径对账
# --------------------------------------------------------------------------- #


def test_both_provisioning_paths_agree(migrated_db: Path) -> None:
    migrated, orm = _reflected(migrated_db), _orm_shape()
    migrated.pop("alembic_version", None)

    assert set(migrated) == set(orm), (
        f"迁移少建 {sorted(set(orm) - set(migrated))} / ORM 多声明 {sorted(set(migrated) - set(orm))}"
    )
    for name in sorted(migrated):
        left, right = migrated[name]["columns"], orm[name]["columns"]
        assert set(left) == set(right), (
            f"{name}: 迁移缺列 {sorted(set(right) - set(left))} / ORM 缺列 {sorted(set(left) - set(right))}"
        )
        mismatched = [
            f"{name}.{column}: 迁移 nullable={left[column]} 但 ORM nullable={right[column]}"
            for column in sorted(set(left) & set(right))
            if left[column] != right[column]
        ]
        assert not mismatched, "可空性分歧（迁移动出来的库允许 NULL，而代码声明非空）：" + "; ".join(mismatched)
        assert migrated[name]["fks"] == orm[name]["fks"], (
            f"{name} 的外键集合分歧：迁移={sorted(migrated[name]['fks'])} ORM={sorted(orm[name]['fks'])}"
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
