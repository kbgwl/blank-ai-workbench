# Blank AI Core Runtime Roadmap

目标：把 Blank AI 从“可调用工具的任务页面”补齐为可恢复、可审计、可扩展的 Agent Harness，能力目标对齐 Codex / Claude Code 这一类工程执行产品。

## 当前基线

> **这一节是 2026-10-16 的快照**，只作历史起点用。下面「尚未达标」的多数条目后来已补齐，
> 现状以「2026-10-16 实现审计」及其后各期交付记录为准；与现状不符的条目已就地标注。

已经存在：

- LangGraph 状态图：计划、执行、观察、验证、修复、总结
- SQLAlchemy checkpoint，可恢复中断的图状态
- ToolGateway：工具注册、参数校验、风险判断、脱敏和调用记录
- DockerSandbox 与本地开发回退模式
- 高风险工具审批、取消、任务事件和交付证据包
- 统一 ModelAdapter，支持 OpenAI 兼容、Anthropic、Ollama 等供应商

尚未达标：

- 任务执行仍由 HTTP 请求同步等待，缺少持久 Worker 队列
- 事件接口是查询式，缺少可恢复的实时事件流协议
- 本地执行回退不是真正隔离，生产环境必须显式授权
- 审批按工具名授权，尚未绑定具体参数、路径和过期时间
- 缺少上下文压缩、工具结果分层和任务级上下文预算
- 多模型只有供应商选择，尚未有路由、fallback、审查和并行协作
- Usage 记录存在，但还没有完整 trace/span、指标和评测闭环
- 日常习惯尚未形成“候选记忆 -> 用户确认 -> Skill 版本”的生命周期

## 2026-10-16 实现审计

以下能力已落地并有代码或测试证据：

- Durable Run：`/tasks/{id}/start` 返回 `run_id`，Run 请求参数持久化，数据库租约、续租、后台恢复扫描和运行状态查询已接入。
- 事件流：SSE 支持 `after_seq` 回放；事件带 `run_id`、`step_id`、`parent_event_id` 和 `trace_id`。
- Safe Compute：Docker 不可用时默认失败；本地执行必须显式开启；审批绑定参数哈希并带过期时间；运行前快照可在证明面板恢复。
- Context / Skill：工具结果有独立上限；历史消息自动压缩；用户确认后的 active Skill 才会进入 Agent 上下文，并支持禁用、版本和回滚。
- Model Runtime：planner/coder/reviewer 路由、工具能力降级、可重试 fallback、审查修复回路、token/延迟/成本/成功率聚合已接入。

仍未完成的生产门槛：

- 当前 Worker 仍运行在 API 进程内，后台数据库扫描解决的是单机恢复，不是独立多副本 Worker 调度。
- ~~工具大输出目前只做截断和证据摘要，还没有独立 artifact 存储与按需读取 API。~~
  **此条已过期**：`services/api/src/blank_api/services/agent/artifacts.py` 有 `ArtifactStore`，
  读取接口是 `GET /tasks/{task_id}/artifacts/{artifact_id}`（`api/routes/tasks.py`），
  大输出按 `ToolCall.artifact_path` 落盘并按需取回。
- 快照恢复、审批过期、跨进程恢复和模型 fallback 仍需要真实数据库、Docker 和多进程端到端评测作为发布门禁。
- 固定评测集尚未形成自动化 CI 门禁。

## 阶段 1：Durable Run Kernel

### 交付

- 引入 `Run` 资源，区分 task、thread、run、step
- `POST /runs` 只创建任务并返回 `run_id`
- Worker 从数据库领取 pending run，进程重启后可继续领取
- 增加 run 状态：queued、running、waiting_approval、paused、succeeded、failed、cancelled
- 所有事件增加 `run_id`、`step_id`、`parent_event_id`
- 提供从 `after_seq` 恢复的 SSE/WebSocket 事件流
- 支持 retry、cancel、resume，且不会重复执行已完成的工具步骤

### 验收

- 请求返回后关闭浏览器，任务仍能完成
- API 进程重启后，未完成 run 能被重新领取
- 同一 run 的工具调用不会因重试重复执行
- 客户端从任意 `after_seq` 连接都能补齐事件
- 审批等待期间不会占用 HTTP 请求

## 阶段 2：Safe Compute

### 交付

- Docker/Podman 沙箱成为生产默认；本地执行必须显式开启
- 文件、网络、环境变量、CPU、内存、进程数和超时全部有策略
- 审批绑定 `approval_id + arguments_hash + path_scope + expires_at`
- 高风险命令按具体动作审批，而不是按工具名永久放行
- 每次文件变更前自动 snapshot，支持一键回滚

