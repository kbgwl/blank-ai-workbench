# Qwen Code 1.0 Preview 源码对照报告（2026-11）

研究对象：[QwenLM/qwen-code](https://github.com/QwenLM/qwen-code)，分支 `v1.0.0-preview`，HEAD `2949d6b9fe1f`（2026-11-16，"docs: 补充 1.0.0-preview.2 发布说明"）。
方法：只读 GitHub API / raw，未克隆仓库。全量文件树 4850 个 blob；对 `goal/`、`judgment/`、`workflows/`、`permissions/`、`agents/`、`memory/`、`skills/`、`protocol/` 逐文件读取。**未标"推测"的均为源码或仓库内文档原文；行号对应该 commit。**
本仓库基线：`E:/projects/kbgai/kbgai`。

> **先更正一个前提。** 任务描述里"README 原话：`/goal` will work until an independent verifier accepts"在本分支**不存在**。我读完了 `README.md`（107 行），并全文检索了 `README.zh-CN.md`，两份都只讲安装/登录/`--continue`/`-p`，没有 `/goal`，也没有 verifier。`/goal` 的实现里也没有独立验证者（见 §2.4）。这句话更可能来自 0.x 文档或外部博客，不能作为 1.0 的设计依据。
>
> **基线也已漂移。** 任务里写的 `services/agent/subagent.py`、`services/agent/persona.py` 已不存在；实际代码在 `services/api/src/blank_api/services/` 下。且 `agent/verifier.py`（503 行，确定性事实验证器）、`agent/goals.py`、`context_curator.py`、`agent/evidence.py` 都已存在。所以本报告把"缺口"改成"与现状的差分"，避免建议已经做完的事。

---

## 结论摘要

1. **1.0 没有重写 agent loop，它是"旧 loop + Qoder 商业壳"。** 主循环仍是 Gemini CLI 血统的 `query()`（`packages/core/src/core/agent-loop.ts:51`），执行被拆到 `event-bus/` 的事件驱动状态机；新增的是 `qoder/`（账户、推理、模型目录、计费字段）、`goal/`、`workflows/`、`judgment/`、`agents/teams/`。README 自己承认底层来自 Gemini CLI v0.8.2（`README.md:102`）。
2. **goal 模式的本质是"Stop hook 否决停止"的续跑器，不是计划器。** 每轮结束 Stop hook 返回 `deny` + 注入续跑提示，模型下一轮继续；轮次默认 100，硬上限强制 `Paused`；"完成"只靠模型按 completion-audit 清单自证后调 `update_goal(status="complete")`。**没有独立 verifier，没有第二次模型判定。**
3. **最值得抄的是 judgment 这一层：把"判定"做成独立服务。** Auto 权限模式把 allow/block 交给另一个模型端点（TypeSafe `/v1/systemone`，独立 model/apiKey/baseUrl），题型 `choice/noul/score`、概率阈值 0.6、任何异常 fail-closed；且判定配置**只从全局用户设置读，明确拒绝 workspace settings**（防止被审查的仓库自己选裁判）。这正好补我们的"验收还是模型自评"。
4. **workflows 是"JS 脚本 DSL + agent() 节点 + 哈希链 journal 重放"，不是多模型竞技。** 只有一个内置流程 `deep-research`；它的"3 票对抗验证"是三个同模型、独立上下文的 agent，不是独立模型。**Agent Arena 在整个仓库不存在**（按文件名全量检索 + README/CHANGELOG 均无）。
5. **我们真正缺的是"回合后自维护"，不是 loop。** 它是 per-turn fork 做记忆抽取（`MEMORY.md` 单写者、200 行/25KB 上限）+ 技能演化评审（评分 ≥8/10 才跑、10 分钟冷却、只读 fork、只出建议不改文件）。这两块是低成本、可灰度、与我们 `context_curator.py`（只读检索、无回写）互补的。

---

## 一、包与模块结构（简）

**事实**：npm workspaces，4 个包（`package.json:11-13`，`version: 1.0.0`，`package.json:3`）：

| 包 | 规模 | 职责 |
| --- | --- | --- |
| `packages/core` | 2299 个 src 文件 | agent loop、工具、权限、goal、workflow、memory、skills、协议 |
| `packages/cli` | 2004 个 | TUI（`ui/` 1217 个文件）、thread-manager 多会话、ACP、headless、daemon 接缝 |
| `packages/resource` | 90 个（src 84） | Agent/Skill/Workflow 的资源发现与快照（`UserResource.agents` 等） |
| `packages/ink-shim` | 3 个 | vendored ink |

核心 loop 在 `packages/core/src/core/` + `packages/core/src/event-bus/`；`core/agent-loop.ts` 只是入口（85 行），真正的循环在 `event-bus/query-stream.ts` 与 `event-bus/conversation/model-settled.ts`。

**与 0.x 的差别**：仓库里只有一句官方说法——"1.0 Preview brings together the agent harness capabilities of Qoder and Qwen Code"（`README.md:5`），CHANGELOG 只列了登录方式、原生安装、会话恢复（`changelog/qwen/changelog.md:14-19`）。**没有迁移文档、没有 breaking-change 清单**。从代码可证实的新增是 `packages/core/src/qoder/`（52 个文件：OAuth、token-manager、model-catalog、inference pipeline、business-info/credits）。**（推测）** 0.x → 1.0 的主线是"多 provider + 商业账户 + 团队/工作流"三件事，而不是 loop 语义变化；未取 0.x 源码，不下断言。

---

## 二、Agent loop 与 goal

### 2.1 一次 turn 的分段（事实）

事件驱动，不是 `while` 里顺序调用节点：

```
submit → request_start → stream_event/assistant_block → assistant
       → tools-settled → after-hooks(Stop) → 续跑 或 Terminal
```

- 事件清单见 `core/agent-loop.ts:35-49`（`request_start/stream_event/assistant_block/assistant/user/system/progress/attachment`）。
- 外层生成器只做 turn 生命周期与 `QueryEnd`，把事件透传给消费方：`event-bus/query-stream.ts:381-453`。
- turn 结束的处理在 `event-bus/conversation/model-settled.ts:877-907`：先跑 after-hook，`stopped` 直接 `end_turn`；有 `continuationMessage` 就重入 loop。
- 续跑消息是**合成的 user message**，`isMeta: true`（不进 TUI 历史/rewind/标题候选，但仍发给模型），`origin.kind = 'human'`：`core/query-hook-continuation.ts:23-38`。

### 2.2 状态怎么表达（这是缺口①的直接参照）

`QueryRuntimeState` 是显式结构，不是自由文本（`core/query-runtime-state.ts:27-45`）：

```ts
messages, turnCount, transition, stopHookActive,
maxOutputTokensRecoveryCount, malformedToolUseRetryCount,
hasAttemptedReactiveCompact, compressionState, autoCompactTracking
```

`transition` 是封闭枚举（`:15-25`），共 10 种：`next_turn / after_hook_continuation / stop_hook_blocking / max_output_tokens_escalate / max_output_tokens_recovery / prompt_too_long_recovery / reactive_compact_retry / token_budget_continuation / goal_auto_continuation / malformed_tool_use_retry`。

终态同样封闭：`terminal.reason` + `terminalDetail`，`TaskTerminalReasonDetail` 有 16 个值（`core/query-terminal.ts:17-33`），包括 `hook_stopped / max_turns / model_refusal / prompt_too_long / malformed_tool_use_exhausted / completed`。

**对我们的意义**：我们的 `graph.py` 节点靠模型文本 + `update_plan` 表达进度（`AgentState` 只有 `repair_rounds` 等少量字段，`graph.py:671-676`）。它这套"transition 枚举 + terminal detail 枚举"是低成本可搬的：状态机自己知道"为什么进入下一轮/为什么结束"，UI 和验收都能读结构而不是读散文。

### 2.3 预算控制（四层，事实）

| 层 | 机制 | 源码 |
| --- | --- | --- |
| turn 数 | `DEFAULT_MAX_TURNS`（值在 `query-params.ts`，本报告未读该文件） | `core/agent-loop.ts:22` |
| 阶段式 token 预算 | 严格递增的累计上限，跨档时给 `additionalTurns` 提示 | `core/turn-budget.ts:14-36` |
| goal 轮次 | 默认 100，`-1` 表示不限；硬上限到点强制暂停 | `goal/GoalManager.ts:55, 417-423`；`goal/goalLoopHook.ts:79-86` |
| workflow | `maxAgents=1000`、`maxItems=4096`、并发 `min(16, cpus-2)`、单 agent 最多 5 次重试 | `workflows/workflow-runtime.ts:95-101, 782, 846-850` |

另有 `core/doom-loop-guard.ts`（7.5KB）做重复工具调用熔断，未细读。

### 2.4 goal：loop 语义、模板、何时收尾（事实）

**驱动方式**：`registerGoalHooks.ts` 无条件挂一个 `Stop` 运行时 hook（`goal/registerGoalHooks.ts:19-41`）。`goalLoopStopHook` 每次模型要停时执行（`goal/goalLoopHook.ts:30-102`）：

1. 没有 goal → allow；
2. `Complete` → 清 goal，allow（`:47-53`）；
3. `Paused` / `Blocked` / `UsageLimited` / `BudgetLimited` → allow（`:56-71`）；
4. 否则 `recordTurn()` 记一轮（`:74`）；
5. 到硬上限 → `pauseForSafetyLimit()` → allow（`:79-86`）；
6. 未到上限 → `decision: 'deny'`，`modelInjection` 填续跑提示（`:92-101`）。

`deny + modelInjection` 经 `resolveHookAfterResult` 变成 `transition: {reason:'stop_hook_blocking'}` 的续跑 user message（`core/query-hook-continuation.ts:61-93`），`model-settled.ts:892-900` 重入 loop。

**maxTurns 语义**：创建时若不传，写入默认值 100，保证模型和用户都看得见（`GoalManager.ts:45-55`）；`-1` 只关闭"轮次"这一种停机条件，其他资源限制仍生效（`:48-54`）。`isOverTurnLimit()` 只是提示，`isOverHardTurnLimit()` 才被 stop hook 用来强制暂停（`:389-423`）。

**credits 预算**：`creditsBudget/creditsUsed` 字段存在（`goal/types.ts:51-54`），`validateCreditsBudget` 校验正数（`:123-129`），但**在 `goal/` 目录内找不到任何 `creditsUsed` 的自增/扣减点**；`UsageLimited/BudgetLimited` 也只是"允许停止"的终态。**（推测）** 扣减与置位在商业计费/宿主层，开源构建只保留状态机。**这是不要照抄的部分**（见 §7）。

**续跑模板的节奏控制**（`goal/template-renderer.ts`）：

- 配置：每 5 轮重发一次完整指令；剩余 ≤3 轮切 wrap-up（`:31-36`）。
- 四种：`full`（首次 + 每 5 轮）、`sparse`（其余轮，只给 objective + 进度）、`wrap_up`（收尾）、`turn_limit`（预算耗尽，明确要求总结而不是假完成）（`:112-163, 253-264`）。
- 完整指令里最重要的是 **completion audit**：把 objective 拆成"提示→产物"清单、逐项找证据、不把"测试绿/manifest 全/verifier 绿"当完成本身、不确定即视为未完成（`:54-78`）。
- 用户目标用 `<untrusted_objective>` 包裹并 XML 转义（`:194-211`），防提示注入。

**goal 状态怎么进上下文**：不是塞 system prompt，而是每轮一条 `<system-reminder>` 附件（`attachments/generators/goal-generators.ts:8-19`；`attachments/models/goal-attachments.ts:15-22, 40-62`）。理由写得很直白：轮次每轮变，塞 system prompt 会让长 goal loop 每轮缓存失效。

**恢复/续额**：`/goal resume` 是唯一的用户侧复活入口，可把 `Paused`/`Blocked` 拉回 `Active`，并在轮次耗尽时按当前配置续一整份预算（`GoalManager.ts:619-689`；`goalCommand.ts:314-321` 传 `grantFreshBudget: true`）。模型侧的 `update_goal` 只能 `complete` 或 `blocked`，不能给自己续额（`tools/goalTools.ts:232-275`；`GoalManager.ts:636-638` 注释）。

### 2.5 有没有独立 verifier？

**没有。** 三条证据：

1. `goal/` 全目录只有状态机、模板、stop hook、持久化、mutex、kickoff；没有调用任何判定模型。
2. 完成判定写死在提示词里，由**同一个模型**执行 completion audit，然后自己调 `update_goal`（`template-renderer.ts:67-78`）。
3. 仓库里唯一的"独立判定"是权限的 JEV（§3.3），与 goal 无关；`/verify` skill 也是同模型按清单跑应用（`skills/bundled/verify.ts:17-235`），不是第二模型。

**独立上下文 ≠ 独立模型**：`deep-research` 的 3 票对抗验证是 3 个并行 subagent（`workflow-builtins.ts:313-358`），上下文互相隔离，但用的还是同一模型/同一套 prompt 体系。

**对我们的意义**：我们的 `verifier.py` 走的是"不调模型、只吃磁盘事实"的确定性路线，比它更硬。**不要为了对齐 Qwen 而把验收退回模型自证。** 真正可借的是它的"完成审计清单"文案结构（提示→产物→证据→反例），可以并到我们 verifier 的 finding 文案里，而不是替代 verifier。

---

## 三、工具与权限模型

### 3.1 工具注册（事实）

工具是声明式的 `BaseDeclarativeTool`，带 `Kind`（`Read/Edit`）和 JSON Schema（`tools/goalTools.ts:72-92`）；工具名收敛在 `tool-names.ts`；注册/过滤在 `tools/tool-registry.ts`（本报告未逐行读，`agents/AGENTS.md:13-14` 明确其为唯一注册处）。子 agent 的工具集由 `agent-setup.ts: buildSubagentRegistries()` 单点构造（`agents/AGENTS.md:216-245`），顺序是：MCP 发现 → `tools` 白名单 → memory 工具补充 → fresh/background 过滤 → `disallowedTools` → 内部工具。

### 3.2 五种权限模式（事实）

`PermissionModes`：`default / acceptEdits / auto / dontAsk / yolo`（`permissions/manager/decision/types/decision.ts:15-21`）。别名 `bypassPermissions → yolo`、`accepting_edits → acceptEdits`（`:35-49`）。Shift+Tab 循环顺序：`dontAsk → default → acceptEdits → yolo → auto`（`permission-modes/modes.ts:103-109`）。

| 模式 | 语义 | 关键实现 |
| --- | --- | --- |
| default | 写入/执行先问；文件编辑给"切 acceptEdits"的建议 | `permission-manager.ts:101-118` |
| acceptEdits | 编辑预放行，但 safety objection / unskippable 仍问 | `:119-142` |
| auto | 先自检，拿不准交分类器；分类器不可用则 deny（fail-closed） | `:181-220` |
| dontAsk | 不弹窗，原本要问的一律拒绝；与 plan 状态互斥 | `:144-165`；`modes.ts:160-172` |
| yolo | 全放行，但 unskippable / bypass-immune 仍问 | `:166-180` |

**plan 不是 mode，是独立状态**（`modes.ts:156-172`；`agents/AGENTS.md:262`）。这跟我们 `permissions.py:31-35` 把 `plan` 当第四种模式不同，差异见 §3.5。

**子 agent 模式合并**：父模式是 `dontAsk/yolo/acceptEdits/auto` 时，子 agent 不能改（不能放宽也不能收紧）；父是 default 时才允许子声明更宽的模式（`modes.ts:131-154`）。这是"父级决定不可被孩子推翻"的硬规则。

**规则语法与来源**：`Tool` 或 `Tool(content)`（`permissions/manager/decision/rules/parser.ts:7`）；settings 形状 `permissions.allow/deny/ask`（`permissions/settings/permissionsLoader.ts:25-40`）；来源 `userSettings/projectSettings/localSettings/flagSettings/policySettings/cliArg/command/session`（`decision.ts:76-84`），policy 只读（`permissionsLoader.ts:42-50`）。

### 3.3 Auto 模式：两段式分类 + JEV 独立判定（事实）

两条路径，二选一（`auto/auto-judge.ts:31-86`）：

- **LLM 分类器**（默认）：同模型两段式——fast 先判，拿不准升 thinking（`auto/classifier.ts:73-75`，`AutoModeClassifierResult.stage: 'fast'|'thinking'`）。输出 JSON `{shouldBlock, reason}`，schema 校验失败或 `shouldBlock` 不是真布尔值 → **默认 block**（`auto/classifier-invoke.ts:70-90`）。
- **JEV 判定**（配置了 `judgment` 时）：调用独立端点，见下。

**judgment 的三种题型**（`judgment/types.ts:20-35`）：

| 题型 | 形状 | 返回 | 用途（仓库内证据） |
| --- | --- | --- | --- |
| `choice` | `criteria: Record<label, 描述\|null>` | `choice` + 可选 `probabilities/confidence` | **唯一有调用点的题型**：权限 allow/block（单题）与 pass/block/unclear（三题） |
| `noul` | 可选 true/false 描述 | `noul: number`（0..1） | 无调用点（全仓文件名 + 已读代码范围内）。**（推测）** 给宿主做二值概率判定 |
| `score` | 至少 2 档的 criteria 数组 | `score` + 可选 `probabilities` | 无调用点。**（推测）** 给排序/打分场景 |

**是不是独立模型调用：是。** `TypeSafeJudge` 直接 POST `${baseUrl}/v1/systemone`，带自己的 `settings.model` 和 `apiKey`，10s 超时、最多 3 次（`judgment/typesafe.ts:79-96, 98-143`）；settings 只接受 `typesafe|bailian` 两种 backend（`judgment/settings.ts:33-42`）。题目与 state 一起发过去，返回按题型做 schema 校验（`judgment/validation.ts:11-119`）。

**谁调用**：`SessionPermissionManager.getAutoModeJudge()` → `JevAutoJudge`（`manager/session-permission-manager.ts:142-144`；`auto/auto-judge.ts:50-86`；`jev-classifier.ts:148-187`）。judge 按 session 懒创建并缓存（`auto-judge.ts:63-74`）。

**判定策略**：单题就是 `allow|block`；多题是 `integrity / risk / restrictions` 三个 `choice`（`jev-multi-policy.ts:37-109`），每题必须给 `pass` 概率，`pass` 且 `p ≥ 0.6` 才算过，任一题不过就 block（`jev-classifier.ts:196-234`，阈值 `jev-multi-policy.ts:10`）。异常一律 `shouldBlock: true`（`jev-classifier.ts:285-306`）。

**一个很值得抄的安全细节**：判定配置**只从全局用户设置读**，注释写明"a checkout must not select the server deciding whether that checkout's commands may execute"（`auto/jev-settings.ts:27-28, 29-94`）。也就是说，被审查的仓库不能自带一个"永远放行"的裁判端点。

### 3.4 与 Claude Code 的异同（事实 + 少量推测）

**它没有公开声明"兼容 Claude Code"**，但兼容痕迹是系统性的：

- 模式别名直接用 CC 的旧名 `bypassPermissions`/`accepting_edits`（`decision.ts:35-49`）。
- 权限规则 `Tool(content)`、settings 分层、`permissions.allow/deny/ask` 与 CC 同构（§3.2）。
- Hook 事件名与 CC 一致：`PreToolUse/PostToolUse/SubagentStop/SessionStart/PreCompact/Stop/UserPromptSubmit`，另加自己的 `TeammateIdle/TaskCreated/TaskCompleted/PermissionRequest/PermissionDenied/WorktreeCreate`（`hooks/types.ts:41-71`）。
- 内部文档明确写"对齐 CC 2.1.233"（`agents/AGENTS.md:178`），测试 fixture 名为 `cc-2.1.260-prompts.json`（`workflows/__fixtures__/`）。

**差异**：

1. CC 的 `plan` 是 mode；Qwen 把 plan 做成独立状态，并新增 `dontAsk` 与其互斥（`modes.ts:160-172`）。
2. Qwen 多一个 `auto`（LLM 分类器）——CC 没有等价物。
3. CC 的 `bypassPermissions` 是规范名；Qwen 规范名是 `yolo`，`bypassPermissions` 只是别名。
4. Qwen 的不可跳过项叫 `unskippable` / `bypass-immune`（`permission-manager.ts:121-124, 168-173`），概念上等于我们的 `IRREVERSIBLE` 闸门（`permissions.py:155`），但它是"逐工具 checker 打的标记"，不是一条全局正则。**（推测）** 这比单条命令正则更难被绕过，但覆盖面依赖每个 checker 是否老实标记。

### 3.5 与本仓库的差分

| 维度 | 本仓库 | Qwen 1.0 | 判断 |
| --- | --- | --- | --- |
| 模式 | default / accept_edits / plan / bypass（`permissions.py:31-35`） | 5 模式，plan 独立 | 各有取舍；不建议为对齐而拆 plan |
| 不可逆闸门 | 全局 `IRREVERSIBLE` 正则（`:155`） | 每工具 checker 标记 unskippable/bypass-immune | 我们的更集中，它的更细；可借"checker 打标"思路 |
| 自动判定 | 无 | auto + 两段分类器 / JEV 独立端点 | **真正的缺口**，但要按 §6 的成本与风险上 |
| 审批 UI | 四模式 + 人工闸门（可打回步骤） | ask handler + hook 桥 + 队列重放（`permissionModeQueueEffect`） | 我们的"打回"更强，不必抄 |

---

## 四、SubAgents / Agent Teams / Arena

### 4.1 SubAgent（事实）

统一一个 `Agent` 工具，`subagent_type` 省略时默认 `general-purpose`；显式 `fork` 走 fork 分支（`agents/AGENTS.md:183-185`）。执行链：`subagent-tool.ts → local-agent-runner.ts → run-agent.ts → run-subagent.ts → core/agent-loop.ts:query()`（`agents/AGENTS.md:26-36`）。

- **上下文隔离**：子 agent 有独立 `ToolRegistry`、独立 query 上下文、独立 transcript lineage（`agents/AGENTS.md:249-253`）；fork 复用父会话工具 schema 但执行仍走子 registry（`:46-48`）。
- **深度/并发**：默认嵌套深度 1、并发 20，均可环境变量覆盖（`agents/subagent-limits.ts:14-15, 17-38`）。到深度上限时既拒绝调用，也把 `Agent` 工具从子 registry 隐藏（`:24-31`）。
- **worktree 隔离**：`isolation: 'worktree'`，agent worktree 是临时命名，删除前要持锁检查 owner（`services/worktreeService.ts:1248-1300, 1323-1337`）。
- **memory 工具补充**：开启 memory 时自动给子 agent 补 Read/Write/Edit（`agents/AGENTS.md:58-60`）。

### 4.2 Agent Teams（事实）

进程内持久 teammate，不是新进程：

- **名册即状态**：`AgentTeamConfig { name, leadSessionId, members[] }`，成员含 `agentId/taskId/status/worktreePath/resolvedModel`（`agents/teams/agent-team-runtime.ts:32-57`）。
- **共享任务列表**：复用统一 `TaskRegistry`（`tasks/task-registry.ts:68-82`），任务类型含 bash/agent/workflow/internal/video，带并发槽位与进度节流。
- **成员预留与唤醒**：`reserveAgentTeamMember()` 用 `proper-lockfile` 锁名册文件，原子预留成员名，失败分 13 种 stage/code（`agent-team-runtime.ts:72-94, 373-540`）；`claimAgentTeamMemberWake()` 用"已 running 就拒绝"的 CAS 语义防重复唤醒（`:626-673`）。
- **邮箱**：`TeamMailboxMessage { from, to, text, read, ... }`，main 是保留收件名（`:59-70, 465-474`）。
- **worktree 写者唯一性**：不在 teams 层，而在 worktree owner 文件——`WorktreeOwnerState { ownerSessionId, ownerPid, ownerClaimId }`，claim 结果显式区分 `owned / owned-by-another-session / unavailable / cleanup-pending`（`services/worktree-owner-state.ts:10-51`）。同一 worktree 同时只有一个 session owner。

### 4.3 Agent Arena（未找到）

- 全量文件树（4850 blob）按 `arena / compare / compet / multi-model / tournament / judge(仅 permissions 一个) / score(0)` 检索，**没有任何竞技/多模型对比模块**。
- `README.md`、`changelog/*` 均未提及 Arena。
- 最接近"多路对比"的是 `deep-research` 的 3 票对抗验证（同模型、独立上下文）与 `auto` 模式的 `shadow` 判定（JEV 影子执行只上报不生效，`jev-classifier.ts:185`），二者都不是"多模型竞技/判分"。

**结论（事实）**：1.0 Preview 没有 Agent Arena。若产品侧有此功能，未随开源分支发布。

---

## 五、Auto-Memory / Skills / curator

### 5.1 记忆：写入、索引、召回（事实）

- **四类**：`user / feedback / project / reference`（`memory/memory-types.ts:11-76`），与 Claude Code 的记忆分类同构。
- **两个根**：用户级 `~/.qwen/memory/`（跨项目）、项目级 `~/.qwen/projects/{proj}/memory/`（`memory/structured-memory-store.ts:46-67`），注入顺序 user 先于 project（`:69-82`）。
- **索引单写者**：`MEMORY.md` 由 **fork agent 独占写**；模块只提供读、截断、以及 TUI 删除时删一行的工程口子，明确"no rebuild-from-frontmatter path"（`memory/memory-index.ts:9-23`）。索引上限 200 行 / 25KB，超限加载时追加 WARNING（`:197-244`）。
- **写入作用域硬校验**：`type: user` 必须落 user 根，`type: project` 必须落 project 根；`feedback/reference` 属于"浮动"，由模型判断（`structured-memory-store.ts:133-178`）。这是"提示词路由 + 代码兜底"的双层。
- **何时抽取**：每轮对话结束后跑 fork（`services/autoMemoryExtractionService.ts:8-21`），fork 只允许 `Read/Write/Edit/Glob/Grep`（`:89-96`），每 N 轮一次（`onTurnComplete`，`:536`；N 由 `autoMemoryPolicy` 决定）。主 session 本轮已直接写过 memory 时跳过（`skipReason: 'main_session_direct_memory_write'`，`:844`）。
- **召回**：索引内容以"`Contents of {path} (scope-scope auto-memory, persists across conversations)`"块注入（`memory-index.ts:150-160`），主题文件由模型按需 Read。
- **独立模型路线存在但默认不启用**：`AutoMemoryGenerationSelection` 有 `existing | fork_full_context | independent_model_incremental` 三档（`auto-memory-generation/strategy-selection.ts:8-11`），但 `resolveAutoMemoryGenerationSelection()` 目前只返回 `existing` 或 `fork_full_context`（`:31-36`）；独立模型 agent 已实现（`independent-model-incremental-agent.ts:79-94`，`MAX_TURNS=5`，只允许 Read/Write/Edit）。

### 5.2 技能：发现、排序、自动演化（事实）

- **发现**：`SkillManager.discoverSkills()` 汇总 bundled/project/user/plugin（`skills/skillManager.ts:524`），两层去重：真实路径 + 名称，跨来源冲突单独计数（`:232-246`）。
- **排序**：`SkillUsage` 记 usageCount + lastUsedAt，得分 = `count * max(0.5^(days/7), 0.1)`（`skills/skillUsage.ts:54-60`），7 天半衰、最低 0.1；写文件用 `proper-lockfile` 且每技能每分钟最多记一次（`:62-90`）。
- **自动演化（curator 的等价物）**：`SkillEvolutionReviewService`，每轮结束触发（`services/skillEvolutionReviewService.ts:381-396`）：
  1. 先算"这轮值不值得评审"的分（用户输入长度、轮数、工具调用数、唯一工具数），阈值默认 **8/10**，低于阈值直接 `skipped`（`:123, 497-523`）；
  2. 10 分钟冷却（`:125, 524-541`）；
  3. 起一个**只读 fork**：只给 Read/Glob/Grep，maxTurns=5，明确"不得改文件"（`:179-212, 573-584`）；
  4. 输出严格 JSON：`{suggestions:[{skillName, action:'create'|'update', summary, confidence≥0.6, rationale}]}`，最多 3 条（`:543-569`）；
  5. **只出建议，不落盘**；create 不得复用已有名，update 只能指向 `source==='user'` 的可写 skill（`:294-306, 558-560`）。
  6. 评审标准是三条"强信号" + 两个 audit（`skillEvolutionReviewCriteria.ts:8-27`），例如"用户说了'以后都这样做'""过程中换了方法""一个可复用的 2+ 步流程出现了"。
- **`/remember`**：不是自动写，而是审查各层记忆、给"提升到 AGENTS.md / 留在 auto-memory / 清理重复"的建议清单，**必须先展示提案再改**（`skills/bundled/remember.ts:20-72`）。
- **`/learn`、`/curator`**：不存在（见 §8）。等价能力是 `/remember` + skill evolution + per-turn memory extraction。

### 5.3 与本仓库差分

我们的 `context_curator.py` 是"任务前确定性检索"（memory hits + 文件名候选 + 预算截断，见文件头 1-15 行），**没有回合后写入、没有索引文件、没有技能演化**。它的 per-turn fork 是补这一段的现成范式，但要解决：谁授权写、写什么类型、如何防重复/膨胀（它用 frontmatter type + scope 硬校验 + 200 行索引上限）。

---

## 六、daemon / Web Shell / channels（简）

- **协议**：JSONL wire protocol `1.5.0`（`protocol/index.ts:9`），控制信封 `control_request/control_response/cancel/keep_alive`（`:32-38`），SDK 消息类型覆盖 goal 快照、skill evolution、task 生命周期、hook 事件（`:208-234`）。
- **多端**：`thread-manager/` 是进程内多会话层（同一 sessionId 同时只能有一个活会话），SDK/ACP/headless 三个宿主只做协议适配，会话不反向依赖宿主（`cli/src/thread-manager/AGENTS.md:3-4, 8-20`）。
- **ACP**：`cli/src/acp/acpServer.ts`（42KB）+ `acpSessionHost.ts`（45KB）+ 工具/计划投影，是真实实现（编辑器接入）。
- **daemon / Web Shell**：**开源构建没有 daemon 服务端**。`SdkServer` 是个基类，`run()` 直接抛 `SDK daemon mode is not supported by this build`，注释说明产品入口用 `AppFramework.createSdkServer` 替换（`cli/src/sdk/sdkServer.ts:19-29`）。**没有 HTTP+SSE，也没有 Web Shell**（文件名 + 已读代码范围内）。
- **启发**：值得学的是"会话能力 = handler 契约，wire 编码归宿主"（`goalHandlers/modeHandlers/...`，`thread-manager/AGENTS.md:126-131`）。我们 FastAPI 已经是这个形状（路由薄、service 厚），不需要改架构；可以借它"commandUuid 贯穿六个生命周期上报点"的做法来去重流式终态（`:133-145`）。

---

## 七、可移植清单（按改造成本升序）

成本按"新增/改动行数"估，不含测试；风险指回归面与线上影响。

| # | 机制 | 落点（本仓库） | 估计 | 风险 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 1 | **goal 状态走 system-reminder 而非 system prompt** | `agent/graph.py` 的 `initial_messages()` / `turn_reminders()`（2718, 2763） | 60–100 行 | 低 | 它给的理由是缓存（`goal-attachments.ts:10-17`）。我们有 goal 表（`agent/goals.py`），只改注入通道，不动语义 |
| 2 | **结构化 transition + terminal detail** | `graph.py`（`AgentState` 671-680）+ `runtime.py` 事件 | 100–180 行 | 低-中 | 枚举可先只覆盖现有分支（plan/execute/validate/review/summarize/repair）；UI 与验收改读结构。**这是缺口①的正解** |
| 3 | **hook 事件补齐 team/task 生命周期** | `services/hooks.py` 事件映射（122-141） | 40–80 行 | 低 | 新增 `TaskCreated/TaskCompleted/TeammateIdle/PermissionDenied`；我们已有 Stop/SubagentStop，映射表是单点 |
| 4 | **worktree owner 文件（写者唯一）** | `services/worktree.py` + `subagent.py`（716-861 已有创建/删除钩子） | 100–160 行 | 低-中 | 照 `worktree-owner-state.ts:36-51` 的字段与结果枚举；重点是把"删除前检查 owner"接进现有 remove 路径 |
| 5 | **续跑提示的 full/sparse/wrap_up 节奏** | 新 `services/agent/goal_prompts.py` + 现有 Stop hook | 180–260 行 | 中 | 可直接借 §2.4 的四段文案与 `remaining<=3` 规则；但**不要**接 100 轮默认续跑（见 §8） |
| 6 | **回合后记忆抽取（单写者 + 索引上限）** | 新 `services/agent/memory_extract.py`；`context_curator.py` 读索引 | 300–450 行 | 中 | 先做"每 N 轮、只写 project 根、MEMORY.md 单写者、200 行/25KB"；写权限走现有审批，不自动放行 |
| 7 | **技能演化评审（只出建议）** | 新 `services/agent/skill_review.py` + `skillsmith` 现有建议通道 | 250–400 行 | 中 | 关键不是 prompt，是闸门：评分阈值、冷却、只读 fork、只建议不落盘。可先离线跑批 |
| 8 | **权限 auto 模式（两段分类器）** | `permissions.py` 新增 MODE_AUTO + 新 `services/agent/auto_classifier.py` | 400–600 行 | 高 | 必须 fail-closed、必须有独立端点/独立 key、必须不能由 workspace 配置；否则等于给提示注入开天窗 |
| 9 | **judgment 服务抽象（独立判定端点）** | 新 `services/judgment.py` | 150–250 行 | 中-高 | 只有在 #8 立项时才做；否则是空转。注意它只解决"判定"，不解决"验收" |

**建议的落地顺序**：1 → 2 → 3 → 4（一周内可完成、无产品决策）；5 需要产品先定"goal 要不要自动续跑"；6/7 先离线验证准确率与成本；8/9 单独立项，不进本轮。

---

## 八、明确不要抄

1. **"模型自证完成 + Stop hook 无限续跑"的 goal 闭环。** 它的完成判定没有独立证据源，只有 completion-audit 提示词；默认 100 轮、每轮都可能带工具调用。我们已有 `verifier.py`（不调模型、只吃磁盘事实），退回自证是负优化。
2. **默认 100 轮 / `-1` 不限轮次。** 这是给"订阅额度制"产品设计的；我们的成本模型不同，且已有 `repair_rounds` 与预算控制。要抄就抄"硬上限 + 用户显式续额"的结构，数值必须重定。
3. **credits 预算那套字段。** `creditsUsed` 在开源代码里没有扣减点（§2.4），照抄会得到一组永远为 0 的字段和四个无人置位的终态。等我们真有计费再接。
4. **JEV / SystemOne 端点依赖。** 它需要单独的模型、baseUrl、apiKey（`judgment/typesafe.ts:79-96`），开源用户默认拿不到；默认路径其实是 LLM 分类器（`jev-settings.ts:53-60`）。把商业判定服务当默认能力会把自托管用户挡在门外。
5. **10 万字节级的分类器 prompt（`classifier-prompt.ts` 103KB）。** 不可维护、不可审计、随模型漂移。我们要的是代码级规则 + 小 prompt，不是把策略写成一篇文章。
6. **每轮都跑 LLM fork 的记忆/技能评审。** 它是"订阅制 + 后台额度"的产物；我们必须先采样/离线评估，否则每轮多一次完整模型调用。
7. **Agent Teams 的完整名册/邮箱/唤醒体系。** 600+ 行、强依赖 `proper-lockfile` 与进程内生命周期；我们连持久 teammate 的产品形态都还没有，先做 worktree owner（#4）即可。
8. **workflows 的 JS 沙箱 DSL。** 需要一整套 parser/runtime/sandbox/journal（约 20 万字节），收益是"用户可写编排"；我们当前没有这个诉求，硬搬会得到一个第二执行内核。

---

## 九、未找到 / 不能确认

| 项 | 结论 | 检索范围 |
| --- | --- | --- |
| Agent Arena（多模型竞技/判分） | **未找到** | 全量文件名（4850）+ README/CHANGELOG + `agents/`、`workflows/`、`judgment/` 目录 |
| `/learn` 命令 | **未找到** | 全量文件名 + `cli/src/commands`、`cli/src/ui/commands` |
| `/curator` 命令 | **未找到**；等价物是 `/remember` + skill evolution | 同上 |
| `creditsUsed` 的自增/扣减点 | **未找到**；只有字段与校验 | `goal/` 全目录 + `goalTools.ts` |
| `goal_auto_continuation` transition 的写入点 | **未找到**；枚举存在（`query-runtime-state.ts:24`），但已读的 goal/hook/query 文件里无人写它 | 已下载的 goal/core/event-bus 文件 |
| Web Shell / HTTP+SSE daemon | **未找到**；开源 `SdkServer.run()` 直接抛错 | `sdkServer.ts:19-29`、`daemon-host.ts`、`thread-manager/AGENTS.md` |
| 0.x 与 1.0 的逐项差异 | **不能确认**；只有 README/CHANGELOG 的概括 | 未取 0.x 分支源码 |
| `noul` / `score` 题型的生产调用点 | **未找到**；只有类型与校验 | 已读的 `judgment/`、`permissions/`、`workflows/`（不排除未下载目录） |
| README 中"independent verifier"原话 | **不存在**；两份 README 全文核对 | `README.md` 107 行、`README.zh-CN.md` 全文 |

---

## 附：一句话给决策

**把 Qwen 1.0 当成"权限判定 + 回合后自维护"的参考实现，而不是"agent loop"的参考实现。** loop 我们已经不比它弱；真正值得投入的是两件小事：让状态机自己说清"为什么进入下一轮"（#2），以及让记忆在回合后有人维护（#6）。其余（auto 权限、judgment、workflows、teams）都要先回答"谁付费、谁授权、谁来验证"，否则抄过来只是多一套会烧钱的空转。
