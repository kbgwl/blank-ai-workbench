# WorkBuddy v156：Desktop 最终失败项修复任务书

工作目录：`E:\projects\kbgai\kbgai`

## 0. 当前验证事实

最新提交：

```text
89fd20e v155：真实主进程修复
```

最新验证：

```text
pnpm test:desktop
57 tests
52 passed
5 failed
```

当前仍失败：

1. 启动阶段日志：测试模式下实际记录 `disabled`，测试期望 `reused`。
2. 真实模式启动：迁移和 spawn 后没有进入 `started`，测试超时。
3. 健康后端复用：健康桩可达时仍然被判定为不可达并尝试 spawn。
4. 托盘关窗：托盘可用时 close 事件没有被拦截。
5. 更新非法 JSON：没有输出规定的结构错误文案。

这 5 个问题没有修复前，不得报告全部完成，不得发布。

## 1. 强制要求

### 必须修改真实逻辑

本次必须实际检查和修改：

- `desktop/main.cjs`
- 必要时修改 `desktop/api-base.cjs` 或 `desktop/shell-policy.cjs`
- 必要时修改测试桩，但测试桩必须模拟真实 Electron 行为

不能只修改 `desktop/tests/main-wiring.test.cjs` 来改变期望结果。

### 禁止事项

- 删除测试。
- 跳过测试。
- 把 `assert` 改成宽松判断。
- 只改日志文本而不修复状态逻辑。
- 健康检查失败时无条件返回 `reused`。
- 测试模式无条件返回 `started`。
- 把真实启动超时改成成功。
- 增加超时时间掩盖 spawn 不退出或健康检查不返回。
- 在测试中使用和生产完全不同的核心逻辑。

## 2. 开始前的复现和代码读取

执行：

```powershell
git status --short
git log -5 --oneline
git show --stat --oneline HEAD
Get-Content desktop/main.cjs
Get-Content desktop/tests/main-wiring.test.cjs
Get-Content desktop/api-base.cjs
Get-Content desktop/shell-policy.cjs
```

单独运行：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动阶段日志"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="真实模式：探活耗尽后走迁移\+启动"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="更新骨架"
```

每个失败都要记录：

- 环境变量。
- 健康检查 URL。
- `backendReachable()` 返回值。
- 是否执行迁移。
- spawn 的命令和参数。
- 子进程 stdout/stderr。
- 窗口和托盘状态。
- 最终启动阶段日志。

## 3. 修复健康检查和后端复用

### 3.1 当前问题

健康桩可达时测试仍然失败：

```text
健康桩可达时应复用而不是 spawn
```

这说明至少有一个问题：

- `BLANK_BACKEND_HEALTH` 没有被实际使用。
- `HEALTH_URL` 在环境变量设置前就被缓存。
- 测试 HTTP server 没有收到请求。
- `fetch()` 请求了错误的路径或端口。
- response 处理错误，把 200 当成不可达。
- 测试模式硬闸在健康检查之前返回。

### 3.2 必须增加诊断日志

在不泄露敏感信息的前提下增加：

```text
[startup] backend | health-url | <origin + path>
[startup] backend | health-result | status=<number> ok=<true/false>
[startup] backend | health-error | <error type>
```

不能记录 API Key、Authorization、Cookie 或完整敏感 URL 查询参数。

### 3.3 健康检查规则

`backendReachable()` 必须满足：

1. 优先使用 `BLANK_BACKEND_HEALTH`。
2. 未设置时才使用 `${API_BASE}/api/v1/health`。
3. 每次调用读取当前有效配置，避免模块初始化时缓存旧值。
4. HTTP 200-299 返回 true。
5. 其他状态返回 false。
6. 网络错误返回 false。
7. 超时返回 false。
8. 无论成功或失败都清理 timer。
9. 不发起多余的重复探活。

### 3.4 `ensureBackend()` 正确顺序

生产行为必须是：

```text
health check
├─ 成功 → reused，不迁移，不 spawn
└─ 失败
   ├─ 测试模式 → disabled，不迁移，不 spawn
   └─ 真实模式 → migrate → spawn → wait health → started/failed
