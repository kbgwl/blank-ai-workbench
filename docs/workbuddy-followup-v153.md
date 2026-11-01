# WorkBuddy 继续任务书：v0.8.17 验证问题修复与发布验收

工作目录：`E:\projects\kbgai\kbgai`

本任务书是上一阶段完成后的继续任务。不要重新设计一套产品，也不要重复堆页面。当前目标是修复真实验证中暴露的问题，并完成可发布闭环。

## 一、当前已知事实

上一轮验证结果如下：

| 项目 | 结果 |
|---|---|
| 后端新增回归测试 | 77 passed |
| Web 测试 | 257 passed |
| TypeScript 类型检查 | 通过 |
| 版本检查 | 通过，0.8.17 |
| Evals | 10 passed |
| Web lint | 命令成功，但有 2 个 warning |
| Desktop 测试 | 失败，6 个 main-wiring 测试超时 |

当前不能声称“全部完成”。必须先处理以下阻塞：

1. `pnpm test:desktop` 的 6 个 `main-wiring` 测试超时。
2. Web lint 中的 2 个 `setState in effect` warning。
3. 模型切换、场景切换、真实文件修改还没有完成端到端验收。
4. npm 安装和桌面零依赖启动还没有在干净环境完成验证。

## 二、执行纪律

### 2.1 先读取，后修改

开始前必须读取：

- `desktop/main.cjs`
- `desktop/tests/main-wiring.test.cjs`
- `desktop/shell-policy.cjs`
- `desktop/api-base.cjs`
- `web/src/components/skills/skills-view.tsx`
- `web/src/components/tasks/plan-panel.tsx`
- 场景选择器相关组件和测试
- 模型配置、模型路由和请求发送相关代码
- `packages/cli/package.json`
- `README.md`
- `docs/deployment.md`

同时执行：

```powershell
git status --short
git log -5 --oneline
pnpm check:versions
```

不得执行：

- `git reset --hard`
- `git checkout --` 覆盖用户文件
- 未经确认删除目录
- 为了让测试通过而删除或跳过测试
- 将超时时间无限增大来掩盖启动死锁

### 2.2 每项任务的完成标准

每项任务必须同时提供：

- 修改文件。
- 修改原因。
- 自动化测试。
- 实际运行结果。
- 是否影响已有 API 或页面。
- 是否需要数据库迁移。

只修改测试、不修复实际逻辑，不算完成。

## 三、P0：修复 Desktop main-wiring 超时

### 3.1 复现命令

