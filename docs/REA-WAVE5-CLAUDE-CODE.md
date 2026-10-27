# REA 第五波：Claude Code 2.1.271 拆解与可借鉴清单

日期：2026-10-27
拆解对象：本机安装的 Claude Code **2.1.271**
（`E:/env/npm-global/node_modules/@anthropic-ai/claude-code`，`bin/claude.exe` 229 MB，
Bun 编译单文件；`dependencies` 为空，平台包走 `optionalDependencies`）。
方法：只认实物。① 包内 `sdk-tools.d.ts`（168 KB，工具入参/出参的权威定义）；
② 从上一步二进制提取的字符串产物 `E:/env/rea/out/claude.uniq.txt`（318,134 条去重串）；
③ 本机配置 `E:\env\claude\home\settings.local.json` 里的权限写法。
凡本文引用的字符串都带产物行号，可用 §11 的命令复读；读不出来的结论不写。
**配置里的 `ANTHROPIC_AUTH_TOKEN` 属于凭据，本文不复制、不落盘。**

## 0. 本轮取证清单

| 实物 | 形状 | 用途 |
| --- | --- | --- |
| `package.json` | `version` 2.1.271，`bin` 指向 `claude.exe` | 版本与分发形态 |
| `sdk-tools.d.ts` | 168 KB，44 个工具定义（上波统计） | 子代理/计划/后台任务的真实入参 |
| `claude.uniq.txt` | 318,134 条 | hooks 契约、记忆、回退、沙箱规则 |
| `settings.local.json` | `permissions.allow` 里是 `PowerShell(git *)` 这种写法 | 权限规则语法 |
| `claude.exe` | 229 MB | 证明已是编译型单文件分发 |

## 1. 子代理：后台优先、可寻址、可停、可隔离

`sdk-tools.d.ts` 的 `AgentInput`（第 692 行起）给出的不是"调一次等一次"：

- `run_in_background?: boolean` —— 注释原文：
  `Agents run in the background by default; you will be notified when one completes.`
- `name?: string` —— 起的代理**可以被点名**（`SendMessage({to: name})`）。
- `isolation?: "worktree" | "remote"` —— 本机 worktree，或远端云环境。
- `TaskOutputInput`（第 764 行）：`task_id` + `block` + `timeout` —— 轮询/阻塞取结果。
- `TaskStop`：按 id 停。
- `Tool` 字符串里还有背景说明（`claude.uniq.txt` 132681）：
  `If true, hook runs in background and wakes the model on exit code 2 (blocking error). Implies async.`

对照我们：`delegate.py` 的 `spawn_agent` 是 `result = await runner(request)`（第 84 行），
`spawn_agents` 是 `runner.fan_out(...)`（第 116 行）——**并发有了，句柄没有**。
子代理跑多久，主代理的工具调用就等多久；期间既不能查询、也不能停，更不能按名字再递话。

**可借**：把 `spawn_agent` 的返回从"结论"改成"句柄"（`agent_id`/`status`/`output`），
主代理用 `task_output(block,timeout)` 取；`task_stop` 杀。
**不借**："默认后台"。我们的用量账与轮归属（`parallel_group`、批号）是按一次工具调用结算的，
默认后台会把这笔账摊到不确定的轮次上，先保持"显式 `background=true` 才后台"。

## 2. Hooks：我们只实现了"能否决"，他们实现了"能改写、能提问、能补上下文"

我们的现状（`services/hooks.py`）：6 个事件，只有 `pre_tool_use` 能阻塞，
stdout 只解析 `decision/reason`；`graph.py:1885` 拿它决定 refuse/execute。

Claude 的契约有四个我们缺的通道：

1. **三种决定**：`permissionDecision` 取 `allow | deny | ask`
   （`claude.uniq.txt` 35743/35748）；`ask` 会把决定权交回用户，而不是二选一。
2. **改写入参**：`updatedInput`（35745/35749），并且会做 schema 校验
   （58373：`returned updatedInput that failed schema validation`）。
3. **补上下文**：`additionalContext`（67704/68366），只进模型上下文，不改工具参数。
4. **失败也有事件**：`PostToolUseFailure`、`PostToolBatch`、`PermissionDenied`（可带
   `retry: true` 让模型重试，151644）、`UserPromptExpansion`、`TaskCompleted`、`TeammateIdle`
   （67918/67992/130561）；`WorktreeCreate`/`WorktreeRemove` 甚至能让非 git 仓库也获得隔离
   （56175/130586）。

