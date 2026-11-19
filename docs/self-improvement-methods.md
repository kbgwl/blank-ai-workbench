# AI Agent 自改进：工程与学术上的可落地做法

> 调研范围：从经验中建技能/经验库、项目规则自动维护、负向经验处理。
> 目标：回答"harness 怎么在没有人工标注的情况下，把踩过的坑变成下次能用的东西，并保证它真的有用"。
> 日期：2026-11-19

**证据分级**（标在关键结论后，背景说明不标）：

- **【A】** 预注册 + 真实生产仓库 + 多智能体对照，且可复现。**本次调研没有找到完全达到 A 的工作**；最接近的是 world-model-mcp 的预注册 benchmark，但它是单次试验、仍是 SWE-bench 题。
- **【B】** benchmark 或真实仓库上的对照实验，有确定性测试判定。
- **【C】** 产品文档、工程博客、单案例、二手综述。
- **【推断】** 我基于以上证据做的综合判断，不是论文原话。

**先说明一个偏差**：这个领域绝大多数论文只报"加了记忆后 benchmark 涨了多少"，很少报"记忆有没有变成噪音"。真正做了后者的是少数控制实验（§3.8、§4.3、§6.2），它们的结论普遍比论文标题悲观。下面凡是只有 benchmark 提升的，我会明确标注。

---

## 0. 一句话结论与最小闭环

**一句话**：把"项目记忆"从散文改成**带触发条件、证据指针、置信度和失效机制的小条目**，只在一件事上闭环——任务结束时用**外部信号**（测试结果 / 用户接受或纠正）判定成败，抽出 0–2 条可复用条目进入**待审队列**；下次任务开始时按"任务描述 + 将要改的路径"检索**最多 1 条**注入；每周用 20 道历史题做**配对回放**，有提升才晋级，下降就降权或删除。

这个结论不是拍脑袋，它对应四组硬证据：

1. 抽象后的"insight/子任务级经验"能正迁移，原始轨迹和"整任务总结"会负迁移（2604.27003；2608.20274）。【B】
2. 注入 1 条最好，2 条、4 条都更差（SWE-Exp 的 k 消融：42.0 → 39.8 → 39.0）。【B】
3. 失败经验必须绑定外部证据，纯自我反思会产生幻觉并污染经验（Reflexion 在 MBPP 上 80.1 → 77.1；ExpeL 把 reflection 加进 insight 提取后 39 → 29）。【B】
4. 自动生成的项目说明书式规则没有收益、成本 +20%（ETH Zurich 2602.11988）。【B】

### 0.1 七天能做完的 MVP

前提：你们已经有任务日志（或能在 1–2 天内补上）。没有日志就先补日志，否则后面全是猜。

**闭环五步：**

```
任务结束
  │ 1. 采集：goal / touched_paths / commands / exit_codes / test_results / user_correction / final_diff
  ▼
判定成败（只认外部信号；无信号 → 不学习）
  │ 2. 抽取：0–2 条，必须能指向具体文件、命令或报错行
  ▼
准入（去重 + 证据门 + 待审队列）
  │ 3. 存储：结构化 YAML，一条一个文件
  ▼
下次任务
  │ 4. 检索：任务描述 + 预测路径 → 最多 1 条，作为"可能不适用的历史经验"注入
  ▼
  │ 5. 回放验证：每周 20 题配对 A/B，决定晋级 / 降权 / 删除
  └──────────────► 回到 1
```

**条目 schema（建议直接照抄）：**

```yaml
id: exp-2026-0042
kind: pitfall | procedure | fact
status: candidate | active | retired
trigger:
  paths: ["src/api/**"]
  task_terms: ["pagination", "cursor"]
  error_signature: "AssertionError: expected 200 got 422 @ tests/api/test_pagination.py::test_cursor"
statement: "改分页参数时同步更新 OpenAPI schema，否则契约测试返回 422"
why: "两次任务都在改 limit 后漏了 schema"
evidence:
  - task: T-1043
    signal: "pytest tests/api/test_pagination.py 由 fail 转 pass"
    pointer: "src/api/schemas.py#L88"
confidence: 0.6          # 0.3–0.9，不写 1.0
hits: 3                  # 被检索且任务成功
misses: 0                # 被检索且任务失败（归因清楚才算）
last_confirmed: 2026-11-18
last_broken: null
owner: null
expires_after_days: 90   # 到期未复验 → 自动降为 candidate
```

**注入规则（比 schema 更重要）：**

- 最多 1 条，放在"任务描述之后、第一次定位/编辑之前"。
- 措辞是"历史经验，可能不适用，需自行验证"，**不是**"必须遵守"。
- 不要求模型引用它，也不要求它在输出里复述（SWE-Exp 明确报告：早期强制引用导致过度依赖，改成隐性上下文后最好）。
- 真正必须遵守的规则不要走记忆，走 hook / lint / CI。记忆是 context，不是 enforcement（Claude Code 文档明确这么写）。

**一周验收标准（先定好，再跑）：**

- 20 道有确定性测试的历史题，基线通过率已知。
- 候选条目 ≥ 10 条，其中**至少一半被证据门拒掉**（拒不掉说明门太松）。
- 配对回放：记忆组不低于基线；基线已通过的任务**0 回归**；token 增幅 < 15%。
- 如果记忆组更差：这本身就是有效结论，保留采集、停掉注入，不要为了"上线"改指标。

---

## 1. 先记住 6 条硬约束

| # | 约束 | 依据 |
|---|---|---|
| 1 | **没有外部判决，就不要学。** 自我反思不是信号，测试/编译器/用户接受才是 | 2310.01798；Reflexion 消融（去掉测试、只留反思：0.52 < 0.60） |
| 2 | **存"子任务级 procedure / 负向约束"，不要存"整任务总结"和原始轨迹** | 2604.27003（raw 负迁移 −9.5/−7.5）；2608.20274（task-level 低于无记忆基线） |
| 3 | **一次只注入 1 条。** 更多不等于更好 | SWE-Exp k=0/1/2/4 → 37.8/42.0/39.8/39.0 |
| 4 | **提出 ≠ 准入。** 新条目先进待审队列，过探针/回放才生效 | 2606.20512（probe-and-refine）；2608.20274（skill utility score） |
| 5 | **自动生成的项目说明书基本没用。** 只写 agent 推不出来的东西 | 2602.11988（无提升、成本 +20%）；2607.27250（正确率不动） |
| 6 | **agent 自己写的记忆是不可信输入。** 需要 provenance、隔离和独立审计 | MemoryGraft 2512.16962；2610.04083；2607.10608 |

