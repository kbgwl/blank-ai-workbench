# 计划 / 目标 / 代码审查机制参考：opencode 与 DeepSeek Harness 对照 Kbgai

> 调研对象：
> - opencode：`anomalyco/opencode`（原 `sst/opencode`），分支 `dev`，commit `d33b5783338b`（2026-11-19）。
> - DeepSeek Harness：`deepseek-ai/deepseek-harness`（CLI 名 `dsh`），分支 `master`，commit `2e39e70496aa`（2026-11-18，`dsh 0.2.2-rc.5`）。
>
> 读法：只读源码与仓库内文档，不引用博客。未标“（判断）”的句子都来自源码/仓库文档；标“（判断）”的是我基于源码的推断。正文用 `[OC-*] / [DSH-*] / [KB-*]` 做来源标签，标签与文件的对应见第 8.4 节。
>
> 我们的基线：`E:/projects/kbgai/kbgai`，FastAPI + LangGraph + Python，关键路径 `services/api/src/blank_api/`。

**目录**：0 结论 · 1 基线 · 2 计划 · 3 目标 · 4 完成判据 · 5 代码审查 · 6 落地清单 · 7 查不到 · 8 来源

---

## 0. 一句话结论：最值得抄的三件（以及一件只能自建的）

按“改了就能见效”排序：

1. **DSH 的 `change-scope`：审查前先产出确定性范围。**
   `scripts/change-scope.ts` 只输出 `baseSha / headSha / mergeBaseSha` 和 `committed / staged / unstaged / untracked` 四组路径，不做任何语义判断；语义审查交给 `dsh-code-review` 技能 `[DSH-R1][DSH-R2]`。我们的 `review_result_node` 现在是“把消息流丢给同一个模型再审一遍”，没有固定的 diff/scope 输入，也没有“这次到底改了哪些文件”的硬事实。抄这一层，审查才有稳定的输入边界。

2. **DSH 的 `exit_plan_mode` 三结局：Approve / Keep planning / Dismiss。**
   `Approve` 返回 `{approved:true}` 并在下一个 pre-step 静默退出计划模式；`Keep planning` 是一个**失败的工具调用**（`PLAN_REVISION_REQUESTED`），用户反馈作为工具结果回到模型；`Dismiss` 返回 `{approved:false}` 并结束当前轮 `[DSH-P3]`。我们已有 `exit_plan_mode`，但“继续规划”目前只是权限拒绝，用户反馈没有作为结构化工具结果回到模型——这是小改但体验差别很大的一处。

3. **opencode 的 goal judge 提示词：把“模型自称完成”明确排除在证据之外。**
   `judge.txt` 原文：`The assistant claiming the goal is met is not evidence.` 它只信工具结果、命令输出和文件内容，输出 `done / continue / blocked / paused / wait` 五选一 JSON `[OC-G2]`。这不是确定性验证器，但正好补我们 `verifier` 覆盖不到的语义判据；可以做成“可选的第二层”，不能替代 `verify_facts()`。

**第四件只能自建：把 `goal_spec.done_when` 从 `string[]` 编译成可执行检查。**
opencode 用独立 judge 补语义，DSH 干脆让执行模型自己 `update_goal(complete)` 并明确写“No independent evaluator” `[DSH-G3]`。两家都没有“每条判据可执行”的实现。我们已经有 `verify_facts()` 这个事实底座，缺的是把 `done_when` 变成 `checks[]`（命令、路径、文件内容、退出码），让 verdict 能逐条回答“哪条判据没过”。详见第 4 节。

---

## 1. 两个仓库的基线

### 1.1 opencode 的目录位置

仓库已重构，不再有 `packages/opencode`。本次相关的内核在：

| 主题 | 路径 |
|---|---|
| 计划模式 | `packages/core/src/plugin/plan.ts` |
| 目标插件 | `packages/core/src/plugin/goal.ts` |
| 目标 schema | `packages/schema/src/goal.ts` |
| 目标提示词 | `packages/core/src/plugin/goal/{kickoff,continuation,judge,reminder}.txt` |
| todo 工具 | `packages/core/src/tool/plugin/todo.ts` + `packages/core/src/tool/todowrite.txt` |
| todo 展示 | `packages/tui/src/util/todo.ts`、`packages/app/src/session/composer/todos.ts` |
| `/review` 命令 | `packages/core/src/plugin/command.ts` + `packages/core/src/plugin/command/review.txt` |
| 目标 UI | `packages/tui/src/feature-plugins/prompt/goal.tsx`、`packages/gui-plugins/src/goal/` |

### 1.2 DSH 的目录位置

DSH 是 Cordis 插件内核，“everything is a plugin”。本次相关的包：

| 主题 | 路径 |
|---|---|
| 计划模式 | `packages/plan/plan-mode/`（`src/index.ts`、`README.md`）、`docs/subsystems/plan.md` |
| 目标服务 | `packages/goal/goal/`（`src/*.ts`、`README.md`） |
| 目标工具 | `packages/goal/tool-goal/`、`packages/goal/command-goal/` |
| 目标续跑 | `packages/goal/goal-round-driver/`（`src/prompt.ts`） |
| todo | `packages/todo/tool-todo/`、`docs/subsystems/todo.md` |
| 仓库级代码审查技能 | `.agents/skills/dsh-code-review/SKILL.md` |
| 审查范围脚本 | `scripts/change-scope.ts` |
| PR 审批治理 | `.github/review-ownership/README.md`、`.github/workflows/weighted-approval.yml` |
| 工具调用 auto review | `packages/experimental/auto-review/`、`.agents/notes/implemented/feature/2026-08-28-auto-review.md` |

### 1.3 三层职责对照（先看清谁管什么）

| 层 | opencode | DSH | 我们 |
|---|---|---|---|
| **目标**（为什么做、何时算完） | `goal` 插件：会话存储 + 独立 judge + 自动续跑 | `goal` 服务：会话日志 + `tool-goal` + `goal-round-driver` | `set_goal` 的 `goal_spec`（task 级）+ `propose_goal` 的 `threads.session_goal`（会话级）；`verifier` 判定 |
| **计划**（怎么做、先做哪步） | `plan` agent（只读）+ `todowrite`（平铺清单） | `plan mode`（软提示 + `exit_plan_mode` 审批）+ `todo_write`（本轮清单） | `update_plan`（步骤 + 里程碑 + 证据）+ 计划文件 + `exit_plan_mode` |
| **审查**（改动对不对） | `/review` 提示词命令（同一会话） | 仓库级 `dsh-code-review` 技能 + PR 审批治理；`auto-review` 是工具权限审查，不是代码审查 | `skills/code-review` + `report_findings` + `review_result_node`（同模型自查）+ `guardian`（工具安全审查，默认关） |

一句话：**DSH 把“目标 / 计划 / todo”拆得最干净；opencode 把“计划”退化成一个 agent 模式；我们已经有最结构化的计划和最硬的验证器，缺的是独立审查与可执行判据。**

---

## 2. A. 计划（plan）机制

### 2.1 opencode：计划 = 一个只读 agent + 一份 todo

#### A1. 计划怎么产生、存在哪

- **没有“计划对象”。** `plan.ts` 只注册一个 `plan` agent：名字 `Plan`，描述 `Read-only agent for exploring the codebase and planning work before implementation.`，`mode = "primary"` `[OC-P1]`。
- 计划正文默认**只存在于对话里**。进入计划模式的提醒原文：
  > `Discuss the plan with the user directly in the conversation. Do not create or update plan files unless the user explicitly asks you to; when they do, write them only in: ${directory}`
  > `[OC-P1]`
- 唯一允许写的目录是 `~/.opencode/plan`；除此之外 `edit` 动作全部 deny `[OC-P1]`。
- **结构化清单是另一个工具 `todowrite`**：`{content, status}` 的数组，status 四态 `pending / in_progress / completed / cancelled`；每次调用提交**完整列表**，覆盖上一版 `[OC-P2]`。
- todo 没有服务端状态：TUI/客户端从“最近一次 completed 的 `todowrite` 调用”里读 `input.todos`，并以 revert 边界截断 `[OC-P3]`。

#### A2. 计划怎么更新

- 计划正文：没有增量更新语义；模型在对话里改，或者用户让它重写计划文件。
- todo：整表替换。工具描述原文：
  > `Send the full list on every call; it replaces the previous one.`
  > `[OC-P2]`
- 触发者：模型自己；没有“计划变更事件”之外的服务端计划状态。

#### A3. 怎么呈现给用户

- 计划本身：聊天正文；没有计划卡、没有进度条、没有里程碑。
- todo：TUI 侧边栏平铺清单，显示 `completed/total` 和 `in_progress`，超过 2 条可折叠 `[OC-P4]`。
- 没有层级（无 milestone、无父子步骤）、没有依赖关系。

#### A4. 是否分“规划阶段”和“执行阶段”，上下文是否隔离

