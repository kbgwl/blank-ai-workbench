"""可复用的计划模板。

解决的问题：计划之前只活在图状态里，每开一个新任务都从"空白纸"开始——
同一类工作（发布一个版本、接一个新接口、排查一类线上问题）每次都要重新拆一遍，
且拆出来的东西丢了就找不回来。

模板把一次任务里验证过的计划固化成可再次套用的对象：

- **来源**：任何一次任务的 `plan_steps` 都能"存为模板"（`POST /plan-templates/from-task`）。
- **套用**：新建任务时选一个模板，步骤与里程碑原样带过去，状态一律重置为 pending
  （模板是"怎么做事"，不是"做到哪了"——带过来的完成态会是假进度）。
- **作用域**：挂在 project 下。跨项目套用没有意义：步骤里写的路径、命令都绑在具体仓库上。

刻意不做"模板版本链"：模板就是一份快照，改了就是新的一份。给它加版本号只会让人
以为旧版本还能回滚，而实际上步骤文本被覆盖后没人能还原当时的上下文。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base


class PlanTemplate(Base):
    __tablename__ = "plan_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("('')")
    )
    # 模板自带的结构化目标：套用时直接填进新任务的 goal_spec，省掉重新拆解。
    goal_spec: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("('{}')")
    )
    # [{id, step, status, milestone_id, evidence}]——status 在套用时会全部重置。
    steps: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("('[]')")
    )
    milestones: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("('[]')")
    )
    # 被套用过几次。用来排序（常用的浮上来），也是"这个东西到底有用没用"的唯一证据。
    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 从哪个任务总结出来的。只是溯源，任务删了不连坐（SET NULL）。
    source_task_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
