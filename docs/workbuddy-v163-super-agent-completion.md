# WorkBuddy v163：Super Agent 完整闭环升级任务书

目标版本：`v0.9.1 Super Agent Completion`

工作目录：`E:\projects\kbgai\kbgai`

## 0. 版本目标

v0.9.0 已完成代码发布、自动化回归和 Benchmark 质量门槛，但仍有两类未闭环问题：

1. 真实 Electron GUI 18 项没有完成取证。
2. Benchmark 中 `coding-035` 仍然是“零改动但判定成功”，fixture 和真实完成判定不一致。

本版本必须把 Super Agent 从“代码能力已实现”推进到“用户真实可用的后台工作系统”：

```text
用户提出目标 → Router 判断意图/权限/优先级 → Worker 后台执行
             → Watchdog 监控心跳/预算/卡死 → GUI 展示状态、Diff、审批和交付证据
             → 用户关闭窗口后任务仍可继续并恢复
```

## 1. 强制规则

- 不得只修改报告、截图或 JSON 数据。
- 不得把单元测试当成真实 GUI 验证。
- 不得用后端 API 放行代替审批卡片真实点击。
- 不得把模型自述当成任务完成证据。
- 不得删除失败样例或放宽判分器掩盖问题。
- 不得覆盖用户已有修改。
- 所有行为改动必须有单测、集成测试和真实端到端证据。
- 所有失败、阻塞和环境限制必须如实写入报告。

## 2. P0：完成真实 Electron GUI 18 项验收

使用 v0.9.1 最新构建版或安装版，不得只使用旧版本窗口。

### 2.1 启动和基础窗口

验证真实 Electron 应用启动、后端 health=200、首页为简洁操作台、版本号为目标版本、关闭后可以重新启动。保存应用版本、启动时间、health 响应和窗口截图。

### 2.2 开发任务闭环

在真实 GUI 中完成：

1. 新建对话并选择开发场景。
2. 选择一个已配置模型。
3. 输入“读取当前工作区并说明入口文件”。
4. 确认真实调用文件读取工具。
5. 输入代码修改任务。
6. 确认文件真实变化并查看 Diff。
7. 运行项目测试并查看退出码。
8. 确认交付只包含完成、改动、验证和需要用户处理的事项。

保存 task_id、run_id、工具事件、Diff、测试命令和最终交付文本。

### 2.3 模型切换

使用两个不同 provider/model：发送模型 A 任务，记录 `task.started`、`turn.context.model` 和 usage；切换模型 B 后再次发送任务，确认 provider、model、上游地址和 usage 真实变化。禁止只验证下拉框文字变化。

### 2.4 场景切换

验证开发、文档、日常、头脑风暴、逆向实验室五个场景。每个场景必须真实改变 Router 快照、系统提示规则、工具白名单、输出格式、记忆策略和审批策略。

- 文档默认只读。
- 头脑风暴不自动修改文件。
- 逆向实验室使用隔离工具和工作区。
- 只有日常显示“记住习惯/不记住”。
- 非日常场景不能写入用户习惯。

### 2.5 审批卡片真实点击

触发高风险 Shell、安装依赖、删除文件或网络访问：确认 GUI 出现审批卡片，卡片展示命令、目标、风险和影响范围；不批准时任务等待；点击批准后任务继续；点击拒绝后安全停止；审批事件与任务状态一致。禁止用后端 API 代替真实按钮点击。

### 2.6 日常记忆

验证 daily 的“记住习惯”：明确选择记住，确认产生 memory 写入并可被下一次任务检索；再验证“不记住”：确认没有 memory 写入事件且记忆文件指纹不变。其他场景不得出现写入记忆开关。

### 2.7 关闭和恢复

创建长任务，关闭或隐藏 Electron 窗口，确认 Worker 继续运行或任务进入可恢复状态；重新打开后能看到状态、事件、Diff 和交付。终止 Worker 后确认 Watchdog 释放租约并由新 Worker 接管。

## 3. P0：修复 coding-035 和零改动成功

当前问题：coding-035 没有修改预期文件、测试退出码为 0、任务却 succeeded，说明 fixture、expectedFilesTouched 和任务语义不一致。

推荐修复：让 fixture 中确实存在需要修复的类型错误，使任务目标、初始代码、测试和 expectedFilesTouched 一致，并重新运行 coding-035，确认 Agent 必须修改 `profiles.py`。

如果确实存在“无需修改”的合法任务，则判定器必须明确区分：

- `act + expectedFiles 非空 + changedFiles 为空`：不能直接成功，必须进入 `needs_review` 或提供结构化的无需修改证据。
- 交付文本不得声称已修改。
- 零改动伪成功必须进入质量统计。

