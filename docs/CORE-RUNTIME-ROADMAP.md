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

### 待做（按拆解结果排序）

| 优先级 | 差距 | 他们的做法 | 备注 |
|---|---|---|---|
| **P1** | 审批粒度过粗 | 命令前缀放行（`approved_command_prefixes` / `prefix_rule`）+ **会话内记住批准**（`user_approved_for_session`）+ 4 档权限模式 | 需同时动 API 与前端，支撑设计里的"审批三档" |
| **P1** | 上下文只有一层压缩 | **两层**：`microcompact`（细粒度裁剪）+ `auto-compact`（整体压缩）+ 压缩边界 + 压缩前后 token 记账（`active_context_tokens_before/after`） | 现有 `ContextCompactor` 是第二层，缺第一层 |
| **P1** | Agent 不能主动提问 | Codex `request_user_input` / Claude `AskUserQuestion`（`AskUserQuestionInput` 已确认存在） | 事件流已具备，缺工具与前端交互 |
| **P1** | 没有子代理 | Codex `thread_spawn` + `parent_thread_id` / `depth` / `agent_path` / `nickname` / `role`；Claude `Task` + **`EnterWorktree`/`ExitWorktree`**（并行代理各占独立 worktree） | worktree 隔离是关键，不然并行会互相踩 |
| **P1** | 工具面窄（7 个） | Codex 有 `tool_search`（工具多到让模型自己检索）；两家都有 `web_search`/`web_fetch` | 先补联网，再考虑检索 |
| **P1** | 没有自动审查 | Codex **`guardian`**：独立线程 + 可配模型/推理强度/超时（`guardian_model` / `guardian_reasoning_effort` / `review_timeout_ms`），审批结果枚举里有 `guardian_approved/denied/aborted` | 这是"两级审批"的上层 |
| **P2** | 没有项目约定文件 | Codex `AGENTS.md`；Claude `CLAUDE.md`；且 Codex 的指令是**分层拼装**（`include_permissions_instructions` / `include_apps_instructions` / `include_collaboration_mode_instructions` / `include_environment_context` 分别可开关） | |
| **P2** | 没有跨窗口交接 | Codex：`auto_compact_fallback_buffer_tokens`（16k 触发缓冲）+ 耗尽时要求模型把检查点写进 **`notes`** 工具，并记录窗口 ID / 条目 ID | 压缩不是丢历史，是**有交接的换窗口** |
| **P2** | 没有记忆固化 | Codex `memory_consolidation` 事件 | |
| **P2** | 没有线程分叉 | `forked_from_thread_id` / `ephemeral` / `trunk_new` / `trunk_reuse` | |
| **P2** | shell 状态不跨调用 | Codex `shell_snapshot.thread_id` + `shell_environment_policy` | |
| **P2** | 没有中途改方向计数 | Codex `steer_count` | 能力已有（执行中插话），缺度量 |

### 另外两条可直接沿用的工程选择

- 两家都已从"npm 里的 JS"改成**编译型单文件二进制**，并且**都自带 ripgrep**（Codex 的
  `codex-path/rg.exe`）。我们的桌面壳已按同样思路自带 Python 运行时，方向一致。
- Codex 另有 `codex-code-mode-host.exe`：把代码执行放进独立宿主进程 —— 与我们沙箱的思路同源。