- **分成两个 agent**：`plan`（primary，只读）和默认 `Build`（primary，可写）`[OC-P1][OC-P5]`。
- **上下文不隔离**：切换 agent 仍在同一个 session、同一份消息历史里；计划模式提醒通过 `session.hook("context")` 插到消息尾部 `[OC-P1]`。
- 压缩或 committed revert 可能把提醒剥掉，所以**每个 context 请求都重新检查**：当前是 plan 且最后一条提醒不是 enter，就补 enter；已经不是 plan 但最后一条是 enter，就补 leave。原文注释：
  > `Compaction and committed reverts can strip reminders while the session's agent stays put. Reconcile per request, appending near the tail so the cached prefix stays warm.`
  > `[OC-P1]`

#### A5. 计划与工具集：规划期是否禁用写工具

- 只 deny `edit` 动作；`edit` 在 `~/.opencode/plan/*` 下 allow；`question` allow `[OC-P1]`。
- **shell 没有被 deny**（默认 agent 规则是 `*:* allow`，见 `packages/schema/src/agent.ts:46`）。也就是说，计划模式不是硬只读边界：模型仍可运行会写文件的 shell 命令，只是提示词要求“不要修改其他文件、不要叫子代理改”。
- 结论（判断）：opencode 的 plan 是“agent + 权限 + 提醒”的软约束，不是安全沙箱。

### 2.2 DSH：计划 = 一个 log-only 模式 + 一次 markdown 提交

#### A1. 计划怎么产生、存在哪

- **模式状态**：`plan/mode` 是 log-only、whole-value-replace 的 session event，payload 只有 `{ active: boolean }`；最后一个事件胜出，没有事件则 fold 成 inactive `[DSH-P1]`。
- **计划正文不在状态里**：模型在 `exit_plan_mode` 的 `plan` 参数里交一整段 markdown，要求以 `#` 标题开头 `[DSH-P3]`。
- `exit_plan_mode` 是渐进披露的：从第一次进入 plan mode 起加入工具声明，并在整个 Session 保持 admitted；之后切换只改 guidance，不反复改工具列表 `[DSH-P3]`。
- **投影**：`plan` unit 从日志 fold 出 `{active, pending}`；`pending` 只表示“用户选了一个还未 append 的状态” `[DSH-P2]`。
- **todo 是另一层**：`todo_write` 每次提交完整列表，`TodoItem` 只有 `content` + `status`（`pending / in_progress / completed`），刻意没有 id、priority、activeForm `[DSH-P4]`。

#### A2. 计划怎么更新

- 计划正文：没有增量步骤；用户选 `Keep planning` 后，模型**重写整段计划**再交一次 `[DSH-P3]`。
- todo：整表替换；`turn/start` 时投影清空成 `null`，也就是说 **todo 只活一轮**，不是跨轮的持久计划 `[DSH-P5]`。
- 模式：`set(agent, active)` 在 turn 内是 `queued`，在 turn 外是 `committed`；只在下一个被接受的 in-turn pre-step append `[DSH-P2]`。
- 限制：`pending` 是进程内的；在 turn 最后一次 accepted pre-step 之后做的选择，如果进程退出就丢，UI 必须重新应用 `[DSH-P2]`。

#### A3. 怎么呈现给用户

- 计划：`exit_plan_mode` 通过 user-questions seam 弹一张 `plan-review` 卡，正文是完整 markdown，两个选项 `Approve` / `Keep planning`；关闭卡片等于“用户要自己说”，本轮结束 `[DSH-P3]`。
- `plan-review` 只是 presentation intent：只有 Web 有专用 renderer，其他交互 provider 走通用问题卡 `[DSH-P2]`。
- 限制：subagent 不能打开 plan review；`exit_plan_mode` 会失败，并让它把未决决策写进最终结果 `[DSH-P2]`。
- `/plan off` 可以直接退出；没有交互审查通道时 `exit_plan_mode` fail closed，`/plan off` 是手动逃生口 `[DSH-P2]`。
- 没有步骤级进度、没有里程碑；计划是一整块可读文本。
- todo：行式清单，有 `counts`（pending/inProgress/completed），没有层级 `[DSH-P5]`。

#### A4. 是否分“规划阶段”和“执行阶段”，上下文是否隔离

- **不分 agent，分模式**：同一个 session，同一个 agent；plan mode 只是在每次请求里加一段 `plan:policy` 提示 `[DSH-P1]`。
- **上下文不隔离**：计划讨论、工具结果、计划正文都在同一份历史里；计划状态靠日志 fold 在 resume/fork/compaction 后恢复。
- 计划模式是 **soft guidance**，原文：
  > `Plan mode is soft guidance. Sandbox mode and approval policy enforce restrictions independently; neither reads or writes plan state`
  > `[DSH-P1]`

#### A5. 计划与工具集：规划期是否禁用写工具

- **不禁用**。README 原文：
  > `It does not restrict the agent: every tool stays callable, so use sandbox mode and approval prompts when you need enforced limits.`
  > `[DSH-P2]`
- 计划模式只注册一个 prompt section 和一个 `exit_plan_mode` 工具；是否真的只读，取决于 sandbox / approval 配置，不取决于 plan mode。
- 结论（判断）：DSH 的 plan mode 是“协作状态 + 提示 + 审批”，不是权限机制。

### 2.3 计划机制对照表

图例：**能** = 语义可直接移植；**部分** = 只借结构或判据；**不能** = 依赖它的架构/前提；**我们已有** = 不需要抄。

| # | 它们怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| P1 | **opencode 没有计划对象**：`plan` 是 primary agent，计划在对话里；plan 文件只在 `~/.opencode/plan` 可选写 `[OC-P1]` | 有 `update_plan`：步骤带 `id/step/status/milestone_id/evidence`，可归到里程碑，整表替换，同一时刻只允许一个 `in_progress`（`services/tools/plan.py:232`） | 不能（降级） | 不要改成“纯聊天计划”。保留 `plan_steps`；要抄的是 opencode 的“计划模式提醒在压缩后重注入”，我们已在 `services/plan_mode.py` 用每 5 轮完整提醒 + 精简提醒实现，确认压缩后仍会补即可。 |
| P2 | **DSH 模式状态是 log-only**：`plan/mode {active}`，最后一个事件胜出；`pending` 是进程内选择，等下一个 pre-step 才 append `[DSH-P1][DSH-P2]` | 计划模式是权限档 + `plan_file`，模式切换即时生效（`services/permissions.py:965`） | 部分 | 只借“pending”语义：turn 内切换模式时，先记 `pending_mode`，在**下一次模型请求前**统一应用，避免当前 step 的提示词与工具策略不一致。落点：`services/plan_mode.py` + `graph.py` 的模型调用入口；不要改 `plan_steps` 的即时性。 |
| P3 | **DSH 计划提交是一整段 markdown**，`exit_plan_mode(plan)` 要求 `#` 标题；没有步骤级状态 `[DSH-P3]` | 计划文件 `.blank/plans/<task>.md`（`services/plan_mode.py:20`）+ `exit_plan_mode` 可读文件或收 `plan` 参数（`services/tools/plan.py:172`） | 部分 | 保留“计划文件 + plan 参数”的双来源；不要改成只有 markdown。可抄 DSH 的“计划必须能被用户原样审阅”：在审批卡里展示完整 markdown，而不是只展示摘要。 |
| P4 | **opencode todo 是平铺清单**，4 状态，整表替换，从最近一次工具调用派生，无服务端状态 `[OC-P2][OC-P3]` | 有里程碑 + 步骤 + `progress`，比它强（`services/tools/plan.py:71`） | 不能（降级） | 不要去掉里程碑。可抄 `cancelled` 状态：如果用户中途取消某步，现在只能删掉或标 completed；加 `cancelled` 能让“计划没做完但不是失败”显形。落点：`services/plan.py:27` 的 `PLAN_STATUSES` 与前端渲染。 |
| P5 | **DSH todo 只活一轮**：`turn/start` 把投影清空 `[DSH-P5]`；plan mode 的待办和长期计划是分开的 | `plan_steps` 跨轮保留在任务上；计划模式还有计划文件 | 我们已有（更强） | 不要抄“每轮清空”。要抄的是“todo 和 goal 分开”的边界：`update_plan` 只管路径，`set_goal` 只管判据；现在两者已经分开（`services/tools/plan.py:85` 的注释也明确写了“目标是判据，计划是路径”），保持。 |
| P6 | **opencode 规划期只 deny edit**，shell 仍可写 `[OC-P1]`；**DSH 完全不禁用工具** `[DSH-P2]` | 硬闸：`MODE_PLAN` 只允许 `READ_ONLY_TOOLS`，写文件只允许计划文件，shell 不在白名单（`services/permissions.py:96-140`） | 不能（我们更强） | 不要为了“像它们”而放松。反而要把这条写进产品说明：我们的 plan mode 是**执行域闸门**，不是提示词自律。 |
| P7 | **两家都没有“计划审批的三结局”**：opencode 没有审批，用户手动切 agent；DSH 有 Approve / Keep planning / Dismiss `[DSH-P3]` | 有 `exit_plan_mode` + 权限层 `mode:switch` 审批；但“继续规划 + 反馈”没有作为工具结果回到模型（`services/tools/plan.py:172`） | 能（DSH） | 把 `_exit_plan_mode` 改成三态：①批准 → 退出并返回计划；②拒绝并带反馈 → `fail("revision_requested", 反馈)`，保持 plan mode，模型可直接改；③关闭卡片 → 结束本轮、保持 plan mode。落点：`services/tools/plan.py:172` + 审批卡的拒绝原因透传。 |
| P8 | **DSH 计划模式是软提示**，不隔离上下文；opencode 也不隔离 `[DSH-P1][OC-P1]` | 同一 task/session，计划文件落盘，计划步骤在状态里 | 我们已有（更强） | 不要做“规划/执行两套上下文”。真正要防的是压缩后丢计划：计划文件已经解决正文丢失，`plan_steps` 解决步骤丢失；再补一条“每轮请求前若 plan mode 且最近 N 条没有模式提醒，则插一条”即可（`services/plan_mode.py` 已有类似逻辑，确认覆盖压缩路径）。 |

