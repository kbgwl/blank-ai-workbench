# WorkBuddy v158：真实安装、GUI 端到端与 Benchmark 收尾任务书

工作目录：`E:\projects\kbgai\kbgai`

## 0. 当前真实基线

已实际验证通过：

```text
Desktop：57 passed / 0 failed
Web：257 passed / 0 failed
TypeScript：通过
Web lint：通过
Evals：10 passed / 0 failed
CLI：5 passed / 0 failed
CLI build：通过
Desktop 包结构检查：通过
版本：0.8.17
```

但当前仍不能说最终发布完成，原因：

1. Coding Benchmark 目前约 `52/55`，不是 100%。
2. npm tarball 只做了 pack dry-run，没有完成真实全局安装启动。
3. Desktop 包结构通过，但没有完成干净环境真实启动和 GUI 操作验收。
4. 模型切换、场景切换、本地文件修改、测试失败修复还没有全部形成真实端到端证据。

本阶段目标：

```text
Benchmark 缺陷收尾
→ npm 干净环境安装
→ Desktop 干净环境启动
→ 真实 GUI 任务闭环
→ 真实模型切换
→ 形成发布证据
```

## 1. 强制规则

### 1.1 不得伪造完成

- 不得把 `pack --dry-run` 当成 npm 安装成功。
- 不得把单元测试当成 GUI 端到端通过。
- 不得把 mock Provider 当成真实模型调用证据。
- 不得把 fixture 测试通过当成所有 Benchmark 任务完成。
- 不得把“启动窗口”当成“任务可执行”。
- 不得把 README 中的说明当成真实运行证据。

### 1.2 不得为了提高分数修改判分标准

Benchmark 失败时必须先判断：

- 是产品真的误修改文件。
- 是模型行为不稳定。
- 是任务定义错误。
- 是测试 fixture 错误。
- 是 runner 没有等待长请求。
- 是审批、取消、恢复流程没有真正执行。

只有确认任务定义错误，才允许修改任务定义，并且必须保留修改前后的对比和单例复跑结果。

## 2. P0：Benchmark 52/55 收尾

### 2.1 读取最近结果

先读取：

```powershell
Get-Content evals/benchmarks/coding/last-run.json
Get-Content evals/benchmarks/coding/README.md
Get-Content evals/benchmarks/coding/tasks/coding-012.json
Get-Content evals/benchmarks/coding/tasks/coding-034.json
Get-Content evals/benchmarks/coding/tasks/coding-035.json
```

列出所有失败任务：

- task id。
- category。
- status。
- failure reason。
- changed files。
- unexpected files。
- test exit code。
- 是否有审批。
- 是否有未完成工具调用。

不要只看 `summary`，必须读取每个失败任务的完整结果。

### 2.2 误修改文件问题

对于 `expectedFiles` 之外的修改，逐个判断：

1. 是否是模型错误修改。
2. 是否是测试本身需要修改但任务定义漏写。
3. 是否是 Agent 为了验证而修改了测试或数据。
4. 是否应该被禁止修改。
5. 是否应该加入工具权限限制。

如果是模型错误：

- 修复 Prompt 或 Agent 约束。
- 增加“禁止修改测试/fixture”的规则。
- 增加错误修改测试。

如果是任务定义错误：

- 修正 `expectedFiles`。
- 在 README 记录原因。
- 重新跑单个任务。
- 再跑完整 55 个任务。

不能只修改统计脚本把错误从失败变成通过。

### 2.3 034 步数耗尽问题

重点检查 `coding-034`：

- 是否因为 Agent 没有在目标完成后停止。
- 是否重复读取相同文件。
- 是否重复运行相同测试。
- 是否上下文过长导致无效调用。
- 是否模型没有收到明确验收条件。
- 是否 runner 步数过低。

要求：

- 如果是 Agent 没有停止，修复完成判断。
- 如果是重复调用，加入缓存或去重。
- 如果是任务定义问题，说明为什么需要更多步骤。
- 如果是合理长任务，不能简单提高上限掩盖效率问题，要记录总耗时和调用次数。

### 2.4 运行要求

先单例复跑失败任务：

```powershell
BLANK_BENCH_API=http://127.0.0.1:8060/api/v1 node evals/benchmarks/coding/run.mjs --mock --task coding-012
BLANK_BENCH_API=http://127.0.0.1:8060/api/v1 node evals/benchmarks/coding/run.mjs --mock --task coding-034
BLANK_BENCH_API=http://127.0.0.1:8060/api/v1 node evals/benchmarks/coding/run.mjs --mock --task coding-035
```

再运行全量：

```powershell
BLANK_BENCH_API=http://127.0.0.1:8060/api/v1 node evals/benchmarks/coding/run.mjs --mock --report evals/benchmarks/coding/last-run.json
```

