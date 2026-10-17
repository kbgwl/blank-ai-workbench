# REA 逆向拆解：Codex 与 Claude Code 的内核

> 方法：REA（Reverse Engineer Anything）—— 不猜、看实物。
> 对**本机已安装的正式版**做静态拆解，所有结论标注可复核的证据来源。

## 0. 证据来源

| 目标 | 版本 | 实物 | 拆解方式 |
|---|---|---|---|
| Codex CLI | 0.154.0 | `codex.exe` **285MB**（Rust） | 提取 1,479,793 条字符串 → 去重 90,511 条 |
| Codex 附带 | — | `codex-code-mode-host.exe`、自带 `rg.exe` | 包结构 |
| Claude Code | 2.1.271 | `bin/claude.exe` **229MB**（Bun 编译） | 提取 479,972 条字符串 → 去重 318,134 条 |
| Claude Code 附带 | — | `sdk-tools.d.ts`（168KB，工具权威定义） | 直接解析 |

工具脚本：`E:\env\rea\strings.py`；产物：`E:\env\rea\out\{codex,claude}.uniq.txt`。

**第一个结论本身就很重要**：两家都已从"npm 里的 JS"改成**编译型单文件二进制**（Codex 是 Rust，Claude Code 是 Bun 编译）。分发形态和我们第 1 节的选择一致。

## 1. Codex 内核

### 1.1 工具面
`exec_command`（统一执行，含 `unified_exec`/`tty`/`write_stdin`）、`apply_patch`、`view_image`、`web_search`、`update_plan`、`request_user_input`、`notes`、`tool_search`、`exec_policy`

值得注意的三个：
- **`update_plan`** —— 计划是**可更新的对象**，不是一次性输出
- **`tool_search`** —— 工具多到让模型**自己检索工具**（`tool_search_call` / `tool_search_output`）
- **`notes`** —— 跨上下文窗口的**持久笔记**，配合压缩交接使用

### 1.2 上下文与压缩（本次最有价值的发现）
```
"auto_compact_fallback_buffer_tokens": 16384
"auto_compact_fallback_prompt": "<context_window_reminder>
  当前上下文窗口已耗尽。不要在这个窗口里继续任务…
  请立刻对 `notes` 做一次写入，保存检查点：目标、决策、进展、学到的、下一步，
  以及仍在处理的每个用户请求的窗口 ID 与条目 ID…"
```
配套的记账字段：`active_context_tokens_before` / `active_context_tokens_after` / `compaction_summary_tokens` / `compaction_ms`。

**即**：自动压缩 + 预留 16k token 作触发缓冲 + **跨窗口交接协议** + 持久 `notes`。压缩不是"丢历史"，是**有交接的换窗口**。

### 1.3 权限与审批（第二有价值的发现）
- 审批策略：`untrusted` / `on-failure` / `on-request` / `never`
- 沙箱档位：`read-only` / `workspace-write` / `danger-full-access` / 自定义（网络可单独开）
- **审批结果枚举揭示了两级审批**：
  `not_needed` · `config_allowed` · **`guardian_approved` / `guardian_denied`** · `user_approved` · **`user_approved_for_session`** · `user_denied` · `user_aborted`
- **guardian 是一个独立审查线程**，且是可配的：
  `guardian_thread_id` · `guardian_model` · `guardian_reasoning_effort` · `guardian_default_review_model_id` · `guardian_review_model_override` · `review_timeout_ms` · `had_prior_review_context`
- **命令前缀放行**：`approved_command_prefixes`，以及工具描述里明确写着
  `NEVER provide a prefix_rule argument for destructive commands like rm.`
- 授权来源分类：`session_approval` · `exec_policy_amendment` · `network_policy_amendment` · `command_execution` · `file_change` · `permissions`
- 拒绝分类：`sandbox_denial` · `network_policy_denial` · **`execve_intercept`**

### 1.4 扩展与生命周期
- **Hooks 全生命周期**：`session_start` · `session_end` · `user_prompt_submit` · `pre_tool_use` · `post_tool_use` · **`pre_compact` · `post_compact`** · `subagent_start` · `subagent_stop`
- **指令分层拼装**：`instructions_file` · `persistent_instructions` · `include_permissions_instructions` · `include_apps_instructions` · `include_collaboration_mode_instructions` · `include_environment_context`；项目约定文件是 **`AGENTS.md`**
- **压缩提示词可外置**：`experimental_compact_prompt_file`（压缩策略本身可调）
- **子代理树**：`thread_spawn` · `parent_thread_id` · **`depth`** · **`agent_path`** · `agent_nickname` · `agent_role` · `agent_limit_reached`
- **记忆固化**：`memory_consolidation` 事件
- **线程可分叉/复用**：`forked_from_thread_id` · `ephemeral` · `trunk_new` / `trunk_reuse`
- **shell 状态跨调用保留**：`shell_snapshot.thread_id` · `shell_environment_policy`
- **中途改方向是一等能力**：`steer_count` 计数字段

## 2. Claude Code 内核

