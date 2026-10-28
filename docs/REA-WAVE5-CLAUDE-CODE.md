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
| `UserPromptExpansion` + `suppressOriginalPrompt` | 53224、68366 | 提交前改写/压缩提示词，原文可不进上下文（**#123 已落，见 §17**） |
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

真模型点验（#120 起在仓库里，不再依赖 `E:/tmp/realmodel` 里的一次性脚本）：

```bash
# 离线自检 + 变异电池（不花钱）
services/api/.venv/Scripts/python.exe -X utf8 -m pytest services/api/tests/test_realmodel_device_v120.py -q
services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/mutation_battery.py

# 起带钩子姿态的隔离实例（自检不过就不起；姿态写进 instance-posture-<port>.json）
BLANK_REALMODEL_HOME=E:/tmp/realmodel bash evals/realmodel/launch-instance.sh --with-hooks &

# 三格真跑（花真模型的钱）；已跑过的按 RM_TASK_MAIN/RM_TASK_ENC/RM_TASK_CTL 复用
BLANK_REALMODEL_PROVIDER=<provider id> BLANK_REALMODEL_HOME=E:/tmp/realmodel \
  services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/async_hooks_v119.py
```

## 12. 本轮没有验证的

- `isolation: "remote"` 的云端执行行为（本机只有字符串，没有云端账号实测）。
- 企业托管规则 `allowManagedPermissionRulesOnly` 的实际下发路径（只读到报错文案）。
- Windows `srt-win` 的 WFP 过滤器行为（本轮只读字符串，未安装该组件；已有的
  `REA-WAVE4-CODEX-RUNTIME.md` 是对 Codex 沙箱的实测，不可移花接木）。
- `asyncRewake` 在真实模型下的唤醒时序（没有构造付费调用）。#119 用真实子
  进程钩子 + FakeAdapter 验证了「唤醒进下一次请求」，但没有验证真实模型收到
  提醒后的续跑行为。
- **#120 更新**：装置进仓库后，`E:/tmp/realmodel/v119_*.py` 那三个一次性脚本不再是
  复现路径；`evals/realmodel/README.md` 的跑法是唯一口径。仍然没验的三格原话抄在这里，
  免得读数被当成通过：真模型看到唤醒后改不改道（要人读回放）、结算后才回来的唤醒
  （产品记 `missed`，装置只确认它没冒充 `delivered`）、`hook.context_injected` 里的正文
  （按脱敏设计只有 `chars`/`sources`，逐字由编码格在线上钉）。
- **#120 新增未验 → #121 已验并更正**：这一条原写「对照格本轮是**复用**早前无钩子那一次的
  读数，不是与主格同一时刻双实例并跑；`premise_error` 只按当前 `BLANK_REALMODEL_BASE`
  那台实例的留痕判定」。2026-10-27 08:45 那一轮把格子与实例解绑后**真跑掉了**：主格 +
  编码格在 :8061（留痕 `hooks`）、对照格在 :8065（留痕 `no-hooks`），三格都是当刻真跑，
  装置当场 `[来源] control 格此刻真跑 @ http://127.0.0.1:8065`、29 条判据红 1 且正是
  该红那条、`RC=0`（见 §15）。复用仍然支持，但读数里会逐格明写「旧读数，不是此刻的实物」。
- **#123 新增未验 → #134–#137 已验并更正**：这一条原写三格没被实物点过，三条都闭掉了。
  ① `decision: "ask"`：产品侧按 fail-closed 阻断（#129），团队入口那一格已真跑
  （`team_submit_v123.py --mode ask`，主格 3 条任务全部 blocked、**0 token**，对照格红 4）；
  普通任务那一格仍不判——提交期没有确认界面可点，产品做了再补。② `_resume` 一路由
  `test_续跑不发提交钩子且抑制从检查点延续` 钉住（钩子只执行一次、续跑请求仍只含替代正文）；
  跨进程崩溃恢复仍未覆盖。③ 真模型上的团队入口已从单成员单轮扩到四格 × 主/对照
  （抑制 / ask / 多成员收尾 / 预算截断，共八次真跑，见 §17）。另外「模型看到补充后改不改道」
  仍不机器判：新增的是**可观察的那一半**——抑制/注入格断言模型没有引发任何工具动作；
  它有没有照做仍要人读回放。详见 §17。

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

> **#120 更正**：这一段记的是当时真实跑过的那一轮，读数照抄不改；但那三个脚本和
> `start-8061-with-hooks.sh` 已被 `evals/realmodel/` 取代，其中启动器 `printf` 手拼的
> `BLANK_HOOKS` 是坏 JSON（见 §14），照着它复现会得到「没有钩子」的姿态。

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



## 14. #120 真模型点验装置进仓库 + 装置离线自检

上一批结尾我自己登记了那条欠账：点验活在 `E:/tmp/realmodel/v119_*.py` 里，
换台机器、换个人就复现不了。现在判据、夹具、钩子、启动器、变异电池全在
`evals/realmodel/`，凭据仍然不在仓库里（`test_装置目录里不许有凭据或密钥文件` 扫整个目录）。
搬运不是复制粘贴——第一次真跑就翻在装置自己身上，翻车点逐条钉成了能失败的用例。

**翻车一：启动器 `printf` 手拼的 `BLANK_HOOKS` 是坏 JSON，产品静默读成「没有钩子」。**
`HookRunner.from_env` 是 fail-closed 的（`services/hooks.py:291-305`）：解析失败就降级成
「没有钩子」，**哪里都不报错**。于是三格照跑、钱照花，回来 11 条钩子判据全红——
红得对，但那 11 条测的是姿态不是产品。修法分两道闸：

- `evals/realmodel/hooks_config.py` 成为 `BLANK_HOOKS` 的唯一来源：`--print` 用
  `json.dumps` 生成（引号交给序列化，不再手拼），`--check` 校验合法 JSON、三个事件齐、
  命令恰好两段、夹具文件真存在、`async` 只落在 `post_tool_use`。启动器先 `--check`，
  不过就 rc 2 **拒绝起服务**。
- 启动器把姿态写进 `instance-posture-<port>.json`；装置在**花钱之前**读它
  （`lib.premise_error`）：要真跑主格/编码格而姿态不是 `hooks` → rc 2 拒跑；要真跑对照格
  而姿态是 `hooks`、或留痕文件不在（实例是手起的，姿态未知）→ 同样拒跑。
  复用早前的任务 id 不花钱，闸门不拦。

坏 JSON 那串留在自检里当永久负基线：产品必须读成 `enabled is False`
（`test_手拼的坏JSON只能读成没有钩子`），配置生成器则必须被 `HookRunner.from_env`
认下来（`test_启动器生成的钩子配置过产品自己的解析器`）——喂**产品的解析器**，
不喂装置自己的读法，否则两边一起错就看不见了。