如果 Windows PowerShell 不识别前缀环境变量，使用：

```powershell
$env:BLANK_BENCH_API="http://127.0.0.1:8060/api/v1"
node evals/benchmarks/coding/run.mjs --mock --report evals/benchmarks/coding/last-run.json
Remove-Item Env:BLANK_BENCH_API
```

### 2.5 Benchmark 验收

必须输出：

- 55 个任务全部结果。
- 失败任务明细。
- completion rate。
- pass rate。
- test pass rate。
- mis-edited files。
- false done count。
- unauthorized high-risk count。
- 平均耗时。
- 平均步骤数。
- 平均 Token。

目标：

- `falseDoneCount = 0`。
- `unauthorizedHighRisk = 0`。
- `testPassRate = 100%`，除非任务明确预期中断。
- 误修改文件必须为 0，或每一项有任务定义解释。
- 真实产品问题不能通过修改判分脚本掩盖。

## 3. P0：npm 真实安装验证

### 3.1 打包

执行：

```powershell
pnpm -C packages/cli test
pnpm -C packages/cli build
pnpm -C packages/cli exec npm pack --dry-run
```

确认 tarball 包含且只包含必要文件：

- `package.json`。
- `README.md`。
- `src/index.mjs`。

不得包含：

- API Key。
- `.env`。
- 用户数据库。
- 本地绝对路径。
- `node_modules`。
- 当前工作区临时文件。

### 3.2 真实安装

必须使用新的临时目录和新的 npm prefix，不能直接使用当前仓库的 workspace 链接：

```powershell
$installRoot = Join-Path $env:TEMP "blank-ai-v158-install"
$npmPrefix = Join-Path $installRoot "npm-prefix"
New-Item -ItemType Directory -Force $installRoot, $npmPrefix | Out-Null
pnpm -C packages/cli exec npm pack --pack-destination $installRoot
npm install --global --prefix $npmPrefix (Join-Path $installRoot "blank-ai-cli-0.1.0.tgz")
```

验证：

```powershell
& (Join-Path $npmPrefix "blank-ai.cmd") --help
& (Join-Path $npmPrefix "blank-ai.cmd") doctor
```

必须确认：

- 命令不是从仓库源码执行。
- `bin` 路径指向安装目录。
- `--help` 成功。
- `doctor` 不崩溃。
- 缺少配置时给出可操作提示。
- 不打印密钥。

### 3.3 真实启动

在临时用户目录启动：

```powershell
$workspace = Join-Path $installRoot "workspace"
New-Item -ItemType Directory -Force $workspace | Out-Null
& (Join-Path $npmPrefix "blank-ai.cmd") start --workspace $workspace
```

验证：

- 进程启动。
- 服务监听地址正确。
- 页面可以访问。
- `BLANK_SOURCE_ROOT` 未设置时行为明确。
- 关闭后没有孤儿进程。
- 用户工作区没有被修改成项目源码目录。

如果 CLI 只能拉起开发源码，必须明确记录为“开发模式”，不能宣传为生产安装完成。

## 4. P0：Desktop 包真实启动

### 4.1 包构建检查

执行：

```powershell
pnpm runtime:desktop
pnpm verify:desktop
```

确认：

- 包内 Python 可执行。
- 后端源码完整。
- Python site-packages 完整。
- 前端静态站完整。
- `main.cjs` 与当前仓库一致。
- `shell-policy.cjs` 与当前仓库一致。
- `preload.cjs` 存在。
- 托盘图标存在。

### 4.2 真实启动测试

在没有设置项目 `.venv`、没有依赖系统 uv 的环境中测试：

- 启动 Windows unpacked 版本。
- 启动安装版或便携版。
- 等待后端健康检查。
- 打开工作台页面。
- 查看启动日志。
- 检查用户数据目录。

必须记录：

- 启动耗时。
- 后端启动耗时。
- 首屏耗时。
- 数据目录位置。
- 日志位置。
- 是否依赖系统 Python。
- 是否依赖当前仓库。

如果本机无法模拟干净环境，使用明确的隔离目录和 PATH 验证，不得只说“包结构检查通过”。

## 5. P0：真实 GUI 任务闭环

### 5.1 准备临时项目

创建独立临时项目，至少包含：

```text
sample-project/
├── package.json 或 pyproject.toml
├── src/
├── tests/
└── README.md
```

其中故意放入一个可修复的测试失败。

### 5.2 开发场景

在 GUI 中执行：

```text
读取项目结构，找到失败测试，修复实现代码，不要修改测试文件，然后运行测试并报告结果。
```

必须观察：

- Agent 是否读取了真实文件。
- 是否修改了正确的实现文件。
- 是否没有修改测试文件。
- 是否实际运行测试。
- 测试失败后是否自动修复。
- 最终 diff 是否正确。
- 最终回复是否简洁且包含证据。