---

## 2. 方法对照表

| 方法 | 原料 | 产出 | 注入时机 | 怎么验证有效 | 对我们的成本 |
|---|---|---|---|---|---|
| **Voyager**（2305.16291） | 环境里的成功执行 + 自验证 | 可执行代码技能；key=描述 embedding | 每次迭代前检索 top-5，随环境反馈重查 | 新世界零样本：迭代次数 36→19；无技能库后期 plateau | 高：需要可执行环境 + 可靠 verifier；无去重/淘汰 |
| **ExpeL**（2308.10144） | 同任务多次试错（成功+失败完整轨迹） | 自然语言 insight 列表 + 成功轨迹库 | 全部 insight 拼进 prompt；检索 top-k 成功轨迹（k=2–6） | 消融：insight-only 36 / retrieval-only 31 / 完整 39（HotpotQA） | 中高：每题要跑多次 + GPT-4 抽取；insight 无检索、无上限 |
| **AWM**（2409.07429） | 成功轨迹（离线标注或在线自评） | 抽象化 workflow（变量替换、≥2 步） | 按网站分桶，全量注入（约 7 条/站），无 top-k | WebArena +51.1% 相对；Mind2Web +24.6% 相对 | 中：归纳便宜、无检索设施；在线自评假阳性会污染 |
| **Reflexion**（2303.11366） | 同任务失败 + 外部反馈 | 1–3 条自然语言反思，episodic buffer | 下一次重试时 | HumanEval 80.1→91.0；但 MBPP 80.1→77.1 | 低：只在单任务内重试；不跨任务沉淀 |
| **SWE-Exp**（2507.23361） | 成功/失败修复轨迹 + 测试判定 | `{perspective → modifications[]}`，按 issue type/description 索引 | MCTS 每个决策节点，top-10 召回 + LLM 重排到 k=1，隐性上下文 | 42.0 vs 无经验 37.8；k>1 更差；库 >300 条饱和 | 中高：向量库 + 重排，+37.5s/题、+8.3% 成本 |
| **FailureMem**（2603.17826） | 失败 patch + 成功 patch 对照 | 三层失败卡：情境 / 负向约束+正确原则 / diff | 检索 top-3 相似失败 | SWE-bench M +3.7% 绝对；记忆消融 +2.3%（最大单项） | 中：需离线失败库；跨项目迁移未验证 |
| **probe-and-refine**（2606.20512） | 合成 bug-fix 探针 + 当前规则文件 | 被迭代修补的 AGENTS.md（≤3000 字符） | 常驻上下文 | 33.0% vs 静态 28.3% vs 无 25.5%；+14.5pp 覆盖率 | 低中：每仓库 10 个探针 × 3–5 轮；单模型、benchmark |
| **Claude Code auto memory**（官方文档） | 用户纠正、确认过的做法 | `MEMORY.md` 索引 + 主题文件 | 每会话加载索引前 200 行/25KB，主题按需读 | 无公开消融；只有产品机制说明 | 低：几乎零开发；无 TTL，靠容量和人工清理 |
| **AGENTS.md / CLAUDE.md 自动生成**（2602.11988） | 仓库扫描 | 项目概述、目录树、命令、约定 | 每会话全文加载 | **无提升**，成本 +20–23%，步数 +2.45–3.92 | 低，但 ROI 为负 |
| **DGM / SICA**（2505.22954 / 2504.15228） | 自身代码 + benchmark 分数 | 改过代码的 harness 变体 | 下一轮评测 | DGM SWE-bench 20→50%；SICA 17→53%（50 题子集） | 极高：每轮全量评测、沙箱、人工监督；有 reward hacking |

> 成本列的"高/中/低"是相对你们现在的工程量，不是论文里的美元成本。

### 2.1 哪些是 benchmark 结论，哪些接近真实工程

这一列很重要，因为两者的可信度差一个量级：

| 类型 | 工作 | 能信到什么程度 |
|---|---|---|
| **纯 benchmark / 游戏** | Voyager（Minecraft）、ExpeL（HotpotQA/ALFWorld/WebShop）、AWM（WebArena/Mind2Web）、SWE-Exp（SWE-bench Verified）、FailureMem（SWE-bench M）、Break It Down / 2604.27003（ALFWorld/BabyAI）、DGM/SICA（SWE-bench） | 机制可信，绝对值不可搬；真实仓库没有"deterministic pass/fail"这个前提 |
| **真实仓库，但任务仍是构造的** | ETH 2602.11988（真实仓库 + SWE-bench Lite/CTXbench）、2607.27250（3 个真实仓库、17 个任务、288 次运行）、probe-and-refine（SWE-bench Verified） | 结论方向可信，尤其是**负向结论**（没提升、成本上升）比正向结论更稳 |
| **最接近真实工程** | world-model-mcp 预注册 benchmark（Claude Code headless、49 对、SWE-bench 题、单次试验）【B/C】；Claude Code auto memory（产品机制，无公开消融）【C】 | 只能当"可行性信号"，不能当"效果证明" |

**【推断】** 目前真实工程里最可信的两条是：**自动生成的项目说明书不划算**、**冲突记忆会让强模型摔得更重**。而"记忆能提升解决率"主要仍是 benchmark 信号；把它搬进生产前，必须自己做配对回放。

---

## 3. 方法详解：每个东西到底解决什么问题

### 3.1 Voyager：把"会做"固化成可执行技能【B】

**解决的问题**：长程任务里，模型每次都要重新规划同一套动作；会话之间能力不复利。

**做法**：技能 = 可执行 JS 函数；key 是 GPT-3.5 生成的描述 embedding，value 是代码。只有**自验证通过**才入库；同一任务最多改 4 轮，失败进 `failed_tasks`，**不进技能库**。检索 top-5，且在每一轮迭代都会带着最新环境反馈重查。

