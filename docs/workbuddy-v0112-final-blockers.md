# WorkBuddy 最终阻塞修复任务单：v0.11.2 发布前闭环

## 1. 当前真实验证结果

当前不能报告“全部完成”，不能直接发布。

已经实际验证通过：

- 前端测试：296/296 通过；
- Web 业务页面已经接入 SelectPopover；
- 输入栏已有场景、模型和权限入口；
- 业务源码中没有真正的原生 select；
- 审批代码已经加入 approval_id、tool_call_id、arguments_hash；
- 最新提交包含自动修复、审批、定时和监控相关改动。

仍然没有通过：

1. 后端自动执行测试长时间卡住；
2. 审批端到端测试没有完整结束；
3. schedule/watch 测试没有完整结束；
4. Clipboard_Screenshot.png 授权后真实读取没有完成 GUI 验证；
5. 关闭窗口后任务继续执行没有真实证据；
6. Web 生产构建仍在 Turbopack CSS 阶段失败；
7. 真实桌面下拉框仍没有截图验收。

本任务必须解决以上问题，不能以“新增代码”“单元测试通过”替代真实闭环。

---

## 2. 当前已知提交

当前 HEAD：

    0ede32c fix(agent): §7 自动修复不再把「没修好」误报成成功

相关提交：

    bac07ff feat(agent): v0.11.2 前置验收——补齐超时诊断与触发器停止/路径边界
    62c6620 fix(approval): v101 返工复验——补齐校验通道审批身份与跨恢复一致性
    e9e8f75 feat(desktop): v0.11.1 原生通知通道
    6bceca8 feat(agent): v0.11.0 定时、监控和自动更新

不要只依据提交标题判断完成，必须按下方验收标准重新执行。

---

## 3. P0：修复后端自动执行测试卡住

### 3.1 当前失败表现

执行以下测试时没有完整退出：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_diagnostics_v101.py tests/test_validation_approval_identity_v101.py

核心目标是：

    只调用 POST /threads/{thread_id}/turns
    → 后端创建 Task
    → 后端创建 Run
    → 后端启动 Runner
    → 任务最终 succeeded

当前测试长时间等待，说明后端接管执行仍未形成可证明闭环。

### 3.2 必须定位

逐项排查：

- create_turn 是否真的触发后端派发；
- auto_start 是否为 true；
- 是否创建 run.created；
- 是否创建 run.started；
- 是否有 worker/in-process runner 消费 queued run；
- 测试 fixture 是否启动 runner；
- provider mock 是否返回；
- 任务当前状态是什么；
- run 当前状态是什么；
- 是否存在数据库锁；
- 是否存在异步任务未 await；
- 是否有异常被后台吞掉；
- 是否仍然等待前端 /run 请求。

### 3.3 超时诊断

所有自动执行测试必须设置 30 秒超时。超时后打印：

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
- 当前 pending asyncio task；
- 最后执行节点。

禁止无限等待，禁止只增加超时时间。

### 3.4 正确实现

auto_start=true 时必须由后端完成：

    创建 Task
    → 创建 Run
    → 写 run.created
    → 入队或启动 Runner
    → 写 run.started
    → 调用模型
    → 执行工具
    → 执行验证
    → 写 task.completed

必须满足：

- 前端不调用 /run，任务也能执行；
- 关闭 Electron 后任务仍能执行；
- API 重启后 queued 任务可以恢复；
- 一个 task 不会重复创建多个 run；
- runner 失败必须写入失败事件；
- 所有执行异常可从事件流追溯。

### 3.5 验收命令

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py

必须得到：

    10 passed
    0 failed
    0 hung

---

## 4. P0：审批和 Clipboard_Screenshot 真实闭环

### 4.1 目标链路

必须真实完成：

    工具请求
    → approval.required
    → 前端显示审批卡
    → 用户点击允许
    → 后端匹配审批身份
    → 工具真实执行
    → 任务继续

### 4.2 审批唯一身份

审批必须至少包含：

- approval_id；
- task_id；
- run_id；
- tool_call_id；
- tool_name；
- resource_path；
- arguments_hash；
- status；
- expires_at。

匹配必须使用：

    approval_id + tool_call_id + tool_name + arguments_hash

不能只使用文件名、task_id、tool_name 或当前任务第一条 pending approval。

### 4.3 必须测试