### 5.3 文档场景

验证：

- 输入文档目标。
- 输出结构清晰。
- 不主动修改代码。
- 不执行高风险命令。
- 回复只包含文档结果。

### 5.4 日常场景

分别验证：

#### 不记住

- 不写入长期记忆。
- 任务后查询不到习惯记录。
- 不修改 Skill。

#### 记住习惯

- 用户明确表达偏好。
- Agent 写入一条可查看记忆。
- 下一次日常任务能使用该偏好。
- 开发场景不能自动读取该记忆。
- 删除后不再生效。

### 5.5 模型切换

准备两个模型配置：

1. 模型 A。
2. 模型 B。

在 UI 切换并分别发送请求，检查：

- 请求中的真实模型 ID。
- Provider 请求日志。
- 任务事件。
- Evidence。
- 页面显示的实际模型。

不能只检查下拉框文字变化。

### 5.6 逆向实验室

验证：

- 上传或选择一个测试目标。
- 目标状态正确显示。
- 调查请求确实发送。
- 无目标时给可操作提示。
- 分析失败时显示真实错误。
- 不把“候选目标”说成“已确认目标”。

## 6. P1：模型路由和上下文性能

### 6.1 模型路由

为以下任务分别验证路由：

- 简单问答。
- 快速文件修改。
- 多文件重构。
- 测试失败修复。
- 长文档。
- 逆向分析。
- 图片分析。

每次记录：

- 候选模型。
- 实际模型。
- 路由原因。
- 耗时。
- Token。
- 是否 fallback。

### 6.2 上下文

构造长任务验证：

- 不重复读取同一文件。
- 工具输出过长时进入 Artifact。
- 摘要保留关键事实。
- 不把日常记忆带入开发任务。
- 任务恢复后知道已完成步骤。

## 7. P1：发布前安全检查

检查以下内容不得进入 Git 或 npm 包：

```powershell
git grep -n -I -E "(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]+|npm_[A-Za-z0-9]+|OPENAI_API_KEY=|ANTHROPIC_API_KEY=)" -- ':!docs/REA-WAVE4-CODEX-RUNTIME.md'
```

检查：

- `.env`。
- 数据库。
- 私钥。
- npm token。
- GitHub token。
- 日志中的 Authorization。
- 打包产物中的工作区绝对路径。

### 用户数据安全

- 数据库写用户目录。
- Secret key 写用户目录。
- 安装目录升级不覆盖用户配置。
- 卸载不删除用户工作区。
- 日志默认不包含完整本机路径和敏感信息。

## 8. 完整回归

执行：

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

后端至少执行：

```powershell
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_delivery_snapshots_v153.py services/api/tests/test_model_switch_v153.py services/api/tests/test_real_file_e2e_v153.py services/api/tests/test_scenarios_v153.py
```

Benchmark 必须执行单例失败项和全量结果，并提交 `last-run.json` 的真实摘要。

## 9. 最终交付报告

```md
# v158 发布闭环报告

## 自动化测试
- Desktop：
- Web：
- Lint：
- TypeScript：
- Evals：
- Backend：
- CLI：

## Benchmark
- 总任务数：
- 完成率：
- 通过率：
- 测试通过率：
- 误修改文件数：
- 错误声称完成：
- 未授权高风险操作：
- 失败任务：

## npm 真实安装
- tarball：
- 干净 prefix 安装：
- help：
- doctor：
- start：
- 服务启动：

## Desktop 真实运行
- 包内 Python：
- 无系统 Python：
- 后端启动：
- 页面打开：
- 用户数据目录：

## GUI 端到端
- 开发场景：
- 文档场景：
- 日常不记住：
- 日常记住：
- 模型切换：
- 真实文件修改：
- 测试失败修复：
- 逆向实验室：

## 未完成
- 必须真实列出

## 发布结论
- 只有真实安装、真实 GUI、真实模型请求、真实文件修改和 Benchmark 结果都合格，才允许写“可以发布”。
```

## 10. 完成标准

本阶段只有全部满足才算完成：

1. Benchmark 失败项都有真实原因和修复结果。
2. 不通过修改判分逻辑掩盖产品缺陷。
3. npm tarball 可以在干净 prefix 安装。
4. 安装后的 CLI 可以执行 help、doctor 和启动流程。
5. Desktop 包可以在隔离环境启动。
6. 真实 GUI 可以完成一次本地代码读取、修改、测试和交付。
7. 模型切换会改变真实请求模型。
8. 日常记忆开关真实控制记忆写入。
9. 逆向实验室真实请求可以运行。
10. 所有自动化测试通过。
11. 工作区没有未解释修改。
12. 未完成前不得报告“全部实现”或“可以发布”。
