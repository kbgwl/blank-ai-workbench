# WorkBuddy 最终返工任务单：v0.11.2 发布阻塞清零

## 1. 当前验证结论

当前不能报告“全部完成”，不能发布。

已经实际通过：

- 前端测试 296/296；
- 业务页面已接入 SelectPopover；
- 输入栏已有场景、模型、权限入口；
- 业务源码没有真正的原生 select；
- 审批代码已有 approval_id、tool_call_id、arguments_hash；
- 自动修复、定时、监控和通知相关代码已有提交。

仍然失败或没有真实证据：

1. 后端自动执行测试长时间卡住；
2. 审批端到端测试没有完整结束；
3. schedule/watch 测试没有完整结束；
4. Clipboard_Screenshot.png 授权后真实读取没有完成；
5. 关闭窗口后任务续跑没有真实证据；
6. pnpm -C web build 仍然在 Turbopack CSS 阶段失败；
7. 真实桌面 GUI 下拉框没有完成截图验收。

本任务优先清除阻塞，不要继续添加新功能，不要只补文档。

---

## 2. 严格规则

1. 先复现失败，再修改；
2. 不允许删除测试、降低断言、跳过测试或无限延长超时；
3. 不允许把前端调用 /run 当作后端自主执行；
4. 不允许把 mock 测试当作真实 GUI 通过；
5. 不允许把“字段存在”当作“功能闭环”；
6. 所有长测试必须设置明确超时并打印诊断；
7. 所有命令必须记录退出码；
8. 没有真实验证的项目必须写“未验证”；
9. 不覆盖已有用户改动；
10. schedule/watch 必须明确属于当前版本还是下一版本。

---

## 3. P0：修复后端自动执行卡住

### 3.1 复现命令

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_diagnostics_v101.py

核心验收：

    只调用 POST /threads/{thread_id}/turns
    → 后端创建 Task
    → 后端创建 Run
    → 后端启动 Runner
    → 任务最终 succeeded

当前测试长时间不结束，必须查明真实阻塞点。

### 3.2 必须排查

- create_turn 是否触发后端派发；
- auto_start 是否为 true；
- 是否产生 run.created；
- 是否产生 run.started；
- worker/in-process runner 是否启动；
- queued run 是否有人消费；
- provider mock 是否响应；
- 是否仍等待前端 /run；
- 是否存在数据库锁或连接池耗尽；
- 是否有异步任务未 await；
- 是否有异常被吞掉。

### 3.3 超时诊断

所有自动执行测试必须使用 30 秒超时。超时必须打印：

- task_id；
- goal_id；
- run_id；
- task.status；
- run.status；
- dispatch.decision；
- dispatch.auto_start；
- provider_id；
- 最近 20 条 AgentEvent；
- runner 是否存在；
- runner 最后心跳；
- pending asyncio tasks；
- 最后执行节点。

禁止让 pytest 无限等待。

### 3.4 正确后端链路

    创建 Task
    → 创建 Run
    → 写 run.created
    → 入队或启动 Runner
    → 写 run.started
    → 执行模型和工具
    → 执行验证
    → 写 task.completed

必须保证：

- 前端不调用 /run，任务也能执行；
- 关闭 Electron 后任务仍能执行；
- API 重启后 queued 任务可恢复；
- 同一个 task 不会重复创建多个 run；
- runner 失败会写失败事件；
- 所有执行异常可追溯。

### 3.5 通过条件

    10 passed
    0 failed
    0 hung

只要测试卡住，就不能报告后端自主执行完成。

---

## 4. P0：审批和 Clipboard_Screenshot 真实闭环

### 4.1 目标链路

    工具请求
    → approval.required
    → 前端显示审批卡
    → 用户点击允许
    → 后端精确匹配审批身份
    → 工具真实执行
    → 任务继续

### 4.2 审批身份

审批对象必须包含：

- approval_id；
- task_id；
- run_id；
- tool_call_id；
- tool_name；
- resource_path；
- arguments_hash；
- status；
- expires_at。

唯一匹配必须使用：

    approval_id + tool_call_id + tool_name + arguments_hash

不能只使用文件名、task_id、工具名或第一条 pending approval。

### 4.3 必须验证

- 正确 approval_id 成功；
- 错误 approval_id 返回 409；
- 错误 tool_call_id 返回 409；
- 错误 arguments_hash 返回 409；
- 过期审批返回明确错误；
- 重复点击不重复执行；
- 拒绝后工具不执行；
- 多个 pending 审批不串台；
- 参数改变后旧审批不可复用；
- 任务恢复后审批身份不变；
- approval.granted 后必须有对应 tool.started。

### 4.4 Clipboard_Screenshot GUI 验收

1. 上传 Clipboard_Screenshot.png；
2. 等待 Agent 请求读取；
3. 点击允许一次；
4. 检查任务继续；
5. 检查图片真实读取；
6. 页面不得再次显示“没有同意”；
7. 事件必须带完整审批身份；
8. 再次点击不得重复执行。

必须保存审批卡截图、授权后状态截图、事件日志和最终结果。

---

## 5. P0：修复生产构建

### 5.1 当前错误

    pnpm -C web build

当前仍失败：

    TurbopackInternalError
    Failed to write app endpoint /page
    globals.css.css

失败涉及 CSS/PostCSS/Turbopack 创建子进程。

### 5.2 排查顺序

