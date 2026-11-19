# DeepSeek Agent Harness 参考报告（Kbgai 对照版）

- 调研日期：2026-11-19
- 调研对象：DeepSeek Harness（`deepseek-ai/deepseek-harness`，CLI 名 `dsh`）、DeepSeek API 文档、DeepSeek-V4 / V4.1-Flash 技术报告、DeepSeek-R1 官方仓库
- 写法约定：**【事实】** = 官方文档/仓库原文可查；**【第三方】** = 媒体报道或第三方榜单，未在官方文档核实；**【推测】** = 我基于事实做的工程判断，不是 DeepSeek 的说法
- 所有来源 URL 见文末第 8 节，正文关键处也给了链接

---

## 1. 一句话结论

**有可抄的，而且抄得到。** DeepSeek 在 2026 年 8 月开源了 `deepseek-harness`（MIT、TypeScript/Node、Cordis 插件内核；发布日第三方报道为 08-13），它不是"演示级 agent"，而是一套带会话日志、压缩、沙箱、后台任务、评测 profile 的完整 runtime。最值得我们抄的是三件：

1. **工具超时后"转后台 job"而不是"杀掉"**：`timeoutMs` 到期后命令转入 background job，模型用 `job_output` / `job_list` / `job_kill` 继续收——直接对应我们"25 分钟全仓测试塞进 120 秒工具调用"的痛点。
2. **系统提示是"历史里的一个节点"，默认原地替换、可选追加**：提示词不是每步重新拼的请求字段，而是 `system/message` surface node 0；不变就不写事件，变了默认原地替换，声明 `systemPromptUpdate: 'in-history'` 时改为"追加在已缓存历史之后"——直接对应我们"系统提示只增不减、10,132 字符"的痛点，而且把"省 token vs 保 KV cache"的取舍摆到了明面上。
3. **上下文治理是三级流水线：确定性裁剪 → 摘要压缩 → 大结果溢写落盘**，且顺序固定为"先便宜后昂贵"（prune 先于 summarize），阈值全部有公开默认值（`8192/4096/1024` 字符、`0.8/0.16/65536`）——直接对应"模型写过的文件在历史里反复重发"的痛点。

**抄不到的是成本上限。** DSH 明确没有预算/费用闸门：token meter 只做展示和定价，官方注释写明 "the value is a user-facing reference, not a billing or gating input"；goal 只有轮次上限 `maxGoalRounds`；agent-loop 的 Known Limitations 里直接写 **"No built-in turn budget"**。所以第 1 个痛点（能看到花了多少、停不下来）必须我们自建，不能指望从 DSH 搬。

---

## 2. 事实底座：DSH 到底是什么

### 2.1 基本信息