还有两条工程约束值得抄：

- **退出码 2 = 阻塞**，且输出处理不了时仍然按阻塞算
  （67918：`blocked (exit 2); its output could not be processed`）。
- **受限会话不认钩子的 allow**（57826）：
  `permissionDecision=allow ignored: a confined session takes grants only from its command line`。
  这正好防住"钩子把危险命令放行"。

**可借（按性价比）**：
(a) `ask` 进等待审批；(b) `updatedInput` 只能**收紧**（改路径到工作区内、去掉危险尾巴），
不能借它把命令放宽；(c) `PostToolUseFailure` 单独成事件；(d) 钩子返回的 `allow`
**不跳过我们自己的 `PermissionPolicy.decide`**——与 57826 同一条原则。

## 3. 权限规则：可手写、能否决、坏了要报错

Claude 的规则是 `Tool(spec)` 语法，本机 `settings.local.json` 实际写着
`PowerShell(git *)`、`PowerShell(mvn *)`、`PowerShell(python --version)`。
产物里能读到：

- 前缀通配：`Bash(git *)`（12015），`Bash(git status *)`（50193）——空格本身是词边界；
- 规则坏了会明说（67670）：
  `must be a '<tool>(<command or prefix>)' permission rule (tool name case-sensitive, …)`；
- 团队模式下只认托管设置（59042）：
  `permission rules are restricted to managed settings (allowManagedPermissionRulesOnly)`；
- 子代理类型也能被规则否决（67575）：`Agent type '…' has been denied by permission rule`。

我们的现状（`services/approval_rules.py`）：只落 `run_shell` 的 **token 前缀**，
而且只能由"用户批准过"产生；没有手工规则文件、没有 deny、没有 `tool(spec)` 表达式。
有一点我们**已经比它严**：`covered_by_rules` 对 `&&` 串联逐段覆盖，
`git status && rm x` 不会因为前一段命中前缀就整条放行（`test_permission_modes_v11.py` 已钉）。

**可借**：在 `approvals.json` 旁加一层手写规则（`allow`/`deny`，`deny` 优先），
语法收窄成 `run_shell(<token 前缀> *)` 即可，**继续保留逐段覆盖语义**。
格式错误必须报错并停用该文件（fail-closed），不能静默丢掉——静默丢规则会让人以为还启着。

## 4. 记忆：类型化、有索引、引用可追溯

Claude 的记忆分类原文（67911 / 187705）：

> `user`: who the user is (role, expertise, preferences).
> `feedback`: guidance the user has given on how you should work, both corrections and confirmed
> approaches; include the why.
> `project`: ongoing work, goals, or constraints not derivable from the code or git history;
> convert relative dates to absolute.
> `reference`: pointers to external resources (URLs, dashboards, tickets).

配套还有：`MEMORY.md` 作为索引（183896 把 `MEMORY.md` 排除在"自动索引"之外，避免索引索引自己）、
上限 2000 文件 / 1 MB、`[[name]]` 互链，以及回复里用
`<cc-memory filenames="…">句子</cc-memory>` 标注"这句话来自哪条记忆"（52200/67911）。
**这条引用机制的价值不在好看**：用户能当场核对"你凭什么这么说"。

我们的现状（`services/memory.py`）：工作区一份 markdown，按"任务结论/交接笔记/自报验收/验证命令"
分节，确定性归纳、按 bullet 去重、12,000 字符上限。没有 user/feedback/project/reference 分类，
没有索引文件，也没有"这条结论来自哪次任务"的引用标记。

**可借**：把现有分节映射成四类（`feedback` 对应"用户纠正/确认过的做法"，
`project` 对应任务结论，`reference` 对应验证命令里出现的外部入口），
并在检索结果里带 `source_task`/`source_event`，前端可点回原任务。
这是低成本项：写入路径已经是确定性的，不需要模型参与。

## 5. 回退：他们是"每轮可退 + 跳过要说清"，我们是"每任务一份 + 失败 500"

Claude 的回退有三种范围（code / conversation / both），且**跳过的文件要报出来**（68107）：

> `Restored the code, but skipped ${Ri} file(s): … Skipped files were left untouched — run with --debug for the paths.`

我们的现状：

- `SnapshotStore.capture()`（`sandbox/snapshot.py:56`）在 `create_plan` 节点动手前拍**一次**，
  整个任务复用 `ensure_snapshot()`（`graph.py:390`）；
