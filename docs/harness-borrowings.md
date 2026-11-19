# 别人家的 harness：可以抄的七件事

> 来源分一手与二手，下面逐条标了。
>
> 关于流传最广的「1.6% 是 AI 逻辑」——**出处已坐实**：Jiacheng Liu / Xiaohan Zhao /
> Xinyi Shang / Zhiqiang Shen，*Dive into Claude Code: The Design Space of Today's and
> Future AI Agent Systems*，arXiv 2604.14228（v3，2026-10-10），VILA Lab（MBZUAI，UCL 合作），
> 代码在 `github.com/VILA-Lab/Dive-into-Claude-Code`。分析对象 Claude Code v2.1.88：
> 1,884 文件、约 51.2 万行 TypeScript。
>
> 但**这个数要打折看**：它比的是「AI 决策逻辑」与「确定性基础设施」两类代码，怎么划线是
> 作者的判断，不是量出来的。仓库自己还有一句更该记住的：跨 harness 的差异
> **可能不超过同配置重复运行的方差**。所以别拿 1.6% 去推「换个 harness 就强几十倍」。

## 一、先看一句最相关的话

> "Your agent is 1.6% model. The rest is the harness."（agenticup.dev，二手）
>
> "about ten architectural decisions move agent scores, and none of them show up in any
> benchmark scorecard."（dev.to 书评，二手）

这和今天量出来的东西是同一件事：一个普通编程任务的首屏上下文里，**工具说明 23,670 字符、
任务内容 1,000 字符**——模型 97% 的注意力被说明书占着。谁的 harness 把这份噪声压得低、
把状态喂得准，同样的模型就多出几十倍的有效输出。

## 二、逐条对照

### 1. 完整上下文重置（fresh context + 交接文件）——最该抄

- **他们**：Ralph Loop（ghuntley，一手）——`while :; do cat PROMPT.md | claude -p; done`。
  每次迭代**全新上下文**，只从磁盘读状态（`prd.json` / `progress.txt` / git log）。
  Addy Osmani 2026 的总结里更直接：Anthropic 自己承认**光靠压缩不够**，长任务需要
  "tear the session down and rebuild it from a compact hand-off file"（二手，但与我们实测吻合）。
- **我们**：26 步 1.79M token，prompt 从 19k 涨到 83k；压缩阈值是 `0.82 × 1M`，
  **永远不触发**。模型最后 14 步里有 5 次工具失败，一直在读自己刚写过的代码。
- **抄什么**：不是"压得更狠"，是**主动重启**：当出现
  「连续 N 步无新证据」+「上下文 > X%」时，写一份交接文件（目标 / 已完成 / 未决 / 失败清单 /
  关键文件路径），然后用它开一个新的 run。我们有 `handoff_notes`，缺的是"主动触发"。
- **判据**：同一个任务，重启版的总 token 应显著低于不重启版，且交付质量不降。

### 2. 压缩后的续写指令必须写「不要复述」

- **他们**：`"This session continues from a previous conversation. Recent messages are
  preserved. Don't recap — just keep going."`（mingz.dev，二手）——没有这句，模型下一次
  回复会礼貌地把摘要再总结一遍，纯浪费。
- **我们**：压缩头写的是「上下文已压缩：省略 N 条较早执行消息；以最近状态和任务证据为准。」
  ——说了"以什么为准"，没说"别复述"。
- **抄什么**：一句话而已，改 `context.py` 两处字符串。**这是今天性价比最高的一行改动。**

### 2.5 空闲压缩：绝对量 + 空闲时长 + 卡在缓存过期前（一手，数字明确）

- **他们**：Claude Code 在 **200K+ token** 的会话**空闲约 54 分钟**后压缩，赶在
  **1 小时 prompt cache 过期**之前动手（README 明确给出的少数数字之一）。
- **我们**：阈值是 `0.82 × context_window(1M) = 820K`，**永远不触发**；26 步跑到 83k
  还在硬扛，最后靠"跑完了"结束。
- **抄什么**：触发条件改成三条任一命中——① 绝对量到 X；② 空闲 N 分钟；
  ③ 距上次压缩已跑 M 步。百分比阈值在 1M 窗口上等于没有阈值。
- **为什么重要**：这直接决定"长任务会不会烂在上下文里"，是我们剩下那个最大窟窿的入口。

### 2.6 给新模型删掉 80% 的 system prompt（一手，最反直觉）

- **他们**：Anthropic 为新一代模型砍掉 Claude Code **80% 以上**的 system prompt，
  编码评测**无可测损失**。仓库结论："a harness should be reviewed and pruned as models
  change, rather than only accumulating instructions."
- **我们**：系统提示 1 万字符、工具说明 2.3 万字符，只加过、没删过。
- **抄什么**：把"删提示"当成常规动作而不是事故。每次换模型/换版本，先做一次
  「删一半，看评测掉不掉」；掉不了就永久删掉。判据：同一批任务，删前删后的交付成功率与步数。

### 3. 大工具结果落盘，只回摘要（tool-call offloading）