1. 清理残留 node、next、turbo 进程；
2. 检查 .next 和锁文件；
3. 检查重复 build；
4. 检查 globals.css；
5. 检查 PostCSS 和 Tailwind；
6. 检查 Next.js/Turbopack 版本兼容；
7. 检查 Windows 文件权限；
8. 检查桌面壳是否占用构建产物；
9. 在干净环境重新构建。

### 5.3 通过条件

优先让以下命令退出码为 0：

    pnpm -C web build

如果必须切换 Webpack，必须固定 package.json、发布脚本和 CI 配置，不能只临时添加命令参数。

禁止只删除 .next 后报告完成，禁止只跑 dev server。

---

## 6. P0：全局下拉框真实验收

### 6.1 代码扫描

    rg -n "<select\b" web/src

业务源码中的原生 select 数量必须为 0。注释和普通函数名不算。

### 6.2 真实 GUI 页面

必须打开并截图：

- 主输入栏场景；
- 主输入栏模型；
- 主输入栏权限；
- 工作区；
- 设置页权限；
- 设置页自动化模式；
- 设置页模型；
- 供应商配置；
- 头脑风暴；
- 逆向实验室；
- 团队页面；
- 任务筛选；
- 监控页面。

### 6.3 视觉要求

不能出现白色浏览器原生菜单。

统一组件必须支持：

- 深色浮层；
- 当前选中标记；
- 图标和说明；
- hover、focus、disabled；
- 上下键；
- Enter；
- Escape；
- 点击外部关闭；
- 自动向上或向下弹出；
- 不被输入框、侧栏和右侧面板裁剪；
- 窄屏适配。

输入栏必须显示：

    [场景] [模型] [权限] [记忆] [附件] [发送]

只有日常场景显示：

    [记住习惯] / [不记住]

---

## 7. P1：关闭窗口后继续执行

真实 GUI 验收：

1. 创建多步骤开发任务；
2. 确认任务进入 queued 或 running；
3. 关闭 Electron 窗口；
4. 等待任务执行；
5. 重新打开应用；
6. 查看后台任务；
7. 任务必须处于 running、waiting_approval、verifying 或 succeeded；
8. 不能回到 planned 且无人接管；
9. 事件链不能中断；
10. 最终结果必须恢复显示。

记录 task_id、关闭前状态、重新打开时间、重新打开状态、最终状态和事件数量。

---

## 8. P1：schedule/watch 真实验证

相关文件：

    services/api/src/blank_api/services/agent/schedule.py
    services/api/src/blank_api/services/agent/watch_scan.py
    services/api/tests/test_schedule_v104.py
    services/api/tests/test_watch_v104.py

必须验证：

- 是否接入真实 API；
- 是否由 worker 消费；
- 到点是否真实创建任务；
- 文件变化是否真实唤醒任务；
- 重复触发是否受控；
- 是否支持暂停和取消；
- 是否有路径边界；
- 是否有权限检查；
- 是否属于当前版本。

属于当前版本就补齐实现和 GUI 验收；不属于当前版本就拆到下一版本，不得混入当前发布。

---

## 9. 必须运行的命令

前端：

    pnpm -C web test
    pnpm -C web lint
    pnpm typecheck:web
    pnpm -C web build

后端：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_diagnostics_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_approval_identity_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_approval_card_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_validation_approval_identity_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_schedule_v104.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_watch_v104.py
    services/api/.venv/Scripts/python.exe -m pytest -q

代码扫描：

    rg -n "<select\b" web/src
    rg -n "approval_id|tool_call_id|arguments_hash" services/api/src web/src
    git status --short

每条命令记录退出码、耗时、通过数量、失败数量、是否超时和失败日志。

---

## 10. 发布门槛

以下条件必须全部满足：

    后端自动启动：通过
    后端自动完成：通过
    后端测试无卡住：通过
    审批身份匹配：100%
    Clipboard_Screenshot 授权：通过
    重复授权不重复执行：通过
    关闭窗口续跑：通过
    原生业务 select：0
    输入栏权限入口：通过
    所有下拉框真实 GUI：通过
    前端测试：全部通过
    后端测试：全部通过
    lint：通过
    typecheck：通过
    生产构建：通过
    schedule/watch：通过

以下任何一项失败，都不能报告“全部完成”：

- 后端测试卡住；
- 审批测试未结束；
- schedule/watch 测试未结束；
- 生产构建失败；
- Clipboard_Screenshot 未实测；
- 关闭窗口续跑未实测；
- GUI 未实测；
- 只有单元测试，没有真实运行证据。

---

## 11. 最终交付格式

    版本：
    - 当前版本号：
    - 当前提交 hash：

    完成：
    - ...

    修改文件：
    - ...

    测试结果：
    - 前端测试：通过/失败/未验证，数量：
    - 后端自动执行：通过/失败/未验证
    - 审批测试：通过/失败/未验证
    - schedule/watch：通过/失败/未验证
    - lint：通过/失败/未验证
    - typecheck：通过/失败/未验证
    - 生产构建：通过/失败/未验证

    真实 GUI：
    - 下拉框：通过/失败/未验证
    - 完全访问：通过/失败/未验证
    - Clipboard_Screenshot：通过/失败/未验证
    - 关闭窗口续跑：通过/失败/未验证
    - 自动修复：通过/失败/未验证

    未完成：
    - ...

    已知限制：
    - ...

    阻塞问题：
    - ...

禁止只回复“全部完成”，必须提供真实测试退出码、截图、事件日志和失败项。
