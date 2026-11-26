# 空白格 AI 操作台（kbgai）全系统审查与重构设计方案

> 基于对 `services/api/src/`、`web/src/`、`desktop/`、`packages/cli/src/` 全量源码的直接审查。  
> 战略目标：**在中国打开市场，编程能力超越 WorkBuddy / Qoder，日常工作处理超越 WorkBuddy / Codex。**

---

## 一、全量源码审查结论：系统目前的 8 个核心病灶

通过逐模块扫描后端、前端、桌面端与 CLI 源码，当前系统最大的问题不是"缺功能"，而是**功能堆叠但底座分裂、编程与日常两张皮、关键链路存在静默失效**：

### 1.1 架构与执行层（Backend）
1. **上帝文件与循环分层**：
   - `services/api/src/blank_api/api/routes/tasks.py` 达 **134KB（2,862 行）**，把 23 个 HTTP 路由和核心编排函数（`persist_task`、`launch_task`、`recover_pending_runs` 等）混在一起；`worker.py`、`threads.py`、`channels/inbound.py` 反向导入路由层，形成循环依赖。
   - `services/api/src/blank_api/services/agent/graph.py` 达 **9,457 行**、`runtime.py` 达 **2,564 行**，状态机、提示词、钩子、审批、压缩、交付格式化全部耦合。
2. **三套并行的执行入口与分裂状态**：
   - `POST /tasks/{id}/run` 绕过 `AgentRun` 队列直接在 HTTP 请求内跑 `AgentRuntime.run()`；
   - `POST /tasks/{id}/start` 和 `POST /threads/{id}/turns` 走队列，但 `threads.py` 把调度状态写进 `task.goal_spec`，而 `tasks.py` 写进 `task.dispatch_snapshot`；
   - `team.py` 完全绕过 `AgentRun` / `worker.py`，用内存里的 `TeamOrchestrator` 和模块级 `_BACKGROUND` 跑任务。
3. **SQLite 并发与数据模型隐患**：
   - `db.py` 未开启 `PRAGMA journal_mode=WAL` 与 `PRAGMA foreign_keys=ON`，多后台协程（`run_manager`、`background_supervisor`、`StreamSupervisor`、HTTP/SSE）并发写会撞回滚日志单写锁，且级联删除失效，迫使路由层手动删子表。
   - `AgentRun` 对 `task_id` 加了唯一约束（`uq_agent_runs_task_id`），导致每次重试都会覆盖原记录（`attempts=0`），**丢失每次执行的真实历史**。
   - `worker.py:run_once()` 串行遍历所有待执行 run，一个长任务会卡死整个独立 worker。
4. **重启即失忆的内存态**：
   - `AgentSession.stall`（`StallBreaker`）、`AgentSession.progress`（`ProgressSentinel`）、`subagent.py` 的 `_BACKGROUND` / `_CONVERSATIONS`、`tools/workflow.py` 的恢复缓存全部仅存进程内存；一旦触发审批中断（LangGraph `interrupt`）或服务重启，计数与会话句柄全部丢失。

### 1.2 编程引擎与沙箱层（Agent / Tools / Sandbox）
5. **多处真实代码 Bug 拖累编程体验**：
   - **读图链路在沙箱/隔离下失效**：`tools/filesystem.py:159` 判断 `if suffix in IMAGE_SUFFIXES and not _routed(context):`，开启 `fs_broker` 或任务隔离时直接跳过读图，把图片当文本读并报错。
   - **输出预算自愈在多模型下静默失效**：`graph.py:_grow_budget` 直接修改 `adapter.max_tokens_override`，但 `models/routing.py` 的 `FallbackAdapter` 未透传该属性给内部 adapter，导致截断重生成完全无效，且并发不安全。
   - **场景工具过滤漏网**：`scenarios.py:197` 的 `WRITE_TOOLS` 漏了 `write_file`、`edit_file`、`notebook_edit`，导致 `brainstorm` 等只读场景依然把写工具 schema 喂给模型。
   - **编辑工具冗余**：`apply_patch` 与 `edit_file`/`write_file` 同时常驻 `CORE_TOOLS` 和 `LEAN_TOOLS`，白白消耗每轮 schema token。