### 验收

- 沙箱内无法读取工作区外文件和未授权密钥
- 默认网络关闭，开启网络必须有任务级记录
- 批准 `pnpm test` 不会同时批准其他 Shell 命令
- 任务失败后能恢复到执行前 snapshot
- Docker 不可用时生产配置直接失败，不静默降级到宿主机

## 阶段 3：Context and Skill Kernel

### 交付

- 当前任务上下文、历史对话、工具结果和用户记忆分层
- 自动压缩上下文，保留目标、约束、已完成动作、失败原因和未决事项
- 工具大输出写入 artifact，只把摘要放进模型上下文
- 日常记忆采用候选、确认、撤销、版本化 Skill 流程
- Skill 只能由用户审核后启用，并支持回滚和效果记录

### 验收

- 长任务超过上下文预算后仍可继续执行
- 压缩前后目标、约束和未完成步骤一致
- 用户未确认的日常内容不会进入长期 Skill
- Skill 可禁用、回滚和查看来源

## 阶段 4：Model Runtime and Observability

### 交付

- 按能力路由 planner、coder、reviewer、tester
- 模型超时、限流和 5xx 支持有限重试与 fallback
- 支持同一任务的多模型审查和结果比较
- 增加 trace/span、token、延迟、成本、重试、审批等待等指标
- 增加固定评测集，覆盖文件修改、测试修复、审批拒绝、恢复执行和提示注入

### 验收

- 主模型不可用时可按策略切换备用模型
- 每个模型调用、工具调用和审批都有可关联 trace
- 任务成功率、平均修复轮次、成本和失败原因可查询
- 评测集结果作为发布门禁，而不是只看单元测试

## 实施顺序

必须按以下顺序推进：

```text
Durable Run Kernel
        ↓
Safe Compute
        ↓
Context / Skill Kernel
        ↓
Model Runtime / Observability
```

页面只消费这些内核资源，不在前端自行模拟任务状态、模型状态或记忆状态。

## 2026-10-17 REA 对照后新增的差距

对**本机安装的正式版**做了静态逆向拆解（Codex 0.154.0 的 285MB Rust 二进制、Claude Code
2.1.271 的 229MB Bun 二进制 + `sdk-tools.d.ts`），证据与结论见
[`REA-CODEX-CLAUDE.md`](REA-CODEX-CLAUDE.md)。两家撞在一起的能力就是"内核该有的样子"。

### 已落地（本轮）

- ✅ **`update_plan` 工具** —— 计划从一次性输出变成**可更新对象**（对标 Codex `update_plan` /
  Claude `EnterPlanMode`+`ExitPlanMode`）：步骤可勾选、可按用户插话追加；新增 `plan.updated` 事件。
- ✅ **hooks 生命周期** —— 落地 `session_start` / `session_end` / `user_prompt_submit` /
  `pre_tool_use` / `post_tool_use` / `post_compact`。配置走 `BLANK_HOOKS`（JSON），
  **默认关闭**；`pre_tool_use` 可拦截工具调用，其余为通知；**钩子失败一律 fail-open**，
  且每次执行都写 `hook.completed` 审计事件。
- ✅ **审批粒度：前缀放行 + 会话内批准 + 四档权限模式**（`services/permissions.py`）
  - **命令前缀签名**：`git status -s` → `git status`，`npm run build` → `npm run`。
    Windows 路径按 basename 归一（`C:\Python312\python.exe` → `python`）。
  - **逐段覆盖**：`npm test && rm -rf src` 不会被 `npm test` 的规则放行——整条命令的每一段
    都得命中，否则照样打断。
  - **四档模式**：`default` / `accept_edits` / `plan` / `bypass`。`plan` 只允许只读取证
    （网关层硬拒绝，不是"询问"）；`accept_edits` 只吞普通文件写入，敏感路径补丁仍是高风险。
  - **作用域批准**：审批返回 `once` / `prefix` / `tool` 三档，写回 `PermissionPolicy`；
    `approval.granted` 事件带上作用域与参数，**审批恢复时从事件日志回放**，
    前端不重发模式也不会丢授权。
  - **不可逆护栏**：`mkfs` / `format` / `rm -rf /` / `DROP DATABASE` / `git push --force`
    即使 `bypass` 也转人工确认；裸可执行名（`rm`/`bash`/`curl`）**不提供"总是允许"这一档**，
    避免"记住 rm"等于全放行。
  - 判定唯一入口是 `ToolGateway.decide()`，图中断与网关执行不可能给出两个答案。