验收：coding-035 passed，55 项中无零改动伪成功，`falseClaimCount=0`。

## 4. P0：Super Agent 后台运行能力

### 4.1 Router

Router 必须输出不可变 dispatch snapshot，包含 intent、scenario、priority、model_policy、tool_policy、notify_policy、memory_policy、steps、budget、approval_required 和 snapshot_id。执行侧禁止重新放宽快照权限。

### 4.2 Worker

支持 queued 领取、租约、心跳、检查点、工具幂等、进程退出恢复、暂停、恢复、取消、重试和预算收尾。

### 4.3 Trigger

至少支持 user_task、task_failed、approval_expired、workspace_changed、schedule。相同 dedupe_key 在有效期内只能生成一个任务，过期事件不能错误补跑，空 goal 不创建空任务。

### 4.4 Watchdog

监控心跳过期、长时间无事件、重复失败、审批等待、token/步骤/耗时预算和缺少交付证据。只能记录、通知、暂停、释放租约、恢复或终止，不能提权、绕过审批、扩大工作区或切换未授权模型。

## 5. P1：低打扰和主动工作

支持 silent、milestone、approval_only、always 四种通知策略，默认使用 milestone 或 approval_only。只有审批、无法恢复的失败、完成交付物或用户明确要求实时进度时打扰。

用户可配置后台执行、最大时长、最大步骤、token 预算、自动恢复次数、免打扰时段和默认模型策略。所有配置必须有安全上限，不能由模型自行提升。

默认交付固定为：

```text
完成：一句话结果
改动：文件和 Diff 数量
验证：测试命令与退出码
需要你：没有则隐藏；有则只显示一个动作
```

禁止空泛表扬、重复复述、完整内部计划、模型原始 JSON 和无证据的“已验证”。

## 6. 数据和可观测性

所有后台任务必须可查询 task_id、run_id、trigger_id、worker_id、snapshot_id、当前状态、当前步骤、最后心跳、最后事件、模型、token、审批状态、Diff 指纹和最终交付证据。前端不能根据最后一条文本猜状态，必须读取结构化状态和事件。

## 7. 测试要求

### 单元测试

- coding-035 判定。
- 零改动成功拦截。
- Router 安全默认值。
- Worker 状态机。
- Trigger 去重和过期。
- Watchdog 诊断和动作升级。
- 通知策略。
- daily 记忆开关。

### 集成测试

- Trigger → Router → Worker。
- Worker 租约、心跳、恢复。
- 工具调用幂等。
- 审批和任务状态同步。
- 模型 usage 记录。
- daily opt-in 记忆写入。

### 真实端到端

- Electron 启动。
- 新对话。
- 场景切换。
- 模型切换。
- 文件读取和修改。
- Diff 和测试。
- 审批点击。
- 后台任务。
- 关闭重开恢复。
- 记住/不记住。

## 8. 完整验收门槛

重新执行一次完整 55 项 Benchmark，并把正式结果写入 `evals/benchmarks/coding/last-run.json`，不得只写临时目录。

必须达到：

```text
completionRate >= 90%
passRate >= 90%
testPassRate = 100%
misEditedFiles = 0
falseDoneCount = 0
falseClaimCount = 0
unauthorizedHighRisk = 0
totalTokens > 0
coding-035 = passed
```

必须执行：

```text
services/api/.venv/Scripts/python.exe -m pytest -q
pnpm test:web
pnpm lint:web
pnpm typecheck:web
pnpm test:desktop
pnpm test:evals
pnpm check:versions
pnpm verify:desktop
pnpm -C packages/cli test
pnpm -C packages/cli build
git diff --check
```

## 9. 发布策略

目标版本为 `v0.9.1`。只有 coding-035 通过、完整 Benchmark 达标、GUI 18 项都有截图和事件证据、npm clean-prefix 安装通过、工作区干净且所有改动已提交后，才允许升版、打 tag 和发布。

如果 GUI 仍无法验证，必须保留当前版本并明确写“GUI 验证未完成”，不得伪称 v0.9.1 已发布。

## 10. 最终报告

必须包含版本号、Commit、Tag、修改文件、coding-035 前后证据、55 项正式 JSON 路径和逐项结果、所有质量指标、自动化回归、Electron GUI 18 项证据、模型切换事件、场景/记忆事件、审批截图和事件、后台恢复证据、npm 安装/doctor/start/health 证据、未完成项和最终工作区状态。

只有所有证据都存在时，才允许写“v0.9.1 可以发布”。