6. **Windows 沙箱与边界隐患**：
   - `sandbox/boundary.py:42` 把 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 错写成 `0x00020008`（正确应为 `windows_appcontainer.py:61` 的 `0x00020009`），导致 AppContainer 能力探针损坏。
   - `LocalSandbox.run()` 先启动进程再调用 `windows_job.assign_pid()`，存在子进程逃逸 Job Object 限制的竞态窗口（不像 `windows_restricted.py` 用 `CREATE_SUSPENDED`）。
   - 默认 Windows 本机模式无文件与网络 OS 级隔离（`filesystem_enforced=False, network_enforced=False`）。

### 1.3 日常办公与国内生态层（Daily / IM Channels）
7. **IM 通道能力严重不对称**：
   - `channels/dingtalk.py` + `stream.py` 支持免公网 WebSocket 长连接，但 `feishu.py` 和 `wecom.py` 仍要求用户自备公网 HTTPS 回调地址（飞书未接官方长连接），国内个人/中小团队几乎无法开箱接收飞书/企微消息。
   - IM 只做到"文本进出"，**审批卡片未同步到 IM**——Agent 触发高风险审批后，用户在钉钉/飞书里无法直接点"批准/拒绝"，必须回到桌面端操作。
8. **日常工作能力被边缘化**：
   - `/daily`、`/brainstorm`、`/features` 已从导航中摘除变成孤儿路由；`SkillMarketPanel` 深埋在设置页三级菜单里。
   - 日常办公缺少结构化工作流（文件批处理、Excel/Word/PDF 生成与解析、定时任务可视化管理、会议纪要转办），仅靠前端一个 `daily` 场景标签切换，本质上还是让日常用户面对一个开发者 IDE 面板。

### 1.4 前端、桌面端与 CLI 层（Web / Desktop / CLI）
9. **页面与状态严重割裂**：
   - **4 套并存的左栏**：`bot/sidebar.tsx`（`/`）、`workbench/workbench.tsx`（`/tasks`）、`app-rail.tsx`（其余 11 页）、`side-nav.tsx`（历史遗留）。
   - **模型状态双真源**：`AppShell` 的 `ModelProvider` 存 `localStorage["blank.model"]`，而首页 `BotApp` 完全无视它，自己存 `localStorage["blank.bot.provider"]`。
   - **前端上帝组件与冗余实现**：`tasks/api.ts`（95KB）、`team/team-view.tsx`（76KB）、`bot/bot-app.tsx`（72KB）；`team-view.tsx` 重复造了一套审批/提问/输入栏组件；3 个模块各自实现文件树和 Diff 视图。
10. **交互细节与硬伤**：
    - `bot/composer.tsx:306` 存在残留替换符 `bg-$1` 导致拖拽上传遮罩无背景；
    - `bot/panel.tsx:579` 文件树目录被设 `disabled={entry.is_dir}`，用户无法点开文件夹；
    - `bot/bot-app.tsx:183` 每次 `threadTurnKey` 变化就对会话内所有轮次并发发 `fetchEvidence`（N+1 请求）；
    - `bot/sidebar.tsx:338` `<button>` 内嵌套 `<span role="button">` 违法 HTML 嵌套；
    - 多处使用阻塞主线程的 `window.confirm` / `window.prompt` 与导致整页重载的 `window.location.href`；
    - `AppShell` 按 `Esc` 会把在 `/settings` 或 `/team` 的用户直接踢回 `/`；
    - `desktop/main.cjs:429` 硬编码把 `TEMP`/`UV_CACHE_DIR` 重定向到 `E:\env\...`，且启动背景色 `#eef1f2` 与前端 `--bg: #e9edff` 冲突；CLI 的 `~/.blank/config.json` 与前端设置完全不互通。

---

## 二、产品重定位：双引擎桌面智能体工作台

要在国内打开市场，并做到**编程超 WorkBuddy/Qoder、日常超 WorkBuddy/Codex**，不能做"另一个套壳聊天框"，也不能做"只给极客用的命令行界面"。

