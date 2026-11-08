# WorkBuddy 返工任务单：v0.10.1 实际验收失败项修复

## 0. 当前结论

本任务针对当前实际验证结果进行返工，不是重新写一份计划。

已经看到的完成项：

- 前端自定义下拉组件已接入多个页面；
- 输入栏已有场景、模型和权限选择；
- 审批对象已有 approval_id、tool_call_id、arguments_hash；
- 前端测试 296/296 通过；
- Web lint 通过；
- TypeScript 类型检查通过。

仍然不能判定全部完成：

1. 后端自动执行测试在第一条核心测试处长时间卡住；
2. 审批端到端测试没有完整结束；
3. Clipboard_Screenshot.png 授权后真实执行没有完成 GUI 验证；
4. Web 生产构建仍失败；
5. 关闭窗口后任务是否继续没有真实证据；
6. schedule.py 和 test_schedule_v104.py 是未跟踪文件，尚未纳入完整版本验证；
7. 代码存在不等于功能闭环。

本任务必须解决以上问题并提供可复现证据。

---

## 1. 执行规则

1. 先读取 git 状态、最近提交、未跟踪文件和运行中的进程；
2. 先复现失败，再修改；
3. 不允许删除测试、降低断言、增加无限等待或跳过用例；
4. 不允许只修改测试，不修改真实执行链路；
5. 不允许把前端自动调用 runTask 当作后端自主执行；
6. 不允许把 mock 通过当作真实 GUI 通过；
7. 不允许把生产构建失败归类为无关问题；
8. 所有长时间测试必须有超时和诊断输出；
9. 所有测试记录退出码；
10. 没有真实验证的项目必须标记为“未验证”；
11. 不覆盖用户已有改动；
12. 先判断 schedule 文件是否属于当前版本，再决定纳入或单独提交。

---

## 2. P0：修复后端自动执行测试卡住

### 2.1 失败用例

当前卡住的核心用例：

    tests/test_backend_autostart_v101.py::test_backend_starts_and_finishes_without_any_frontend_call

它要求：

    只调用 POST /threads/{thread_id}/turns
    → 后端创建任务
    → 后端创建 run
    → 后端启动 Runner
    → 任务最终 succeeded

当前实际表现是长时间不结束，无法证明后端真正接管执行。

### 2.2 必须排查

逐项检查：

- threads.create_turn 是否调用了后端派发逻辑；
- auto_start 是否为 true；
- 是否创建 run.created；
- 是否创建 run.started；
- 是否有 worker 或 in-process runner 消费 queued run；
- 测试 fixture 是否启动真实 runner；
- runner 是否等待永远不会到来的前端请求；
- provider mock 是否没有响应；
- 任务当前到底停在 draft、queued、running、waiting_approval 还是其他状态；
- 是否存在数据库死锁、连接池耗尽或异常被吞掉。

### 2.3 必须增加诊断输出

测试失败或超时时必须打印：

- task_id；
- goal_id；
- run_id；
- task.status；
- run.status；
- dispatch.auto_start；
- dispatch.decision；
- provider_id；
- 最近 20 条 AgentEvent；
- runner 是否存在；
- runner 最后一次心跳。

不能让 pytest 无限制等待。

### 2.4 正确实现

auto_start=true 时必须走：

    创建 Task
    → 创建 Run
    → 写 run.created
    → 入队或启动 Runner
    → 写 run.started
    → 执行模型和工具
    → 执行验证
    → 写 task.completed

要求：

- 前端不调用 /run，任务也能执行；
- 关闭桌面窗口后任务仍能执行；
- API 重启后 queued 任务可以恢复；
- 同一个 task 不会重复创建两个 run；
- 异常进入 failed 或可恢复状态；
- 后台异常不能静默吞掉。

### 2.5 验收命令

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py

要求：

    10 passed
    0 failed
    0 hung

超过 30 秒必须主动失败并输出诊断。

---

## 3. P0：修复审批端到端链路

### 3.1 当前问题