**计划部分的结论：**

- 我们已经是三家里面**计划结构最完整、权限最硬**的：步骤 + 里程碑 + 证据 + 计划文件 + 审批。
- 唯一值得抄的是 **DSH 的“计划审批三结局”**，以及 **opencode 的“压缩后重注入模式提醒”**（我们已有，需确认覆盖）。
- 不要抄两家的“软提示 / 平铺 todo / 每轮清空”。

---

## 3. B. 目标（goal）机制

### 3.1 opencode：Goal + 独立 judge

#### B1. goal 的数据结构

`packages/schema/src/goal.ts` 的 `Goal.State` `[OC-G1]`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | goal id |
| `objective` | string | 目标原文，最大 4,000 字符（`MAX_OBJECTIVE`） |
| `status` | `"active" \| "paused" \| "complete"` | 持久状态 |
| `reason` | string? | 最后一次变化的原因：judge 的 reason，或暂停原因 |
| `rounds` | number | 自创建/恢复以来发出的 continuation 数 |
| `maxRounds` | number | 轮次上限，默认 50 |
| `stalls` | number? | 连续无进展轮数：没有工具调用，或 reason 与上一轮相同 |
| `boot` | string | 设置 goal 的服务进程 id；进程重启后不会自己续跑 |
| `updatedAt` | number | 最后更新时间 |

- 存储：插件自己的 storage，key 是 `session:<sessionID>`；不是 session message `[OC-G1]`。一个 session 一个 goal；children/forks 默认没有（plugin storage 是插件自己的）`[OC-G2]`。
- 读写通过 RPC：`get` + `changed` 事件 `[OC-G1]`。
- **没有 `done_when`、没有 `constraints`、没有 `out_of_scope`**。完成判据只有自然语言 `objective`。

#### B2. 完成判据怎么定义，谁判定“达成”

- 判据：**自然语言 objective**，没有可执行检查。
- 判定者：**一个独立的 judge 角色**。`goal.ts` 在 session idle 时调用 `ctx.session.generate({sessionID, prompt})`，prompt 来自 `judge.txt` `[OC-G2]`。
- judge 的硬规则（原文）：
  > `Stop acting as the assistant. You are now the reviewer for the session's goal. Do not continue the work and do not call any tools.`
  > `Decide whether the goal below has been met, using only evidence in the conversation above: tool results, command output and file contents. The assistant claiming the goal is met is not evidence.`
  > `"done" only when the evidence shows the whole goal is met. When in doubt, answer "continue".`
  > `[OC-G2]`
- 输出是严格一行 JSON：`done / continue / blocked / paused / wait`，其中 `continue` 可带 `next_step` `[OC-G2]`。代码用 `parseVerdict` 从第一个 `{` 截到最后一个 `}`，容忍 reviewer 包代码围栏或加一句话 `[OC-G2]`。
- judge 的输入包括 objective、当前仍在运行的后台 shell（如果有），以及会话上文；`wait` 只在有后台 shell 时允许，避免“没有东西会唤醒却一直等” `[OC-G2]`。
- 代码形态：`session.generate` 是一次生成调用，没有新建 agent/session，也没有换模型路由的代码（判断：**角色分离，不保证模型分离**）；代码只读取 `result.text`，没有看到把 judge 文本作为消息 append 的分支；会话里能看到的是 `continue` 产生的 `synthetic` continuation（判断：judge 的裁决文本本身不进对话）`[OC-G2]`。

#### B3. 有没有“目标偏移检测”

- **没有专门的 drift detector。** 它做的是三件事：
  1. `kickoff.txt` / `continuation.txt` 每次都重述 objective，并要求“用证据证明” `[OC-G4]`；
  2. `context` hook 在压缩后如果发现对话里没有 objective，就插一条 `REMINDER` `[OC-G5]`；
  3. judge 每次 idle 都拿 objective 重新看一遍证据，不满足就 `continue` `[OC-G2]`。
- 这些是“目标提醒 + 重新判定”，不是“比较当前计划与原目标、检测语义漂移”。（判断：能缓解压缩后遗忘，不能拦住“做着做着换了个目标但仍有进展”的情况。）

#### B4. 轮次 / 回合上限与“目标未达成就停”

- `MAX_ROUNDS = 50`；`MAX_STALLS = 3`；`WAIT_LIMIT = 30 分钟` `[OC-G6]`。
- 只有轮次/停顿/等待上限，没有 token/金额预算；成本闸在我们这边独立。
- 停止语义：
  - `done` → `status = "complete"`；
  - `blocked` / `paused` / 无法解析 verdict → `status = "paused"`，reason 写清楚；
  - `continue` 但同一 reason 连续 3 次，或这一轮没有任何工具调用 → `stalls + 1`，到 3 就 `paused`，reason `Not making progress: ...`；
  - `rounds >= maxRounds` → `paused`，reason `Reached the limit of 50 rounds`；
  - 等待后台超过 30 分钟 → `paused`；
  - 服务重启 → 之前 active 的 goal 恢复成 `paused`。
- 也就是说：**“没达成”不会变成 `failed`，只会变成 `paused` 并留下 reason**；用户可以 `/goal resume` 从第 1 轮重新开始（`rounds: 1`）`[OC-G7]`。

#### B5. 怎么呈现给用户

- TUI 在输入框下方显示 `◎ goal` / `◎ goal paused` / `◎ goal met`；点开 dialog 显示 objective、状态、reason，并提供 pause / resume / clear `[OC-G8]`。
- **不显示 `rounds / maxRounds`**（判断：轮次只在内部用，UI 只给状态和原因）。
- Web 侧有 pinned goal bar（`packages/gui-plugins/src/goal/`），文案是 `Following goal / Goal paused / Goal reached`。

### 3.2 DSH：Goal 状态机 + 模型自证

#### B1. goal 的数据结构

`packages/goal/goal/src/types.ts` + `docs/subsystems/goal.md` `[DSH-G1]`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | `GoalId` | 稳定身份 |
| `revision` | number | 每次持久变更 +1；所有修改用 `{id, revision}` 做 CAS |
| `objective` | string | 人类提出的完成目标；**没有 done_when/constraints** |
| `phase` | `active \| paused \| blocked \| complete` | 持久生命周期 |
| `blockedReason` | `{code, message}`? | 只在 `blocked` 时存在；`code` 是策略给的 lower-kebab-case |
| `maxGoalRounds` | number | 总轮次上限，默认 256 |
| `roundsStarted` | number | 已准入的最高轮次（`GoalView`） |
| `createdAt` / `updatedAt` | number | epoch ms |
| `activation` | `armed \| disarmed` | **进程内**，永不持久化 |

- 存储：session log 里的 `goal/change` 事件，version 1，payload 是**完整快照**或 clear 墓碑；`goal` projection 严格重放 `[DSH-G2]`。
- 分层：`GoalSnapshot` 含 id/revision/objective/phase/blockedReason/maxGoalRounds；`GoalView` 再加 `roundsStarted/createdAt/updatedAt` 和进程内 `activation` `[DSH-G1]`。
- 约束：一个 session 只有一个 current goal；未完成的 goal 必须先 edit/pause/resume/block/clear 才能换；completed 可以直接替换 `[DSH-G1]`。
- 重放会拒绝：非正轮次、轮次跳号、过期 revision、stopped phase 上继续记轮次、超过 cap `[DSH-G2]`。

#### B2. 完成判据怎么定义，谁判定“达成”

- 判据：**只有自然语言 `objective`**。没有 `done_when`，没有可执行检查。
- 判定者：**执行模型自己**。它调用 `update_goal(action="complete")`；工具只做权限/轮次/ref 校验，不判断语义 `[DSH-G3]`。
- 系统提示原文：
  > `Mark complete only when the objective is actually achieved. Mark blocked only after the same blocking condition persists for at least 3 consecutive goal rounds, and report that concrete condition in blocked_reason; difficulty, uncertainty, or useful remaining work is not blocked.`
  > `[DSH-G3]`
- DSH 自己把这件事写进了限制：
  > `No independent evaluator — the caller that records completion or blocking is authoritative; evaluator-backed certification is deferred to a separate policy layer.`
  > `[DSH-G1]`
