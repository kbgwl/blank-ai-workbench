from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer

from blank_api.limits import DEFAULT_MAX_STEPS, HARD_MAX_STEPS
from blank_api.services.plan import plan_progress

# 产品只做两个场景：编程（dev）与日常（daily）。
#
# 旧的 docs / brainstorm / reverse 仍留在 ``scenarios.py`` 的 SCENARIOS 里，
# 只为了让**已经建成**的那些任务按原场景跑完——它们的历史事件、工具白名单与
# 记忆权限都挂在那三个 key 上，改 key 等于改写事实。但新建与重新运行一律不收：
# 入口只有两个，界面才不会退化成一张功能菜单。
ScenarioKey = Literal["", "dev", "daily"]


class AttachmentIn(BaseModel):
    """一条附件引用。文件本体已经通过 /projects/{id}/uploads 上传过了。"""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""
    # 验收条件：接受 ["服务器能起来"]，也接受
    # [{"criterion": "...", "milestone_id": "...", "step_ids": ["..."]}]。
    # 后者能把验收点指到具体步骤上，验收失败时知道是哪一步没做完。
    acceptance_criteria: list[str | dict] = Field(default_factory=list, max_length=20)
    # 结构化目标 {objective, done_when, constraints, out_of_scope}。给了就用它，
    # 没给则从 goal 原话兜出一个只有 objective 的版本。
    goal_spec: dict = Field(default_factory=dict)
    # 套用计划模板：步骤与里程碑原样带过来，状态一律重置为 pending。
    plan_template_id: str | None = None
    provider_id: str | None = None
    # 场景（scenarios.py 五选一）。决定系统提示附加段、工具白名单与记忆权限；
    # 不向用户 prompt 拼前缀。空/缺省 = 开发场景。
    scenario: ScenarioKey = ""
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=HARD_MAX_STEPS)
    # 这一轮贴进来的图片。上限 8 张：再多模型侧也塞不下，而且每张都要按图计费。
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=8)
    # v0.10.1 §2：True=后端在 create_task 里就地入队/启动，不等前端读快照后
    # 二次调 /run——窗口关了、前端崩了任务也照跑。
    auto_start: bool = False
    # v0.10.1 §4：创建时钉进任务快照的权限档位；空串 = 用设置页默认模式。
    permission_mode: Literal["", "default", "accept_edits", "plan", "bypass"] = ""
    # 仅日常场景有效（与 TaskRunIn.memory_opt_in 同口径）。
    memory_opt_in: bool = False
    # 纯聊天：这一轮没有工作目录，只直接回答，不给文件/命令工具。
    # 前端在新对话未选目录时置 True；项目本身没有 workspace 时也按纯聊天处理。
    chat_only: bool = False
    # 用户明确要求这一轮不要碰工具（只回复/不要读取/不要调用工具）。
    # 与 chat_only 的区别：可以有工作目录，只是不许调用任何文件/命令工具。
    no_tools: bool = False
    # 用户明确要求不要修改/不要执行，但可以读。工具表保留，只读闸门硬拒写操作。
    read_only: bool = False
    # 用户只要文件/目录清单：只给 list_files / glob，不读文件内容。
    list_only: bool = False


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    provider_id: str | None
    scenario: str = "dev"
    # 派发快照（v162，dispatch.py）。空 dict = 这条任务不是按快照派的。
    # 原样带出去而不是只给摘要：证据页要能回答「当时按什么权限跑的」。
    dispatch_snapshot: dict = Field(default_factory=dict)
    # UP-04：创建响应里告知沙箱**当前默认档位**实际强制了什么。
    # 只在创建时填充；其余读取路径为空 dict，运行时事实以 evidence.boundaries 为准。
    sandbox_constraints: dict = Field(default_factory=dict)
    # goal 的来历（斜杠命令展开）。原样带出去：证据页要能回答"这段提示词是怎么来的"，
    # 提交钩子的载荷也从这里读，两处必须是同一份未打码的值。
    expansion: dict = Field(default_factory=dict)
    # 所属会话（多轮对话）。独立任务为 None。
    thread_id: str | None = None
    title: str
    goal: str
    status: str
    # 既能是历史数据里的纯字符串，也能是 {id, criterion, milestone_id, step_ids}。
    # 前端两种都认（`api.ts` 里取 criterion/text/title 兜底），这里不强制统一形状——
    # 强制统一就得迁移存量行，而存量行除了形状不同没有任何问题。
    acceptance_criteria: list[str | dict]
    acceptance_results: list[dict]
    # 结构化目标与持久化计划。任务结束后仍然可读——这是"跨轮看见做到哪了"的依据。
    goal_spec: dict = Field(default_factory=dict)
    plan_steps: list[dict] = Field(default_factory=list)
    milestones: list[dict] = Field(default_factory=list)
    plan_revision: int = 0
    # 回放要看得到"当时它看到的图"。形状 {id, name, mime, size}。
    attachments: list[dict] = Field(default_factory=list)
    max_steps: int
    steps_used: int
    result_summary: str
    failure_reason: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_utc(self, value: datetime) -> str:
        # 库里存的是无时区的 UTC 值，补上偏移后前端才能算对相对时间。
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.isoformat()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def plan_progress(self) -> dict:
        """进度汇总。前端直接画"完成 3/7"，不必自己数，也不会各算一套。"""
        return plan_progress(self.plan_steps or [], self.milestones or [])