### 核心定位
> **本地优先、双模一体（编程工作台 + 日常数字员工）、国产生态原生打通的 AI 交付操作台。**

| 维度 | 对标竞品短板 | 空白格重构后的核心优势 |
|------|-------------|----------------------|
| **编程能力**（超 WorkBuddy / Qoder） | 改完不敢信、上下文越聊越贵、容易假完成 | **证据链驱动交付**：LSP 语义定位 + 紧凑 Edit 工具 + 确定性 `done_when` 门禁 + 真实测试闭环 + 完整可回溯 Diff/快照 |
| **日常办公**（超 WorkBuddy / Codex） | Codex 偏纯代码；WorkBuddy 偏聊天/轻任务，缺自动化闭环 | **结构化数字员工**：开箱即用的文档/表格/汇报技能 + 本地定时巡检（Cron/Watch）+ 飞书/钉钉/企微免公网长连接 + IM 内直接审批 |
| **信任与安全** | 权限黑盒、删错文件难追回 | **可见可控的沙箱**：本机文件快照秒级回滚 + 清晰的风险分层审批 + 真实边界告知（不虚假承诺隔离） |

---

## 三、逻辑与架构重设计（Backend & Agent Core）

### 3.1 统一执行底座：消灭三套入口与上帝模块

```text
统一请求入口（对话 / 定时触发 / IM 消息 / 团队协作）
        │
        ▼
  Dispatch & Policy Service（独立服务，不再放在 routes/tasks.py）
        │  生成不可变 ExecutionSnapshot（模型/预算/沙箱策略/场景）
        ▼
  AgentRun Queue（DB 持久化队列，支持 1:N 多次重试记录）
        │
        ├─▶ 桌面端/本地单机：InProcessPool（asyncio 并发池，支持多任务并行）
        └─▶ 独立部署：WorkerPool（worker.py 并发消费，不再串行阻塞）
```

**具体重构清单：**
1. **拆解 `routes/tasks.py`（134KB）**：
   - `routes/tasks.py` 仅保留纯 HTTP 参数校验与响应序列化（目标 < 400 行）；
   - 将 `persist_task`、`launch_task`、`_execute_claimed_run`、`recover_pending_runs`、`recover_orphan_approvals` 下沉至 `services/orchestration/runner.py`；
   - 废弃绕过队列的 `POST /tasks/{id}/run`，统一走 `AgentRun` 入队。
2. **统一 `Team`、`Thread` 与 `Task`**：
   - 废除 `team.py` 独立的 `_BACKGROUND` 与 `TeamOrchestrator` 孤岛，让团队 Bot 发言也作为标准 `AgentRun` 进入统一调度池；
   - 统一调度状态字段：只保留 `task.dispatch_snapshot` 为唯一真源，删除写入 `task.goal_spec["dispatch"]` 的旁路。
3. **修复 SQLite 并发与历史丢失**：
   - `db.py` 初始化连接时强制执行：`PRAGMA journal_mode=WAL;`、`PRAGMA synchronous=NORMAL;`、`PRAGMA foreign_keys=ON;`、`PRAGMA busy_timeout=10000;`；
   - 移除 `AgentRun` 上的 `uq_agent_runs_task_id` 唯一约束，改为 `(task_id, attempt_no)`，保留每次重试/恢复的完整耗时、Worker ID、失败原因与 Token 账单。
4. **跨进程实时事件总线**：
   - 保留内存 `LiveFeed` 用于单进程零延迟推送；当启用独立 `worker.py` 且配置了 `redis_url` 时，通过 Redis Pub/Sub 桥接 `LiveFeed`，消除跨进程退化为轮询的延迟。

### 3.2 Agent 执行内核重构（全面对标 Claude Code，打牢编程底座）

对标 **Claude Code** 的核心竞争力，编程能力强不是靠"提示词写长"，而是靠**低噪声工具面 + 快速代码定位 + 即时反馈闭环 + 持久化子智能体**：