- `blocked` 的机制：`tool-goal` 默认 `blockedAfterConsecutiveRounds = 3`；运行时只保证“已经过了 3 个不同的 goal round”，**不判断这 3 轮是不是同一个阻塞条件**——那仍是模型判断，必须在 `blocked_reason` 里写具体条件 `[DSH-G3]`。
- `blockedReason.code` 的来源：模型自报 block 固定 `model-reported`；driver 到轮次上限用 `round-limit` `[DSH-G3][DSH-G7]`。
- 人类可以 `pause / resume / clear`；`resume` durable paused 状态必须走用户命令/Web，模型工具 `update_goal(resume)` 会拒绝 `GOAL_TOOL_RESUME_PAUSED` `[DSH-G3]`。

#### B3. 有没有“目标偏移检测”

- **没有 drift detector。** 它做的是：
  1. 每轮 continuation prompt 重述 objective，并明确“当前 workspace / tool results / durable session state 是权威，不要相信之前的叙述” `[DSH-G6]`；
  2. `revision` + CAS 防止旧视图覆盖新目标；
  3. `roundsStarted` 严格递增、无跳号，防止轮次被伪造；
  4. `blocked` 需要连续 3 轮，防止一时困难就停。
- 这些都是**一致性/防篡改**，不是语义漂移检测。原文也承认：`Same-condition blocking remains model judgment` `[DSH-G3]`。

#### B4. 轮次 / 回合上限与“目标未达成就停”

- `maxGoalRounds` 默认 **256**；`goal-round-driver` 只在 agent 完全 idle、goal active 且 armed、还有余量时排队一轮 `[DSH-G7]`。
- 只有真正进入模型历史的 goal round 才消耗 cap；人类消息和陈旧 reservation 不消耗 `[DSH-G7]`。
- 停止语义：
  - 到 cap → driver 记录 blocker，stable code `round-limit`，phase 变 `blocked` `[DSH-G7]`；
  - complete / pause / block → 停止续跑；
  - turn 以 max tokens 结束、durability write 失败、agent 被取消、插件卸载 → driver 自己停；
  - 取消不会自动重启；goal 的 round 被取消 → 下一个 idle 点 pause；无关取消 → 只 disarm；
  - 进程优雅关闭/插件 reload → abort round，只 disarm，goal 保持 active `[DSH-G7]`；
  - 异常失败不自动重试：transient provider/persistence failure 需要之后人工 `resume`，driver 不会隐式重试 `[DSH-G7]`。
- 明确没有资源预算：
  > `Round-count budget only — maxGoalRounds does not meter tokens, currency, wall time, or provider quotas.`
  > `[DSH-G1]`

#### B5. 怎么呈现给用户

- Web 有 docked goal bar；`/goal` 命令可以 `pause / resume / clear`；goal tools 返回 compact JSON（id、revision、objective、phase、roundsStarted、maxGoalRounds、blockedReason、activation）`[DSH-G3]`。
- 没有 judge 可展示；goal view 暴露 `phase` 和 `roundsStarted/maxGoalRounds`，具体客户端是否把轮次画出来需看对应前端（本次未读 DSH Web UI，判断：至少数据是有的）。

### 3.3 目标机制对照表

| # | 它们怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| G1 | **opencode**：goal 字段 `id/objective/status/reason/rounds/maxRounds/stalls/boot/updatedAt`，无 done_when `[OC-G1]` | `goal_spec`：`objective/done_when/constraints/out_of_scope`（`services/tools/plan.py:85`），比它结构化 | 不能（降级） | 不要去掉 `done_when/constraints/out_of_scope`。可抄 `stalls` 的“同一 reason 连续 N 次”思想：如果 `verifier` 连续几轮给出同一个 blocking reason，就停止自动修复而不是换命令重试。落点：`services/agent/graph.py` 的修复回路 + `stall.py`。 |
| G2 | **DSH**：goal 字段 `id/revision/objective/phase/blockedReason/maxGoalRounds/roundsStarted/activation`，无 done_when `[DSH-G1]` | 有 task 级 `goal_spec` + 会话级 `propose_goal`（`services/tools/goals.py`）+ `goals` 表（`models/goal.py`） | 部分 | 借 `revision` 的 CAS 语义：`set_goal` / `update_plan` 的更新带 `revision`，防止旧视图覆盖新目标。落点：`services/tools/plan.py` 的 `goal_spec` 写入 + `services/agent/goals.py` 的 `GoalTracker.transition`。不必引入 13 个 goal 状态；我们已有更细的任务状态。 |
| G3 | **opencode 用独立 judge**：`judge.txt` 只信工具结果/文件内容，`done` 要求证据，`continue` 可带 `next_step` `[OC-G2]` | `review_result_node` 是**同一个模型**看消息流后输出 `{approved, findings}`（`services/agent/graph.py:6272`）；`verifier` 是独立事实检查 | 能（作为第二层） | 新增一个**可选**的 semantic judge：只在 `done_when` 里有“无法执行”的条目时调用；prompt 直接借 `judge.txt` 的规则；输出 `done/continue/blocked`，但**不能覆盖** `verifier` 的 blocking。落点：`services/agent/verifier.py` 之后、`review_result_node` 之前，新增 `semantic_review` 节点或函数。 |
| G4 | **DSH 让执行模型自证**：`update_goal(complete)`，明确“No independent evaluator” `[DSH-G3]` | 我们也允许模型通过 `review_result_node` 自证，但 `verifier` 会拦“零改动伪成功/假称测试通过” | 不能（最弱） | 不要把“是否完成”交给执行模型单独决定。保留 `verifier` 为主判据；模型只能提出“我认为完成了”，由 `verifier` 用事实复核。落点：`services/agent/graph.py` 的 `review_result_node` 返回值不要直接决定 `status`，只作为 `findings` 输入。 |
| G5 | **两家都没有 drift detection**：opencode 靠每轮重述 objective + judge；DSH 靠每轮重述 objective + revision `[OC-G5][DSH-G6]` | `out_of_scope` 已存在，但 `verifier` 没有按它检查；`no_extra_files` 目前恒 True（`services/agent/verifier.py:237`） | 必须自建 | 加一个 `scope_drift` check：①把 `out_of_scope` 编译成路径/命令模式，命中就 `blocking=False` 但记 finding；②把 `changed_paths` 与 `plan_steps` 的 `expected_files` 比对，计划外的改动记 finding；③在 `verifier` 的 `checks` 里新增 `scope_drift`。落点：`services/agent/verifier.py` 的 `CHECKS` 与 `verify()`。 |
| G6 | **opencode 50 轮 / DSH 256 轮**，到顶是 paused/blocked，不是 failed `[OC-G6][DSH-G7]` | `DEFAULT_MAX_STEPS=200` + token 预算；verdict 有 `incomplete` / `budget_exhausted`，语义比它们细 | 不能（我们更强） | 不要换成“目标轮次上限”。保留步数/预算闸；把“预算耗尽”继续映射成 `INCOMPLETE(budget)`，不要映射成 `FAIL`。可抄的是“到顶时写清楚 reason”：在 verdict 的 `findings` 里明确 `Reached the limit of ... rounds`。 |
| G7 | **opencode goal 有 UI 状态条**：`◎ goal / paused / met`，dialog 显示 objective + reason + pause/resume/clear `[OC-G8]` | 我们有任务状态与 goal 表，但前端是否有同等“目标条”需单独确认（本次未读前端） | 部分 | 如果前端还没有，补一个 pinned 状态条：显示 objective 摘要 + 状态 + reason；不要显示 judge 的内部推理（opencode 也没显示）。落点：前端任务页；后端只暴露 `goal_spec` 和 `verdict.reason`。 |
| G8 | **DSH 的 goal 是 session log 事件**，resume/fork/compaction 都能恢复；activation 不持久化 `[DSH-G2]` | `goal_spec` 在 task 行 JSON；`goals` 表存长期目标；事件账已有 | 部分 | 不需要改成事件溯源；但要把 `goal_spec` 的每次变更写一条事件（如果现在只写最终值），这样 verdict 能引用“当时的目标版本”。落点：`services/agent/graph.py` 的 `set_goal` 分支（`:5381`）+ `goal_spec` 写入处。 |

**目标部分的结论：**

- 我们的 `goal_spec` 字段比两家都全；差距不在“字段”，在“判据可执行”和“谁有权判完成”。
- opencode 的 judge 提示词是最值得借的**语义判定模板**；DSH 的“模型自证”是最不该借的。
- 两家都没有 drift detection；我们的 `out_of_scope` 是现成的抓手，应该让 `verifier` 真的用它。

---

## 4. 完成判据：自然语言 vs 可执行检查，哪种更适合有 verifier 的我们

### 4.1 三种做法并排