- ✅ **两层上下文压缩**（`services/agent/context.py`）
  - 第一层 `microcompact`：确定性裁剪——超长工具结果、重复输出、内联 base64 一律先削掉，
    **不花模型钱**，每次都安全可调用。
  - 第二层 `ContextCompactor`：窗口滑出时把历史折进五要素摘要（目标 / 约束 / 已完成 /
    失败 / 待办），增量更新，不重复摘要。
  - `CompactionReport` 记账 `active_context_tokens_before/after` 与节省量，
    事件 `context.microcompacted` / `context.compacted` / `context.measured` 三条分开，
    压缩边界可见可解释；摘要模型不可用时退回确定性截断，绝不因压缩拖垮任务。
- ✅ **Agent 主动提问**（`tools/ask.py`，工具名 `request_user_input`）
  - 与审批共用同一条中断通道：`kind=question` 的中断 → `question.required` 事件 →
    `POST /tasks/{id}/answer` 恢复；支持多问题、选项、自由文本，答案回填为工具结果。
  - 前端在同一对话流里渲染**提问卡**（选项 chip + 自由输入，答完才能提交），
    `question.asked` 与 `question.required` 两条记录合并成一张卡，不会出现双重提问。
- ✅ **子代理 + worktree 隔离**（`services/subagent.py` / `services/worktree.py` / `tools/delegate.py`）
  - `spawn_agent` 起一个受限步数、受限工具集的嵌套代理，事件带 `parent_thread_id` /
    `depth` / `agent_path`，能还原"谁在哪个分支上干活"。
  - 隔离模式在 git worktree 副本里跑，回收时产出 diff；子代理**没有第二套审批通道**，
    写操作照样过 `PermissionPolicy`。
- ✅ **联网工具**（`tools/web.py`，`web_search` / `web_fetch`）
  - SSRF 防护：私网 / 回环 / 链路本地地址一律阻断，逐跳校验重定向，
    域名可配白名单（白名单存在时默认拒绝），响应体与超时都有上限。
- ✅ **guardian 自动审查**（`services/guardian.py`）
  - 独立审查线程，按可配模型 / 超时给 `allow` / `deny` / `abstain`；`BLANK_GUARDIAN` 默认关闭。
  - **只能收紧不能放行**：`abstain` 与解析失败都退回原审批路径；`deny` 直接拒。
    事件 `guardian.reviewed` 记录模型、延迟、判据。
- ✅ **指令分层**（`services/instructions.py`）
  - 层级：内建系统提示 → 用户级 `~/.blank/AGENTS.md` → 项目 `AGENTS.md` / `.blank/AGENTS.md`
    → 本次任务附加指令，按"越靠近任务优先级越高"拼装，并显式写明**任何层都不得放宽
    安全与审批规则**。
  - 单文件 8k / 总预算 12k 双重上限，超预算时**先削最弱层**；
    `instructions.loaded` 事件只记来源与长度，不把内容抄进日志。
- ✅ **跨窗口交接笔记**（`services/notes.py` + `write_notes` / `read_notes`）
  - 笔记按任务落在应用数据目录（不进用户仓库、不进 diff），append 带时间戳与步号；
  - **压缩后必带、换窗口后首次调用必带**（`_insert_handoff` 插在系统提示之后），
    模型忘了读也还在；容量上限保留最新内容。
- ✅ **记忆固化**（`services/memory.py` + `memory_search`）
  - 任务成功后把结论 / 笔记 / 验收 / 验证命令蒸馏进按工作区划分的 markdown，
    条目级去重、总长有上限、超限丢弃最旧段落；`memory.consolidated` 记 added/skipped/dropped。
  - 全程确定性，不额外调模型；写入失败只记 `memory.consolidation_failed`，不改任务结论。
- ✅ **线程分叉**（`services/fork.py` + `POST /tasks/{id}/fork`）
  - `trunk_reuse` 复制最新检查点与其写入（可从断点继续），`trunk_new` 只留血缘；
    复制时**刻意切断 `parent_checkpoint_id`**，源线程被清理后分叉仍可读。
  - 线程号分配同时统计检查点与 `thread.forked` 事件，空树干不会撞号。
