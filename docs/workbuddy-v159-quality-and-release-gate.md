# WorkBuddy v159：质量基线、真实安装和最终发布闸门

工作目录：`E:\projects\kbgai\kbgai`

## 0. 当前真实状态

当前自动化测试已经通过：

```text
Desktop：57 passed
Web：258 passed
TypeScript：通过
Lint：通过
Evals：10 passed
CLI：5 passed
后端核心：33 passed
```

但当前版本仍然不能称为“全部实现”，原因：

### Benchmark 未达标

当前 `evals/benchmarks/coding/last-run.json` 显示：

```text
总任务：55
完成率：71%
通过率：71%
测试通过率：95%
误修改文件数：3
falseDone：3
未授权高风险操作：1
totalTokens：0
```

### 真实环境证据不足

- Electron GUI 窗口没有在当前机器上完成真实启动验证。
- npm CLI 的干净环境安装需要重新独立验证。
- 模型真实请求虽然有部分报告，但需要重新保存可复核证据。
- Token usage 当前为 0，无法用于成本和效率评估。

## 1. 总体目标

本阶段只做质量和发布收尾，不再增加无关页面。

最终目标：

```text
Benchmark 可解释且达到发布阈值
→ Token usage 可追踪
→ npm 真实安装启动
→ Desktop GUI 真实启动
→ GUI 真实任务闭环
→ 发布证据完整
```

## 2. 强制规则

### 2.1 不能用报告代替验证

以下内容都不能单独作为证据：

- README 中的描述。
- WorkBuddy 的文字总结。
- `closure-report.md` 中的结果。
- 只有单元测试的结果。
- 只有 mock Provider 的结果。
- 只有 tarball dry-run 的结果。
- 只有 Desktop 包结构检查的结果。

每个发布结论必须包含：

- 执行命令。
- 执行环境。
- 原始结果。
- 产物或日志位置。
- 失败时的具体原因。

### 2.2 禁止修改判分标准掩盖失败

Benchmark 失败时只能按以下顺序处理：

1. 读取完整失败任务。
2. 判断是产品错误、模型错误、任务定义错误、fixture 错误还是 runner 错误。
3. 如果是产品错误，修复 Agent 或工具逻辑。
4. 如果是任务定义错误，保留修改前后对比，并单例复跑。
5. 再跑全量 Benchmark。

禁止：

- 直接把失败改为预期失败。
- 放宽 `expectedFiles` 来忽略误修改。
- 把 falseDone 从统计中删除。
- 把未授权操作从统计中删除。
- 只增加步数而不解释原因。
- 只修改报告数字。

## 3. P0：Benchmark 质量修复

### 3.1 读取完整结果

执行：

```powershell
Get-Content evals/benchmarks/coding/last-run.json
Get-Content evals/benchmarks/coding/README.md
```

生成失败清单，至少包括：

- task id。
- category。
- status。
- expectedStatus。
- failure_reason。
- changedFiles。
- unexpectedChanges。
- testExit。
- approvals。
- tool calls。
- 最后一条 Agent 回复。
- 是否出现 falseDone。
- 是否出现 unauthorized high risk。

不得只看 summary。

### 3.2 falseDone 修复

对 3 个 falseDone 任务逐一复现：

- Agent 回复是否声称完成。
- 实际文件是否修改。
- 测试是否执行。
- 测试结果是否通过。
- Evidence 是否完整。
- 判分器是否正确读取终态。

如果 Agent 确实错误声称完成：

- 修改交付判断逻辑。
- 没有测试证据时禁止输出“已完成”。
- 没有 expected file 修改时禁止输出成功。
- 有未完成审批或工具调用时只能输出“未完成/等待”。
- 增加对应回归测试。

如果是判分器错误：

- 修复判分器。
- 保留原始结果。
- 说明为什么原判分错误。
- 单例和全量都要复跑。

### 3.3 未授权高风险操作修复

对 `unauthorizedHighRisk = 1` 的任务定位：

- 哪个工具。
- 哪条命令。
- 为什么被判定为高风险。
- 是否出现审批卡。
- 是否执行了实际命令。
- 是否有授权事件。

要求：

- 高风险工具没有授权时不能执行。
- `run_tests` 也必须遵守审批门。
- 取消、拒绝、超时都不能继续执行写操作。
- 事件、ToolCall 和 Evidence 的授权状态必须一致。
- 增加“未授权绝不执行”的端到端测试。

### 3.4 误修改文件修复

对 3 个误修改任务逐一确认：

- 修改了哪些额外文件。
- 额外文件是测试、数据、配置还是源码。
- Agent 是否被允许修改它们。
- 任务定义是否明确声明了文件范围。

如果是 Agent 错误：

