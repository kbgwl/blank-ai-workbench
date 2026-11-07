# WorkBuddy v165：v0.10.0 Autonomous Super Agent 全量迭代功能清单

目标版本：`v0.10.0 Autonomous Super Agent`

当前基线：`v0.9.1 Super Agent Completion`

核心目标：让系统从“用户点击任务后 Agent 执行”升级为真正的：

```text
持续监控聊天
→ 理解用户目标
→ 判断是否需要行动
→ 自动创建任务
→ 自动调度 Worker
→ 自动读取、修改和测试
→ 自动分析失败并修复
→ 自动交付结果
→ 持续监听后续指令和事件
```

## 1. 版本边界

### 1.1 必须实现

- Chat Supervisor 聊天监督者。
- Goal Tracker 目标跟踪器。
- Autonomous Planner 自动规划器。
- Task Dispatcher 自动任务下发器。
- Verifier 事实验证器。
- 多轮对话连续执行。
- 用户说“继续”后自动恢复未完成任务。
- 测试失败后的自动修复闭环。
- 后台任务持续运行。
- 用户关闭窗口后任务继续。
- 任务完成后自动回到对话。
- 三级自动化模式。
- 全流程安全限制和审批。
- 完整事件、状态、成本和审计记录。

### 1.2 可选实现

- 文件变化触发。
- 定时任务。
- Webhook 任务。
- 邮件、日历、IM Connector。
- 多 Worker 并行执行。
- 本地模型自动降级。

### 1.3 不在本版本默认开启

- 自动生产部署。
- 自动发送外部消息。
- 自动删除大量文件。
- 自动访问未授权目录。
- 自动复制或上传敏感数据。
- 无限 Agent 自主循环。

## 2. Chat Supervisor 聊天监督者

### 2.1 基本职责

Chat Supervisor 是 v0.10.0 的核心入口，持续监听当前对话和任务事件：

- 监听用户新消息。
- 监听用户补充要求。
- 监听“继续”“暂停”“取消”“重试”等控制词。
- 监听任务完成、失败、等待审批和预算超限事件。
- 监听文件变化和外部触发事件。
- 判断当前消息是否形成新目标。
- 判断当前消息是否应当继续旧目标。
- 判断当前消息是否只是普通问答。
- 判断当前消息是否需要创建任务。
- 判断当前消息是否需要用户确认。

### 2.2 意图类型

必须支持：

```text
answer              普通回答
inspect             读取和分析
execute             执行任务
continue            继续旧任务
retry               重试失败任务
resume              恢复暂停任务
pause               暂停任务
cancel              取消任务
approve             批准等待中的操作
reject              拒绝等待中的操作
brainstorm          头脑风暴
schedule            创建定时任务
watch               创建持续监控任务
remember            记住习惯
forget              删除或忽略习惯
ask_user            请求用户补充信息
```

### 2.3 消息判断结果

每条用户消息必须产生结构化判断：

```json
{
  "message_id": "",
  "intent": "execute",
  "goal_id": "",
  "related_task_id": "",
  "scenario": "dev",
  "automation_mode": "assist",
  "needs_execution": true,
  "needs_approval": false,
  "needs_clarification": false,
  "priority": "normal",
  "confidence": 0.94,
  "reason": "用户要求修改代码并自行测试"
}
```

### 2.4 防止误执行

以下情况不能自动执行：

- 用户只是讨论方案。
- 用户只是问“能不能做”。
- 用户没有明确目标。
- 目标涉及未授权目录。
- 任务风险无法判断。
- 消息可能是引用文本而不是用户指令。
- 上一任务已结束，当前消息没有明确关联。

这时应当：

- 直接回答。
- 简短询问一个必要问题。
- 展示“开始执行”按钮。
- 请求用户选择场景或自动化模式。

## 3. Goal Tracker 目标跟踪器

### 3.1 目标对象

每个长期目标必须有独立 `goal_id`，保存：

- 原始目标。
- 当前目标摘要。
- 所属 conversation_id。
- 所属 project_id。
- 当前场景。
- 当前自动化模式。
- 优先级。
- 当前状态。
- 已完成步骤。
- 未完成步骤。
- 失败步骤。
- 阻塞原因。
- 下一步动作。
- 相关 task_id / run_id。
- 预算和累计用量。
- 最后活动时间。

### 3.2 目标状态

```text
captured
→ clarifying
→ planned
→ executing
→ waiting_approval
→ waiting_user
→ verifying
→ repairing
→ succeeded
→ failed
→ paused
→ cancelled
→ archived
```

