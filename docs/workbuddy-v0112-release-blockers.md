# WorkBuddy 发布阻塞修复任务单：v0.11.2 前置验收

## 0. 当前验证结论

当前代码不能报告“全部完成”，也不能直接发布。

已经确认：

- 前端测试 296/296 通过；
- 业务页面已接入 SelectPopover；
- 输入栏已有场景、模型和权限入口；
- 审批代码已有 approval_id、tool_call_id、arguments_hash；
- v0.11.0 已加入定时和监控相关代码；
- v0.11.1 已加入桌面通知相关代码；
- 最新提交包含图片模型识别和审批身份修复。

仍然存在发布阻塞：

1. 后端自动执行测试长时间卡住；
2. 审批端到端测试没有完整结束；
3. Clipboard_Screenshot.png 授权后真实读取没有完成 GUI 验收；
4. Web 生产构建仍在 Turbopack CSS 阶段失败；
5. 关闭窗口后任务继续执行没有真实证据；
6. schedule.py、watch_scan.py 及对应测试的版本归属和真实执行链路未完成验收；
7. 前端测试通过不能代替后端和真实 GUI 验收。

本任务必须先解决上述问题，不能继续用“代码已经存在”代替“功能已经闭环”。

---

## 1. 执行规则

1. 先读取当前 git status、最近提交、未跟踪文件和运行中的 node/python 进程；
2. 先复现每一个失败，再开始修改；
3. 不允许删除测试、降低断言、屏蔽异常或无限延长超时；
4. 不允许通过 mock 结果伪造后端自动完成；
5. 不允许让前端 /run 成为任务启动的必要条件；
6. 不允许只验证字段存在，必须验证字段参与真实执行；
7. 不允许把生产构建失败归类为无关环境问题后直接发布；
8. 所有测试必须有超时、退出码和诊断；
9. 所有 GUI 功能必须提供真实桌面截图或事件日志；
10. 没有验证的项目必须写“未验证”；
11. 不覆盖现有用户改动；
12. schedule 和 watch 如果不属于当前版本，必须明确拆到下一版本。

---

## 2. P0：修复后端自动执行卡死

### 2.1 必须复现

执行：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py

同时执行：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_diagnostics_v101.py

重点用例：

    test_backend_starts_and_finishes_without_any_frontend_call

测试必须验证：

    只调用 POST /threads/{thread_id}/turns
    → 后端创建 Task
    → 后端创建 Run
    → 后端启动 Runner
    → 任务最终 succeeded

当前测试长时间卡住，必须查出实际阻塞点，不能只增加等待时间。

### 2.2 必须输出诊断

测试超时时必须输出：

- task_id；
- goal_id；
- run_id；
- task.status；
- run.status；
- dispatch.decision；
- dispatch.auto_start；
- provider_id；
- 最近 20 条 AgentEvent；
- worker 是否启动；
- in-process runner 是否启动；
- runner 最后心跳；
- 当前数据库连接状态；
- 当前 pending asyncio task；
- 最后进入的代码节点。

建议所有自动执行测试使用明确超时：

    asyncio.wait_for(operation, timeout=30)

超时后必须主动失败，不能让 pytest 进程无限挂起。

### 2.3 正确执行链路

当 auto_start=true 时，后端必须完成：

    创建 Task
    → 创建 Run
    → 写入 run.created
    → 入队或启动 Runner
    → 写入 run.started
    → 执行模型
    → 执行工具
    → 执行验证
    → 写入 task.completed

前端只能观察状态，不能负责启动任务。

必须保证：

- 前端没有调用 /run 时任务仍能执行；
- Electron 窗口关闭后任务仍能执行；
- API 重启后 queued 任务可以恢复；
- 同一个 task 不会重复创建多个 run；
- runner 异常会记录 task.dispatch_failed 或 run.failed；
- 所有异常都能在事件流中找到。

### 2.4 验收条件

以下命令必须完整结束：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py

要求：

    10 passed
    0 failed
    0 hung

如果用例仍卡住，必须报告具体卡点，不能报告“后端自动执行已完成”。