**翻车二：装置有两套复用变量名，闸门把合法跑法判成失效。**
姿态闸门按格子名拼 `RM_TASK_CONTROL`，各格自己读 `RM_TASK_CTL`。于是「复用对照格、
只花主格+编码格的钱」在闸门眼里是「要新跑对照格」，rc 2 拦死。拦的方向没错（没白花钱），
但装置自己把唯一不烧钱的跑法堵死了。现在变量名只有 `async_hooks_v119.TASK_ENV` 一处，
`reused()`/`reuse_map()` 与三个 `run_*` 都走它；改名那一下立刻红
（电池 M20）。顺带补了 `RM_CELL` 格子名拼错必须 rc 2——写错一个字母静默少跑一格，
剩下的看着全绿其实是没判（电池 M21）。

**翻车三：`reachable()` 探的是 `/health`，而产品只有 `/api/v1/health`。**
404 也满足「有响应」，实例根本没起来时装置照样认为活着。现在探应用自己的健康路由、
且要求 `status_code < 400`（电池 M15/M16）。

**收紧 #118 清理那一条**：旧判据只问「`hook-events.log` 里出现过 `worktree_remove`」，
而这份日志在 `BLANK_REALMODEL_HOME` 里跨轮累积——上一轮的同名行能让它**假绿**。
现在拆成两格：事件流里的 `worktree.removed`（按轮天然隔离）与带外审计行，
后者必须命中**本轮** `mode=hook` 的那个副本路径（`judgments.py:61-110`）。
`test_陈旧审计行只能红在带外那一格` 先断言旧形状在这份日志上本来会绿，
再断言新判据的红集合恰好那一条（电池 M13/M14）。

**变异电池进仓库**：`evals/realmodel/mutation_battery.py`，21 条变异打在
`lib.py`/`judgments.py`/`hooks_config.py`/`async_hooks_v119.py` 上。它自己也是 Windows
上的一课：用 `read_text`/`write_text` 还原源码时把 LF 文件静默翻成 CRLF，
「逐字节还原」那句承诺是假的——现在按字节读写、记住原行尾，每条变异后立刻比对 sha。
期望红集合来自实测登记（当时那一份是 `fixtures/battery-booked-v120.json`；#121 把测试并入
后登记换代成 `battery-booked-v121.json`，旧那一代不再有代码引用，已随换代删除，内容留在
git 历史 `b3c9341` 里），第二轮起逐字核对；本轮 21/21 无哑弹，`[ALL-OK]`。

**线上读数（2026-10-27，:8061，DeepSeek，主格+编码格真跑、对照格复用无钩子那一次）**：
29 条判据全部读到，红 1 条且正是那条必须为红的（`对照·mode 不该是 hook`），`RC=0`。
关键读数：`worktree.created{mode:"hook", path:"…\hookcopy-d9ugm5nf"}`，子代理在副本里读到
钩子独有的 `FROM-HOOK.txt`；`hook-events.log` 的清理行带着**同一个**路径；
`hook.completed{async, exit_code:2}` 四条、`additional_context_chars=45` = 原文（修复前同格读成 39）；
注入块 119 字（下限 59）、`sources` 指向 `post_tool_use`/`read_file`；`spawn_agent` 返回
`agent-2fdb1091`、`duration_ms=0`，`task_output` 用同一个 id 取回结论；#117 五条读数全绿
（`restorable=false` 加原话 `skipped_reason`）。这一轮的实物取回仓库当绿基线
（`fixtures/events-main-v120.json` / `evidence-main-v120.json` / `hook-log-main-v120.txt`），
离线整轮重放 16 条全绿并钉住上述实测值（`test_姿态修好后那轮主格实物全绿且读数钉死`）。

装置目录、跑法与「不覆盖什么」见 `evals/realmodel/README.md`。仍然判不了的三格照旧写清：
真模型看到唤醒后改不改道（要靠人读回放）、结算后才回来的唤醒（产品记 `missed`，装置只确认
它没被冒充成 `delivered`）、注入事件里的正文（按脱敏设计只有 `chars` 与 `sources`）。

## 15. #121 装置改成一格一实例：对照格与主格同刻并跑

§12 那条欠账的形状是「装置只认识一台实例」。`BLANK_REALMODEL_BASE` 一个值管三格，
于是两件事同时成立：真跑对照格只能借用主格那台的姿态（一定被闸门拦下），
而「复用早前无钩子那一次」又让「两种姿态各起一台」这句话永远只能引用旧读数。
这一批把**格子**与**实例**解绑。

**名字只登记一处。** `lib.CELLS` 一张表给每格三样：任务 id 变量、base 变量、home 变量
（`RM_TASK_MAIN`/`RM_BASE_MAIN`/`RM_HOME_MAIN` …）。`cell_config(cell)` 从这里取值，
缺省回落到全局 `BLANK_REALMODEL_BASE`/`_HOME`；`reuse_map()`/`reused()` 也从这里取，
驱动侧的 `TASK_ENV` 改成这张表的投影。§14 翻车二那种「两套名字」在这里第三次露头之前
就被钉住：`test_任务id与实例的变量名同出一张表` + 电池 M24（把 `RM_BASE_CTL` 拼成
`RM_BASE_CONTROL` 立刻红）。

**闸门按实例分组核对姿态。** `premise_error(cells, reuse, targets)` 现在吃每格自己的
`lib.Target`（base、port、该端口的留痕），把**要真跑**的格按 base 分群，逐群三条判据：
一台同时领了钩子格与对照格 → 拦，且文案必须点名「用 `RM_BASE_CTL` 起第二台」+ 那条
`launch-instance.sh` 命令，不许退化成「今天没法并跑」；钩子格那台留痕不是 `hooks` → 拦；
对照格那台留痕不在或不是 `no-hooks` → 拦。留痕文件按端口命名
（`instance-posture-8061.json` / `-8065.json`），所以主格的姿态永远读不到对照格那台。

**写用例时才发现的真缺陷。** 第一版对对照格只判「姿态是不是 `hooks`」，于是
`{"posture": "坏掉的留痕文件"}`（留痕存在但内容不是那两种值）被当成「大概没钩子」放行——
装置会在一台姿态不明的实例上花钱跑对照格。收紧成必须读到 `no-hooks` 才算数，
钉了 `test_花钱之前按每格自己那台的留痕判定[坏留痕不许当无钩子]`、
`test_留痕文件坏了照样拦` 与电池 M23（把它改回 `if got == "hooks"` 就红）。

**驱动按格建客户端。** `cfgs/clients/targets` 三个字典各格一份；探活按 base 去重后逐台问，
连不上就点名是哪几台；跑之前逐格念 `[来源] <格> 此刻真跑 @ <base>` 或
`复用 task=… （旧读数，不是此刻的实物）`——「同刻双实例」这句话只由这一行证据支持，
不由文档声称（`test_读数要自报每格的来源`）。

**第二台的库从哪儿来。** `launch-instance.sh` 新增 `BLANK_REALMODEL_SEED_HOME`：目标 home
缺 `run.db` 时从主实例**拷文件**过去（`run.db`/`secret.key`/`settings.json`），
只搬路径、一个字节都不读进仓库或日志。密钥必须同一把，因为供应商行里那枚 Key 是它加密封存的；
库必须各一份，因为两台会各自写。