### 3.3 多轮连续执行

用户说以下内容时，必须能关联旧目标：

- 继续。
- 接着做。
- 修复刚才的问题。
- 再测试一下。
- 把刚才的方案改简单。
- 这个问题还没解决。
- 按刚才的要求继续。

不能因为新消息到来就丢失原目标、重新开始或引用错误任务。

### 3.4 目标冲突处理

当用户提出新目标时：

- 与旧目标同一文件、同一上下文：合并到旧目标。
- 与旧目标不同但优先级更高：暂停旧目标，创建新目标。
- 与旧目标冲突：明确提示冲突并请求选择。
- 用户说“取消刚才的”：只取消明确关联的目标。

## 4. Autonomous Planner 自动规划器

### 4.1 规划流程

```text
理解目标
→ 收集上下文
→ 识别相关文件
→ 估算风险
→ 选择工具
→ 制定最小步骤
→ 估算成本和时间
→ 生成执行计划
```

### 4.2 计划结构

```json
{
  "goal_id": "",
  "plan_id": "",
  "steps": [
    {
      "id": "step-1",
      "title": "读取入口文件",
      "kind": "inspect",
      "tools": ["list_files", "read_file"],
      "risk": "low",
      "depends_on": [],
      "success_condition": "找到入口文件"
    }
  ],
  "max_steps": 30,
  "max_seconds": 1800,
  "token_budget": 150000,
  "approval_points": [],
  "rollback_strategy": "workspace_diff"
}
```

### 4.3 动态调整

Planner 必须根据真实执行结果调整计划：

- 文件不存在：重新定位，不重复盲读。
- 测试失败：增加修复步骤。
- 发现额外依赖：暂停并请求审批。
- 发现任务目标冲突：回到 waiting_user。
- 发现风险扩大：停止当前计划并重新审批。
- 已经满足目标：提前进入验证，不继续无意义执行。

### 4.4 防止无限循环

- 相同工具签名连续失败达到上限时停止。
- 相同文件无变化重复修改达到上限时停止。
- 相同测试无新结果达到上限时停止。
- 总步骤、总时间、token 和恢复次数均有硬上限。
- 每次循环必须产生新证据，否则不能继续。

## 5. Task Dispatcher 自动任务下发

### 5.1 自动下发条件

满足以下条件才自动创建任务：

- 用户目标明确。
- 当前工作区已绑定。
- 场景已确定。
- 工具权限可确定。
- 风险可评估。
- 自动化模式允许执行。
- 没有待解决的澄清问题。

### 5.2 下发对象

自动创建并关联：

- conversation。
- goal。
- task。
- run。
- dispatch_snapshot。
- worker lease。
- notification policy。
- approval policy。

### 5.3 调度策略

支持：

- 立即执行。
- 排队执行。
- 等待审批。
- 等待用户补充。
- 定时执行。
- 文件变化后执行。
- 失败后自动恢复。

### 5.4 任务优先级

```text
urgent > high > normal > low
```

优先级不能绕过安全策略，只影响调度顺序和通知方式。

## 6. Verifier 事实验证器

### 6.1 验证对象

Verifier 必须独立于 Agent 判断：

- 目标是否完成。
- 文件是否真实修改。
- 预期文件是否修改。
- 是否产生多余文件。
- 测试是否真的执行。
- 测试退出码是否为 0。
- Diff 是否符合目标。
- 是否越权。
- 是否存在 false claim。
- 是否存在 zero-change success。
- 是否有最终交付证据。

### 6.2 验证顺序

```text
读取磁盘事实
→ 读取工具结果
→ 读取测试结果
→ 读取 Diff
→ 读取审批记录
→ 对比任务目标
→ 生成最终 verdict
```

### 6.3 失败处理

- 测试失败：进入 repairing。
- 文件未改：重新判断是否真的需要修改。
- 多余文件：清理或判定失败。
- 无测试证据：不得写验证通过。
- 目标部分完成：进入 waiting_user 或继续规划。
- 权限异常：立即终止并记录。

## 7. 自动修复闭环

### 7.1 标准循环

```text
执行步骤
→ 运行验证
→ 发现失败
→ 读取失败输出
→ 定位根因
→ 修改最小范围
→ 重新验证
→ 达标则继续
→ 超过上限则停止
```

### 7.2 允许自动修复的范围

默认允许：

