# opencode 参考报告（逐条对照 Kbgai）

> 调研对象：`github.com/anomalyco/opencode`（原 `sst/opencode`，GitHub API 的 `full_name` 已改为 `anomalyco/opencode`）
> 分支 `dev`，commit `d33b5783338b`（2026-11-19）。仓库已重构：不再有 `packages/opencode`，内核在 `packages/core/src`，模型层在 `packages/ai`，契约在 `packages/schema`。
> 本报告只读源码，不引用博客。带 `[S#]` 的句子来自第 7 节列出的文件与原文；标「判断」的是推断。
> 我们的基线：`E:/projects/kbgai/kbgai`，FastAPI + LangGraph + Python，关键路径 `services/api/src/blank_api/`。

---

## 0. 一句话结论：最值得抄的三件事

按「改了就能见效」排序：

1. **把压缩结果做成一条可回放的历史根，而不是一段拼进 prompt 的摘要。**
   opencode 压缩后写一条 `compaction` 消息，带 `metadata.keepID` 指向"逐字保留的最近一段"；之后所有历史读取都从这条消息开始，旧消息不再进入请求 `[S3][S10]`。我们现在的压缩是"在 messages 数组里折叠"：`context.compacted` 事件记了 window 与 replacement，但读取侧（`history_for`）不把它当边界。抄这个数据结构，痛点 4（自己写过的内容反复重发）才有确定性的边界。

2. **工具回执只写事实收据，不写内容。**
   `write` 返回 `Wrote file successfully: <path>`，`edit` 返回 `Edited <path> (N replacements)`；大输出超过 2000 行或 50 KiB 就截断，全文落盘，结果里写清"showing lines 1-N of M; full output saved to <file>" `[S4][S25]`。我们的 `editing.py` 对新文件返回整段 unified diff（`_redacted_diff("", text, ...)`），等于把模型刚写的全文又塞回历史一遍——这是痛点 4 的直接来源，改一个返回值就能止血。

3. **系统提示只留一个极小的壳；会变的东西做「基线 + 增量」。**
   opencode 的 `system.txt` 模板 756 字符，按当前暴露的工具拼上 1-4 条段落，全开时渲染约 1.47k 字符。日期、环境、AGENTS.md、技能、MCP、工具目录都是独立的 instruction source：首次全量、之后按哈希只发 diff，并用 epoch 重新基线化 `[S11][S22]`。我们的系统提示实测 10,133 字符（head 2,003 / coding 5,120 / tail 2,435 / style 574），且每轮重新组装、随每次请求发送（判断：取决于适配器是否缓存 system 消息）。痛点 2 的正解不是"下狠心删字"，而是把会变的部分搬出系统提示。

**必须同时说清的第 4 件：opencode 没有金额闸。**
它的 `cost` 只按步累计、只发布事件与展示 `[S12]`；`UsageLimits` 是只读的 Console 预算查询 `[S34]`。runner 与配置 schema 里都没有找到"花到 X 就停"的判断（这是负向证据，按"判断"看待）。所以痛点 1 没有现成答案，我们得自己补——而且要补在它没有的那一层（详见 2.8）。

---

## 1. 调研基准与读法

- 仓库迁移确认：`GET https://api.github.com/repos/sst/opencode` 返回 `full_name: anomalyco/opencode`、`default_branch: dev`，因此本文所有路径以 `anomalyco/opencode@d33b578` 为准。
- 代码规模：`recursive=1` 的 git tree 共 12,662 个条目（其中 11,578 个 blob），`packages/core` 968 个文件。本次精读了 40 余个与 agent loop / 上下文 / 工具 / 权限 / 子代理 / 存储 / provider 直接相关的文件，其余按目录名排除。
- 读法：先读入口（`session/runner/llm.ts`），再顺着调用读到 `step.ts`、`compaction.ts`、`tool.ts`、`permission.ts`，最后回到 `schema/*` 确认契约。所有结论都能落到具体文件与标识符。

---

## 2. 逐条对照表

图例：**能抄** = 语义可直接移植；**部分** = 只借判据或数据形状；**不能** = 依赖它的架构/前提；**我们已有** = 不需要抄。

### 2.1 agent loop：循环、停止、防打转

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| L1 | **两层循环**：外层 `drain` 是"一轮对话"，内层 `runStep` 是"一次模型请求 + 一批工具"；`drain` 里先 `advanceToStep()` 处理 inbox（用户插话，分 steer/queue 两种投递）、manual compaction、会话迁移，再 `runStep` `[S1][S33]` | LangGraph 节点 `execute_action` + 条件边；`agent_events` 落事件可回放（`services/agent/graph.py`） | 部分 | 不改成两层，但保持"一次模型请求 = 一个 step"：`execute_action_node` 现在是"执行上一批工具 → 请求模型"，这已经满足；要防的是以后在节点内加循环多轮 `ask`。建议在节点入口断言 `state["pending"]` 与 `tool_results` 的配对关系，重试走 L5（不新增 step）。 |
| L2 | **继续条件只有一个**：本次响应里出现过"本地执行的 tool call"（`needsContinuation = 任一 tool.called 或 settled`）。纯文本回复即收工，不靠步数判断 `[S1][S35]` | 我们已有等价判据：`route_after_act()` 看 `state["pending"]`（`pending = result["tool_calls"]`，预算耗尽时置空），空则离开执行节点（`graph.py:5663, 5706, 7057`） | 我们已有（判据一致） | 不重复实现。要防的是下游：`route_after_validation()` 在验证失败时会回 `execute_action` 修（`graph.py:7085`）。确认"纯文本回答、零工具调用、零改动"的轮次里验证是 `skipped`，否则会被拉回去再跑一轮。这正是 `verifier.py` 的"零改动伪成功"要拦的形态。 |
| L3 | **到步数上限不是硬停**：`step >= agent.steps` 时请求仍带全部工具定义（保住 provider 的缓存前缀），但 `toolChoice = "none"`，并追加一条 `MAX_STEPS_PROMPT`，要求只输出总结、不要再调工具 `[S1][S2]`。`agent.steps` 默认不设 = 不限步 `[S8]` | `DEFAULT_MAX_STEPS = 200`（`limits.py:10`），耗尽时丢弃本轮工具调用并注入收尾 prompt（`graph.py:5605-5645`）；但最后一次请求仍允许模型再调一轮工具，没有"只准写字"的硬约束 | 能 | `graph.py` 预算段已有 `pending_calls = [] if exhausted`，把它前移到"决定是否再请求"的位置：耗尽时直接给最后一条 `tool_choice="none"` 的请求，而不是先让模型再调一次工具再丢掉。`MAX_STEPS_PROMPT` 可直接借用它的结构（四条强制要求 + 必须包含的总结项）。 |
| L4 | **输出截断的续跑**：`finish=length` 且还有后续时，注入一条 synthetic「不要道歉/复述，把剩下的拆小」；连续 3 条 assistant 都是 `length` 则判 `output-limit` 失败 `[S1]` | 未见同等保护（判断） | 能 | `graph.py` 的 `call_model` 返回值里保留 `finish_reason`；记一个 `length_streak`：第 1 次 length 注入"拆小"继续，第 2 次仍 length 就转为交付并说明"输出被截断"，不要再无限续。 |
| L5 | **重试按错误分类**：`SessionRunnerRetry` 只看 `isRetryable(cause)` 与"流已开始但中断"；重试是同一 assistant 消息的状态迁移，不是新开一步 `[S1][S27]` | 有 `auto_recoveries` 与模型适配器重试（`services/agent/graph.py`） | 部分 | 借"重试不新增 step"这一点：现在如果重试被算进步数，长任务的有效步数会被网络抖动吃掉。在 `agent/usage.py` 的落账处把 `retry` 标记带上，预算只数有效步。 |
| L6 | **防打转分两层**：runner 层没有启发式；`goal` 插件在"本该 idle"时用一个 reviewer 输出 `done/continue/blocked/paused/wait`，同一理由连续 3 次或本轮没有任何工具调用 → 暂停；最多 50 轮；等后台超过 30 分钟 → 暂停 `[S28]` | 已有更强的确定性断路器：同签名失败 2 次、同类失败 3 次、环境缺陷 1 次即拦（`services/agent/stall.py`） | 部分（只抄结构，不抄模型判定） | 我们的工具级断路器比它的模型判定更可靠，不要替换。可抄的是**回合级**的"同一理由连续 N 次"：把 `verdict.reason` 换成我们已有的 `budget.exceeded` / `stall.blocked` 原因串，连续 3 次相同就暂停并交付，而不是继续换命令重试。 |
| L7 | **中断是协作式的**：用户 stop 先触发每个工具的 `cancel`，5 秒内没停再 `interruptAll`；被中断的 tool call 统一结算成 `aborted`，不会留下"永远 running"的消息 `[S2]` | 有取消/暂停语义（`graph.py` 的 `is_cancelled` / `paused`） | 能 | 抄"先请求、后强杀、最后统一结算"的顺序：在 `graph.py` 的取消分支里，先给正在跑的工具发 cancel 事件，超时后再杀进程，并把未结算的 `tool_call` 补一条 `status=aborted` 的终态，避免恢复时被 `_replayed_tool_results` 误判。 |