- **他们**：Addy——"keeps the head and tail tokens above a threshold and offloads the full
  output to the filesystem, where the agent can read it on demand"（二手，但理念清楚）。
- **我们**：`payload.py` 直接**丢掉**中段（`run_shell` 6k+8k，其余折成一句"已省略 N 字符"），
  丢掉的没法取回；`git diff` 稍大就只剩前后两段。
- **抄什么**：超预算的结果整份写进 `.blank/tool-results/<step>-<tool>.txt`，
  回执里给**路径 + 头尾**。省 token 的方式从"删"变成"挪"。我们已经有工作区、有 `read_file`，
  这条路零新依赖。

### 4. 成功沉默、失败喧哗（hook 里跑 typecheck / lint / test）

- **他们**：HumanLayer / Addy——"success is silent, failures are verbose"：一条编辑之后
  自动跑校验，过了就什么都不说，挂了把错误原文塞回循环（二手）。
- **我们**：校验要模型自己想起来调 `run_tests`；真机里它连调两次都超时，最后干脆不验。
- **抄什么**：给 `PostToolUse` 挂一个**默认**校验钩子（改了 `.tsx/.py` 就跑对应的
  typecheck/单测子集），静默成功、失败注入。我们已有 hooks 基建，缺的是默认策略。

### 5. 推测性工具执行：模型还在流式输出，读工具先跑

- **他们**：Claude Code 在模型还在出 token 时就把「必然只读」的工具发出去
  （"you already know it's going to grep"）（二手，书里列为"十个决定分数之一"）。
- **我们**：`PARALLEL_SAFE_TOOLS` 有了并发执行，但**没有**推测执行；每一步都等模型说完。
- **抄什么**：对 `read_file / glob / search_code` 做一次"提前量"：解析到第一个完整工具
  参数的瞬间就发出去。收益是延迟，不是 token；风险是白跑一次读。
- **先别做**：这条改动面在流式解析器上，收益是体感不是质量，排后面。

### 6. 工具说明按需加载（我们刚做完，可以对照他们的说法）

- **他们**：Anthropic 的 skills 是"frontmatter at startup, full content on invocation"；
  工具的 `shouldDefer` + `tool_search` 同理（二手）。
- **我们**：今天已经把可见工具 44 → 27、说明 23,670 → 16,382 字符，首调 prompt
  19,631 → 16,156 token。这条**已经抄到位**，接着要守的是预算（`SCHEMA_BUDGET_CHARS`）。

### 7. 输出上限的"留槽 + 截断才升级"

- **他们**：默认 `max_tokens = 8K`，只有检测到 `stop_reason == max_tokens` 才用 64K 重试
  ——因为 max_tokens 是向 API 预留 GPU 座位，99% 的回复用不到（P99 = 4,911 token）（二手，讲得很具体）。
- **我们**：`grown_max_tokens()` 已经是"截断就翻倍续写"，方向一致；
  差别是我们**续写**（新请求接上去）而不是**升级重发**。续写更省，但会多一次请求。
- **结论**：这条不用改，属于我们已经做对的。

## 三、立刻能做的（按性价比）

| 动作 | 改动面 | 预期 |
|---|---|---|
| ① 压缩续写加「不要复述，接着干」 | 两处字符串 | **已做**（`1f24c6f`） |
| ② 大结果落盘，回执给路径 + 头尾 | `payload.py` + 新增 `.blank/tool-results/` | `git diff`/测试日志不再被砍中段 |
| ③ 空闲压缩（绝对量 + 空闲时长 + 每 M 步） | `context.py` 的 water 判定 | 长任务不再"永远不压缩" |
| ④ 默认校验钩子（改了就跑 typecheck/单测子集） | `hooks` 默认策略 | 模型不必"想起来"验证 |

再往后才是：**主动重启（交接文件）**——它最大，也最需要判据先立起来。

## 四、参考

- ghuntley，Ralph Loop（一手）：https://ghuntley.com/ralph/
- Addy Osmani，Agent Harness Engineering（二手综述，覆盖面最好）：https://addyosmani.com/blog/agent-harness-engineering/
- mingz.dev，8 patterns from reverse-engineering Claude Code（二手，逐条具体）：https://mingz.dev/blog/agent-harness-patterns
- claude-code-from-source，第 17 章性能（二手，slot reservation 与缓存边界讲得最细）：https://github.com/eramax/claude-code-from-source/blob/main/book/ch17-performance.md
- agentpatterns.ai，Loop Strategy Spectrum（积累 / 压缩 / 全新上下文三种策略怎么选）：https://agentpatterns.ai/loop-engineering/loop-strategy-spectrum
- **一手**：Liu / Zhao / Shang / Shen，*Dive into Claude Code*，arXiv 2604.14228（v3）：
  https://arxiv.org/abs/2604.14228
- **一手**：论文官方仓库（含架构图、设计指南、跨系统对比）：
  https://github.com/VILA-Lab/Dive-into-Claude-Code
- **二手解读**（用户贴的那篇，转述准确但请以上面的原文为准）：
  https://agenticup.dev/posts/claude-code-harness-architecture-98-percent/