- 工作区过大/扫描超预算时 `copied=false`，只在 `meta.json` 里写 `skipped_reason`（`snapshot.py:96`）；
- 但恢复路由（`api/routes/tasks.py:260`）直接 `store.restore(...)`，而 `restore()`
  在 `tree/` 不存在时 `raise FileNotFoundError`（`snapshot.py:148`）——**未捕获，返回 500**。
  也就是说：快照明明拍了"只记录清单、没复制"，用户点恢复得到的是服务器错误，
  而不是"这份快照恢复不了，原因是……"。

**可借**：
(a) 恢复接口先看 `copied`：不能恢复就 409 + `skipped_reason`，并在列表里把该快照标成"仅清单"；
(b) 恢复响应带 `restored` 与 `skipped` 两个清单，事件里也分开记；
(c) 每轮回退点（P2）：在对话消息上标 checkpoint，回退时同时说明"代码回到第 N 轮、
对话回到第 M 轮"，两者不假装原子。

## 6. 沙箱：允许单次越界，但要有显式开关和审计

Claude 的 Bash 工具带 `dangerouslyDisableSandbox`（`sdk-tools.d.ts` BashInput；产物 79137），
配置侧写着（102326）：

> `Allow commands to run outside the sandbox via the dangerouslyDisableSandbox parameter.
> When false, the dangerouslyDisableSandbox parameter is completely ignored and all commands
> must run sandboxed. Default: true.`

另外还有 `autoAllowBashIfSandboxed`（104027：沙箱内的命令自动放行）与企业策略
"要求沙箱但本机不可用 ⇒ 直接拒绝"（上一波已记录）。

我们的现状：沙箱档位在任务级解析（`tools/shell.py:239` 的 `resolve_tier`），
**没有单次调用越界的入口**。好处是不会被模型一句话绕过；坏处是遇到"某条命令在 Job 里起不来"
只能整任务降档或人工处理。

**可借**：`run_shell(sandbox="host")` 这种单次参数，但**默认关闭**、必须走审批卡、
事件里落 `sandbox_bypass=true` 与原因，且 `blank doctor --sandbox` 能列出本次任务用过几次。
**不借默认值 true**——Claude 的默认值是为交互式 CLI 设计的，我们的桌面端默认应是"不放行"。

## 7. 计划模式：模型能主动进出，我们只能人切

Claude 的 `EnterPlanMode`/`ExitPlanMode` 是模型可调用的工具；
`ExitPlanModeOutput`（`sdk-tools.d.ts` 第 3305 行）包含 `plan`、`filePath`、
`planWasEdited`、`awaitingLeaderApproval`、`requestId`——**计划是一份可被审阅、可被改的文件**，
批准后才离开计划模式。

我们的现状：`permissions.py:30` 有 `MODE_PLAN`，`decide()` 在计划模式下放行只读/计划类工具
（721-724），`update_plan` 让计划可增删改（`tools/plan.py`）。但**模式切换权在人**：
模型不能"我调研完了，申请进入执行"，也不能"这活儿比预想大，申请退回计划"。

**可借**：两个工具（`propose_plan` / `request_execution`）→ 生成审批卡，卡面就是计划正文；
批准后切换模式并落事件。这比让用户去设置页拨开关自然得多，也正好承接我们已有的
"执行中插话并入计划"。

## 8. 其他可借项（低优先，但都是真实现）

| 项 | 证据 | 说明 |
| --- | --- | --- |
| `WorktreeCreate`/`WorktreeRemove` 钩子 | 56175、130586 | 非 git 仓库也能靠用户钩子做隔离；我们的 `worktree.py` 现在是"git 不行就整树复制" |
| 异步钩子 + `asyncRewake` | 132681、184549 | 后台钩子退出码 2 时唤醒模型；适合慢检查（lint/测试） |
| `UserPromptExpansion` + `suppressOriginalPrompt` | 53224、68366 | 提交前改写/压缩提示词，原文可不进上下文 |
| `SessionStart` 可给 `initialUserMessage`/`sessionTitle` | 68366 | 会话开场注入首条消息与标题 |
| `CronCreate`/`ScheduleWakeup`（最短 1 小时） | `sdk-tools.d.ts` 2837/2862 | 定时任务；桌面端暂不需要 |
| `ProposeSkills`/`ProposeGoal` | `sdk-tools.d.ts` 2928 | 代理主动提议技能/目标；我们已有 skills 路由，但模型不能提议 |

