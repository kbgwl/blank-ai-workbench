# WorkBuddy v155：真实 Desktop 主进程修复任务

工作目录：`E:\projects\kbgai\kbgai`

## 0. 任务结论

当前项目不是全部完成状态。

最新验证结果：

```text
pnpm test:desktop
55 tests
51 passed
4 failed
```

当前最新提交 `82861b9` 只修改了：

- `desktop/tests/main-wiring.test.cjs`
- `docs/workbuddy-v154-desktop-fix.md`

但没有修改 `desktop/main.cjs`。因此本次任务必须修改真实 Desktop 主进程逻辑，不能继续只调整测试时序。

最终目标：

```text
pnpm test:desktop
55 passed
0 failed
```

在达到目标前，不得报告“全部完成”，不得报告“可以发布”。

## 1. 强制规则

### 1.1 不允许的做法

- 不得删除失败测试。
- 不得跳过失败测试。
- 不得把断言改弱。
- 不得把失败状态改成无条件成功。
- 不得只增加等待时间来掩盖竞态或死锁。
- 不得只修改 `desktop/tests/` 而不修改真实实现。
- 不得伪造 `backend reused` 日志。
- 不得在测试桩中写死主进程应该返回的结果。
- 不得修改测试环境使健康检查永远返回成功。

### 1.2 必须做到的事情

- 先读完整 `desktop/main.cjs`。
- 先复现每个失败并记录日志。
- 每个失败都要找到真实原因。
- 修改 `desktop/main.cjs` 或其实际依赖模块。
- 为修复增加行为测试，而不是只改断言。
- 保留已有通过的 51 个测试。
- 修复后运行完整 Desktop 测试和完整回归。

## 2. 开始前检查

执行：

```powershell
git status --short
git log -5 --oneline
git show --stat --oneline HEAD
git diff HEAD~1 -- desktop/main.cjs
Get-Content desktop/main.cjs
Get-Content desktop/tests/main-wiring.test.cjs
```

确认当前工作区是否干净。若有用户修改，不得覆盖。

