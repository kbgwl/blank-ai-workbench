# WorkBuddy v160 最终质量与发布闸门

工作目录：`E:\projects\kbgai\kbgai`

## 当前事实

自动化测试已通过：Desktop 57、Web 258、Evals 10、后端相关测试 84、CLI 5、TypeScript 和 lint。

但当前版本仍未完成：`last-run.json` 仍显示 55 个 Benchmark 任务完成率 71%、通过率 71%、测试通过率 95%、误修改文件 3、falseDone 3、未授权高风险 1、totalTokens 0。`glm-fix-verify.json` 只有 5 个任务，不能代表全量结果。工作区还有大量未提交修改。

## 强制规则

- 不得把报告文件当成实际验证证据。
- 不得把 5 个任务结果当成 55 个任务结果。
- 不得修改判分规则、删除任务、放宽断言来提高分数。
- 不得把 mock Provider、pack dry-run、Desktop 自动化测试当成真实发布验证。
- 必须修改真实逻辑并保留回归测试。
- 未满足发布闸门前不得报告“全部实现”或“可以发布”。

## P0-1：重新建立 Benchmark 全量基线

先读取 `evals/benchmarks/coding/last-run.json`、`failure-analysis-v159.json`、`glm-fix-verify.json`，逐个列出失败任务的 status、failure_reason、changedFiles、unexpectedFiles、testExit、approvals、tool calls 和最终交付文本。

必须区分：setup_failed、provider_failed、model_failed、agent_failed、test_failed、mis_edit、false_done、unauthorized_high_risk、succeeded。429 和网络错误不能直接算作产品能力失败。

逐个修复：

- falseDone：没有测试证据、没有目标文件修改、仍有审批/问题未完成时，禁止输出完成。
- unauthorizedHighRisk：未授权的高风险工具和 run_tests 不得执行；拒绝、取消、超时后不得继续写操作。
- misEditedFiles：目标文件之外默认不可写；测试和 fixture 默认不可修改；最终 diff 必须检查额外文件。
- 真实测试失败：修复 Agent、工具、Prompt 或模型路由，不得只改分数。

先单例复跑所有失败任务，再全量运行 55 任务。新的 `last-run.json` 必须由真实 runner 生成，不能手工修改。

最低阈值：completionRate ≥ 90%、passRate ≥ 90%、testPassRate = 100%（排除明确预期中断）、falseDoneCount = 0、unauthorizedHighRisk = 0、misEditedFiles = 0。

## P0-2：Token Usage 全量闭环

检查 OpenAI、Responses、Anthropic、OpenRouter、Ollama、流式和非流式适配器。

统一记录：input_tokens、output_tokens、total_tokens、cache_read_tokens、cache_write_tokens、reasoning_tokens、usage_source。

要求：

- Provider 有 usage 时照实记录。
- Provider 没有 usage 时写 null/unknown，不伪造 0。
- retry 和 fallback 每次调用单独记录。
- Task、Run、Turn 不重复累计。
- Evidence、Usage Ledger、前端和 Benchmark 使用同一口径。
- 流式响应必须读取最终 usage chunk。

新增测试覆盖非流式、流式、无 usage、retry、fallback、cache、reasoning 和 Benchmark 汇总。全量 Benchmark 必须显示 totalTokens > 0、平均 Token、P95 Token 和按模型统计。

## P0-3：npm 干净安装

使用全新临时 npm prefix，不允许从 workspace 链接执行。执行 npm pack、npm install --global --prefix、安装版 `blank-ai --help`、`doctor`、`start --workspace <临时目录>`。

必须证明：命令来自 prefix；数据库迁移成功；/health 返回 200；静态页面能打开；workspace 参数生效；关闭后无孤儿进程；重启后数据仍存在。

保存安装日志、doctor 输出、health 响应和进程状态。只做 pack dry-run 不算完成。

## P0-4：Desktop 真实 GUI

启动真实 unpacked 或安装版 Electron，不要只跑 node test。验证窗口出现、页面加载、后端健康、输入框可用、场景切换、模型切换、托盘显示/隐藏/退出和重启数据保留。

如果本机 Electron 无法启动，必须记录 OS、Node、Electron、GPU、沙箱、命令和错误日志，并在独立 Windows runner 或另一台 Windows 机器验证。报告必须写“本机未验证/独立环境已验证”，不能直接算通过。

## P1：真实 GUI 编码闭环

准备独立临时项目，真实执行：读取项目、定位失败测试、只改实现文件、运行测试、修复失败、再次测试、查看 diff、查看 Evidence。

必须证明：真实读取文件、真实修改文件、没有误改测试、测试命令真实执行、最终结果有证据且回复简洁。

同时真实验证：文档场景不主动改代码；日常不记住不写长期记忆；日常记住会落盘并可删除；模型切换改变真实请求；逆向实验室真实请求和错误状态正常。

## P1：未提交修改审计

逐文件检查当前所有 modified/untracked 文件：是否属于本阶段、是否有测试、是否需要迁移、是否有临时调试代码、是否影响 API、是否应提交。

迁移 `0030_usage_source.py` 必须验证空库 upgrade、已有库 upgrade、revision/down_revision、重复迁移和 downgrade 行为。

## 完整回归

必须执行：`pnpm test:desktop`、`pnpm test:web`、`pnpm lint:web`、`pnpm typecheck:web`、`pnpm test:evals`、`pnpm check:versions`、`pnpm -C packages/cli test`、`pnpm -C packages/cli build`、`pnpm verify:desktop`、`git diff --check`。

后端至少执行 handoff、delivery、model switch、real file、scenarios、model retry、stream output、usage levels 相关测试。

Benchmark 必须单例复跑失败任务，再全量 55 任务，保存真实新报告。

## 提交闸门

只有以下全部满足才允许提交发布候选：

1. Benchmark 达到阈值。
2. falseDone、未授权操作、误修改文件均为 0。
3. totalTokens > 0。
4. npm 干净 prefix 安装、doctor、start、health 通过。
5. Desktop 真实 GUI 或独立 Windows runner 通过。
6. 真实项目完成读取、修改、测试和交付。
7. 模型切换和日常记忆有真实证据。
8. 自动化回归全部通过。
9. 无密钥和绝对路径泄露。
10. 工作区清理并提交后保持干净。

## 最终报告必须包含

- 自动化测试每条命令和结果。
- 55 任务 Benchmark 全量指标和失败明细。
- Token 总量、平均值、P95 和按模型统计。
- npm clean prefix 安装、doctor、start、health 证据。
- Desktop 真实窗口启动证据。
- GUI 场景、模型、文件修改、测试修复证据。
- 未完成项和真实原因。
- commit hash 和最终 git status。

未满足以上条件，不得报告“全部实现”或“可以发布”。
