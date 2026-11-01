# WorkBuddy v154 任务书：Desktop 5 个失败修复

工作目录：`E:\projects\kbgai\kbgai`

## 任务目标

修复当前 Desktop 测试剩余的 5 个失败。

当前状态：

```text
pnpm test:desktop
55 tests
50 passed
5 failed
```

本次只允许修复真实 Desktop 主进程逻辑和测试桩契约，不允许：

- 删除失败测试。
- 跳过失败测试。
- 放宽断言。
- 无限增加测试超时时间。
- 用假的日志掩盖真实状态。
- 只修改测试、不修改 `desktop/main.cjs`。

最终目标：

```text
pnpm test:desktop
55 passed
0 failed
```

## 一、开始前必须执行

```powershell
git status --short
git log -5 --oneline
Get-Content desktop/main.cjs
Get-Content desktop/tests/main-wiring.test.cjs
Get-Content desktop/shell-policy.cjs
Get-Content desktop/api-base.cjs
```

确认上一轮提交没有未提交修改后，再开始工作。如果工作区有用户修改，必须保留，不得覆盖。

单独复现所有失败：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动阶段日志"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="全局快捷键"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="更新骨架"
```

不要一开始运行完整测试后只看最后一句。每个失败都要保留实际日志和调用顺序。

## 二、失败一：启动阶段日志状态错误

### 现象

测试：

```text
启动阶段日志（§3.3）：阶段行齐全且 startup: complete 收尾
```

实际日志包含：

```text
[startup] backend | health-check-start
[startup] backend | failed | 测试模式不拉起后端
```

测试期望的是后端阶段完成，例如：

```text
[startup] backend | reused
```

### 要求

明确区分以下三种状态：

1. `reused`：健康检查确认已有后端可用。
2. `started`：本进程成功启动了后端并等待健康检查通过。
3. `disabled`：测试模式明确禁止拉起后端，且这是可接受的测试替身状态。
4. `failed`：真实启动失败或健康检查失败。

不能把“测试模式不拉起后端”直接记录为普通失败，因为这会让启动链路看起来像异常。

### 修改建议

检查 `desktop/main.cjs` 中：

- `ensureBackend()`。
- `backendReachable()`。
- `startBackend()`。
- `migrateDatabase()`。
- `BLANK_SHELL_TEST` 相关分支。
- `startupLog()` 或阶段日志方法。

测试模式下：

- 如果健康桩可达，记录 `reused`。
- 如果健康桩不可达且测试明确要求不启动真实 Python，记录 `disabled`，不要记录 `failed`。
- 仍然必须继续创建窗口、注册快捷键、执行更新检查。
- 必须输出 `startup | complete`。

### 验收

- 启动阶段日志测试通过。
- 日志状态不能伪造健康后端。
- `failed` 只用于真实失败。
- `disabled` 必须说明原因。

## 三、失败二：健康桩可达却没有复用后端

### 现象

测试：

```text
启动接线：复用已有后端、托盘就绪、更新骨架只取元数据不下载
```

失败断言：

```text
健康桩可达时应复用而不是 spawn
```

### 要求

当 `BLANK_BACKEND_HEALTH` 指向测试服务器且返回 HTTP 200 时：

- 必须判定后端已存在。
- 不得执行 `migrateDatabase()`。
- 不得调用 `spawn()`。
- 不得修改后端端口。
- 必须记录 `backend | reused`。
- 必须继续创建窗口和托盘。

### 重点排查

1. `HEALTH_URL` 是否在模块加载时读取了旧环境变量。
2. 测试是否在加载 `main.cjs` 前设置环境变量。
3. `backendReachable()` 是否请求了 `/api/v1/health` 而不是注入的 `/health`。
4. `fetch()` 是否因为响应处理方式导致 200 被当成失败。
5. `BLANK_SHELL_TEST` 是否在健康检查之前直接返回了错误状态。
6. `ensureBackend()` 是否先执行迁移再检查复用。
7. 健康检查的 AbortController 是否提前取消请求。

### 修改要求

健康检查必须在 `ensureBackend()` 的第一步执行：

```js
if (await backendReachable()) {
  logStartup("backend", "reused");
  return { ok: true, mode: "reused" };
}
```

不能改成只看 `BLANK_SHELL_TEST` 就返回，也不能无条件返回成功。

### 验收

- 健康桩可达时 `spawn` 调用次数为 0。
- 健康桩不可达时测试模式不启动真实 Python。
- 真实模式下健康检查失败时可以启动后端。
- 启动接线测试通过。

## 四、失败三：托盘可用时关窗没有被拦截

### 现象

测试：

```text
关窗路由：托盘可用时藏进托盘，托盘菜单「退出」后放行
```

失败断言：

```text
托盘可用时 ✕ 必须被拦下
```

### 目标行为

当托盘可用且用户点击窗口关闭按钮时：

- `event.preventDefault()` 必须被调用。
- 窗口隐藏或最小化到托盘。
- 应用进程不能退出。
- 托盘菜单仍然可以显示窗口。

当用户点击托盘菜单“退出”时：

- 设置显式退出状态。
- 调用 `app.quit()`。
- 关闭事件允许继续传播。
- 后端进程被回收。

当托盘不可用时：

- 关闭窗口直接退出。
- 不允许留下不可见且无法操作的进程。

### 重点排查

- `BrowserWindow` 是否注册了 `close` 事件。
- 事件注册是否在 `createWindow()` 之前或之后丢失。
- `shellState.trayAvailable` 是否及时设置为 true。
- 测试桩的 `event` 是否传入正确。
- 判断是否错误使用了 `app.isQuitting`、`explicitQuit` 或旧状态名。
- 是否把“隐藏到托盘”和“窗口销毁”混用了。

### 修改要求

关窗逻辑必须集中到单一函数，例如：

```js
function handleWindowClose(event) {
  if (shellState.explicitQuit || !shellState.trayAvailable) return;
  event.preventDefault();
  mainWindow.hide();
}
```

不要在多个事件监听器里分别判断，避免一个监听器放行、另一个监听器隐藏。

### 验收

- 托盘可用关闭窗口：`preventDefault === true`。
- 窗口仍存在但不可见。
- 托盘显示窗口可以恢复窗口。
- 托盘退出菜单最终可以退出应用。
- 托盘不可用时关闭窗口直接退出。

## 五、失败四：全局快捷键注册后的行为错误

### 现象

测试：

```text
全局快捷键：注册成功时真能唤出/隐藏，被占用与抛异常都降级不崩
```

当前注册逻辑测试部分通过，但主进程接线中实际快捷键行为断言失败。

### 目标行为

快捷键注册成功时：

- 窗口可见且已聚焦：隐藏窗口。
- 窗口隐藏或不可见：显示窗口并聚焦。
- 窗口最小化：先 restore，再 show/focus。
- 窗口不存在：不抛异常，并尝试重新创建或记录降级状态。

快捷键被占用或注册抛异常时：

- 应用继续启动。
- 托盘仍可用。
- 关闭和退出逻辑不受影响。
- 记录明确的降级日志。

退出时：

- 必须调用 `globalShortcut.unregisterAll()` 或对应注销方法。
- 不能留下系统级快捷键占用。

### 重点排查

- `globalShortcut.register()` 的返回值是否被正确处理。
- 测试桩的 callback 是否被保存并实际调用。
- `mainWindow.isVisible()`、`isMinimized()` 是否与桩行为匹配。
- 是否只改变了状态变量，没有调用真实窗口方法。
- 快捷键回调是否闭包引用了旧的 `mainWindow`。
- 窗口关闭后快捷键是否仍然调用已销毁对象。

### 验收

- 成功注册后，调用测试桩快捷键回调，窗口能隐藏。
- 再调用一次，窗口能显示并聚焦。
- 最小化窗口能恢复。
- 注册返回 false 时测试通过且应用不崩溃。
- 注册抛异常时测试通过且应用不崩溃。

## 六、失败五：更新检查非法 JSON 日志不符合契约

### 现象

测试：

```text
更新骨架的失败形态：500 / 版本读不懂 / 同版本 / 未配置都不安装
```

`garbage` 情况失败，因为日志没有包含：

```text
feed JSON 既不是条目数组
```

### 目标行为

更新检查只负责获取和判断元数据，不下载、不安装。

以下情况必须都不安装：

- HTTP 500。
- 返回非法 JSON。
- JSON 不是数组，也不是合法 `{ entries: [...] }`。
- 版本号缺失或非法。
- 当前版本相同。
- 返回更旧版本。
- 下载地址缺失或协议不合法。
- sha256 缺失或格式非法。
- 未配置更新源。

每种失败都必须有稳定、可读、可测试的日志原因。

### 建议统一错误文案

```text
更新检查跳过：未配置更新源
更新检查失败：HTTP 500
更新检查失败：feed JSON 既不是条目数组，也不是 entries 数组
更新检查失败：没有可解析的版本号
更新检查跳过：没有比当前版本更新的版本
更新检查失败：更新条目缺少合法下载地址
更新检查失败：更新条目缺少合法 sha256
```

日志必须避免输出 Token、内部路径和完整响应内容。

### 验收

- garbage 测试通过。
- 500 测试通过。
- 同版本测试通过。
- 未配置测试通过。
- 所有分支 `install` 都为 false。
- 更新检查不会阻塞主窗口启动。

## 七、不要破坏已通过功能

修复 Desktop 时必须保留：

- 单实例锁。
- 第二实例唤回。
- 托盘可用/不可用两种模式。
- GPU 降级逻辑。
- API 地址注入。
- 更新策略纯函数测试。
- 后端失败后的托盘状态。
- 桌面日志脱敏。

每次修改后先跑局部测试，再跑完整测试：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动阶段日志"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="全局快捷键"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="更新骨架"
pnpm test:desktop
```