**这台机器上 :8063 已被占用**（一个无关的 `node.exe stub2.mjs` 在听），所以第二台落在 :8065。
不是装置的问题，但值得记：端口冲突时启动器会 bind 失败，而留痕文件是**上一次**那台的——
所以换端口要用新文件名，装置读的还是 `<port>` 那一份，`test_对照格指到另一台时只有它自己变`
钉的就是这个分文件行为。

**离线自检 96 条（70 + 26）**，新增的 26 条在 `services/api/tests/test_realmodel_instances_v121.py`：
表是唯一来源（1）、缺省回落到全局那一台（1）、每格 base/home/端口/留痕文件名/工作区各自独立（1）、
姿态闸门 12 个参数化形状（含「挤在一台要点名第二台」「坏留痕不许当无钩子」）、
成因文案要念得出端口与 env 名（1）、假客户端证明路由真的分两台（4，含「指错实例时
一个请求都不许发」与「坏留痕照样拦、一个字节都不发」）、读数自报来源（1）、
双实例那一轮实物的离线重放（5）。
（#123 后来又给这个文件补了两条行尾用例——混合即拒、纯 CRLF/纯 LF 各自往返——现在合跑是 98 条。）

**电池 25 条变异**（M22 分组键写成常量、M23 坏留痕放行、M24 base 变量名分叉、
M25 每格 home 不分成同一份），登记换代为 `fixtures/battery-booked-v121.json`。
重登记是因为实测而不是推测：并入 §4 那 5 条重放用例后，M9/M11/M14 三条变异的红集合
各自多出一个新节点，其余 22 条逐字未变——先跑一次拿到 diff、再删登记重烧、第三轮 `[ALL-OK]`，
四个源文件 sha 逐字节还原，0 哑弹。

**线上读数（2026-10-27 08:45，两台实例，DeepSeek）**：主格 + 编码格 @:8061（留痕 `hooks`）、
对照格 @:8065（留痕 `no-hooks`），**三格当刻真跑、无一格复用**。29 条判据、红 1 条且正是
那条必须为红的（`对照·mode 不该是 hook`），`RC=0`；`E:/tmp/realmodel/live-v121.out`。
关键读数：主格副本 `mode=hook` 路径 `…\realmodel\worktrees\hookcopy-wk1c6xvy`，`FROM-HOOK.txt`
被子代理读到，句柄 `agent-2af1ec67` 在 spawn 与 task_output 两处同值，`duration_ms=0`，
唤醒正文 45 字 = 原文，注入块 119 字；对照格副本 `mode=copy` 路径
`…\realmodel-ctl\worktrees\v119ctl-5446c6f0`——**另一台的 home**，且它的事件里一个钩子事件都没有。
两份实物（`fixtures/events-main-v121.json` / `evidence-main-v121.json` /
`hook-log-main-v121.txt` / `events-encoding-v121.json` / `events-control-v121.json`）各带
自己那台的 base 回仓库，离线重放复现同一副红绿。

**这一批仍不覆盖**：两台实例的库是**拷贝**关系而非共享，跑完各写各的，跨实例的对账
（同一 thread 在两台都出现过之类）装置不做；`SEED_HOME` 只在缺库时铺一次，主实例后来
新增的行不会同步过去；同刻并跑的「同刻」是装置串行发三格请求，不是并发压测。

## 16. #122 执行后端现场探测：六态读数、一台机器问一次

`docs/COST.md` 把这条登记成「还没做」已经好几天了：老口径是
`shutil.which("docker") is not None`，它只回答「这个文件名在不在 PATH 里」，
而装了 Docker Desktop 但守护进程没起来是常态。那种机器上老口径一路把容器说成「可用」：
界面写「可用」、任务照样起、然后每条命令各自撞一次墙。

**六态，不是一个布尔。** `services/sandbox/docker_probe.py` 给
`ready / not_installed / daemon_unreachable / timeout / probe_failed / pending`，
文案由 `state_label()` 单处产出，后端与界面共用（界面上那句「可用」是 `usable and isolation`
推出来的，不再自己写一遍——M41 钉这条）。判定顺序写在 `probe_once()` 里：
超时 → 装置不可信 → 退 0 **且**输出里有 `Server Version` 那行才算 ready →
退 0 但没看见守护进程那一行 = `daemon_unreachable` → 非零 = `daemon_unreachable` 并把 stderr 原样念出来。
「退 0 不等于可用」是本批的正身，M27（撤掉标记检查）实测红 9 条。

**三条铁律从 `services/sandbox/boundary.py` 借过来。** ① 判定必须带阳性对照：探针先用
`sys.executable -c "print(...)"` 证明"这台机器起得了子进程、读得到它的输出"，对照失败时
任何读数都只能写成 `probe_failed`——否则「这台机器没装 docker」是装置自己编的假事实（M29 红 1）；
② 测不到就降级不猜（超时、异常都不许冒充「不可用」）；③ 结论带时间戳，`age_seconds`
一路端到界面（「｜122.7s 前探测」），没探过的读数 `age_seconds` 是 `None` 而不是编一个 0。

**超时那一档是实测出来的形状。** `subprocess.run(timeout=…)` 只杀直接子进程，然后照样等管道关；
`docker.bat` 是 cmd 再拉一个孙进程的载体，孙进程占着管道就得白等。同一份桩（`ping -n 5` ≈4 秒）
配 0.4 秒闸门，当刻重跑 3 次：那种写法 **4.06–4.08s**，改成 `Popen + communicate(timeout)` +
`taskkill /PID /T /F` 杀整棵树 **0.63–0.65s**（一次性量具 `E:/tmp/v122/rig/killtree_cost.py`，
收尾即删；跑完 `tasklist` 里没有残留 ping）。判据 `test_探测超时既不算可用也要自己把进程带走` 钉的就是那个上界，
M28 实测红 1。

**缓存：一台机器问一次，但检查必须在锁内。** TTL 60s + `force` + `invalidate`。
10 个并发 `ensure_fresh` 只起 **1** 条子进程、总耗时 0.098s（一次性量具 `E:/tmp/v122/rig/cost_final.py`，收尾即删）；
把缓存检查挪到 `with _LOCK:` 外面就会变成各探各的——M44 实测红那一条正是并发用例。

**一条撤回的错误结论，值得记下来。** 早前一版脚本打出「10 并发起了 10 条子进程、耗时 2.395s」，
我据此判定决策点有并发风暴，并加了 `_IN_FLIGHT` 合用逻辑。更窄的装置复测**复现不出来**
（起 1 条、0.077s），而"撤掉合用"那条变异在电池里 0 红 = 哑弹。两件事凑齐就判掉：
那条缺陷不成立，代码回退，判据改成钉住真实形状（缓存检查在锁内），
而不是钉一个我推测出来的机制。教训是**红集合必须实测**——先有实现再补的判据，很容易把
"我以为的缺陷"写成永远绿的那一格。

**决策点绝不在事件循环里同步起子进程。** `probe_for_decision()` 在循环里只读缓存，没缓存就返
`pending`（`search_code` 那次把整个后端占死就是同步子进程独占循环，§早期批次记过）。
真起进程那一下交给 `ensure_fresh` → `asyncio.to_thread`。M31 实测红 2。