class TaskRunIn(BaseModel):
    provider_id: str | None = None
    # 场景与记忆开关：空串 = 沿用任务创建时的存档（审批恢复不重发也不能被改掉）。
    scenario: ScenarioKey = ""
    # 仅日常场景有效：True=记住习惯（开放 memory_write），False=不记住。
    # 非日常场景无论取值都不开放记忆——日常习惯不污染其他场景。
    memory_opt_in: bool = False
    approve_high_risk: bool = False
    approved_tools: list[str] = Field(default_factory=list)
    approved_actions: list[str] = Field(default_factory=list, max_length=50)
    # 命令前缀放行（对标 Codex 的 approved_command_prefixes）："git status"、"npm run" 这类签名。
    approved_command_prefixes: list[str] = Field(default_factory=list, max_length=50)
    # 四档权限模式（对标 Claude Code 的 defaultMode）；空串表示沿用该任务已有的模式。
    permission_mode: Literal["", "default", "accept_edits", "plan", "bypass"] = ""
    # 三档沙箱档位；空串表示用设置页的默认档。档位只决定"跑在哪"，审批模式决定"问不问"。
    sandbox_tier: Literal["", "off", "workspace", "workspace-net"] = ""
    fallback_provider_ids: list[str] = Field(default_factory=list, max_length=5)
    role_provider_ids: dict[str, str] = Field(default_factory=dict, max_length=8)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    run_id: str = ""
    origin_run_id: str = ""
    step_id: str = ""
    parent_event_id: str = ""
    node: str
    type: str
    payload: dict


class TaskApprovalIn(BaseModel):
    approve: bool
    provider_id: str | None = None
    # v0.10.1 §3.3：审批身份。带上这三样后端就按 identity 精确匹配——不匹配
    # 返回 409「审批对象已变化」，而不是一句含混的「没有同意」。留空表示老客户端，
    # 行为与从前一致（按任务当前等待的那张卡处理）。
    approval_id: str = Field(default="", max_length=64)
    tool_call_id: str = Field(default="", max_length=128)
    arguments_hash: str = Field(default="", max_length=128)
    # 四元匹配的第四元：带上就一起校验；老客户端不带时不改变既有行为。
    tool_name: str = Field(default="", max_length=64)
    # 批准作用域：仅本次 / 本会话该命令前缀 / 本会话这类动作 / 本会话该工具。
    scope: Literal["once", "prefix", "session", "tool"] = "once"
    # 驳回时"打回到第几步"（计划步骤 id）。空串表示普通驳回——只否掉这次调用，
    # 计划不动。带上就表示整段退回重做：该步及其之后全部重开成 pending。
    send_back_step_id: str = Field(default="", max_length=64)
    # 驳回时用户写的那句话（可选）。会被原样带进工具回执交给模型——
    # "被驳回了"没有信息量，模型只能原样重试或换等价写法；"先把测试方案补上"才有用。
    feedback: str = Field(default="", max_length=600)


