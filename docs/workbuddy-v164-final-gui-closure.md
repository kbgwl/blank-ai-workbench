# WorkBuddy v164：v0.9.1 Super Agent 最终 GUI 闭环任务书

项目目录：`E:\projects\kbgai\kbgai`

目标：完成 v0.9.1 尚未完成的真实产品问题，并完成最终 GUI 18/18 验收。当前不要新开大功能，不要重做已经通过的 Benchmark 和后台运行时。

## 0. 当前真实状态

已通过：

- Super Agent Router / Worker / Trigger / Watchdog 基础能力。
- coding-035 修复。
- 55 项 Benchmark：completion 91%、pass 100%、testPass 100%。
- 后端 2121 passed / 23 skipped / 0 failed。
- Web、Desktop、CLI、Eval 自动化测试。
- 工作区已提交，版本号为 0.9.1。

未完成：

1. GUI 18 项中 17 项通过，2.7“关闭窗口后重新打开能看到并继续任务”失败。
2. 任务关闭窗口后可能出现 `conversation_id=None`，从会话列表消失。
3. 日常场景收到“修改文件”请求时答非所问，返回上一轮任务内容。
4. 文档场景权限为 `allow_write=True`，与文档默认只读要求不一致。
5. 逆向实验室仍使用 `isolation=host`，没有真正独立工作区。
6. v0.9.1 尚未打 Tag，尚未作为最终安装版发布。

## 1. 强制规则

- 不得只修改报告、截图或测试结果。
- 不得删掉失败 GUI 证据。
- 不得把 17/18 写成 18/18。
- 不得用后端接口代替真实 GUI 操作。
- 不得改变 Benchmark 判分器来掩盖问题。
- 不得用“提示词里写了隔离”冒充系统级隔离。
- 不得让模型自行决定扩大权限。
- 每个修复必须有单测、集成测试和真实 GUI 证据。
- v0.9.1 在 18/18 通过前不得打 Tag 或宣称正式发布。

## 2. P0：修复关闭窗口后任务不可见

### 2.1 当前问题

真实 GUI 测试中：

- Worker 在窗口关闭后仍然继续运行。
- 后端任务也继续产生事件。
- 但重新打开窗口后，会话列表找不到这条任务。
- 任务出现 `conversation_id=None`。
- 如果任务停在审批，用户无法重新找到任务并批准。

### 2.2 推荐修复

发送任务时必须先创建 conversation/thread，再创建 task/run：

```text
用户点击发送
→ 先创建 conversation/thread
→ 持久化 conversation_id
→ 创建 task
→ 创建 run
→ 写入 dispatch snapshot
→ Worker 执行
```

不得采用“先创建 task，窗口关闭后再补 conversation”的不可靠方式。

### 2.3 后端要求

- Task 必须有可追溯的 `conversation_id` 或等效 thread_id。
- 任务和会话写入必须在同一事务中完成，或有可靠的补偿机制。
- 如果历史任务没有 conversation_id，任务列表必须提供孤儿任务兜底入口。
- 任务详情接口必须能通过 task_id 打开对应会话。
- waiting_approval 任务必须能在重新打开后继续审批。
- 不允许因窗口关闭而取消后台任务。

### 2.4 前端要求

- 应用启动后加载 running、waiting_approval、paused、failed 的最近任务。
- 会话列表显示正在运行的后台任务。
- 任务消失时显示“后台任务”入口或恢复卡片。
- 点击任务后能看到当前步骤、最后事件、审批卡片和交付结果。
- 窗口关闭、最小化、重启三种情况都必须测试。

### 2.5 测试

新增：

- `conversation_id` 在发送瞬间落库。
- 创建 task/run 失败时 conversation 不产生孤儿。
- task 创建成功后窗口关闭，后台任务继续。
- 重新打开后任务出现在会话列表。
- waiting_approval 任务重新打开后可以批准或拒绝。
- 没有 conversation_id 的历史任务可以通过后台任务入口访问。

## 3. P0：修复日常场景答非所问

### 3.1 当前问题

日常场景收到“修改文件”请求后：

- 写工具被正确禁用。
- 但 Agent 返回了上一轮 `apply_discount` 相关内容。
- 用户无法知道当前请求为什么不能执行。

### 3.2 正确行为

当 daily 场景收到修改文件、运行命令、删除文件等请求时，必须返回简洁明确的拒绝或引导：

```text
日常场景不会直接修改文件。
请切换到开发场景后再执行此操作。
```

如果用户只是咨询代码，可以回答当前问题，但不得复用上一轮任务结果。

### 3.3 修复要求

- 每一轮必须使用当前 turn 的 user message。
- 禁止从上一轮 delivery、answer 或缓存内容拼接当前回答。
- `scenario=daily + intent=execute` 必须进入明确的 `needs_user` 或 `answer` 分支。
- daily 不得调用 apply_patch、run_shell、run_tests、delete、dependency install。
- 返回内容不得包含上一任务的文件名、结果或结论。
- 对话历史压缩后也不能把上一轮结果误当作当前请求。

### 3.4 测试

覆盖：

- daily + “修改 config.py” → 明确要求切换开发场景。
- daily + “删除文件” → 明确拒绝。
- daily + 普通问答 → 回答当前问题。
- 连续两轮不同问题 → 第二轮不能复用第一轮答案。
- 压缩上下文后再次提问 → 仍使用当前 turn。
- daily + 记住 → 只写用户习惯，不写任务结果。

## 4. P0：文档场景权限分级