**pending 在决策点按 PATH 兜底，是刻意的。** 装完 Docker 没重启服务的窗口里，若把 `pending`
当「不可用」，正常机器会被 fail-closed 拦死。代价是这一页在**从没测过**的机器上会报
`isolation=container`。这就是两处补探的理由：
① 设置页——闸门挂在**策略** `SandboxPolicy().enabled` 上而不是读数上（开着沙箱必须补探，
关着时读数只是资料，不值得把客户点开设置那一下卡在一条子进程上；M45/M46 两个方向各红 1）；
② 装配层——任务 `/run` 与团队建群前各补探一次（M47/M48 实测各红 1，
`test_建群那一刻也先探一次` 是同一条里自带 ready 桩的对照格，防止红点落在「建群本来就不通」）。

**开机那行读数要当场落盘。** lifespan 里挂了 `probe_execution_backend()`（M35 红 1；
`test_应用启动装配里真的挂着那一次探测` 自己开 lifespan，因为 `ASGITransport` 不跑它）。
真机 :8061 实测：stdout 重定向到文件时那行不会当场落盘——启动后约 6 秒读日志只有 4 行
uvicorn 自己的日志，而缓存里那时已经躺着 `docker_age_seconds=56.4`（探测早跑完了，print
还压在块缓冲里）；打完三次 HTTP 请求后再读（约一分半后）它才出现。
加上 `flush=True` 后重启，5 秒内那行就在文件里了：
`[sandbox] 执行后端探测：not_installed｜PATH 里找不到 docker 可执行文件`
（工件 `E:/tmp/realmodel/8061-v122b.log`）。探测自身炸了不许挡 API 启动（M36 红 1）。

**代价（写进 `docs/COST.md`，不烧 token，只花墙钟）。** 单条 `docker version` 0.079–0.097s
（闸门 5s）；设置页第一次读：开沙箱 0.112s、关沙箱 0.034s，TTL 内第二次都 0.001s；
真机 :8061（沙箱关着）`GET /settings/safety/runtime` 首读 0.248s（age 56.4s）、
`POST /safety/runtime/reprobe` 0.131s 换新读数（age 0.0）、紧接着再读 0.053s（age 0.1s，没再起进程）。

**判据与电池。** 后端 26 条 `services/api/tests/test_backend_probe_v122.py`（6.4s 全绿），
全部用 PATH 前置的**真桩**打出来（Windows `docker.bat` / POSIX shebang `docker`），
不用假读数；前端 5 条 `web/src/components/settings/api.test.ts`（node:test，5 pass 0 fail）。
变异电池 23 条（M26–M48），**0 哑弹**、逐字节还原校验通过，红集合当刻读数：
M26 红 6、M27 红 9、M28 红 1、M29 红 1、M30 红 2、M31 红 2、M32 红 2、M33 红 3、M34 红 1、
M35 红 1、M36 红 1、M37 红 1、M38 红 2、M39 红 1、M43 红 1、M44 红 1、M45 红 1、M46 红 1、
M47 红 1、M48 红 1、M40/M41 红 2、M42 红 3（电池 `E:/tmp/v122/rig/battery.py` 是一次性装置，
收尾即删；上面那行红集合就是它当刻的读数 `battery-out6.txt`，逐条抄在这里，`[ALL-OK] 变异 23 条，哑弹 0 条`）。

**界面点验（真 Chrome，:3210 私有 dev 实例代理到 :8061，`:3000/:8000` 未碰）。**
`#safety` 面板横幅渲染出
`Docker 未安装：PATH 里没有 docker 可执行文件｜122.7s 前探测`（`text-warn` 档，
`title` 里带成因），点「重新探测」后同一行换成 `｜0s 前探测`，并出现
「执行后端重新探测完成：…」提示——指针点击在这台机器不可用，改用 `evaluate_script` 派发真 DOM 事件。

**全量回归**：后端 `1546 passed / 23 skipped`（345.52s），前端 `205 pass / 0 fail`（node:test，rc=0），
`tsc --noEmit` rc=0 且零输出。

**这一批仍不覆盖**：`not_installed` 时到底该不该自动降成本机执行（默认档仍是产品裁决，没替客户改）；
Docker 真装好的那台机器（本机没有容器可用，`ready` 一档只由桩证明）；
桩在 POSIX 分支上也写了（shebang 脚本 + `exit`），但**本机只真跑过 Windows**，
mac/Linux 上那两条分支没实物点过；
`remote` 隔离形态完全没接；缓存是进程级的，多 worker 各探各的（本批只保证单进程内不重复）。

## 17. #123 提交钩子的三道决定：拦下、抑制、补上下文

§8 那行「`UserPromptExpansion` + `suppressOriginalPrompt`」挂了好几天，老行为是三样都不成立：
`user_prompt_submit` 只跑钩子、钩子给的 `additionalContext` 只进审计（正文被静默丢掉）、
`suppressOriginalPrompt` 这个键我们**连读都不读**（`HookResult` 里连字段都没有）。
这一批把三道都真的作用在「送进模型的那一份」上。

**唯一去处是 `goal_for_model`。** `runtime.py:_submit_prompt`（:517）产出 `_PromptDecision`，
运行时里只有**一个调用点**——`runtime.py:668`，在 `task.started` 之前。三道决定各是一条返回路径：
`blocked` → `_finish(failed, steps=0)` 且**连 `task.started` 都不发**（它从未开始跑，
先念「已开始」再念「失败」是给时间线作假）；`suppressed` → 送出去的是
`（本节由提交钩子改写，用户原始提交未进入上下文）` + 替代正文（:594）；
只有 `additionalContext` → 原文照送、在后面追加那一段（:563）。钩子没说话时 `goal_for_model` 就是原文。

**抑制的是上下文，不是账。** 这批最容易写歪的一条：`tasks.goal` 必须逐字等于用户写的那份，
被换掉的只是送进模型的那一节。所以抑制格同时断言两件事——事件里的
`original_chars/original_sha` 对得上账面 goal，且账面 goal 没被改写。另一头，
上下文管家在抑制时拿到空 goal（`runtime.py:744` 的 `goal="" if prompt.suppressed`）：
管家按关键词挑工作区文件，留着原文就等于给「抑制」留了个侧门。

**事件只带读数，不带正文。** 三条新事件（`hook.prompt_blocked` /
`hook.prompt_substituted` / `hook.prompt_injected`）按既有脱敏口径只登记长度 + 12 位指纹 + 来源，
正文一个字都不进事件流。

