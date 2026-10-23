"""把两条建库路径的可空性对齐：12 个时间列在迁移里是"可空"，在模型里是"非空"

Revision ID: 0023_timestamps_not_null
Revsers: 0022_ledger_no_cascade

`Base.metadata.create_all` 与 `alembic upgrade head` 是同一份 schema 的两种出生方式，
今天实测（`E:/tmp/m48/mysql_schema_agreement.log` S3b，真 MySQL 8.4）两边在 12 个时间列上不一致：
迁移动出来的库允许 NULL，而代码声明的是 `Mapped[datetime]`（非空）。

    search_docs.created_at/updated_at, team_bots.created_at/updated_at,
    team_groups.created_at/updated_at, team_members.created_at,
    team_messages.created_at, team_rounds.created_at/updated_at,
    threads.created_at/updated_at

成因：0014/0016/0017 建表时只写了 `sa.Column("created_at", sa.DateTime(), server_default=sa.func.now())`，
漏掉 `nullable=False`；模型侧一直是非空。SQLite 不拦这条，ORM 插入时也从不显式写 NULL，
所以 12 列里今天一个 NULL 都没有（下面逐列数过才敢改）；但"库比代码宽松"这件事在换库那天
会变成一句谎言：任何绕过 ORM 的写入（手工修数、别的客户端）都能造出 `created_at IS NULL` 的行，
应用读出来是 `None`，然后炸在任何 `.isoformat()` 上。

先数 NULL 再改：这一列上只要还有一行 NULL 就当场抛错让整条迁移回滚，
而不是让 MySQL 用一个 1138 警告把存量数据静默变成 `0000-00-00`。
SQLite 没有 `ALTER COLUMN`，走 `batch_alter_table` 整表重建（0021 已确立的写法）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_timestamps_not_null"
down_revision: Union[str, None] = "0022_ledger_no_cascade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 实测出来的 12 列（不是"看起来像"，是 schema 对账脚本逐列比出来的）
TIMESTAMPS = (
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
)


def _null_count(table: str, column: str) -> int:
    """数这一列还有几行是 NULL。

    用 SQLAlchemy 的构造而不是手写引号：MySQL 里双引号是字符串定界符（除非开 ANSI_QUOTES），
    `SELECT ... FROM "threads"` 在真 8.4 上直接 1064（本批实测），标识符引法必须交给方言。
    """
    bind = op.get_bind()
    reflected = sa.Table(table, sa.MetaData(), autoload_with=bind)
    return bind.execute(
        sa.select(sa.func.count()).select_from(reflected).where(reflected.c[column].is_(None))
    ).scalar_one()


def _alter(table: str, column: str, nullable: bool) -> None:
    """按方言改一列的可空性。SQLite 只能整表重建，所以走 batch。"""
    kwargs = dict(
        existing_type=sa.DateTime(),
        nullable=nullable,
        existing_nullable=not nullable,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, **kwargs)
    else:
        op.alter_column(table, column, **kwargs)


def upgrade() -> None:
    for table, column in TIMESTAMPS:
        nulls = _null_count(table, column)
        if nulls:
            raise RuntimeError(
                f"{table}.{column} 还有 {nulls} 行是 NULL，收不成 NOT NULL；"
                "先给这些行补上时间（或明确删掉它们）再跑这条迁移"
            )
        _alter(table, column, nullable=False)


def downgrade() -> None:
    for table, column in TIMESTAMPS:
        _alter(table, column, nullable=True)