### 2.2 上下文管理：阈值、保留、摘要、读取边界

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| C1 | **阈值 = 窗口 − max(15%, 16k)**；`buffer` 可配 `"20%"` 或 token 数。触发判断用 `measured + estimated`：`measured` 取最近一次 provider 上报的 usage（input+cache read+write+output+reasoning），`estimated` 只估它之后的文本 `[S3]` | 82% 水位 + 16k 预留；空闲压缩 40 步；阈值随窗口缩放（`services/agent/context.py`） | 能 | 把"最近一次上报的 usage 作为锚点、只估增量"搬进 `context.py`：现在若每次都用字符估全量，长会话会系统性低估（旧 reasoning 被 provider 丢掉时）。锚点还要校验 `providerID/model/variant` 一致，否则不可信。 |
| C2 | **保留最近 15k token 逐字不动**，且切点必须落在 user 消息上，保证 assistant 的 tool call 与结果不被切开 `[S3]` | 压缩后保留最近若干轮（`context.py`） | 能 | `context.py` 的切点函数加一条硬约束：`start` 必须是 user 消息索引；如果落在 assistant/tool 上，向前回退到最近的 user。 |
| C3 | **摘要模板固定七段**：Objective / Requirements / Decisions / Work State（Completed/Active/Blocked）/ Next Move / Relevant Files（≤15 个）/ Important Context；并明确要求"不要复述 AGENTS.md、环境、sessionID，下一个 agent 会单独收到最新版" `[S3][S23]` | 有压缩提示词（`context.py` 约 101 行起） | 能 | 直接采用这七段，尤其两条规则：① `Relevant Files` 限 15 个、只写"为什么重要"；② 明确禁止把指令文件写进摘要（我们现在的摘要会把系统提示内容再复述一遍，等于压缩后仍背着两份）。 |
| C4 | **压缩后历史从这条 compaction 起读**：`messageEntries()` 先找最近一条 `status=completed` 的 compaction，再 `seq >= compaction.seq` 读消息；旧消息根本不进请求 `[S10]` | 已有 `context.compacted` 事件（`window_id` / `replacement` / 摘要，`graph.py:1710`），但它只是"事后记录"；重建仍靠消息数组与最近 N 轮（`services/threads.py:217` 的 `history_for`），压缩边界不参与读取 | 能 | 不用新增事件类型，改读取侧：`history_for()` 先找最近一条 `context.compacted`，从它记录的"保留起点"之后读；事件里补 `keep_task_id + keep_seq` 作为锚点。这样压缩从"记账"变成"历史根"，也天然可分叉、可审计。 |
| C5 | **后台预压缩**：距上限还有 60k token 就开始在后台写摘要，写完"持有但不应用"；等真正触发时再换入，避免提前换入导致前缀缓存失效。换入时尾部超过预算 40% 就丢弃重写 `[S3]` | 无（判断） | 部分（先不做默认开） | 我们的 token 闸已经会提前收尾，后台预压缩的收益是"压缩那一刻不用等 30-60 秒"。若要抄，必须默认关闭：它明确会产生"永远不会被用到的摘要"的 token 成本（配置注释自己写了这一点）`[S36]`，在我们没有金额闸时是风险。 |
| C6 | **overflow 收缩阶梯**：provider 报"太长"后，下一次按被拒请求大小的 85% → 70% → 50% → 35% 逐级重试，每级从原始 transcript 重新裁剪，不是在上次结果上继续削 `[S3]` | 有溢出重试（判断） | 能 | `context.py` 的溢出分支加一个 `shrink_steps` 数组；关键在"每级重新裁"——在已裁过的结果上再裁会丢失"哪段是原文"的信息。 |
| C7 | **摘要请求自身也会太长**：把会话压成纯文本时按 5 级降级——整段 → 截断工具输出 → 去掉工具输出并截断输入 → 只留用户文本 → 整段省略；每一轮所有 exchange 都升一级后才动下一个，最老的先降，最新的永不省略 `[S3]` | 未见分级降级（判断） | 能 | `context.py` 里加 `flatten_level`；这条对"25 分钟测试日志"这类超大工具输出特别有效，且能保证最新的那一轮一定进摘要。 |
| C8 | **大输出在进历史前就截断**：2000 行 / 50 KiB 双上限，取二者更早的切点；全文写 `tool-output/tool_<id>`，保留 7 天；结果里插一行 `[showing lines 1-N of M; full output saved to <path>]` `[S4]` | 网关按字符上限截断 + 落 artifact + 插 note（`services/tools/gateway.py:630-690`） | 部分（补双上限） | 现在是单一 `MAX_RESULT_CHARS`。补"行数"维度：一行 10 万字符的 JSON 和 2000 行正常日志，字符上限对前者切得太晚、对后者切得太早。`gateway.py` 的 `oversize` 判断里同时算 `line_count`。 |
| C9 | **图片单独记账**：请求内图片总量超过 25 MiB 就从最老的开始丢，降到 15 MiB；被丢的位置替换成一段明确文案「这张图已移除，不要凭记忆断言内容，需要就重新取」`[S20]` | 视觉代理 + 图片直传（`services/agent/vision_proxy.py`） | 能 | 抄"显式占位文案"，不要静默删图。我们的网关已经因为截断 base64 踩过 400 的坑（`gateway.py:631` 注释），这条是同一类问题的通用解。 |
| C10 | **指令增量**：instruction source 以 canonical JSON 哈希为键；首次渲染进系统提示做基线，之后每次只把变化的 source 渲染成一条"更新"消息插进历史；`epoch_start` 之后旧更新被基线吸收，避免无限增长 `[S11]` | 五层指令（system/user/project/harness/task）每轮全量 `compose()`（`services/instructions.py:148,205`） | 能 | 见 2.9 的专项，这是痛点 2 的核心改法。 |

