"""账本不跟配置一起消失：SQLite 与 MySQL/PostgreSQL 在 `model_usage.provider_id` 上的分歧。

`ModelUsage.provider_id` 声明的是 `ForeignKey(..., ondelete="CASCADE")`，0003 建表时也是这么写的，
活库 DDL 里那句 `FOREIGN KEY(provider_id) REFERENCES model_providers (id) ON DELETE CASCADE`
是实测读出来的。它今天不咬人，只因为 SQLite 的连接层从不打开 `PRAGMA foreign_keys`。
把同一个声明放到会强制外键的方言上，四条实物结果（`E:/tmp/m48/fk48_probe.py`，真 MySQL 8.4）是：

    F1 CASCADE + 强制外键：删掉供应商 → 账本实测 0 行 / 0 tokens（原本 3 行 / 600）
    F2 RESTRICT + 强制外键：删供应商被服务端拒（1451），账本 3 行 / 600 tokens 原样留着
    F3 今天的 SQLite：删供应商成功，留下 3 行指向已经不存在的 provider_id
    F4 一旦把 `foreign_keys` 打开而 CASCADE 还在：当场清账（同 F1）

也就是说"配置行被删 = 钱从账上消失"这句话写在 schema 里，只是我们这边没人执行它。
读模型这边同样有两条口径：`services/agent/evidence.py:197` 用的是 outerjoin，注释写明了契约
（"供应商行被删或指向空 provider_id 的用量记录，钱是真花出去了。用内连接会让这些行从求和里静默消失"），
而 `api/routes/observability.py:39` 还在用内连接——所以 F3 那条路径在今天的 SQLite 上就已经漏算钱。

下面每条判定都能失败，期望值全部来自接口返回、迁移后的 DDL 或表里的行。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import CreateTable
from sqlalchemy.dialects import mysql, postgresql, sqlite

from tests.test_agent_v04 import API, create_provider, env  # noqa: F401

from blank_api.db import Base, dispose_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage

CWD = Path(__file__).resolve().parents[1]

# 一笔真钱：prompt+completion，够算合计，也够和"消失"区分开
ROWS = (("u-1", 110, 20, "thread-a"), ("u-2", 220, 40, "thread-b"))
TOTAL_TOKENS = sum(prompt + completion for _, prompt, completion, _ in ROWS)
TOTAL_CALLS = len(ROWS)
# #46 在真实库里量到的最长 run_id 形状，用来证明重建表不会把值改短或改空
ATTEMPT_KEY = f"task:{uuid.uuid4()}:attempt:1"


async def _seed(provider_id: str, rows=ROWS, model: str = "mock-coder") -> None:
    async with get_session_factory()() as session:
        for index, (row_id, prompt, completion, trace_id) in enumerate(rows):
            session.add(
                ModelUsage(
                    id=row_id,
                    provider_id=provider_id,
                    task_id=f"task-{index}",
                    trace_id=trace_id,
                    node="execute_action",
                    model=model,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    latency_ms=10 + index,
                    run_id=ATTEMPT_KEY if index == 0 else "",
                )
            )
        await session.commit()


async def _summary(env) -> dict:  # noqa: F811
    resp = await env.client.get(f"{API}/observability/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _spent(summary: dict) -> int:
    """接口念出来的钱：所有模型桶的 prompt+completion 之和。"""
    return sum(
        bucket["prompt_tokens"] + bucket["completion_tokens"] for bucket in summary["models"].values()
    )


async def _usage(env) -> dict:  # noqa: F811
    resp = await env.client.get(f"{API}/usage/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _usage_tokens(summary: dict) -> int:
    """总览页的算法：把每个供应商桶的 token 相加。"""
    return sum(row["prompt_tokens"] + row["completion_tokens"] for row in summary["providers"])


@pytest.fixture(autouse=True)
async def _release_engine():
    yield
    await dispose_engine()


# --------------------------------------------------------------------------- #
# 读模型：供应商这一行没了，钱还得在合计里
# --------------------------------------------------------------------------- #


async def test_deleting_a_provider_must_not_delete_the_money(env):  # noqa: F811
    """删供应商是配置动作，不是销账动作：账上的笔数与 token 合计一格不许少。

    今天的 `observability.py` 用内连接，F3 那条路径就让这 2 笔 390 tokens 从
    `model_calls`/`roles`/`models`/`traces` 里整体消失。
    """
    provider_id = await create_provider(env)
    await _seed(provider_id)

    before = await _summary(env)
    assert before["model_calls"] == TOTAL_CALLS, json.dumps(before["models"], ensure_ascii=False)
    assert _spent(before) == TOTAL_TOKENS
    assert before["traces"] == len({trace for *_, trace in ROWS})

    deleted = await env.client.delete(f"{API}/providers/{provider_id}")
    assert deleted.status_code == 204, deleted.text

    after = await _summary(env)
    assert after["model_calls"] == TOTAL_CALLS, json.dumps(
        {"before": before["model_calls"], "after": after["model_calls"]}, ensure_ascii=False
    )
    assert _spent(after) == TOTAL_TOKENS, json.dumps(
        {"before": _spent(before), "after": _spent(after)}, ensure_ascii=False
    )
    assert after["traces"] == before["traces"], "trace 数也会随内连接一起少"
    coder = after["roles"]["coder"]
    assert coder["calls"] == TOTAL_CALLS and coder["prompt_tokens"] == 330, coder
    # 装置自检：合计必须由桶加起来得出，不然"钱没少"是假的
    assert sum(bucket["calls"] for bucket in after["models"].values()) == after["model_calls"]
    assert sum(
        bucket["prompt_tokens"] + bucket["completion_tokens"] for bucket in after["models"].values()
    ) == _spent(after)


async def test_the_orphan_money_is_not_relabelled_as_a_live_vendor(env):  # noqa: F811
    """钱挂在谁名下可以念不出，但不许念成 `None`，更不许挂到别家供应商身上。

    直接 outerjoin 的话桶键会变成 `"None/mock-coder"`：既读不出人话，又让
    `estimate_cost(model_key.split("/", 1)[1])` 之外的口径把这一桶当成一家供应商。
    """
    other_id = await create_provider(env)
    other_name = (await env.client.get(f"{API}/providers/{other_id}")).json()["name"]
    gone_id = await create_provider(env)
    gone_name = (await env.client.get(f"{API}/providers/{gone_id}")).json()["name"]
    await _seed(gone_id)

    before = await _summary(env)
    before_cost = before["models"][f"{gone_name}/mock-coder"]["cost"]
    assert (await env.client.delete(f"{API}/providers/{gone_id}")).status_code == 204

    after = await _summary(env)
    keys = list(after["models"])
    assert not any(key.startswith("None/") for key in keys), keys
    assert all(key.split("/", 1)[0] != gone_name for key in keys), f"供应商已经删了，桶不许再用它的名字：{keys}"
    assert all(key.split("/", 1)[0] != other_name for key in keys), f"孤儿不许算进活着的 {other_name}：{keys}"
    orphan = [key for key in keys if key.split("/", 1)[0] not in {other_name}]
    assert len(orphan) == 1, keys
    bucket = after["models"][orphan[0]]
    assert bucket["calls"] == TOTAL_CALLS and _spent(after) == TOTAL_TOKENS
    assert bucket["cost"] == before_cost, "成本是按模型算的，供应商行消失不该改价"


async def test_a_row_pointing_at_a_provider_that_never_existed_is_still_money(env):  # noqa: F811
    """`provider_id` 悬空是这条链路上的正常状态（删过、或老库里落下的），不是脏数据可以不看。"""
    await create_provider(env)
    await _seed("provider-never-here")

    summary = await _summary(env)
    assert summary["model_calls"] == TOTAL_CALLS, json.dumps(
        {"models": summary["models"]}, ensure_ascii=False
    )
    assert _spent(summary) == TOTAL_TOKENS


async def test_the_usage_page_total_survives_the_deleted_provider(env):  # noqa: F811
    """成本页与总览页读 `/usage/summary`，那里也挂着同一条内连接——漏算会直接念到界面上。

    `usage-panel.tsx:76` 念的是 `row.provider`，`live-panels.tsx:41` 把各桶的 token 相加当总量；
    供应商一删，这两处就会同时少算，而且界面上没有任何东西提示"这里原来有家供应商"。
    """
    provider_id = await create_provider(env)
    name = (await env.client.get(f"{API}/providers/{provider_id}")).json()["name"]
    await _seed(provider_id)

    before = await _usage(env)
    assert before["total_calls"] == TOTAL_CALLS, json.dumps(before["providers"], ensure_ascii=False)
    assert _usage_tokens(before) == TOTAL_TOKENS

    assert (await env.client.delete(f"{API}/providers/{provider_id}")).status_code == 204
    after = await _usage(env)
    assert after["total_calls"] == TOTAL_CALLS, json.dumps(
        {"删供应商前": before["total_calls"], "删供应商后": after["total_calls"]}, ensure_ascii=False
    )
    assert _usage_tokens(after) == TOTAL_TOKENS
    assert sum(item["calls"] for item in after["providers"]) == after["total_calls"], "桶与总数口径要一致"
    row = after["providers"][0]
    # React 把 null 渲染成空白，所以"念得出人话"这一条要落在类型上，不只是不等于字符串 "None"
    assert isinstance(row["provider"], str) and row["provider"] not in {"None", "", name}, json.dumps(
        row, ensure_ascii=False
    )
    assert row["model"] == "mock-coder", row
    assert after["total_cost"]["amount"] == before["total_cost"]["amount"], "价格是按模型算的，不该随供应商消失"


# --------------------------------------------------------------------------- #
# schema：账本上不许写着一条能把账抹掉的删除规则
# --------------------------------------------------------------------------- #


def _sqlite_ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=sqlite.dialect()))


def _enforce_connection(path: Path) -> sqlite3.Connection:
    """开 `PRAGMA foreign_keys` 的连接：SQLite 在这一条上就是 MySQL/PG 的替身。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "替身没上岗，后面全白测"
    return conn