### 2.1 工具面（44 个，来自 `sdk-tools.d.ts` 权威定义）
核心：`Bash` `FileRead` `FileWrite` `FileEdit` `Glob` `Grep` `NotebookEdit`
计划与任务：**`EnterPlanMode` / `ExitPlanMode`**、`TaskCreate/TaskGet/TaskUpdate/TaskList`、`TaskOutput`、**`Task`**（子代理）、`TaskStop`
协作：**`AskUserQuestion`**、`ReportFindings`、`SendFeedback`
扩展：`Mcp`、`ListMcpResources`、`ReadMcpResource`、`RefreshMcpTools`、**`ProposeSkills`**、`ProposeGoal`
运行时：**`EnterWorktree` / `ExitWorktree`**（并行代理各占独立 git worktree）、`REPL`、`Workflow`
后台与调度：`CronCreate/CronDelete/CronList`、`ScheduleWakeup`、`RemoteTrigger`、`Monitor`、`PushNotification`、`ReadNotifications`
产出：`Artifact`、`Projects`、`ClaudeDesign`

**三点最值得学**：
1. **计划模式是一等公民**（`EnterPlanMode`/`ExitPlanMode`）—— 与 Codex 的 `update_plan` 殊途同归
2. **worktree 隔离** —— 并行代理不在同一份代码上打架
3. **代理能主动提问**（`AskUserQuestion`）与**主动提议技能/目标**（`ProposeSkills`/`ProposeGoal`）

### 2.2 上下文与压缩
`auto-compact` · `autocompact` · **`microcompact`** · **`compact_boundary`** · `/compact` · `context_limit` · `maxOutputTokens`

**两层压缩**：microcompact（细粒度裁剪）+ auto-compact（整体压缩），并用 `compact_boundary` 在对话里标记压缩边界。

### 2.3 权限
4 档权限模式：`defaultMode` · **`acceptEdits`** · **`bypassPermissions`** · **`plan mode`**
工具级控制：`allowedTools` · `disallowedTools` · `additionalDirectories`（额外可访问目录）

### 2.4 扩展与生命周期
- **Hooks**：`PreToolUse` · `PostToolUse` · `UserPromptSubmit` · `SessionStart` · `SessionEnd` · **`PreCompact` · `PostCompact`** · `Notification` · `Stop` · `SubagentStop` —— **与 Codex 几乎同名**
- 扩展面：`CLAUDE.md`、`SKILL.md`、`skills`、`slashCommand`、`subagent_type`、`mcp__` 前缀

## 3. 两家撞在一起的地方（这就是"内核"该有的样子）

| 能力 | Codex | Claude Code | 判断 |
|---|---|---|---|
| 计划作为可更新对象 | `update_plan` | `EnterPlanMode`/`ExitPlanMode` | **共识，必做** |
| 工具级生命周期 hooks | 9 个事件 | 10 个事件 | **共识，必做** |
| pre/post 压缩钩子 | ✅ | ✅ | **共识** |
| 权限档位 | 4 策略 + 3 沙箱档 | 4 模式 + 工具黑白名单 | **共识** |
| 会话内记住批准 | `user_approved_for_session` | `allowedTools` | **共识** |
| 子代理 | `thread_spawn` + depth/path | `Task` + worktree 隔离 | **共识** |
| 项目约定文件 | `AGENTS.md` | `CLAUDE.md` | **共识** |
| 压缩后接续 | notes 检查点交接 | compact_boundary | **共识** |
| 自动审查 | `guardian` 独立线程 | — | Codex 独有 |
| 工具检索 | `tool_search` | — | Codex 独有 |
| 联网 | `web_search` | `WebSearch`/`WebFetch` | **共识** |
| 主动提问 | `request_user_input` | `AskUserQuestion` | **共识** |

## 4. 我们的差距（按性价比排序）

| # | 差距 | 现状 | 他们的做法 | 优先级 |
|---|---|---|---|---|
| 1 | **计划不可更新** | 一次性 `create_plan` 节点 | `update_plan` / plan mode | **P0** |
| 2 | **没有 hooks 生命周期** | 无任何扩展点 | 9~10 个事件 | **P0** |
| 3 | **审批粒度过粗** | 高风险一律阻塞 | 4 策略档 + 命令前缀放行 + 会话内记住 | **P0** |
| 4 | **上下文只有一层压缩** | 字符预算 + 摘要 | 两层压缩 + 压缩边界 + token 记账 | P1 |
| 5 | **Agent 不能主动提问** | 只能等指令 | `request_user_input` / `AskUserQuestion` | P1 |
| 6 | **没有子代理** | 单线程 | thread_spawn/Task + worktree 隔离 | P1 |
| 7 | **工具面窄** | 7 个 | 44 个（含 web/图片/检索） | P1 |
| 8 | **没有项目约定文件** | 无 | `AGENTS.md` / `CLAUDE.md` | P2 |
| 9 | **没有跨窗口交接** | 摘要即丢 | `notes` 检查点 | P2 |
| 10 | **没有记忆固化** | 无 | `memory_consolidation` | P2 |

## 5. 本轮实施的升级（P0）

1. **`update_plan` 工具** —— 计划从"一次性输出"变成**可更新对象**：步骤可增删、可标记完成、可插话追加（正好承接设计里"执行中插话并入计划"的交互）
2. **hooks 生命周期** —— 落地 6 个事件：`session_start` / `session_end` / `user_prompt_submit` / `pre_tool_use` / `post_tool_use` / `post_compact`。这是**扩展机制的地基**，没有它后面所有"可插拔"都是空谈
3. **审批细化** —— 命令前缀放行 + 会话内批准（`user_approved_for_session`），直接支撑设计里的审批分档

P1/P2 项进入 `CORE-RUNTIME-ROADMAP.md`，按顺序做。