## 9. 不建议照搬

1. **钩子 `allow` 直接放行**：我们保持"钩子只能收紧"。57826 的受限会话语义证明连 Anthropic
   自己也不敢让钩子越过沙箱授权。
2. **子代理默认后台**：用量与轮归属会漂；先做显式后台。
3. **`dangerouslyDisableSandbox` 默认 true**：桌面产品默认关闭。
4. **44 个工具的广度**：我们靠档位裁剪（`tiered_tools`）控制面宽，不靠堆工具。
5. **macOS/Linux 沙箱技术**（sandbox-exec / bubblewrap）：本机是 Windows，
   我们已有 Job Object + `srt-win` 的对照结论（见 `REA-WAVE4-CODEX-RUNTIME.md`）。

## 10. 优先级与"能失败的判据"

> 每条判据都写成"现在跑会红、实现后才会绿"的形状；只写"应该有"不算判据。

### P0

| # | 项 | 能失败的判据 | 成本/风险 |
| --- | --- | --- | --- |
| 1 | 钩子三种决定 + `updatedInput` + `additionalContext` | 配一个钩子把 `echo A` 改写成 `echo B`：工具输出必须是 `B`，事件 `hook.completed` 里 `arguments.command` 也是 `B`；当前实现会输出 `A`。再配一个返回 `ask` 的钩子：任务必须进 `waiting_approval`，当前会直接执行 | 中/低：只加字段，`decide()` 仍是最终闸门 |
| 2 | `PostToolUseFailure` 事件 | 让 `run_shell` 失败（非零退出）：必须出现一条 `hook.completed{event:"post_tool_use_failure"}`；当前 `graph.py:1986` 只有 `post_tool_use`，该事件数为 0 | 低/低 |
| 3 | 手写 `deny` 规则 + `tool(spec)` 语法 | 写 `deny: run_shell(rm *)` 后，即使已批准过 `rm` 前缀，`rm notes.txt` 也必须被拒并给出规则来源；当前 `approval_rules.py` 没有 deny，会放行。再写一条坏规则：加载必须报错并整体停用（fail-closed），不能静默忽略 | 中/中：解析器要防宽 |
| 4 | 恢复快照不再 500 | 构造 `copied=false` 的快照（工作区超预算）→ `POST /tasks/{id}/snapshots/{sid}/restore` 必须返回 409 且带 `skipped_reason`；当前 `snapshot.py:148` 抛 `FileNotFoundError`，路由未捕获 → 500 | 低/低 |

### P1

| # | 项 | 能失败的判据 | 成本/风险 |
| --- | --- | --- | --- |
| 5 | 子代理句柄（`agent_id` + `task_output` + `task_stop`） | `spawn_agent(background=true)` 必须在 1 秒内返回句柄，主代理还能继续发下一条工具调用；`task_output(block=true)` 能拿到最终结论；`task_stop` 后状态为 `cancelled`。当前调用会阻塞到子代理跑完 | 高/中：要处理用量归属与取消传播 |
| 6 | 计划进出由模型发起 | 在 `default` 模式下让模型调用 `propose_plan`：必须进 `waiting_approval`，卡面含计划正文；批准后模式变 `plan`。当前模型没有这个入口 | 中/中：模式切换要留审计 |
| 7 | 记忆类型 + 来源引用 | 写入一条 `feedback` 记忆后，`memory_search(type="feedback")` 能命中、别的类型不返回；检索结果含 `source_task`，前端可跳回原任务。当前 `memory.py` 无类型字段 | 中/低 |
| 8 | 单次越界沙箱（默认关） | 开关关闭时 `run_shell(sandbox="host")` 必须 400/拒绝且不执行；打开时必须出审批卡，事件含 `sandbox_bypass=true`，`doctor --sandbox` 计数 +1。当前无此参数，传了会被忽略并照常执行（更糟） | 中/高：安全边界，默认必须关。**已落地，见 §13** |

### P2

| # | 项 | 能失败的判据 | 成本/风险 |
| --- | --- | --- | --- |
| 9 | 每轮回退点（代码+对话） | 两轮之后回退到第 1 轮：对话消息截断到该轮，代码恢复，响应分别给出 `restored`/`skipped`；当前只有任务级快照 | 高/中 |
| 10 | `WorktreeCreate` 类钩子 | 在非 git 目录配置该钩子后，子代理隔离必须用钩子返回的路径；当前 `worktree.py` 只会整树复制 | 低/低 |
| 11 | 异步钩子唤醒 | 后台钩子退出码 2 时模型收到一条系统提醒并继续；当前钩子是同步等待 | 中/中 |