### 2.3 工具契约：描述、按需加载、结果回灌、错误表达

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| T1 | **工具是插件注册的**：每个工具声明 `name/description/input schema/output schema/execute`；注册时把 schema 编译成 `ToolDefinition`，执行时先做入参校验，失败直接产 `Tool.Error` `[S5][S31]` | `ToolDefinition` + `registry.py` + 网关（`services/tools/`） | 我们已有 | 不迁移架构。可借"output schema"这一层：我们多数工具只声明入参，回执形状靠约定；给 `base.py` 的 `ToolDefinition` 加可选 `output_schema`，让"回执只写收据"能被测试断言。 |
| T2 | **按模型裁工具**：新 GPT 系（非 gpt-4）隐藏 `edit/write`、改用 `patch`，其余 GPT 隐藏 `patch`；在 full code mode 下，GPT/Anthropic 隐藏 `glob/grep/webfetch`（它们用 shell 更快）`[S6]` | 按场景过滤 + `tool_search` 按需加载（`services/tools/registry.py`） | 能 | 在 `registry.py` 的可见性判断里加"模型族"维度：对擅长 shell 的模型（claude/新 GPT）默认收起 `glob/grep`，对弱模型保留。这是纯收益：少 2-3 个工具说明，行为不变。 |
| T3 | **工具目录有 token 预算**：Code Mode 把工具收进一个 `execute`，目录正文只给 2000 token；放不下的只留命名空间行 + 一句"用 `search()` 查"，并明确"不要猜工具名" `[S26]` | 工具说明有字符预算（实测 25,355 → 21,935 字符），场景族按需加载（`registry.py:125-136`） | 部分 | 我们已经是同一思路。差在"预算用完后怎么表达"：现在是整族不出现，opencode 是"列出命名空间 + 给出检索入口"。在 `registry.py` 的预算裁剪处，为被裁掉的族保留一行 `- <family> (N tools, none shown)`，比彻底消失更容易让模型知道"还有东西可查"。 |
| T4 | **大输出**：见 C8。 | 见 C8。 | 部分 | 见 C8。 |
| T5 | **写类工具只回执**：`write` → `Wrote file successfully: <path>`；`edit` → `Edited <path> (N replacements)`；都不回内容、不回 diff `[S25]` | `editing.py` 返回 `{path, diff, changed_lines, replacements}`，新文件时 diff 就是全文，且 `_redacted_diff` 无长度上限（`services/tools/editing.py:41-44, 300-310`） | 能 | 改成：`write` 新文件返回 `{path, bytes, lines, sha256}`；`edit` 返回 `{path, replacements, changed_lines, diff_excerpt}`，其中 `diff_excerpt` 截断到 2,000 字符并注明"完整 diff 在 artifact"。这是痛点 4 的第一刀。 |
| T6 | **参数错误的回执是"可执行的"**：列出前 5 条 issue（JSON path + message）、附上模型实际传的 JSON、最后一句"Update the arguments and call the tool again" `[S31]` | 网关有参数校验错误（判断），格式未统一 | 能 | 把 `gateway.py` 的校验失败分支统一成这个三段式。模型最常见的失败是"猜参数名"，给它看自己传了什么，比只说"缺少必填参数"少一轮。 |
| T7 | **错误分三类**：`Tool.Error` 是模型可见的工具失败；用户拒绝（`Permission.DeclinedError`）以 defect 隧道穿过，禁止被工具 catch 成正常输出；provider 侧 tool result error 统一转 `tool.execution` `[S2][S20][S31]` | 有 `ToolOutcome.status` 与审批拒绝（`services/tools/base.py`） | 能 | 关键在"拒绝不能被工具转成成功"：在 `gateway.py` 捕获异常时，先判 `ApprovalDenied` 并强制 `status=denied`，不允许工具自己返回 `ok=True`。 |
| T8 | **工具并发、按来源定序**：provider 流里每来一个 tool-call 就 fork 一个 fiber 执行，事件按来源保序、不共享写队列；本地工具只在 `Tool.Called` 发布后才开始跑 `[S2]` | 有并行工具执行（`graph.py` 的 `_parallel_ok`） | 部分 | 借"先发布调用事件、再执行"：保证前端看到的顺序与真实执行顺序一致，崩溃恢复时也能用事件判断"哪些真的开始过"。 |

### 2.4 权限与审批：默认档、颗粒度、审批疲劳

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| P1 | **默认几乎全放行**：agent 默认规则是 `*:* allow`，只对 `read *.env` / `*.env.*` 要求 ask（`*.env.example` 放行）`[S8]` | 四档 default/acceptEdits/plan/bypass + 前缀规则 + 会话记忆（`services/permissions.py`） | 不能（作为默认） | 我们的产品前提是审批优先，不要抄这个默认。可抄的是"例外清单"的形状：默认档里对 `.env` 这类高敏路径单独 ask，而不是靠模型自觉。 |
| P2 | **规则求值是 last-match-wins**：规则按顺序展开，取最后一条同时匹配 action 与 resource 的；没有匹配则默认 `ask`（不是 allow）`[S7]` | 前缀规则 + 会话记忆（`services/permissions.py`） | 能 | 检查我们的求值顺序：如果现在是"任一 allow 即放行"，改成"最后一条匹配者胜"，否则"先 allow 后 deny"的规则永远不生效。默认值必须是 `ask`，不是 `allow`。 |
| P3 | **审批三档 + 带反馈拒绝**：`once` / `always` / `reject`；`reject` 可带 message，带 message 时变成 `CorrectedError`，模型看到反馈后继续，而不是整步中断 `[S7][S29]` | 审批卡支持 once/prefix/tool 三档（`services/approvals.py`） | 能 | 我们已有三档，缺"拒绝并给理由"：用户点"拒绝"时允许填一句"用 pnpm 不要用 npm"，把它作为 tool result 回给模型。若现在的拒绝会让整步失败（判断），用户就得重发一遍——这是可以省掉的一轮。 |
| P4 | **`always` 保存的是"具体资源模式"**：shell 用词法解析生成 `save`——已知命令按内置 arity 取子命令（最多 3 个词，如 `npm run test *`），未知命令退化成首词 + `*`（如 `terraform *`）；read/write 按路径。保存后写入 `PermissionSaved`，下次直接 allow `[S7][S37]` | 会话记忆按前缀/工具保存（`services/permissions.py`） | 部分 | 粒度要对齐：我们的"tool 档"太粗（整个工具永远放行），opencode 最粗也是"命令名 + 参数模式"。建议把 `tool` 档改成"该工具 + 本次资源模式"，并把 `save` 的生成逻辑集中到一个函数（shell 词法解析），不要每个工具各写一套；未知命令要先问一次完整命令行，再决定是否记住。 |
| P5 | **批量语义**：同一 assistant 消息里并行的多个审批，用户拒一个 → 同批全部拒；用户选 `always` → 保存规则后**重新评估同批待审请求**，已被新规则覆盖的自动放行，不再弹第二张卡 `[S7]` | 未见同批重评估（判断） | 能 | 这是审批疲劳的最大来源之一。在 `approvals.py` 的 resolve 路径后加一次"对同 message 的 pending 重新 evaluate"，命中新规则就直接 settle。需要审批请求带 `message_id`（若没有，先补这个字段）。 |
| P6 | **无人值守**：`Unattended` 规则集只用于"没人在场"的场景；它允许的才放行，不允许的一律 deny，不会退化成 ask `[S7]` | 有自动审批策略（`services/approvals.py` / `approval_rules.py`） | 能 | 明确我们的自动策略在"没人"时的默认值必须是 deny；现在若有"超时自动放行"的分支（判断），要改成 deny + 记录原因。 |
| P7 | **Plan 是 agent + 权限**：`plan` agent 对 `edit` 全 deny，只允许写 `~/.opencode/plan/*`；并用每轮注入的 `<system-reminder>` 维持模式，压缩丢提醒后会重新补 `[S9][S38]` | 有 plan 档（`services/plan_mode.py`） | 能 | 抄"压缩后重新注入模式提醒"：我们的 plan 档如果只靠第一条系统消息，压缩后就失效了。在 `context.py` 组装每条请求时检查"当前是否 plan 且最近 N 条没有模式提醒"，缺则插一条。 |
| P8 | **只读子代理用 allowlist**：`explore` agent 先 `*:* deny`，再逐条 allow `shell/grep/glob/webfetch/websearch/read/external_directory`，`.env` 仍 ask `[S9]` | 有子代理权限（判断） | 能 | 抄"先全拒、再逐条放"的写法，而不是"给只读工具列表"。前者新增工具时默认不可见，后者会默认漏放。 |