**真模型五格同刻并跑（:8061 四格带钩子 + :8065 对照格无钩子）。**
工件 `E:/tmp/realmodel/prompt-submit-v123-20261027-202540.json`（仓库内绿基线
`evals/realmodel/fixtures/prompt-submit-v123-green.json`，其 `provenance` 里带着同一份工件路径与
`rc: 0` / `control_red: true`）：主格 21 条判据**零红**，对照格那 8 条重放抑制格判据、红 6 条，
其中白名单三条非红不可（`judgments.CONTROL_123_RED`）。逐字读数——
抑制格（task `6bfe4e34`）`suppressed=true`、`original_chars=68`、`original_sha=165901c5c3b3`、
`replacement_chars=69`、模型答 `改写版正文-7733`、`curated=[]`；
拦下格（`015977ce`）`reason=装置夹具：带这个锚的提交一律不进模型`、`original_chars=40`、
`exit_code=2`、`usage=0 started=0 steps_used=0`、`status=failed`（5 个事件，一次模型调用都没有）；
补上下文格（`d9dfb2c2`）`chars=24 original_kept=true`、无抑制事件，模型把 `绿杨二十三号` 与
`黄锚生效-5166` 都点了名并自己判定「标注来自提交钩子，是外部注入的指令串」——**它选择拒绝照做**，
判据钉的是「两个串都到得了模型的输出」，读得到不等于照做；无关格（`bf9601e8`）只有
`hook.completed`、照读 `白塔九号`；对照格（`54a405ea`）`curated.files=["紫椒八十一号-清单.txt"]`、
`summary=紫椒八十一号`——侧门与原文泄漏在没钩子时确实发生，所以主格那三条测的才是钩子路径。

**前两轮 rc=1 红的是装置，不是产品**（stdout 都在：`E:/tmp/v123/realmodel_run.txt` →
`…-202209.json`、`realmodel_run2.txt` → `…-202349.json`）：① 替代正文那句夹具文案被模型读成
「去写文件」；② 驱动把对照格的红并进主判定表，把白名单之外的红也算成产品缺陷——
改成「对照格用自己那张判定表、只交一件事：那三条到底红没红」（`prompt_submit_v123.py:judge_cell`）
之后才是 rc=0。

**装置自检 80 条 + 25 条变异电池。** `services/api/tests/test_prompt_submit_device_v123.py`
拿五格实物重放判据，`test_同一套对照判据打在带钩子的实物上不该全红` 是反向格（防「对照判据写宽」），
`test_锚与正文只写一处夹具里不许出现第二份抄本` 钉夹具与判据共读的那份规格
（`fixtures/prompt-submit-v123.json`）。25 条电池把事件删掉、把长度改成 `None`、把 goal 换成
抑制后的那份、把姿态留痕的 `true` 换成 `"true"`/缺键……每条的期望红集合**都来自实测**，
红集合漂移即 `[ABORT]`。团队入口那一段是四格 × 主/对照的实物重放，外加 **23 条损伤变异**
（抑制 6 / ask 7 / 多成员 5 / 预算 5）：先以占位符跑出实测红集合、逐字登记，再跑一次
确认全绿——`任务行整个丢了` 在 ask 格里红 5 条、在 multi 格里红 5 条，`收尾轮丢了`
只红多成员那一条（损伤里同时把账面 goal 改回原文，否则它会连带打红「账面 goal」）。
产品侧另有 17 条（`test_prompt_submit_v123.py`），含
`test_团队入口的成员任务也过提交钩子`、`test_团队入口的ask档也按拒绝处理`、
`test_fan_out子代理的goal来自模型而不是这次提交`、`test_钩子要求确认时按拒绝处理并说明`、
`test_续跑不发提交钩子且抑制从检查点延续`。合跑 97 passed。

**电池这一轮先把自己修好再交付。** 第一次完整跑停在 M5 `[ABORT] judgments.py 没回到原字节`：
#123 的编辑让这个 CRLF 文件混进了裸 LF，而电池按它认到的那一种行尾写回，等于静默归一化——
「逐字节还原」那句承诺当时是假的，红的却是装置自己。`load()` 现在先数两种行尾、混了就拒绝
（`行尾混合（CRLF=… 裸 LF=…）`），配两条用例：混合即拒、纯 CRLF/纯 LF 各自往返逐字节一致。
反证：把 `if crlf and bare:` 改成 `if False:`，`test_电池对混合行尾的文件拒绝动手` 当刻红
（`1 failed, 1 passed`，rc=1），还原 sha256 一致。修好后整轮读数（`E:/tmp/v123/battery_full_v123.txt`）：
基线 `rc=0 红=[]`、**25 条全 rc=1 且红集合与 `fixtures/battery-booked-v121.json` 逐字相等**、
四文件逐字节还原、还原后复跑全绿 `[ALL-OK]`。收尾这批（judgments.py 加团队判据之后）又跑了一轮
`E:/tmp/v132_battery.txt`：25 条同样全红、红集合逐字相等、还原后复跑 `rc=0 红=[]`，`[ALL-OK]`。

**界面三卡 + 反证（真 Chrome，:3100 私有 dev 代理到 :8061，`:3000/:8000` 未碰）。**
`web/src/components/tasks/timeline.ts:286-329` 三张卡分别 `text-danger` / `text-warn` / muted，
读数全部从事件本身取。点验 16 条判据全绿（`E:/tmp/v123/ui_baseline2.txt`，`红 0 / 判据 16`），
卡片里的 `40 字`/`68 字`/`69 字`/`24 字` 是当场从 `/api/v1/tasks/<id>/events` 取的，不是抄的。
反证电池 8 条（`E:/tmp/v123/mut_ui_v123.py`，改仓库真源码 → UI 与前端单测同跑 → 红集合逐字核 →
sha256 还原）：`E:/tmp/v123/mut_ui_run4.txt` 读数与登记逐字相等，`电池 8 条，异常 0 条`。
其中两条是特意用来**量判据能不能失败**的：MU6（改隐藏列表里的 `hook.` 前缀）两层都绿 = 等价变异，
登记为空集；MU7（让前端把正文抄上屏）只有单测红、UI 照旧绿——真事件流的 payload 里根本没有正文字段，
所以「卡片不落正文」这条性质在浏览器层**不可失败**。据此摘掉 UI 基线里那三条（U1e/U2e/U3d，19→16），
性质移交能失败的那条单测（`三道决定的卡片只报读数：payload 里混进正文也不抄上屏`，
用带 `goal`/`text`/`replacement` 字段的 payload 打）。

**#129 `decision: "ask"`：提交这一步没有确认界面，只能按拒绝处理。**
钩子说「先问一声」时，工具档有审批卡可弹；提交这一步没有。#129 之前 `asks` 只随
`hook.completed` 记账、正文照送——等于把钩子写下的条件静默丢掉。现在 `_submit_prompt`
在 deny 分支之后接 ask：`hook.prompt_blocked` 带 `decision="ask"` 与钩子原因、
`_finish(failed, steps=0)`、连 `task.started` 都不发。用例
`test_钩子要求确认时按拒绝处理并说明`（stdout 给 `{"decision": "ask", "reason": "先问一声再放行"}`）
断言模型零请求、失败原因含 ask 与那条原因、事件带长度不带原文。反证（一次性装置，已删）
`E:/tmp/v123/redlock_ask.py` 把分支改成 `if False:`，目标用例当刻红（rc=1），还原 sha 一致。
所以「ask 能放行」这条路在产品里不存在，装置不判它；哪天做了提交期确认界面再补一格。

