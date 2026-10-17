# Blank AI Core Runtime Roadmap

目标：把 Blank AI 从“可调用工具的任务页面”补齐为可恢复、可审计、可扩展的 Agent Harness，能力目标对齐 Codex / Claude Code 这一类工程执行产品。

## 当前基线

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
- 工具大输出目前只做截断和证据摘要，还没有独立 artifact 存储与按需读取 API。
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

### 待做（按拆解结果排序）

| 优先级 | 差距 | 他们的做法 | 备注 |
|---|---|---|---|
| **P1** | 工具检索缺失 | Codex `tool_search`：工具多到让模型自己检索 | 我们现在 20+ 个工具还没到需要检索的规模 |
| **P2** | 分叉缺 `ephemeral` 与并行调度 | 两家都能并行跑多个 worktree 代理 | 隔离已具备，缺并发编排与配额 |
| **P2** | 压缩触发缓冲未对齐 | Codex `auto_compact_fallback_buffer_tokens`（16k 缓冲） | 我们按字符预算触发，未做剩余 token 水位 |
| **P2** | guardian 无 UI 配置面 | Codex 可配 `guardian_model` / `reasoning_effort` / `review_timeout_ms` | 后端已支持环境变量，缺设置页 |

### 另外两条可直接沿用的工程选择

- 两家都已从"npm 里的 JS"改成**编译型单文件二进制**，并且**都自带 ripgrep**（Codex 的
  `codex-path/rg.exe`）。我们的桌面壳已按同样思路自带 Python 运行时，方向一致。
- Codex 另有 `codex-code-mode-host.exe`：把代码执行放进独立宿主进程 —— 与我们沙箱的思路同源。