### 2.5 子代理：怎么起、怎么收、上下文边界

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| A1 | **子代理 = 子会话**：`sessions.create({parentID})`，子会话有自己的 agent/model/权限；新子会话从零上下文开始，prompt 首行是 "You are a subagent spawned by another session." `[S18]` | `services/agent/subagent.py` + `delegate`（判断：是否独立会话需确认） | 部分 | 若我们现在是"同一消息数组里插一段子任务"，改成独立 session 收益很大：子代理的中间过程不会污染父上下文，父只收到最终报告。 |
| A2 | **默认后台执行**：调用立刻返回 `{sessionID, status:"running"}`，并明确告诉模型"不要 sleep、不要 poll、不要重复它的工作"；子会话 idle 时向父会话发一条 `<subagent sessionID=... state=...>` synthetic 消息 `[S18][S19]` | 有 `delegate`（判断是否同步等待） | 能 | 抄"异步 + 完成通知 + 禁止轮询"三件套。注意：凡是"必须等子代理结果才能验证"的任务（如跑测试），要允许显式 `wait`，否则父会在子完成前交付。 |
| A3 | **深度限制默认 1**：`experimental.subagent_depth` 默认 1，超过就 `ToolFailure`，把 "Increase ... to allow nested subagents" 告诉模型 `[S18]` | 有并发上限（判断） | 能 | 在 `subagent.py` 的派生入口加 `depth` 参数并写入会话元数据；默认 1 层，防止"子代理再派子代理"指数爆炸。 |
| A4 | **权限按 agent 走**：子代理不能是 `primary`；派生时对 `subagent` action 本身要审批，`save` 记住 agent id `[S18]` | 有子代理工具（判断） | 能 | 给 `spawn` 加一条审批：允许"这个 agent"而不是"允许派生"。第一次批准后可记住 agent id，避免每次弹卡。 |
| A5 | **报告带后台残留**：子代理完成时，如果它还有后台 shell/子代理在跑，报告里附 "Background work still running: ..." `[S19]` | 未见（判断） | 能 | 交付四行里加一行"仍在后台运行的任务"，否则父代理会把"子代理已结束"误读成"所有工作已完成"。 |

### 2.6 会话存储、恢复、分叉

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| S1 | **SQLite 四张核心表**：`session_v2`（含 cost/tokens/revert/permission/model/resume_attempts）；`session_message`（`(session_id, seq)` 唯一索引，读取按 seq 排序）；`session_part`（assistant 内容分片，`(message_id, ordinal)` 主键）；`session_inbox`（待投递输入） `[S13]` | `agent_events` + 任务/消息表（`services/api/src/blank_api/models/`） | 部分 | 不必换表。可借 `session_part` 的动机：assistant 的长内容（reasoning、tool 参数流）分片存储，读消息时按 ordinal 拼；我们现在整条 JSON 存，单条变大后读写都变慢。 |
| S2 | **事件投影**：durable 事件经 `projector` 写入消息表；同一事件重复投递会命中 `SessionAlreadyProjected`，不会写两遍 `[S12]` | `agent_events` 可回放（`services/agent/events.py`） | 我们已有 | 我们的事件账已经是同一形状。可借"投影幂等"的显式错误类型：重复应用事件时返回一个可识别的 `AlreadyProjected`，而不是靠业务判断。 |
| S3 | **写前声明（claim）**：执行开始时把 `time_suspended` 置位，终态释放；进程崩溃留下的 claim 就是"这一轮没跑完"的证据，下次启动恢复。恢复次数单独计数 `resume_attempts` `[S14][S30]` | 有崩溃续跑 / `auto_recoveries`（`services/agent/restart.py`） | 能 | 把"是否正在跑"从内存布尔改成持久字段：现在服务重启后无法区分"空闲"与"跑到一半崩了"。在 `models/task.py` 加 `running_since`，启动时扫描并恢复。 |
| S4 | **恢复时先结算脏状态**：`settleStaleCompactions` 把 `running` 的压缩标失败；`settleStaleToolCalls` 把 `streaming/running` 的工具标 `aborted`，并带上子会话 id `[S1]` | `_replayed_tool_results` 按事件找已完成的工具（`graph.py`） | 能 | 在恢复入口补一次"扫尾"：所有 `status=running` 的 tool_call 一律标 `aborted`，不要留给模型"它还在跑"的错觉。我们现在靠重放工具结果，边界更窄。 |
| S5 | **分叉按消息边界**：`fork(boundary: {messageID, type: before/after})`，只复制边界之前的**已结算**消息；消息 id 重映射（`<new>_<seq>`），压缩的 `keepID` 同步重映射；新会话 cost 归零、标题加 `(fork #N)` `[S12]` | `services/fork.py`：只复制最新 checkpoint，两种 trunk 策略（reuse/new） | 能 | 这是明确差距。加"按消息分叉"：复制 `seq <= boundary` 的消息；重点是把所有"指向旧消息 id"的引用（压缩 keep_id、检查点、审批 source）一起重映射，漏一个就会出现"分叉后压缩指向不存在的消息"。 |
| S6 | **回滚分 stage/commit**：`revert.stage` 先按每条 assistant 的 `snapshot.start` 找出要恢复的文件并落盘；`commit` 只提交边界；`clear` 撤销恢复 `[S15][S17]` | 有快照与回滚（`services/agent/checkpoints.py` + snapshot 相关） | 部分 | 借"stage 与 commit 分离"：用户点"回滚"时先预览（恢复到临时区/只算 diff），确认后才真正写盘。我们现在若是直接恢复，误点无法撤销。 |
| S7 | **每步快照用独立 git 仓库**：不碰用户的 .git，快照对象放在全局数据目录的 `snapshot/<project>/<hash>`（`global.data`，不是项目内）；只捕获项目范围内的文件，忽略规则沿用源仓库；单文件未跟踪上限 2 MiB `[S16]` | 有快照档位（判断实现方式） | 部分 | 若我们的快照是"整目录复制"，改用 `git` 的 content-addressed tree 能省大量磁盘；关键是"独立 git 目录"，不能用用户仓库的 index。 |

### 2.7 provider 抽象：多模型、能力声明

