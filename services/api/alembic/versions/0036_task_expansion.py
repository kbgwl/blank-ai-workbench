"""任务记下 goal 的来历（斜杠命令展开，对标上游 UserPromptExpansion）

Revision ID: 0036_task_expansion
Revises: 0035_session_goal_skill_meta

## 为什么改

`UserPromptExpansion` 钩子的载荷是 `expansion_type / command_name / command_args /
command_source / prompt` 五个键，钩子要读的正是 `command_args` 里的参数原文。
展开那一刻的事实原本只落在 `command.expanded` 审计事件里，而事件落库前经过
`mask_deep`——参数里的路径会被打码，钩子拿到的就不是用户真写的那份。

## 改了什么

``tasks`` 加一列 ``expansion``（JSON，服务端默认 ``'{}'``），存未打码的整份展开事实。
空对象 = 这条 goal 不是斜杠命令展开来的，提交钩子里的 ``user_prompt_expansion``
一个都不触发，行为与从前完全一致。

## 兼容

带存在性检查（与 0028/0029/0030/0031 同口径）：列已在时跳过。
"""
from alembic import op
import sqlalchemy as sa

revision = "0036_task_expansion"
down_revision = "0035_session_goal_skill_meta"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "expansion" in _columns("tasks"):
        return
    op.add_column(
        "tasks",
        # MySQL 8.4 不接受给 JSON 列写字面量默认值，必须用表达式默认值；
        # 与 0028/0029/0031 的 text("('[]')") 同一写法。
        sa.Column("expansion", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
    )


def downgrade() -> None:
    if "expansion" in _columns("tasks"):
        op.drop_column("tasks", "expansion")