## 11. 复现命令

```bash
# 版本与分发形态
cat E:/env/npm-global/node_modules/@anthropic-ai/claude-code/package.json

# 字符串产物规模
wc -l E:/env/rea/out/claude.uniq.txt

# 逐条复读本文引用的字符串（window.py 见 E:/tmp/rea-claude/window.py）
PYTHONIOENCODING=utf-8 E:/env/python-e2e/Scripts/python.exe -X utf8 \
  E:/tmp/rea-claude/window.py 'permissionDecision' 60 160 10
PYTHONIOENCODING=utf-8 E:/env/python-e2e/Scripts/python.exe -X utf8 \
  E:/tmp/rea-claude/window.py 'updatedInput' 50 140 10
PYTHONIOENCODING=utf-8 E:/env/python-e2e/Scripts/python.exe -X utf8 \
  E:/tmp/rea-claude/window.py 'Restored the code' 120 300 4
PYTHONIOENCODING=utf-8 E:/env/python-e2e/Scripts/python.exe -X utf8 \
  E:/tmp/rea-claude/window.py 'dangerouslyDisableSandbox' 60 200 10

# 工具入参的权威定义
grep -n 'interface AgentInput' -A 40 \
  E:/env/npm-global/node_modules/@anthropic-ai/claude-code/sdk-tools.d.ts
```

## 12. 本轮没有验证的

- `isolation: "remote"` 的云端执行行为（本机只有字符串，没有云端账号实测）。
- 企业托管规则 `allowManagedPermissionRulesOnly` 的实际下发路径（只读到报错文案）。
- Windows `srt-win` 的 WFP 过滤器行为（本轮只读字符串，未安装该组件；已有的
  `REA-WAVE4-CODEX-RUNTIME.md` 是对 Codex 沙箱的实测，不可移花接木）。
- `asyncRewake` 在真实模型下的唤醒时序（没有构造付费调用）。#119 用真实子
  进程钩子 + FakeAdapter 验证了「唤醒进下一次请求」，但没有验证真实模型收到
  提醒后的续跑行为。

## 13. 本批落地：#110–#119

> 每一条都按「先写能失败的判据，再实现」落；这里记形状与实测读数，
> 不复述代码。撤掉实现后测试必须红，红集合都是实测出来的，不写宽。

### #115 单次越界沙箱（P1 第 8 项）

**形状**：`run_shell(sandbox="host")` 是一次**请求**，不是一次授权。

- 部署开关 `BLANK_SANDBOX_ALLOW_HOST_BYPASS` 默认关。关闭时 `decide()` 直接给
  `sandbox:host_bypass_disabled`，连审批卡都不得出现——否则用户点了允许、命令仍不执行，
  卡片本身就在撒谎。打开后 DEFAULT / ACCEPT_EDITS / BYPASS / `approve_high_risk`
  一律只能 ASK（`sandbox:host_bypass`），计划模式仍按 `mode:plan` 拒绝。
  会话前缀、落盘前缀、工具档、手写 allow、guardian 都不能代批；卡片只给“仅本次”。
- 批准用掉即销账：handler 在消费时写 `approval.consumed`（只带
  `tool` / 真实 `approval_key` / `reason`，不带命令正文），
  `AgentRuntime._rehydrate` 把 `approval.granted` / `approval.consumed` /
  `approval.denied` 按 seq 一起回放。只回放 granted 会让同一枚 key 在每次 resume
  时复活；实测症状是「批准一次、执行两次」，用户的驳回被回放掉的批准顶掉。
- 执行层只对“确实有沙箱可绕”的档位记 `sandbox_bypass=true`；off 档与部署关闭时
  仍写「本就在宿主上执行」，不借参数造一次假越界。越界执行 `sandbox_backend=null`、
  enforcement 的限额全 false、`host_execution`/`sandbox_bypassed` 为 true。
- 证据与 doctor：`boundaries.sandbox_bypass = {calls, tool_call_ids}`，
  P-AUDIT 念「单次越界宿主执行 N 次」；缺键读成「未记」，坏形状读成 UNKNOWN，
  都不默认成 0。前端把 `bypassed` 单独成一种宿主执行文案，不并进关档。