- ✅ **shell 快照 + 环境策略**（`services/shell_env.py`）
  - 每个线程记录 cwd / PATH 条目 / 解释器 / venv，事件 `shell.snapshot` 只记形状不记值；
    项目 venv 自动前置进 PATH，多步命令解析到同一个解释器。
  - 三档策略 `inherit` / `minimal` / `strict`，并额外按**变量名与值形态**丢弃凭证；
    **策略只能收窄不能放宽**——`strict` 下起的任务不会因为某个工具要 `inherit` 就拿到更多变量。
- ✅ **中途插话度量**（`POST /tasks/{id}/steer` + `drain_steer`）
  - 插话入事件日志（跨进程、跨审批恢复都不丢），在下一次模型调用前注入并写 `steer.applied`
    （含步号、字数、排队到生效的延迟）；任务收尾的 `steer.count` / `steer.steps` 即 `steer_count`。

## 0.4.0：四个收尾口子（2026-10-17）

上一版把 P1/P2 的主体做完了，剩下四条"用得着但不影响跑通"的缺口。这一版全部补齐，
每一条都按"改了什么行为"而不是"加了什么文件"记录。

### 1. 工具按需加载（`tool_search`）

对标 Codex 的 `tool_search` 与 Claude 的 deferred tool loading。默认只下发 11 个核心工具
（看、改、跑、问、记计划、写笔记），把 git / 联网 / 记忆检索 / 子代理收进待加载集合。

- `services/tools/registry.py`：`ToolRegistry` 是**会话级**视图，`CORE_TOOLS` 与
  `DEFERRED_TOOLS` 两张开在一处，新增工具不登记就默认常驻（不会变成隐形能力）。
- `services/tools/search.py`：确定性打分（工具名 / 别名 / 描述），**空查询**列出还能加载的，
  **无命中**就明说没命中，绝不甩一份目录让模型自己猜；命中即激活，下一轮可直呼其名。
- 三道自加约束：**deferral 只是展示策略**，权限判定与加载与否无关（有专门测试）；
  检索工具自己不参与检索；同一文件里 `MIN_SCORE` 挡住"只是描述里蹭到同一个词"的噪音。
- 设置页可关掉按需加载（`tools.search=false`），关掉后所有工具每轮直接下发、`tool_search` 一并移除。

### 2. 多副本并行编排（`spawn_agents`）

对标两家都能"并行跑多个 worktree 代理"，我们之前只有串行单发。

- `SubAgentRunner.fan_out()`：`asyncio.Semaphore` 控并发（默认 3，上限 4 个代理），
  每个子代理独立副本、独立上下文、独立 `agent_path`（`root/par-<group>-<n>`），
  事件带 `parallel_group` 与 `index`，前端能把同一批的结论串起来。
- **合并刻意保守**（`worktree.apply_changes`）：同一文件被两个子代理改过 → 冲突，谁的都不写；
  主工作区该文件已有未提交改动 → 跳过；删除类改动 → 一律交回人工。
  并行是为了分头探索，落盘的决定权留给主代理。
- `BLANK_WORKTREE_ROOT` 可指定副本落哪个卷（默认系统临时目录），跑完统一清理。

### 3. 压缩按 token 水位触发

之前按字符预算判定，窗口大小完全没参与计算。现在：

- `ContextCompactor` 增加 `context_window` / `trigger_ratio` / `reserve_tokens`；
  水位 = `min(窗口×比例, 窗口−预留)`，与 Codex 的 `auto_compact_fallback_buffer_tokens` 同义。
- **用真实用量校准估算**：每次模型调用后把 `prompt_tokens` 与估算值做滑动平均
  （`observe()`），区间夹在 0.2–5.0 之外一律丢弃，免得一次缓存命中把水位永久带偏。
- 窗口未知（`context_window=0`）时**完整退回字符预算**，老部署行为一字不变（有回归测试）。
- 事件 `context.measured` 现在带 `context_window` / `trigger_at` / `water_ratio` /
  `calibration` / `triggered_by`，前端能直接说"水位 78%"。

### 4. guardian 设置页

后端早就能审查，但只能靠环境变量配。这一版把它变成一等设置：

- `services/settings.py`：`data/settings.json` 落盘，四段（guardian / compaction /
  permissions / tools），**归一化即夹取**——越界值返回边界值，界面显示的永远是生效值。
- `GET|PUT /settings/agent`、`POST /settings/agent/reset`；设置文件缺失或损坏回落默认。
- `Guardian.from_settings()` 的优先级：**有设置文件时以设置页为准**，
  没有设置文件才看 `BLANK_GUARDIAN`（CI / 无头环境行为不变）。