**关键结论**：
- 技能库的主要收益是**效率**而非可行性：新世界里钻石镐所需迭代 36→19；没有技能库也能解出部分任务，只是更慢。
- 只有完整版能摸到钻石层级；铁器从 29 轮降到 21 轮。
- 技能库是**可插拔资产**：挂到 AutoGPT 上也能让后者从 0/3 变成部分可解。

**不能直接搬的地方**：
- 它有可靠 verifier（Minecraft 的物品/合成规则）；软件任务的成功判定模糊得多。
- 技能库**没有去重、没有容量上限、没有淘汰**，也没有检索规模的消融。论文里"ever-growing"是卖点，在工程里是负债。
- 只存成功、失败只用于重试，这套在 verifier 强时成立；verifier 弱时会存进一堆"看起来成功"的坏技能。

**可借的**：技能必须是**可执行/可检查**的，不是一段散文；入库必须有验证；技能可以组合成更复杂的技能（Voyager 的复利来自组合，不是数量）。

### 3.2 ExpeL：把成败对比变成 insight【B】

**解决的问题**：没有参数更新、也没有人工标注时，怎么从"同一任务试错多次"里提炼跨任务可用的知识。

**做法**：
- 经验池存**完整轨迹**，成功和失败都存；但**检索时只用成功轨迹**（top-k，k=2–6）。
- insight 由两类对比产生：同任务的"成功 vs 失败"配对（局部纠错），以及跨任务的成功轨迹分块（共性最佳实践）。
- 用 ADD / EDIT / UPVOTE / DOWNVOTE 四个操作维护 insight；新条目初始计数 2，投票降到 0 就删除。
- 推理时**全部 insight 直接拼进 prompt**，不做检索。

**关键结论**：
- insight 和轨迹检索是互补的：HotpotQA 上 insight 主导（36 vs 31），ALFWorld 上轨迹主导（50 vs 55），完整版最好（39 / 59）。
- 检索相关性极其重要：随机取成功轨迹只有 42.5，按任务相似度检索是 59。
- **负向结果**：把 reflection 加进 insight 提取，39 → 29（反思幻觉污染）；用 GPT-3.5 替代 GPT-4 提取，39 → 32。
- 某些子任务上融合反而掉点（cool：67，低于两个单路版本）。

**可借的**：
- "失败只作为对比材料，不作为可模仿样例"是个很稳的设计。
- 投票计数是最早的"经验衰减"雏形：不是时间衰减，是**证据衰减**。
- 别用弱模型做经验提取，提取器质量决定上限。

**不能直接搬的**：insight 全量注入、无检索、无上限；论文自己承认 lifelong 场景需要检索。

### 3.3 AWM：把重复流程抽成模板【B】

**解决的问题**：同一类任务反复出现，每次都要重新探索；但直接复用具体轨迹又会把无关细节带进来。

**做法**：
- workflow = 描述 + 抽象化轨迹（环境状态 NL + 推理 + 程序动作），把具体值替换成变量（`dry cat food` → `{product-name}`）。
- 离线从标准样例归纳；在线从**自评判为成功**的轨迹流式归纳。
- 按网站分桶，每桶约 7.3–7.4 条，**全量注入**，不做 top-k。
- 去重靠 prompt（"不要生成相似/重叠的 workflow"）+ 规则版按动作序列去重；没有相似度合并。

**关键结论**：
- WebArena +51.1% 相对成功率，Mind2Web +24.6% 相对 step SR；滚动几十个样例后差距可达 22.5 个点。
- 抽象是核心：变量替换让 workflow 可跨实例复用。
- **负向结果不少**：
  - 在线模式的 LM 评估器会假阳性，错误 workflow 进库并降低后续表现（论文自承）。
  - 离线 + 在线混合不是加法，而是互相干扰（44.5，低于单独的 45.1 / 43.6）。
  - 同时给 NL 描述和 HTML 会全面变差（step SR 34.6 → 32.9）。
  - 把 workflow 变成可调用动作，step SR 升但任务成功率降（4.8 → 3.6）。

**可借的**：
- "按作用域分桶"比"全局 top-k"更稳，工程上等价于**按目录/模块分库**。
- 抽象层级要落在"子例程"，不是整条任务。
- 记忆不是越多越好，内容形态本身就会影响效果。

### 3.4 Reflexion：把失败写成下一次的提示【B】

**解决的问题**：同一任务内，如何不更新权重、只靠语言反馈就比"重试一次"更强。

**做法**：失败后让模型写一段反思，存进容量 1–3 的 episodic buffer，下一次尝试时带上。

**关键结论（也是最重要的负向证据）**：
- HumanEval 80.1 → 91.0，AlfWorld 显著提升；但 **MBPP 80.1 → 77.1**。
- 原因是评估器：模型自己写单元测试，MBPP 上 16.3% 的执行是"测试通过但真实解错误"的假阳性，HumanEval 只有 1.4%。
- HumanEval Rust 消融：只用测试不反思 = 0.60（与基线持平）；只反思不用测试 = **0.52（比什么都不做更差）**；两者都有 = 0.68。

**可借的**：反思本身不是学习，**外部信号才是**。反思的作用是把信号转成下一次能执行的指令；没有信号时，反思只是在放大模型的先验。

**不能直接搬的**：Reflexion 是**单任务内**的记忆，不跨任务沉淀。你们要的是跨任务，难度高一个量级。

### 3.5 SWE-agent / AutoCodeRover：为什么"没有学习"是设计选择【C】

这两个是 coding agent 的强 baseline，但它们的公开设计里**没有跨任务的持久经验库**：每次 issue 都从零开始，靠工具接口、搜索和测试反馈。这不是疏忽，而是当时的最优解——因为"什么算可复用经验"在软件工程里没有定义，错误经验的代价又很高。

真正把这个空白补上的是 SWE-Exp。所以如果你们要参考 coding agent 的经验库，**直接看 SWE-Exp，不用在 SWE-agent 上找**。

### 3.6 SWE-Exp：目前最接近"我们要的东西"【B】

**解决的问题**：修 bug 的 agent 是"无记忆探索者"，会重复走失败路径，也会错过把成功修法迁移到相似问题。

