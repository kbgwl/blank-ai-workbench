"""计划持久化 + 结构化目标 + 里程碑 + 可复用模板

Revision ID: 0028_plan_persistence
Revises: 0027_max_tokens_default

## 为什么改

计划之前只活在图状态（LangGraph checkpoint）里：任务一结束就随之沉底，
新任务永远从空白纸开始。带来的四个实际问题：

1. **跨轮丢失**——同一会话里第二轮问"计划做到哪了"，模型答不出来，因为它看不到。
2. **无法复用**——同类工作每次重新拆，拆完就扔。
3. **看不到进度**——只有每步 pending/in_progress/completed，没有"完成几分之几"。
4. **目标只剩一句话**——``goal`` 是用户原话，无法承载"完成标准 / 约束 / 不做什么"。

## 改了什么

``tasks`` 加四列：

| 列 | 用途 |
|---|---|
| ``goal_spec`` | 结构化目标 ``{objective, done_when, constraints, out_of_scope}``；``goal`` 继续只存原话 |
| ``plan_steps`` | 持久化步骤 ``[{id, step, status, milestone_id, evidence}]`` |
| ``milestones`` | 里程碑 ``[{id, title, status, step_ids}]`` |
| ``plan_revision`` | 计划被改过几次 |

新表 ``plan_templates``：把一次任务的计划固化成可再次套用的对象。

## 为什么带存在性检查

本仓的开发库存在**版本号与实际结构不一致**的历史情况（``alembic_version`` 停在
0017，表里却已有 0026 的列）。这种情况下跑到本迁移时列可能已经在了，
裸 ``ADD COLUMN`` 会整条链断在半路，而断在这里的后果是**启动不了**——
安装版每次启动都跑 ``upgrade head``。

所以每一步先查再改：已经在的一律跳过。MySQL 与 SQLite 的 inspector
口径一致，两种方言共用同一段逻辑。

## 兼容

四列全部带服务端默认值（``'[]'`` / ``'{}'`` / ``0``），存量行不需要回填。
``acceptance_criteria`` 的**列类型不变**——它从 ``["文本"]`` 升格成
``[{id, criterion, milestone_id, step_ids}]`` 走的是应用层归一化，
读旧数据时把字符串包一层即可，不在这里改数据。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_plan_persistence"
down_revision: Union[str, None] = "0027_max_tokens_default"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    existing = _columns("tasks")

    # MySQL 8.4 拒绝给 JSON 列写字面量默认值（错误 1101），只认表达式默认值这一种写法。
    additions = (
        ("goal_spec", sa.JSON(), sa.text("('{}')")),
        ("plan_steps", sa.JSON(), sa.text("('[]')")),
        ("milestones", sa.JSON(), sa.text("('[]')")),
        ("plan_revision", sa.Integer(), "0"),
    )
    for name, column_type, default in additions:
        if name in existing:
            continue
        op.add_column(
            "tasks",
            sa.Column(name, column_type, nullable=False, server_default=default),
        )

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("plan_templates"):
        op.create_table(
            "plan_templates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("('')")),
            sa.Column("goal_spec", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
            sa.Column("steps", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
            sa.Column("milestones", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
            sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_task_id", sa.String(36), nullable=True),
            # 和 ORM 的 `Mapped[datetime]` 必须一致地非空：两条建库路径（迁移 / create_all）
            # 形状不一致时，跑测试的库和装出来的库行为会分叉。
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("plan_templates")
    } if sa.inspect(op.get_bind()).has_table("plan_templates") else set()
    if "ix_plan_templates_project_id" not in indexes:
        op.create_index("ix_plan_templates_project_id", "plan_templates", ["project_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("plan_templates"):
        for index in inspector.get_indexes("plan_templates"):
            if index["name"] == "ix_plan_templates_project_id":
                op.drop_index("ix_plan_templates_project_id", table_name="plan_templates")
        op.drop_table("plan_templates")
    existing = _columns("tasks")
    for name in ("plan_revision", "milestones", "plan_steps", "goal_spec"):
        if name in existing:
            op.drop_column("tasks", name)