- **默认仍是 off**：开审查意味着每个高风险动作多一次模型调用，这件事必须由用户显式选择，
  不能在升级后悄悄加延迟与"被拒绝"的意外。设置页把 veto 标为推荐。
- 顺带把「新任务默认权限档位」也搬进设置，避免前后端各有一套默认值。

### 验证

- 后端全量：**263 passed / 14 skipped**（上一版 198，新增 65 例，零回归）。
- 新增测试：`test_tool_search_v13.py`（19）、`test_context_watermark_v13.py`（14）、
  `test_worktree_parallel_v13.py`（14）、`test_settings_v13.py`（18）、
  `test_agent_e2e_v13.py`（2，端到端验证"模型自己加载 git 然后真的用上"）。
- 前端：`tsc --noEmit` 干净；设置页新增「执行与智能体」四张卡。

### 剩余（非内核缺口）

代码签名证书采购、托盘 / 全局快捷键 / 单实例 / 自动更新——都属于壳层工程，不影响内核能力。

### 另外两条可直接沿用的工程选择

- 两家都已从"npm 里的 JS"改成**编译型单文件二进制**，并且**都自带 ripgrep**（Codex 的
  `codex-path/rg.exe`）。我们的桌面壳已按同样思路自带 Python 运行时，方向一致。
- Codex 另有 `codex-code-mode-host.exe`：把代码执行放进独立宿主进程 —— 与我们沙箱的思路同源。



---

## 0.4.1：修两个「功能明明做了却看不见」的问题（2026-10-18）

0.4.0 交付后用户打开设置页，结论是「页面没有改」。排查下来是两个叠在一起的问题，
两种都让新能力等于不存在，而打包日志一切正常。

### 1. 新设置被排到折叠线以下

`ProvidersView` 那三张大卡（模型供应商 / 供应商配置 / 流式输出）把「执行与智能体」四张卡
推到了首屏之外。用户打开设置页，看到的东西和上一版一模一样。

- 顺序改为：**执行与智能体 → 模型供应商 → 沙箱与角色模型**，并各加分组标题与说明。
- 新分组打「0.4.0 新增」标签，一眼能看出变化在哪。
- 原则：**设置页按「用户打开就要看的东西」排，不按模块归属排**。

### 2. 包内 Python 运行时带着一份旧的 `blank_api` 副本

这是更严重的一个，也是「页面没改」的真正主角：

- 桌面版运行时 `site-packages/blank_api` 里有一份旧代码（没有 settings 路由）。
- uvicorn 从 `backend/api` 目录启动，而源码在 `backend/api/src/blank_api`，
  所以 `import blank_api` 落到 site-packages 的旧副本上。
- 结果：**前端是新的、后端是旧的**。设置页渲染出新卡片，但 `GET /settings/agent` 返回 404，
  界面上是一片 "Not Found"。
- 之前的验证方式（`sys.path.insert(0, "api/src")`）恰好强行指向源码，把这个问题完全盖住，
  所以 0.4.0 的包其实已经坏了一版却通过了自检。

修法：删掉 `site-packages` 里的副本，改用 `blank_api.pth`（内容 `../../../api/src`）
指向源码，**单一真源**，以后更新后端不必同步两份。

### 3. 新增打包后自检 `desktop/verify-package.cjs`（`npm run verify`）

针对上面第 2 条加的护栏，核心是**复刻应用真实的启动方式，不许改 sys.path**：

- 包内 Python / 后端源码 / 前端静态站是否存在；
- `site-packages` 里不许有 `blank_api` 副本，`.pth` 内容正确；
- 用包内解释器在 `cwd=api` 下真实 `import blank_api`，断言落点在 `api/src`；
- `settings` 路由已注册、前端 `settings.html` 含新分组标记。

任一项不过就 `exit 1`，打包后跑一次即可。这条能防住「前端新、后端旧」这一类静默错配。

### 4. 主进程日志落文件（`BLANK_LOG_FILE`）

Electron 在 Windows 是 GUI 子系统程序，由父进程拉起时没有控制台，`console.log` 直接消失，
启动失败等于「双击没反应」且一句日志都拿不到。加 `BLANK_LOG_FILE` 把主进程输出写进文件。
另有 `BLANK_START_PATH` 可直接把自检落在指定页（如 `settings.html`），不必先点导航。

### 验证

- `npm run verify` 全绿：import 落点 `backend/api/src/blank_api`，settings 路由已注册。
- 包内实测 `GET /api/v1/settings/agent → 200`，设置页四张卡完整渲染，无 Not Found。
- 前端 `tsc --noEmit` 干净，`next build` 12 页。