先单独复现，不要直接跑全部桌面测试：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="第二实例信号"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
```

然后运行完整命令：

```powershell
pnpm test:desktop
```

### 3.2 重点排查路径

按顺序检查：

1. `app.requestSingleInstanceLock()` 是否正确返回。
2. `app.whenReady()` 是否进入回调。
3. `ensureBackend()` 是否阻塞。
4. `backendReachable()` 是否使用了测试注入的健康检查地址。
5. `migrateDatabase()` 是否在测试环境执行了过长操作。
6. `startBackend()` 是否等待不存在的后端进程。
7. `createTray()` 是否在托盘禁用时阻塞。
8. `createWindow()` 是否被后端启动阻塞。
9. `registerGlobalShortcut()` 是否抛异常后没有继续启动。
10. `checkForUpdates()` 是否没有返回或没有记录最终决策。

### 3.3 必须增加启动阶段日志

为主进程增加可测试的阶段日志，至少包含：

```text
single-instance: acquired
app: ready
backend: health-check-start
backend: reused / migration-start / spawn-start / ready / failed
tray: ready / disabled / failed
window: created
shortcut: registered / unavailable / failed
update: skipped / checked / failed
startup: complete
```

日志不能包含 API Key、密码、Token 或完整用户路径。

### 3.4 后端启动规则

修复要求：

- 如果已有后端健康检查成功，直接复用，不执行数据库迁移和重新启动。
- 数据库迁移必须有明确超时，例如 10 秒或可配置值。
- 迁移超时后必须记录失败原因，并继续决定是否启动后端。
- 健康检查必须有 AbortController 超时。
- 后端启动超时必须终止子进程，不能留下孤儿进程。
- `BLANK_BACKEND_CMD`、`.venv`、uv、系统 Python 的优先级必须有测试。
- 测试环境可以注入 fake backend，不得依赖真实 Python 服务。

### 3.5 更新检查规则

更新检查不能阻塞主窗口：

- 更新检查必须异步执行。
- 超时、500、非法 JSON、同版本都必须最终返回。
- 未配置更新地址时必须快速跳过。
- 不允许在启动阶段下载更新包。
- 不允许更新检查失败导致窗口无法创建。

### 3.6 托盘和窗口规则

必须验证：

- 托盘可用：关窗进入托盘，不退出进程。
- 点击托盘菜单退出：才真正退出。
- 托盘不可用：关窗直接退出，不留下不可见进程。
- 第二实例：不创建第二个窗口、不启动第二个后端，并唤回第一个窗口。
- 窗口创建失败：有明确日志和退出路径。

### 3.7 全局快捷键规则

必须验证：

- 注册成功：快捷键能显示/隐藏窗口。
- 快捷键被占用：应用仍能正常启动。
- 注册抛异常：应用仍能正常启动。
- 关闭窗口后快捷键不会访问已销毁窗口。
- 退出应用时必须注销快捷键。

### 3.8 Desktop 验收标准

以下命令必须全部通过：

```powershell
node --test desktop/tests/main-wiring.test.cjs
pnpm test:desktop
```

并且不能通过修改测试超时时间、跳过测试、删除测试来达成。

## 四、P0：修复 Web lint warning

当前 warning：

```text
web/src/components/skills/skills-view.tsx:54
web/src/components/tasks/plan-panel.tsx:48
```

### 4.1 skills-view.tsx

检查 `useEffect` 中调用 `load()` 的方式：

- 避免组件每次渲染重复创建请求。
- 使用稳定的 callback 或请求层缓存。
- 组件卸载后不能继续 setState。
- 请求失败必须显示错误状态。
- 竞态请求不能用旧结果覆盖新结果。

### 4.2 plan-panel.tsx

检查任务变化导致的计划状态同步：

- 不要在 effect 中无条件同步多个状态导致级联渲染。
- 如果状态完全由 `task` 推导，优先使用派生值。
- 如果确实需要本地编辑状态，必须比较 revision 后再更新。
- 切换任务时不能显示上一个任务的计划。
- 外部刷新计划时不能覆盖用户正在编辑的内容，除非 revision 变化。

### 4.3 Lint 验收

```powershell
pnpm lint:web
```

目标：

- 0 errors。
- 0 warnings。
- 不能通过关闭规则或添加全局 ignore 掩盖问题。

## 五、P0：真实验证模型切换

### 5.1 前端行为

顶部模型选择器必须：

- 能显示当前模型。
- 能展开可用模型列表。
- 能切换模型。
- 切换后保留当前输入内容。
- 当前对话显示实际使用的模型。
- 模型不可用时显示真实错误。

### 5.2 后端行为

验证：

- 前端选择的模型 ID 传到 API。
- API 将模型 ID 传给模型路由层。
- 实际 Provider 请求使用该模型。
- ToolCall、事件和证据中记录实际模型。
- 重试或 fallback 时记录原模型和备用模型。
- 不允许 UI 显示 A，实际请求使用 B 而不说明。

### 5.3 自动化测试

至少增加：

1. UI 切换模型后请求参数变化。
2. 当前模型不可用时 fallback。
3. fallback 失败时返回明确错误。
4. 模型切换不丢失对话上下文。
5. 多轮任务中模型配置保持一致，除非用户主动切换。

## 六、P0：真实验证场景切换

默认场景：

- 开发
- 文档
- 日常
- 头脑风暴
- 逆向实验室

场景切换必须同步更新：

- 当前 Prompt。
- 默认模型。
- 可用工具。
- 输入框提示。
- 输出规则。
- 文件修改权限。
- 是否启用长期记忆。

验收：

- 开发场景可以读写代码并运行测试。
- 文档场景默认输出文档结构，不主动修改代码。
- 日常场景显示“记住习惯/不记住”。
- 头脑风暴场景不自动执行高风险操作。
- 逆向实验室可以上传/选择目标并展示分析状态。
- 切换场景不丢输入，不误改当前运行任务。

## 七、P1：分页和长任务性能

重点检查以下代码是否存在无限 `.all()`：

- ToolCall 查询。
- AgentEvent 查询。
- Evidence 构建。
- Artifact 引用查询。
- 文件变更列表。
- 任务时间线。
- 事件压缩和孤儿清理。

要求：

- 默认分页大小不超过 100。
- 设置最大页大小，例如 500。
- API 返回 `items`、`page`、`page_size`、`has_more`。
- 时间线支持按游标增量拉取。
- 大型 Artifact 不进入普通 JSON 响应。
- 前端长列表使用虚拟滚动或分段加载。
- 查询必须有数据库索引。

验收：

- 生成 10 万条事件时 API 不一次性加载全部数据。
- 页面首屏只加载当前需要的数据。
- 翻页不会重复或丢失事件。
- 长任务运行时内存不会持续线性增长。

## 八、P1：真实文件执行端到端测试

创建临时测试工作区，验证完整流程：

1. 创建一个真实 Python 或 Node 项目。
2. 让 Agent 读取文件。
3. 让 Agent 修改文件。
4. 让 Agent 执行测试。
5. 故意制造测试失败。
6. 让 Agent 定位失败原因。
7. 让 Agent 修复文件。
8. 再次运行测试。
9. 检查 diff、事件和 Artifact。
10. 取消任务并确认不再有后续写操作。

必须验证：

- 文件确实被修改。
- 不是只返回了模拟结果。
- 测试命令确实执行。
- 失败结果没有被包装成成功。
- 最终回复只报告真实结果。

## 九、P1：npm 和桌面安装验收

### 9.1 CLI

确认：

```powershell
pnpm -C packages/cli test
pnpm -C packages/cli build
```

验证：

- npm 包包含必要文件。
- `bin` 入口可执行。
- `blank-ai --help` 正常。
- `blank-ai` 可以拉起本地服务或打开工作台。
- API Key 不写入日志。

### 9.2 干净目录安装

使用临时目录，不要污染当前项目：

- 安装 CLI。
- 启动 CLI。
- 打开页面。
- 配置模型。
- 创建对话。
- 执行一次文件读取和修改。
- 运行测试。
- 检查卸载不会删除用户工作区。

### 9.3 Desktop 包

必须确认：

- 是否真的内置 Python runtime。
- 是否真的包含 API 源码和依赖。
- 没有系统 Python 时能否启动。
- 首次启动数据库迁移是否成功。
- 升级后数据库和密钥是否保留。
- Windows 安装版和便携版都能启动。

如果尚未做到无 Python 启动，必须在 README 中明确写“当前仍依赖 Python”，不能宣传为零依赖。

## 十、P1：回复质量验收

成功任务只能输出：

```text
已完成。

