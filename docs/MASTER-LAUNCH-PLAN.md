# 空白格 AI 操作台（kbgai）— 产品重塑、交互设计与全量工程落地总规划

> **文档版本**：2026-11-26（大师级产品与设计深化版 · 唯一全量执行源）  
> **受众**：产品经理、UI/UX 设计师、前后端工程师、桌面端/CLI 工程师、以及自动化开发 Agent  
> **核心目标**：**能力强、产出高、花费少、轻便、好用**  
> **阶段范围**：面向内部团队先行推广上线。**模型选型/商业计费**与**桌面端代码签名**暂不纳入本阶段范围；本规划聚焦**源码缺陷清零、Claude Code 级编程底座、WorkBuddy 级自学习闭环、Linear/Raycast 级极简高密度交互、国内生态免公网打通与内部上线安全网**。

---

## 目录

1. [产品战略与北极星指标（PM 视角）](#一产品战略与北极星指标pm-视角)
2. [用户体验与设计系统规范（顶尖 Designer 视角）](#二用户体验与设计系统规范顶尖-designer-视角)
3. [核心界面线框图与微交互状态机（逐组件详设）](#三核心界面线框图与微交互状态机逐组件详设)
4. [全量源码缺陷清单（31 项，已精确定位）](#四全量源码缺陷清单31-项已精确定位)
5. [Phase 1：即刻止血与降本改造（UP-01~03 + P0 Bug 清零）](#五phase-1即刻止血与降本改造up-0103--p0-bug-清零)
6. [Phase 2：底座解耦与 Claude Code 级编程重构（UP-04, 06, 07）](#六phase-2底座解耦与-claude-code-级编程重构up-04-06-07)
7. [Phase 3：前端统一重构与 WorkBuddy 级自学习闭环（UP-05）](#七phase-3前端统一重构与-workbuddy-级自学习闭环up-05)
8. [Phase 4：日常办公、国内生态与内部上线最小必改（UP-08）](#八phase-4日常办公国内生态与内部上线最小必改up-08)
9. [一遍改完执行流水线与核心文件路径速查](#九一遍改完执行流水线与核心文件路径速查)

---

## 一、产品战略与北极星指标（PM 视角）

### 1.1 产品定位（Positioning Statement）

> **致需要高效交付代码与日常办公成果的内部成员：**  
> **空白格 AI 操作台（kbgai）** 是一款**本地优先、双模一体、越用越懂你的桌面级 AI 交付工作台**。  
> 不同于只有聊天框的套壳工具（改完不敢信、聊久了越贵）或纯极客向终端工具（日常办公用不起来），空白格通过 **「确定性验收门禁 + 秒级快照回滚 + 对话流自学习进化 + 飞书/钉钉免公网审批」**，把 AI 从"陪聊助手"升级为"可验证、可托付的数字同事"。

### 1.2 核心用户画像与 Jobs-to-be-Done（JTBD）

| 用户角色 | 高频场景（Jobs-to-be-Done） | 当前核心痛点 | 重构后的核心解法 |
|---|---|---|---|
| **Persona A：一线研发（💻 编程模式）** | 修 Bug、跨文件重构、补单测、Code Review | 模型瞎猜文件多轮试错费 Token；`done_when` 不阻塞导致"假完成"；怕 Agent 改乱工作区。 | **RepoMap 骨架导航 + LSP 编辑即诊 + `done_when` 强制门禁 + 一键快照回滚**。 |
| **Persona B：职场/业务同事（📋 日常模式）** | 汇总 Excel/Word 周报、提炼 PDF 合同、每日定时巡检发群通知 | 工具藏太深、只有代码面板；飞书/企微需要公网 IP 配不通；高危审批还得切回电脑点。 | **原生 Office/PDF 工具链 + 右侧即时预览 + 可视化定时/Watch + 飞书长连接与 IM 内一键审批**。 |
| **Persona C：内部团队管理员（安全与支持）** | 推广给同事用、控制本地越权风险、快速排查报错 | 本机沙箱无资源/网络约束；`.env` 密钥可能随文件发给外部模型；出 Bug 难定位。 | **`.env`/密钥发送前自动脱敏 + Windows Job 资源上限 + 30 秒新手引导 + 一键导出诊断包**。 |

### 1.3 北极星指标与可量化验收目标（KPIs）

| 维度 | 衡量指标 | 当前基线（实测/现状） | 上线目标值 |
|---|---|---|---|
| **能力强** | 编程评测集一次通过率（`done_when` 全绿） | 存在假完成，无硬门禁 | **≥ 90%**（`done_when` 100% 强制阻塞） |
| **花费少** | 典型多文件修改任务（T09 类）Token 峰值 | ~611K token | **下降 ≥ 40%**（`editing` 瘦身 + Prompt 分层 + LSP 即诊） |
| **产出高** | 新用户冷启动到完成首个有效任务耗时 | 无引导，多处配置割裂 | **≤ 30 秒**（内置 Onboarding + 3 个示例任务） |
| **轻便** | 界面一级入口数 / 左侧栏实现套数 / 启动闪屏 | 13 个碎片路由 / 4 套左栏 / 有白闪 | **5 个一级入口 / 1 套 `AppShell` / 0 闪屏** |
| **好用** | 自学习建议采纳率与前端交互阻塞点 | 自学习完全隐身；存在 N+1 与原生弹窗 | **对话流 1 次点击采纳技能/下一步；0 原生 `confirm`/`prompt`** |

### 1.4 明确的不做清单（Non-Goals for Internal Launch）

为保证内部快速高质量落地，本阶段明确**不做**：
- ❌ 多租户商业计费、配额账单与外部注册登录体系（保留本地轻量 owner 身份）；
- ❌ 模型供应商商务接入与微调训练；
- ❌ Windows/macOS 商业代码签名与自动静默更新服务器（内部直接分发安装包/内网包）。

---

## 二、用户体验与设计系统规范（顶尖 Designer 视角）

参考 **Linear / Raycast / Claude / Notion** 的现代桌面工具设计原则，确立以下 4 条不可妥协的设计法则：

### 2.1 四大核心设计原则

1. **渐进式披露（Progressive Disclosure）**：
   - 默认只展示用户此刻需要做决定的信息（任务结论、变更摘要、审批请求、推荐下一步）；
   - 工具调用的原始参数、Stdout 日志、LSP 诊断细节全部折叠在紧凑的时间线胶囊内，点击才展开，彻底告别"满屏滚代码日志的压迫感"。
2. **双模场景自适应，但心智模型唯一（Dual-Mode, Single Mental Model）**：
   - 全站只有一个统一操作台 `/`；顶部切换 **`[ 💻 编程 ]`** 与 **`[ 📋 日常 ]`**，右侧栏自动变身（编程看文件树/Diff/终端，日常看文档/表格/PDF 预览与导出），用户永远不需要在 13 个页面之间迷路。
3. **信任源于可预期与可撤回（Trust by Transparency & Undo）**：
   - 任何修改本地文件的操作，在执行前写快照，在界面上明示修改行数，并在结果旁直接挂接**「↩ 一键撤销本轮改动」**；
   - 本机模式若网络未隔离，在顶部状态胶囊清晰提示而非静默放行，让用户对边界有确定预期。
4. **键盘优先与零阻塞打断（Keyboard-First & Non-Blocking）**：
   - 全局 `Ctrl/Cmd + K` 呼出命令面板（切项目、切模式、开新任务、搜历史）；
   - `Esc` **仅用于关闭当前弹窗/抽屉/取消输入焦点**，严禁跨页面强行跳回首页；
   - 彻底消灭 `window.confirm` / `window.prompt` / `window.location.href` 整页刷新。

### 2.2 视觉 Token 与布局栅格系统

- **空间与布局（8px 基准栅格）**：
  - 全局左侧栏（`AppShell`）：展开态 `240px`，收起态 `56px`（小屏 `< 1024px` 自动收起）；
  - 中央对话与时间线区：自适应弹性宽度（内容阅读最大宽度 `780px` 居中，保证长文与代码可读性）；
  - 右侧产物与上下文面板：默认 `380px`~`460px`（支持拖拽调宽与一键折叠）。
- **色彩与启动一致性**：
  - 统一桌面端窗口背景色与前端 CSS 变量 `--bg`（`#e9edff`，或配套深色模式 `#0F1117`），彻底消除 Electron 启动瞬间 `#eef1f2` 带来的闪烁割裂；
  - **语义色严格克制**：
    - 主交互与 AI 强调色（Accent）：品牌靛蓝（用于发送按钮、`✨ 技能提案`、焦点边框）；
    - 成功（Success）：翡翠绿（用于测试通过、`done_when` 达成）；
    - 警告（Warning）：琥珀黄（用于本机网络未隔离提示、LSP Warning、审批待确认）；
    - 危险（Danger）：珊瑚红（用于高危命令拦截、一键回滚确认、失败状态）。
- **微动效规范**：
  - 卡片展开、抽屉滑入统一 `150ms cubic-bezier(0.16, 1, 0.3, 1)`；
  - 严禁流式输出时造成外层容器高度剧烈跳动（预留骨架高度与平滑滚动锚定）。

### 2.3 信息架构（IA）：从 13 个碎片路由收敛为 5 个核心入口

```text
全局唯一左侧栏（AppShell — 唯一导航与数据源）
├── 顶部区：空白格 Logo + [ + 新建任务 ] + 全局搜索 (Ctrl+K)
├── 工作区切换器：置顶/最近项目下拉（一键切换本地目录，实时同步模型与沙箱状态）
├── 核心一级导航（仅保留 5 项）：
│   ├── 1. 工作台（/）           ← 合并原 / 与 /tasks/detail，对话 + 执行 + 产物一体化
│   ├── 2. 调度与监控（/monitor） ← 合并原 /tasks 列表 + /monitor + 可视化 Cron/Watch
│   ├── 3. 技能与模板（/skills）  ← 合并技能市场 + AI 成长看板 + 场景/决策模板（原 /brainstorm）
│   ├── 4. 团队与消息（/team）    ← 多 Bot 协作 + 飞书/钉钉/企微长连接与审批配置
│   └── 5. 项目与设置（/settings）← 项目管理 + 全局模型/沙箱设置 + 审计归档（原 /evidence）+ 诊断包导出
└── 底部状态栏：运行健康胶囊（后端在线/沙箱等级）+ 全局唯一模型快切器
```

> **路由清理决议**：彻底删除 `/features`（假开关页）与 `/daily`（习惯打卡）；`/brainstorm` 并入 `/skills`；`/reverse-lab` 降级为按需加载的扩展技能面板。

---

## 三、核心界面线框图与微交互状态机（逐组件详设）

### 3.1 主工作台（`/`）完整线框图

```text
┌──────────────┬─────────────────────────────────────────┬──────────────────────────────┐
│ 统一左栏      │ 中央：指令与执行主区                      │ 右侧：动态产物与上下文面板    │
│              │                                         │                              │
│ [工作区:kbgai]│ 顶部栏：[💻 编程] [📋 日常] | 模型快切   │ Tab 自动随顶部模式切换：       │
│              │        [🛡 沙箱:受限] | 1.2s / 3.4K tok  │                              │
│ [+ 新建任务]  │                                         │ ── [编程模式] ──             │
│              │ ┌─────────────────────────────────────┐ │  [文件树] [Diff] [终端] [验收] │
│ 今天         │ │ 🧠 已应用历史经验：测试需带 --env=local│ │                              │
│  ● 修复登录   │ │    [ 👍 仍有效 ]  [ 🗑 已过时淘汰 ]   │ │  ▾ src/                      │
│  ● 写单元测试 │ ├─────────────────────────────────────┤ │    ▾ auth/                   │
│              │ │ 执行时间线（低噪声折叠卡片）           │ │      M login.py (+18/-4)     │
│ 最近 7 天    │ │  ▸ ✓ 读取 3 个文件（0.4s）            │ │      + test_login.py (+42)   │
│  ✓ 周报汇总  │ │  ▾ ✓ 修改 auth/login.py (+18 / -4)   │ │  ──────────────────────────  │
│              │ │      LSP 即诊：0 Error / 0 Warning    │ │  [ ↩ 一键回滚本轮全部修改 ]   │
│ 定时运行中(2) │ │  ⚠ 请求审批：执行 pytest -q          │ │                              │
│              │ │     [ 批准 ] [ 拒绝 ] [ 本目录总是允许 ]│ │ ── [日常模式] ──             │
│              │ ├─────────────────────────────────────┤ │  [产物预览] [导出] [定时规则] │
│              │ │ ✨ 新技能提案：auth-test-guard        │ │                              │
│              │ │    "自动在改完鉴权后补跑单测并校验"     │ │  📄 周报_2026W48.docx (预览) │
│              │ │    [ 一键启用 ]  [ 微调规则 ] [ 忽略 ] │ │  [ 📂 打开所在文件夹 ] [导出] │
│              │ ├─────────────────────────────────────┤ │                              │
│              │ │ 💡 推荐下一步：                        │ │                              │
│              │ │   [ 补充异常单测 ]  [ 生成 Commit 信息 ]│ │                              │
│              │ └─────────────────────────────────────┘ │                              │
│              │                                         │                              │
│              │ 底部 Composer：                          │                              │
│              │  @引用文件  /快捷技能  📎附件  [ 发送 ]   │                              │
└──────────────┴─────────────────────────────────────────┴──────────────────────────────┘
```

### 3.2 关键组件交互与状态机规范

| 组件 | 核心状态与交互行为 | 边界与异常处理 |
|---|---|---|
| **`<TimelineFeed />`（时间线）** | 1. **折叠态**（默认）：同类只读步骤（如连续 3 次 `read_file`）自动聚合为一行 `▸ ✓ 读取 3 个文件（0.4s）`；<br>2. **写文件态**：展示文件名、`+新增/-删除` 行数、LSP 诊断红绿灯；<br>3. **流式态**：SSE 实时增量追加，底部显示当前正在执行的工具脉冲指示器。 | SSE 超过 30s 无任何消息（含 `ping`）时，顶部展示轻量化黄条「连接已断开，正在重连 (1/5)…」，通过 `Last-Event-ID` 断点续传，不丢失已渲染卡片。 |
| **`<ApprovalCard />`（审批卡片）** | 展示风险等级（高/中）、精确待执行命令或写入路径、影响说明；提供 `[ 批准 ]`、`[ 拒绝并说明原因 ]`、`[ 本工作区总是允许此类操作 ]`。 | 若用户开启了飞书/钉钉通道且桌面端未在 30s 内响应，**同步向 IM 推送交互审批卡片**；任一端点击批准后，另一端卡片自动变为「已由 XX 于 17:40 批准」锁定态。 |
| **`✨ 技能提案` 与 `💡 下一步` 卡片** | 1. 任务收尾时若产生 `status="proposed"` 技能，直接在结果下方渲染提案卡，点击 `[ 一键启用 ]` 立即转正并弹出 Toast；<br>2. `💡 推荐下一步` 渲染 1~3 个胶囊按钮，点击后自动填充/发送对应指令。 | 点击 `[ 忽略 ]` 后静默归档，同一模式 7 天内不再重复打扰。 |
| **首次启动 30 秒 Onboarding** | 检测到无最近工作区时，中央区域不显示空白聊天框，而是展示 3 步卡片：<br>**Step 1** 选择/拖入一个本地项目或文档文件夹 → **Step 2** 自动完成环境体检（Git/Python/沙箱边界提示）→ **Step 3** 展示 3 个可一键试跑的任务卡片（`修一个示例 Bug` / `总结一份本地 PDF` / `清洗一份 Excel 表`）。 | 环境体检发现缺少可选依赖时，不阻断启动，仅在对应卡片标注「可离线使用基础文件能力」。 |

---

## 四、全量源码缺陷清单（31 项，已精确定位）

### 4.1 P0 静默 Bug（6 项 — 直接导致功能失效或崩溃）

| 编号 | 模块与精确位置 | 现象与根因 | 成熟修复方案 |
|---|---|---|---|
| **BUG-01** | `services/api/src/blank_api/services/tools/filesystem.py:159` | `if suffix in IMAGE_SUFFIXES and not _routed(context):` 在启用 `fs_broker` 或任务隔离时跳过读图逻辑，导致模型读取图片拿到原始二进制乱码。 | 解除 `not _routed(context)` 限制；在隔离/broker 路径下通过只读字节流接口走多模态图片封装返回，补充隔离模式读图单测。 |
| **BUG-02** | `services/api/src/blank_api/services/agent/graph.py` (`_grow_budget`) & `models/routing.py` (`FallbackAdapter`) | `_grow_budget` 直接修改 `adapter.max_tokens_override`，但 `FallbackAdapter` 未透传给内部 adapter，多模型回退下输出预算动态扩容静默失效，且非并发安全。 | 将 `max_tokens_override` 改为每次请求的显式 `CallOptions` 参数，从 `graph.py` 传入 `FallbackAdapter` 并透传到底层 Adapter。 |
| **BUG-03** | `services/api/src/blank_api/services/agent/scenarios.py:197` (`WRITE_TOOLS`) | `WRITE_TOOLS` 漏了 `write_file`、`edit_file`、`notebook_edit`，导致 `brainstorm`/只读场景仍向模型暴露写工具 Schema。 | 在工具注册元数据统一标注 `ToolSpec.is_mutating = True`，`scenarios.py` 按元数据自动过滤，禁止手动维护字符串黑名单。 |
| **BUG-04** | `services/api/src/blank_api/services/sandbox/boundary.py:42` | `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 常量错写成 `0x00020008`（正确值按 `windows_appcontainer.py:61` 为 `0x00020009`），导致 AppContainer 能力探针损坏。 | 统一引用 `windows_appcontainer.py` 的权威常量 `0x00020009`，消除跨文件硬编码重复。 |
| **BUG-05** | `services/api/src/blank_api/services/sandbox/local.py` (`LocalSandbox.run`) | 先启动子进程，再调用 `windows_job.assign_pid()` 绑定 Job Object，存在子进程逃逸资源限制的竞态窗口。 | 统一改用 `CREATE_SUSPENDED` 启动进程 → 立即 `AssignProcessToJobObject` → `ResumeThread` 恢复执行。 |
| **BUG-06** | `services/api/src/blank_api/services/agent/graph.py` (`_call_adapter`, `_clear_speculation`) | 无条件读写 `session.speculative`，在测试或轻量化 session 缺少该属性时抛 `AttributeError`（导致 6 个 stream 用例失败，见 `UP-02`）。 | 统一改为 `getattr(session, "speculative", False)` 与 `hasattr` 安全赋值，并在 session 基类初始化默认值。 |

### 4.2 P0 架构缺陷（7 项 — 导致并发阻塞、数据丢失与无法演进）

| 编号 | 模块与精确位置 | 现状问题 | 修复方案 |
|---|---|---|---|
| **ARCH-01** | `services/api/src/blank_api/api/routes/tasks.py`（134KB，2,862 行） | 23 个 HTTP 路由与核心编排函数（`persist_task`、`launch_task`、`recover_pending_runs` 等）混写，`worker.py`、`threads.py`、`channels/inbound.py` 反向导入路由层形成循环依赖。 | 拆分为薄路由层（`routes/tasks.py` < 400 行）+ 领域服务层（`services/orchestration/runner.py`）。 |
| **ARCH-02** | `services/api/src/blank_api/services/agent/graph.py`（9,457 行）& `runtime.py`（2,564 行） | LangGraph 状态图、Prompt、Hooks、审批、压缩、交付格式化全部耦合在一个文件。 | 拆分为 `agent/graph_builder.py`、`agent/nodes/`、`agent/gates.py`、`agent/recovery.py`、`agent/prompts/`。 |
| **ARCH-03** | 任务执行引擎（3 条并行路径） | `POST /tasks/{id}/run` 绕过队列直跑；`threads.py` 写 `task.goal_spec`，`tasks.py` 写 `task.dispatch_snapshot`；`team.py` 用内存 `_BACKGROUND` 跑。 | 废弃直跑路由，统一走 `Dispatch & Policy Service` → `AgentRun` 队列；`task.dispatch_snapshot` 为唯一调度真源。 |
| **ARCH-04** | `AgentRun` 表 (`uq_agent_runs_task_id`) | `task_id` 加了唯一约束，每次重试直接覆盖原记录（`attempts=0`），丢失历史执行轨迹与账单。 | 移除唯一约束，改为 `Task 1 : N AgentRun`（联合 `(task_id, attempt_no)`），保留每次重试记录。 |
| **ARCH-05** | `worker.py` (`run_once`) | 在 `for` 循环中**串行**消费所有待执行 run，一个长任务阻塞全队列。 | 改为 `asyncio.Semaphore(MAX_CONCURRENT_RUNS)` 并发池调度 + 心跳超时恢复。 |
| **ARCH-06** | `db.py` (SQLite 连接配置) | 未开启 `PRAGMA journal_mode=WAL` 与 `PRAGMA foreign_keys=ON`，多协程并发写撞单写锁报错 `database is locked`，且级联删除失效。 | 连接初始化 Hook 强制执行 `WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=10000`。 |
| **ARCH-07** | `team.py` (`_BACKGROUND` + `TeamOrchestrator`) | 团队模式用进程内存托管协程，完全绕过 `AgentRun` 队列，重启即任务蒸发。 | 团队子任务全部作为标准 `AgentRun` 入库调度，支持持久化恢复与进度汇总。 |

### 4.3 P1 功能残缺（8 项）

1. **写文件回执上下文爆炸**（`UP-01`）：`tools/editing.py` 返回全文或完整 diff，实测 T09 任务单次约 611K token。
2. **`done_when` 验收不阻塞**（`UP-06`）：校验失败仅记 `finding`，任务仍标记 `SUCCEEDED`（假完成）。
3. **安全钩子默认放行**（`UP-07`）：`HookSpec.on_failure` 默认 `warn`，高危拦截异常时工具照常执行。
4. **Windows 本地沙箱无网络/资源限制**（`UP-04`）：默认 `filesystem_enforced=False, network_enforced=False`，Job Object 未设 CPU/内存上限。
5. **熔断与 Subagent 状态纯内存化**：`StallBreaker`、`ProgressSentinel`、`subagent.py`（`_BACKGROUND`/`_CONVERSATIONS`）、`workflow.py` 恢复缓存仅在内存，中断或重启即丢失。
6. **测试体系污染**（`UP-03`）：`services/api/tests/` 积累 417 个按版本号命名的文件（约 8MB），存在 worktree 残留与旧 schema 失败（35 个失败）。
7. **IM 渠道国内断层**：仅钉钉支持 `stream.py` 长连接，飞书/企微需公网 HTTPS 回调；且无 IM 交互式审批卡片。
8. **SSE 跨进程退化与断线无心跳**（`UP-05`）：`LiveFeed` 进程内隔离，独立 `worker.py` 运行时前端退化为轮询，且无 15s 心跳重连。

### 4.4 P2 前端与桌面端硬伤（10 项）

1. **4 套互不兼容的左侧栏**：`bot/sidebar.tsx`、`workbench/workbench.tsx`、`app-rail.tsx`、`side-nav.tsx`。
2. **双份模型配置分裂**：`localStorage["blank.model"]`（`AppShell`）vs `localStorage["blank.bot.provider"]`（`BotApp`）。
3. **前端巨无霸文件**：`tasks/api.ts`（95KB）、`team/team-view.tsx`（76KB）、`bot/bot-app.tsx`（72KB）。
4. **文件树目录无法点击展开**：`bot/panel.tsx:579` 写了 `disabled={entry.is_dir}`。
5. **N+1 请求风暴**：`bot/bot-app.tsx:183` 在 `threadTurnKey` 变化时对每轮并发发 `fetchEvidence(id)`。
6. **样式与 HTML 语义 Bug**：`bot/composer.tsx:306` 残留 `bg-$1`；`bot/sidebar.tsx:338` `<button>` 内嵌 `<span role="button">`。
7. **阻塞弹窗与整页重载**：多处使用 `window.confirm`/`window.prompt` 与 `window.location.href`（导致 Electron 整页刷新）。
8. **反直觉快捷键与 4 个孤儿路由**：`AppShell` 全局 `Esc` 从任何页面跳回 `/`；`/daily`、`/brainstorm`、`/features`、`/reverse-lab` 未接入主导航。
9. **桌面端硬编码与启动白闪**：`desktop/main.cjs:429` 硬编码 `E:\env\blank-tmp`；启动背景 `#eef1f2` 与 `--bg: #e9edff` 冲突；点击系统通知调用 `win.loadURL` 整页重载。
10. **CLI 配置孤岛**：`packages/cli/src/index.mjs` 读取 `~/.blank/config.json`，与 Web/Desktop 不互通。

---

## 五、Phase 1：即刻止血与降本改造（UP-01~03 + P0 Bug 清零）

### 5.1 修复 `speculative` 属性兼容性 Bug（`UP-02` / `BUG-06`）
- **文件**：`services/api/src/blank_api/services/agent/graph.py`
- **改动**：`_call_adapter` 和 `_clear_speculation` 中所有直接读写 `session.speculative` 的地方，改为 `getattr(session, "speculative", False)` 与 `if hasattr(session, "speculative"): session.speculative = ...`；同步更新测试 mock 签名。
- **验收**：以下 6 个用例全绿（零修改测试逻辑）：
  ```bash
  cd services/api
  uv run pytest tests/ -k "stream_fallback or reasoning_only_stream or skill_model_v52 or truncated_tool_call_v175" -v
  ```

### 5.2 写文件工具回执瘦身（`UP-01`）
- **文件**：`services/api/src/blank_api/services/tools/editing.py`、`services/api/src/blank_api/services/agent/payload.py`（检查 `DROP_DIFF_TOOLS` 避免重复）
- **改动**：写入成功后只返回一行摘要：
  ```python
  return ToolResult(content=f"已写入 `{path}`（{changed_lines} 行变更）")
  ```
  > 写入失败仍返回完整错误信息；前端 Diff 面板数据源是 `file_changes` 表，不受影响。
- **验收**：写入 100 行文件返回字符串 < 100 字符；真机跑 T09 任务 Token 峰值下降 ≥ 25%。

### 5.3 修复其余 5 个 P0 静默 Bug（`BUG-01` ~ `BUG-05`）与 SQLite 并发（`ARCH-06`）
- `filesystem.py:159`：移除 `not _routed(context)` 限制，通过 `fs_broker` 安全只读字节流恢复隔离模式下读图能力；
- `graph.py` & `models/routing.py`：`max_tokens_override` 改为请求级 `CallOptions` 参数传入 `FallbackAdapter` 并透传到底层 adapter；
- `scenarios.py:197`：基于 `ToolSpec.is_mutating` 自动过滤只读场景写工具（覆盖 `write_file`、`edit_file`、`notebook_edit`）；
- `sandbox/boundary.py:42`：常量改为引用 `windows_appcontainer.py` 的 `0x00020009`；
- `sandbox/local.py`：进程创建改用 `CREATE_SUSPENDED` → 绑定 Job Object → `ResumeThread`；
- `db.py`：连接初始化强制执行 `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000;`。

### 5.4 测试套件归档与隔离（`UP-03`）
- **步骤**：
  1. 在 `services/api/tests/` 下新建 `_archive/`，将同时满足"带旧版本号后缀 + 已有更新版本 + 全量持续失败"的旧测试移入，并在 `services/api/pyproject.toml` 配置 `norecursedirs = ["_archive", ".venv", "__pycache__", ".tmp-test"]`；
  2. `services/api/conftest.py` 中为 `temp_worktree` fixture 补充 `tmp_path` 隔离与 `yield` 后强清理；
  3. 修复 `test_approval_card_v101`、`test_permission_modes_v11`、`test_handoff_e2e_v12`、`test_unknown_facts_e2e_v147`、`test_output_styles_v47` 的断言格式（不改产品接口）。
- **验收**：`uv run pytest tests/ --ignore=tests/_archive -x -q` 结果为 `0 failed`。

---

## 六、Phase 2：底座解耦与 Claude Code 级编程重构（UP-04, 06, 07）

### 6.1 统一执行底座（`ARCH-01` ~ `ARCH-05`, `ARCH-07`）
- **拆解 `routes/tasks.py`（134KB -> < 400 行）**：将 `persist_task`、`launch_task`、`_execute_claimed_run`、`recover_pending_runs`、`recover_orphan_approvals` 下沉至 `services/orchestration/runner.py`，消除反向循环导入；废除绕过队列的 `POST /tasks/{id}/run`。
- **统一状态与队列**：仅保留 `task.dispatch_snapshot` 为唯一调度真源；移除 `AgentRun` 的 `uq_agent_runs_task_id` 唯一约束，改为 `(task_id, attempt_no)`；`worker.py:run_once()` 改用 `asyncio.Semaphore(MAX_CONCURRENT_RUNS)` 并发消费；`team.py` 子任务全部作为标准 `AgentRun` 入库调度。

### 6.2 Agent 内核重构与 Claude Code 级编程能力
1. **拆解 `graph.py`（9,457 行）**：拆分为 `agent/graph_builder.py`、`agent/nodes/{plan,act,validate,review,summarize}.py`、`agent/gates.py`、`agent/recovery.py`、`agent/prompts/`。
2. **Prompt 三层按需加载（首轮 Token 降 30%~40%）**：
   - **静态前缀层**（System + 精简工具 Schema，跨轮字节不变以命中 Prompt Cache，去掉 `apply_patch` 与 `edit_file` 双工具重复）→ **仓库记忆层**（`AGENTS.md`/`CLAUDE.md` + 命中 `k=1` 经验）→ **动态工作态层**（对话流 + 当前快照）；`daily` 模式不注入编程规则，`coding` 模式不注入办公指令。
3. **RepoMap（AST 骨架索引）+ LSP 编辑后即时诊断**：
   - 进任务前用 `tree-sitter` 增量构建轻量目录与顶层类/函数签名树（2K~4K token），让模型首轮即定位精确文件与行号；
   - 每次 `edit_file` / `write_file` 成功后，自动调用 `services/lsp/`（Python: `pyright`/`ruff`，TS: `tsc --noEmit`）拉取本文件新增 Error/Warning，直接附在工具回执末尾，200ms 内当场修掉低级错误。
4. **断点与 Subagent 持久化**：
   - 将 `StallBreaker`、`ProgressSentinel`、`workflow` 恢复状态纳入 `AgentState`（存入 `AgentCheckpoint`）；
   - 新增 `subagent_sessions` 表落库，支持并发派发只读探索（Explore）与验证子智能体，重启后 `send_message` 仍可续聊。

### 6.3 质量硬门禁与 Windows 沙箱加固（`UP-06`, `UP-07`, `UP-04`）
1. **`done_when` 强制阻塞（`UP-06`）**：
   - **文件**：`services/agent/goals.py`、`delivery.py`、`closure.py`
   - **改动**：`evaluate_done_when` 未通过时抛出 `DoneWhenNotMetError`，`delivery.py`/`closure.py` 捕获后强制将任务置为 `FAILED`（或打回修复迭代直到耗尽预算），严禁标记 `SUCCEEDED`。
   - **验收**：新增 `tests/test_goals_blocking_v1.py`，断言验收不达标时状态必为 `FAILED`。
2. **安全 Hook 强阻断（`UP-07`）**：
   - **文件**：`services/agent/governance.py`、`services/permissions.py`、`services/tools/gateway.py`
   - **改动**：`HookSpec` 增加 `on_failure: Literal["ignore", "warn", "block"] = "warn"`，安全/审批类钩子强制设为 `"block"`，抛异常时工具网关必须返回 `BLOCKED` 阻断执行。
   - **验收**：模拟权限钩子抛 `RuntimeError`，`gateway.call_tool("delete_file", ...)` 必须返回 `status == "BLOCKED"`。
3. **Windows 沙箱边界与本机快照（`UP-04` + Safe Undo）**：
   - `services/sandbox/rules.py`：本机模式返回 `sandbox_constraints.network_isolated: false` 并推送 `sandbox.warning` 事件；
   - `services/sandbox/windows_job.py`：绑定 Job Object 后设置 `JobObjectExtendedLimitInformation`（`CPU_RATE_LIMIT=80`、`MEMORY_LIMIT_MB=2048`、`PROCESS_COUNT_LIMIT=32`，非管理员失败仅告警不阻断）；
   - **本机一键快照回滚**：每次工具改/删文件前自动写入轻量快照（Git 隐藏 ref 或 `.blank/snapshots/<run_id>/`），前端提供「↩ 一键回滚本轮修改」。

---

## 七、Phase 3：前端统一重构与 WorkBuddy 级自学习闭环（UP-05）

### 7.1 前端组件统一、Hard Bug 清零与 SSE 跨进程修复（`UP-05`）
1. **统一全局 Store**：用 Zustand / TanStack Query 管理 `currentProviderId`、`currentProjectId`、`threads`，删除 `blank.model` 与 `blank.bot.provider` 双重 `localStorage`。
2. **拆解巨型文件 & 公共组件**：拆解 `tasks/api.ts`（95KB）、`team-view.tsx`（76KB）、`bot-app.tsx`（72KB）；抽离 `<TimelineFeed />`、`<ApprovalCard />`、`<QuestionCard />`、`<ConfirmDialog />`、`<WorkspaceTree />`、`<DiffViewer />`。
3. **清零 10 项交互硬伤**：
   - 修复 `bot/panel.tsx:579` 目录 `disabled`（改为异步懒加载展开）；
   - 修复 `bot-app.tsx:183` N+1 `fetchEvidence`（线程汇总接口直接返回）；
   - 修复 `composer.tsx:306` `bg-$1` 样式与 `sidebar.tsx:338` 按钮嵌套；
   - 用 `<ConfirmDialog />` 替换 `window.confirm`/`prompt`；用 `router.push()` 替换 `window.location.href`；取消 `AppShell` 全局 `Esc` 跳回 `/`；
   - **Desktop（`desktop/main.cjs`）**：删除 `E:\env\blank-tmp` 硬编码改用 `app.getPath("temp")`；启动背景色统一为 `#e9edff`；点击通知改用 `webContents.send("blank:navigate-thread", threadId)` 无刷新切换；
   - **CLI（`packages/cli/src/index.mjs`）**：新增 `blank run "<任务>" --mode dev|daily` 流式执行，并打通统一配置接口。
4. **SSE 心跳重连与跨进程共享（`UP-05`）**：
   - 后端 `api/routes/monitor.py` 每 15s 发 `event: ping`，支持 `Last-Event-ID` 续传；独立 `worker.py` + `redis_url` 时通过 Redis Pub/Sub 桥接 `LiveFeed`；
   - 前端 `web/src/components/tasks/use-task-events.ts` 超 30s 无消息自动指数退避重连（最多 5 次）；
   - 将 `scripts/smoke_flow.py`（15 项端到端冒烟，修复审批留痕红项）接入 `./check.sh` CI，确保 `15/15` 全绿。

### 7.2 自学习与主动推荐飞轮（打通已有底座，超越 WorkBuddy）

后端已存在 `experience.py`（774 行）、`persona.py`、`skillsmith.py`（263 行）、`skill_proposals.py`（278 行）、`learning.py`（62 行），但前端完全不可见且 `skillsmith` 仅硬编码 `rigor`/`concise`。改造如下：
1. **通用化 `skillsmith.py`**：新增「工作流模式归纳器」——当同一工作区连续 ≥2 次人工纠偏或经历"报错重试 → 验证通过"/3 步以上可复用流程时，自动生成规范草稿 Skill（`source="agent_created", status="proposed"`），并支持对已有 Skill 提出 `improvement` 修订建议。
2. **Turn 级推荐引擎（替换纯关键词 `skill-suggest.ts`）**：在 `summarize` 节点结束时零额外轮次顺带产出 1~3 条上下文感知的**下一步建议**（Action Chips）。
3. **主对话流直出 3 类卡片**：
   - **「✨ 新技能提案」卡片**：`[ 一键启用 ] [ 微调规则 ] [ 忽略 ]`；
   - **「💡 推荐下一步」Action Chips**：点击即发起对应动作（如补充单测、生成 Commit、导出 PDF）；
   - **「🧠 命中经验」轻量徽标**：展示召回出处，支持一键 `[ 👍 仍有效 ]` / `[ 🗑 已过时淘汰 ]`（调用 `POST /learning/experience/{fp}/retire`）。
4. **「AI 成长与经验库」看板**：将 `LearningPanel` 升级并入 `/skills` 顶部 Tab，直观展示 `agent_created` 技能数、避坑经验库、命中次数与节省 Token。

---

## 八、Phase 4：日常办公、国内生态与内部上线最小必改（UP-08）

### 8.1 日常办公与国内 IM 生态
1. **原生 Office/PDF 工具链**：后端内置 `doc_read` / `doc_write`（基于 `python-docx`、`openpyxl`、`python-pptx`、`pymupdf`），支持结构化读取、表格提取、模板填充与导出；日常模式右侧栏支持表格/Markdown/PDF 实时预览。
2. **飞书/企微免公网长连接**：`channels/feishu.py` 接入官方 WebSocket 长连接（`lark_oapi.ws.Client`，对齐钉钉 `stream.py`）及企微兼容长连接，内网电脑零公网 IP 开箱即用。
3. **IM 交互式审批卡片**：高风险操作触发审批时，向钉钉/飞书推送带 **`[ 批准 ] / [ 拒绝 ]`** 按钮的交互卡片，手机端点击直接回调解锁桌面端任务。
4. **可视化定时与 Watch**：在 `/monitor` 提供表单化 + 自然语言创建 Cron 定时汇总（如每日 18:00 发日报到飞书）与本地文件夹 Watch（如新入发票 PDF 自动提取汇总到 Excel）。

### 8.2 内部上线前的 5 项最小必改（非模型/非签名）
1. **冷启动 30 秒新手引导（Onboarding）**：首次启动 3 步引导（选工作目录 → 一键自检沙箱/Git/环境 → 提供 3 个一键试跑示例任务）。
2. **本机文件快照一键回滚（Safe Undo）**：确保内部同事敢放心让 Agent 改代码、改文档。
3. **国内网络与镜像适配**：包安装与环境探测支持配置 PyPI 清华/阿里源、`npmmirror`，保证离线工具（读图、AST 索引、本地检索）100% 不依赖外网。
4. **隐私保护与密钥脱敏（Secret Scrubbing）**：发送外部模型前默认跳过 `.env`、`*.pem`、`id_rsa`，自动将 `sk-`、AccessKey、数据库连接串等脱敏为 `***REDACTED***`。
5. **一键导出诊断包**：设置页提供按钮自动打包脱敏后的配置、`AgentRun` 状态、后端日志与前端错误，方便内部快速排障。

### 8.3 评测集与自动对比（`UP-08`）
- 在 `evals/tasks/` 扩充 5 类任务（编程修复、功能新增、文档同步、代码重构、多文件联动，每类 ≥ 5 题），扩充 `evals/compare.mjs` 自动输出通过率、耗时、Token 与竞品对比表：
  ```bash
  cd evals
  node compare.mjs --tasks tasks/ --model kbgai --baseline workbuddy
  ```

---

## 九、一遍改完执行流水线与核心文件路径速查

### 9.1 推荐执行流水线（按依赖顺序一遍改完）

```text
[Step 1: 止血]  UP-02(speculative) → UP-01(editing瘦身) → BUG-01~05 + SQLite WAL → UP-03(测试归档隔离)
      ↓
[Step 2: 底座]  拆 tasks.py/graph.py + 统一 RunExecutor & AgentRun 1:N & Worker 并发池 & Team 入库
      ↓
[Step 3: 编程]  Prompt 分层 + RepoMap + LSP 即时诊断 + UP-06(done_when阻塞) + UP-07(Hook阻断) + UP-04(沙箱) + 快照回滚
      ↓
[Step 4: 前端]  UP-05(SSE心跳/Redis桥接/冒烟进CI) → 5入口AppShell + 全局Store + 拆巨型组件 + 修10项硬伤 + Desktop/CLI打通
      ↓
[Step 5: 智能]  skillsmith 通用化 + Turn 级推荐引擎 + 对话流 ✨/💡/🧠 卡片 + AI 成长看板
      ↓
[Step 6: 上线]  Office/PDF 工具链 + 飞书长连接 + IM审批卡片 + 可视化Cron/Watch + Onboarding/镜像/脱敏/诊断包 + UP-08评测
```

### 9.2 核心文件路径速查表

| 领域 | 关键文件路径 | 对应改造 |
|---|---|---|
| **后端入口与数据库** | `services/api/src/blank_api/app.py`, `db.py` | WAL/外键/并发 |
| **路由与调度** | `api/routes/tasks.py`, `threads.py`, `worker.py`, `team.py`, `api/routes/monitor.py` | `ARCH-01,03,05,07`, `UP-05` |
| **Agent 核心** | `services/agent/graph.py`, `runtime.py`, `goals.py`, `governance.py`, `delivery.py`, `closure.py`, `scenarios.py` | `UP-02,06,07`, `BUG-02,03`, `ARCH-02` |
| **工具与 LSP** | `services/tools/editing.py`, `filesystem.py`, `gateway.py`, `workflow.py`, `subagent.py`, `services/lsp/` | `UP-01,07`, `BUG-01`, LSP/RepoMap |
| **自学习底座** | `services/agent/experience.py`, `persona.py`, `skillsmith.py`, `services/tools/skill_proposals.py`, `api/routes/learning.py` | 通用归纳器 + Action Chips |
| **沙箱与权限** | `services/sandbox/boundary.py`, `local.py`, `windows_restricted.py`, `windows_job.py`, `rules.py`, `permissions.py` | `UP-04,07`, `BUG-04,05` |
| **IM 通道** | `services/channels/dingtalk.py`, `stream.py`, `feishu.py`, `wecom.py`, `inbound.py` | 飞书长连接 + IM 审批卡片 |
| **前端核心** | `web/src/components/bot/bot-app.tsx`, `panel.tsx`, `composer.tsx`, `sidebar.tsx`, `tasks/api.ts`, `tasks/use-task-events.ts`, `team/team-view.tsx`, `lib/skill-suggest.ts` | 5 入口重构、Store 统一、硬伤清零、`UP-05` |
| **桌面端与 CLI** | `desktop/main.cjs`, `packages/cli/src/index.mjs` | 去硬编码、无刷新跳转、配置打通 |
| **测试与评测** | `services/api/tests/`, `conftest.py`, `scripts/smoke_flow.py`, `evals/compare.mjs`, `check.sh` | `UP-03,05,08` |