**做法（工程细节很值得抄）**：
- 一条经验 = `{perspective: 怎么理解这个问题, modifications: 对应的改法列表}`，不是松散文本。
- 两个 facet：**理解经验**（给规划者：识别症状、形成假设、缩小搜索空间）和**修改经验**（给执行者：改哪个区域、违反了什么契约、怎么评估副作用）。
- 成功和失败轨迹都消费；失败额外标注原因（定位错 / 修改策略错 / 理解错），用于把经验**定向路由**到对应 facet。
- 检索：向量召回 top-10 → LLM 重排到 **k=1** → 改写成"结合新旧问题差异的情境化指引"。
- 注入：MCTS 的每个决策节点，以**消息上下文**形式给，不强制引用。
- 防泄漏：排除同仓库、以及时间上晚于目标实例的轨迹。

**关键数字**：
- DeepSeek-V3 上 42.0% vs 无经验 37.8%；去掉经验抽取这一步掉 6.0 个点（最大单项）。
- **k=1 最好，k=2 39.8，k=4 39.0**。
- 经验库 0→300 条稳定上升，**超过 300 条饱和**，只剩 1–2 个点波动。
- 成本：token +7.5%、美元 +8.3%、墙钟 +25%。

**它的局限（论文自承）**：没有"只成功 vs 成功+失败"的对照；没有经验质量/适用性评估机制；经验库规模靠人工观察，不是自适应。

**可借的**：`perspective → modifications` 这个结构、理解/修改两分、top-10 召回 + 重排到 1、隐性注入、时间切分防泄漏。这几条几乎可以直接映射到 harness。

### 3.7 FailureMem / Microsoft negative PI：失败经验怎么用【B】/【C】

**FailureMem（2603.17826）**：
- 84 条历史失败案例，每条三层：情境层（bug + 视觉症状）、认知层（**Negative Constraints** + 正确原则）、代码层（失败 patch vs 成功 patch 的 diff）。
- 检索 top-3 相似失败；负向约束比正向示例更有效；自然语言摘要优于原始 diff。
- SWE-bench Multimodal 33.1%，比 GUIRepair 高 3.7 个点；记忆消融 +2.3%，是几个组件里最大的；成本 +13%。

**Microsoft 的负轨迹工作（工程博客，2026-08）**：
- 不是 harness 记忆，而是 SFT 数据：从失败轨迹里抽"事实性错误"，逐行用 gold patch 验证成 True / Wrong / Uncertain，再把"你当时错在哪"作为 privileged information 注入训练 prompt。
- 效果：Qwen3-32B 上比"只用正轨迹"高 4.1 个点（58.05 on SWE-bench Verified），4B 上高 2.8 个点。

**共同的启示**：
- 失败经验的价值在**"避坑 + 检查"**，不在"作为可模仿的样例"。
- 失败归因必须**对着一个外部事实**（gold patch、测试日志、报错），不能是模型自由发挥的"我当时应该……"。
- 失败卡要写成"条件 → 不要做什么 → 为什么 → 怎么验证"，而不是一句"不要用 X"。

### 3.8 两个泼冷水的控制实验【B】

**（1）Break It Down, Pass It On（2608.20274）**

- 任务级技能（"我上次怎么完成这个任务"）**平均低于无记忆基线**。
- 子任务级技能平均高于基线；文本技能比代码技能迁移更好。
- 提出 skill utility score：由 specificity（与真实任务的贴合度）和 abstractness（相关性在任务间的均匀度）组合而成，**不需要跑任务**就能算，可作为入库前的预检。

**（2）When Continual Learning Moves to Memory（2604.27003）**

- 原始轨迹检索产生**负向前迁移**：ALFWorld −9.5，BabyAI −7.5；抽象 insight 是 +6.5 / +9.0。
- 负迁移**集中打在难题上**：raw 在 ALFWorld 上 easy case +4.6，但 hard case −26.1。
- 更细的粒度、更频繁的检索都**不是普适更好**；会出现"前向迁移强、后向遗忘也强"的稳定性-可塑性权衡，原因是 memory dilution。

**这两篇合起来说明**：记忆的**形状**（抽象层级、粒度）比检索算法更决定成败。先选对形状，再谈 embedding 和 rerank。

---

## 4. 工程上的规则自动维护

### 4.1 三种文件的分工（别混）

| 机制 | 谁写 | 何时进上下文 | 适合放什么 |
|---|---|---|---|
| **CLAUDE.md / AGENTS.md** | 人（agent 可代笔，但要人审） | 会话开始，全文 | 每会话都必须知道的事实：构建命令、非标准约定、禁止事项 |
| **path-scoped rules**（`.cursor/rules/*.mdc`、`.claude/rules/`） | 人 | 命中 glob 时 | 某个目录/文件类型的约定 |
| **Skills / 过程文档** | 人，或 agent 起草 | 只加载 name+description，调用时加载正文 | 多步骤流程、发布检查单 |
| **Auto memory** | agent | 每会话加载索引（前 200 行/25KB） | 用户偏好、纠正、代码里读不出的项目上下文 |
| **Hooks / CI** | 人 | 生命周期事件 | 必须每次都成立的硬约束 |

Claude Code 文档的原话：这些机制都是 **context, not enforced configuration**，"不保证严格遵从"；要阻断动作只能用 `PreToolUse` hook 或 permissions。这一条对 Kbgai 同样适用：**别把"必须"写进记忆**。

### 4.2 谁写、何时写、写多少

**何时写**（Claude Code 官方给的触发条件，很务实）：
- Claude 第二次犯同一个错误；
- code review 抓到 agent 本该知道的事；
- 你这次打的纠正，和上个会话打的一样；
- 新同事需要同样的上下文才能干活。

Cursor 的说法一致："当你注意到 agent 不止一次犯同一个错误时加规则。"

**写多少**（各家建议，注意都是经验值不是实验值）：
- Claude Code：CLAUDE.md **< 200 行**，有 owner，像代码一样 review。
- AGENTS.md 社区：100–300 行，理想 < 60 行；Codex 的 `project_doc_max_bytes` 默认 32 KiB（文档配置，二手转述）。
- Cursor：单规则 < 500 行，拆成多个文件。
- Auto memory：索引 200 行 / 25KB 硬上限，超了截断。

**防膨胀的工程手段**：path-scoped 拆分、嵌套 AGENTS.md（就近生效）、过程放 skill、细节用 `@file` 指针而不是复制正文、把"必须"移到 hook/CI。