1. **拆分 `graph.py`（9,457 行）为清晰管线**：
   - `agent/graph_builder.py`（图拓扑与路由）
   - `agent/nodes/plan.py`、`act.py`、`validate.py`、`review.py`、`summarize.py`
   - `agent/gates.py`（前置事实检查、提问中断、Pre-Tool Hook、审批门禁）
   - `agent/recovery.py`（预算自愈、截断重试、溢出折叠）
2. **上下文与 Prompt 分层（对齐 Claude Code 缓存友好架构）**：
   - **静态前缀层**（System + 精简工具 Schema，跨轮次保持字节不变以命中 Prompt Cache）→ **仓库级记忆层**（项目 `AGENTS.md`/`CLAUDE.md` + 命中的 `k=1` 历史经验）→ **动态工作态层**（对话流 + 当前文件快照）；
   - 工具说明与回执极致瘦身：`editing.py` 写后只返摘要，不返全文 diff；去掉 `apply_patch` 与 `edit_file` 的双工具重复，减少每轮 1,500+ 字符冗余 Schema。
3. **代码定位与编辑即时反馈（消灭瞎猜与无效轮次）**：
   - **RepoMap / AST 符号索引**：进任务前自动构建轻量目录与顶层符号树，让模型第一轮就知道核心类/函数在哪，不必靠 `search_code` 盲搜 5 轮；
   - **编辑后 LSP 即时诊断**：每次 `edit_file` / `write_file` 成功后，自动调用 `services/lsp/` 拉取当前文件诊断（语法/类型/未定义导入），直接附在工具回执末尾，200ms 内把低级错误扼杀在当轮，不等跑全量测试；
   - **多文件编辑容错与并发**：完善同一批次并行读/并行写不同文件的调度，减少往返轮次。
4. **断点状态与 Subagent 全持久化**：
   - 将 `StallBreaker` 失败签名计数、`ProgressSentinel` 读写追踪、`workflow` 恢复状态纳入 `AgentState`（存入 `AgentCheckpoint`），确保人工审批恢复或进程重启后不丢失熔断与防死循环状态；
   - `subagent` 会话记录落库（新表 `subagent_sessions`），支持像 Claude Code 一样稳定派发只读探索（Explore）与验证子智能体并发执行，且重启后 `send_message` 依然可续聊。
5. **修复核心静默 Bug 与质量硬门禁**：
   - **修复 `scenarios.py` 的 `WRITE_TOOLS`**：补全 `write_file`、`edit_file`、`notebook_edit`，确保只读模式/头脑风暴模式彻底屏蔽写工具 Schema；
   - **修复 `filesystem.py` 隔离读图 Bug**：当 `fs_broker` 激活时，由 broker 提供安全的只读字节流接口给 `_read_image`，恢复隔离环境下的多模态看图能力；
   - **修复 `FallbackAdapter` 预算透传**：把 `max_tokens_override` 从 adapter 实例状态改为每次调用的 `CallOptions` 传参，解决并发竞态与 `FallbackAdapter` 静默失效问题；
   - **`done_when` 升级为 Hard Blocking**：验收条件未满足时强制打回或标记 `FAILED`，彻底消灭"假完成"；`HookSpec` 强制支持 `on_failure: "block"`，安全钩子异常必须 Fail-Closed。
   - **修复 `filesystem.py` 隔离读图 Bug**：当 `fs_broker` 激活时，由 broker 提供安全的只读字节流接口给 `_read_image`，恢复隔离环境下的多模态看图能力；
   - **修复 `FallbackAdapter` 预算透传**：把 `max_tokens_override` 从 adapter 实例状态改为每次调用的 `CallOptions` 传参，解决并发竞态与 `FallbackAdapter` 静默失效问题。
4. **质量硬门禁（超越竞品的杀手锏）**：
   - `done_when` 验收条件检查从"仅记 finding"升级为 **Hard Blocking**：存在未满足条件且无合理豁免说明时，强制打回 `execute_action` 或标记 `FAILED`；
   - `HookSpec` 强制支持 `on_failure: "block"`，安全与审批前置钩子异常时必须 Fail-Closed；
   - **代码智能增强**：LSP 诊断自动挂载到 `edit_file`/`write_file` 之后（编辑完自动返回语法/类型错误给模型，减少专门跑一次全量测试的轮次）。