复现四个失败：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动阶段日志"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="更新骨架"
```

## 3. 修复一：后端启动状态和健康检查

### 3.1 当前失败

实际日志：

```text
[startup] backend | health-check-start
[startup] backend | failed | 测试模式不拉起后端
```

测试要求：

- 健康检查成功：`reused`。
- 测试模式明确不启动真实后端：`disabled`。
- 真实启动失败：`failed`。
- 当前主进程启动完成：`startup | complete`。

不能把三种状态混为一个 `failed`。

### 3.2 修改范围

重点检查和修改：

- `desktop/main.cjs` 的 `ensureBackend()`。
- `desktop/main.cjs` 的 `backendReachable()`。
- `desktop/main.cjs` 的 `startBackend()`。
- `desktop/main.cjs` 的 `migrateDatabase()`。
- `BLANK_SHELL_TEST` 分支。
- 后端启动阶段日志函数。

### 3.3 目标逻辑

`ensureBackend()` 必须按以下顺序执行：

```text
1. 健康检查已有后端
2. 如果 HTTP 200：返回 reused
3. 如果是测试模式且健康检查失败：返回 disabled
4. 如果是真实模式：执行迁移
5. 启动后端
6. 等待健康检查
7. 成功返回 started
8. 超时返回 failed，并回收进程
```

伪代码：

```js
async function ensureBackend() {
  logStartup("backend", "health-check-start");

  if (await backendReachable()) {
    shellState.backendOnline = true;
    logStartup("backend", "reused");
    return { ok: true, mode: "reused" };
  }

  if (process.env.BLANK_SHELL_TEST === "1") {
    shellState.backendOnline = false;
    logStartup("backend", "disabled", "测试模式不拉起真实后端");
    return { ok: true, mode: "disabled" };
  }

  // 真实模式才迁移和启动
  ...
}
```

注意：伪代码只是行为要求，必须根据现有实现整理，不要直接破坏生产逻辑。

### 3.4 健康检查要求

- 使用 `BLANK_BACKEND_HEALTH` 时必须请求该地址。
- 不得无条件拼接 `/api/v1/health` 覆盖注入地址。
- HTTP 200 才是可复用。
- HTTP 500、网络错误、超时都是不可达。
- 每次请求都必须清理 AbortController timer。
- 健康检查不能超过配置的超时时间。
- 健康检查失败不能阻止窗口创建，除非产品明确要求退出。

### 3.5 验收

新增或保留测试：

1. 健康桩 200：spawn 次数为 0，日志包含 `backend | reused`。
2. 健康桩不可达 + 测试模式：不 spawn，日志包含 `backend | disabled`。
3. 真实模式健康桩不可达：进入迁移和启动逻辑。
4. 后端启动超时：终止子进程并记录 `failed`。
5. 不论后端是否可用，都必须完成窗口和启动阶段流程。

## 4. 修复二：托盘可用时正确拦截关窗

### 4.1 当前失败

测试：

```text
关窗路由：托盘可用时藏进托盘，托盘菜单「退出」后放行
```

失败：

```text
托盘可用时 ✕ 必须被拦下
```

### 4.2 目标行为

托盘可用时点击窗口关闭按钮：

- 必须执行 `event.preventDefault()`。
- 必须隐藏窗口。
- 不得销毁窗口。
- 不得退出应用。
- 后端继续运行。
- 托盘菜单可以重新显示窗口。

用户点击托盘菜单“退出”：

- 设置 `shellState.explicitQuit = true`。
- 回收后端。
- 调用 `app.quit()`。
- 后续 close 事件允许放行。

托盘不可用时：

- close 事件不拦截。
- 应用正常退出。

### 4.3 重点检查

- `createWindow()` 中是否注册了 close listener。
- close listener 是否注册到了真正的 `mainWindow`。
- `shellState.trayAvailable` 设置时机是否正确。
- `createTray()` 失败时是否错误地保留 true。
- `explicitQuit` 是否在托盘退出菜单中设置。
- `mainWindow.hide()` 是否被测试桩实现和调用。
- 是否存在多个 close listener 互相覆盖。

### 4.4 推荐实现结构

将关窗逻辑集中：

```js
function onMainWindowClose(event) {
  if (shellState.explicitQuit || !shellState.trayAvailable) return;
  event.preventDefault();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.hide();
  }
}
```

将显式退出集中：

```js
function quitApplication() {
  shellState.explicitQuit = true;
  stopBackend();
  app.quit();
}
```

不要通过修改测试桩的 `preventDefault` 来伪造成功。

### 4.5 验收

- 托盘可用 close：`preventDefault` 为 true。
- close 后窗口不可见但仍存在。
- 托盘菜单显示窗口后窗口可见并聚焦。
- 托盘退出菜单可以真正退出。
- 托盘不可用 close：不调用 `preventDefault`。

## 5. 修复三：更新 feed 非法 JSON 错误日志

### 5.1 当前失败

更新测试中 `garbage` 分支失败，要求日志包含：

```text
feed JSON 既不是条目数组
```

### 5.2 目标行为

更新检查必须只读取和判断元数据，不下载、不安装。

必须区分：

- HTTP 失败。
- JSON 解析失败。
- JSON 结构不合法。
- 没有可解析版本。
- 当前版本或更旧版本。
- 下载地址缺失。
- sha256 缺失。

对于以下响应：

```json
{"foo": 1}
```

必须记录可读错误，至少包含：

```text
feed JSON 既不是条目数组，也不是 entries 数组
```

### 5.3 重点检查

- `checkForUpdates()`。
- `extractFeedEntries()`。
- `decideUpdate()`。
- 更新 fetch 的 JSON 解析分支。
- 更新日志是否把错误原因覆盖成了泛化的“检查失败”。

### 5.4 修改要求

建议将解析错误作为结构化结果返回：

```js
{
  kind: "invalid-feed",
  reason: "feed JSON 既不是条目数组，也不是 entries 数组"
}
```

主进程只负责把 `reason` 写入日志，不要重复拼接多套文案。

禁止输出完整 feed 内容，避免日志泄露外部数据和敏感信息。

### 5.5 验收

- 500：不安装，日志包含 HTTP 状态。
- garbage：不安装，日志包含结构错误。
- 同版本：不安装，日志包含无需更新。
- 未配置：快速跳过，不发请求。
- 所有分支 `install === false`。
- 更新检查不阻塞窗口创建。

## 6. 测试文件要求

允许修改测试以补充行为覆盖，但不允许把失败测试改成弱断言。

必须增加或保留以下测试：

### 后端状态

- `reused`。
- `disabled`。
- `started`。
- `failed`。

### 托盘

- close 拦截。
- close 隐藏。
- tray show 恢复。
- explicit quit 放行。
- tray unavailable 退出。

### 更新

- 500。
- 非法 JSON。
- 非法结构。
- 同版本。
- 未配置。
- 新版本但缺下载信息。

测试必须验证真实可观察行为：

- `preventDefault` 是否调用。
- `hide/show/focus/restore` 是否调用。
- `spawn` 次数。
- `app.quit` 是否调用。
- 日志内容。
- 是否下载或安装。

## 7. 局部验证命令

每修复一个问题立即执行：

```powershell
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动阶段日志"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="启动接线"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="关窗路由"
node --test desktop/tests/main-wiring.test.cjs --test-name-pattern="更新骨架"
```

不能只运行新增的单个测试，必须确认原失败测试本身通过。

## 8. 完整回归命令

全部修复后执行：

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

## 9. 最终交付报告

必须按以下格式输出：

```md
# v155 Desktop 修复报告

## 真实代码修改
- desktop/main.cjs：...
- 其他文件：...

## 四个失败项
- 启动阶段日志：通过/失败
- 健康后端复用：通过/失败
- 托盘关窗：通过/失败
- 更新非法 JSON：通过/失败

## 测试结果
- pnpm test:desktop：xx passed / xx failed
- pnpm lint:web：...
- pnpm test:web：...
- pnpm typecheck:web：...
- 后端核心测试：...

## 是否发布
- 只有 Desktop 55 passed、0 failed 时才允许写“可以发布”。

## 未解决问题
- 必须真实列出，不能写“无”来掩盖未验证项目。
```

## 10. 完成标准

只有满足以下全部条件，v155 才算完成：

1. `desktop/main.cjs` 有真实逻辑修复。
2. `pnpm test:desktop` 为 `55 passed, 0 failed`。
3. 后端状态正确区分 reused、disabled、started、failed。
4. 健康桩可达时不会 spawn。
5. 托盘可用时关闭窗口会被拦截并隐藏。
6. 托盘退出菜单可以真正退出。
7. 非法更新 feed 有明确结构错误日志。
8. 更新检查所有失败分支都不会安装。
9. Web lint 为 0 warning、0 error。
10. 工作区无未解释修改。
11. 未通过前禁止报告“全部实现”。