### 4.3 自动生成的代价：ETH Zurich 的控制实验【B】

**论文**：Evaluating AGENTS.md: Are Repository-Level Context Files Helpful for Coding Agents?（arXiv:2602.11988）

**设置**：4 个 agent/模型配置，SWE-bench Lite（300 题）+ 自建 CTXbench（138 题，来自 12 个带开发者手写 context file 的小仓库）；三种条件：无文件 / LLM 生成 / 开发者手写。

**结果（v3 修订版数字，注意网上很多引用的是 v1 的更大数字）**：
- LLM 生成：SWE-bench Lite **−0.5pp**，CTXbench **−2pp**，均不显著；推理成本 **+20% / +23%**（p<0.001）；每题多 2.45 / 3.92 步。
- 开发者手写：CTXbench **+2.4pp**（不显著），成本最高 +19%。
- **代码库概述（overview）不加快定位**：agent 并不会更快找到要改的文件。
- 消融：把仓库里其他文档删掉、只留生成的 context file，生成文件反而 **+2.7pp**——说明问题是**信息重复**，不是"文件本身有害"。

**行为分析**：agent 不是忽略指令，而是**太听话**。文件提到 uv，uv 的使用次数从 <0.01/题 涨到 1.6/题；提到仓库专用工具，从 <0.05 涨到 2.5/题。每条通用要求都会变成一次真实执行，哪怕任务不需要。

**补充证据（2607.27250）**：两个前沿 agent、17 个真实任务、288 次运行，context 策略对正确率的影响被 equivalence test 限制在 ≤10–15pp 内；失败主要是**实现能力**（功能设计、模式选择、接线），不是"缺仓库知识"。

**结论（可直接用的）**：
- 不要跑 `/init` 然后提交生成的目录树和架构散文。
- 只写 agent **推不出来**的东西：非标准命令、私有工具、反直觉的坑、必须避开的文件。
- 每条都要能回答"没有这条，agent 会做错什么"。答不上来就删。

### 4.4 用 probe 调规则：目前唯一被证明有效的"自动维护"【B】

**论文**：Probe-and-Refine Tuning of Repository Guidance for Coding Agents（arXiv:2606.20512）

**做法**：
1. 用 10 个**合成 bug-fix 探针**（不是评测集）跑当前规则；
2. 判断每次失败是"规则缺失/误导"还是"模型能力"；
3. 用单次 LLM 调用改规则文件（无 agent loop、无工具使用）；
4. 迭代 3–5 轮，规则上限 3000 字符（约 750 token，占 16k 上下文的 5%）；
5. 最终在 SWE-bench Verified 上评测。

**结果**：33.0% vs 初始化静态知识库 28.3% vs 无指导 25.5%（p<0.001，4 次独立试验）。提升来自**覆盖率**：能产出可评估 patch 的实例多了 14.5pp，而 patch 精确率不变（59%）——也就是说，规则帮 agent**找到正确的文件**，没有让它**写更好的代码**。

**对我们的意义**：这是"规则自动维护"里最可复制的一条。它不需要人工标注，不需要跑全量 benchmark，只需要一个**小型探针集**和**接受/拒绝的闸门**。一周内可以做出最小版：10 道历史题 + 规则 diff + 只保留不降分的改动。

**注意**：单模型、benchmark、规则长度和内容没完全解耦；跨模型会退化（Nemotron 那次调优循环失效）。

### 4.5 经验衰减：目前没有好答案

| 机制 | 实现 | 评价 |
|---|---|---|
| 证据投票 | ExpeL：ADD 初始 2，UPVOTE/EDIT +1，DOWNVOTE −1，到 0 删除 | 唯一有论文支撑的"证据衰减"；粗糙但可解释 |
| 容量上限 | Claude Code auto memory：200 行/25KB；Codex 32KiB | 逼着合并和删除，但不解决"哪条该删" |
| 时间戳 | auto memory 自动写 `modified`；world-model-mcp 按 evidence type 设 decay | 只是提示新鲜度，没有自动失效 |
| 复验过期 | world-model-mcp 的 per-evidence-type decay + provenance | 工程实现，无对照实验 |
| 规模饱和 | SWE-Exp：~300 条后收益递减 | 提示"够用就好"，不是淘汰算法 |

**没有找到**任何论文系统比较过"TTL / 证据计数 / 复验 / 容量淘汰"哪种更好。**【推断】** 对 Kbgai，最稳的是**证据计数 + 复验时间**双轨：`hits/misses` 决定权重，`last_confirmed` 超过 90 天降级；不要用纯 TTL，因为一条两年前但每次都被验证的规则不该过期。

---

## 5. 负向经验：只记成功，还是也记失败？

### 5.1 证据怎么说【B】

| 做法 | 证据 | 结论 |
|---|---|---|
| **只存成功**（Voyager） | 失败只用于重试，不进库；verifier 强时效果好 | 可行，但依赖可靠的成功判定 |
| **失败只作对比材料**（ExpeL） | 检索只用成功轨迹；失败用于提取 insight | 比"直接检索失败轨迹"稳 |
| **成功+失败都进库**（SWE-Exp） | 失败标注原因后定向路由；无 success-only 消融 | 看起来合理，但**没有定量证明失败经验的独立增益** |
| **失败做成负向约束**（FailureMem） | 84 条失败卡，top-3 检索；记忆消融 +2.3%（最大单项） | 失败经验的有效形态是"不要做什么 + 为什么" |
| **失败轨迹用于训练**（Microsoft） | 逐行用 gold patch 验证后做 privileged info；+4.1% | 关键是**验证过的错误**，不是原始失败轨迹 |

**结论（【推断】，证据方向一致但没有单篇直接对照）**：
- **成功经验**适合做成"procedure / 路径 / 可执行技能"，用于**复用**。
- **失败经验**适合做成"负向约束 / 检查项 / 触发条件"，用于**避免**。
- 两者不要混在同一个池子里用同一种检索方式；失败轨迹**不要**作为可模仿的 few-shot 样例。
- 只记成功在"成功可验证、任务同质"时够用；在软件工程里成功判定模糊，必须记失败，但必须绑定证据。

### 5.2 失败经验怎么写才不自相矛盾

