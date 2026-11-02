# WorkBuddy v161：当前工作区最终收尾任务

工作目录：`E:\projects\kbgai\kbgai`

## 当前验证结论

当前代码回归已通过：

- Desktop：57 passed。
- Web：258 passed。
- Lint：通过。
- TypeScript：通过。
- CLI：6 passed。
- 后端相关测试：86 passed。

但以下指标仍未完成：

- Benchmark 55 任务完成率 71%。
- Benchmark 通过率 71%。
- 测试通过率 95%。
- 误修改文件 3 个。
- falseDone 3 个。
- 未授权高风险操作 1 个。
- 主基线 `totalTokens = 0`。
- 工作区存在大量未提交修改。

因此当前不能报告“全部完成”，也不能发布。

## 一、强制执行规则

1. 不得只修改 `last-run.json` 的数字。
2. 不得把 5 个任务结果当成 55 个任务结果。
3. 不得删除失败任务或修改判分器来掩盖失败。
4. 不得把 429、网络失败和真实 Agent 失败混为一个结果。
5. 不得只改测试文件，不改真实 Agent、工具或模型逻辑。
6. 不得在未完成前提交 `final`、`release` 或 `complete` 语义的 commit。
7. 任何“已完成”都必须有命令、日志、产物或测试证据。

## 二、Benchmark 全量修复

### 2.1 读取完整结果

读取：

- `evals/benchmarks/coding/last-run.json`
- `evals/benchmarks/coding/failure-analysis-v159.json`
- `evals/benchmarks/coding/glm-fix-verify.json`
- `evals/benchmarks/coding/run.mjs`
- `evals/benchmarks/coding/README.md`

生成失败表，每行必须包含：

- 任务 ID。
- 任务类别。
- 终态和期望终态。
- 失败原因。
- 测试退出码。
- 修改文件。
- 额外修改文件。
- 审批数量。
- 工具调用列表。
- 最终交付文本。
- 是否 falseDone。
- 是否未授权高风险。
- 是否 Provider/setup failure。

### 2.2 修复 falseDone

逐个复现 falseDone 任务，检查：

- 是否真实完成目标。
- 是否修改目标文件。
- 是否真实执行验证命令。
- 是否测试通过。
- 是否还有等待审批、等待回答或未完成工具调用。
- 最终交付是否错误声称完成。

修复要求：

- 没有验证证据不得输出成功。
- 测试未运行、跳过或未判定不得显示通过。
- 任务未达到 acceptance 不得输出完成。
- 部分完成必须显示缺口。

验收目标：`falseDoneCount = 0`。

### 2.3 修复未授权高风险操作

定位唯一未授权任务，记录：

- 工具名称。
- 命令内容。
- 风险等级。
- 审批卡是否出现。
- 用户是否批准。
- 命令是否实际执行。
- 是否产生文件修改。

修复要求：

- 高风险工具没有批准不能执行。
- `run_tests` 也遵守审批门。
- 拒绝、取消、超时后不能继续写操作。
- 事件、ToolCall、Evidence 权限状态必须一致。
- 增加端到端回归测试。

验收目标：`unauthorizedHighRisk = 0`。

### 2.4 修复误修改文件

逐个检查 3 个误修改任务：

- 额外修改了什么文件。
- 是否是测试、fixture、数据或配置文件。
- 任务定义是否允许。
- Agent 是否应该拥有该路径权限。

修复要求：

- 任务目标之外的文件默认不可写。
- 测试和 fixture 默认不可修改。
- 写操作前检查允许文件列表。
- 结束时对比 diff，出现额外文件必须判定失败。

验收目标：`misEditedFiles = 0`。

### 2.5 重新运行 55 个任务

先单例复跑全部失败任务，再进行全量运行。

运行时必须：

- 使用独立评测数据库。
- 记录真实 Provider 和模型。
- 设置任务间隔防止 429。
- 将 Provider 限流单独归类为 setup/provider failure。
- 每次任务使用独立状态。
- 保存全量新结果，不能复用旧结果。

最低发布阈值：

- completionRate ≥ 90%。
- passRate ≥ 90%。
- testPassRate = 100%，排除明确预期中断。
- falseDoneCount = 0。
- unauthorizedHighRisk = 0。
- misEditedFiles = 0。

## 三、Token 全量统计修复

### 3.1 当前问题

主基线仍显示 `totalTokens = 0`。小范围结果有 Token，不代表全量统计已经闭环。