## 0.4.2 · 把设计稿真正落到界面上（2026-10-18）

### 问题

`docs/app-mockup.html` 一直是**设计稿**，从来没有进过产品代码：界面还是深色的旧首页
（「今天要做什么？」+ 单栏对话），所以每次打开都觉得「页面没改」。

### 改法

- **主题**：`globals.css` 的令牌整组换成设计稿的浅色（白底 / `#f8f9fa` 侧栏 / teal `#0e7490`），
  组件只认令牌，换肤不动业务代码。
- **布局**：新增 `components/workbench/workbench.tsx` —— 左栏（新任务 / 搜索 / 工作区 / 最近任务 /
  底部导航）、中栏（标题+状态、执行流、输入框、水位条）、右栏（变更 / 证据 / 活动）。
  首页与 `/tasks` 都走这一个工作台；`/tasks/detail?id=` 仍保留为深链。
- **顶栏**：首页自带品牌与导航，不再重复一条全局顶栏（`AppShell` 按路径判断）。
- **「继续说」是真的**：输入框在有进行中任务时走 `POST /tasks/{id}/steer`（排队、下一步生效），
  没有任务时创建并运行新任务。
- **对比度**：`bg-black/30` 这类深色硬编码全部换掉，浅色下不再有看不清的代码块。

### 顺手修掉的一个旧 bug

`normalizeChanges` 取文件列表时先命中 `changes.files`（只有路径字符串），把后面的 `items`
挤掉了，于是右栏一直显示「未知文件」。改成优先 `items`，单文件时把外层整段 diff 挂上去。

### 验证

- `next build` 12 页通过，`tsc --noEmit` 干净。
- 包内实测：三栏工作台、可展开工具卡、Diff、审批卡、设置页四张卡都在浅色下正常。

## 0.4.4 · 「输入以后没有任何返回」的五层原因（2026-10-18）

用户反馈：会话删不掉；模型健康检查正常，但输入一句话之后界面没有任何返回。
「没有返回」不是单一 bug，是五处问题叠在一起，任何一处都足以让界面看起来死掉。
按发现顺序记下来，因为它们分别属于不同的层，以后不会同时再犯。

### 1. 静默失败：异常只改了状态，没写事件

`_execute_claimed_run()` 的 `except Exception:` 只把 `agent_runs.status` 置 `failed`，
既不写事件也不动 `tasks.status`。于是任务在库里是 failed，界面上永远停在「排队中」。
改为 `_record_run_failure()`：写 `task.failed`（含 `error / reason / error_type / traceback`），
同时把任务状态一起落下来。同一个 `task_id + run_id` 已有 `task.failed` 就不重复写
（runtime 正常收尾时自己会写一条）。

### 2. 快照把任务判死

第一层修好后终于能看见原因了：`task.failed` 的 payload 是 `shutil.Error`。
工作区指到本仓库，`desktop/dist-*` 里躺着整份 Python 运行时，几万个文件撞上 Windows
`MAX_PATH`，`SnapshotStore.capture()` 的 `copytree` 抛错，任务就被判死。
整树复制本来就不该是启动任务的前置条件：加 `MAX_SNAPSHOT_FILES/MAX_SNAPSHOT_BYTES`
阈值与 `dist-*/build-*` 前缀忽略，超限或复制失败返回 `copied:false + skipped_reason`，
`runtime.run()` 也整段 try/except。单文件的回滚另由写入前备份保证。

顺带修掉：跳过快照复制时 `<id>/` 目录压根没建，写 `meta.json` 直接 `ENOENT`，
于是每次任务都多一条假的 `snapshot.failed`。

### 3. 真正的崩溃：`steps` 被覆盖成列表

`TypeError: unsupported operand type(s) for -: 'int' and 'list'`。
`execute_action_node` 里 `steps` 是步数计数器（int），而 `update_plan` 工具的结果分支
又写了一次 `steps = payload["data"]["steps"]`（列表）—— 变量遮蔽。
于是模型只要调用一次 `update_plan`，后面算预算 `session.max_steps - steps` 立刻崩，
整个 run 死掉。改名为 `plan_steps`。

### 4. 纯问答也被拉去跑测试