**判据**：`services/api/tests/test_sandbox_bypass_v115.py`（27 条，含 e2e
「批准一次 → 执行一次 → 第二次重新挂卡 → 驳回 → 证据包 calls=1」）；
前端 `src/components/tasks/timeline.test.ts` 三条。

**变异实测**：
- `_rehydrate` 忽略 `approval.consumed` → 红 3 条（2 条单元 + 1 条 e2e）；
- handler 不写消费事件 → 红 2 条（handler 单元 + e2e）。

### #110–#114（判据在测试模块里）

这五条的形状与判据已经写在各自测试模块的 docstring 与用例名中，本批不再复述：

| # | 项 | 判据工件 |
| --- | --- | --- |
| #110 | 快照不可恢复时返回 409 + `skipped_reason`，列表标出「仅清单」 | `services/api/tests/test_snapshot_restore_v110.py` |
| #111 | 钩子契约 `ask` / `updatedInput` / `additionalContext` / `PostToolUseFailure` | `services/api/tests/test_hook_contract_v111.py` |
| #112 | 手写 `allow/deny` + `tool(spec)`，坏规则 fail-closed | `services/api/tests/test_permission_rules_v112.py` |
| #113 | 模型发起 `enter/exit_plan_mode`，模式切换留审计 | `services/api/tests/test_plan_mode_v113.py` |
| #114 | 记忆类型化与来源引用 | `services/api/tests/test_memory_types_v114.py` |

### #116 后台子代理句柄（P1 第 5 项）

**形状**：`spawn_agent(background=true)` 只返回句柄，不阻塞主代理。

- 句柄登记在本任务的运行域里（`_BACKGROUND`），`agent_id` 是唯一寻址方式；
  未知 id 明确报错，不猜、不复用别人的结果。
- `task_output(block=true, timeout=…)` 只等结果，**超时不取消子代理**——等待和
  终止是两件事，混在一起会让一次查询把正在干的活杀掉。
- `task_stop` 取消子代理并删掉它的隔离副本；任务收尾时统一取消残留句柄并
  在事件里报告数量。`delegate` 的返回里给出 `agent_id`，网关同时暴露
  `task_output` / `task_stop` 两个工具，模型不必知道内部注册表。
- 子代理用量仍计入同一套账（#33 的口径不变），后台化不产生第二本账。

**判据**：`services/api/tests/test_background_subagent_v116.py` 6 条——
1 秒内返回句柄、`task_output` 超时不动子代理、`task_stop` 取消并清副本、
未知 id 不猜、收尾取消残留、网关真的暴露三个入口。

**变异实测**：逐处撤掉实现后，红集合为 5 / 1 / 2 / 1 / 1 条（阻塞返回、超时
误杀、停止不清副本、收尾不清理、网关不暴露）。本批收口时全量后端回归实测
`1424 passed, 23 skipped`（含 #110–#119 与真模型点验读出的钩子编码修复）。

### #117 每轮回退点（P2 第 9 项）

**形状**：回退是「截断对话 + 把代码放回那一轮结束后」，两件事分开报告。

- `tasks.rewound_at` 标记不再属于对话的轮次，**行本身保留**：事件、工具调用、
  用量都挂在它上面，删行等于毁掉审计材料。可空而不是默认 0——「没回退过」
  与「回退时间未知」是两件事（迁移 `0026_task_rewound_at`）。
- `thread_turns` 只返回未回退的轮次；`rewind_to_turn` 把目标轮之后的轮次盖戳、
  把 `thread.turn_count` 退回目标轮。目标轮不存在 → 404 `turn_not_found`；
  有轮次还在 `queued/running/waiting_approval` → 409 `task_active`，且不落半截状态。
- 代码恢复取「目标轮之后最早一份可恢复快照」：那份快照拍在下一轮动手之前，
  正好是目标轮结束时的样子。`GET /rewind-points` 逐轮说明能否恢复；
  `POST /rewind` 在不能恢复时仍截断对话，但响应里 `snapshot_id` 为空、
  `skipped_reason` 写明原因，绝不假装代码也回去了。
- 回退事件 `thread.rewound` 挂在目标轮上；之后新一轮复用被释放的序号，
  旧行因 `rewound_at` 不会重复出现在对话里。归档会话 409 `thread_archived`。