**#130 续跑：钩子只跑一次，抑制从检查点延续。**
`_submit_prompt` 的唯一调用点在首轮，恢复执行没有新的提交。用例
`test_续跑不发提交钩子且抑制从检查点延续` 跑真链路：夹具钩子每次被调用就往计数文件追一个字节、
随后给抑制决定；`apply_patch` 写 `.env` 触发审批，任务进 `waiting_approval`（计数器=1、
模型已见替代正文），批准后断言：计数器仍=1、续跑的每一次请求都不含原文、含替代正文、
`hook.completed(user_prompt_submit)` 恰一条、账面 goal 仍含原文。反证（一次性装置，已删）
`E:/tmp/v123/redlock_resume.py` 在 `_resume` 里插回 `await self._submit_prompt(scope)`，
用例当刻红（`assert 2 == 1`），还原 sha 一致。**跨进程崩溃后的恢复已由 #140 之三闭合**
（两个真进程：检查点由被杀进程写下、新进程续跑，见本节末尾用例与变异电池）。

**#131 团队入口：真模型两格 → 四格 × 主/对照（#134–#137）。**
群聊的成员任务 goal 直接由 `services/team.py` 从用户发言取，所以提交钩子对它是第二条入口。
第一轮（#131）只跑单成员单轮：主格成员任务 `8f08b858` 的事件是
`team.turn.started → hook.completed → hook.prompt_substituted → task.started`，五条判据零红；
对照格红 3 且与 `CONTROL_123_TEAM_RED` 逐字相等（工件
`E:/tmp/realmodel/team-submit-v123-20261028-085330.json` 与
`E:/tmp/realmodel-ctl/team-submit-v123-control-20261028-085407.json`）。那一轮暴露出判据第一版
的真缺陷：原先按「goal 含原文锚」挑成员任务，账面被改写的任务会直接从样本里消失、
第三条判据成了恒真；现在按 `team.turn.started` 认任务、期望 goal 取这一轮用户发言原文，
收尾轮按 `WRAP_UP_PREFIX + 原文` 算，装置自检钉住前缀与 `team.py` 同字。

`team_submit_v123.py` 随后扩成四格（`--mode suppress|ask|multi|budget`），主格与对照格各自
同刻真跑，八次全跑、没有一格靠复用（工件在 `E:/tmp/realmodel*`，实物进仓库
`fixtures/team-submit-v123-green.json`）：`suppress` 3 任务 / 12634 tokens 零红 → 对照红 3；
`ask` 3 任务全部 failed / **0 tokens** 零红 → 对照红 4（`CONTROL_123_TEAM_ASK_RED`）；
`multi` 两位成员 + 收尾共 4 任务 / 16384 零红 → 对照红 3；`budget` 2 任务后截断 /
8121 ≥ 5000 零红 → 对照红 3。对照红集合一律按模式分派且**精确相等**（多一条少一条 rc 2），
`multi`/`budget` 的结构判据不进红集合——它们在没钩子时本来就成立。

同批还钉了两处边界：**`fan_out` 子代理的 goal 来自模型工具参数、不经过提交钩子**
（产品用例 `test_fan_out子代理的goal来自模型而不是这次提交` 断言父请求含替代正文不含原文、
子请求两者都不含、子 messages 第一条 user 逐字等于 child_goal，钩子各恰一次），
以及**抑制/注入格的「没有引发任何工具动作」判据**——语义反应里可观察的那一半成了机器判据。
团队入口的 23 条损伤变异（抑制 6 / ask 7 / 多成员 5 / 预算 5）期望红集合全部实测登记。
仍不覆盖：子代理该不该过钩子（现在的答案是「不该，goal 由模型写」）；群轮进行中的**用户追加发言**
与队列重排（#140 之二只钉了中途加人与改发言上限，同一轮里用户再发一条仍未验）；
模型有没有照做（工具动作之外的部分）。

**全量回归**：后端 `1647 passed / 23 skipped`（352.36s，`E:/tmp/v138_full_backend2.txt`，rc=0，
含上面两条时序用例的修法与加宽），前端 `211 pass / 0 fail`（node:test，1015.99ms，rc=0），
`tsc --noEmit` rc=0 且零输出。前一轮全量（`E:/tmp/v138_full_backend.txt`）是
`1 failed / 1646 passed`：红的就是 `test_read_only_calls_overlap`（并行 0.63s / 串行 0.71s），
更早一轮红的是收尾取消那条——两条都已按上文处理。
这一轮四格 × 主/对照的账单也抄进 `docs/COST.md`（主格 37,139 token、对照格 50,823，
模型调用 9 / 11 次；`ask` 主格那 0 是「一次都不调」的账单证据）。

**全量回归里红过的那条时序用例：是产品的一个真缺口，不是测试噪声。**
`test_close_cancels_overdue_hooks_and_reports_abandoned` 在 13 分钟的全量里报
「收尾取消只取消了任务，子进程还在后台跑」。第一步按负载敏感处理（子进程 sleep 0.6→3.0s、
观察窗改成 3.6s 轮询、窗口内任何时刻出现标记即红），单独连跑 12 次全绿；但一次隔离复跑里
同样红了——加宽窗口没消掉它，说明噪声之外另有东西。定点实验把范围收到 `_execute` 的
**进程创建段**：取消正好落在 `create_subprocess_shell` 还没返回的那几十毫秒里时，
`except CancelledError` 分支还没拿到 `process`，`_kill_process` 无从下手，子进程就留在机器上。
机器越忙、创建越久，这个窗口越容易被撞上，所以它只在全量回归里露头。
修法是把创建包进 `asyncio.shield`（`hooks.py` 新增 `_spawn_shell`）：创建捂完整、拿到进程
先杀再重抛取消，取消语义不变。新用例 `test_cancel_during_spawn_still_kills_the_child`
用 monkeypatch 把「创建中」这一段拉长到 0.4s、收尾窗口压到 0.05s：修前必红（实测红在
`取消落在创建途中，子进程成了孤儿`，`spawn-window.txt` 出现），修后绿。加宽窗口那条照旧保留
（判据没收窄）。

**同一轮全量里另一条负载敏感判据：只读工具并行。**
`test_read_only_calls_overlap` 读到并行 0.63s / 串行 0.71s，比值 0.89 超过 0.8 上界，而交错日志
成立——它确实在并行，红的是负载。改成**每侧三轮取最小值**：比值仍是 0.8、并行轮每一轮都要交错、
串行基线每一轮都不许交错。一次性探针把「看起来并发、实际进了锁」的实现按同一套测法量了一遍
（`E:/tmp/locked_probe.py`）：真并行三轮 0.30/0.32/0.31 → 比值 0.54 判绿，进锁三轮
0.54/0.55/0.57 → 比值 0.97 判红，交错日志两种都成立。最小值只压噪声，压不动锁死的实现。

**群轮进行中的追加发言、队列中途失配与停止（#139）。**
上一批留下的「追加发言/队列重排未跑」按四条产品契约补成可失败用例
（`services/api/tests/test_team_edges_v139.py`，脚本化模型，6 passed / 10.57s）：

1. **追加发言 fail-closed 且无痕**：手放一轮 `running` 后 POST 第二句 → 409
   `team_group_busy`、文案要指出路（含「停止」）；库里这句零消息、user 消息恰 1、轮恰 1；
   轮次自己走完后再发同一句 → 201 且新轮 id 不同、user 序列 `[第一句, 第二句]`。
2. **队列里成员中途停用**：`PATCH /team/bots/{id} {"enabled": false}` 后轮次仍完成，
   系统说明含「已停用」、失败者不出气泡、`Task` 计数不变。