校验节点无条件执行 `run_tests`，而桌面壳默认 `BLANK_SANDBOX_ENABLED=false`，
`run_tests` 返回 `SandboxUnavailable`，任务被判 `failed`，理由写着「测试未能执行」——
用户只是打了句「你好」。改成：本次没有改动任何文件就不校验（`status: "skipped"`），
`resolve_status()` 把 `skipped` 与 `ok` 同等对待，审查节点也一并跳过
（只读任务没有交付物可审，硬审只会拿到一段散文，然后白修三轮）。

### 5. 答案根本没送到界面上

两层：
- 后端 `summarize_result_node` 在没有验收条件时，`summary` 固定成「任务执行完成」，
  模型真正的回答被丢掉；`_extract_json` 又是 `find("{")..rfind("}")`，
  模型把 JSON 混在解释文字里就解析失败。改成先取最后一条 assistant 正文兜底，
  JSON 提取改成逐个平衡括号尝试。
- 前端 `workbench.tsx` 引用了 `task.resultSummary`，却从来没有渲染它 ——
  所以哪怕后端算出了答复，中间那栏也只剩一串事件。

### 另外两处顺手改的

- **系统提示不分流**：不管输入是什么都按「在仓库里干活」处理。补一条规则：
  闲聊/概念提问直接回答、不给计划、不调用工具。
- **`_parse_plan` 太宽**：只要一行超过 3 个字就算一步，于是「你好！我是…可以帮你做这些事：」
  也被判成执行计划。改成只认编号或项目符号。
- **主密钥跟着包走**：数据库在用户数据目录、`.blank_secret.key` 却在包内 `data/`，
  换包就换钥匙，已存的供应商 Key 全部 `secret_undecryptable`。
  新增 `BLANK_SECRET_KEY_FILE`，桌面壳指到 `%APPDATA%/blank-ai-workbench-desktop/`，
  并做一次从老位置的搬迁（老用户重装后不用重录 Key）。

### 验证

- 纯问答「你好」：`task.created → snapshot.created → task.started → plan.created → task.completed`，
  0 步工具调用，`succeeded`，`result_summary` 是模型真实回答。
- 带验收条件的任务：`task.failed` 只有一条，失败原因可读。
- `DELETE /tasks/{id}` 返回 204，列表同步清空。
- 后端 `pytest` 全绿（见提交记录），前端 `tsc --noEmit` 干净。

## 0.5.0 · 把「权限」交给客户（2026-10-18）

原话：「我们要客户必须要可控制权限的」。

上一版沙箱只在有 Docker 时才存在，客户端装机不带容器运行时，于是"安全"等于没有。
这一版把安全拆成两层，顺序不能反：

| 层 | 模块 | 回答的问题 | 命中后的结果 |
|---|---|---|---|
| 能力边界 | `services/safety.py` | **能不能做** | 拒绝，人工审批也放不过去；要放行只能改设置 |
| 审批节奏 | `services/permissions.py` | **要不要先问** | 生成审批请求，批准后执行并记住 |

### 为什么必须先判「能不能做」

反过来会出现最难受的一种体验：审批卡上点了「允许」，工具还是失败。用户会认为
产品坏了，而不是意识到"管理员在设置里禁了这条"。所以 `ToolGateway.decide()`
在最前面过一遍 safety，`deny` 直接返回，连问句都不生成。

### 「每条命令都确认」不另造状态机

safety 的 `confirm_all_commands` 只是把这次调用的风险等级抬到 `high`，
后面全部复用既有逻辑：审批卡、`approve_actions` 记忆、「总是允许这个前缀」白名单。
再造一套"确认状态"会导致同一个动作被两层各记一次账。

`bypass` 档是用户显式选择的"我全权负责"，此时不抬风险；`plan` 档直接放行到
permissions 层，由它统一拒绝 —— 否则计划模式下会出现两条互相矛盾的拒绝理由。

### 默认值全部取最保守的一档

- `shell_enabled = false`：装好就能跑命令的产品，第一次误删就没人敢用了。
- `network_enabled = false`：`curl` / `git clone` / `pip install` / `npm install` 都算出网。
- `confirm_all_commands = true`：改这一项是最省事的"放宽"，放在最显眼的位置。
- `restrict_to_workspace = true`：工作区 + 用户显式加的目录之外，真实路径一律拒绝。

### 三个实现取舍

1. **只拦"真实存在"的越界路径**。`echo "C:/no/such"` 是很常见的字面量，把它判成
   越界会让人直接把整条策略关掉 —— 误杀的代价比漏判高。
2. **跳过每段命令的第一个 token**。那是程序本身（`C:/.../.venv/python.exe`、`npm`），
   不是被读写的文件；否则"跑自己的测试"都会被判越界。