**判据**：`services/api/tests/test_thread_rewind_v117.py` 8 条（截断+恢复、
无快照明说、运行中拒绝、缺轮 404、rewind-points 标注、事件挂载、序号复用、
归档拒绝）。

**变异实测**（按字节备份还原，逐条实测红数）：不过滤 `rewound_at` → 3；
不退回 `turn_count` → 2；恢复点永远为空 → 2；不标记被回退行 → 3；
谎报 `atomic` → 2；去掉运行中检查 → 1；把目标轮自身快照纳入候选 → 1。

**迁移实测**：临时 SQLite 库 `upgrade head`（0025 → 0026）RC=0，PRAGMA 确认
`rewound_at` 存在，`downgrade -1` 成功。

### #118 WorktreeCreate / WorktreeRemove 钩子（P2 第 10 项）

**形状**：非 git 工作区的隔离副本由钩子决定建在哪、删什么。

- `HOOK_EVENTS` 增加 `worktree_create` / `worktree_remove`；
  `HookResult.worktree_path` 从 `worktreePath` / `worktree_path` / `path`
  容错读取（与 #111 的字段容错同一口径）。
- `create_worktree_with_hooks`：钩子 `deny` → 直接失败，**不落默认副本**
  （否则用户以为隔离被拒绝，实际却复制了一份）；返回的路径必须是存在的目录
  且不等于基准工作区，否则记一条原因并退回整树复制，`notes` 里写明「钩子给的
  路径不可用」。成功时 `mode="hook"`，来源写进 `notes`。
- `remove_worktree_with_hooks`：先通知钩子（payload 带 `path/mode/base`），
  `mode="hook"` 由钩子负责删，其余仍走默认删除。
- 子代理正常结束、取消、`fan_out` 清理都走同一对函数；`hook.completed` 事件
  记录 `worktree_create` / `worktree_remove` 的结果。`SubAgentRunner` 把
  `session.hooks` 传给子代理——端到端测试直接调 `run_subagent` 测不到这一层，
  所以单独有一条 runner 传递判据。

**判据**：`services/api/tests/test_worktree_hooks_v118.py` 6 条（钩子路径生效、
删除钩子拿到路径、坏路径退回复制、deny 不落副本、runner 传 hooks、子代理
隔离走钩子）。

**变异实测**：忽略有效钩子路径 → 3；信任不存在路径 → 1；忽略 deny → 1；
不通知 `worktree_remove` → 1；runner 不传 hooks → 1（补上 runner 判据后实测）。

### #119 异步钩子 + 退出码 2 唤醒（P2 第 11 项）

**形状**：后台钩子不挡路；它叫醒模型的那一下必须真的进下一次请求。

- `HookSpec.async_run` 由配置 `"async": true`（也认 `asyncRun` / `background`）
  打开；只在**非阻断事件**上生效。`pre_tool_use` 上的 async 配置被忽略：
  拿不到结果的 veto 不是 veto，这条不靠文档自觉，有判据钉住。
- `HookRunner.run` 对后台钩子只 `create_task` 并立即返回；结果攒在
  `_completed`，退出码 2 的再进 `_wakeups`（文本取 `additionalContext`，
  即 stderr，兜底 `reason`）。正常退出只进审计，不唤醒。
- `call_model` 在注入 `additionalContext` 的同一位置先取审计结果写
  `hook.completed{async:true}`，再把唤醒拼成系统提醒注入本次请求并写
  `hook.wakeup_delivered`。注入后清空，不重复占上下文。
- 收尾有界等待：`BLANK_HOOK_SETTLE`（默认 1s，上限 10s）。窗口内完成的写
  `hook.completed{phase:"session_end"}`；退出码 2 但已无下一次模型调用的写
  `hook.wakeup_missed{delivered:false}`，**不假装送达**；没等到而被取消的写
  `hook.abandoned`（命令掩码）。取消必须杀进程树（Windows 用
  `taskkill /F /T`），只 kill 外层 shell 会留下孤儿——这条由「取消后标记文件
  不许出现」的判据钉住。

**边界（诚实记录）**：任务收尾后才到达的唤醒不会触发一次续跑。续跑意味着
从 START 重放图、可能重复副作用与消耗预算，本批选择「有界等待 + 如实记
`missed`」，没有伪装成「模型已继续」。真实模型下的投递链路已线上核对
（见下一节），但**没有**核对「模型收到提醒后因此改变了行为」——那需要构造
可观测的后续动作，本批没做。