### 4.1 当前冲突

任务书要求文档场景默认只读，但实际代码 `DOCS.allow_write=True`，否则无法创建和修改用户明确要求的 Markdown 文档。

### 4.2 正确设计

文档场景采用文件类型级权限，而不是简单全开或全关：

- 默认允许读取所有工作区文件。
- 默认只允许写入文档类文件：`.md`、`.txt`、`.rst`、`.adoc`、`.docx` 等明确允许类型。
- 默认禁止修改 `.py`、`.js`、`.ts`、`.json`、`.yaml`、`.toml`、数据库和构建文件。
- 修改文档类文件仍需遵守工作区白名单。
- 修改其他类型文件必须明确提示用户切换开发场景或单独审批。
- 删除、重命名、批量移动文档仍然需要审批。

### 4.3 验收

- 文档场景写 README.md：允许。
- 文档场景写普通说明文档：允许。
- 文档场景修改 Python 源码：拒绝或要求切换开发场景。
- 文档场景修改 package.json：拒绝或要求切换开发场景。
- 文档场景运行 Shell：默认拒绝。
- 文档场景安装依赖：拒绝。
- 前端 UI 明确显示当前可写文件类型。

## 5. P0：逆向实验室真正隔离

### 5.1 当前问题

逆向实验室虽然使用了专用提示和只读工具，但实际 `isolation=host`，与开发场景处于同一主机隔离档位。

### 5.2 目标

逆向实验室必须拥有独立的实验工作区：

- 默认只读原始样本。
- 派生文件写入独立 analysis workspace。
- 原始项目工作区不能被修改。
- Shell、网络、文件访问使用独立工具策略。
- 任务状态、Diff 和产物路径清晰区分原始样本与分析产物。
- 退出任务后能清理或保留分析工作区，由用户明确选择。

### 5.3 安全要求

- 不得自动复制密钥、`.env`、数据库和用户隐私文件进入分析区。
- 不得默认允许外网访问。
- 不得允许分析工具写入原始工作区。
- 高风险操作必须审批。
- sandbox tier 必须在 dispatch snapshot 中固定。

### 5.4 验收

- 逆向任务读取样本成功。
- 逆向任务生成分析文件时写入独立目录。
- 原始文件 md5 不变化。
- 开发工作区 Diff 为空。
- 分析产物可以在 GUI 中单独查看。
- 逆向任务不能调用开发场景的写入工具。

## 6. Super Agent 后台闭环增强

确认并补齐：

- Router snapshot 不可变。
- Worker 租约和心跳。
- 任务暂停、恢复、取消、重试。
- Trigger 去重、过期和失败触发。
- Watchdog 监控心跳、审批、预算和卡死。
- 任务关闭窗口后继续。
- 重新打开后可见。
- 任务等待审批时可以从 GUI 恢复。
- 任务完成后有结构化 delivery。
- 四行交付只展示事实，不展示无用过程。

后台任务最少必须能查询：

```text
task_id
run_id
conversation_id
trigger_id
worker_id
snapshot_id
status
current_step
last_event
last_heartbeat
model
tokens
approval
changes
delivery
```

## 7. 最终 GUI 18/18 验收

重新执行全部 GUI 项，不能沿用 17/18 的旧结论：

1. 启动真实 Electron。
2. health=200。
3. 首页和版本正确。
4. 新建对话。
5. 选择开发场景。
6. 选择模型 A。
7. 读取真实工作区。
8. 修改真实文件。
9. 查看 Diff。
10. 运行测试并查看退出码。
11. 交付内容简洁且有事实。
12. 切换模型 B 并确认真实 provider/model 变化。
13. 验证五个场景。
14. 触发审批卡片并真实点击批准。
15. 真实点击拒绝并确认任务停止。
16. 日常记住/不记住。
17. 关闭窗口后 Worker 继续。
18. 重新打开后任务可见、可审批、可继续或可查看结果。

每一项都需要截图、task_id/run_id、事件或磁盘事实。不能用单元测试替代。

## 8. 自动化回归

执行：

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

Benchmark 必须确认：

```text
completionRate >= 90%
passRate >= 90%
testPassRate = 100%
misEditedFiles = 0
falseDoneCount = 0
falseClaimCount = 0
zeroChangeSuccessCount = 0
unauthorizedHighRisk = 0
coding-035 = passed
totalTokens > 0
```

## 9. 发布条件

只有以下全部满足才允许打 `v0.9.1` Tag：

- GUI 18/18 通过。
- 关窗恢复问题修复。
- daily 答非所问修复。
- 文档权限分级完成。
- 逆向实验室隔离完成。
- coding-035 通过。
- 自动化回归全绿。
- npm clean-prefix 安装、doctor、start、health 通过。
- 工作区干净。
- 报告和真实产物一致。

如果 GUI 仍为 17/18，不得打 Tag，不得写“v0.9.1 可以发布”。

## 10. 最终报告

报告必须逐项写出：

- 四个问题的修复文件和测试。
- GUI 18 项截图和事件证据。
- 关窗恢复的 conversation_id、task_id、run_id。
- daily 场景前后回答对比。
- 文档场景允许/拒绝的文件类型矩阵。
- 逆向工作区路径、原始文件 md5 和分析产物 Diff。
- Benchmark 正式 JSON 路径和指标。
- 所有回归命令和退出码。
- npm 安装和 health 证据。
- 最终 Commit、Tag 和工作区状态。
- 未完成项。

只有所有证据真实存在，才允许写“v0.9.1 已完成”。