---

## 3. P0：审批真实闭环

### 3.1 目标

必须真实验证：

    工具请求
    → approval.required
    → 前端显示审批卡
    → 用户点击允许
    → 后端校验审批身份
    → 工具真实执行
    → 任务继续

### 3.2 审批身份

审批对象必须至少包含：

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

不能只使用：

- 文件名；
- task_id；
- tool_name；
- 当前任务第一条 pending approval；
- 前端显示顺序。

### 3.3 API 行为

正确审批必须返回成功并继续任务。

错误审批必须返回明确 409：

- approval_id 不匹配；
- tool_call_id 不匹配；
- arguments_hash 不匹配；
- 审批已过期；
- 审批已处理；
- 审批属于其他任务。

错误消息必须说明：

    审批对象已变化，请重新生成审批请求

不能只返回“没有同意”。

### 3.4 幂等

- 第一次允许：执行一次；
- 第二次允许：返回已处理状态；
- 不得重复执行命令；
- 不得重复修改文件；
- 不得出现两条 tool.started；
- 拒绝后不得执行工具。

### 3.5 Clipboard_Screenshot 实测

真实桌面应用中执行：

1. 上传 Clipboard_Screenshot.png；
2. 等待 Agent 请求读取；
3. 点击允许一次；
4. 检查任务继续；
5. 检查图片真实进入模型或分析流程；
6. 检查页面不再提示没有同意；
7. 检查事件包含完整审批身份；
8. 再次点击不重复执行。

必须保存：

- 审批卡截图；
- 点击允许后的状态截图；
- 事件或审计日志；
- 最终任务结果。

### 3.6 审批测试

执行：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_approval_identity_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_approval_card_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_validation_approval_identity_v101.py

所有测试必须完整结束，不允许卡住。

---

## 4. P0：修复 Web 生产构建

### 4.1 当前错误

默认构建错误：

    TurbopackInternalError
    Failed to write app endpoint /page
    globals.css.css

错误发生在 CSS loader、PostCSS 或 Turbopack 创建子进程阶段。

之前切换 Webpack 也出现：

    EPERM: operation not permitted
    mkdir web/.next/server/app/projects

### 4.2 排查顺序

1. 检查并结束残留 node、next、turbo 进程；
2. 检查 .next 是否被进程或桌面壳占用；
3. 检查是否存在锁文件；
4. 检查 globals.css 和 PostCSS 配置；
5. 检查 Tailwind/Next.js 版本兼容；
6. 检查 Windows 文件权限；
7. 检查构建目录是否可以创建；
8. 检查是否有重复 build；
9. 检查桌面应用是否正在读取构建产物；
10. 在干净工作区中重新构建。

### 4.3 修复要求

优先保证：

    pnpm -C web build

如果必须切换 Webpack：

1. 修改 package.json 固定构建配置；
2. 修改发布脚本；
3. 修改 CI；
4. 重新运行构建；
5. 说明为什么不能使用 Turbopack。

禁止只删除 .next 后声称完成。

### 4.4 构建验收

必须得到：

    exit code 0
    生成完整生产构建产物
    没有 Turbopack panic
    没有 EPERM
    没有 CSS loader 错误

---

## 5. P0：全局下拉框真实 GUI 验收

### 5.1 代码扫描

执行：

    rg -n "<select\b" web/src

业务界面原生 select 数量必须为 0。

只允许出现在：

- 注释；
- 测试；
- 非 UI 普通函数文本；
- 第三方依赖内部代码。

### 5.2 必须检查的页面

真实打开并截图：

- 主输入栏场景；
- 主输入栏模型；
- 主输入栏权限；
- 工作区选择；
- 设置页权限；
- 设置页自动化模式；
- 设置页模型；
- 模型供应商配置；
- 头脑风暴；
- 逆向实验室；
- 团队页面；
- 任务筛选；
- 监控页面。

### 5.3 视觉要求

不能再出现截图中的白色浏览器原生菜单。

所有菜单必须：