- 正确 approval_id 可以继续；
- 错误 approval_id 返回 409；
- 错误 tool_call_id 返回 409；
- 错误 arguments_hash 返回 409；
- 过期审批返回明确错误；
- 重复点击允许不重复执行；
- 拒绝后工具不执行；
- 多个 pending 审批不串台；
- 参数变化后旧审批不能复用；
- 任务重试生成新审批；
- 任务恢复后审批身份一致；
- approval.granted 后必须有对应 tool.started。

### 4.4 Clipboard_Screenshot GUI 验收

1. 上传 Clipboard_Screenshot.png；
2. 等待 Agent 请求读取；
3. 点击允许一次；
4. 检查任务继续；
5. 检查图片真实读取；
6. 检查页面不再显示“没有同意”；
7. 检查事件有完整审批身份；
8. 再次点击不重复执行。

必须保存：

- 审批卡截图；
- 授权后的状态截图；
- 事件日志；
- 最终任务结果。

---

## 5. P0：修复 Web 生产构建

### 5.1 当前失败

执行：

    pnpm -C web build

仍然失败：

    TurbopackInternalError
    Failed to write app endpoint /page
    globals.css.css

失败位置包括：

- CSS loader；
- PostCSS；
- Turbopack 创建子进程；
- 生成 app endpoint。

### 5.2 必须排查

- 残留 node/next/turbo 进程；
- .next 目录锁定；
- 构建锁文件；
- 重复 build；
- globals.css 内容；
- PostCSS 配置；
- Tailwind 配置；
- Next.js/Turbopack 兼容性；
- Windows 文件权限；
- 桌面应用是否占用构建产物。

### 5.3 修复要求

优先保证：

    pnpm -C web build

如果必须使用 Webpack：

1. 固定 package.json 构建脚本；
2. 更新发布脚本；
3. 更新 CI；
4. 重新执行生产构建；
5. 写明不能使用 Turbopack 的原因。

禁止只删除 .next 后报告完成，禁止只运行 dev server。

最终必须：

- exit code 为 0；
- 生成完整生产构建产物；
- 没有 Turbopack panic；
- 没有 EPERM；
- 没有 CSS loader 错误。

---

## 6. P0：全局下拉框真实验收

### 6.1 代码扫描

执行：

    rg -n "<select\b" web/src

业务源码中原生 select 数量必须为 0。注释和普通函数名不算 UI 使用。

### 6.2 必须真实打开的页面

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

### 6.3 视觉和交互

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
- 自动向上/向下弹出；
- 不被输入框或侧栏裁剪；
- 窄屏适配。

输入栏必须显示：

    [场景] [模型] [权限] [记忆] [附件] [发送]

只有日常场景显示：

    [记住习惯] / [不记住]

---

## 7. P1：关闭窗口后任务继续

真实 GUI 验收：

1. 创建多步骤开发任务；
2. 确认 queued 或 running；
3. 关闭 Electron 窗口；
4. 等待任务执行；
5. 重新打开；
6. 查询后台任务；
7. 任务必须处于 running、waiting_approval、verifying 或 succeeded；
8. 不能回到 planned 且无人接管；
9. 事件链不能中断；
10. 最终结果必须恢复显示。

记录 task_id、关闭前状态、重新打开时间、重新打开状态、最终状态和事件数量。

---

## 8. P1：schedule/watch 真实验收

当前相关文件：

    services/api/src/blank_api/services/agent/schedule.py
    services/api/src/blank_api/services/agent/watch_scan.py
    services/api/tests/test_schedule_v104.py
    services/api/tests/test_watch_v104.py

必须确认：

- 是否接入真实 API；
- 是否由 worker 实际消费；
- 到点是否真实创建任务；
- 文件变化是否真实唤醒任务；
- 重复触发是否被阻止；
- 是否支持暂停和取消；
- 是否有路径边界；
- 是否有权限检查；
- 是否属于当前版本。

如果属于当前版本，补齐实现、前端、测试和 GUI 验收。

如果不属于当前版本，拆到下一版本，不能混入当前发布。

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

每条命令记录命令、退出码、耗时、通过数量、失败数量、是否超时和失败日志。

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
    全部下拉框真实 GUI：通过
    前端测试：全部通过
    后端测试：全部通过
    lint：通过
    typecheck：通过
    生产构建：通过
    schedule/watch：通过

以下任一项失败，都不能报告“全部完成”：

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

必须按以下格式回复：

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

禁止只回复“全部完成”，必须提供测试退出码、截图、事件日志和失败项。
