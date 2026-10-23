"""账本不跟配置一起消失：摘掉 `model_usage.provider_id` 上的 ON DELETE CASCADE

Revision ID: 0022_ledger_no_cascade
Revises: 0021_usage_origin_run_id

0003 建表时给这一列写了 `ForeignKey("model_providers.id", ondelete="CASCADE")`，活库 DDL 里那句
`FOREIGN KEY(provider_id) REFERENCES model_providers (id) ON DELETE CASCADE` 是实测读出来的。
它今天不咬人，只因为 SQLite 的连接层从不打开 `PRAGMA foreign_keys`。真 MySQL 8.4 上同一份声明
（`E:/tmp/m48/fk48_probe.py`）：

    删一家供应商 → 它名下 3 笔 / 600 tokens 的账实测变成 0 笔 / 0 tokens
    换成 RESTRICT → 服务端拒这次删除（1451），账本 3 笔 / 600 tokens 原样留着

账本行是历史事实，不该跟着可删改的配置一起消失（Codex 的 `token_usage_record` 也不引用供应商配置表），
所以这里既不是改成 RESTRICT 就完事——RESTRICT 会让"删除供应商"这个正常配置动作在强制外键的库上直接失败。
这一条把引用完整性从账本上摘下来：列照旧、索引照旧（`ix_model_usage_provider_id` 是按供应商查账的入口，
外键没了它更要留着），悬空由读模型负责念成「供应商已删除」。

SQLite 不能 ALTER 掉一条内联外键，只能整表重建：列、默认值、主键从当前库反射出来照抄，
先建新表搬行、行数对得上才换名，索引按原名字原列序补回去。
MySQL/PostgreSQL 用反射拿到真实约束名再 `DROP CONSTRAINT`，不需要搬数据。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_ledger_no_cascade"
down_revision: Union[str, None] = "0021_usage_origin_run_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEDGER = "model_usage"
PARENT = "model_providers"
STAGING = "model_usage__0022_staging"
# 回滚时要重新挂上的约束名。MySQL/PostgreSQL 上这是显式命名，不是靠库自己编号。
FK_NAME = "fk_model_usage_provider"


def _server_default(column) -> Union[str, None]:
    """把反射出来的默认值还原成 DDL 文本（`''`、`'chat'`、`CURRENT_TIMESTAMP` 都从这里走）。"""
    if column.server_default is None:
        return None
    arg = getattr(column.server_default, "arg", None)
    return getattr(arg, "text", arg)


def _table_args(reflected: sa.Table, cascade: bool) -> list:
    """照抄当前表的列与主键，外键按 `cascade` 决定要不要重新声明。"""
    args: list = [
        sa.Column(
            column.name,
            column.type,
            nullable=column.nullable,
            server_default=sa.text(raw) if (raw := _server_default(column)) is not None else None,
        )
        for column in reflected.columns
    ]
    args.append(sa.PrimaryKeyConstraint(*[column.name for column in reflected.primary_key.columns]))
    if cascade:
        args.append(sa.ForeignKeyConstraint(["provider_id"], [f"{PARENT}.id"], ondelete="CASCADE"))
    return args


def _count(bind, table: str) -> int:
    return bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()


def _rebuild(bind, cascade: bool) -> None:
    """整表重建：SQLite 没有 ALTER DROP CONSTRAINT，只能建新表—搬行—换名—补索引。

    先建后删：账本表在任何一个时刻都还在库里，行数对不上就当场抛错让整条迁移回滚。
    """
    meta = sa.MetaData()
    live = sa.Table(LEDGER, meta, autoload_with=bind)
    columns = [column.name for column in live.columns]
    # 索引规格必须在看得到老表的时候抄下来：DROP 以后它们就跟着没了
    index_specs = [
        (index.name, [column.name for column in index.columns], index.unique)
        for index in sorted(live.indexes, key=lambda item: item.name)
    ]
    if cascade and PARENT not in meta.tables:
        # 声明外键要能在同一个 MetaData 里找到被引用表，否则编译 DDL 时解析不到目标列
        sa.Table(PARENT, meta, autoload_with=bind)

    staging = sa.Table(STAGING, meta, *_table_args(live, cascade))
    seeded = _count(bind, LEDGER)
    staging.create(bind=bind)
    quoted = ", ".join(f'"{name}"' for name in columns)
    bind.execute(sa.text(f'INSERT INTO "{STAGING}" ({quoted}) SELECT {quoted} FROM "{LEDGER}"'))
    moved = _count(bind, STAGING)
    if moved != seeded:
        # 静默搬空一张账本表是这条迁移最坏的失败方式
        raise RuntimeError(f"{LEDGER} 重建前后行数不等：{seeded} -> {moved}")
    bind.execute(sa.text(f'DROP TABLE "{LEDGER}"'))
    bind.execute(sa.text(f'ALTER TABLE "{STAGING}" RENAME TO "{LEDGER}"'))
    for name, index_columns, unique in index_specs:
        op.create_index(name, LEDGER, index_columns, unique=unique)


def _declared_fks(bind) -> list[dict]:
    return list(sa.inspect(bind).get_foreign_keys(LEDGER))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _rebuild(bind, cascade=False)
        return
    for foreign_key in _declared_fks(bind):
        if not foreign_key["name"]:
            raise RuntimeError(
                f"{bind.dialect.name} 上 {LEDGER} 有一条叫不出名字的外键，"
                "无法定点删除；请手工处理后再跑这条迁移"
            )
        op.drop_constraint(foreign_key["name"], LEDGER, type_="foreignkey")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _rebuild(bind, cascade=True)
        return
    op.create_foreign_key(
        FK_NAME, LEDGER, PARENT, ["provider_id"], ["id"], ondelete="CASCADE"
    )