def _seed_enforcing(conn: sqlite3.Connection, provider_id: str) -> int:
    conn.execute(
        "insert into model_providers (id, name, kind, base_url, api_key_encrypted, default_model,"
        " temperature, max_tokens, context_window, supports_tools, supports_vision, enabled)"
        f" values ('{provider_id}', '{provider_id}', 'openai_compatible', 'https://x', '',"
        " 'm', 0.2, 100, 1000, 0, 0, 1)"
    )
    for row_id, prompt, completion, trace_id in ROWS:
        # 列清单按编译出来的 DDL 走：ORM 那几个只有 Python 侧 default 的列（latency_ms）
        # 在建表语句里没有 DEFAULT，裸 SQL 必须自己填上
        conn.execute(
            "insert into model_usage (id, provider_id, task_id, trace_id, node, model,"
            " prompt_tokens, completion_tokens, latency_ms, status)"
            f" values ('{row_id}', '{provider_id}', 't', '{trace_id}', 'execute_action', 'm',"
            f" {prompt}, {completion}, 8, 'ok')"
        )
    conn.commit()
    return conn.execute("select count(*) from model_usage").fetchone()[0]


def _fresh_enforcing_db(tmp_path: Path) -> sqlite3.Connection:
    """按 ORM 现在的声明建一张真库，再在强制外键的连接上准备删供应商。

    同一张库里另建一张 `probe_cascade` 表带 `REFERENCES ... ON DELETE CASCADE` 作正对照：
    删供应商它必须消失。不然"model_usage 的行还在"可能只是因为外键根本没上岗。
    """
    path = tmp_path / "enforce.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        _sqlite_ddl(ModelProvider.__table__)
        + ";\n"
        + _sqlite_ddl(ModelUsage.__table__)
        + ";\ncreate table probe_cascade (id text primary key,"
        " provider_id text references model_providers(id) on delete cascade);"
    )
    raw.commit()
    raw.close()
    conn = _enforce_connection(path)
    seeded = _seed_enforcing(conn, "p-enforce")
    assert seeded == TOTAL_CALLS, "种下去的笔数不对，判定没有基数"
    conn.execute("insert into probe_cascade (id, provider_id) values ('probe', 'p-enforce')")
    conn.commit()
    return conn