```

伪代码：

```js
async function ensureBackend() {
  const reachable = await backendReachable();

  if (reachable) {
    shellState.backendOnline = true;
    logStartup("backend", "reused");
    return { ok: true, mode: "reused" };
  }

  if (isShellTestMode()) {
    shellState.backendOnline = false;
    logStartup("backend", "disabled", "测试模式不拉起真实后端");
    return { ok: true, mode: "disabled" };
  }

  return startManagedBackend();
}
```

不能让测试模式在健康桩可达时直接进入 disabled。健康检查必须优先。

### 3.5 验收

必须增加行为断言：

- 健康桩 200 时，收到 `/health` 请求。
- 健康桩 200 时，spawn 次数为 0。
- 健康桩 200 时，日志包含 `backend | reused`。
- 健康桩不可达 + 测试模式，日志包含 `backend | disabled`。
- 健康桩不可达 + 真实模式，才会迁移和 spawn。

## 4. 修复真实模式启动超时

### 4.1 当前失败

测试：

```text
真实模式：探活耗尽后走迁移+启动，后端就绪记 started
```

实际日志停在：

```text
[startup] backend | migration-start
[startup] backend | spawn-start
[backend] 启动方式 BLANK_BACKEND_CMD → python
```

没有出现：

```text
backend | started
startup | complete
```

### 4.2 重点检查

检查 `resolveBackendCommand()` 返回值：

```js
{
  command,
  args,
  cwd,
  env,
  label
}
```

重点确认：

- `BLANK_BACKEND_CMD` 是命令名还是完整命令行。
- spawn 是否把参数错误地重复拼接。
- `cwd` 是否是可用目录。
- `BLANK_BACKEND_PORT` 是否与健康检查端口一致。
- 测试桩 Python 是否真的会启动 HTTP health server。
- stdout/stderr 监听是否导致异常。
- child 的 `error`、`exit`、`close` 事件是否都能结束等待。
- `backendReachable()` 是否探测了错误端口。

### 4.3 迁移要求

数据库迁移不是启动成功的必要条件，不能无限阻塞主窗口。

要求：

- 迁移必须有明确超时。
- 迁移失败必须记录原因。
- 迁移完成后才 spawn，或者根据现有架构明确允许并行，但必须可验证。
- 迁移进程必须被正确回收。
- migration 子进程不能继承会阻塞测试的 stdin。
- migration 的 stdout/stderr 必须被消费。

### 4.4 spawn 要求

- 传入数组参数，不要拼接 shell 字符串。
- Windows 下设置 `windowsHide: true`。
- 监听 `error`、`exit`、`close`。
- 后端进程退出后立即标记 offline。
- 启动等待有最大时间。
- 超时后 kill/terminate 子进程。
- 不得留下孤儿进程。

### 4.5 健康等待要求

启动子进程后：

```text
循环探活
├─ 200 → started
├─ 子进程已退出 → failed
├─ 超时 → kill + failed
└─ 继续等待
```

必须记录：

```text
[startup] backend | started
```

不能只记录普通 `[backend] 后端就绪`，因为测试和日志消费者需要稳定的启动阶段事件。

### 4.6 验收

- 真实模式测试可以完成，不超时。
- 迁移和 spawn 都被执行。
- 测试桩后端确实被健康检查探测到。
- 最终状态为 `started`。
- 进程退出或超时时状态为 `failed`。
- 失败后窗口仍有可操作的错误状态。

## 5. 修复启动阶段日志

### 5.1 当前失败

日志是：

```text
[startup] backend | disabled | 测试模式不拉起真实后端
```

但启动阶段日志测试期望 `reused`。

不要简单把 `disabled` 文本改成 `reused`。必须先保证：

- 健康桩确实可达时记录 reused。
- 健康桩不可达时记录 disabled。

### 5.2 日志状态定义

| 状态 | 条件 |
|---|---|
| reused | 已有后端健康检查成功 |
| disabled | 测试模式禁止拉起，且已有后端不可达 |
| started | 本进程启动后端并健康检查成功 |
| failed | 真实模式启动或健康检查失败 |

启动日志必须始终有：

```text
single-instance
app ready
tray
backend
window
shortcut
update
startup complete
```

## 6. 修复托盘关窗

### 6.1 当前失败

测试：

```text
关窗路由：托盘可用时藏进托盘，托盘菜单「退出」后放行
```

仍然报：

```text
托盘可用时 ✕ 必须被拦下
```

### 6.2 必须验证状态时机

在 `createTray()` 成功后立即设置：

```js
shellState.trayAvailable = true;
logStartup("tray", "ready");
```

失败时：

```js
shellState.trayAvailable = false;
logStartup("tray", "disabled", reason);
```

不能先创建窗口，再因为异步托盘初始化太晚导致 close 事件读取到 false。

### 6.3 close 事件目标行为

```js
function handleMainWindowClose(event) {
  if (shellState.explicitQuit) return;
  if (!shellState.trayAvailable) return;

  event.preventDefault();
  mainWindow.hide();
}
```

必须确保：

- 事件监听注册在真实 `mainWindow` 上。
- 不是注册在临时窗口对象上。
- 没有另一个监听器覆盖行为。
- `explicitQuit` 初始值为 false。
- 托盘退出菜单设置 explicitQuit 后才调用 app.quit。

### 6.4 验收

- 托盘可用关闭窗口：preventDefault true。
- 窗口 hide 被调用。
- 窗口没有 destroy。
- 进程不退出。
- 点击托盘显示窗口后 show/focus 被调用。
- 点击托盘退出后 app.quit 被调用。
- 托盘不可用时关闭窗口直接退出。

## 7. 修复更新 feed 结构错误

### 7.1 当前失败

返回：

```json
{"foo": 1}
```

测试要求日志包含：

```text
feed JSON 既不是条目数组，也不是 entries 数组
```

### 7.2 解析契约

支持：

```json
[
  {"version":"1.0.1"}
]
```

或：

```json
{
  "entries": [
    {"version":"1.0.1"}
  ]
}
```

拒绝：

```json
{"foo":1}
[]
{"entries":{}}
```

对拒绝情况返回结构化原因，不要只返回 null：

```js
{
  entries: null,
  reason: "feed JSON 既不是条目数组，也不是 entries 数组"
}
```

### 7.3 日志契约

要求：

- 非法 JSON：记录 JSON 解析失败。
- 对象缺少 entries：记录“既不是条目数组，也不是 entries 数组”。
- entries 不是数组：记录相同结构错误。
- 不输出完整 feed 内容。
- 不下载、不安装。

### 7.4 验收

- `garbage` 测试通过。
- 500 测试通过。
- 同版本测试通过。
- 未配置测试通过。
- 所有分支 `install` 为 false。
- 更新检查不阻塞窗口创建。

## 8. 测试桩要求

测试桩必须模拟真实行为，而不是绕过实现：

- fake health server 必须真正监听端口。
- `fetch` 必须真正请求 fake server。
- fake spawn 必须触发 stdout、health ready、exit 等事件。
- fake BrowserWindow 必须支持 hide、show、focus、restore、isVisible、isMinimized。
- fake app 必须记录 `quit` 和 `whenReady`。
- fake globalShortcut 必须记录 register/unregister。

如果测试桩与真实 Electron API 不一致，优先修测试桩的 API 形状，不要把生产代码改成只适配测试桩的写法。

## 9. 局部测试命令

修复过程中必须逐项执行：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动阶段日志"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="真实模式：探活耗尽后走迁移\+启动"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="更新骨架"
```