- 增加 expected files 边界提示。
- 修改 Agent 系统规则。
- 工具层增加允许修改文件范围。
- 最终 diff 检查拒绝额外文件。

如果是任务定义错误：

- 修正任务定义。
- 添加变更说明。
- 单例复跑。
- 全量复跑。

目标：

```text
misEditedFiles = 0
```

### 3.5 运行 Benchmark

先运行失败任务单例，再运行全量：

```powershell
$env:BLANK_BENCH_API="http://127.0.0.1:8060/api/v1"
node evals/benchmarks/coding/run.mjs --mock --task <id>
node evals/benchmarks/coding/run.mjs --mock --report evals/benchmarks/coding/last-run.json
Remove-Item Env:BLANK_BENCH_API
```

如果使用真实模型：

- 记录 Provider 和模型 ID。
- 记录请求失败和限流情况。
- 设置任务间隔，避免把 Provider 429 当成产品失败。
- 不把 setup_failed 直接算作 Agent 能力失败。
- 失败任务必须支持复跑。

### 3.6 Benchmark 发布阈值

最低发布阈值：

- `falseDoneCount = 0`。
- `unauthorizedHighRisk = 0`。
- `misEditedFiles = 0`。
- `testPassRate = 100%`，除非任务明确预期中断。
- completion rate ≥ 90%。
- pass rate ≥ 90%。

如果没有达到阈值，必须标记为“质量基线未达标”，不能宣传为顶级编码能力。

## 4. P0：Token Usage 和成本统计

### 4.1 当前问题

当前 Benchmark 中：

```text
totalTokens = 0
```

这会导致无法判断：

- 哪个模型更快。
- 哪个模型更省。
- 哪个任务上下文过长。
- fallback 是否造成额外成本。
- Agent 是否重复调用。

### 4.2 修复模型响应 usage

检查所有 Provider 的响应适配：

- 非流式响应。
- 流式响应最终 chunk。
- OpenAI 兼容接口。
- Anthropic 风格响应。
- OpenRouter 响应。
- 本地模型响应。

统一字段：

```json
{
  "input_tokens": 0,
  "output_tokens": 0,
  "total_tokens": 0,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "reasoning_tokens": 0,
  "usage_source": "provider|estimated|unknown"
}
```

要求：

- Provider 返回 usage 时必须记录。
- 没有 usage 时明确标记 unknown，不得伪造 0。
- 流式响应结束时必须合并最终 usage。
- 重试和 fallback 每次调用单独计费。
- ToolCall 不得重复累计模型 Token。
- 任务、run、turn 三个层级口径一致。

### 4.3 Token 验收

增加测试：

- 非流式 usage 入账。
- 流式最终 usage 入账。
- 缺 usage 时为 null/unknown。
- fallback 两次调用分别记录。
- 缓存 Token 有独立字段。
- Evidence、事件和前端显示一致。

Benchmark 中必须输出：

- 每个任务 tokens。
- 每个模型 tokens。
- 平均 tokens。
- P95 tokens。
- 失败任务 tokens。

## 5. P0：npm 真实干净环境安装

### 5.1 不能只做 dry-run

执行真实打包和安装：

```powershell
$root = Join-Path $env:TEMP "blank-ai-v159"
$prefix = Join-Path $root "npm-prefix"
$workspace = Join-Path $root "workspace"
New-Item -ItemType Directory -Force $root, $prefix, $workspace | Out-Null

pnpm -C packages/cli exec npm pack --pack-destination $root
npm install --global --prefix $prefix (Join-Path $root "blank-ai-cli-0.1.0.tgz")
```

验证安装版本：

```powershell
& (Join-Path $prefix "blank-ai.cmd") --help
& (Join-Path $prefix "blank-ai.cmd") doctor
```

### 5.2 真实启动

```powershell
& (Join-Path $prefix "blank-ai.cmd") start --workspace $workspace
```

必须确认：

- 执行文件来自 `$prefix`，不是仓库。
- 服务进程启动。
- `/health` 返回 200。
- 数据库迁移成功。
- 静态页面能打开。
- `--workspace` 指向临时目录。
- 关闭后没有孤儿进程。
- 重启后配置仍然存在。

保存：

- npm 安装日志。
- `doctor` 输出。
- `/health` 响应。
- 进程列表。
- 启动和退出日志。

## 6. P0：Desktop 真实 GUI 验证

### 6.1 自动化测试不等于 GUI 通过

以下只能算自动化通过：

- `pnpm test:desktop`。
- `verify:desktop`。
- 包内文件存在。

必须额外验证真实 Electron 窗口：

- 实际启动 exe。
- 实际显示窗口。
- 实际加载页面。
- 实际点击输入框。
- 实际切换场景。
- 实际切换模型。
- 实际关闭和恢复窗口。

