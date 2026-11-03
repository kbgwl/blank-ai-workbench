# WorkBuddy v162：Super Agent 升级版（目标版本 0.9.0）

工作目录：`E:\projects\kbgai\kbgai`

目标：把空白格从“用户发一句话，Agent 执行一次”升级为低打扰、可持续工作的 Super Agent 操作台。参考图片中体现的 Router/Worker、事件触发、后台执行和 Watchdog 能力，但不复制页面，也不引入复杂项目管理界面。

## 0. 版本策略

目标发布版本：`0.9.0`。当前 `0.8.17` 仍是基线，不能在质量门槛未通过前提前改版本号。

只有下列条件全部通过后，才修改 `desktop/package.json`、根 `package.json` 和 README 版本号：

- v161 遗留 Benchmark 问题全部修复。
- Super Agent P0 功能有单元、集成和真实端到端证据。
- npm 干净安装、CLI 启动和 Desktop 启动通过。
- 工作区干净，报告和提交记录一致。

## 1. 产品目标

```text
用户输入目标 → Router 判断意图/场景/风险 → Worker 领取并执行
             → 事件、检查点、Watchdog 持续运行
             → 只有审批、失败、需要决策或最终交付时打扰用户
```

产品原则：

- 页面仍是单一 Codex 风格操作台，不新增复杂项目管理首页和重复导航。
- 默认只显示“正在做什么、做了什么、结果是什么、需要我决定什么”。
- 不展示模型内部推理，不把原始 JSON、长计划和无用过程塞给用户。
- Worker 可以真实读取文件、修改文件、运行测试；高风险操作继续经过权限审批。
- 所有后台行为可暂停、恢复、取消、重试，并能从检查点继续。
- “已完成”必须有工具结果、测试结果、Diff 或结构化证据。

## 2. v0.9.0 范围

### P0 必须进入版本

1. Router / Worker 双层运行时。
2. 事件触发和后台任务队列。
3. Watchdog、租约、心跳和断点恢复。
4. 低打扰通知策略。
5. 任务优先级、超时、步数和成本预算。
6. 统一任务状态、检查点和可观测性。
7. v161 遗留质量问题修复。
8. CLI、Desktop、网页启动链路稳定。

### P1 可选开启

- 本地定时器、文件变化、Webhook 等 Connector 抽象。
- 用户自定义 Trigger。
- 主动执行时间段和免打扰时间段。
- 失败后的有限次数自动恢复。
- 任务成本、耗时和模型用量摘要。

### 不进入 0.9.0

- 无限多 Agent 自由协商。
- 默认自动部署生产环境、发邮件或删除大量文件。
- 复杂拖拽式 Workflow 编辑器。
- 默认长期保存所有用户内容。

## 3. Router 设计

Router 只做决策，不直接执行文件和 Shell：

- 识别 `answer / execute / brainstorm / schedule / watch / ask_user`。
- 选择 `dev / docs / daily / brainstorm / reverse` 场景。
- 选择模型策略、工具白名单和通知策略。
- 判断立即执行、排队、等待事件还是请求用户确认。
- 生成短计划、风险摘要和不可变执行快照。

内部协议至少包括：

```json
{
  "intent": "execute|answer|brainstorm|schedule|watch|ask_user",
  "scenario": "dev|docs|daily|brainstorm|reverse",
  "priority": "low|normal|high|urgent",
  "model_policy": "default|fast|quality|local|user_selected",
  "tool_policy": "read_only|workspace_edit|shell_with_approval|external_connectors",
  "notify_policy": "silent|milestone|approval_only|always",
  "memory_policy": "off|read_only|opt_in_write",
  "steps": [],
  "risk_summary": "",
  "needs_user_decision": false
}
```

约束：

- Router JSON 不能直接展示给用户。
- 缺字段使用安全默认值，不能默认放开工具权限。
- 只有 daily 允许“记住习惯/不记住”写入选择；其他场景只能 off 或 read_only。
- urgent 不能绕过高风险审批。

验收：开发任务进入 Worker；普通问答不创建无意义后台任务；文档默认只读；头脑风暴不自动修改工作区；逆向实验室使用隔离工作区和专用工具；Router 失败进入可解释状态。

## 4. Worker 设计

Worker 是唯一执行者：

- 从数据库队列领取任务，校验执行快照、工作区和权限。
- 按步骤调用文件、Shell、测试、模型和连接器工具。
- 每个工具完成后写事件和检查点，写入结构化交付证据。
- 心跳续租，异常退出后允许另一个 Worker 接管。
- 对同一检查点幂等，恢复时不重复执行已经完成的工具。

统一状态：

```text
queued → routing → planned → running → waiting_approval
                         ↓          ↓
                    needs_user   paused
                         ↓          ↓
                    succeeded / failed / cancelled
```

每个检查点保存 task/run/worker、当前和下一步骤、工具调用 ID、结果指纹、Diff 指纹、模型和 token 用量、审批状态及恢复输入快照。

恢复规则：已完成工具结果指纹一致时不重跑；高风险动作恢复时重新确认审批；租约抢占必须互斥；超过恢复次数进入 failed 并说明原因。

## 5. 事件触发和 Watchdog

统一事件字段：`event_id`、`event_type`、`source`、`payload`、`dedupe_key`、`created_at`、`expires_at`。

P0 触发器：用户创建任务、任务失败、审批超时、Worker 租约过期、工作区文件变更、显式开启的本地定时器。邮件、日历、IM 先只定义接口，未完成授权和真实测试时不能声称支持。

规则：