每个命令必须显示目标测试通过，不能只运行整个文件后忽略单项结果。

## 10. 完整回归命令

```powershell
pnpm test:desktop
pnpm lint:web
pnpm test:web
pnpm typecheck:web
pnpm test:evals
pnpm check:versions
git diff --check
git status --short
```

后端核心测试：

```powershell
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_delivery_snapshots_v153.py services/api/tests/test_model_switch_v153.py services/api/tests/test_real_file_e2e_v153.py services/api/tests/test_scenarios_v153.py
```

## 11. 最终报告格式

```md
# v156 验收报告

## 实际修改
- desktop/main.cjs：...
- desktop/tests/main-wiring.test.cjs：...
- 其他文件：...

## Desktop 结果
- 总数：
- 通过：
- 失败：

## 五项修复结果
- 后端健康检查与复用：通过/失败
- 真实模式启动 started：通过/失败
- 启动阶段日志：通过/失败
- 托盘关窗：通过/失败
- 更新 feed 结构错误：通过/失败

## 完整回归
- lint：
- Web：
- typecheck：
- evals：
- versions：

## 未完成和风险
- 必须真实列出

## 发布结论
- 只有 `pnpm test:desktop` 57 passed、0 failed 才能写“可以发布”。
```

## 12. 最终完成标准

全部满足才算完成：

1. `desktop/main.cjs` 有真实代码修复。
2. `pnpm test:desktop` 达到 `57 passed, 0 failed`。
3. 健康后端复用时不会 spawn。
4. 测试模式和真实模式状态语义正确。
5. 真实模式能够完成 migration、spawn、health ready、started。
6. 托盘可用时关窗会被拦截并隐藏。
7. 显式退出可以真正退出。
8. 非法 feed 有明确结构错误日志。
9. Web lint 为 0 warning、0 error。
10. 完整回归通过。
11. 未达到以上条件，不得报告“全部实现”。