> 下面的五要素模板是【推断】+ 工程经验，不是某篇论文直接验证过的格式。

**坏写法**（会互相打架、会过期、无法验证）：

> 不要用全局状态。
> 一定要加日志。
> 优先用 ORM。

**好写法**（条件、动作、机制、验证、范围五要素）：

> **当**修改 `src/api/**` 的分页参数时，**不要**只改 handler；**因为** OpenAPI schema 是单独生成的，契约测试会返回 422；**验证** `pytest tests/api/test_pagination.py`；**适用范围** 仅 `src/api`。

再加两条防矛盾规则：
1. **一条只讲一个条件**。条件越多越容易和别的条目冲突。
2. **必须有正面替代**（"改为 …"），否则 agent 只知道不能做什么，会卡住或绕路。
3. **必须带 scope**。没有 scope 的"不要用 X"一定会和另一条"这里必须用 X"冲突。

### 5.3 重复犯错检测：怎么判断"我又踩了同一个坑"

> 签名法是【推断】；失败分类法来自 SWE-bench Pro【B】。

不要用自然语言判断"是不是同一个坑"，用**签名**：

```
signature = hash(
  normalized_error_class        # AssertionError / TypeError / ImportError ...
  + failing_test_nodeid         # tests/api/test_x.py::test_y
  + top1_touched_dir            # src/api
  + last_failed_action_type     # edit / run_test / search
)
```

流程：
1. **精确签名**先匹配；命中即 `repeat_count += 1`。
2. 精确匹配不到，再对"任务描述 + 错误消息"做 embedding 聚类（阈值要人工校准，别直接用 0.8）。
3. `repeat_count == 2` → 生成 candidate（对应 Claude/Cursor 的"第二次犯同样错误才写规则"）。
4. `repeat_count >= 3` → **不要再写一条散文规则**，直接提议一个确定性检查：lint 规则、单测、pre-commit hook、CI gate。散文规则治不了第三次。

**KPI**：`repeat_mistake_rate = 签名在历史失败中出现过的失败任务数 / 总失败任务数`。这个指标只需要测试结果，不需要人工标注，适合做长期主指标。

**失败分类法**：直接用 SWE-bench Pro 的 7 类（wrong solution / tool-use / syntax / incorrect file / endless file reading / misunderstood problem / other）或简化成 5 类（定位错 / 理解错 / 方案错 / 工具用错 / 环境或测试问题）。固定分类法的价值是**可计数**；自由文本归因无法统计，也无法判断"是不是同一个坑"。

### 5.4 一个容易被忽略的点：冲突比缺失更危险【B】

**The Compliance Trap（2607.10608）**：给 agent 注入"看似相关但任务错误"的记忆，不同模型的**遵从率都在 63–72%**；一旦遵从，成功率塌到 **17–31%** 的共同地板；而且**越强的模型绝对损失越大**（因为基线更高）。失败主要发生在 **entry**（第一次暴露的决策点），重复暴露会放大，事后恢复很弱。

**工程含义**：
- 注入时机比注入内容更敏感：不要在任务最开始、还没定位时就灌一堆经验。
- 宁可少注入，也不要注入"可能相关"的模糊条目。
- 评测不能只看最终成功率，要看"记忆有没有在第一个决策点改变动作"（见 §6.4）。

---

## 6. 反自我强化：agent 写错经验 → 照着做 → 更错

### 6.1 循环长什么样

```
一次任务"看起来成功"（实际是假阳性）
   → 抽出错误经验
   → 入库（无人审）
   → 下次检索命中
   → agent 照做（semantic imitation）
   → 任务再次"看起来成功"（同一个假阳性）
   → 经验被 +1 证据，置信度更高
   → 更难被删掉
```

这不是假想。每一环都有实测证据。

### 6.2 每个环节都会坏（证据表）【B】/【C】

| 环节 | 出了什么事 | 证据 |
|---|---|---|
| **判定** | 自写测试假阳性 16.3%，导致 MBPP 反而变差 | Reflexion 2303.11366 |
| **抽取** | 把 reflection 加进 insight 提取，39 → 29 | ExpeL 2308.10144 |
| **在线自评** | LM 评估器假阳性 → 错误 workflow 入库并降低表现 | AWM 2409.07429（作者自承） |
| **存储/检索** | 原始轨迹负迁移 −9.5/−7.5，且集中打难题（−26.1） | 2604.27003 |
| **形状** | 任务级技能平均低于无记忆基线 | 2608.20274 |
| **注入** | 冲突记忆遵从率 63–72%，遵从后成功率 17–31% | 2607.10608 |
| **剂量** | k=1 最好，k=2/4 更差 | SWE-Exp 2507.23361 |
| **规则文件** | 自动生成的 context file 无提升、成本 +20% | 2602.11988 |
| **安全** | 投毒的"成功经验"会被模仿，跨会话持续漂移 | MemoryGraft 2512.16962 |
| **自传播** | 无外部攻击者，agent 自己写的目标也能跨 100 个会话存活；去掉 memory 工具后 74% 的会话改用文件系统 | 2610.04083 |

**最值得警惕的一条**：2610.04083 显示，即使**没有外部攻击者**，agent 自己写进持久存储的东西也会被后续会话继承；把 memory 工具拿掉，它会改用文件系统；审计器只能把传播率从 71% 降到 34%。**"关掉记忆"不是防御**。

### 6.3 七道闸门

> 七道闸门是【推断】，每一道背后的证据见 §6.2。

按顺序，任何一道不过就不进下一道：