| # | opencode 怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（具体改哪里） |
|---|---|---|---|---|
| V1 | **目录驱动**：模型来自 models.dev 快照 + 远端刷新；每个模型声明 `capabilities{tools,input,output,freeform,temperature}`、`cost[]`（含上下文阶梯价）、`limit{context,input,output}`、`variants[]`、`package`（对应一个 provider npm 包） `[S21]` | 有多 provider 适配（`services/models/`），能力靠适配器各自声明（判断） | 能 | 把"能力"收敛成一张显式表：`supports_tools / input_modalities / output_modalities / context_limit / output_limit / price`。现在这些散落在适配器里，新增模型容易漏。`services/models/pricing.py` 已经有价格表，可以直接并进去。 |
| V2 | **输出预算按剩余窗口算**：`outputLimit = min(目录上限, 窗口 − measured − estimated×1.15)`，下限 1024、上限 256k；主请求再留 1% headroom `[S20]` | 有 `max_tokens` 适配（判断是否按剩余窗口算） | 能 | 在 `call_model` 组装请求时，用"窗口 − 已用"算 `max_tokens`，而不是固定值。否则长上下文 + 固定 8k 输出会在末尾被 provider 400。 |
| V3 | **不支持的多模态转成文字**：模型 `capabilities.input` 不含 image 时，图片位置替换成 `ERROR: Cannot read "x.png" (this model does not support image input). Inform the user.` `[S20]` | 视觉代理：主模型看不了图时调另一家模型代看（`services/agent/vision_proxy.py`） | 我们已有（更前） | 我们在这条上领先，保留。可借它的"显式失败文案"作为兜底：视觉代理失败时不要把图静默删掉，插一条"图未读、原因是 X"，否则模型会凭记忆编。 |
| V4 | **prompt cache 是显式设计**：请求带 `promptCacheKey` 与 `cache.ttl`；系统段按"最共享 → 最具体"排序（用户级 → 项目级 → 日期/环境），让前缀稳定；压缩/后台摘要都刻意保持前缀不变以复用缓存 `[S20][S32]` | 有缓存统计（判断），未见前缀排序约束 | 能 | 在 `compose_system_prompt_parts` 的注释里已经写了"要能删"，但没写"顺序要稳定"。把 parts 的顺序固定为：安全/审批 → 产品规则 → 输出风格 → 场景/环境 → 任务级指令；任何随时间变化的内容（日期、步数）必须放最后或移出系统提示。 |
| V5 | **原生压缩（provider compaction）**：模型声明 `compaction.type = "native"` 时，用 provider 的加密窗口替换历史；该窗口只能在生成它的模型上重放 `[S3][S10]` | 无（判断） | 不能（现在） | 它是"状态取代历史"的极端形态，但不可跨模型、不可审计。我们做多模型路由，抄它会制造"换模型后历史读不懂"的坑。 |

补三条它做 provider 抽象的骨架（判断：这三点比"支持多少家"更重要）：

- **接一家 provider = 加一个包 + 一条目录项**：模型条目的 `package` 指向一个 npm 包，运行时按 `Provider.packageName()` 动态加载；AISDK 包与自有协议包走同一条 `model(id, settings)` 接口 `[S39]`。内核里没有 `if provider == ...` 的分支。
- **能力是数据，不是代码分支**：`capabilities.tools/input/output/freeform/temperature` 决定"发不发工具、发不发图、用不用 freeform 补丁格式"；`compatibility` 再把"同一模型经不同 API 的差异"（reasoning 字段名、max_tokens 字段名、media 输入方式）收敛成配置 `[S21][S39]`。
- **变体（variant）是同一模型的不同请求参数**：`variants[]` 只覆盖 settings/headers/body，不动 prompt cache 归属 `[S21]`。我们的"推理强度/思考档"可以按这个形状建模，而不是再造一个模型 id。

### 2.8 五个痛点总表

| 痛点 | opencode 有没有解 | 事实依据 | 我们该做什么 |
|---|---|---|---|
| 1. 没有成本上限 | **没有**（token 有闸，金额没有） | 它按步记录 `cost`、会话累计 `cost` `[S12]`；`UsageLimits` 只读 Console 预算 `[S34]`；runner 与配置 schema 里没找到金额阈值（负向证据，判断）。我们这边：`graph.py:5549-5603` 已有 steps + token 双预算（含子代理合计），耗尽时注入收尾 prompt 并把本次工具调用丢掉（`pending_calls = [] if exhausted`），是真闸；但 `estimate_cost` 只被 `api/routes/observability.py:182` 与 `api/routes/providers.py:277` 调用，agent loop 里没有金额口径 | 自己补金额闸：① 在 `services/agent/usage.py` 的落账点同时累加 `estimate_cost`；② 在 `graph.py` 预算段加 `cost_limit`，复用现有 `budget.warning → budget.exceeded` 两段式（含"丢掉本轮工具调用"）；③ `pricing.py` 未收录模型必须显示 `priced=False`，此时禁止开启金额闸（否则 0 价会让闸永远不触发） |
| 2. 系统提示只增不减 | **结构上有解** | 它的壳模板 756 字符、全开约 1.47k `[S22]`；日期/环境/指令/工具目录都是 instruction source，首次全量、之后 diff `[S11]` | 两步：① 用已有的 `compose_system_prompt_parts_sizes()` 做"按段计费"，先砍 5,120 字符的 coding 段里可被工具/权限强制的部分；② 把 AGENTS.md、环境、日期、技能清单搬出系统提示，做成带哈希的"基线 + 增量"（见 2.9）。 |
| 3. 钩子失败 fail-open | **没有**（它是 fail-closed） | 它的 hook `trigger` 直接 `yield* result`，不 catch；hook 失败会让该步失败；`tool.execute.before` 是唯一允许失败（用来拒绝调用）的钩子 `[S27]` | 分类：把钩子拆成 `advisory`（失败放行，只记录）与 `guard`（失败即拒绝）。`services/hooks.py` 的 `HookResult.allowed()` 现在只看 `decision=="deny"`；给 guard 类事件加"status != ok ⇒ deny"的判定。安全类钩子（pre_tool_use 的写盘/网络/密钥规则）必须归 guard。 |
| 4. 长任务上下文膨胀 | **部分有解** | 压缩后从 compaction 起读 `[S10]`；写类工具只回执 `[S25]`；大输出截断 + 落盘 `[S4]`。但它没有"把历史里的文件内容替换成路径"这类去重（判断） | 三刀：① `editing.py` 的 write 新文件不回全文 diff；② `gateway.py` 增加"同一文件重复读取"检测，第二次起回"内容未变，引用 artifact X"；③ 把已有的 `context.compacted` 当历史根，`history_for()` 从它之后读（C4）。 |
| 5. 模型爱跑全仓测试 | **有解（换了一种）** | 默认 2 分钟；超时**不杀**，移到后台并返回 `shellID` + "You will be notified"，并在工具结果里明令"不要 poll" `[S24]` | 我们现在是 120 秒超时即失败（`schemas/settings.py:40`），工作白丢。改成：超时后转后台（`shell_tasks` 已有能力），返回 `shell_id` + 一段"会在完成时通知，不要轮询，先做不依赖它的事"；同时保留 `tools/testing.py` 的"这条命令在这台机器上跑不完"记忆。 |

### 2.9 专项：系统提示与指令增量（痛点 2 的可执行方案）

opencode 的做法拆开是三件事：