### 3.3 Windows 沙箱与安全加固

1. **立即修复已知底层 Bug**：
   - 修正 `sandbox/boundary.py:42` 常量为 `0x00020009`；
   - `LocalSandbox.run()` 改用 `subprocess.CREATE_SUSPENDED` 创建进程，先完成 `AssignProcessToJobObject` 再 `ResumeThread`，消除子进程逃逸窗口；
   - `DockerSandbox.run()` 补齐结构化 `enforcement` 账本字段。
2. **文件快照与一键撤回（本机模式核心安全网）**：
   - 在工具层每次修改/删除文件前自动写入轻量化内容快照，前端任务结束时提供"一键撤回本次所有改动"按钮，弥补本机模式下无容器隔离的用户心理顾虑。

---

## 四、功能体系重设计（Features：编程 + 日常双轮驱动）

### 4.1 场景双引擎统一（Scenario Architecture）

将散落的 `DEV`、`DAILY`、`DOCS`、`BRAINSTORM`、`REVERSE` 收敛为清晰的两大主模式 + 按需插件扩展：

| 模式 | 定位 | 默认激活工具集 | 右侧动态面板 | 交付物形态 |
|------|------|---------------|-------------|-----------|
| **💻 编程模式（Dev）** | 读库、改代码、跑测试、修 Bug | 文件、搜索、Edit、Shell、Git、Testing、LSP | 文件树（可展开）、实时 Diff、终端、验收证据 | 代码变更 + 测试报告 + 验收合同 |
| **📋 日常模式（Daily）** | 文档/表格处理、纪要、调研、定时汇报 | 文件读写、Office/PDF 解析与生成、网页抓取、浏览器、定时任务、通知 | 产物预览（Markdown/表格/PPT/PDF）、执行清单、定时状态 | 可下载文档/报表 + 纪要摘要 + 自动化规则 |

### 4.2 日常办公能力全面补强（超 WorkBuddy / Codex）

1. **原生办公文件处理工具链（内置 Python 工具，不依赖模型现写脚本）**：
   - 新增 `doc_read` / `doc_write`（支持 `.docx`、`.xlsx`、`.pptx`、`.pdf` 的结构化读取、表格提取、模板填充与导出），解决国内职场高频的"整理 Excel 报表"、"写周报 Word"、"提取 PDF 合同"需求；
   - 前端提供对应的高保真预览卡片与一键打开本地目录/下载。
2. **自动化任务中心（把 Cron + Watch 变成一等公民）**：
   - 将后端已有的 `cron.py` 和文件系统 `watch` 封装为可视化"定时与监控"卡片：例如"每天 18:00 汇总当天 Git 提交并生成日报发到钉钉/飞书"、"监控某文件夹有新发票 PDF 自动提取汇总到 Excel"；
   - 支持自然语言一句创建定时任务，无需配置复杂参数。
3. **中国本土 IM 全渠道免公网闭环**：
   - **补齐飞书长连接**：在 `channels/feishu.py` 接入飞书官方 WebSocket 长连接模式（对齐钉钉 `stream.py`），让国内用户在无公网 IP 的公司内网/家里电脑上也能 1 分钟配好飞书机器人；
   - **IM 交互式审批卡片**：当 Agent 触发高风险操作或提问时，通过通道向钉钉/飞书发送交互卡片，用户在手机上点"同意/拒绝"直接回调解锁桌面端任务。
4. **场景化模板与技能市场前置**：
   - 把原本藏在设置里的技能市场与废弃的 `/brainstorm`、`/daily` 整合为**快捷技能库**，在主输入框上方提供按行业/场景分类的启动卡片（代码审查、Bug 定位、周报生成、Excel 数据清洗、方案对比）。

### 4.3 自学习、主动推荐与自动生成 Skill 飞轮（对标并超越 WorkBuddy）