- 当前工作区内的代码错误。
- 测试失败。
- 类型错误。
- 文档格式错误。
- lint 错误。
- 明确指定的 UI 问题。

必须审批：

- 安装依赖。
- 修改数据库结构。
- 删除文件。
- 网络操作。
- 修改权限。
- 访问工作区之外的路径。
- 部署和发布。

### 7.3 自动结束条件

满足任一条件结束：

- 目标完成并验证通过。
- 用户明确取消。
- 需要用户审批。
- 需要用户补充信息。
- 达到预算。
- 达到重试上限。
- 检测到权限风险。
- 检测到无限循环。

## 8. 三种自动化模式

### 8.1 手动模式

- 用户明确点击执行。
- 每个高风险步骤需要审批。
- Agent 不主动创建后台任务。

### 8.2 辅助模式（默认）

- 自动理解聊天目标。
- 自动读取文件和分析问题。
- 自动修改允许范围内文件。
- 自动运行测试。
- 高风险动作需要审批。
- 完成后自动交付。

### 8.3 自动驾驶模式

用户明确开启后允许：

- 自动创建任务。
- 自动多步骤执行。
- 自动修复测试失败。
- 自动后台运行。
- 自动恢复任务。
- 自动汇报里程碑。

仍然禁止自动：

- 大量删除。
- 生产部署。
- 外部发送消息。
- 上传敏感文件。
- 扩大工作区。
- 提升系统权限。

## 9. 聊天和任务界面升级

### 9.1 顶部状态

显示：

- 当前场景。
- 当前模型。
- 自动化模式。
- 当前目标。
- 后台任务数量。
- 服务状态。

### 9.2 对话区

显示：

- 用户消息。
- Agent 简洁回复。
- 当前动作。
- 等待审批。
- 等待用户。
- 最终交付。

不显示完整思考过程。

### 9.3 后台任务入口

显示：

- 正在运行。
- 等待审批。
- 已暂停。
- 失败待重试。
- 已完成。

点击任务后进入对应 conversation 或 task detail，不能出现孤儿任务。

### 9.4 控制按钮

至少支持：

- 暂停。
- 继续。
- 重试。
- 取消。
- 查看计划。
- 查看 Diff。
- 查看终端。
- 查看审批。
- 查看用量。

## 10. 事件和状态系统

新增或统一事件：

```text
message.observed
intent.classified
goal.created
goal.updated
plan.created
plan.revised
task.dispatched
worker.claimed
worker.heartbeat
step.started
step.completed
verification.started
verification.failed
repair.started
repair.completed
approval.required
approval.decided
task.paused
task.resumed
task.completed
task.failed
delivery.recorded
```

每个事件必须包含：

- event_id。
- task_id。
- goal_id。
- conversation_id。
- run_id。
- created_at。
- actor。
- payload 摘要。
- trace_id。

## 11. 记忆和用户习惯

### 11.1 可记忆内容

- 用户偏好的测试命令。
- 用户偏好的代码风格。
- 用户偏好的回复长度。
- 用户偏好的模型。
- 用户偏好的工作时间。
- 用户明确要求保存的工作习惯。

### 11.2 不可默认记忆

- API Key。
- 密码。
- 身份证件。
- 私密文件内容。
- 完整聊天原文。
- 未经用户允许的任务结果。

### 11.3 记忆规则

- 只有 daily + 用户明确 opt-in 才能写入习惯。
- 用户可以查看、删除和关闭记忆。
- 记忆写入必须产生事件。
- 记忆写入不能影响工具权限。

## 12. 模型路由

支持按任务自动选择：

- fast：简单问答和分类。
- quality：复杂编码和修复。
- local：隐私敏感和离线任务。
- user_selected：用户指定模型。
- fallback：主模型失败后的备用模型。

模型路由必须记录：

- 为什么选择该模型。
- provider。
- model。
- 输入和输出 token。
- 成本估计。
- fallback 原因。

模型切换不能绕过当前工具和权限快照。

## 13. 安全和审批

所有自动操作必须经过：

```text
意图判断
→ 工作区范围检查
→ 工具策略检查
→ 风险检查
→ 审批检查
→ 执行
→ 结果验证
```

高风险操作包括：

- 删除文件。
- 批量修改。
- 安装依赖。
- 数据库迁移。
- 网络访问。
- Git push。
- 部署。
- 外部消息发送。
- 访问工作区之外目录。

Super Agent 不能因为“自动驾驶模式”而跳过这些限制。

## 14. 低打扰通知

支持：

- silent。
- milestone。
- approval_only。
- always。