| 做法 | 代表 | 判定者 | 证据要求 | 优点 | 缺点 | 适合 |
|---|---|---|---|---|---|---|
| **自然语言 + 独立 LLM judge** | opencode `goal` + `judge.txt` | 另一个“角色”的模型调用（同一 session，不保证换模型） | 只信工具结果/命令输出/文件内容；模型自称不算证据 | 能处理“语义上算不算完成”；能给出 `next_step`；比自证多一层独立性 | 不确定、不可复现；judge 仍可能被有说服力的叙述骗；多一次模型成本；无法逐条定位“哪条判据没过” | 主观目标、UI/文案/体验类验收 |
| **自然语言 + 执行模型自证** | DSH `update_goal(complete)` | 执行模型自己 | 只有系统提示要求“actually achieved”；运行时只校验权限/轮次/ref | 最便宜；无额外调用 | 最弱；模型既有动机又有权力宣布完成；DSH 自己写“No independent evaluator” | 开放式长任务、人类会逐轮检查的场景 |
| **自然语言 + 可执行检查** | 我们（目标形态） | 确定性 `verifier`，不调用模型 | 磁盘事实、工具结果、测试退出码、文件内容 | 可复现、可审计；能拦“零改动伪成功/假称测试通过”；能逐条回答哪条没过 | 只能检查可观察的事实；“用户体验好”这类判据无法直接执行；需要把自然语言判据编译成检查 | coding 任务、有明确产物的交付 |

### 4.2 我们现在的实际状态（重要）

`set_goal` 已经接收 `done_when: string[]`，但 `verifier` 目前只做了两件事 `[KB-V1]`：

1. `delivery_matches_plan`：有 `done_when`、计划要求改文件、磁盘却零改动 → `blocking = True`；
2. `goal_met`：没有 blocking、没有预算耗尽、且“有 done_when 时必须有交付文本” → 才算 goal met。

也就是说，**`done_when` 的每一条文字并没有被逐条验证**；它现在更像“必须有交付文本”的触发器。这正是最值得改的地方。

### 4.3 建议：三层判据，确定性优先

```
第 1 层（主判据，必须）：可执行 checks
  done_when: [
    { text: "登录接口返回 200",
      check: { type: "http", method: "POST", path: "/login", expect_status: 200 } },
    { text: "新增文件存在",
      check: { type: "path_exists", path: "src/auth/login.py" } },
    { text: "单测通过",
      check: { type: "command", cmd: "pytest -q tests/test_login.py", expect_exit: 0 } },
    { text: "错误提示包含“密码错误”",
      check: { type: "file_contains", path: "src/auth/login.py", text: "密码错误" } }
  ]

第 2 层（辅助，可选）：独立 semantic judge
  只处理第 1 层标为 "manual" / "semantic" 的条目；
  prompt 借 opencode judge.txt 的规则；
  输出 done/continue/blocked，但只能把 "done" 降级为 "continue"，
  不能把 verifier 的 blocking 改成 passed。

第 3 层（展示，永远保留）：自然语言
  text 字段原样给用户看；模型可以读写，但不作为判定依据。
```

落地细节：

- **兼容旧数据**：`done_when` 允许 `string | {text, check}`；`normalize_goal` 把 string 包成 `{text, check: null}`，旧数据不迁移 `[KB-P1]`。
- **check 类型先做四种**：`command`（退出码）、`path_exists`、`file_contains`、`git_diff`（路径/内容）。这四种覆盖大多数 coding 验收，且全部可由 `verifier` 只读磁盘/工具结果完成。
- **`unknown` 不是失败**：命令没跑、环境不可用、审批被拒 → `check.status = "unknown"`，verdict 记 `INCOMPLETE`，不要判 `FAIL`。这与我们现有的“验证没跑成 ≠ 验证失败”一致。
- **模型不能改判据**：`set_goal` 之后，`done_when` 的 `check` 字段只能由用户或 `verifier` 读；模型改 `text` 可以，改 `check` 要留事件。否则模型可以先把判据改松再宣布完成。
- **返回值**：`verifier` 的 `checks` 从 `dict[str, bool|None]` 扩展成 `dict[str, {status, evidence}]`，或者在 `evidence` 里加 `done_when: [{text, status, evidence}]`；前者改动大，后者兼容性好（判断）。

### 4.4 结论

**有 verifier 的我们，应该以“可执行检查”为主，opencode 式 judge 只做第 2 层。**

理由不是“确定性更优雅”，而是：

1. 我们的产品已经在卖“拦零改动伪成功/假称测试通过”，如果完成判据仍是自然语言，这两条拦截就只覆盖了“有没有交付文本”，没有覆盖“交付是否满足每一条验收”；
2. opencode 的 judge 能补语义，但它自己也需要“证据”，而证据正是 verifier 已经在收集的磁盘事实；让 judge 读 verifier 的结构化结果，比让它读整段对话更省 token、更少幻觉；
3. DSH 的自证模式已经被它自己标记为“No independent evaluator”，不适合作为我们的主路径。

---

## 5. C. 代码审查（review）机制

### 5.1 opencode：`/review` 是一段提示词，不是独立流程

#### C1. 有没有独立 review 流程 / 子代理 / 插件

- **没有独立 reviewer agent。** `/review` 是 `command.ts` 注册的一个命令，执行方式是把 `review.txt` 拼上用户参数，作为**当前 session 的一条 prompt** 提交 `[OC-R1]`：
  > `text: append(PROMPT_REVIEW.replace("${path}", location.project.directory), input.prompt.text)`
  > `[OC-R1]`
- 也就是说：同一个模型、同一个会话、同一套工具；它可以调用 `Explore` 子代理去搜代码，但审查结论仍由主模型写。
- 没有 `/review` 的独立模型、独立上下文、独立工具集。

#### C2. 审查的输入是什么

`review.txt` 明确按参数选择范围 `[OC-R2]`：

| 输入 | 命令 |
|---|---|
| 无参数（默认） | `git diff`（未 staged）+ `git diff --cached`（staged）+ `git status --short`（untracked） |
| commit hash | `git show $ARGUMENTS` |
| branch | `git diff $ARGUMENTS...HEAD` |
| PR URL/编号 | `gh pr view` + `gh pr diff` |

然后强制要求：

> `Diffs alone are not enough. After getting the diff, read the entire file(s) being modified to understand the full context.`
> `[OC-R2]`

还会读 `CONVENTIONS.md`、`AGENTS.md`、`.editorconfig` 等约定文件；可用 `Explore agent` 查既有模式，可用 Web Search 查最佳实践 `[OC-R2]`。

#### C3. 审查输出什么结构

- **自然语言**，没有 JSON schema、没有固定字段、没有行号必填。
- 要求：
  - Bugs 优先：逻辑错误、边界条件、并发、安全、错误处理；
  - Structure：是否符合现有模式、是否该用已有抽象、嵌套是否过深；
  - Performance：只在明显有问题时提（O(n²)、N+1、热路径阻塞 I/O）；
  - Behavior Changes：行为变化要提，尤其是可能无意的；
  - “Be certain”：不确定就写 `I'm not sure about X`，不要冒充确定 bug；
  - 严重度要明确，不要夸大；要写清楚触发场景/环境/输入；
  - 语气 matter-of-fact，禁止奉承 `[OC-R2]`。
- 输出是**给用户读的评审意见**，不是机器可消费的 finding 列表。

#### C4. 审查结果怎么影响主流程

- **不影响。** `/review` 只是往会话里注入一段 prompt；模型输出评审意见后，用户自己决定改不改。
- 没有阻断、没有自动修复、没有 review 状态机、没有“必须解决才能交付”。
- 判断：它把 review 定位成“一个可调用的专家提示”，不是交付门禁。

#### C5. 是否区分“我写的代码”和“别人写的代码”

- **不区分作者，只区分范围。**
- 默认无参数 = 未提交改动（通常是自己刚写的）；commit/branch/PR = 指定范围（可能是别人的）。
- prompt 明确：
  > `Only review the changes - do not review pre-existing code that wasn't modified`
  > `[OC-R2]`
- 没有 author weight、没有 blame ownership、没有“作者自审不算”的规则。

### 5.2 DSH：`dsh-code-review` 是仓库级技能，不是运行时功能

#### C1. 有没有独立 review 流程 / 子代理 / 插件

- 有 `.agents/skills/dsh-code-review/SKILL.md`，但它的定位是：
  > `Use when reviewing a pull request in the deepseek-harness repo — orients the reviewer to this codebase's standards ...`
  > `[DSH-R1]`
- 技能自己开头就写：`This skill is guidance, not a complete checklist.` `[DSH-R1]`——它不追求把审查变成可执行规则。
- 这是**给在本仓库里干活的 agent（或人）用的仓库级技能**，不是 `dsh` 产品对用户代码的通用 review 功能。
- 在 master 全树里没有找到 `/review` 命令、`code_review` 工具或 review 节点（负向证据：22,247 个路径中，`review` 相关只有这个 skill、`auto-review` 权限审查、Forge PR UI 和治理脚本）。
- 从技能形态看，它不是子代理；技能加载后由当前 agent 按步骤执行。

#### C2. 审查的输入是什么

`SKILL.md` 规定的顺序 `[DSH-R1]`：