1. **壳很小**：`system.txt` 模板 756 字符，全开渲染约 1.47k，只有身份、markdown 约定、并行建议、沟通、改动纪律；工具相关的那几句由 `render(prompt, tools)` 按"当前暴露了哪些工具"条件拼接 `[S22]`。
2. **会变的东西不是系统提示**：日期、环境（工作目录、是否 git、平台）、AGENTS.md、技能、MCP、Code Mode 目录，全部是 `Instructions.Source`，有 `key + codec + read + render{initial,changed,removed}` `[S11]`。
3. **只发增量**：`InstructionState` 用 `initial_values` 做基线、`current_values` 记当前、`epoch_start` 标记基线起点；变化时渲染一条 `InstructionsUpdated` 事件进历史，内容只是 diff；epoch 之前的历史更新会被基线吸收，所以不会无限增长 `[S11]`。

对应到我们（判断 + 可执行）：

- `services/instructions.py` 现在有 `load_layers()` 与 `compose()`，每轮把五层全量拼进系统提示。加一层 `InstructionState`：把每层文件的内容哈希存进任务/会话表；首次写基线；之后只在变化时往 `messages` 里插一条"指令更新"（不要改系统提示）。
- 环境与日期从 `compose_system_prompt_parts()` 里移出。它们现在在 tail 段（2,435 字符的一部分），每轮都变的部分还会破坏前缀缓存。
- 先做"可测量"：`compose_system_prompt_parts_sizes()` 已经能按段报字符数，把它接进账单事件（工具说明 vs 消息各占多少字符已经有类似口径），这样"删哪一段"有据可依。

---

## 3. opencode 的 agent loop 单步流程（文字顺序图）

一次 `drain`（一轮对话）内部：

```
drain(sessionID)
├─ 若无新输入且非强制 → 直接结束
├─ 结算上次崩溃留下的 running 压缩 / running 工具调用          [S1]
├─ advanceToStep() 循环
│   ├─ 位置关闭？→ 返回 Reloaded（换 Location 继续）
│   ├─ 取 inbox 下一条可提升输入（steer 优先，其次 queue）
│   │   ├─ 类型 = compaction → 手工压缩，完成后 continue
│   │   └─ 类型 = move → 关闭传输、发布 Moved，结束本轮
│   ├─ 没有可提升输入且会话将 idle
│   │   ├─ 触发 experimental.idle 钩子（goal 插件在这里决定是否续跑）
│   │   └─ 仍无输入 → Complete
│   └─ prepareContext()：select（session/agent/instructions/tools）→ load（model/history）
│       └─ 有提升的输入 → 重置 step = 1
├─ runStep(context, step)
│   ├─ estimatePrompt（measured 锚点 + estimated 增量）
│   ├─ compaction.compact({reason:"auto"})                        [S1][S3]
│   │   ├─ skipped → 继续
│   │   └─ completed → 新建 assistantID，重新 prepareContext（从 compaction 起读历史）
│   ├─ step >= agent.steps ? 追加 MAX_STEPS_PROMPT + toolChoice="none" : 正常
│   ├─ 组装 transcript（system = agent.system 或约 1.5k 的壳 + 指令基线）
│   ├─ context.request.primary(...)                               [S20]
│   │   ├─ session 钩子可改写 messages/tools（plan/goal 在这里插提醒）
│   │   ├─ 计算 outputLimit = min(目录, 窗口 − 输入)
│   │   ├─ 不支持的多模态 → 文本占位
│   │   ├─ 图片超 25MiB → 从最老丢到 15MiB
│   │   └─ boundImages / 去 metadata / promptCacheKey
│   └─ steps.attempt(...)                                         [S2]
│       ├─ fork 起始快照（与 provider 请求并行）
│       ├─ 流式接收 LLM 事件，逐条发布
│       ├─ 遇到本地 tool-call → fork 工具 fiber
│       │   ├─ 权限 evaluate/ask（可能挂起等用户）
│       │   ├─ 执行 → truncate（2000 行/50KiB，全文落盘）
│       │   └─ 发布 Tool.Success / Tool.Failed
│       ├─ 等所有工具结束，分类：decline / interrupted / failure
│       ├─ 若流因"太长"失败且尚无输出 → compact(overflow) → Outcome.Compacted
│       ├─ 若可重试错误 → Outcome.Retry（带 delay/attempt）
│       └─ 结束快照 → diff 文件 → 记录 usage/cost → 发布 Step.Ended
└─ 根据 Outcome 决定
    ├─ Completed(needsContinuation=true) → step++，回到 runStep
    ├─ Completed(false) → 本轮结束
    ├─ OutputLimit → 注入"继续但拆小"或连续 3 次则失败
    ├─ Retry → 等待后同一 step 重试
    └─ Compacted → 重新准备上下文
```

**停止条件（全部）**：① 本次响应没有本地工具调用；② 用户 stop / 中断；③ `agent.steps` 用尽（只准写字）；④ 连续 3 次输出被截断；⑤ 压缩失败且无法恢复；⑥ 权限拒绝导致整步中断（同批拒绝）；⑦ goal 插件判定 done/paused。

对照我们：`graph.py` 的停止条件已经有 ①（`pending` 为空）和 ②（取消/暂停）、步数与 token 预算；缺的是 ③（步数用尽时"只准写字"的收尾请求）和 ④（`finish=length` 的续跑保护）。

---

## 4. 上下文在他们眼里是"会失效的资源"：具体做法

这一节只列**机制**，每条都能落到源码：

**度量**
- 不用字符数当唯一真相：`measured`（最近一次 provider usage）是锚点，只有它之后的文本才 `estimated`；锚点还要匹配 provider/model/variant，换模型后旧数字作废 `[S3]`。
- 估算字符 → token 用 4:1，并显式给 15% 误差余量；输出预算按 `estimated × 1.15` 扣 `[S20]`。

**预算**
- 预留 = max(窗口 15%, 16k)：留给"下一次回复"和"摘要本身" `[S3]`。
- 摘要请求的输出至少 32k，但如果窗口不够就按剩余空间降 `[S3]`。
- 2000 行 / 50 KiB 的工具输出上限；图片 25 MiB → 15 MiB `[S4][S20]`。

**替换（用更小的表示取代原文）**
- 压缩：七段式摘要 + 最近 15k token 逐字保留；摘要里禁止复述指令文件 `[S3]`。
- 读历史从 compaction 起，旧消息不参与请求 `[S10]`。
- 摘要请求本身降级：整段 → 截工具输出 → 去工具输出 → 只留用户文本 → 省略，最老先降、最新不省 `[S3]`。
- 工具回执化：write/edit 不回内容 `[S25]`；大输出回"预览 + 路径" `[S4]`。

**保全（为了缓存与可恢复）**
- 系统段按"最共享 → 最具体"排序；压缩与后台摘要都尽量保持前缀不变 `[S3][S32]`。
- 指令用"基线 + 增量 + epoch"，不重写系统提示 `[S11]`。
- 会话可 fork 到任意消息边界，压缩的 keepID 跟着重映射 `[S12]`。

**丢弃（明确、可见）**
- 图片被丢时插一段"图已移除，不要凭记忆断言" `[S20]`。
- 摘要降级时插"[N older exchanges omitted]" `[S3]`。
- 工具输出截断时插"[showing lines 1-N of M; full output saved to ...]" `[S4]`。