def test_fresh_schema_survives_provider_deletion_under_enforced_fk(tmp_path):
    """按现在的 ORM 声明建库，在强制外键的连接上删掉供应商：钱必须还在。

    MySQL 上是 F1（实测 0 行 / 0 tokens），SQLite 打开 `foreign_keys` 就是同一条路（F4）。
    """
    conn = _fresh_enforcing_db(tmp_path)
    conn.execute("delete from model_providers where id='p-enforce'")
    conn.commit()
    left = conn.execute(
        "select count(*), coalesce(sum(prompt_tokens + completion_tokens), 0) from model_usage"
    ).fetchone()
    # 正对照：同一连接上声明了 CASCADE 的那张表必须被抹干净，否则这一轮没测到强制外键
    probe = conn.execute("select count(*) from probe_cascade").fetchone()[0]
    fk = _delete_rules(conn)
    conn.close()
    assert probe == 0, "正对照没消失：PRAGMA foreign_keys 实际没生效，下面的判定不算证据"
    assert left == (TOTAL_CALLS, TOTAL_TOKENS), json.dumps(
        {"删供应商前": (TOTAL_CALLS, TOTAL_TOKENS), "删供应商后": left, "foreign_key_list": fk},
        ensure_ascii=False,
    )


def test_no_dialect_compiles_a_rule_that_deletes_the_ledger():
    """把 ORM 的声明编译成各方言的 DDL：账本表上不许出现 ON DELETE CASCADE。

    SQLite 看不见这一条（它默认不强制），MySQL/PG 会照着执行——这正是"换库才炸"那一类。
    """
    compiled = {
        "mysql": str(CreateTable(ModelUsage.__table__).compile(dialect=mysql.dialect())),
        "postgresql": str(CreateTable(ModelUsage.__table__).compile(dialect=postgresql.dialect())),
        "sqlite": _sqlite_ddl(ModelUsage.__table__),
    }
    for dialect, ddl in compiled.items():
        assert "ON DELETE CASCADE" not in ddl.upper(), f"{dialect}：{ddl}"
    assert "provider_id" not in {
        fk.parent.name for fk in ModelUsage.__table__.foreign_keys
    }, "账本对配置不该有引用完整性：删一家供应商不许动它名下的账"