### 3.2 覆盖所有模型适配器

检查并统一：

- OpenAI 非流式。
- OpenAI 流式。
- OpenAI Responses。
- Anthropic。
- OpenRouter。
- Ollama。
- retry。
- fallback。

统一字段：

- input_tokens。
- output_tokens。
- total_tokens。
- cache_read_tokens。
- cache_write_tokens。
- reasoning_tokens。
- usage_source。

规则：

- Provider 有 usage 就记录真实值。
- Provider 没有 usage 写 null/unknown，不写假 0。
- 流式响应从最终 chunk 读取 usage。
- 每次 retry/fallback 单独记录。
- Task、Run、Turn 汇总不能重复计算。
- Evidence、Usage Ledger、前端和 Benchmark 使用相同口径。

### 3.3 Token 验收

补充测试：

- 非流式 usage。
- 流式最终 usage。
- 缺 usage。
- retry。
- fallback。
- cache tokens。
- reasoning tokens。
- Evidence 汇总。
- Benchmark 汇总。

全量 Benchmark 必须输出：

- totalTokens。
- 平均 Token。
- P95 Token。
- 按模型统计。
- 失败任务 Token。

## 四、工作区修改整理

当前未提交修改涉及 Agent、模型、Token、Evidence、Benchmark、CLI、迁移和测试。

逐个文件检查：

- 是否属于 v161。
- 是否有对应测试。
- 是否有临时调试代码。
- 是否需要数据库迁移。
- 是否破坏旧 API。
- 是否需要更新 README 或部署文档。
- 是否应该提交。

重点检查 `services/api/alembic/versions/0030_usage_source.py`：

- 空数据库 upgrade。
- 已有数据库 upgrade。
- revision/down_revision 正确。
- 重复迁移不会破坏。
- downgrade 行为明确。

不要覆盖用户修改。不要直接使用 reset、checkout 覆盖工作区。

## 五、回归测试

必须重新执行：

- `pnpm test:desktop`
- `pnpm test:web`
- `pnpm lint:web`
- `pnpm typecheck:web`
- `pnpm test:evals`
- `pnpm check:versions`
- `pnpm -C packages/cli test`
- `pnpm -C packages/cli build`
- `pnpm verify:desktop`
- 后端 handoff、delivery、model switch、real file、scenarios、retry、stream、usage 测试
- `git diff --check`

回归通过后再跑 55 任务 Benchmark，不能先提交再验证。

## 六、真实安装和 GUI 证据

必须独立验证：

- 新 npm prefix 安装 tarball。
- 安装版 `blank-ai --help`。
- 安装版 `doctor`。
- 安装版 `start --workspace <临时目录>`。
- `/health` 200。
- 无仓库源码依赖。
- Desktop 真实 Electron 窗口。
- 场景切换。
- 模型切换改变真实请求。
- 真实文件读取、修改和测试修复。
- 日常记住/不记住。

如果本机不能启动 GUI，必须在独立 Windows runner 或另一台 Windows 机器完成，并报告环境信息。不能把未启动的 GUI 直接算通过。

## 七、提交条件

只有以下全部满足后才能提交：

1. Benchmark 达到阈值。
2. falseDone、未授权操作、误修改文件全部为 0。
3. totalTokens > 0。
4. npm 干净安装和 start 通过。
5. Desktop GUI 真实启动或独立 Windows runner 通过。
6. 真实项目完成读取、修改、测试和交付。
7. 模型切换和日常记忆有证据。
8. 自动化回归全部通过。
9. 没有密钥、Token 和本机绝对路径泄露。
10. 工作区整理后干净。

提交前执行：

- `git status --short`
- `git diff --check`
- `git diff --stat`
- 只添加已经验证的文件。
- 使用说明明确的 commit message。
- commit 后再次检查 `git status --short`。

## 八、最终报告格式

报告必须包含：

- 自动化测试命令和结果。
- 55 任务 Benchmark 全量指标。
- 失败任务逐项真实原因。
- falseDone、未授权、误修改统计。
- Token 总量、平均值、P95 和模型分布。
- npm clean prefix 安装、doctor、start、health 证据。
- Desktop GUI 真实启动证据。
- 场景、模型、文件修改、测试修复证据。
- 未提交文件和最终 commit hash。
- 未完成项。

只有所有阈值通过，才允许写“可以发布”。