**判据**：`services/api/tests/test_async_hooks_v119.py` 9 条（不阻塞调用点、
退出码 2 进队列且文本来自 stderr、正常退出不唤醒、阻断事件忽略 async、
收尾取消真的杀进程、唤醒进真实模型请求、收尾后记 missed、配置解析、
钩子正文不许按码页静默丢字）。

**变异实测**：后台分支失效 → 6；不写唤醒队列 → 3；见完成就唤醒 → 1；
阻断事件也异步 → 1；收尾不取消 → 1；`call_model` 不取结果 → 1；收尾不记
missed → 1；不解析 async 配置 → 2（含一条真实运行时配置判据）。

### 真模型点验（:8061，DeepSeek，实跑三格）

装置：`E:/tmp/realmodel/v119_check.py`（主格）、`v119_encoding.py`（编码格）、
`v119_control.py`（对照格）。钩子实物在 `E:/tmp/realmodel/hooks/`，实例用
`start-8061-with-hooks.sh` 起（`BLANK_HOOKS` 三条 + `BLANK_HOOK_SETTLE=2`），
点验完已按 PID 收回原姿态（不带 `BLANK_HOOKS`）。

| 判据 | 线上读数 |
| --- | --- |
| #118 副本来自钩子 | `worktree.created{mode:"hook", path:"…\\worktrees\\hookcopy-utlfxet_"}`；子代理读到了钩子独有的 `FROM-HOOK.txt`，全文出现在 `task_output` 结论里 |
| #118 清理契约 | `hook-events.log` 收到 `{"event":"worktree_remove","path":"…hookcopy-utlfxet_","mode":"hook","base":"…ws-v119"}` |
| #116 句柄不阻塞 | `spawn_agent` 的 `duration_ms=0`，返回 `agent-c490c785`；模型随后用**同一个** id 调 `task_output`，`ok=true` 取回结论 |
| #116 同一本账 | 证据包内含 `subagent` 节点；时间线有 `subagent.started/completed`、`subagents.released` |
| #119 唤醒投递 | `hook.completed{async:true, exit_code:2}` → `hook.wakeup_delivered{count:1, events:["post_tool_use"]}` → `hook.context_injected{chars:119, sources:[{event:"post_tool_use", tool:"read_file"}]}` |
| #117 回退点 | `rewind-points` 两轮各带 `restorable` 与 `skipped_reason`；`POST /rewind{turn:1}` 返回 `rewound:[{turn:2}]`、`restored:[]`、`skipped_reason` 写明「被回退的轮次里没有可恢复的快照」，`atomic=false`；会话列表只剩第 1 轮，`thread.rewound` 事件在目标轮上 |

主格 23 条判据里 22 绿、1 红；那一条红**不是判据写宽，而是读出一个真缺陷**：

- 判据原指望在 `hook.context_injected` 的 payload 里看到钩子原文，但该事件按
  脱敏设计只带 `chars` 与 `sources`。把这条改到正确的可观测量上（注入块长度
  ≥ 原文 + 包装语，且 sources 指向 `post_tool_use`）。
- 顺着长度对账才发现真问题：注入块 113 字，手算应为 119。差 6 个字来自
  `_execute` 用 `decode("utf-8","ignore")` 读钩子输出——Windows 上没
  `reconfigure` 的子进程按 cp936 写管道，解不出的字节被**静默丢掉**，送进模型
  系统消息的是乱码 `̨˵ … ܹ·`。丢字比乱码更糟：产物里看不出少过东西。
- 修复：`_decode` 按「UTF-8 优先 → 本机码页 → gbk → 最后 `replace`」解码，
  任何字节都不许无声消失（`services/hooks.py`）。判据
  `test_hook_console_text_survives_the_local_encoding` 改前红（实测
  `additional_context_chars=39`，原文 45），改后线上读到 45/119 与手算逐字相等。
  变异：退回 `ignore` → 红 1；只留 UTF-8 不留码页兜底 → 红 1。

对照格（不带 `BLANK_HOOKS` 的同一段目标）实测：副本仍创建但
`mode="copy"`，`worktree_create` / `hook.completed{async}` / `hook.wakeup_delivered`
三类事件全为 0——主判据在该红的地方红了，整次点验才成立。装置自身也自检过：
把对照格指向**带钩子**那一次的事件流，`control_red=false` 且三条不变量转红，
`rc=2`；指向无钩子那一次 `rc=0`。