class AnswerAnnotation(BaseModel):
    """逐题备注（对齐 Claude AskUserQuestion 的 annotations[问题]。{preview, notes}）。

    preview 是用户看到并选中那一段的展示内容，不进模型上下文；notes 是用户自己
    写的补充，会渲染给模型。两者都由前端原样回传，长度在服务端再截一次。
    """

    preview: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=4000)


class TaskAnswerIn(BaseModel):
    """回答 Agent 的提问：按问题 id / 问题文本回填，或直接给一个自由文本答案。

    ``answers`` 的值可以是字符串（单选）或字符串数组（多选）。Claude 的回执是
    逗号串，HTTP 客户端更自然的是数组；两种都收，落账前统一成同一形状。
    """

    answers: dict[str, str | list[str]] = Field(default_factory=dict, max_length=8)
    answer: str = Field(default="", max_length=4000)
    # 跨题的「补充」一句话（Claude 的 response）与逐题注解。
    response: str = Field(default="", max_length=4000)
    annotations: dict[str, AnswerAnnotation] = Field(default_factory=dict, max_length=8)
    provider_id: str | None = None


class TaskForkIn(BaseModel):
    """从某个检查点分叉出新线程：trunk_reuse 带上状态继续，trunk_new 只留血缘。"""

    from_thread_id: str = Field(default="", max_length=120)
    strategy: Literal["trunk_reuse", "trunk_new"] = "trunk_reuse"


class TaskSteerIn(BaseModel):
    """运行中插话：不打断当前步骤，从下一次模型调用开始生效。"""

    message: str = Field(min_length=1, max_length=4000)


class PlanStepIn(BaseModel):
    """手工改一步。只给要改的字段——整份替换会把 id 换掉，引用随之失效。"""

    id: str = ""
    step: str | None = None
    status: Literal["pending", "in_progress", "completed", "cancelled", "failed"] | None = None
    milestone_id: str | None = None
    evidence: str | None = None


class PlanUpdateIn(BaseModel):
    """改计划。两种改法互斥：

    - 给 ``steps``：整份替换（沿用同 id 的步骤会保住状态与引用）；
    - 给 ``patch``：按 id 逐条改，适合"把这个勾上"这种用户动作。
    """

    steps: list[PlanStepIn] | None = None
    milestones: list[dict] | None = None
    patch: list[PlanStepIn] = Field(default_factory=list)
    explanation: str = ""


class GoalUpdateIn(BaseModel):
    """改目标。四个字段都是可选覆盖——只改约束时不用重述整个目标。"""

    objective: str | None = None
    done_when: list[str] | None = None
    constraints: list[str] | None = None
    out_of_scope: list[str] | None = None


class PlanProgressOut(BaseModel):
    total: int
    completed: int
    in_progress: int
    pending: int
    percent: int
    milestones: list[dict] = Field(default_factory=list)


class TaskPlanOut(BaseModel):
    """一份任务的计划全貌：目标 + 里程碑 + 步骤 + 进度。"""

    task_id: str
    goal: str
    goal_spec: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list)
    milestones: list[dict] = Field(default_factory=list)
    progress: PlanProgressOut
    revision: int = 0


class TaskClosureIn(BaseModel):
    """验收决定。revision 由服务端按当前工作区核对，不接受调用方自报。

    验收不是"模型说完成"，所以必须有 decision 与 decided_by 两样；两者缺一都返回
    校验错误，而不是默默记成通过。
    """

    decision: Literal["accepted", "rejected"]
    decided_by: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