1. **验证 PR 的 live base 和 exact head**（不能凭记忆）；
2. 跑 `pnpm --silent run change-scope --base <verified-base-ref> --head <verified-head-ref>`；
3. 读 `change-scope` 报告：它只给路径和 dirty layer，**不替代语义审查**；
4. 把改动路径映射到“拥有该目录的文档”（AGENTS.md、defensive-patterns、各子系统文档）；
5. **先读要求，再看 diff**；
6. 读 diff 以及足够的上下文代码。

`change-scope.ts` 的输出结构 `[DSH-R2]`：

```ts
interface ChangeScopeReport {
  formatVersion: 1
  repositoryRoot: string
  input: { base: string; head: string }
  resolved: { baseSha: string; headSha: string; mergeBaseSha: string }
  paths: {
    committed: string[]
    staged: string[]
    unstaged: string[]
    untracked: string[]
  }
}
```

它**不输出 diff 内容、不输出行号、不做语义判断**；只解决“这次改动的边界是什么”。

#### C3. 审查输出什么结构

- 不是 JSON，是 GitHub review comment 形态。
- 原文：
  > `State the defect, location, impact, and evidence. Place a localized defect inline on the tightest relevant diff range; use a PR-level comment for cross-cutting architecture, scope, or review-wide synthesis. Separate blockers from suggestions and omit issues already enforced by a green gate.`
  > `[DSH-R1]`
- 严重度只有两档：**blocker** 和 **suggestion**；没有 P0/P1/P2，没有 `CONFIRMED/PLAUSIBLE`。
- 位置：能落到 diff 行的就 inline；跨模块/架构问题放 PR 级评论。
- 要求“短而实”：`a short review with one substantiated blocker is better than a list of nits` `[DSH-R1]`。

#### C4. 审查结果怎么影响主流程

分两层，必须分清：

1. **代码审查意见本身是建议性的**：作者要“verify each claim and fix or rebut it on technical grounds”；没有自动修复，也没有“review 不通过就不能 commit”的运行时闸门 `[DSH-R1]`。
2. **合并门禁是 PR 审批治理，不是代码审查技能**：
   - `weighted approval` 需要 **2 分**；
   - `CHANGES_REQUESTED` 会让状态保持 pending，即使分数够了；
   - 作者自己的 review 不算分；
   - 作者贡献 `min(100, mergedPRCount) × 11 / 1000` 分（最多 1.1），不能单独满足 2 分；
   - reviewer 的权重按“被改动的生产代码行中，该 reviewer 通过 git blame 拥有的比例”提升：`min(2, 1 + 4 × ownedLines / totalLines)`；
   - `/delegate @user` 可以把自己的分数转给别人的有效 approve `[DSH-R3]`。

所以：**review 技能产出意见，审批治理决定能不能合。** 两者不是同一个东西。

#### C5. 是否区分“我写的代码”和“别人写的代码”

- **在审批治理层，区分得非常清楚**：
  - `The pull-request author's own review ... do not count`；
  - 作者的“信用分”来自历史 merged PR 数，和 reviewer 的 approve 分是两条独立的线；
  - reviewer 的权重和被改动行的 blame ownership 挂钩——你越拥有这段代码，你的 review 越值钱；
  - 这不是“作者自审”，而是“作者的历史信用 + 别人的 ownership 加权”。
- **在 review 技能层，不区分作者**；它只按 PR 的 base/head 审 diff。

### 5.3 DSH 另外两个 “review” 不要混淆

| 名字 | 是什么 | 输入 | 输出 | 是否代码审查 |
|---|---|---|---|---|
| `dsh-code-review` skill | 仓库级 PR 审查指南 | PR base/head、change-scope、diff、文档 | GitHub review comments（blocker/suggestion） | 是，但是**仓库自用**，不是 runtime 功能 |
| `experimental/auto-review` | 工具调用前的权限审查 | 当前 session 的完整 action history + 待执行的 tool call | `{risk, decision}`，`low` 只能 allow、`medium` 可 allow 或 deny、`high` 只能 deny | **不是**；它审的是“这个工具调用能不能执行”，不是“代码写得对不对” `[DSH-R4]` |
| Forge review UI | PR 面板/评论线程 | GitHub PR | UI 状态 | 不是审查模型；是展示层 |

`auto-review` 的价值不在代码审查，而在“权限审查也要有完整证据链”：它把 session 里所有 user/direct-parent 消息、项目指令、历史调用、tool schema 按 surface 顺序拼成一条固定请求，待执行调用只放最后一条；缺 step ownership 就 fail closed `[DSH-R4]`。如果我们要做“独立审查”，这套**输入构造**比它的风险分类更值得借。

### 5.4 代码审查对照表

| # | 它们怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| R1 | **opencode `/review` 是同一会话的提示词**，不是独立 agent `[OC-R1]` | `review_result_node` 也是同一模型看消息流后输出 JSON；`skills/code-review` 是提示词技能 | 部分 | 不要只抄“提示词”。保留 `review_result_node` 的修复回路，但把 reviewer 换成**独立调用**（不同 system prompt + 只读工具 + 固定输入），不要让它继承执行模型的完整对话。落点：`services/agent/graph.py:6272`。 |
| R2 | **opencode 审查输入先 diff、再读全文件**，明确“Diffs alone are not enough” `[OC-R2]` | `review_result_node` 没有固定 diff 输入；`skills/code-review` 让模型自己跑 `git status/diff`，但不强制 | 能 | 新增 `review_scope` 步骤/工具：先产出 `base/head`、`changed_paths`、`untracked`，再把 diff + 全文件 + `goal_spec.done_when` 作为 reviewer 输入。落点：`services/tools/review.py` 或 `graph.py` 的 review 节点前。 |
| R3 | **DSH `change-scope` 只输出路径**，语义交给 skill `[DSH-R2]` | 有 `report_findings`（file/line/summary/failure_scenario/verdict/outcome，max 32），但触发它的 scope 不固定 | 能（最高优先） | 把 `change-scope` 的字段搬成一个 `review_scope` 工具或节点：`repository_root / base_sha / head_sha / merge_base_sha / committed / staged / unstaged / untracked`。不要让它输出 diff 内容；diff 由 reviewer 按路径自己取。落点：`services/tools/review.py` 新增工具 + `services/tools/registry.py` 注册。 |
| R4 | **DSH 审查先读要求文档，再看 diff** `[DSH-R1]` | `skills/code-review` 有“先取范围、只看本次改动、找调用点”的顺序，但没有“按路径映射到 owning docs” | 能 | 在 `skills/code-review/SKILL.md` 加一步：按改动路径找 `AGENTS.md` / 模块 README / 约定文件；没有就明确写“无 owning doc”。落点：`skills/code-review/SKILL.md` 第一节。 |
| R5 | **DSH 输出 blocker/suggestion，落 inline diff 行** `[DSH-R1]` | `report_findings` 有 `level low/medium/high/xhigh/max`（**审查投入档位，不是严重度**）+ `verdict CONFIRMED/PLAUSIBLE` + `outcome`；`skills/code-review` 有 `阻断/建议/可选` | 我们已有（部分更强） | 保留我们的结构；给 finding 增加 `severity: blocking/suggestion/optional`（不要复用 `level`）；再把 DSH 的“omit issues already enforced by a green gate”抄进技能。落点：`services/tools/review.py` 的 finding schema + `skills/code-review/SKILL.md` 输出节。 |
| R6 | **DSH 审查意见不阻断，审批分数才阻断** `[DSH-R3]` | `review_result_node` 的 `approved=false` 可以触发修复回路，修不完会 `status=failed`；`report_findings` 不阻断 | 部分 | 保留“review 可以触发修复”，但把“阻断”收敛到 `verifier`：reviewer 的 `blocking` findings 写进 `verdict.checks`，由 `verifier` 判 `FAIL`；reviewer 自己不要直接改 `status`。落点：`services/agent/graph.py:6272` + `services/agent/verifier.py`。 |
| R7 | **DSH 审批区分作者/他人**：作者自审不算分，reviewer 按 blame ownership 加权 `[DSH-R3]` | 我们单 agent，默认不区分；多 agent 场景没有 author 标记 | 部分（未来） | 如果做多 agent/团队审查，给 `report_findings` 加 `author_agent_id`，并在汇总时排除“作者自审”；不需要抄整套 weighted approval（2 分、delegate、blame 加权），那是 GitHub 治理，不是 harness。落点：`services/tools/review.py` 的 finding schema + 汇总逻辑。 |
| R8 | **DSH `auto-review` 构造完整输入**：所有 user/direct-parent 消息、项目指令、历史调用、tool schema，待执行调用放最后 `[DSH-R4]` | 我们的 `guardian` 只看“即将执行的工具调用 + 工作区根 + 风险等级”，输入很短（`services/guardian.py:GUARDIAN_SYSTEM`） | 部分 | 不要把它当代码审查；但可以借“输入要完整且按 surface 排序”的思路：如果以后做“权限审查升级”，把相关用户指令、历史拒绝、当前调用按顺序拼；现在不必改。 |
| R9 | **opencode 审查明确“be certain / 不确定就说不确定”** `[OC-R2]` | `skills/code-review` 有“不为凑数编问题”，但没有“不确定要明说”的硬规则 | 能 | 在技能输出节加一条：`不确定的写成“未验证/不确定”，不要冒充确定 bug`；与 `report_findings` 的 `verdict=PLAUSIBLE` 对齐。落点：`skills/code-review/SKILL.md` 第三节。 |