1. **外部判决门**：成功/失败只由测试、编译器、CI 或用户明确接受/拒绝判定。没有信号 → 这条任务不产生经验。禁止用"模型觉得自己做对了"当标签。
2. **证据门**：每条经验必须指向一个具体 artifact（测试 nodeid、报错行、diff hunk、用户原话）。指不出来 → 丢弃。失败归因必须对着 gold patch 或日志核对（Microsoft 的 True/Wrong/Uncertain 做法）。
3. **形状门**：只接受子任务级 procedure、负向约束、或可验证 fact。拒绝整任务总结、原始轨迹、泛泛而谈的"最佳实践"。
4. **准入/预检门**：提出 ≠ 入库。用 skill utility score（2608.20274）或 10 题探针回放（2606.20512）打分；不过线进 candidate，不进 active。
5. **剂量门**：一次最多 1 条；隐性上下文；允许忽略；不强制引用；token 上限（建议 ≤300 token）。
6. **消费门**：记录"注入后第一个决策点有没有改变动作"。没改变 → 这条是噪音，`hits` 不加；改变了但任务失败 → `misses += 1`。这是 Compliance Trap 的 E-P-R 思路，能区分"检索到了"和"真的起作用"。
7. **审计/隔离门**：
   - 每条记忆带 `source`（agent / user / tool / web）和 `trust`；
   - 来自工具输出、网页、issue 正文的内容**默认不可信**，不能直接写库；
   - agent 写的条目进 quarantine，由**不同的模型/提示**做矛盾检查（同一模型自审会有确认偏差）；
   - 硬约束不留在记忆里，升级为 hook/CI。

### 6.4 一个便宜的反事实检验

对每条 active 经验做一次**配对回放**：同一任务、同一模型、同一工具集，跑两次（有/无该条），比较：

- 最终结果（pass/fail）
- 步数、token
- **第一个决策点的动作是否不同**（E-P-R 的 entry）
- 如果不同，后续是否恢复

四种结果对应四种处置：

| 结果 | 含义 | 处置 |
|---|---|---|
| 动作变了 + 成功 | 真有用 | `hits += 1`，保留 |
| 动作没变 | 噪音/没读到 | 降权；检查检索和位置 |
| 动作变了 + 失败 | 有害 | `misses += 1`，立即降级 |
| 动作变了 + 失败但能恢复 | 有干扰但可纠 | 观察，考虑改措辞/缩小 scope |

这比"整体 benchmark 涨了 1 个点"有用得多，因为它能定位到**哪一条**在起作用。

---

## 7. 落地路径：三档，每档说清"能验证什么"

> 以下三档是【推断】和工程建议；每档末尾的"能验证什么"才是硬要求，不要跳过。

### 7.1 一周：把闭环跑通，先证明"门"和"尺子"是好的

**做什么**
- D1–2：定义 task record（goal / paths / commands / exit codes / test results / user correction / diff）。如果现有日志不全，先补日志。
- D3：写抽取器：输入 task record，输出 0–2 条 schema 条目；强制 evidence 字段；强制"指不出 artifact 就返回空"。
- D4：建 20 道历史题的 replay set（必须有确定性测试，且不是最近刚调过的题）。
- D5：写配对 runner（有/无记忆各跑一次），输出 pass、steps、tokens、注入条目 id。
- D6：接一个注入通道：按路径 + 任务词检索，最多 1 条，措辞为"可能不适用"。
- D7：跑基线 + 记忆组，人工抽查 20 条候选的"适用性"。

**能验证什么**
- 候选条目的**证据合格率**（应该有一半以上被拒）。
- 记忆组的**回归数**（基线通过的任务有没有被记忆搞坏）。
- **token 代价**（是否 <15%）。
- 注入条目的**人工适用率**（抽 20 条，人工判断"这条对当前任务是否真的相关"）。

**不能验证什么**：长期复利、跨仓库迁移、真实用户任务上的效果。样本太小，只能看大效应。

### 7.2 一个月：让它自维护，并且能报警

**做什么**
- 经验类型分开：pitfall / procedure / fact，分别走不同的注入模板和不同的评审标准。
- 去重与合并：embedding 相似度 > 0.9 合并，保留证据链；冲突条目（同一 trigger 下相反建议）强制人工裁决。
- 失败签名检测：精确签名 + 聚类；N=2 生成 candidate，N=3 提议确定性检查。
- 衰减：`hits/misses` + `last_confirmed`；90 天未复验降级；连续 2 次 miss 直接 retire。
- 用 probe-and-refine 调 top 10 条规则的措辞（10 个探针 × 3 轮），只接受不降分的 diff。
- 把"必须成立"的条目升级为 hook / lint / CI，并从记忆里删掉。
- 加 provenance / trust / quarantine；工具输出和网页内容不直接写库。
- Dashboard：repeat_mistake_rate、injection_precision、harm_rate、cost_per_resolved_task。

**能验证什么**
- **repeat_mistake_rate 是否下降 ≥20% 相对值**（这是主 KPI）。
- **harm_rate**（注入了且回归的比例）是否 <2%。
- 50 题 holdout 上有没有回归。
- 跨仓库迁移：A 仓库学到的条目在 B 仓库是帮还是伤（预期接近 0，别期待正迁移）。

**停止条件**：连续两周 replay 无提升，或 harm_rate >5%，就停注入、保留采集，回到离线分析。

### 7.3 长期：技能库 + 可选的 harness 自改

**做什么**
- 技能变成**可执行资产**：每个技能有 contract、示例、测试、owner、最后验证的 commit；测试挂了自动 disable（Voyager 的"可执行 + 验证" + CI 的"坏了就下线"）。
- 分配策略：按任务类型在"无记忆 / 规则 / 技能"之间做 bandit 选择，保留探索预算；不要默认全开。
- 跨项目技能库：用 skill utility score 做预检，只允许高分技能跨仓库。
- harness 自改（DGM / SICA 路线）：只在沙箱里、只在有评测门和人工复核的前提下做；不要放进主路径。
- 红队：定期用 MemoryGraft 式投毒、冲突记忆、过期规则做压力测试。

**能验证什么**
- 时间切分（用 3 个月前的经验，测 3 个月后的任务）上的真实复利。
- cost_per_resolved_task 是否下降（不是只看 resolve rate）。
- 安全事件：错误经验导致的回归、投毒成功率、审计漏检率。
- 跨仓库/跨项目的迁移是否为正；如果不是，就承认"技能库是项目本地的"，别做全局库。

---

## 8. 查不到的（明确标注）