默认：

- 普通执行不连续通知。
- 审批必须通知。
- 无法恢复的失败必须通知。
- 完成且有交付物时通知。
- 用户明确要求时才显示实时过程。

## 15. 可观测性和运维

必须提供：

- 当前活跃目标列表。
- 当前后台任务列表。
- Worker 健康状态。
- 队列长度。
- 平均任务耗时。
- 任务失败率。
- 自动修复次数。
- 审批等待时间。
- token 和成本统计。
- provider 错误率。
- Watchdog 告警。
- 卡死任务列表。

所有后台任务都能通过 task_id、goal_id 或 conversation_id 找到。

## 16. 评测和质量门槛

新增评测类别：

- 聊天意图判断。
- 旧目标继续执行。
- 用户取消和暂停。
- 测试失败自动修复。
- 多轮上下文一致性。
- 零改动伪成功。
- false claim。
- 高风险审批。
- 关窗恢复。
- 记忆 opt-in。
- 模型 fallback。
- Trigger 去重。
- 无限循环阻断。

必须保持：

```text
completionRate >= 90%
passRate >= 90%
testPassRate = 100%
misEditedFiles = 0
falseDoneCount = 0
falseClaimCount = 0
zeroChangeSuccessCount = 0
unauthorizedHighRisk = 0
totalTokens > 0
```

新增 Super Agent 门槛：

```text
conversationGoalLinkRate = 100%
orphanTaskRate = 0%
approvalStateMismatch = 0
resumeAfterCloseRate = 100%
automaticRepairEvidenceRate = 100%
currentTurnAnswerLeak = 0
```

## 17. 分阶段实施

### Phase 1：Chat Supervisor 和 Goal Tracker

- 消息观察。
- 意图分类。
- 目标创建和关联。
- “继续/暂停/取消/重试”控制。
- 多轮目标状态。

验收：用户一句自然语言可以自动形成目标；后续“继续”能恢复原目标。

### Phase 2：自动下发和自动修复

- Planner。
- Dispatcher。
- Verifier。
- 测试失败自动修复。
- 事实交付。

验收：用户提出代码目标后，不需要手动创建任务，系统可以完成读取、修改、测试和修复。

### Phase 3：后台和持续监听

- 关闭窗口后继续。
- 重开后恢复。
- Trigger。
- Watchdog。
- 预算和免打扰。

验收：任务在后台持续工作，用户回来后可以继续交互。

### Phase 4：自动化模式和安全策略

- 手动模式。
- 辅助模式。
- 自动驾驶模式。
- 工具权限和审批矩阵。
- 风险拦截。

验收：自动驾驶可以连续完成低风险任务，但不能绕过高风险审批。

### Phase 5：真实 GUI 和发布

- 聊天监督任务完整 GUI。
- 目标和任务状态。
- 后台任务列表。
- 审批卡片。
- Diff、终端和交付。
- 18 项真实 GUI 验收。
- Benchmark 和回归。
- npm、Desktop 打包。

验收：真实用户可以只通过聊天完成一次完整开发任务。

## 18. 发布条件

只有以下全部满足才允许发布 v0.10.0：

- Chat Supervisor 真实工作。
- Goal Tracker 真实关联多轮对话。
- 用户自然语言可以自动下发任务。
- 自动修改和测试闭环成功。
- 测试失败可以自动修复或明确停止。
- 关闭窗口后任务继续且重开可见。
- 高风险操作仍需审批。
- 三种自动化模式可用。
- Super Agent 新增质量门槛全部达标。
- 真实 GUI 18 项通过。
- npm 安装和 Desktop 打包通过。
- 工作区干净。
- 正式报告、Commit 和 Tag 一致。

## 19. WorkBuddy 最终报告

必须提交：

1. Chat Supervisor 架构和消息状态图。
2. Goal Tracker 数据结构和状态转换。
3. Planner、Dispatcher、Verifier 的实现文件。
4. 自动修复闭环日志。
5. 多轮“继续/暂停/恢复/取消”证据。
6. 关窗恢复证据。
7. 模型路由和 fallback 证据。
8. 三种自动化模式证据。
9. 高风险审批证据。
10. 完整 Benchmark 和新增指标。
11. GUI 截图和 task/run/goal 事件。
12. npm 和 Desktop 安装证据。
13. 最终 Commit、Tag 和工作区状态。
14. 未完成项和风险。

只有所有证据齐全，才允许写“v0.10.0 Autonomous Super Agent 已完成”。