3. **内置禁止清单不受配置影响**：格式化磁盘、关机、改系统注册表、接管文件所有权
   这类操作，问了也没意义。它们不是"高风险所以要确认"，而是"任何设置都不该放行"。

### 客户端没有 Docker 是常态，界面必须说实话

`GET /settings/safety/runtime` 返回 `backend_info()`：容器隔离还是本机受限执行，
Docker 装没装。桌面版老老实实写「本机受限执行：策略级隔离，不是内核级隔离」——
含糊地写一句"已开启沙箱"，客户会以为跑不可信代码是安全的。

### 顺手修的三处

- **设置文件跟着包走**：`data/settings.json` 落在 cwd（桌面版即包内），升级一次
  客户的权限边界就悄悄变回默认。新增 `BLANK_SETTINGS_FILE`（桌面壳指到用户数据目录）
  并做老位置搬迁。
- **`guardian` 被"存在但没配"的文件顶掉**：只写了 safety 段的设置文件会让
  `Guardian.from_settings` 认为 guardian 也配过了（normalize 出默认 `off`），
  于是 `BLANK_GUARDIAN=veto` 失效。加 `SettingsStore.has_section()`，
  按"显式配置过"判断而不是"文件存在"。
- **命令被策略拦下时不再判任务失败**：`run_validation_node` 原来把 `denied` 当成
  "测试未通过"，任务被判 failed，用户去翻代码——而真正该做的是改设置。
  现在记成 `skipped` 并写明「命令执行未获授权，本次未做自动化校验」。

### 验证时抓到的第四个问题：`search_code` 会把整个后端占死

现象：`POST /tasks/{id}/start` 返回 202 之后，后端连 `GET /health` 都不回包，
界面看起来是「应用卡死」。这一次不是"任务失败"，是进程根本没在服务。

用 `faulthandler.dump_traceback_later()` 抓栈，一眼看到：

```
graph.execute_action_node → gateway._run → filesystem._search_code
  → paths.iter_text_files → pathlib.read_text   ← 同步阻塞在事件循环里
```

两个原因叠在一起：

1. `TREE_IGNORE` 只有精确名（`dist`、`build`、`.next`），而构建残留是带后缀的
   （`dist-prev-0.4.3`、`out-old6`、`.next-old5`），一个都没被忽略。工作区指到本仓库时，
   搜索会去读 `dist-prev-*/resources/backend/runtime/Lib/site-packages` 里几万个文件。
2. `iter_text_files` 是同步生成器，`_search_code` 直接在协程里 `for` 它 ——
   整个搜索期间事件循环被独占。

修法不是加一个 `if`：

- `workspace.ignored_name()` 成为忽略口径的**唯一来源**（精确名 + 构建产物前缀），
  搜索、目录列举、快照三处共用，避免各写一套又各漏一半。
- `iter_text_files` 加文件数与时间预算（2 万文件 / 8 秒），并去掉"预读一次验可解码、
  搜索时再读一次"的双重 IO —— 解码失败交给读取处跳过。
- `_search_code` 用 `asyncio.to_thread` 下放线程，并一次读取同时用于预过滤与逐行匹配。
- 快照同理：`_scan` 改 `os.walk` 剪枝（`rglob` 先遍历再过滤，几万个文件一个都跳不掉），
  加 3 秒扫描预算，`capture()` 在 `runtime` 里用 `asyncio.to_thread` 调用。

修完实测：任务执行期间 `/health` 每 10 秒回 200，`confirm_all_commands` 正常产出
`waiting_approval` + 待审批请求（`run_shell` / `risk:ask`）。

### 界面

设置页第一屏换成「安全与权限」：顶部一条执行后端横幅（容器 / 本机受限 + Docker 状态），
下面两个面板——能力开关（4 个 toggle + 单条命令上限）、路径与命令边界
（允许目录、禁止前缀，两个可增删的列表）。

### 验证

- 新增 `tests/test_safety_policy_v050.py`（28 条）：默认关、内置禁止、网络开关、
  路径越界与允许列表、`confirm_all` 抬风险、网关先 deny 后 ask、设置 clamp 与部分更新。
- `tests/conftest.py` 给夹具写入宽松档（只放宽 `shell_enabled` 与 `confirm_all`），
  路径边界与内置禁止保持原语义；策略默认值本身由上面那 28 条断言。
- 后端 pytest 301 passed / 14 skipped / 0 failed；前端 `tsc --noEmit` 干净。