- 相同 `dedupe_key` 在窗口内只创建一个任务。
- 后台默认 `milestone` 或 `approval_only`，不连续刷屏。
- 只有需要审批、无法恢复的失败、完成交付物或用户要求实时汇报时主动打扰。
- 每个任务必须有最大步数、最大时长、最大恢复次数和成本预算。

Watchdog 监控心跳、无事件时间、重复失败、未审批高风险调用、预算超限和缺少交付证据。动作依次为记录诊断、一次通知、暂停、释放租约恢复、终止并要求重试。Watchdog 不得提高权限、扩大工作区或切换未授权模型。

## 6. 低打扰界面

保留：顶部场景/模型/服务状态；左侧新对话、逆向实验室、设置、最近对话；中央目标、当前动作、结果；底部输入框；详细过程折叠在“活动”中。

固定交付模板：

```text
完成：一句话说明结果
改动：文件和 Diff 数量
验证：测试命令与退出码
需要你：没有则隐藏；有则只显示一个明确动作
```

禁止空泛表扬、重复复述、展示完整内部计划、无证据声称验证通过、直接展示 Router JSON 或模型错误协议。

## 7. v161 遗留问题并入本版本 P0

1. `coding-008`：校验节点使用工作区解释器，不能因系统 Python 缺少 pytest 将真实通过任务判失败。
2. `coding-039`：交付前清理临时验证脚本，`misEditedFiles=0`。
3. `coding-042`：测试退出码非 0 时不得输出“已验证通过”。
4. `coding-014`：断连正确归类并按策略重试，不能吞成 setup failure。
5. 日常纯问答：解析 `intent=answer` 的 answer 字段；为空时给简洁可读结果，不能展示原始 JSON。
6. 在 CI 或独立 Windows 环境完成真实 npm clean-prefix 安装；pnpm 结果不能替代 npm 证据。
7. 补充真实审批卡片的 UI 点击测试，不能只用后端接口放行替代。

## 8. 分阶段计划和验收

### Phase 0：契约和基线（1–2 天）

- 冻结 v0.8.17 行为。
- 增加 Router、Worker、Trigger、Watchdog 状态机和协议测试。
- 增加 v0.9.0 feature flag，默认关闭后台主动执行。

验收：旧回归通过；新协议有单测；关闭 flag 时旧行为不变。

### Phase 1：Router/Worker（3–5 天）

- Router 生成执行快照。
- Worker 只执行快照，不重新解释用户权限。
- 接入现有 queue、lease、heartbeat、evidence。
- 工具调用、检查点和交付证据统一落库。

验收：真实读取、修改、测试；杀掉进程可恢复；已完成工具不重复执行。

### Phase 2：事件和 Watchdog（3–5 天）

- 实现 file_changed、task_failed、schedule、approval_expired。
- 增加去重、过期、预算和最大恢复次数。
- Watchdog 发现卡死后暂停、释放租约并恢复。

验收：重复事件只生成一个任务；Worker 终止后可恢复；超预算任务停止并说明原因。

### Phase 3：低打扰 UI（2–4 天）

- 保持单页操作台，不增加重复页面。
- 增加任务状态、活动折叠、后台任务和通知策略。
- daily 显示记住/不记住，其他场景不显示。
- 模型和场景切换必须改变真实后端请求。

验收：普通回复使用固定短模板；无审批/失败/交付不弹通知；状态全部来自后端事件。

### Phase 4：真实闭环（3–5 天）

- 修复第 7 节的全部问题。
- 单次完整跑完 55 项 Benchmark，不合并不同运行结果。
- 完成 npm clean-prefix 安装、CLI、health 和真实 Electron GUI。

验收阈值：

```text
completionRate >= 90%
passRate >= 90%
testPassRate = 100%
misEditedFiles = 0
falseDoneCount = 0
unauthorizedHighRisk = 0
totalTokens > 0
```

### Phase 5：0.9.0 发布候选（1–2 天）

- feature flag 真实环境测试。
- 更新安装包、CLI、README 和 CHANGELOG。
- 通过 `pnpm check:versions`、完整回归、npm 安装和 Desktop 包校验。
- 生成最终报告，修改版本号为 0.9.0，提交并打 tag。

## 9. 测试矩阵

单元：Router 安全默认值、状态机、事件去重、检查点幂等、Watchdog、通知策略、answer JSON 解析。

集成：API 入队/领取/心跳/完成、Worker 恢复、Trigger→Router→Worker、usage 记录、daily opt-in 记忆。

端到端：新对话→开发任务→文件修改→测试→Diff→简洁交付；关闭界面后恢复；高风险审批；真实模型切换；npm 安装后脱离仓库运行。

## 10. 禁止的验收作弊

- 不得只修改 JSON 报告数字。
- 不得把 5 个任务当成 55 个。
- 不得删除失败样例或放宽判分器。
- 不得用报告自述替代 GUI、模型、文件和测试证据。
- 不得用后端接口放行冒充真实审批点击。
- 不得把 provider 429、环境断连和 Agent 能力失败混在一个指标里。
- 功能未通过前不得把版本号改为 0.9.0 并声称已发布。

## 11. 交付报告

必须提交修改文件归属、架构说明、单元/集成/端到端结果、55 项原始结果及逐例失败原因、npm 安装和 health 证据、Desktop GUI 证据、场景/模型/文件/测试/记忆证据、token 分布、未完成项、`git status --short`、`git diff --check` 和提交哈希。

只有本文件 Phase 5 全部通过后，才允许写“0.9.0 可以发布”。