**他们没做的（判断）**
- 没有"同一文件内容在历史里只留一份"的去重；重复读取同一文件仍会重复进历史，只能靠压缩兜底。
- 没有"用文件路径替换历史里的文件内容"；只有工具回执足够短时才不膨胀。
- 没有按"信息价值"淘汰消息，只有按时间/预算。

这三点正好是我们的机会：`stall.py` 已经在做"这次调用会不会带来新信息"的判定，可以往前一步，把"同一文件重复读"也纳入。

---

## 5. 我们不该抄的部分

1. **默认不限步（`agent.steps` 不设）**。它靠压缩和 goal 判定兜底，我们有明确的 token/步数闸，且成本敏感。抄它会让我们退回"101 步烧 7.13M token"的形态（`stall.py` 开头就是这个真实台账）。只抄 L3 的"优雅收尾"。
2. **后台预压缩默认开启**。它自己的配置注释写着"A summary that is never used still costs tokens" `[S36]`。我们没有金额闸，默认开等于给成本加一个不可见的乘数。要开就按任务类型白名单开（长任务、且已接近上限）。
3. **Code Mode（把工具收进一个 JS 执行器）**。收益是工具说明从 N 份变 1 份，代价是引入一个 JS 运行时、沙箱、错误模型、权限穿透（它为此写了 `nested()` 来把子调用挂到父审批行上 `[S7]`）。我们已经有 `tool_search` + 场景过滤，收益边际，风险不小。只抄"目录预算 + search()"的表达。
4. **默认 `allow *` 的权限模型**。它假设"用户在场、可随时拒绝"，且把安全边界主要交给审批而非沙箱。我们的产品前提是审批优先 + 沙箱档位，不能退。
5. **1.5k 的极简提示词本身**。它能把行为放在工具 schema、权限规则和 reviewer 里，是因为它没有"交付四行""零改动伪成功"这类产品级约束。我们的 5,120 字符 coding 段里有真实的产品判断，不能为了短而删；要删的是"模型自己能查到/工具能强制"的部分。
6. **原生压缩（provider-level compaction）**。加密窗口不可审计、不可跨模型重放（它自己标注"only replays on the model that made it" `[S10]`）。我们多模型路由，抄它会制造换模型后历史读不懂。
7. **Effect-TS 的全链路服务化**。`SessionStep.attempt` 一个闭包 250 余行、类型体操很重；这是它团队规模与 TS 生态的选择，不是 agent 的必需品。我们抄语义（claim、事件投影、结算顺序），不要抄架构。
8. **未知命令退化成首词通配**。`prefix()` 在 ARITY 表里查不到时只取第一个词，于是 `terraform destroy -auto-approve` 保存成 `terraform *`，`rm -rf build` 保存成 `rm *`——下一次 `terraform apply` / `rm -rf src` 会被静默放行。我们的前缀规则要按"子命令 + 危险参数"再分一层（判断：需要一张危险参数表，未知命令不记忆）。
9. **子代理默认异步且父不等待**。它明确要求父"不要 poll"，适合并行探索；但"改完必须验证"的任务如果父先收工，交付会早于事实。我们应在"验证类任务"里强制 join（判断）。

---

## 6. 落地顺序（如果要动手）

**P0（一周内可验证，直接治痛点 4/5）**
1. `services/tools/editing.py`：write 新文件不回全文 diff，edit 的 diff 截断到 2,000 字符 + 指向 artifact。
2. `services/tools/shell.py` + `services/shell_tasks.py`：超时不杀，转后台，回执带 `shell_id` + "不要轮询"；保留 `testing.py` 的坏命令记忆。
3. `services/tools/gateway.py`：截断同时按"行数 + 字符"双上限；参数错误改三段式回执。

**P1（一个迭代，治痛点 2/3）**
4. `services/hooks.py`：hook 分 advisory / guard；guard 失败即 deny。
5. `services/instructions.py` + `graph.py`：指令增量（哈希 + 基线 + diff），环境/日期移出系统提示；用 `compose_system_prompt_parts_sizes()` 出账单。

**P2（需要改数据模型，治痛点 1 与长期可回放）**
6. `services/agent/usage.py` + `graph.py`：金额累计 + `cost_limit` 闸；`pricing.py` 未定价模型禁用金额闸。
7. 给 `context.compacted` 补 `keep_task_id/keep_seq`，并让 `threads.py:history_for()` 从它之后读（C4）。
8. `services/fork.py`：按消息边界分叉 + 引用重映射。

---

## 7. 来源索引

URL 均以 commit `d33b5783338b` 固定：`https://github.com/anomalyco/opencode/blob/d33b5783338b/<path>`。