#### 源码现状诊断：为什么用户觉得"没有"？
源码审查显示，后端其实**已经实现了一套自学习底座**，但因为设计割裂和前端埋得太深，导致用户完全感知不到：
- `services/agent/experience.py` + `persona.py` 已实现带证据门禁的子任务级经验存储（`pitfall`/`procedure`/`fact`）与使用者偏好追踪；
- `services/tools/skill_proposals.py`（`propose_skills` 工具）与 `services/agent/skillsmith.py` 已支持将流程沉淀为 `status="proposed"` 的技能草案；
- **致命断层在于**：
  1. **前端完全隐身**：技能提案（`proposed`）被藏在 `/skills` 页面（`skills-view.tsx`），自学习记录被藏在 `设置 → LearningPanel`，**主对话流里没有任何可见的提案卡片或采纳按钮**；
  2. **触发范围过窄**：`skillsmith.py` 仅针对 `rigor` 和 `concise` 两条硬编码标准生成技能；`web/src/lib/skill-suggest.ts` 仅做保守的纯关键词匹配（`MIN_HITS=2`），没有基于执行结果的智能下一步推荐；
  3. **缺少自动归纳闭环**：任务踩坑修复后，没有统一的轻量收尾归纳器把"踩坑→解法→验证命令"自动提炼成可读的 `agent_created` Skill 草案推送给用户。

#### 重构后的三层自学习与推荐闭环

```text
1. 任务执行中（踩坑 → 修复 → 验证通过）
        │
        ▼
2. 收尾智能归纳（Learning & Skill Synthesizer）
   ├── 提炼坑点/做法 → 写入 ExperienceStore（带文件/命令证据指针）
   ├── 提炼可复用工作流 → 生成 Skill 草案（status=proposed, agent_created=true）
   └── 生成下一步建议 → 结合当前 Diff/测试结果产出 1~3 条可执行推荐动作
        │
        ▼
3. 对话流内即时呈现（所见即所得，无需跳去设置页）
   ├── [经验命中条]："已应用历史经验：上次在此目录遇到 XX 报错" [👍 有效] [🗑 停用]
   ├── [✨ 新技能提案卡]："我把刚才解决 XX 的步骤整理成了技能，是否以后自动复用？" [一键启用] [微调] [忽略]
   └── [💡 推荐下一步]："检测到新增 2 个接口，建议：1. 补充异常单测  2. 同步更新接口文档"（点击即执行）
```

**具体改造点：**
1. **升级 `skillsmith.py` + `propose_skills`**：
   - 不再局限于 `work-rigorously` / `work-concisely` 两条规则；当一次任务经历了"报错/重试 → 最终验证通过"或执行了 3 步以上可复用办公流程时，自动触发轻量归纳生成规范的 `Skill` 草案（标注 `source: "agent_created"`），同时对已有 Skill 的不足之处自动提出 `kind: "improvement"` 修订建议。
2. **升级推荐引擎（替换纯关键词 `skill-suggest.ts`）**：
   - 将"单轮末尾被动关键词匹配"升级为**上下文感知的推荐意见引擎（Next-Step Recommender）**：在每个 Turn 结束时（`summarize` 节点顺带产出，零额外轮次开销），根据文件变更类型、未覆盖测试、Lint 状态或日常文档产物，给出 1–3 条高价值可点击动作芯片（Action Chips）。
3. **主对话流直出交互卡片（`bot/` 与统一 `<TimelineFeed />`）**：
   - 任务结果卡片下方直接挂接**「✨ 技能提案」**与**「💡 推荐下一步」**卡片，用户在当前页面 1 次点击即可完成"采用技能"或"继续执行推荐动作"，不再需要去设置页翻找；
   - 召回 `ExperienceStore` 经验时在时间线顶部轻量化展示出处，支持一键点击调用 `POST /learning/experience/{fp}/retire` 淘汰过时经验。
4. **「AI 成长与经验库」可视化看板**：
   - 将原深埋在设置页的 `LearningPanel` 升级并入一级入口「技能与模板（`/skills`）」，直观展示：**Agent 自生成的技能（`agent_created`）**、**仓库踩坑经验库**、**命中次数与节省轮次**，让用户清晰看到"越用越懂我"。