- 使用深色浮层；
- 使用统一圆角、边框、阴影和间距；
- 有当前选中标识；
- 有 hover、focus、disabled 状态；
- 有图标和必要说明；
- 支持上下键、Enter、Escape；
- 点击外部关闭；
- 自动判断向上或向下弹出；
- 不被输入框、侧栏和右侧面板裁剪；
- 窄屏可用。

输入栏必须显示：

    [场景] [模型] [权限] [记忆] [附件] [发送]

日常场景才显示：

    [记住习惯] / [不记住]

---

## 6. P1：关闭窗口后持续执行

真实 GUI 验收：

1. 创建一个多步骤开发任务；
2. 确认任务进入 queued 或 running；
3. 关闭 Electron 窗口；
4. 等待任务继续；
5. 重新打开应用；
6. 查看后台任务；
7. 任务必须处于 running、waiting_approval、verifying 或 succeeded；
8. 不得回到 planned 且无人接管；
9. 事件链不能中断；
10. 最终结果必须恢复显示。

记录：

- task_id；
- 关闭前状态；
- 重新打开时间；
- 重新打开后状态；
- 最终状态；
- 事件总数；
- 是否产生 run.started；
- 是否产生 task.completed。

---

## 7. P1：自主决策和自动修复

用户只发送：

    修复这个页面并运行测试

系统必须自动：

    检查项目
    → 找到目标文件
    → 修改文件
    → 运行测试
    → 测试失败后自动修复
    → 再次运行测试
    → 输出最终结果

不能要求用户分别发送：

    继续
    运行测试
    修复错误
    再试一次

可以自动修复：

- 编译错误；
- 类型错误；
- 格式错误；
- 明确测试失败；
- 明确路径错误；
- 页面布局错误。

必须询问：

- 业务目标不明确；
- 可能删除重要数据；
- 工作区外路径；
- 发布或生产操作；
- 敏感外部网络请求。

没有实际修改、命令结果、测试结果和验证事件，不能回复“完成”。

---

## 8. schedule 和 watch 功能归属

当前新增：

    services/api/src/blank_api/services/agent/schedule.py
    services/api/src/blank_api/services/agent/watch_scan.py
    services/api/tests/test_schedule_v104.py
    services/api/tests/test_watch_v104.py

必须确认：

1. 是否属于当前 v0.11.1；
2. 是否属于下一版本；
3. 是否接入真实 API；
4. 是否由 worker 实际消费；
5. 到点是否真实执行；
6. 文件变化是否真实唤醒任务；
7. 是否有权限和安全边界；
8. 是否有取消、暂停和重复触发保护；
9. 是否有 GUI 入口；
10. 是否有真实验收截图。

属于当前版本就补齐实现并纳入提交；不属于当前版本就拆到下一版本，不能保持版本归属不明。

---

## 9. 必须执行的命令

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

每条命令记录：

- 命令；
- 退出码；
- 耗时；
- 通过数量；
- 失败数量；
- 是否超时；
- 失败日志；
- 是否真实执行。

---

## 10. 最终发布门槛

以下条件全部满足，才允许报告完成：

    后端自动启动测试：通过
    后端自动完成测试：通过
    后端测试无卡住：通过
    审批身份匹配率：100%
    Clipboard_Screenshot 授权闭环：通过
    重复授权不重复执行：通过
    关闭窗口后任务继续：通过
    全局业务原生 select：0
    输入栏权限入口：通过
    所有下拉框深色自定义样式：通过
    前端测试：全部通过
    后端测试：全部通过
    lint：通过
    typecheck：通过
    生产构建：通过
    真实 GUI：通过
    schedule/watch 版本归属明确：通过

以下任一项失败，都不能报告“全部完成”：

- 后端测试卡住；
- 审批测试未结束；
- 生产构建失败；
- Clipboard_Screenshot 未实测；
- 关闭窗口续跑未实测；
- GUI 未实测；
- schedule/watch 归属不明；
- 只有单元测试，没有真实运行证据。

---

## 11. 最终交付格式

最终必须按以下格式回复：

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