# --------------------------------------------------------------------------- #
# 已有库：迁移必须真的把这条规则摘掉，且不搬坏账本
# --------------------------------------------------------------------------- #


def _alembic(args: list[str], db_path: Path, cwd: Path = CWD) -> None:
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env_vars["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", *args],
        cwd=str(cwd),
        env=env_vars,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _rows_of(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.execute("select * from model_usage order by id")
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _indexes_of(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    # PRAGMA 不接受绑定参数，名字只能拼进语句；这些名字来自这个库自己的 index_list
    for row in conn.execute("pragma index_list('model_usage')").fetchall():
        name, origin = row[1], row[3] if len(row) > 3 else "c"
        if origin == "pk":
            continue
        columns = [info[2] for info in conn.execute(f'pragma index_info("{name}")').fetchall()]
        out[name] = columns
    return out


def _delete_rules(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """账本表上声明着的 (被引用表, ON DELETE 规则)。PRAGMA foreign_key_list 的列序：
    id, seq, table, from, to, on_update, on_delete, match。
    """
    return [
        (row[2], row[6])
        for row in conn.execute("pragma foreign_key_list('model_usage')").fetchall()
    ]


@pytest.fixture(scope="module")
def db_before(tmp_path_factory) -> Path:
    """停在 0021 的库：还没被 0022 处理过的外键原样在那儿。"""
    path = tmp_path_factory.mktemp("v28") / "before.db"
    _alembic(["upgrade", "0021_usage_origin_run_id"], path)
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into model_providers (id, name, kind, base_url, api_key_encrypted, default_model,"
        " temperature, max_tokens, context_window, supports_tools, supports_vision, enabled)"
        " values ('p-22', 'p-22', 'openai_compatible', 'https://x', '', 'm', 0.2, 100, 1000, 0, 0, 1)"
    )
    for row_id, prompt, completion, trace_id in ROWS:
        conn.execute(
            "insert into model_usage (id, provider_id, task_id, trace_id, node, model,"
            " prompt_tokens, completion_tokens, latency_ms, status, run_id, origin_run_id)"
            f" values ('{row_id}', 'p-22', 't-22', '{trace_id}', 'execute_action', 'm',"
            f" {prompt}, {completion}, 12, 'ok', '{ATTEMPT_KEY}', '{ATTEMPT_KEY}')"
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def db_at_0022(db_before, tmp_path_factory) -> Path:
    """同一份数据只走到 0022：这一格的判据是「0022 的重建没搬坏账本」。

    不能拿 head 来比：之后每加一列（0023 时间戳、0024 发起轮、0025 档位）都会让
    「逐列相等」变成一条永远修不完的红。head 与 ORM 的一致性由下面那条单独管。
    """
    path = tmp_path_factory.mktemp("v28-0022") / "at-0022.db"
    shutil.copy(db_before, path)
    _alembic(["upgrade", "0022_ledger_no_cascade"], path)
    return path


@pytest.fixture(scope="module")
def db_after(db_before, tmp_path_factory) -> Path:
    """同一份数据走完 0022 的库。"""
    path = tmp_path_factory.mktemp("v28") / "after.db"
    shutil.copy(db_before, path)
    _alembic(["upgrade", "head"], path)
    return path


def test_the_legacy_database_really_declares_the_cascade(db_before):
    """负对照：0021 的库上确实带着那条会清账的规则，后面的判定才有靶子。"""
    conn = sqlite3.connect(db_before)
    fk = _delete_rules(conn)
    ddl = conn.execute("select sql from sqlite_master where type='table' and name='model_usage'").fetchone()[0]
    conn.close()
    assert ("model_providers", "CASCADE") in fk, fk
    assert "ON DELETE CASCADE" in ddl.upper(), ddl


def test_migration_0022_leaves_the_ledger_intact(db_before, db_at_0022):
    """0022 摘掉外键，但不许搬坏账本：行、值、列宽、索引一个都不能变。

    SQLite 不能 ALTER 掉一条内联外键，只能整表重建——所以"重建后和重建前逐列相等"
    是这条迁移唯一算数的证据。对照库停在 0022，不跟着 head 走（见 fixture 说明）。
    """
    before, after = sqlite3.connect(db_before), sqlite3.connect(db_at_0022)
    rows_before, rows_after = _rows_of(before), _rows_of(after)
    assert len(rows_before) == TOTAL_CALLS, "基数没自证，比较没有意义"
    assert rows_after == rows_before, json.dumps(
        {"before": rows_before, "after": rows_after}, ensure_ascii=False
    )
    assert _delete_rules(after) == []
    ddl = after.execute(
        "select sql from sqlite_master where type='table' and name='model_usage'"
    ).fetchone()[0]
    assert "FOREIGN KEY" not in ddl.upper(), ddl

    # 索引必须原样回来：FK 没了以后 `provider_id` 仍是按供应商查账的入口
    indexes_before, indexes_after = _indexes_of(before), _indexes_of(after)
    assert len(indexes_before) >= 2, indexes_before
    assert indexes_after == indexes_before, json.dumps(
        {"before": indexes_before, "after": indexes_after}, ensure_ascii=False
    )
    assert "provider_id" in indexes_after["ix_model_usage_provider_id"]
    before.close()
    after.close()


def test_the_rebuilt_table_keeps_its_defaults(db_after, tmp_path):
    """整表重建最容易弄丢的是列默认值：新笔账不填可选列，必须照旧落进表里。"""
    path = tmp_path / "insert.db"
    shutil.copy(db_after, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into model_usage (id, provider_id, model) values ('u-new', 'p-22', 'm')"
    )
    conn.commit()
    row = dict(
        zip(
            [d[0] for d in conn.execute("select * from model_usage where id='u-new'").description],
            conn.execute("select * from model_usage where id='u-new'").fetchone(),
        )
    )
    conn.close()
    assert row["node"] == "chat", row
    assert row["status"] == "ok", row
    assert row["trace_id"] == "" and row["run_id"] == "" and row["origin_run_id"] == "", row
    assert row["prompt_tokens"] == 0 and row["cached_tokens"] == 0, row
    assert row["created_at"], "时间戳默认值没了，账就没法按时间排"


def test_the_migrated_schema_matches_the_orm(db_after):
    """迁移路径和 `create_all` 路径必须给出同一张账本表，否则新库和老库口径分家。

    逐列比 declared type 与 NOT NULL——#47 那一类"列宽只在 SQLite 上看不出来"的分歧，
    在这里就会红：老库走迁移、新库走 ORM，两边必须长得一样。
    """
    conn = sqlite3.connect(db_after)
    live = {row[1]: (row[2].upper(), row[3]) for row in conn.execute("pragma table_info('model_usage')").fetchall()}
    indexes = _indexes_of(conn)
    fk = _delete_rules(conn)
    conn.close()
    assert fk == [], fk
    orm_ddl = _sqlite_ddl(ModelUsage.__table__)
    assert "FOREIGN KEY" not in orm_ddl.upper(), orm_ddl
    assert set(live) == {c.name for c in ModelUsage.__table__.columns}, json.dumps(
        {"live": sorted(live), "orm": sorted(c.name for c in ModelUsage.__table__.columns)},
        ensure_ascii=False,
    )
    dialect = sqlite.dialect()
    for column in ModelUsage.__table__.columns:
        declared, notnull = live[column.name]
        assert declared == column.type.compile(dialect=dialect).upper(), (column.name, declared)
        assert notnull == (0 if column.nullable else 1), (column.name, notnull, column.nullable)
    assert {c.name for c in Base.metadata.tables["model_usage"].indexes} == set(indexes), (
        f"ORM 索引={sorted(c.name for c in Base.metadata.tables['model_usage'].indexes)} "
        f"库里索引={sorted(indexes)}"
    )


def test_downgrade_restores_the_rule_without_losing_rows(db_after, tmp_path):
    """往返守恒：退到 0021 要能重新带上 CASCADE（不然事故没法回滚），行数不许变。"""
    path = tmp_path / "round.db"
    shutil.copy(db_after, path)
    _alembic(["downgrade", "0021_usage_origin_run_id"], path)
    conn = sqlite3.connect(path)
    fk = _delete_rules(conn)
    rows = _rows_of(conn)
    conn.close()
    assert ("model_providers", "CASCADE") in fk, fk
    assert len(rows) == TOTAL_CALLS, json.dumps(rows, ensure_ascii=False)