## 八、最终回归

Desktop 通过后执行：

```powershell
pnpm check:versions
pnpm test:web
pnpm typecheck:web
pnpm lint:web
pnpm test:evals
pnpm test:desktop
git diff --check
git status --short
```

后端核心测试至少执行：

```powershell
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_delivery_snapshots_v153.py services/api/tests/test_model_switch_v153.py services/api/tests/test_real_file_e2e_v153.py services/api/tests/test_scenarios_v153.py
```

## 九、交付报告

必须按以下格式汇报：

```md
# v154 Desktop 修复报告

## 修复内容
- 启动阶段日志：通过/未通过
- 已有后端复用：通过/未通过
- 托盘关窗：通过/未通过
- 全局快捷键：通过/未通过
- 更新检查错误分支：通过/未通过

## 测试结果
- pnpm test:desktop：xx passed / xx failed
- pnpm lint:web：结果
- pnpm test:web：结果
- 后端核心测试：结果

## 修改文件
- 文件路径和修改原因

## 未解决问题
- 如果有，必须写明实际原因

## 是否可以发布
- 只有 Desktop 0 failed 且完整回归通过时才写“可以发布”
```

## 十、完成标准

只有满足全部条件才算完成：

1. `pnpm test:desktop` 为 `55 passed, 0 failed`。
2. 不删除、跳过或弱化任何失败测试。
3. 健康后端可达时确实不 spawn。
4. 测试模式和真实模式的 backend 状态含义清晰。
5. 托盘关窗、显式退出、快捷键行为均经过测试。
6. 更新检查所有失败分支都不安装。
7. `pnpm lint:web` 为 0 warning、0 error。
8. 工作区无未解释的临时文件。
9. 提交信息说明真实修复内容。
10. 在未达到以上条件前，不得声称“全部完成”或“可以发布”。