| 项 | 内容 | 来源 |
|---|---|---|
| 仓库 | `https://github.com/deepseek-ai/deepseek-harness` | 【事实】 |
| 定位 | "Model + Harness = Agent"，开源 agent harness（不是模型） | 【事实】[官方页](https://www.deepseek.com/harness/en) |
| 许可证 | MIT | 【事实】README |
| 语言/栈 | TypeScript / Node.js，pnpm + tsdown + vitest；含 `python/`、`native/`、Electron 桌面端 | 【事实】README 与仓库结构 |
| 状态 | Developer preview，README 全大写警告 **"THERE WILL BE COMPATIBILITY-BREAKING CHANGES."** | 【事实】README |
| 内核 | Cordis（`cordiverse/cordis`），论文 *A Programming Paradigm for Spatiotemporal Composability*（arXiv:2608.25512） | 【事实】README |
| 架构口号 | "Everything is a plugin"，模型适配、工具注册表、会话日志、**agent loop 本身**都是插件，没有特权内核 | 【事实】[官方页](https://www.deepseek.com/harness/en) |
| 安装 | `npx @deepseek-ai/dsh web`（默认 `127.0.0.1:3080`），或 clone 后 `pnpm install && pnpm run build && pnpm dsh web` | 【事实】README |
| 发布日 | 2026-08-13（与 V4-Pro 同日） | 【第三方】[s5labs](https://s5labs.io/resources/insights/deepseek-harness-open-source-agent-framework)、miraheze wiki；官方仓库未标日期 |

相关但不同的东西（避免混淆）：

- `dsh-superagent-prototype`：常驻 GitHub 仓库、接 issue/飞书消息、自己开 PR 的研究原型，MIT、TypeScript、仓库只有一个 commit，README 自述"不是 DeepSeek 官方产品"。**【第三方】**[搜狐报道](https://www.sohu.com/a/1102809216_122947431)。本文不把它当正式 harness 依据。
- 仓库里的 `benchmarks/` **不是** SWE-bench 评测，而是 `active-stream-reconnect`、`conversation-fold`、`long-session-browser`、`session-open` 这类**运行时性能微基准**。**【事实】**[benchmarks 目录](https://github.com/deepseek-ai/deepseek-harness/tree/master/benchmarks)

### 2.2 四种运行模式（直接对应"评测要可归因"）

| 模式 | 工具面 | 用途 | 来源 |
|---|---|---|---|
| Standard | 文件编辑、shell、文件/网页搜索、skills、planning、goals、subagents、workflows | 日常 coding agent | 【事实】[官方页](https://www.deepseek.com/harness/en) |
| Code | 同 Standard，但工具经 Code Mode SDK 暴露，模型写一段 TypeScript 编排多轮工具调用 | 多步批处理、减少往返 | 【事实】同上 |
| **Minimal** | **只有 persistent bash + `str_replace_editor` 两个工具** | **"for benchmarking models in a minimal environment"** | 【事实】同上 |
| Creator | Standard + runtime 检视、内存里试插件、写 preset | 造新 preset | 【事实】同上 |

这个 Minimal 模式很关键：它把"评测模型"和"评测 harness"分开了。V4-Pro 模型卡里 code-agent 分数就是用 DSH 的 minimal mode 跑的（**【第三方】**[s5labs 引用模型卡脚注](https://s5labs.io/resources/insights/deepseek-harness-open-source-agent-framework)；我尝试直接打开 HF 模型卡失败，见第 7 节）。

另外仓库里有一个 `sdk-minimal` profile：persistent shell（Linux/macOS 走 Bash，Windows 走 PowerShell）+ `read_image`，**persistent shell 超时 300 秒**，默认自动压缩，不含 jobs/subagents/skills/文本文件工具。**【事实】**[sdk-minimal README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md)

---

## 3. Agent loop 结构（我们最该对照的部分）

### 3.1 Turn / Step 的实际阶段

DSH 把"可重放的持久事实"和"实时控制面"分成两条通道：

- `session/event`：持久、append-only、可 replay 的转录事实（system prompt、reasoning、tool call/result、subagent 调度、每次 context 注入）
- `agent/*`：瞬时协调（队列、流式 chunk、steering、continuation、错误）

**【事实】**[agent-lifecycle](https://deepseek-harness.github.io/deepseek-harness/en/reference/agent-lifecycle)、[官方页](https://www.deepseek.com/harness/en)

一个 step 的顺序（**事实**，来自 agent-lifecycle 与 agent-loop README）：

```
assembly（渲染系统提示 + 工具 schema）
  → user admission
  → agent/pre-step（压缩在这里做"压力"判断）
  → request derivation（冻结请求头）
  → provider stream
  → settlement：assistant/message（成功）或 assistant/attempt（失败/取消/流错误）
  → tool execution（完整 raw batch 交给 ctx.tools.execute）
  → 下一步
```

几个细节值得直接照搬：

- **重试不重放昂贵阶段**：`assembly`、`pre-step`、`user admission` 只做一次；重试只重做 `prepareCall` + 对已保留的渲染装配做 reconcile + request derivation。**【事实】**agent-loop README
- **成功但空内容也记账**：`assistant/message` 记录**每一次成功的 provider 调用**，包括 content-less 和 `max-tokens` 结束；空内容不进派生历史。**【事实】**agent-lifecycle
- **失败必须有落点**：失败/重试/取消/流错误的 attempt，只要走到 settlement 且没有 surface message，就写 `assistant/attempt`；流式 chunk 是瞬时的，进程在 settlement 前硬崩则"不留半成品"。**【事实】**同上
- **重试必须有进展**：压缩恢复只在 "pruning 或 summarization 推进了 surface replacement generation" 时才允许重试，否则原始 request error 保持权威——避免"内容没变还重试"的死循环。**【事实】**同上

### 3.2 工具调度与并发

- `maxParallelToolCalls` 默认 **10**，`1` 表示串行；每次 batch 把当前值传给 `execute()`，**优先于 executor 配置**；热改从下一批生效。**【事实】**[agent-loop README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md)
- 内置 executor 负责"raw batch 解析、调度、取消、Session call/result 日志"，并且**保持模型给出的结果顺序和独占屏障（exclusive barriers）**。**【事实】**同上
- 工具管线是固定顺序的三个 waterfall：`tools/pre-execute` → 单调守卫 → `tools/execute` → `tools/post-execute`；审批（`ctx.approval`）在单调守卫**之前**解析；"不能被重排的策略"必须注册成 guard，不能放 waterfall。**【事实】**[tool-execution-pipeline](https://deepseek-harness.github.io/deepseek-harness/en/reference/tool-execution-pipeline)

对我们的直接含义：**"并行执行"和"结果顺序"是两件事**。可以并发跑只读工具，但回填给模型时必须按模型原始调用顺序逐条配对，否则模型会把结果错配到调用上。

### 3.3 没有的东西（很重要）

agent-loop 的 Known Limitations 原文：

> **No built-in turn budget** — tool calls or steering continue the current turn; a policy that bounds runaway turns must cancel from an existing lifecycle extension point such as `agent/turn-stopping`.

**【事实】**也就是说 DSH 自己不做"跑多少轮/花多少钱就停"，它只提供 `agent/turn-stopping` 这个扩展点让你自己写。这和第 1 个痛点是同一件事：**成本上限在 DSH 里是空的，必须自建。**

---

## 4. 逐条对照表

> 读法："能不能抄" 分三档：✅ 直接抄 / ⚠️ 抄思路但要改 / ❌ 他们没有，别等

### 4.1 执行与工具层

| # | 他们怎么做的 | 我们的现状 | 能不能抄 | 怎么抄（落到 Kbgai） |
|---|---|---|---|---|
| 1 | Turn/step 双通道：`session/event` 持久可回放，`agent/*` 瞬时控制；重试不重放 assembly/pre-step/admission | 已有单任务 step 循环 + 事件账可回放 | ✅ | 把"实时流"与"落账事实"分开存；verdict 只读已 commit 的事件，不读流式缓冲 |
| 2 | `maxParallelToolCalls` 默认 10；只读并发、写操作靠 exclusive barrier；**结果按模型顺序回填** | 未提并发策略 | ✅ | 工具分两类：`read_only` 并发上限 8–10，`mutating` 串行；结果数组按 `tool_call.index` 排序后再回填 |
| 3 | bash `timeoutMs` 到期**不杀进程，转 background job**；`run_in_background` 立即返回 job id 且**无超时**；`job_output/job_list/job_kill` 收口；完成通知通过 `agent.inject()` 注入 | 120 秒硬超时，25 分钟测试必然被杀 | ✅ **最高优先** | 见 5.3 的 bash 工具契约；超时语义从"失败"改为"已转后台" |
| 4 | 工具失败的三种记录：`TOOL_OUTCOME_UNKNOWN`（有 call 记录无 result）、`TOOL_NOT_STARTED`（连 call 记录都没有）、正常 result；并明确"outcome unknown 只允许只读/幂等操作重试" | 已有"验证没跑成 ≠ 验证失败"三档 | ✅ | 把三档扩成四档：`ok / error / unknown / not_started`；`unknown` 不自动重试有副作用的调用 |
| 5 | 取消后未 dispatch 的调用补 `ABORTED_BEFORE_DISPATCH` + 固定文案 | 未提 | ✅ | 取消时给每个未执行调用补一条合成 result，保持 call/result 配对，避免下一轮 400 |
| 6 | 工具结果不可变、lossless JSON；超预算结果 spill 到私有文件，返回 head/tail + locator + `retrievalHint` | 已有"结果落盘" | ⚠️ | 落盘之外再补"head/tail 预览 + 取回提示"，否则模型不知道文件里有什么、要不要读 |
| 7 | 审批在单调守卫之前；"不可重排"的策略必须是 guard | 已有权限四档 + 审批卡 | ⚠️ | 把"编辑前必须先读""删除前必须校验绝对路径"做成 guard，不要放普通 hook |

### 4.2 上下文 / 成本 / 评测层

| # | 他们怎么做的 | 我们的现状 | 能不能抄 | 怎么抄 |
|---|---|---|---|---|
| 8 | 系统提示渲染成 `system/message` **surface node 0**；不变不写事件；变了默认**原地替换**；`systemPromptUpdate:'in-history'` 时**追加在缓存历史之后** | 系统提示只增不减，10,132 字符 | ✅ | 拆"冻结核心 + 追加增量"；核心永不变，动态事实走 `<system-reminder>` 追加 |
| 9 | `PromptContext` 是"cache-safe"的动态上下文：只在**内容变化**或**被压缩移除**时写快照 | 每轮可能重拼 | ✅ | 动态上下文做内容哈希，未变不写；变化才追加一条 |
| 10 | 工具结果确定性裁剪：`thresholdChars=8192`、`headChars=4096`、`tailChars=1024`（Unicode 码点） | 无裁剪，整段重发 | ✅ | 先裁剪再压缩；超 8K 字符的结果只留头 4K + 尾 1K，中间用 `…truncated N chars, full at <path>…` |
| 11 | 压缩阈值 `floor(min(W×0.8, W−O−65536))`；保留尾部 `W−O` 的 `0.16`；`maxOverflowRetries=1`、`compactionRetries=1` | 阈值随窗口缩放，但无公开公式 | ✅ | 直接采用公式；小窗口必须改 `headroomTokens`（见 5.2） |
| 12 | 顺序固定：**先 prune（便宜）→ 重新度量 → 还不够才 summarize（贵）**；prune 后已达标就不调模型 | 未提顺序 | ✅ | 压缩入口先跑裁剪；裁剪后低于阈值直接返回，不产生摘要调用 |
| 13 | 压缩摘要要求固定 8 节 Markdown（Primary Request / Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs / Current Work / Next Step / Critical Context） | 空闲压缩，摘要格式未固定 | ✅ | 直接抄这 8 节；"保留精确路径、命令、错误串、标识符、数字、函数签名" |
| 14 | 每次测量都按"路由声明的价格"给每个 surface 节点定价；`baseline.kind` 为 `usage` 或 `estimated` | 能看到 token，不能停 | ⚠️ | 定价逻辑可抄；**闸门要自建**（见 5.5） |
| 15 | 明确没有预算/费用闸门；token meter 的值"不是 billing or gating input"；goal 只有 `maxGoalRounds` | 痛点 1 | ❌ | 自建硬预算 + 预估下一步成本 + 预算耗尽走 `INCONCLUSIVE` 而非 `FAIL` |
| 16 | 权限预设是"沙箱档 + 审批策略"的二元组：`workspace-write`(`workspace-write`+`ask`)、`danger-full-access`(`danger-full-access`+`never`)、`auto`(全访问+ask，额外强制由 Auto review 负责)、`custom`(只读派生，不可选) | 权限四档 + 审批卡 | ⚠️ | 我们的四档可以映射成"档位 = 沙箱 × 审批"的矩阵，避免档位与审批策略各自漂移 |
| 17 | `session/event` append-only；resume/fork/search/replay 都基于同一事件流；压缩/权限选择等是 **log-only**，不进模型 transcript | 已有事件账可回放 | ✅ | 把"用户选了哪个权限档""压缩发生了"记成 log-only，不要塞进对话历史 |
| 18 | Subagent 只回传 `output`（最后一条非空 assistant 消息）+ 可选 `structured` + `diagnostic`（≤4096 UTF-8 bytes，强制脱敏）；内部轨迹永不进父上下文；`delivery:'caller'` 连通知都省掉 | 长任务上下文膨胀 | ✅ | 大块探索（读全仓、跑测试、查日志）丢给 subagent，父级只收结论；需要机器可读就上 `outputSchema` |
| 19 | 评测用 Minimal 模式（bash + editor 两工具）跑 agent benchmark；官方分数用 Terminal-Bench / DeepSWE / CyberGym / Automation-Bench | 验证层是自建 verdict | ⚠️ | 见第 6 节：做一个 `minimal` profile 做归因，不要只跑全功能 harness |

---

## 5. 对"用 deepseek-flash 当默认模型"的具体建议

### 5.1 模型参数基线

| 参数 | 建议值 | 依据 |
|---|---|---|
| `model` | `deepseek-flash`（当前实际服务 DeepSeek-V4.1-Flash-1017） | 【事实】[API 首页](https://api-docs.deepseek.com/) |
| 上下文窗口 | 1,000,000 | 【事实】[定价页](https://api-docs.deepseek.com/quick_start/pricing) |
| 最大输出 | 384K（模型上限，**不要每个请求都拉满**） | 【事实】同上 |
| thinking | 默认开启，effort 默认 `high` | 【事实】[thinking mode](https://api-docs.deepseek.com/guides/thinking_mode) |
| effort 映射 | `minimal→low`、`low→low`、`medium→high`、`high→high`、`xhigh→high`、`max→max`、`ultra→max` | 【事实】同上 |
| 单请求输出上限（agent 用） | 16K–32K，规划步可给 64K | 【推测】按工具调用轮次的典型输出量估 |
| temperature | thinking 模式下**不支持**，传了也不报错但不生效 | 【事实】thinking mode |
| `top_p` | flash 仅在 thinking 模式生效，有效范围 0.95–1.0，低于 0.95 按 0.95 算；非 thinking 固定 1.0 | 【事实】同上 |

调用姿势（OpenAI SDK 下 `thinking` 要放 `extra_body`）：

```python
resp = client.chat.completions.create(
    model="deepseek-flash",
    messages=messages,
    tools=tools,
    reasoning_effort="high",
    extra_body={"thinking": {"type": "enabled"}},
)
```

### 5.2 上下文预算与压缩阈值（1M 窗口到底配多少）

先把 DSH 的公式抄过来（**【事实】**[compaction-basic README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md)）：

```
trigger = floor( min( W × thresholdRatio, W − O − headroomTokens ) )
B = 65,536
thresholdRatio = 0.8
retain = 0.16 × (W − O)
```

其中 `W` = 窗口，`O` = 该路由的有效输出上限，`headroomTokens` = 输出预留之外的额外余量。

代入 deepseek-flash（W = 1,000,000）：

| 你设的最大输出 O | `W × 0.8` | `W − O − 65536` | 实际触发点 | 逐字保留尾部 |
|---|---|---|---|---|
| 32K | 800,000 | 902,464 | **800,000** | 154,880 |
| 128K（DSH 某 adapter 默认值，举例） | 800,000 | 806,464 | **800,000** | 139,520 |
| 384K（模型上限） | 800,000 | 550,464 | **550,464** | 98,560 |

**【事实】**公式与默认值；表格为按公式计算。结论：**只要输出上限不超过 ~134K，1M 窗口的触发点就是 800K；一旦把输出拉到 384K，触发点反而降到 550K。**

但"DSH 的默认值" ≠ "适合我们的值"。DSH 调的是**能力**（尽量少压缩、保留长尾），不是**成本**。我们的建议是分层：

| 层级 | 触发点 | 动作 | 理由 |
|---|---|---|---|
| L0 软预算 | 150K（或 200K 窗口的 75%） | 只跑确定性裁剪（8K/4K/1K） | 便宜、不破坏前缀结构 |
| L1 工作预算 | **250K–300K** | 摘要压缩，保留尾部 60K | 覆盖 95% coding 任务；单步重发成本可控 |
| L2 硬预算 | 500K | 强制摘要 + 禁止再整文件读入 | 防止"1M 能放就全放" |
| L3 溢出 | provider 报 `CONTEXT_WINDOW_EXCEEDED` | 一次最大化头部裁剪，重试 1 次 | 对应 `maxOverflowRetries=1` |

**【推测】**上面 250K/300K/500K 是我的工程建议，不是 DeepSeek 官方数字。依据是下面的成本算术。

**成本算术（peak 价，1M input token 单价）：**

| 场景 | cache hit | cache miss |
|---|---|---|
| 单价 | $0.006 | $0.30 |
| 500K 上下文跑 1 步 | $0.003 | $0.150 |
| 500K 上下文跑 50 步 | $0.15 | $7.50 |
| 800K 上下文跑 50 步 | $0.24 | $12.00 |

**【事实】**单价来自[定价页](https://api-docs.deepseek.com/quick_start/pricing)；乘法是我算的。

两个反直觉结论：

1. **cache 命中率比压缩阈值重要 50 倍。** 500K 上下文如果全命中，50 步只要 $0.15；全不命中要 $7.50。先把前缀稳定性做好，再谈压缩。
2. **压得太勤可能更贵。** 一次压缩 = 摘要调用（比如 600K miss ≈ $0.18）+ 压缩后重新 prefill 尾部（140K miss ≈ $0.042）。如果你每 10 步压一次，光压缩就 $0.02/步；而让 800K 上下文一直命中，只要 $0.0048/步。**压缩的价值在"控制延迟和质量衰减"，不在"省 input token"。**

### 5.3 工具调用注意事项（含"25 分钟测试"的正确解法）

#### (a) bash 工具契约直接照抄 DSH

DSH 的 bash 工具暴露这些参数（**【事实】**[tool-catalog](https://deepseek-harness.github.io/deepseek-harness/en/reference/tool-catalog)）：

| 参数 | 必填 | 说明（原文要点） |
|---|---|---|
| `description` | ✅ | 5–10 词主动语态描述，UI 展示；**要求放在 `command` 之前** |
| `command` | ✅ | 要执行的 bash 命令 |
| `timeoutMs` | ❌ | 执行器套用自己的默认值与上限；**到期后命令转后台 job，而不是被杀** |
| `workdir` | ❌ | 工作目录；不要用 `cd` |
| `run_in_background` | ❌ | 立即返回 job id，用 `job_output` 收、`job_kill` 停；**无超时** |

工具描述里还有几条"教模型怎么用"的硬规则，建议原文抄进我们的 bash 工具描述：

- "Each call runs in a fresh shell; pass `workdir` instead of using `cd`."
- "Long output is truncated to its tail; the full output is saved to a file whose path is reported when available."
- "Before any delete or move, verify that the resolved absolute target path is the intended one; never run it against a computed path you have not checked."
- "An unset variable expands to an empty string, so guard variables in such paths with `${VAR:?}`."
- 沙箱拒绝文案：`[sandbox: file access denied under <mode> mode]`，并明确 **"a policy denial: do not retry another way."**

**【事实】**同上。

#### (b) 把"超时"从失败语义里拆出去

我们现在的 120 秒硬超时，会把"测试还在跑"误判成"测试失败"。改成：

```
timeoutMs 到期
  → 进程不杀，注册为 job（job_id）
  → 工具返回：{status:"running", job_id, hint:"用 job_output 继续收，job_kill 可停"}
  → 模型下一轮调 job_output(job_id, wait_ms)
```

DSH 的 job 运行时给了这些默认值，可以直接当我们的初始配置（**【事实】**[jobs](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/jobs)）：

| 项 | 默认值 |
|---|---|
| 每 owner 并发 job 上限 | 10 |
| live ring 保留 | 262,144 bytes（256 KiB） |
| settled ring 保留 | 16,384 bytes（16 KiB） |
| 拉取轮询 `pumpPollMs` | 150 ms |
| `wait` 超时 | 无默认，调用方必须给正数毫秒 |

一个容易踩的坑（也是 DSH 在 Terminal-Bench 上丢分的原因）：**任务结束时清理它启动的所有后台进程**，会导致"评测要检查的服务"已经死了。**【第三方】**[Harness Leaderboard 分析](https://aicoder.com/news/news-20261111-hf-harness-leaderboard-adds-deepseek-harness-dsh-tied-first-all-five-evals-deepswe-2nd-terminal-bench-6th)：3 个需要"agent 退出后服务仍在"的任务，DSH 36 次只过 2 次，OpenCode/Pi/Hermes 36/36。第三方未披露是 job 层还是进程组层清理，但结果是确定的。

对我们的启示：**"任务结束"和"进程结束"必须分开**。给 job 加 `persist_after_turn: bool`，验证类任务默认 true，并在 verdict 里显式记录"该服务在验证时仍存活"。

#### (c) 工具 schema 与 strict mode

DeepSeek 的 `strict` 模式是 Beta，要求（**【事实】**[Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)）：

1. `base_url="https://api.deepseek.com/beta"`
2. 每个 function 设 `"strict": true`
3. schema 必须合规，否则服务端直接报错

strict 模式下的硬约束：

- **每个 `object` 的所有 properties 都必须是 `required`**，且 `additionalProperties: false`
- 支持：`object`、`string`、`number`、`integer`、`boolean`、`array`、`enum`、`anyOf`、`$ref`/`$def`
- `string` 支持 `pattern`、`format`（email/hostname/ipv4/ipv6/uuid）；**不支持 `minLength`/`maxLength`**
- `array` **不支持 `minItems`/`maxItems`**

对我们的含义：**不要指望模型侧的长度约束**。`minLength`/`maxItems` 这类必须在我们网关里做二次校验；strict 模式只保证"结构对"，不保证"语义对"。

#### (d) 并行调用

- 官方示例用 `for tool in tool_calls` 逐条处理，并未限制只能有一个；但官方**没有**"并行调用开关 / 并行度"的文档，也没有明确说多个 tool call 会并发执行。**【事实+缺口】**（见第 7 节）
- 建议：只读工具并发，写工具串行；**结果按原始 index 回填**；某个调用失败不影响同批其他调用的结果落账。

#### (e) 工具失败怎么重试（官方口径）

DeepSeek 官方**没有**"工具调用失败重试策略"的专门文档，只有 API 错误码的处理建议（**【事实】**[Error Codes](https://api-docs.deepseek.com/quick_start/error_codes)）：

- `429`：请求太快/超并发/QPS —— "pace your requests reasonably"，**没有让你指数退避重试**
- `500`：服务端问题 —— "retry your request after a brief wait"
- `503`：过载 —— "retry your request after a brief wait"
- `504`：网关超时 —— "Retry later, or send a smaller file"

真正可抄的是 **DSH 在 harness 层的语义**（**【事实】**agent-loop README）：

> 对已记录但无结果的调用：`Its outcome is unknown.`，并且**只允许只读或幂等操作重试**；可能有副作用的，必须先检查外部状态或问用户。
> 对连开始记录都没有的调用：`The tool call was interrupted before the Harness recorded it as started. Retry it if it is still needed.`

这套文案和判定，比"失败就重试三次"安全得多，建议直接进我们的工具网关。

### 5.4 thinking 内容要不要进历史（这是最容易踩 400 的地方）

DeepSeek 当前（V4 系列）的规则和 R1 时代**不一样**，不要混用：

| 场景 | `reasoning_content` 怎么处理 | 来源 |
|---|---|---|
| 请求**不带** `tools` | 不需要回传；传了也会被忽略、不拼进上下文 | 【事实】[thinking mode](https://api-docs.deepseek.com/guides/thinking_mode) |
| 请求**带** `tools` | **所有历史轮的 `reasoning_content` 必须完整回传**，并会拼进上下文；不回传 → **400** | 【事实】同上 |
| 带 tools 且某一轮没调工具 | 该轮的 `reasoning_content` **也要回传** | 【事实】同上 |
| 流式 | `reasoning_content` 与 `content` 同级返回 | 【事实】同上 |

最省事的实现（官方示例）：直接把 `response.choices[0].message` 整个 append 进 `messages`，它同时带 `content`、`reasoning_content`、`tool_calls`。

**代价**：thinking 的 token 按 output 计费，然后在后续每一轮又作为 input 重发。一个 50 步的任务，如果每步 2K reasoning token，就是 100K token 的额外历史。**【推测】**这是 agent 场景里比"系统提示 10K 字符"更大的隐藏成本项。

建议：

1. **同一请求序列内不要切换 thinking 开关**。要么全程开，要么全程关；中途关掉再开，历史里会出现"有的轮有 reasoning_content、有的没有"的混合形态，虽然文档没明说会 400，但风险不值得冒。**【推测】**
2. 需要省成本，用 `reasoning_effort` 降档（`high → low`），不要关 thinking。
3. **不要把 `reasoning_content` 写进我们自己的"可见对话"或前端**；它只用于回传 API。DSH 的做法是记在 session log 里、由 replay 还原，但不作为普通消息展示。**【事实+推测】**（session log 记录 reasoning 是事实；"不展示"是我们的产品选择）

### 5.5 中间切 effort 会不会破 KV cache

DSH 的规则说得很清楚（**【事实】**agent-loop README "KV Cache effect"）：

> "Prefix reuse requires earlier messages and declarations to stay **byte-identical** under the **same provider and model route**."
> "A prompt change that replaces a system node in place makes the request differ from that node's first token — **in full when the node is node 0** — so the provider prefix cache misses from there."
> "when the prepared call declares `systemPromptUpdate: 'in-history'`, a non-empty prompt change inside a continuing request series is **appended after the cached history**, so the prefix through that history stays reusable."
> "Changing a retained tool definition or rebuilding declarations invalidates reuse from the first altered request token."

翻译成我们的场景：

- **只改 `reasoning_effort` 这个请求参数，理论上不动消息前缀**，不应该破坏 prefix cache（文档没有把 effort 列为失效因素）。**【推测】**
- **但如果我们的"切 effort"实现方式是改写系统提示**（比如往 system prompt 里塞"当前 effort=high"），那就是改 node 0，**整条前缀全废**。**【推测，但风险很高】**
- **换模型路由**（spark → deepseek-flash）一定废，因为要求 "same provider and model route"。**【事实】**

落地建议：

1. effort 只作为请求参数传，**绝不写进 system prompt / 历史消息**。
2. 如果产品上必须让模型"知道自己当前 effort"，用一条追加在历史末尾的 `<system-reminder>`，不要改 node 0。
3. 切模型/切 effort 放在 turn 边界，不要在一个 turn 的中间步骤切；切完接受一次 cache miss，别在 50 步里切 5 次。
4. 监控 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（API 响应 `usage` 里有），**把 hit rate 做成和 token 数一样的一等指标**。**【事实】**[Context Caching](https://api-docs.deepseek.com/guides/kv_cache)

### 5.6 成本闸门（这块 DSH 没有，必须自建）

给一个可直接实现的模型：

```
单价（peak）:
  HIT   = 0.006 / 1M
  MISS  = 0.30  / 1M
  OUT   = 1.20  / 1M

spent = hit_tokens*HIT + miss_tokens*MISS + output_tokens*OUT
next_step_worst = ctx_tokens*MISS + max_output*OUT

if spent >= budget:                 stop(reason="budget_exhausted")
if spent + next_step_worst > budget: 
    downgrade_effort() or shrink_max_output() or stop()
```

三个要点：

1. **用 worst case 预估下一步**（全 miss + 满输出），不要用历史平均值；否则永远在"下一次就超"的边缘反复横跳。
2. **预算耗尽是 `INCONCLUSIVE`，不是 `FAIL`**。这和"验证没跑成 ≠ 验证失败"是同一个原则：没跑完不等于做错了。
3. **闸门放在工具网关之前，不放在模型之后**。模型已经生成、工具已经执行再发现超预算，钱已经花了。

建议的默认阈值（**【推测】**，按我们的任务时长调）：

| 阈值 | 动作 |
|---|---|
| 50% | UI 提示 + 事件账记 `budget_warning` |
| 80% | 禁止新开 subagent、禁止整文件读入、effort 降一档 |
| 100% | 停止，verdict = `INCONCLUSIVE(budget)`，保留现场供人接手 |
| 单步 > $0.50 | 记 `cost_spike` 事件，打印该步 context 构成（系统提示/工具结果/历史） |

---

## 6. 评测 harness：他们怎么跑、我们怎么借

### 6.1 事实

- DSH 的 Minimal 模式是**两工具**（persistent bash + `str_replace_editor`），官方定位就是"在最小环境里 benchmark 模型"。**【事实】**[官方页](https://www.deepseek.com/harness/en)
- V4-Pro 模型卡的 code-agent 分数注明 "evaluated with the minimal mode of DeepSeek Harness as the agent framework"。**【第三方】**[s5labs 引用](https://s5labs.io/resources/insights/deepseek-harness-open-source-agent-framework)（HF 原文抓取失败，见第 7 节）
- V4.1-Flash 技术报告里的 agent 评测是 **Terminal-Bench 3.0 / DeepSWE v1.1 / CyberGym / Automation-Bench**，不是 SWE-bench：Terminal-Bench 3.0 = 30.0，DeepSWE v1.1 = 74.2，CyberGym = 88.1，Automation-Bench = 54.8。**【事实】**[arXiv:2609.19969](https://huggingface.co/papers/2609.19969)（PDF 首页表格）
- DSH 仓库的 `benchmarks/` 是运行时性能微基准，**不是** SWE-bench runner。**【事实】**[目录](https://github.com/deepseek-ai/deepseek-harness/tree/master/benchmarks)
- 第三方 Harness Leaderboard 的跑法：固定模型与 effort、只换 harness、每个任务 **3 次**、跑 **Terminal-Bench 2.1 + DeepSWE v1.1**、公布全部 trajectory、用配对 bootstrap 95% 区间判断"是否并列"。DSH 结果：五项评测统计上并列第一，DeepSWE 三模型平均第 2（落后 Hermes 0.5 分），Terminal-Bench 第 6，**每次成功解题平均 $0.53**。**【第三方】**[AICoder 汇总](https://aicoder.com/news/news-20261111-hf-harness-leaderboard-adds-deepseek-harness-dsh-tied-first-all-five-evals-deepswe-2nd-terminal-bench-6th)

### 6.2 对我们的验证层，四条可直接抄

1. **做两个 profile：`full` 和 `minimal`。** minimal 只留 bash + edit，用来回答"这次失败是模型不行还是 harness 不行"。我们现在的 verdict 只看结果，不看归因。
2. **同一任务跑 3 次，报 pass@1 的分布，不报单次。** DSH 的官方评测和第三方榜单都是多次平均；R1 的官方建议也明确写 "conduct multiple tests and average the results"。**【事实】**[R1 README](https://github.com/deepseek-ai/DeepSeek-R1/blob/main/README.md)
3. **verdict 只读已 commit 的事实。** DSH 把 `session/event`（持久）和 `agent/*`（瞬时）分开，replay 只读持久结算。我们的"独立 verdict 只读事实"应该更彻底：**不读流式缓冲、不读 UI 状态、只读工具 result 事件**。
4. **"没跑成"要有独立状态，不能折叠进 fail。** DSH 的 `stopReason` 有 `completed / aborted / error / max-tokens / refusal` 五档；job 有 `running / stopping / completed / killed / failed`，还有 `cause: teardown`；连 `job/settled` 缺失都明确规定"仍视为未完成，不许自己编结束时间"。**【事实】**[subagent](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/subagent)、[jobs](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/jobs)

建议我们的 verdict 直接采用：

```
PASS
FAIL
INCONCLUSIVE(reason): timeout | aborted | infra | budget | max_tokens | unknown_outcome
```

并且 `INCONCLUSIVE` 不允许触发"自动重试有副作用的操作"。

---

## 7. 查不到 / 未验证（不要编）

这一节是刻意留白的，比编造有价值。

| # | 问题 | 状态 |
|---|---|---|
| 1 | DSH 有没有成本上限/预算闸门 | **确认没有**。token-meter 官方注释："a user-facing reference, not a billing or gating input"；goal 只有 `maxGoalRounds`；agent-loop 明写 "No built-in turn budget"。这不是"查不到"，是"确认不存在" |
| 2 | DSH 仓库里有没有 SWE-bench runner | **未找到**。`benchmarks/` 是性能微基准；没有公开的 SWE-bench 复现脚本或配置 |
| 3 | 官方 SWE-bench 的具体设置（trials / timeout / scaffold） | **未核实**。V4 技术报告 §5.3.1 Evaluation Setup 与 §5.4.4 Code Agent 在 HTML 抓取时被截断（页面过长），没拿到正文；HF 模型卡也抓取失败 |
| 4 | bash 工具的默认 `timeoutMs` / `maxTimeoutMs` / `maxOutputBytes` 具体数值 | **未公开**。config-catalog 只有字段名和 JSDoc，没有默认值；只有 `sdk-minimal` profile 的 persistent shell 是 **300 秒** |
| 5 | spill 的 `maxInlineTokens` 默认值、head/tail 保留比例 | **未公开**。spill 页面只有参数名，没有数值；明确不存在字节阈值 |
| 6 | 官方对"1M 窗口该在多少 token 压缩"的通用建议 | **没有**。只有 DSH 的实现参数（0.8 / 0.16 / 65536），且官方自己说这是实现默认值，不是模型建议 |
| 7 | 官方对"工具调用失败怎么重试"的专门指引 | **没有专门文档**。只有 Error Codes 里 500/503 "retry after a brief wait"；harness 层的语义在 DSH README 里，不在 API 文档 |
| 8 | API 是否支持/推荐并行 tool calls、有没有并行度上限 | **未找到明确说明**。官方示例只展示逐条处理；并行度是 DSH 在 harness 层自己控制的（默认 10） |
| 9 | 切 `reasoning_effort` 是否破坏 KV cache | **官方未说明**。文档只列了"改系统提示、改工具定义、换模型路由"会失效；effort 未列入。本文的"参数不破、改提示才破"是**推测** |
| 10 | HF 模型卡 "minimal mode of DeepSeek Harness" 原文 | **未直接核实**。huggingface.co 抓取失败；目前依据是 s5labs 的引用（第三方） |
| 11 | DSH 内部是否用 SWE-bench 训练/评测 | **未找到公开材料** |
| 12 | `DEFAULT_MAX_PARALLEL_TOOL_CALLS` 的常量值 | agent-loop README 明确写默认 **10**；但 config-catalog 里只显示符号名，未见常量定义 |

---

## 8. 来源清单

### 官方仓库 / 官方文档站

- DSH 仓库：https://github.com/deepseek-ai/deepseek-harness
- DSH 官方介绍页：https://www.deepseek.com/harness/en
- DSH 文档站：https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart
- Agent lifecycle：https://deepseek-harness.github.io/deepseek-harness/en/reference/agent-lifecycle
- Tool execution pipeline：https://deepseek-harness.github.io/deepseek-harness/en/reference/tool-execution-pipeline
- Tool catalog：https://deepseek-harness.github.io/deepseek-harness/en/reference/tool-catalog
- Config catalog：https://deepseek-harness.github.io/deepseek-harness/en/reference/config-catalog
- Compaction：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/compaction
- Token meter：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/token-meter
- System prompt：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/system-prompt
- Spill storage：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/spill
- Background jobs：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/jobs
- Shell：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/shell
- Subagents：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/subagent
- Goals：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/goal
- Permission presets：https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/permission-presets
- agent-loop README：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md
- compaction-basic README：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md
- sdk-minimal README：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md
- benchmarks 目录：https://github.com/deepseek-ai/deepseek-harness/tree/master/benchmarks

### DeepSeek API 文档

- 首页 / 模型名：https://api-docs.deepseek.com/
- 模型与定价：https://api-docs.deepseek.com/quick_start/pricing
- Thinking Mode：https://api-docs.deepseek.com/guides/thinking_mode
- Tool Calls（含 strict mode）：https://api-docs.deepseek.com/guides/tool_calls
- Function Calling 入口：https://api-docs.deepseek.com/guides/function_calling
- Context Caching：https://api-docs.deepseek.com/guides/kv_cache
- Error Codes：https://api-docs.deepseek.com/quick_start/error_codes

### 论文 / 模型

- DeepSeek-V4 技术报告：https://arxiv.org/abs/2606.19348
- DeepSeek-V4.1-Flash 技术报告：https://huggingface.co/papers/2609.19969
- DeepSeek-R1 仓库与 Usage Recommendations：https://github.com/deepseek-ai/DeepSeek-R1/blob/main/README.md
- Cordis 设计论文：https://arxiv.org/abs/2608.25512

### 第三方（标注为参考，不作事实依据）

- s5labs 对 DSH 的分析（含模型卡脚注引用）：https://s5labs.io/resources/insights/deepseek-harness-open-source-agent-framework
- Harness Leaderboard 结果汇总：https://aicoder.com/news/news-20261111-hf-harness-leaderboard-adds-deepseek-harness-dsh-tied-first-all-five-evals-deepswe-2nd-terminal-bench-6th
- DSH 背景与发布信息：https://ai.miraheze.org/wiki/Deepseek_Harness

---

## 9. 落地优先级（只列动作，不列工期）

**P0（直接对应现有痛点，改动小、收益大）**

1. bash 工具：`timeoutMs` 到期改为"转 background job"，新增 `job_output` / `job_list` / `job_kill` 三个工具；job 加 `persist_after_turn`。
2. 工具结果：超过 8,192 字符先做 head 4,096 + tail 1,024 裁剪；完整内容落盘并在结果里给路径 + 取回提示。
3. 成本闸门：按 5.6 的公式在网关前做 `spent + next_worst > budget` 判断；预算耗尽 verdict = `INCONCLUSIVE(budget)`。
4. verdict 枚举扩成 `PASS / FAIL / INCONCLUSIVE(timeout|aborted|infra|budget|max_tokens|unknown_outcome)`，`INCONCLUSIVE` 不触发有副作用的自动重试。

**P1（解决上下文膨胀与提示膨胀）**

5. 系统提示拆成"冻结核心（≤4K tokens）+ 动态 `<system-reminder>` 追加"；核心不随 turn 变。
6. 动态上下文按内容哈希去重，未变不写事件。
7. 压缩入口改成"先 prune → 重新度量 → 还不够才 summarize"；阈值用 `floor(min(W×0.8, W−O−65536))`，小窗口单独配 `headroomTokens`。
8. thinking 的 `reasoning_content` 全程保留并回传（带 tools 时），且不进前端展示。

**P2（归因与评测）**

9. 增加 `minimal` profile（bash + edit 两工具），同一任务集跑 3 次，和 `full` 对比。
10. 事件账区分 "durable fact" 与 "live stream"，verdict 只读 durable。
11. 记录 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，把 cache hit rate 做成 dashboard 一等指标。
12. 大块探索（读全仓、跑长测试、查日志）走 subagent，父级只收 `output` + 可选 `structured`。