### 6.2 启动验证

使用打包后的 unpacked 或安装版：

1. 限制 PATH，不提供项目 `.venv`。
2. 启动 Desktop。
3. 等待窗口出现。
4. 检查后端健康状态。
5. 检查静态页面。
6. 检查用户数据目录。
7. 关闭并重新启动。

如果本机 Electron 无法启动，必须：

- 记录 OS、GPU、Electron、错误日志。
- 在另一台 Windows 机器或 CI Windows runner 验证。
- 不得把“本机环境限制”直接当成产品完成。

### 6.3 GUI 任务闭环

准备临时项目，执行：

```text
读取项目
定位失败测试
只修改实现文件
运行测试
修复失败
再次运行测试
展示 diff
输出简洁结果
```

必须截图或保存日志证明：

- 页面加载。
- 模型选择。
- 场景选择。
- 工具调用。
- 文件修改。
- 测试结果。
- 最终交付。

### 6.4 日常记忆

验证两次：

#### 不记住

- 记忆文件字节不变。
- 没有 memory.consolidated。
- 下一次任务不能使用该偏好。

#### 记住

- 产生 memory.consolidated。
- 记忆真实落盘。
- 下一次日常任务能读取。
- 删除后立即失效。
- 开发场景不会自动读取。

## 7. P1：模型切换证据

准备两个模型配置，分别发送任务。

必须同时检查：

- UI 当前模型。
- 请求 payload 模型。
- Provider 日志。
- task event。
- turn context。
- Evidence。

模拟第一个模型失败，验证：

- fallback 是否发生。
- fallback 是否经过允许的策略。
- 原模型错误是否保留。
- 最终模型是否正确显示。
- Token 是否分别统计。

不能只检查下拉框值变化。

## 8. 安全检查

执行：

```powershell
git status --short
git diff --check
git grep -n -I -E "(ghp_[A-Za-z0-9]+|npm_[A-Za-z0-9]+|OPENAI_API_KEY=|ANTHROPIC_API_KEY=)"
```

检查：

- npm 包无密钥。
- Desktop 包无密钥。
- 日志无 Authorization、Bearer、API Key。
- 不包含本机绝对路径。
- 用户数据库和密钥不进入 Git。
- 评测 Provider 凭据不进入 `last-run.json`。

## 9. 最终回归命令

```powershell
pnpm test:desktop
pnpm test:web
pnpm lint:web
pnpm typecheck:web
pnpm test:evals
pnpm check:versions
pnpm -C packages/cli test
pnpm -C packages/cli build
pnpm verify:desktop
git diff --check
git status --short
```

后端：

```powershell
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_handoff_e2e_v12.py services/api/tests/test_delivery_snapshots_v153.py services/api/tests/test_model_switch_v153.py services/api/tests/test_real_file_e2e_v153.py services/api/tests/test_scenarios_v153.py
```

Benchmark：

- 先单例复跑所有失败任务。
- 再跑全量 55 个任务。
- 保存新 `last-run.json`。
- 给出失败任务逐项原因。

## 10. 最终发布闸门

只有全部满足才允许报告“可以发布”：

1. Desktop 自动化测试全部通过。
2. Web、lint、typecheck、CLI、后端核心测试全部通过。
3. Benchmark 达到发布阈值。
4. `falseDoneCount = 0`。
5. `unauthorizedHighRisk = 0`。
6. `misEditedFiles = 0`。
7. Token usage 不再全部为 0。
8. npm tarball 在干净 prefix 安装成功。
9. 安装后的 CLI 可以执行 `help`、`doctor` 和 `start`。
10. Desktop GUI 在真实环境中启动成功，或已在独立 Windows runner 验证。
11. 至少一个真实项目完成读取、修改、测试和交付闭环。
12. 模型切换有真实请求证据。
13. 日常记忆的记住/不记住都有真实证据。
14. 工作区无未解释修改。

## 11. 最终报告格式

```md
# v159 质量与发布报告

## 自动化测试
- Desktop：
- Web：
- Lint：
- TypeScript：
- Evals：
- Backend：
- CLI：

## Benchmark
- 总数：
- completion rate：
- pass rate：
- test pass rate：
- falseDone：
- unauthorized high risk：
- misEditedFiles：
- totalTokens：
- 失败任务及原因：

## npm 真实安装
- tarball：
- clean prefix install：
- help：
- doctor：
- start：
- health：

## Desktop 真实 GUI
- 启动：
- 页面：
- 后端：
- 模型切换：
- 场景切换：
- 文件修改：
- 测试执行：
- 托盘：

## 未完成
- ...

## 发布结论
- 只有所有发布闸门通过，才允许写“可以发布”。
```