---

## 五、页面与信息架构重设计（UI/UX & Desktop/CLI）

### 5.1 信息架构收敛：从 13 个碎片页面到 5 个核心入口

**当前问题**：13 个路由中 4 个孤儿页（`/daily`、`/brainstorm`、`/features`、`/reverse-lab`），3 套互不相同的左栏，且首页 `/` 与其余页面的模型选择、会话数据不同步。

**重构后导航结构（全局统一单一 `AppShell`）：**

```text
全局唯一左侧栏（240px 展开 / 56px 收起，全站样式与数据源唯一）
├── 顶部：空白格 Logo + [ + 新建任务 ] + 全局搜索 (Ctrl+K)
├── 工作区切换器（置顶/最近项目，一键切换本地目录）
├── 核心导航（5 个一级入口）：
│   ├── 1. 工作台（/）          ← 合并原 / 与 /tasks/detail，对话 + 执行 + 产物一体化
│   ├── 2. 调度与监控（/monitor）← 合并原 /tasks（工作台列表）+ /monitor + 定时/Watch 管理
│   ├── 3. 技能与模板（/skills） ← 提升为一级入口，合并原 /skills + /brainstorm 决策模板
│   ├── 4. 团队与消息（/team）   ← 合并多 Bot 协作 + 飞书/钉钉/企微 IM 通道管理
│   └── 5. 项目与设置（/settings）← 合并原 /projects + /settings + /evidence 审计归档
└── 底部：运行状态胶囊（后端健康/沙箱等级）+ 当前模型快切
```

> **处置废弃路由**：删除 `/features`（本地假开关）和 `/daily`（习惯打卡）；`/brainstorm` 并入技能模板；`/reverse-lab` 降级为可插拔的扩展技能/独立面板，不再作为空壳路由占坑。

### 5.2 主工作台（`/`）重设计：低噪声、高密度、双模自适应

```text
┌──────────────┬───────────────────────────────────────┬──────────────────────────┐
│ 统一左栏      │ 中央：指令与执行主区                    │ 右侧：动态产物与上下文面板  │
│              │                                       │                          │
│ [工作区:kbgai]│ 顶部栏：[💻 编程] [📋 日常] | 模型选择  │ Tab 自动随模式切换：       │
│              │        沙箱状态胶囊 | 耗时/Token        │                          │
│ 今天         │                                       │ [编程模式]               │
│  ● 修复登录  │ ┌───────────────────────────────────┐ │  • 文件树（修复可点击展开）│
│  ● 写单元测试 │ │ 执行时间线（低噪声折叠卡片）         │ │  • 实时 Diff（含一键回滚） │
│              │ │  ✓ 读取 3 个文件（0.4s）           │ │  • 终端输出              │
│ 最近 7 天    │ │  ✓ 修改 auth.py (+18 / -4)         │ │  • 交付证据/验收合同     │
│  ✓ 周报汇总  │ │  ⚠ 请求审批：执行 pytest -q        │ │                          │
│              │ │     [ 批准 ]  [ 拒绝 ]  [ 总是允许 ]│ │ [日常模式]               │
│              │ │  ✨ 新技能提案：auth-test-guard     │ │  • 产物预览（文档/表格）  │
│              │ │     [ 一键启用 ] [ 微调 ] [ 忽略 ]  │ │  • 导出/打开所在文件夹    │
│ 定时运行中(2) │ │  💡 推荐下一步：[补充单测] [生成提交]│ │  • 关联定时规则          │
│              │ └───────────────────────────────────┘ │                          │
│              │                                       │  • 导出/打开所在文件夹    │
│              │ 底部 Composer：                        │  • 关联定时规则          │
│              │  @引用文件 / 快捷技能 / 附件 / 发送     │                          │
└──────────────┴───────────────────────────────────────┴──────────────────────────┘
```