修改：
- 文件列表

验证：
- 测试结果
```

失败任务只能输出：

```text
未完成。

原因：
- 具体原因

已完成：
- 已经实际完成的部分

下一步：
- 可执行建议
```

禁止：

- 无实际动作的过程播报。
- 长篇重复计划。
- 没有证据却说“完成”。
- 测试未执行却说“验证通过”。
- 把 warning、skip、pending 说成 pass。

增加回复快照测试，至少覆盖：

- 成功。
- 失败。
- 等待授权。
- 等待用户回答。
- 测试跳过。
- 测试未验证。
- 部分完成。

## 十一、最终测试矩阵

WorkBuddy 结束前必须执行并记录：

```powershell
pnpm check:versions
pnpm test:web
pnpm typecheck:web
pnpm lint:web
pnpm test:evals
pnpm test:desktop
```

后端至少执行：

```powershell
pnpm test:api -- --collect-only
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_artifacts_lifecycle_v152.py services/api/tests/test_event_usage_lifecycle_v152.py services/api/tests/test_evidence_paging_v152.py services/api/tests/test_model_retry_fallback_v152.py services/api/tests/test_rate_limit_config_v152.py services/api/tests/test_run_tests_detect_v152.py services/api/tests/test_worker_recovery_v152.py
```

如果完整后端测试耗时较长，可以分组执行，但必须记录全部分组结果，不能只报新增测试。

## 十二、最终交付报告格式

最终只允许按以下格式汇报：

```md
# v0.8.17 验证报告

## 已完成
- ...

## 测试结果
- 命令：...
- 结果：...

## 未通过
- ...

## 未验证
- ...

## 修改文件
- ...

## 是否可以发布
- 可以 / 不可以
- 原因：...

## 下一步
- ...
```

只有以下条件全部满足，才允许说“可以发布”：

- Desktop 测试全部通过。
- Web lint 0 warning、0 error。
- 模型切换经过真实请求验证。
- 场景切换经过真实 UI 验证。
- 本地文件修改和测试执行经过端到端验证。
- npm CLI 在干净目录安装启动成功。
- 桌面安装包已经确认运行时依赖。
- Git 工作区干净。
