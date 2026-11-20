# LoopForge 源码对照报告（2026-11）

研究对象：[Tencent/LoopForge](https://github.com/Tencent/LoopForge)，`main` 分支，HEAD `d58a85ad0363`（2026-11-20）。
方法：只读 GitHub raw / API，逐文件核对；未克隆仓库。**本文所有行号对应上述 commit，未标注“推测”的条目均为源码或文档原文。**
本仓库基线：`E:/projects/kbgai/kbgai`（下称“本仓库”）。

---

## 结论摘要

1. **“打回指定步骤”本仓库已经做完一半，缺的只是前端。** 服务端链路已在**当前工作区**打通（截至本文写作时未提交：`git diff --stat` 显示 4 个文件 +78 行，另有未跟踪测试 `services/api/tests/test_rewind_gate_v483.py`）：`TaskApprovalIn.send_back_step_id` → `graph.py` 写入 `session.rewind_request` → 审批门之后 `rewind_to_step()` 把该步及之后重置为 pending、`plan_manifest.revision + 1`。前端审批回调只有 `approve/deny + scope + feedback`，没有传 `step_id`。**这是当前性价比最高的一项，约 150–250 行前端改动，零迁移；前提是这 78 行先落主干。**
2. **LoopForge 的“边”其实没有条件分支，返工靠节点上的 `rejectTo` 字段，不靠边。** `WorkflowEdge` 只有 `{id, source, target, when}`，调度器 `readyNodes()` 只检查所有 `source` 是否 `succeeded`，从不读 `when`；DAG 校验明确禁止环（`workflow must be a DAG; use rejectTo for controlled rework`）。**最值得抄的是这个“字段级回跳”而非 DAG 引擎**：不引入环也能表达返工。
3. **节点终态结构化提交（`pass / rework / fail / human`）是角色分离的前置件，且成本低。** LoopForge 每个 Agent 节点结束时必须调用 `submit_gate_result`，把“过/返工/失败/需人工”作为机器可判的终态；本仓库目前靠模型自由文本 + `update_plan` 自报状态。补这一层不需要新表，把 verdict 存进 `plan_steps[i]` / `plan_manifest` 即可。
4. **Requirement 在 LoopForge 不是独立表，而是 `issues` 行 + 聚合视图。** `Issue.Kind` 取 `requirement|issue`，`RequirementSnapshot` 由 issue + runs + tasks + bindings 现算，状态机是派生的（`not_started/active/waiting/succeeded/attention/closed`）。**本仓库若为“需求”新建一套一等实体，会与 goal/task/conversation 三套现有实体重叠；应先做 task 维度的聚合读模型，而不是先建表。**
5. **沙箱与检查点不要抄。** 它把整个工作区打成 tar.gz 存进 MySQL `MEDIUMBLOB`（单 run 一行），配合 Docker 沙箱与 `sandboxProfileId`；本仓库是“真实工作目录 + LangGraph 状态检查点”。可吸收的只有两个概念：**attempt 作用域产物**、**retry 前先恢复再重跑**。

---

## 一、SOP 的节点模型

### 1.1 机制说明

**没有“节点表”。** 一张 `workflows` 表只有 `id/name/version/revision/status/payload(JSON)`，节点、边、产物契约全部序列化在 `payload` 里（`internal/repo/models.go:224-235`）。所以“节点数据结构”只有 Go struct，没有 migration DDL。

四类节点由同一个 `WorkflowNode` 承载，用 `Type` 区分（`internal/domain/workflow.go:39-80`）：

| 类型 | 关键字段 | 语义 |
| --- | --- | --- |
| `agent` | `agentId`、`maxAttempts`、`maxReworks`、`retryStrategy`、`carryOver`、`artifactOutputs` | 引用角色定义；任务说明来自角色的 `prompt`，节点自身不写 prompt（`workflow.go:41-42` 注释） |
| `script` | `scriptLanguage(shell/python)`、`script`、`credentialEnv`、`executionScope`、`runOnWorkspaceRebuild` | 直接执行；凭据只声明环境变量名，密钥不进定义（`workflow.go:62-68`） |
| `approval` | `taskPrompt`（“需要确认什么”） | 纯人工闸门：进 `waiting_approval`；只有基线对齐类 run 会走自动通过分支（`internal/engine/node_execute.go:372-398`） |
| `sop` | `sopWorkflowId` | 子 SOP：在父工作区里起子 run，等结束后导入产物（`internal/engine/sop_node.go:18-155`） |

类型校验在 `internal/engine/workflow_graph.go:189-216`，实际接受 `agent / approval / sop / script / command`。**`docs/api/orchestration.md:25` 只写了 `agent|script|approval`，漏了 `sop`；以代码为准。**（事实）

**节点间如何传上下文与产物**（三条独立通道）：

1. **run 输入**：`{{input}}`、`{{repository}}`、`{{pipelineUrl}}`、`{{subject}}`（`internal/engine/prompt.go:34-59`）。
2. **上游文本**：`{{upstream}}` 按边顺序拼接所有直接上游的非空 `Output`；也支持精确引用 `{{node.<id>.output}}`（`prompt.go:79-86, 112-121`）。
3. **产物文件**：节点在**工作区根目录**按 `ArtifactContract{name,path,mediaType,required}` 写文件，引擎落库为 `Artifact`，可按 `{{artifact.<nodeId>.<name>}}` 注入下游（`prompt.go:87-89`）。`Artifact` 是**按 attempt 不可变快照**，旧 attempt 保留以供审计，下游消费最新成功版本（`internal/domain/run.go:236-253`）。

**节点任务书**由引擎拼装，不是模型自由发挥：`# 节点任务书` + 执行角色 + 任务说明 + 交付物 + 完成条件/门禁证据 + 失败反馈与重试策略 + `{{feedback}}`（`prompt.go:197-222`）。

**“边”的表示**：

```go
type WorkflowEdge struct {
    ID     string `json:"id"`
    Source string `json:"source"`
    Target string `json:"target"`
    When   string `json:"when"` // success
}
```

（`workflow.go:103-108`）

调度只做 DAG 拓扑 + 全部前驱成功：`readyNodes()` 把 `deps[target]` 收齐，逐个要求 `succeeded`，`When` 未被读取（`workflow_graph.go:11-32`）。建图时 Kahn 算法拒绝环（`workflow_graph.go:240-274`）。

**回跳不是边**，而是节点字段 `rejectTo`（`workflow.go:72`），校验时必须指向已存在节点，且 `onFailure=reject` 时必须配 `rejectTo`（`workflow_graph.go:175-182`）。

### 1.2 源码依据

- 节点/边/产物契约：`internal/domain/workflow.go:39-108, 146-187`
- 节点类型校验：`internal/engine/workflow_graph.go:189-216`
- DAG + 禁环：`internal/engine/workflow_graph.go:240-274`
- 就绪判定（无视 `When`）：`internal/engine/workflow_graph.go:11-32`
- 上下文占位符：`internal/engine/prompt.go:24-93, 112-121, 197-222`
- 产物 attempt 快照：`internal/domain/run.go:236-253`
- 子 SOP 节点：`internal/engine/sop_node.go:18-155`
- 存储形态：`internal/repo/models.go:224-235`

### 1.3 事实 / 推测

- 事实：四类节点、JSON 存储、`When` 无调度语义、回跳靠 `rejectTo`、产物按 attempt 不可变。
- 推测：`When` 是给前端画线/未来条件边预留的字段；我没有在任何已读调度代码里看到第二个取值（见“未找到”）。

---

## 二、人工闸门“打回指定步骤”

### 2.1 机制说明

**打回不新建 run，也不重放整条流水线，而是原地重置状态。** 同一个 `Run`，把目标节点及其全部后代改成 `pending`，目标节点 `reworkCount++`，原因写进 `Feedback`，run 回到 `running`。

`Reject()` 的核心逻辑（`internal/engine/run_ops.go:271-351`）：

1. 目标解析：请求里的 `targetId` 优先；为空时取被打回节点的 `rejectTo`（`:295-305`）。
2. 预算检查：若被打回节点声明了 `maxReworks > 0`，且目标的 `reworkCount` 已达上限，直接报 `rework limit reached`（`:309-313`）。
3. 重置范围：`descendants(wf, target) + target`（`:314-315`）；`descendants` 是沿边向后传递的闭包（`workflow_graph.go:79-94`）。
4. 每个被重置节点：`status=pending`、清 `Error/Output/ChildRunID/FinishedAt`；**只有目标** `reworkCount++` 且 `Feedback=recordHumanContext(...)`（`:319-341`）。
5. 收集被重置节点的 `ChildRunID`，异步取消仍在跑的子 SOP（`:317-321, 345`）。
6. 取消被重置范围内的在跑执行（`cancelNodeExecutions`，`:349`）；**注意 `Reject` 本身不丢容器**，容器保留到该 run 的 retry/replay 分支再决定（见 `:545-555`，那是 `retryContext` 的逻辑）。
7. 落库用 `PutRunAtRevision(r, r.Revision)` 做乐观并发（`:29`），冲突即失败，不会静默覆盖。

**失败自动打回**：节点失败时若 `allowReject && node.RejectTo != "" && 目标 reworkCount < maxReworks`，转为 `Reject(...)`；预算耗尽则**不硬失败**，而是挂到 `waiting_approval` 让人决定（`internal/engine/node_outcome.go:50-87`）。注意预算的声明方是“打回方”，计数方是“被打回方”，这是按边计预算的语义（`:60, 82`）。

> 移植警告：两条路径对“预算为 0”的解释不一致。自动路径用 `max(1, node.MaxReworks)`，即默认预算为 1（`node_outcome.go:60, 82`）；手动 `Reject` 只在 `MaxReworks > 0` 时检查，即 0 视为不限（`run_ops.go:310-311`）。抄逻辑时必须先统一，否则“审批打回”可以无限循环而“失败打回”只允许一次。

**Agent 主动返工**：Agent 节点必须调用 `submit_gate_result`，`decision=rework` 时映射为 `reject` 路径；若节点没配 `rejectTo`，降级为 `fail` 并给出明确错误（`internal/engine/gate_submit.go:86-105`）。

**幂等/防重复副作用**（五道）：

| 机制 | 作用 | 依据 |
| --- | --- | --- |
| `Artifact` 按 attempt 不可变 | 旧 attempt 产物保留但下游只取最新成功版本 | `domain/run.go:236-253` |
| `generation + attempt` 写栅栏 | 迟到结果不能覆盖已重置的节点，也不能把同一节点调度两次 | `engine/node_outcome.go:37-48` |
| `ChildRunID` 重接 | 父节点恢复时优先接回原 child run，不重复启动子 SOP | `engine/sop_node.go:46-61` |
| run revision CAS | 控制决策与并发执行不会互相覆盖 | `engine/run_ops.go:14-45` |
| 恢复前先还原工作区 | 工作区已被清理时，先从 checkpoint 恢复再重跑 | `engine/run_ops.go:491-527` |

**人工“通过”只开放给脚本节点**：`Pass()` 要求节点是 `script`，只改状态不重新执行；Agent 节点不允许人工标记通过（`run_ops.go:186-269`，文档 `docs/api/orchestration.md:226-232`）。

### 2.2 源码依据

- `Reject` 全流程：`internal/engine/run_ops.go:271-351`
- 失败自动打回与预算耗尽转人工：`internal/engine/node_outcome.go:50-87`
- Agent 终态映射：`internal/engine/gate_submit.go:82-105`
- 重试与整段重放丢容器：`internal/engine/run_ops.go:437-630`（尤其 `:545-555`）
- 恢复后重跑：`internal/engine/run_ops.go:491-527`
- 公共 API（`targetId`、`reason`）：`docs/api/orchestration.md:234-254`

### 2.3 与本仓库对照（关键）

本仓库已经实现了“打回指定步骤”的**服务端完整链路**（事实；注意：这是当前工作区的未提交改动，`git diff --stat` 为 4 文件 +78 行，不在 HEAD 上）：

- 请求字段：`services/api/src/blank_api/schemas/task.py:149-151`（`send_back_step_id`，空串 = 普通驳回）。
- 路由透传：`services/api/src/blank_api/api/routes/tasks.py:1873-1881`（仅非空时放进 `decision`）。
- 审批门只登记请求，不直接改计划（保证中断重放安全）：`services/api/src/blank_api/services/agent/graph.py:4682-4692`。
- 审批门之后统一执行回退：`graph.py:5225-5256`，调用 `plan.rewind_to_step()`。
- 回退语义与 LoopForge 一致：目标步及之后全部 `pending`、清 `evidence`；**不是只把那一步改回去**：`services/api/src/blank_api/services/plan.py:626-648`。
- 前端未接：审批回调签名只有 `"approve" | "deny", scope, feedback?`（`web/src/components/bot/turn.tsx:168, 262`），全 `web/src` 无 `send_back` 引用。

差异：LoopForge 由“节点配置的 `rejectTo`”决定目标，本仓库由“人在卡上选步骤”决定目标。两者可以并存，但**语义要区分**：人选的步骤 = 显式退回；模型 verdict = 默认按 `depends_on` 回退一步。

---

## 三、Requirement 作为一等实体

### 3.1 机制说明

**LoopForge 没有独立的 `requirements` 表，也没有 Requirement 状态机表。** 需求就是 `issues` 表的一行：

```go
type issueRow struct {
    ID, ProjectID string
    Number, Title string
    Status string   // backlog, todo, in_progress, in_review, done, closed, blocked, cancelled
    Kind   string   // requirement, issue
    WorkflowID string
    Payload []byte  // JSON: SOPBindings, SOPMode, DeliveryMRs, ConversationSessionID, ...
}
```

（`internal/repo/models.go:306-322`；`internal/domain/issue.go:9-69`）

关键关联字段（`domain/issue.go`）：

| 关联 | 字段 | 说明 |
| --- | --- | --- |
| 与 SOP | `WorkflowID`、`SOPBindings[]`、`PlaybookID`、`SOPMode` | 支持“一条需求串多条 SOP”，有 `order/dependsOn/handoff`，见 `domain/issue_sop.go:9-17` |
| 与 chat | `ConversationSessionID` | “在对话里直接改代码、没有走 SOP”时记录来源会话（`issue.go:59-61`） |
| 与 run | `LatestTaskID`，反向是 `Run.ExecutionContext.IssueID` | 一次需求可多次派发 |
| 与 PR/MR | `DeliveryMRs[]`、`DeliveryMRURL/State`、`DeliveryState` | 一个仓库一条 MR（`issue.go:45-51`） |
| 与协作空间 | `requirement_spaces(issue_id, project_id, payload)` | 只是协作容器，不是需求本体（`repo/models.go:681-690`；DDL `migrations/018_requirement_spaces.sql`，注释写明 “state layered on a collaboration room, optionally carrying one requirement issue”） |

**状态是派生的，不是存的。** `BuildRequirementSnapshot()` 每次现算：

- lane 状态来自最新 run：任一节点 `failed` → `attention`；任一节点 `waiting_approval` → `waiting`；run `running/queued` → `active`；`succeeded` → `succeeded`（`internal/repo/requirement.go:307-333`）。
- 需求总状态取所有 lane 中“最需要关注”的一个，优先级 `attention < waiting < active < succeeded < closed < not_started`；`issue.DeliveryLocked()` 直接判 `closed`（`requirement.go:566-585`）。

所以“状态机”实际有两层：**issue 的人工状态机**（backlog→todo→in_progress→in_review→done/closed，另有 blocked/cancelled 旁路）与**派生的执行态**（not_started/active/waiting/attention/succeeded/closed）。

**与 SOP run 的聚合**：一个 issue 的多个 run 按 `workflowId` 归并为 lane；子 SOP run（`ParentRunID != ""`）不单列 lane，只把 token/成本并入父 lane（`requirement.go:94-110, 137-187`）。

### 3.2 源码依据

- Issue 结构：`internal/domain/issue.go:9-69`
- issue 表：`internal/repo/models.go:306-322`
- 聚合读模型：`internal/repo/requirement.go:11-61`（入口）、`:137-219`（lane）、`:307-333`（lane 状态）、`:566-585`（总状态）
- 多 SOP 链：`internal/domain/issue_sop.go:9-125`
- 协作空间表：`internal/repo/models.go:681-690`；DDL 与注释 `migrations/018_requirement_spaces.sql`（`requirement_spaces`、`space_members`、`space_artifacts`、`space_threads`）

### 3.3 对本仓库的直接结论

本仓库没有 Requirement 实体，但有 `task`、`goal`（`services/api/src/blank_api/services/agent/goals.py:13-25`，按 conversation 维护目标）和 `project`。**缺的不是“表”，而是“任务 → 交付”的聚合读模型。** 直接照抄 LoopForge 建 `requirements` 表，会立刻面对“需求与 goal 谁是真身”的双写问题。建议先做只读聚合（task + goal + delivery + 改动文件），确认口径后再决定是否落表。

---

## 四、角色（Role）分离

### 4.1 机制说明

角色 = `agents` 表的一行，`payload` 是 `AgentDefinition`（`internal/domain/agent.go:64-97`；表 `repo/models.go:127-134`）。字段分六组：

| 组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | `id, name, version, projectId, shared, description` | 项目角色 vs 平台共享角色 |
| 提示词 | `prompt` | **单一自由文本**；旧的 `instructions` + `taskPrompt` 已在 `migrations/023_role_prompt.sql` 折叠为 `prompt` |
| 能力 | `rules[], skills[], mcpServers[], platformMcp, knowledgeBase` | 规则/技能/MCP 都是 ID 引用 |
| 执行器 | `executorId, model, effort` | `executorId` 指向 CLI（codebuddy/codex/claude-code/...），不是 provider |
| 自循环 | `loop{enabled,maxIterations,exitCondition,onExhausted,feedbackPrompt}` | 单次调用内的自纠错，不跨节点 |
| 闸门/产物 | `verification[]`、`artifactOutputs[]`、`interventions[]`、`hooks[]` | 门禁与产物契约**挂在角色上**，节点可细化（`engine/node_outcome.go:168-184` 合并契约） |

SOP 里只写 `agentId` 引用（`workflow.go:54`）；引擎解析后把“角色名 + 产物契约 + 门禁 + 重试策略”包成节点任务书。通用模板复制到项目时，会把模板角色复制进项目并**重写节点上的 `agentId`**（`docs/api/orchestration.md:74`）。

**chat 子代理与 SOP 节点共用同一份定义**（事实）：`resolveRoleSubagent()` 的注释明确“resolved for the conversation's project the way an SOP node resolves them”，并复用 `AgentDefinition` / `RuleDefinition` / `SkillDefinition`（`internal/httpapi/assistant_subagents.go:19-49`）。渲染方式不同：chat 把它渲染成 CodeBuddy 的 agent 文件（frontmatter 里的 name/description/skills + 角色 prompt + rules），并屏蔽两个 Main Bot 专属工具（`assistant_subagents.go:66-110`）；SOP 则渲染成节点任务书。chat 不挂 MCP server（`assistant_subagents.go:28-30`），这是两者的已知差异。

### 4.2 源码依据

- 角色定义：`internal/domain/agent.go:64-135`
- 角色表：`internal/repo/models.go:127-134`
- 节点引用：`internal/domain/workflow.go:54`；任务书拼装 `internal/engine/prompt.go:197-222`
- 模板复制重写 `agentId`：`docs/api/orchestration.md:74`
- chat 复用：`internal/httpapi/assistant_subagents.go:19-49, 66-100`
- prompt 折叠迁移：`migrations/023_role_prompt.sql:1-13`

### 4.3 与本仓库对照

本仓库已有 `AgentDefinition`，但它是 **Claude 子代理文件格式**（`name/description/instructions/tools/skills/model/effort/...`，`services/api/src/blank_api/services/agents_defs.py:108-144`），用于 `delegate` 派发；没有“执行器 + 规则 + 门禁 + 产物契约”这层，也没有被计划/流程节点引用。

结论：**不需要照抄六组字段**。最小可用的 Role 是四字段：`prompt + rules + skills + model`，加上一个 `executorId` 可选。先把“同一份定义同时供 delegate 与流程节点使用”打通，比补齐 `loop/interventions/verification` 更重要。

---

## 五、沙箱与检查点

### 5.1 机制说明

**保存粒度是 run，不是 node。** 表定义：

```sql
CREATE TABLE run_workspace_checkpoints (
  run_id VARCHAR(64) NOT NULL PRIMARY KEY,
  archive MEDIUMBLOB, sha256 VARCHAR(64),
  last_used_at DATETIME(3), saved_at DATETIME(3),
  follow_up BOOLEAN, error TEXT,
  lease_token VARCHAR(64), lease_until DATETIME(3)
);
```

（`migrations/013_workspace_checkpoints.sql`；主键只有 `run_id`，这就是“没有节点级检查点”的直接证据。）

内容：整个工作区打 **tar.gz**，带压缩/展开双重上限，排除 `.git` 内部对象但保留已跟踪的 `node_modules` 等（`internal/runtime/workspace_checkpoint.go:92-190, 258-330`）。恢复：先解到 staging，再原子 rename 替换，逐条校验路径逃逸，symlink 按惰性数据还原（`workspace_checkpoint.go:403-568`）。

保存时机：**空闲/回收前**，不是每个节点后。`conversationWorkspaceIdle` + `BeginWorkspaceCheckpointSave` 抢租约，满足“已释放 / 首次 / run 有更新 / 上次失败”之一才真正打包（`internal/engine/conversation_workspace.go:320-396`；打包入口 `internal/engine/workspace_checkpoint_stream.go:20-58`）。

恢复时机：retry/replay 发现 `WorkspaceCleanedAt` 非零时，先 `restoreCheckpointBeforeExecution`，再改状态、再重跑（`internal/engine/run_ops.go:491-527`；`conversation_workspace.go:398-429`）。HTTP 层把“恢复”和“重跑”拆成异步两步：`202 restoring` → 轮询 `/workspace/restore` → `ready` 后再提交重跑（`docs/api/orchestration.md:248-254`）。

**一致性靠组合，不靠快照本身**：

- 容器复用：代码只在“重置范围覆盖全部节点”（整条 SOP 重放）时显式丢容器，其余 retry 不主动重建，因此大概率继续用原容器（`run_ops.go:545-555`；这是从分支条件推断，未逐一核对 `acquireRunEnvironment` 的复用命中率）。
- 工作区被清理后：从 archive 恢复，校验 sha256，失败则 `failed` 而不是“假装 ready”（`engine/workspace_restore.go:14-43`）。
- 节点级：靠 attempt 作用域产物 + 重置后代 + 清 `ChildRunID`（见第二节）。
- 脚本节点：暂停后继续也从头执行（`node_outcome.go:193-195` 的活动文案明确写“继续后脚本将从头执行”）；Agent 节点在 `Reject` 路径不设置 `ResumeSessionID`（`run_ops.go:319-341`，据此推测为新会话），而在 `ResumePaused` 时会设置 `ResumeSessionID` 续会话（`run_ops.go:785-801`）。

沙箱本身：`sandboxProfileId` 必填；为空时系统回退到 `local`（`internal/engine/workflow_graph.go:53-57`），通用模板则在 payload 里显式写 `generic-docker`（`migrations/023_role_prompt.sql`）。一次 run 一个 `SandboxID`，历史上按节点存 sandbox 的旧数据在读取时上提到 run 级（`domain/run.go:15, 121-144`）。

### 5.2 源码依据

- 表：`migrations/013_workspace_checkpoints.sql`
- 打包/恢复：`internal/runtime/workspace_checkpoint.go:92-190, 403-568`
- 保存时机：`internal/engine/conversation_workspace.go:320-396`
- 恢复入口：`internal/engine/conversation_workspace.go:398-429`；`internal/engine/run_ops.go:491-527`
- 异步恢复 API：`docs/api/orchestration.md:248-254`
- 容器丢弃：`internal/engine/run_ops.go:545-555`
- 沙箱上提：`internal/domain/run.go:121-144`

### 5.3 与本仓库对照

本仓库的“检查点”是 **LangGraph 状态检查点**（`agent_checkpoints` + `agent_checkpoint_writes`，存 `thread_id/checkpoint_id/serde bytes`，`services/api/src/blank_api/models/checkpoint.py:9-49`；写入器 `services/api/src/blank_api/services/agent/checkpoints.py:1-54`），只保模型消息与图状态，**不保工作区文件**。工作区收尾只把 harness 自己的暂存目录外移（`services/api/src/blank_api/services/agent/workspace_state.py:85-132`），没有整目录快照。

因此：**LoopForge 的“文件级快照”与本仓库的“状态级检查点”是互补而非替代**。要抄就抄“retry 前先恢复、恢复失败必须显式 failed”这条纪律，不要抄 MySQL blob + Docker。

---

## 可移植清单（按改造成本从低到高）

| # | 项 | 本仓库要改的文件 | 估计行数 | 风险点 |
| --- | --- | --- | --- | --- |
| A | 前端“打回到第 N 步” | `web/src/components/bot/turn.tsx`（审批卡增加步骤选择 + 回调签名）、`web/src/components/bot/turns.ts`（透传）、任务详情 API 调用处（传 `send_back_step_id`） | 150–250 | ① 依赖那 78 行未提交的服务端改动先落主干；② 只能回传后端真实 `step-N`；`web/src/components/tasks/plan.ts:97-121` 对字符串步骤兜底成 `legacy-N`、对缺 id 的字典步骤兜底成 `idx-N`，这类兜底 id 回传后端必然 `rewind_index=-1`；③ 没有 `plan_steps` 时必须隐藏入口；④ 必须区分“普通驳回”与“整段退回”，否则用户会误以为只否掉一次工具调用 |
| B | 节点终态 `pass/rework/fail/human` | `services/api/src/blank_api/services/agent/graph.py`（`execute_action` 收尾要求提交 verdict、事件记录）、`services/api/src/blank_api/services/plan.py`（校验/落 `plan_steps[i].verdict`）、`services/api/src/blank_api/services/agent/planner.py`（节点默认 verdict 规则） | 300–450 | ① 不要在审批门内部改状态（重放安全），沿用 `rewind_request` 的“先登记、门后执行”；② **不要抄** LoopForge “rework 跳过产物/门禁校验”的行为（`node_execute.go:887-906`），会让模型用 verdict 短路证据；③ 与 `update_plan` 自报状态要收敛成一条路径 |
| C | `rejectTo` + 返工预算 | `planner.py`（`reject_to` 默认上一步、`max_reworks`）、`plan.py`（预算计数）、`graph.py`（失败自动回退 vs 转人工） | 200–300 | ① 与 A 的“人选步骤”语义重叠，建议：人选优先，模型只能按 `reject_to`；② 预算耗尽要转 `waiting_approval`，不能硬失败；③ 预算计在“被打回方”，声明在“打回方”，别写反 |
| D | 角色最小版（prompt/rules/skills/model） | 新 `models/agent_role.py` + 迁移；`services/agent/agents_defs.py`（适配/合并）；`services/agent/graph.py`（计划节点引用 role）；`web` 设置页 | 600–900 | ① 现有 `AgentDefinition` 是 Claude 文件格式，加字段要兼容解析；② 不要绑定到 CLI 进程（本仓库是 provider 模型）；③ 同名角色在 project/shared 两级的作用域要先定 |
| E | Requirement 聚合读模型 | 新 `services/requirements.py`（聚合查询）+ API + 任务详情 UI；若落表再加 `models/requirement.py` + 迁移 | 400（只读）/ 900–1200（落表） | ① 与 `goal`、`task`、`conversation` 三套实体语义重叠；② 先只读聚合验证口径，再决定是否落表；③ 多 run/多 SOP 链是主要复杂度 |
| F | 完整 SOP DAG 引擎（nodes/edges/revisions/模板目录） | 不建议 | 3000+ | 与本仓库“单图循环 + plan_steps”冲突；会变成两套执行引擎并行 |

排序理由：A 是**已经完成 80% 的功能**；B/C 是把现有线性计划变成“可路由计划”的最小增量；D 才触及角色分离；E 是产品口径问题不是技术问题；F 是重写。

---

## 明确不要抄的

1. **MySQL `MEDIUMBLOB` 存整包 tar.gz**（`migrations/013_workspace_checkpoints.sql`）。单 run 一行、几十 MB 起步，备份/迁移/主从延迟都会受影响；本仓库是 SQLite/PG + 真实目录，直接冲突。要存就存对象存储或本地快照目录。
2. **Docker 沙箱驱动 + `sandboxProfileId` 体系**（`domain/workflow.go:167`；通用模板统一写 `"sandboxProfileId": "generic-docker"`，见 `migrations/023_role_prompt.sql` 各 `UPDATE ... payload`）。本仓库已有 `sandbox/` 与进程级隔离，引入等于换执行底座，收益与风险不匹配。
3. **条件边 `When`**（`domain/workflow.go:107`）。字段存在但调度器不读（`workflow_graph.go:11-32`），至少说明条件边没有在调度侧落地；抄一个“有字段没语义”的设计只会增加解释成本。
4. **`Pass()`：人工把失败脚本标成成功**（`run_ops.go:186-269`）。它会让“没验证”变成“通过”，与本仓库 `delivery.py` 刻意区分「未跑校验」的诚实口径（`services/api/src/blank_api/services/agent/delivery.py:110-121`）直接冲突。
5. **gate MCP 的 stdio 子进程**（`internal/gatemcp/server.go:61-140`，一个只暴露 `submit_gate_result` 的独立二进制，把结果写到文件）。本仓库工具在进程内，多一个二进制就多一个部署面；用一个普通工具返回结构化 JSON 即可。
6. **28KB 的 SQL 数据迁移**（`migrations/023_role_prompt.sql` 全文是 `UPDATE generic_templates SET payload='...'`）。模板/角色种子应走代码 seed，不要走 SQL 字符串。
7. **多实例 lease / worker / runner / 抢占**（`engine/run_control.go`、`engine/workspace_restore.go` 的大量租约逻辑）。本仓库是 FastAPI + 任务队列（`services/api/src/blank_api/services/agent/queue.py`），这套租约是为多实例 worker 设计的；不先论证多实例部署就移植，只会增加空转状态。
8. **Group 协作 / `SOPCheckpoint` group mode**（`domain/workflow.go:124-153`）。代码自己把它标成退役概念：`HistoricalGroup is a record of the retired role groups`（`internal/domain/run.go:92-96`），没有 SOP 图可恢复或重试。

---

## 未找到 / 未核实

1. **条件边实现**：没有找到 `When != "success"` 的取值或任何按 `When` 分支的调度代码；只能确认 `readyNodes()` 不读它。判定为“预留字段/未完成”，但未穷尽 1391 个 `internal/` 文件。
2. **节点级检查点**：不存在。`run_workspace_checkpoints` 主键只有 `run_id`，保存时机是空闲/回收，不是节点边界。
3. **Requirement 独立表/状态机**：不存在。`migrations/018_requirement_spaces.sql` 建的是协作空间（`requirement_spaces` 只带一个可空 `issue_id`），不是需求实体；需求状态是 `issue.status` + `BuildRequirementSnapshot` 派生，没有独立状态机。首次 raw 拉取曾得到 0 字节（网络截断），已改用 GitHub Contents API 解码核对（1641 字节）。
4. **角色解析的优先级**：只看到调用点 `workflowAgent(st, wf, node.AgentID)`（`internal/engine/prompt.go:17`），没有拉到其定义文件；“项目角色优先于共享角色”目前只有 `docs/api/orchestration.md:74` 的复制重写逻辑作为间接证据，**未逐行核实**。
5. **`model` 如何传给具体 CLI**：`AgentDefinition.Model` 存在（`agent.go:90`），但 executor 适配层未逐行核对；不要据此认为“角色级模型”已经端到端可用。
6. **`node_execute.go` 的初次拉取失败**（0 字节），后改用 GitHub Contents API 取得（1245 行）并核对了节点分发与产物校验（`:316-398, 880-928`）；但节点内部的重试循环细节（iteration 上限、`AgentLoopPolicy` 的实际执行）未逐行读完。