**关键交互升级：**
1. **统一组件库**：彻底消除 `bot/`、`tasks/`、`team/` 三套重复代码，抽离公共 `<TimelineFeed />`、`<ApprovalCard />`、`<QuestionCard />`、`<WorkspaceTree />`、`<DiffViewer />`；
2. **统一数据状态**：使用单一全局 Store（如 Zustand / TanStack Query）管理 `currentProviderId`、`currentProjectId`、`threads`，彻底消灭首页 `BotApp` 与 `AppShell` 各自读写不同 `localStorage` 的分裂；
3. **清零前端交互硬伤**：
   - 拆掉 `tasks/api.ts`（95KB）按领域分文件（`api/client.ts`、`api/tasks.ts`、`api/threads.ts`、`api/sse.ts`）；
   - 拆掉 `bot-app.tsx`（72KB）和 `team-view.tsx`（76KB）为独立 hooks 与子组件；
   - 修复 `panel.tsx` 文件树目录 `disabled` 问题，支持异步懒加载子目录；
   - 修复 `composer.tsx` 的 `bg-$1` 样式；修复 `sidebar.tsx` 按钮嵌套；
   - 用统一的非阻塞 `<ConfirmDialog />` 替换全部 `window.confirm` / `window.prompt`；
   - 用 Next.js `router.push()` 替换全部 `window.location.href`；
   - 取消 `AppShell` 全局按 `Esc` 强制跳回 `/` 的反直觉行为；
   - 去掉 `BotApp` 的 N+1 `fetchEvidence` 轮询，改为在线程汇总接口直接返回变更文件数与最新证据摘要。

### 5.3 Desktop 与 CLI 一体化打磨

1. **Desktop（`desktop/main.cjs`）**：
   - 删除 `bindRuntimeTempToEDrive` 中硬编码的 `E:\env\blank-tmp` 逻辑，改用系统标准 `app.getPath("temp")` 或用户可配置路径；
   - 统一启动背景色与前端 `--bg`（`#e9edff`），消除启动白闪/色差；
   - 点击系统通知不再 `win.loadURL` 整页重载，改用 `webContents.send("blank:navigate-thread", threadId)` 实现毫秒级无刷新切换。
2. **CLI（`packages/cli/src/index.mjs`）**：
   - 增加 `blank run "<任务>" --mode daily|dev --watch` 无头执行与流式输出能力，对齐 Codex CLI 体验；
   - 让 `~/.blank/config.json` 与桌面端/Web 端设置接口打通，消除配置孤岛。

---

## 六、分阶段实施路线图（建议开发顺序）

| 阶段 | 核心目标 | 包含事项 | 预期效果 |
|------|---------|---------|---------|
| **Phase 1：止血与统一底座** | 修真实 Bug、稳并发、降 Token | SQLite WAL+外键、`graph.py` 预算/隔离读图/`WRITE_TOOLS` Bug 修复、`editing.py` 回执瘦身、`boundary.py` 常量与 Job 竞态修复、测试归档 | 稳定绿灯，Token 成本下降 30%+，核心链路零静默失败 |
| **Phase 2：内核解耦与 Claude 级编程底座** | 编程质量对标 Claude Code | 拆分 `tasks.py`/`graph.py`、`AgentRun` 1:N 历史、Prompt 分层与 RepoMap/AST 索引、LSP 编辑后即时诊断、断点持久化与 Subagent 落库、`done_when` blocking + Hook `on_failure: block`、本机一键回滚 | 编程一次通过率大幅提升，彻底消除"假完成" |
| **Phase 3：前端收口与自学习闭环** | 统一体验 + 超越 WorkBuddy 的自学习 | 统一左栏与全局 Store、5 大一级页面重构、拆解大组件、对话流直出「✨ 技能提案」「💡 推荐下一步」「经验命中」卡片、升级通用 `skillsmith` 自动归纳与「AI 成长看板」 | 界面统一流畅，越用越聪明、用户直接可见可点 |
| **Phase 4：中国市场与日常突围** | 日常办公超 WorkBuddy / Codex | 内置 Office/PDF 工具链与预览、可视化 Cron/Watch 定时任务、飞书长连接 + IM 交互审批卡片、场景模板前置、Desktop/CLI 一体化打磨 | 覆盖开发+职场高频场景，支持国内 IM 零公网开箱即用 |