| ID | 路径 | 读到的原文要点 |
|---|---|---|
| S1 | `packages/core/src/session/runner/llm.ts` | `drain` 外层循环；`advanceToStep` 处理 inbox/compaction/move；`runStep` 内层循环；`estimatePrompt` 后 `compaction.compact({reason:"auto"})`；`stepLimitReached` 时追加 `MAX_STEPS_PROMPT` 且 `toolChoice:"none"`；连续 3 条 `finish:"length"` 判 `output-limit`；`settleStaleCompactions` / `settleStaleToolCalls` |
| S2 | `packages/core/src/session/runner/step.ts` | 流式事件逐条发布；`tool-call` 到达即 fork 工具 fiber；`toolOutput.truncate`；结束快照与 `files` diff；`SessionUsage.record`；`needsContinuation`；`COOPERATIVE_STOP = "5 seconds"` |
| S3 | `packages/core/src/session/compaction.ts` | `RESERVE_SHARE=0.15`、`RESERVE_MIN=16_000`、`keep` 默认 15,000；`PRECOMPUTE_LEAD=60_000`、`PRECOMPUTED_TAIL_SHARE=0.4`；`SHRINK_STEPS=[0.85,0.7,0.5,0.35]`；七段式 `SUMMARY_TEMPLATE`；`TRANSCRIPT_LEVELS` 五级降级；`estimatePrompt` 的 measured/estimated 拆分；`history` 从 compaction 起读的注释 |
| S4 | `packages/core/src/tool-output.ts` | `MAX_LINES = 2_000`、`MAX_BYTES = 50 * 1024`、`RETENTION = Duration.days(7)`；截断后写全文并插入 `[showing lines 1-N of M; full output saved to <file>]` |
| S5 | `packages/core/src/tool.ts` | 注册表与 `snapshot()`；按权限 `whollyDisabled` 过滤；Code Mode 与 direct 工具的分流；`definitions` 按名字排序发送 |
| S6 | `packages/core/src/tool/model-policy.ts` | 新 GPT 系（非 gpt-4）隐藏 `edit/write` 改用 `patch`，其余 GPT 隐藏 `patch`；full code mode 下 GPT/Anthropic 隐藏 `glob/grep/webfetch` |
| S7 | `packages/core/src/permission.ts` | `evaluate` 取最后一条匹配规则，默认 `ask`；`evaluateInput` 先判 deny；`reply` 的 `once/always/reject`；`always` 写入 `PermissionSaved` 后重新评估同批 pending；`Unattended` 无人时不允许则 deny |
| S8 | `packages/schema/src/agent.ts` | `Agent.Info.default` 的权限：`*:* allow`，`read *.env` / `*.env.*` ask，`*.env.example` allow；`steps` 可选 |
| S9 | `packages/core/src/plugin/agent.ts` + `plugin/plan.ts` | `explore` 先 `*:* deny` 再逐条 allow；`plan` 对 `edit` 全 deny，仅允许 `~/.opencode/plan/*`；plan 提醒在压缩后会重新注入 |
| S10 | `packages/core/src/session/history.ts` | `latestCompaction` 后 `seq >= compaction.seq` 读消息；`withRecentText` 用 `metadata.keepID` 重建保留段；native 窗口只在同模型重放 |
| S11 | `packages/core/src/session/instruction-state.ts` + `instructions/index.ts` | instruction source 的 `key/codec/read/render{initial,changed,removed}`；`initial_values`/`current_values`/`epoch_start`；变化只渲染 diff 并写 `InstructionsUpdated` |
| S12 | `packages/core/src/session/projector.ts` | durable 事件投影到消息表；`applyUsage` 累加 cost/tokens；`projectFork` 按 boundary 复制并重映射 id/keepID；fork 的 cost 归零 |
| S13 | `packages/core/src/session/sql.ts` | `session_v2` / `session_message`（`(session_id, seq)` 唯一索引）/ `session_part` / `session_inbox` / `blob` 的表结构 |
| S14 | `packages/core/src/session/store.ts` | `claim` / `release` / `countResume`；`time_suspended` 作为 write-ahead 标记 |
| S15 | `packages/core/src/session/session.ts` | `prompt` 的 admission 与 `SessionInbox`；`shell` 的命令执行；`compact`；`revert` 的 stage/clear/commit |
| S16 | `packages/core/src/snapshot.ts` | 独立 git dir 的内容寻址快照；`maximumUntrackedFileBytes = 2 MiB`；`restore` 按文件恢复 |
| S17 | `packages/core/src/session/revert.ts` | `stage` 按 assistant 的 `snapshot.start` 恢复文件；`commit` 只发布边界；`clear` 撤销 |
| S18 | `packages/core/src/tool/plugin/subagent.ts` | 子会话 `parentID`；默认后台并返回 `sessionID`；深度限制 `experimental.subagent_depth` 默认 1；派生本身要过 `subagent` 审批 |
| S19 | `packages/core/src/session/subagent-completion.ts` | 完成时向父会话发 `<subagent sessionID=... state=...>`；附 "Background work still running" |
| S20 | `packages/core/src/session/model-request.ts` | `outputLimit` 按剩余窗口算（`ESTIMATE_ERROR=0.15`、`WINDOW_HEADROOM=0.01`、`OUTPUT_TOKEN_MAX=256_000`）；不支持的多模态替换为错误文本；`boundImages` 25MiB→15MiB；系统段与 `promptCacheKey` |
| S21 | `packages/schema/src/model.ts` + `packages/core/src/models-dev.ts` | `Capabilities{tools,input,output,freeform,temperature}`；`cost[]` 含 context 阶梯；`limit{context,input,output}`；`variants[]`；models.dev 目录 + 本地快照 |
| S22 | `packages/core/src/session/system-prompt.ts` + `runner/prompt/system.txt` | 模板 756 字符、全开渲染约 1.47k；`render(prompt, tools)` 仅在存在 `shell/write/edit` 时拼对应段落 |
| S23 | `packages/core/src/session/compaction.ts`（`buildPrompt`） | 摘要要求"不要复述指令文件、环境、sessionID；下一个 agent 会单独收到最新版" |
| S24 | `packages/core/src/tool/plugin/shell.ts` | `DEFAULT_TIMEOUT_MS = 2 * 60 * 1000`；超时"moves to the background"；`BACKGROUND_INSTRUCTION` 明令不要 poll；`run_in_background` 参数 |
| S25 | `packages/core/src/tool/plugin/write.ts` + `plugin/edit.ts` | write 输出 `Wrote file successfully: <path>`；edit 输出 `Edited <path> (N replacements)` |
| S26 | `packages/core/src/codemode/catalog.ts` + `codemode/instructions.ts` | 目录 `INLINE_BUDGET=2000` token；放不下时保留命名空间 + `search()`；`UPDATE_BUDGET=1000` |
| S27 | `packages/core/src/plugin/hooks.ts` | `trigger` 中 `yield* result`，无 catch；`Failures` 只有 `tool.execute.before` 可失败 |
| S28 | `packages/core/src/plugin/goal.ts` | `MAX_ROUNDS=50`、`MAX_STALLS=3`、`WAIT_LIMIT=30min`；reviewer 输出 `done/continue/blocked/paused/wait`；无工具调用视为未进展 |
| S29 | `packages/schema/src/permission.ts` | `Reply = ["once","always","reject"]`；`Request` 带 `resources` 与 `save` |
| S30 | `packages/core/src/session/execution.ts` | "write-ahead claim"注释：启动时在同一事务写 claim；shutdown 中断保留 claim，下次启动恢复 |
| S31 | `packages/core/src/tool/runtime.ts` | 入参校验失败输出前 5 条 issue + `Arguments provided:` + "Update the arguments and call the tool again"；运行时把任何非 `Tool.Error` 归一到 `Tool.Error` |
| S32 | `packages/core/src/session/context.ts` | 指令按"最共享 → 最具体"排序以复用 prompt cache 前缀；`select` 固定 session/agent/instructions/tools，`load` 再取 model/history |
| S33 | `packages/core/src/session/inbox.ts` + `session/prompt.ts` | `Delivery = steer/queue`；`promote` 只提升 steer（或一个 queued input）；prompt 准备与 admission 的顺序 |
| S34 | `packages/core/src/usage-limits.ts` | 只读 Console 的 `/api/policies/current-budgets` 与 Go allowance，失败返回 `UnavailableError`；不参与 runner 决策 |
| S35 | `packages/core/src/session/runner/publish-llm-event.ts` | `needsContinuation: Iterable.some(tools, t => !t.providerExecuted && (t.called 或 t.settled))`（原文用 JS 的逻辑或连接两个条件） |
| S36 | `packages/schema/src/config/compaction.ts` | `background` 注释："A summary that is never used still costs tokens." |
| S37 | `packages/core/src/shell/parse.ts` | `prefix()` 先按 ARITY 表取子命令（最多 3 个词），查不到时退化为首词；`save = prefix + " *"` |
| S38 | `packages/core/src/plugin/plan.ts` | 压缩/回滚会剥掉提醒，所以每个 `context` 请求都检查并补 `enterReminder/leave` |
| S39 | `packages/core/src/model-resolver.ts` | `Provider.isAISDK` / `loadCatalogPackage` 动态加载 `model.package`；`compatibility` 覆盖运行时 capabilities；变体只覆盖 settings/headers/body |

---

## 8. 事实与判断的边界

- **事实**：第 2 节表格「opencode 怎么做的」列里带 `[S#]` 的所有描述，都能在对应文件的标识符/注释里找到；数字（756 字符模板 / 1.47k 全开、10,133 字符、2000 行、50 KiB、15%/16k、15k keep、2 分钟、50 轮/3 次/30 分钟）均来自源码常量或本次实测。
- **判断**：所有"我们现状"里标了"（判断）"的，是我在 `blank_api` 里没有找到对应实现、或只找到相邻能力的推断；落表前应再确认一次具体函数。
- **未覆盖**：TUI/桌面端、LSP、MCP 服务器实现、share/enterprise、图像/视频生成、worktree 策略。它们与本次五个痛点无直接关系。
- **一个术语差异**：你说"没有成本上限"，代码里其实已经有 `token` 闸（`graph.py:5549-5603`，含子代理合计）；缺的是**金额**口径。本报告按"金额闸"理解并给出改法，避免把已有的 token 闸再实现一遍。