**审查输出这块，我们其实比两家都强：** `report_findings` 已经有 `file / line / summary / failure_scenario / verdict / outcome / max 32` 的结构，缺的是 `severity` 字段和“独立触发”。不要为了抄 DSH 退回纯 prose，也不要为了抄 opencode 退回“只有一个 `/review` 提示词”。

### 5.5 review 流程的完整时序（文字图）

#### A. opencode `/review`

```
用户输入 /review 或 /review <commit|branch|PR>
        │
        ▼
command.ts 把 review.txt + 参数拼成一条 prompt
        │  （同一 session，同一模型，不是子代理）
        ▼
模型执行范围内的 git 命令
  ├─ 无参数：git diff / git diff --cached / git status --short
  ├─ commit：git show <sha>
  ├─ branch：git diff <branch>...HEAD
  └─ PR：gh pr view + gh pr diff
        │
        ▼
读 diff 涉及的完整文件 + CONVENTIONS/AGENTS
        │
        ├─ 可选：调用 Explore 子代理找既有模式
        └─ 可选：Web Search 查最佳实践
        ▼
输出自然语言评审：bug / 结构 / 性能 / 行为变化
  + 严重度 + 触发场景 + “不确定”标注
        │
        ▼
用户自己决定改不改
（没有阻断、没有自动修复、没有 review 状态机）
```

#### B. DSH `dsh-code-review` + PR 审批

```
PR open / synchronize / ready
        │
        ▼
agent 加载 .agents/skills/dsh-code-review/SKILL.md
        │
        ▼
验证 live base 和 exact head
        │
        ▼
pnpm run change-scope --base <base> --head <head>
  → 只输出 base/head/merge-base + committed/staged/unstaged/untracked 路径
        │
        ▼
按改动路径映射 owning docs（AGENTS.md / defensive-patterns / 子系统文档）
        │
        ▼
先读要求，再看 diff；读 diff + 足够上下文
        │
        ▼
输出 GitHub review comment：
  - 每条：defect / location / impact / evidence
  - 能落行就 inline，跨模块放 PR 级
  - blockers vs suggestions；已被绿闸覆盖的不报
        │
        ▼
作者逐条 verify：fix 或 rebut
        │
        ▼
weighted approval（2 分）
  - 作者自己的 review 不算
  - 作者历史 merged PR 信用分另算
  - reviewer 按改动生产行的 blame ownership 加权
  - CHANGES_REQUESTED 保持 pending
        │
        ▼
merge
```

#### C. 我们建议的目标时序（把两者拼起来）

```
交付前 / 用户触发 review
        │
        ▼
review_scope（确定性，优先自建）
  - git status / git diff --name-only
  - base / head / merge-base
  - committed / staged / unstaged / untracked
  - 只输出路径，不做语义判断
        │
        ▼
构造 reviewer 输入（固定、可复现）
  - goal_spec.objective / done_when / constraints / out_of_scope
  - plan_steps（预期改哪些文件）
  - diff + 每个改动文件的完整内容
  - 相关 AGENTS.md / 模块约定
        │
        ▼
独立 reviewer（不同 system prompt + 只读工具）
  - 不继承执行模型的完整对话
  - 先读要求，再看 diff
  - 输出 report_findings：
      file / line / summary / failure_scenario /
      severity(blocking|suggestion|optional) /
      verdict(CONFIRMED|PLAUSIBLE) / outcome
        │
        ▼
verifier 汇总
  - blocking finding → verdict = failed
  - suggestion → findings，不阻断
  - unknown / 未验证 → INCOMPLETE，不判 FAIL
        │
        ▼
有限轮修复（共用 repair_rounds 预算）
        │
        ▼
交付
```

---

## 6. 落地清单（只列动作，不列工期）

### P0（直接对应“没有独立审查 / 完成判据不可执行”）

1. **`done_when` 编译成可执行 checks。**
   - `services/tools/plan.py:298-325`：`done_when` 的 JSON schema 从 `string[]` 改成 `(string | {text, check})[]`。
   - `services/plan.py:normalize_goal`：兼容旧 string，统一成 `{text, check}`。
   - `services/agent/verifier.py:293-310`：逐条执行 `check`，输出 `done_when: [{text, status, evidence}]`；`unknown` 不阻断。
   - `services/agent/verifier.py:18` 的 `CHECKS` 加 `done_when_checks` 或复用 `goal_met`。

2. **`exit_plan_mode` 三结局。**
   - `services/tools/plan.py:172`：`_exit_plan_mode` 增加 `revision_requested` 失败返回；保持 `mode=plan`；把用户反馈放进 `output`。
   - 审批卡：拒绝时透传 `reason`；关闭卡片时只结束本轮，不退出 plan mode。
   - 与 DSH `PLAN_REVISION_REQUESTED` 的语义对齐（失败工具调用而不是普通用户消息）。

3. **审查先取确定性 scope。**
   - `services/tools/review.py` 新增 `review_scope` 工具（或 `graph.py` 的 review 前置节点），输出 `base/head/merge-base + 四组路径`。
   - `services/agent/graph.py:6272`：`review_result_node` 的输入改为“scope + diff + 全文件 + goal_spec”，不再直接看完整消息流。
   - reviewer 输出走现有 `report_findings`（`services/tools/review.py:125`），不要另造 schema。

### P1（让审查结果真正影响 verdict）

4. **blocking findings 进 verifier。**
   - `services/tools/review.py` 的 finding schema 增加 `severity: "blocking" | "suggestion" | "optional"`；**不要复用 `level`**（`level` 是审查投入档位）。
   - `services/agent/verifier.py` 新增输入 `review_findings`；`severity=blocking` 且 `verdict=CONFIRMED` → `blocking=True`；`suggestion/optional` 只进 `findings`。
   - `review_result_node` 不再直接改 `status`；它只产出 findings，由 verifier 决定 `failed/incomplete`。

5. **目标漂移 check。**
   - `services/agent/verifier.py` 的 `CHECKS` 加 `scope_drift`：`out_of_scope` 编译成路径/命令模式；命中记 finding；`changed_paths` 与 `plan_steps.expected_files` 比对，计划外改动记 finding。
   - `services/agent/graph.py` 在每轮/压缩后注入 `objective + out_of_scope` 提醒（如果还没有）。

6. **审查技能补两条规则。**
   - `skills/code-review/SKILL.md`：①按路径找 owning docs；②不确定写“未验证/不确定”，不要冒充确定 bug；③已被绿闸覆盖的不报。
   - 与 `report_findings` 的 `verdict=PLAUSIBLE` / `outcome` 对齐。

### P2（体验与治理，可选）

7. **计划状态条。**
   - 前端任务页显示 `objective` 摘要 + 状态 + `verdict.reason`；不要显示 judge 内部推理（opencode 也没显示）。
   - 后端只暴露 `goal_spec` + `verdict.reason`，不新增敏感字段。

8. **多 agent 作者标记。**
   - 如果以后做多 agent 审查，给 `report_findings` 加 `author_agent_id`，汇总时排除“作者自审”；不要抄 DSH 的 2 分/delegate/blame 加权，那是 GitHub 治理。

---

## 7. 查不到 / 没有公开实现

这一节刻意留白，避免把“没找到”写成“它这么做”。