虽然代码已经增加审批身份字段，但没有完成真实链路验证：

    工具请求
    → approval.required
    → 前端显示审批卡
    → 用户点击允许
    → 后端精确匹配审批对象
    → 工具真实执行
    → 任务继续

### 3.2 审批唯一身份

审批对象至少包含：

- approval_id；
- task_id；
- run_id；
- tool_call_id；
- tool_name；
- resource_path；
- arguments_hash；
- status；
- expires_at。

只能使用以下组合匹配：

    approval_id + tool_call_id + tool_name + arguments_hash

不能只使用文件名、task_id、工具名或当前任务的第一条 pending approval。

### 3.3 必须测试

- 正确 approval_id 可以继续执行；
- 错误 approval_id 返回 409；
- 错误 tool_call_id 返回 409；
- 错误 arguments_hash 返回 409；
- 过期审批返回明确错误；
- 重复点击允许不会重复执行；
- 拒绝后工具不会执行；
- 多个审批同时存在时不会串台；
- 参数变化后旧审批不能复用；
- 任务重试会生成新审批；
- 任务恢复后审批身份仍一致；
- Clipboard_Screenshot.png 授权后真实读取成功。

### 3.4 事件要求

审批事件必须按以下顺序产生：

    approval.required
    approval.decided
    approval.granted / approval.denied
    tool.started
    tool.completed

每条事件都必须包含 approval_id、tool_call_id、tool_name、arguments_hash、resource_path。

禁止出现 approval.granted 但没有对应 tool.started 的情况。

### 3.5 GUI 验收

1. 上传 Clipboard_Screenshot.png；
2. 等待 Agent 请求读取；
3. 点击允许一次；
4. 检查任务继续；
5. 检查图片真实读取；
6. 检查不再出现“没有同意”；
7. 检查事件中存在完整审批身份；
8. 再次点击不能重复执行。

必须保存审批卡、任务状态、事件日志和最终结果截图。

---

## 4. P0：修复生产构建

### 4.1 当前失败

默认构建曾失败：

    TurbopackInternalError
    Failed to write app endpoint /page
    globals.css.css

切换 Webpack 后仍失败：

    EPERM: operation not permitted, mkdir web/.next/server/app/projects

### 4.2 必须排查

- .next 是否被旧进程占用；
- 是否有残留 node/next 进程；
- 是否存在锁文件；
- 是否有重复 build 进程；
- globals.css 是否存在非法内容；
- CSS loader、PostCSS、Tailwind 配置；
- Windows 路径和文件权限；
- 构建目录是否可以创建；
- 是否有桌面壳或杀毒软件占用产物；
- Next.js/Turbopack 配置是否兼容。

### 4.3 验收

优先让默认命令通过：

    pnpm -C web build

如果只能使用 Webpack，必须把发布脚本固定为 Webpack并说明原因。

禁止只删除 .next 后不验证，禁止只说“环境问题”，禁止只跑开发服务器。

---

## 5. P0：真实验证全局自定义下拉框

### 5.1 全局排查