3. **成员模型 500 不拖死整轮**：失败气泡如实记原因（非空）、后续成员与主理人照常收尾，
   `Task` 状态序列 `["failed","succeeded","succeeded"]`。
4. **停止落在等待审批这一刻**：等待那条记 `cancelled`/「已停止」、队列里没人建任务、
   不再收尾；停完同一群立刻能开新一轮。

同批修掉两个真缺陷：

- **点名超上限静默消失**。`create_user_round` 原先 `queue[:cap]` 直接砍人，群聊里看不出
  「你点的人没排上」。现在建轮时补一条系统说明：上限几次、谁被挤掉、下一轮再点名。
  用例 `test_mentions_beyond_the_speaker_cap_say_who_missed_out` 先红后绿
  （红基线实测 `system_rows == []`）。
- **`latest_round` 把上一轮读成当前轮**。`team_rounds.created_at` 走 `CURRENT_TIMESTAMP`，
  SQLite 只到秒；同一秒先后两轮并列时，单键 `order_by(created_at desc)` 读回旧轮，
  前端面板于是显示旧轮的状态/队列/用量（停止用例 30 s 超时就是这么露出来的）。
  改为用群内自增 `TeamMessage.id` 的 `max` 作相关标量子查询兜底，`coalesce(...,0)`
  防 PG 的 DESC NULLS FIRST；用例显式把两轮 `created_at` 置为同一值钉住（修前必红）。

变异电池 11 条（只改 `team.py`、逐字节还原，装置 `E:/tmp/teamedges/mutate_team_edges.py`，
登记 `E:/tmp/teamedges/book.json`）：拆 busy 闸门 / 说明不落库 / 不写上限数字 / 不点名 /
跳过不留说明 / 失败不算发言 / 失败不写原因 / 停止不取消等待那条 / 停止只置 cancelling /
`latest_round` 回旧写法 / 并列兜底方向反了——每条**恰 1 个红点**，相邻文件零波及。
整轮复跑被中止后改为对 M4 定点两轮（`check_m4.py`），两轮都只红「追加发言」那条；
中途读到过 2 条，追查是工作树里残留了一条 M8 变异（`request_stop` 里 `if False and ...`），
还原后复测 1 条——**变异装置的收尾必须核对工作树无残留，否则后续红集合全部带脏**。

**本批全量回归**：后端 `1652 passed / 1 failed / 23 skipped`（418.97s）。红的是
`test_run_lease_v102.py::test_a_non_owner_cannot_write_the_terminal_state`（租约 `lease_seconds=0`
后立刻由 B 接管），单跑 20/20 绿、整文件 6/6 绿，只在全量负载下偶发一次；与本批团队改动无关，
按负载敏感的时序用例记录，没有放宽判据。前端 `node --test` 211 pass / 0 fail，`tsc --noEmit` rc=0。

**这一批仍不覆盖**：模型看到改写/补充后改不改道，机器只判到「有没有引发工具动作」这一层，
照没照做仍要人读回放；`fan_out` 子代理的 goal 由模型写
（现状即契约，用例钉的是它不过钩子也不泄漏）；`ask` 在产品里只有 fail-closed 一条路
（提交期确认界面不存在，做了再补）。

**群轮中途丢点名补逐人说明（#140 之一）。**
建轮时点满上限已有说明（上一批），但轮内 `_finalize_turn` 因发言上限或 `MAX_QUEUE_DEPTH`
丢掉后续点名时，只有轮末一句「达到上限」，被丢的人静默消失。现在排不下时先归类被哪条闸
挡住，再落一条 system 逐一念出名字：`本轮发言上限是 N 次（主理人的开场也算在内），@A、@B
这次没排上，需要时请下一轮再点名。`；队列深度那条同理念「这一轮的待发言队列最多 4 位」。
上限每次从 `group.max_speakers` 实时读，不缓存。
用例 `services/api/tests/test_team_queue_v140.py`（2 passed）：发言上限丢人、队列深度丢人
（4 名成员建群 + 第 5 人直插成员表，断言系统行含「运维」「没排上」「4」且 `round_id`
属于当前轮）。变异电池 `E:/tmp/m141/battery_v140.py`（bytes 读写 + sha256 还原）实测：
M1 删掉丢人说明 → 红 `test_mentions_dropped_at_the_speaker_cap_are_named` 与
`test_mentions_dropped_by_queue_depth_say_so` 两条；M2 一律说成发言上限 → 只红队列深度那条；
还原 sha256 一致，复跑 2 passed。

**中途加人 / 改发言上限对当前轮是否生效（#140 之二）。**
量清的三条口径：① 发言上限在 `_advance` 每次循环实时读，**调低立即收口、调高后本轮后续
点名按新上限排得进**；② 队列在 `create_user_round` 时定格，中途进群的人不会被自动塞进当前
轮，但被 @ 时可以上场；③ `PATCH /team/groups/{id}` 轮中允许改 `max_speakers`，改
`member_ids` 回 409 `team_group_busy`，轮结束后同一请求 200。
用例 `services/api/tests/test_team_midround_v141.py`（5 passed，连跑 5 次稳定）：降上限 5→2
后只有主理人与评审发言、说明念出「2」；升上限 2→4 后评审收尾点名的两位按新上限入场且不再
说「没排上」；中途用 `team_add_member`（高风险、走真实审批挂起）把运维拉进群，不被 @ 时不
发言、被 @ 时发言；轮中改成员列表 409、轮后 200。装置自检会直接查库确认运维真的在轮次中途
进了群，避免「人没进来所以没发言」的假绿。一个踩过的坑：**从 mock 模型 handler 里同步写
sqlite 当同步点不可靠**（`database is locked`，或写成功但编排器读不到），改用真实审批挂起 +
真实 PATCH / 工具调用做同步点后才稳定。
变异电池 `E:/tmp/m141/battery_v141.py`（bytes 读写 + sha256 还原）实测红集合：
M1 `_advance` 上限写死 5 → 红 `test_lowering_the_speaker_cap_mid_round_stops_at_the_new_value`；
M2 `_finalize_turn` 上限写死 2 → 红 `test_raising_the_speaker_cap_mid_round_lets_later_mentions_in`；
M3 点名只认开轮队列里的人 → 红 `test_a_member_added_mid_round_joins_when_named` 与
`test_raising_the_speaker_cap_mid_round_lets_later_mentions_in`；
M4 `_advance` 每次推进把未发言成员补进队列 → 红 `test_a_member_added_mid_round_is_not_queued_without_a_mention`；
M5 取消轮中调整成员列表的 409 → 红 `test_member_list_cannot_change_while_a_round_is_running`。
每条变异后逐字节还原并校验 sha256 一致，复跑 5 passed。