| # | 问题 | 状态 |
|---|---|---|
| 1 | opencode 有没有结构化 plan 对象 / plan 审批 | **确认没有**。`plan.ts` 只有 agent + 权限 + 提醒；没有 `exit_plan_mode`、没有 plan 存储、没有 approve。计划正文在对话里，可选写 `~/.opencode/plan` `[OC-P1]`。 |
| 2 | opencode 有没有 `done_when` / constraints / out_of_scope | **确认没有**。`Goal.State` 只有 `objective`（≤4000）+ 状态 + 轮次/停顿 `[OC-G1]`。 |
| 3 | opencode 有没有目标漂移检测 | **没有专门的 detector**。只有每轮重述 objective、压缩后 reminder、idle 时 judge 重判 `[OC-G2][OC-G5]`。 |
| 4 | opencode `/review` 有没有结构化输出 / 阻断 / 自动修复 | **确认没有**。`review.txt` 只要求自然语言 + 严重度 + 场景；`command.ts` 只是注入 prompt；没有 JSON、没有 gate、没有 repair loop `[OC-R1][OC-R2]`。 |
| 5 | opencode 审查是否区分作者 | **不区分**；只区分“未提交 / commit / branch / PR”范围 `[OC-R2]`。 |
| 6 | DSH 有没有独立 goal evaluator | **确认没有**。README 原文 `No independent evaluator — the caller that records completion or blocking is authoritative` `[DSH-G1]`。 |
| 7 | DSH 有没有 `done_when` / 完成判据字段 | **确认没有**。`GoalSnapshot` 只有 `objective` + `phase` + `blockedReason` + `maxGoalRounds` `[DSH-G1]`。 |
| 8 | DSH 有没有目标漂移检测 | **没有专门的 detector**。只有每轮重述 objective + revision/round 一致性 `[DSH-G6]`。 |
| 9 | DSH 有没有运行时的通用 code-review 工具 / `/review` 命令 | **未找到**。master 全树 22,247 个路径里，review 相关只有 `.agents/skills/dsh-code-review`（仓库级技能）、`experimental/auto-review`（权限审查）、Forge PR UI、`scripts/change-scope.ts` 与审批治理。没有 `code_review` 工具、没有 review 节点。 |
| 10 | DSH `dsh-code-review` 是否自动修复 | **没有**。技能只输出 review comment；作者 fix 或 rebut；没有自动改代码 `[DSH-R1]`。 |
| 11 | DSH `dsh-code-review` 的维护工具是否在仓库里 | **不在**。`docs/cookbook/maintaining-dsh-code-review.md` 明确说维护工具是维护者的私有基础设施，仓库只记录工作流保证 `[DSH-R5]`。 |
| 12 | DSH weighted approval 的完整实现细节 | 只能读到 README + `approval-policy.json` + Python 脚本；`README` 有截断，具体的 GraphQL 分页/错误处理未逐行读完。结论“2 分 + 作者自审不算 + blame ownership 加权”是 README 事实；更细的边界未验证。 |
| 13 | opencode judge 是否换模型 / 换 effort | **未公开**。代码只看到 `ctx.session.generate({sessionID, prompt})`；没有显式 model/effort。判断：角色分离，不保证模型分离 `[OC-G2]`。 |
| 14 | opencode plan mode 是否在 UI 有专用计划面板 | **未找到**。只有 agent 切换和 todo 侧边栏；计划正文在聊天里 `[OC-P1]`。 |
| 15 | 两家有没有“审查自己的 diff 再交付”的内置流水线 | **都没有**。opencode 是用户主动 `/review`；DSH 是仓库 PR 流程；运行时都没有“交付前自动审 diff”的节点。 |
| 16 | 我们 `review_result_node` 是否已接入独立模型 | **没有**。`services/agent/graph.py:6272` 用的是 `session.ask(state, prompt, node)`，同一模型、同一会话上下文；这是“第二意见”，不是独立审查（代码注释也这么写）。 |

---

## 8. 来源

### 8.1 opencode（`anomalyco/opencode@d33b578`）

- 计划插件：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/plan.ts
- 目标插件：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/goal.ts
- 目标 schema：https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/goal.ts
- 目标 judge 提示词：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/goal/judge.txt
- 目标 kickoff / continuation / reminder：同目录 `kickoff.txt`、`continuation.txt`、`reminder.txt`
- todo 工具：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/plugin/todo.ts
- todo 描述：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/todowrite.txt
- todo 读取：https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/util/todo.ts
- todo 客户端读取：https://github.com/anomalyco/opencode/blob/dev/packages/app/src/session/composer/todos.ts
- todo 侧边栏：https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/feature-plugins/sidebar/todo.tsx
- agent 定义（Build / Plan）：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/agent.ts
- `/review` 命令注册：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/command.ts
- `/review` 提示词：https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/command/review.txt
- 目标 UI：https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/feature-plugins/prompt/goal.tsx
- 目标 UI 文案：https://github.com/anomalyco/opencode/blob/dev/packages/gui-plugins/src/goal/i18n/en.ts
- agent 默认权限：https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/agent.ts

### 8.2 DeepSeek Harness（`deepseek-ai/deepseek-harness@2e39e704`）

- 计划模式包：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/plan/plan-mode/README.md
- 计划模式源码：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/plan/plan-mode/src/index.ts
- 计划子系统：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/plan.md
- 目标服务：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/goal/goal/README.md
- 目标类型：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/goal/goal/src/types.ts
- 目标 fold：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/goal/goal/src/fold.ts
- 目标子系统：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/goal.md
- 目标工具：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/goal/tool-goal/README.md
- 目标续跑：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/goal/goal-round-driver/README.md
- 目标续跑提示词：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/goal/goal-round-driver/src/prompt.ts
- 目标子系统（类型/API）：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/goal
- todo 源码：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/todo/tool-todo/src/index.ts
- todo 子系统：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/todo.md
- 代码审查技能：https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/skills/dsh-code-review/SKILL.md
- 审查范围脚本：https://github.com/deepseek-ai/deepseek-harness/blob/master/scripts/change-scope.ts
- PR 审批治理：https://github.com/deepseek-ai/deepseek-harness/blob/master/.github/review-ownership/README.md
- 审批策略：https://github.com/deepseek-ai/deepseek-harness/blob/master/.github/review-ownership/approval-policy.json
- 工具调用 auto review：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/experimental/auto-review/README.md
- auto review 设计说明：https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/feature/2026-08-28-auto-review.md
- 维护 code-review 技能：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/maintaining-dsh-code-review.md

### 8.3 我们的基线（Kbgai）

- 计划 / 目标工具：`services/api/src/blank_api/services/tools/plan.py`
- 计划数据模型：`services/api/src/blank_api/services/plan.py`
- 计划模式协议：`services/api/src/blank_api/services/plan_mode.py`
- 权限与 plan 硬闸：`services/api/src/blank_api/services/permissions.py`
- 事实验证器：`services/api/src/blank_api/services/agent/verifier.py`
- 交付前同模型再审：`services/api/src/blank_api/services/agent/graph.py:6272`
- 审查发现工具：`services/api/src/blank_api/services/tools/review.py`
- 代码审查技能：`services/api/src/blank_api/skills/code-review/SKILL.md`
- 工具安全审查：`services/api/src/blank_api/services/guardian.py`
- 长期目标模型：`services/api/src/blank_api/models/goal.py`
- 会话目标提案：`services/api/src/blank_api/services/tools/goals.py`

### 8.4 来源标签 → 文件

| 标签 | 文件 |
|---|---|
| `[OC-P1]` | `packages/core/src/plugin/plan.ts` |
| `[OC-P2]` | `packages/core/src/tool/plugin/todo.ts` + `packages/core/src/tool/todowrite.txt` |
| `[OC-P3]` | `packages/tui/src/util/todo.ts` + `packages/app/src/session/composer/todos.ts` |
| `[OC-P4]` | `packages/tui/src/feature-plugins/sidebar/todo.tsx` |
| `[OC-P5]` | `packages/core/src/plugin/agent.ts`（`Build` / `Plan` 定义） |
| `[OC-G1]` | `packages/schema/src/goal.ts` |
| `[OC-G2]` | `packages/core/src/plugin/goal.ts` + `packages/core/src/plugin/goal/judge.txt` |
| `[OC-G4]` | `packages/core/src/plugin/goal/kickoff.txt` + `continuation.txt` |
| `[OC-G5]` | `packages/core/src/plugin/goal/reminder.txt` + `goal.ts` 的 `context` hook |
| `[OC-G6]` | `packages/core/src/plugin/goal.ts`（`MAX_ROUNDS / MAX_STALLS / WAIT_LIMIT`） |
| `[OC-G7]` | `packages/core/src/plugin/goal.ts` 的 `/goal resume` 分支 |
| `[OC-G8]` | `packages/tui/src/feature-plugins/prompt/goal.tsx` |
| `[OC-R1]` | `packages/core/src/plugin/command.ts` |
| `[OC-R2]` | `packages/core/src/plugin/command/review.txt` |
| `[DSH-P1]` | `docs/subsystems/plan.md` |
| `[DSH-P2]` | `packages/plan/plan-mode/README.md` |
| `[DSH-P3]` | `packages/plan/plan-mode/src/index.ts` |
| `[DSH-P4]` | `docs/subsystems/todo.md` |
| `[DSH-P5]` | `packages/todo/tool-todo/src/index.ts` |
| `[DSH-G1]` | `packages/goal/goal/README.md` |
| `[DSH-G2]` | `docs/subsystems/goal.md` + `packages/goal/goal/src/fold.ts` |
| `[DSH-G3]` | `packages/goal/tool-goal/README.md` |
| `[DSH-G6]` | `packages/goal/goal-round-driver/src/prompt.ts` |
| `[DSH-G7]` | `packages/goal/goal-round-driver/README.md` |
| `[DSH-R1]` | `.agents/skills/dsh-code-review/SKILL.md` |
| `[DSH-R2]` | `scripts/change-scope.ts` |
| `[DSH-R3]` | `.github/review-ownership/README.md` + `approval-policy.json` |
| `[DSH-R4]` | `.agents/notes/implemented/feature/2026-08-28-auto-review.md` + `packages/experimental/auto-review/README.md` |
| `[DSH-R5]` | `docs/cookbook/maintaining-dsh-code-review.md` |
| `[KB-P1]` | `services/api/src/blank_api/services/tools/plan.py` + `services/plan.py` |
| `[KB-V1]` | `services/api/src/blank_api/services/agent/verifier.py` |