搜索：

    <select
    select[
    role="combobox"

至少覆盖：

- 输入栏场景；
- 输入栏模型；
- 输入栏权限；
- 工作区；
- 设置页权限；
- 设置页自动化模式；
- 设置页模型；
- 模型供应商配置；
- 头脑风暴；
- 逆向实验室；
- 团队页面；
- 项目页面；
- 连接器页面；
- 任务筛选器。

业务界面原生 select 数量必须为 0。

### 5.2 统一组件要求

所有页面必须使用同一个自定义下拉组件，不能各自复制实现。

必须支持：

- 当前值、图标、标题、描述、选中状态；
- disabled、warning、loading；
- 上下键、Enter、Escape；
- 点击外部关闭；
- aria 属性；
- 长文本截断；
- 深色主题；
- 统一 z-index；
- 自动判断向上或向下弹出；
- 不被输入框、侧栏和右侧面板裁剪；
- 窄屏可用。

### 5.3 视觉验收

不能再出现截图中的白色浏览器原生菜单。

统一菜单必须：

- 使用深色浮层；
- 使用统一圆角、边框和间距；
- 使用品牌色表示当前项；
- 清晰区分 hover、focus、selected；
- 显示权限范围和场景说明；
- 不遮挡输入文字和发送按钮。

### 5.4 输入栏验收

输入栏必须显示：

    [场景] [模型] [权限] [记忆] [附件] [发送]

开发场景显示权限，不显示日常记忆。

日常场景显示：

    [记住习惯] / [不记住]

其他场景不能显示日常记忆开关。

---

## 6. P1：验证关闭窗口后的持续执行

真实 GUI 测试：

1. 创建一个多步骤开发任务；
2. 确认进入 queued 或 running；
3. 关闭 Electron 窗口；
4. 等待任务执行；
5. 重新打开应用；
6. 查询后台任务；
7. 任务必须仍处于 running、waiting_approval、verifying 或 succeeded；
8. 不能回到 planned 且无人接管；
9. 事件必须连续；
10. 最终结果必须可恢复显示。

记录：

- 关闭窗口时间；
- task_id；
- 关闭前状态；
- 重新打开时间；
- 重新打开后状态；
- 最终状态；
- 事件数量。

---

## 7. P1：验证自主决策和自动修复

用户只发送：

    修复这个页面并运行测试

Agent 必须自动完成：

    检查项目
    → 找到文件
    → 修改文件
    → 运行测试
    → 失败后自动修复
    → 再次测试
    → 交付结果

不能要求用户分别发送“继续、运行测试、修复错误、再试一次”。

默认可以自动修复编译、类型、格式、明确测试失败、明确路径和页面布局问题。

必须询问业务歧义、数据删除、工作区外路径、发布、生产和敏感外部请求。

没有实际文件变化、命令结果、测试结果和验证事件，不能回复“完成”。

---

## 8. 处理未跟踪 schedule 文件

当前存在：

    services/api/src/blank_api/services/agent/schedule.py
    services/api/tests/test_schedule_v104.py

必须明确：

1. 是否属于当前 v0.10.1；
2. 是否属于下一版本；
3. 是否接入真实 API；
4. 是否有数据库迁移；
5. 是否有前端入口；
6. 是否有权限和安全边界；
7. 是否有真实调度执行器；
8. 是否只有测试没有产品入口。

若属于当前版本，补齐实现、API、前端、测试并纳入提交。

若不属于当前版本，单独放入下一版本，不要混入本次发布，也不要保持归属不明的未跟踪状态。

---

## 9. 必须运行的命令

前端：

    pnpm -C web test
    pnpm -C web lint
    pnpm typecheck:web
    pnpm -C web build

后端：

    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_backend_autostart_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_approval_identity_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q tests/test_approval_card_v101.py
    services/api/.venv/Scripts/python.exe -m pytest -q

代码扫描：

    rg -n "<select\b" web/src
    rg -n "approval_id|tool_call_id|arguments_hash" services/api/src web/src
    git status --short

每条命令必须记录命令、退出码、耗时、通过数量、失败数量和是否超时。

---

## 10. 最终验收指标

必须达到：

    后端自动启动测试：通过
    后端自动完成测试：通过
    后端测试无卡住：通过
    审批身份匹配率：100%
    Clipboard_Screenshot 授权闭环：通过
    重复授权不重复执行：通过
    关闭窗口后任务继续：通过
    全局原生 select：0
    输入栏权限入口：通过
    所有下拉框深色自定义样式：通过
    前端测试：全部通过
    后端测试：全部通过
    生产构建：通过
    真实 GUI：通过
    schedule 文件归属明确：通过

以下任一项失败，都不能报告“全部完成”：

- 后端自动执行测试卡住；
- 审批测试未结束；
- 生产构建失败；
- GUI 未验证；
- 关闭窗口续跑未验证；
- Clipboard_Screenshot 未真实验证；
- 未跟踪文件归属不明。

---

## 11. 最终交付报告格式

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

禁止只回复“全部完成”，必须提供真实测试结果、退出码、截图或事件证据。