**跨进程崩溃恢复的续跑（#140 之三）。**
上一轮只有仓库外探针（`E:/tmp/m90/worker/probe_worker.py`）证明过「真杀进程、另一个进程
从检查点续跑」，仓库里没有任何一条用例。现在落成
`services/api/tests/test_crash_resume_cross_process_v142.py`：进程内起真 HTTP 桩模型，
父进程建库/项目/供应商/任务与一条 queued run，启动真的 `python -m blank_api.worker`
（`BLANK_WORKER_ONCE=1`、租约 2 s）；桩模型在「工具已执行、结果已回给模型」的那次调用上挂住，
父进程确认 **检查点是被杀进程自己写下的**（`agent_checkpoints` 有行、run 为 running/attempts=1）
后 `TerminateProcess`；租约过期再起第二个 worker，断言 run 变 succeeded/attempts=2、
`progress.txt` 恰好一行、`run_shell` 工具账恰好一条、`task.started` 一条、
`task.resumed` 一条且 `crash_recovery=True`。单跑 1 passed / 6.14 s，连跑三次 6.14/6.13/6.15 s。
顺带把 `tests/conftest.py` 的主密钥固定成合成值（`BLANK_SECRET_KEY`）：默认落盘位置在
`data/.blank_secret.key`，跨进程用例里父子 cwd 不同会各生成一份，存进去的 Key 直接解不开。
变异电池 `E:/tmp/m141/battery_v142.py`（bytes 读写 + sha256 还原）实测红集合：
M1 `crashed = attempts > 0` 改成 `False`（崩溃后当新任务重跑）→ 红在
「工具应恰好执行一次：['crash-probe','crash-probe']」；
M2 `resume_crash` 传 `crash_recovery=False` → 续跑路径当场失败，红在
「run 应为 succeeded/attempts=2，实际 failed/2」；
M3 事件载荷里 `crash_recovery` 写成 `False` → 红在「续跑必须标成崩溃恢复」。
每条还原后 sha256 一致，复跑 1 passed。

**`run_id` 四形状：裁决与写侧对齐（#141）。**
待裁决项按“不统一写侧”收口：四种形状是**出处**而不是同一种东西的四种写法——空串=回合归属
上线前没记，`team-<轮>-<消息>[-resume]`=群聊某成员的这次发言，`task:<uuid>:attempt:N`=
同步入口兜底键，`agent_runs.id`（uuid）=排队入口真实运行。统一格式会丢掉「这一笔是谁引起的」，
而读侧 `run_id_kind()` 已在 `usage.turns[].kind` 给出唯一命名；不做数据迁移。
`services/api/tests/test_run_id_shapes_v143.py`（3 passed / 4.07 s）钉真实产出而不是构造函数：
① 群聊一轮（含审批放行后的续跑）产出的每个 `run_id` 都被念成 `team`，普通键逐条指向真实消息，
续跑键恰为 `team-<轮>-<该消息>-resume`；② 同步 `POST /tasks/{id}/run` 的账本键恰为
`task:<task_id>:attempt:1`、念成 `attempt`；③ `BLANK_INPROCESS_RUNNER=0` 时
`POST /tasks/{id}/start` 返回的 `run_id` 就是 `agent_runs.id`、念成 `run`。
变异电池 `E:/tmp/m141/battery_v143.py` 实测红集合：M1 读侧不认 `team-` 前缀 → 红群聊那条；
M2 群聊写侧改成 `team:` 前缀 → 红群聊那条；M3 attempt 键改成 `task-<uuid>-attempt-N`
→ 红同步与群聊两条（团队成员任务同样走这个线程键）；M4 排队入口借 `team-` 前缀
→ 红排队那条。还原 sha256 一致，复跑 3 passed。

**0.8.5 打包与产物点验（#142）。**
前端先过两道：`node --test` 211 pass / 0 fail，`tsc --noEmit` rc=0 且零输出；随后
`BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npx next build` 静态导出成功。
第一次 `npm run dist` **失败**，不是代码问题：打包器要从 GitHub 取 Electron，
`⨯ connect ETIMEDOUT 20.205.243.166:443`（本机没配代理）。绕行是拿本机缓存包
`electron-v38.8.6-win32-x64.zip` 解压到 `E:/tmp/m141/electron`，改用
`npx electron-builder --config.electronDist=E:/tmp/m141/electron` 重打，rc=0。
产物（2026-10-28 18:27）：`空白格-便携版-0.8.5.exe` 111,188,892 字节、
`空白格-安装版-0.8.5.exe` 111,409,001 字节、`空白格-安装版-0.8.5.exe.blockmap` 117,940 字节。
`node verify-package.cjs` rc=0，**33 条** `ok` 全绿（壳文件逐字节一致、包内 api-base 六种 env
同解、前端烘焙了绝对后端地址、含 /team 页与团队视图、包内源码无明文 Key 等）。
「包内是老代码」这条判定不是靠命令成功推出来的，而是三样实物：
① `diff -rq --exclude=__pycache__ services/api/src <包内>/resources/backend/api/src` 无差异（rc=0）；
② 包内 `team.py` 与仓库 sha256 同为 `352d063e…c51429`（两侧 `grep -c` 都是 2 处「这次没排上」）；
③ 对照格能红：`git show HEAD:…/team.py`（本批改动尚未提交的旧版）sha256 为 `13ab40da…5747c5`，
`cmp` 对包内与仓库**都返回 1**——包若还是老代码，这两条判定当场红。
没做、也不擅自做的两件：`npm run install:local` 会写用户本机的
`D:\Softwares\kbg\blank-ai-workbench-desktop`，属仓库外写入，留给用户裁决；
0.8.5 的「发布」（上传/分发/通告）同样没做，只产出并点验了本地产物。

**重打包暴露的一条过期判据（#142 附带）。**
全量回归红 1：`test_real_desktop_asar_answers_are_known` 断言「5 个文件」，0.8.5 产物答「6 个」。
不是解析器退化——包内 asar 的条目是 `api-base.cjs / gpu-guard.cjs / main.cjs / package.json /
preload.cjs / shell-policy.cjs`，与 `desktop/package.json` 的 `build.files` 逐项相同；
`gpu-guard.cjs` 自 `a1aac25`（2026-10-21）起就在 files 里。这条用例写在 10-27，量的是当时 dist
里的旧产物（上一轮全量回归它还是绿的，即旧 asar 确为 5 项），此后到这次重打包之间没有人再量过；
旧产物少的是哪一个文件没有在旧包上复测（安装包是压缩的，字符串检索不可用），不猜。
现在把字面量改成当刻实测的 6 个文件，并把「文件表」钉成精确集合（少一个、多一个、换一个都红）。
单跑 1 passed / 0.08 s；修正前那次红就是这条判据的对照格（同一份产物、同一个 report）。

**本批全量回归与提交。**
修正前全量：`1 failed / 1663 passed / 23 skipped`（455.23 s，`E:/tmp/m141/full_backend_wave5.txt`），
唯一红点就是上面那条 asar 过期判据；改完重跑：`1664 passed / 23 skipped`（432.96 s，rc=0，
`E:/tmp/m141/full_backend_wave5b.txt`）。上一批记过偶发红的
`test_run_lease_v102.py::test_a_non_owner_cannot_write_the_terminal_state` 在这两轮全量里都通过、
没有复现——判据不放宽，根因仍按「未定论」记。前端同批 `node --test` 211 pass / 0 fail、
`tsc --noEmit` rc=0。本批一批一提交、不 push。