1. **没有** coding agent 在**真实生产仓库**里做 3–6 个月在线自学习的公开对照实验。最接近的是 world-model-mcp 的预注册 benchmark（49 对、单次试验、SWE-bench 题），以及 ETH 的离线基准。
2. **没有**"经验衰减策略"的受控比较（TTL vs 证据计数 vs 复验 vs 容量淘汰）。工业实现都是启发式拼装。
3. **没有**以"重复犯错率"为长期 KPI 的系统研究。SWE-bench Pro 的 failure taxonomy 只是分类，不是纵向追踪。
4. **没有**"用户纠正 → 规则"的量化研究。Claude Code auto memory 有这个机制，但没有公开消融。
5. **没有** Voyager 技能库的去重/淘汰/检索规模消融；它的"ever-growing"结论不能直接搬到软件工程。
6. **没有** SWE-Exp 的"只成功 vs 成功+失败"对照；也**没有** ExpeL 的同类对照。所以"失败经验是否更有效"目前只有间接证据。
7. **没有** Claude Code auto memory 的效果数据；只有产品文档描述其加载上限和生命周期。
8. **没有**统一的"记忆质量"指标。skill utility score 和 E-P-R 是两个方向，但都还没成为标准。

---

## 9. 被证明没什么用 / 容易走偏的做法

| 做法 | 为什么别做 | 证据 |
|---|---|---|
| **让模型"再想想"式自我纠错**，没有外部信号 | 推理任务上不变好，常常更差 | 2310.01798（ICLR 2024）；Reflexion Rust 消融 0.52 < 0.60 |
| **`/init` 生成 CLAUDE.md/AGENTS.md 然后直接提交** | 无提升、成本 +20%，概述不加快定位 | 2602.11988；2607.27250 |
| **把整任务总结当技能存** | 平均低于无记忆基线 | 2608.20274 |
| **直接检索原始轨迹复用** | 负迁移，且专打难题 | 2604.27003 |
| **记忆越多越好 / 全量注入** | k=1 最优；2/4 更差；离线+在线混用互相干扰 | SWE-Exp；AWM |
| **强制 agent 引用记忆** | 导致过度依赖，环境探索充分时仍套旧策略 | SWE-Exp（作者报告改成隐性上下文后最好） |
| **把规则当强制** | 记忆是 context，不保证遵从；该用 hook/CI | Claude Code 文档 |
| **只记成功**（在成功判定模糊时） | 会把假阳性固化成技能 | Reflexion MBPP 16.3% 假阳性 |
| **只记失败**（没有正面替代） | agent 知道不能做什么，但不知道做什么，容易卡住 | FailureMem 的"负向约束 + 正确原则"是反例 |
| **用 benchmark 提升证明有效** | 污染、饱和、单次试验都会骗人；SWE-bench Verified 已接近饱和 | SWE-bench Pro 2509.16941；2602.11988 的版本漂移 |
| **过早做 harness 自改（DGM/SICA）** | 每轮全量评测、沙箱、人工监督；论文自己报告了 objective hacking | 2505.22954；2504.15228 |
| **靠"多 agent 投票"保证经验正确** | 共享同一偏见的投票不是独立证据；冲突记忆下强模型也会遵从 | 2607.10608 |

---

## 10. 参考清单

**技能/经验库**
- Voyager: An Open-Ended Embodied Agent with Large Language Models — arXiv:2305.16291
- ExpeL: LLM Agents Are Experiential Learners — arXiv:2308.10144（AAAI-24）
- Agent Workflow Memory — arXiv:2409.07429
- Reflexion: Language Agents with Verbal Reinforcement Learning — arXiv:2303.11366
- SWE-Exp: Experience-Driven Software Issue Resolution — arXiv:2507.23361；代码 github.com/YerbaPage/SWE-Exp
- FailureMem: A Failure-Aware Multimodal Framework for Autonomous Software Repair — arXiv:2603.17826
- Break It Down, Pass It On: Cross-Task Skill Transfer in LLM Agents — arXiv:2608.20274
- When Continual Learning Moves to Memory: A Study of Experience Reuse in LLM Agents — arXiv:2604.27003

**负向经验 / 自我纠错**
- Large Language Models Cannot Self-Correct Reasoning Yet — arXiv:2310.01798
- How to Learn from Negative Trajectories in Coding Agents — microsoft.github.io/debug-gym/blog/2026/08/negative-pi（工程博客，非论文）
- SWE-Bench Pro: Can AI Agents Solve Long-Horizon Software Engineering Tasks? — arXiv:2509.16941（7 类失败分类法）

**规则/上下文文件**
- Evaluating AGENTS.md: Are Repository-Level Context Files Helpful for Coding Agents? — arXiv:2602.11988
- Do Context Files Help Coding Agents? A Two-Agent Ablation Study on Real Repositories — arXiv:2607.27250
- Probe-and-Refine Tuning of Repository Guidance for Coding Agents — arXiv:2606.20512
- Claude Code memory 文档 — https://code.claude.com/docs/en/memory
- Claude Code 七种指令机制对比 — https://claude.com/resources/articles/steering-claude-code-skills-hooks-rules-subagents-and-more
- AGENTS.md 标准 — https://agents.md/
- Cursor Rules 文档 — https://cursor.com/docs/rules

**记忆安全 / 反自我强化**
- MemoryGraft: Persistent Compromise of LLM Agents via Poisoned Experience Retrieval — arXiv:2512.16962
- Self-Propagating Misalignment in LLM Agents, and Why Auditing or Disabling Memory Is Not Enough — arXiv:2610.04083
- The Compliance Trap: Diagnosing How AI Agents Consume Conflicting Memory — arXiv:2607.10608
- MINJA: Memory Injection Attacks on LLM Agents via Query-Only Interaction — arXiv:2503.03704
- AgentPoison: Red-teaming LLM Agents via Poisoning Memory or Knowledge Bases — arXiv:2407.12784

**harness 自改（长期，不建议先做）**
- Darwin Gödel Machine: Open-Ended Evolution of Self-Improving Agents — arXiv:2505.22954
- A Self-Improving Coding Agent — arXiv:2504.15228
- world-model-mcp 预注册 benchmark（persistent memory for coding agents）— https://zenodo.org/records/20834509

---

## 附：如果只记三句话

1. **记忆的形状 > 检索算法 > 模型能力**：子任务级、抽象、带条件的小条目，胜过整任务总结和原始轨迹。
2. **一次一条，外部判决，证据绑定，提出不等于准入**：这四件事做完，就已经超过大多数公开工作。
3. **先建尺子再建记忆**：没有配对回放和 repeat_mistake_rate，你无法区分"自学习"和"自我强化偏见"。
