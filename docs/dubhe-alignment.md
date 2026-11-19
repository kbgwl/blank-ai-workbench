# DUBHE 目标架构 × 当前系统：对照、缺口与取舍

> 输入：`第一章～第十章` 目标文档（七层架构、自主运行循环、记忆分层、能力进化）。
> 目的：判断**哪些已经存在、哪些是真缺口、哪些不该重复建设**。
> 原则沿用第十章那句：**不允许为了架构好看而重复建设。**

## 一、七层对照

| 层 | 目标里的东西 | 现状 | 缺口 |
|---|---|---|---|
| 1 身份与治理 | Identity / Constitution / Goal / Governance / Policy | **部分**：`permissions.py`（四级能力边界）、`settings`（策略）、`plan.py` 的 `set_goal` | **没有身份与宪章**：没有一个"我是谁、我为什么存在、哪些原则不可改"的自存对象；Goal 只有会话级，没有 Mission→Strategic→Project→Operational→Task 的层级 |
| 2 认知与决策 | Perception / World Model / Reasoning / Planning / Decision / Opportunity / Uncertainty | **部分**：`plan.py`（结构化计划+里程碑）、`verifier.py`（事实裁决）、`review_result_node`（审查）、`stall.py`（挫败识别） | **没有世界模型与机会引擎**：不主动发现"能做什么"，只在被叫时工作；决策依据不进库 |
| 3 执行与协作 | Task / Agent Orchestrator / Model Router / Tool Bus / Workflow / Communication / Device Bridge | **强**：`tasks` 路由 + 事件账本、`team.py`（多 agent 编排）、provider 抽象（模型可换）、`tools/gateway.py`（工具总线+审批）、`workflow` 工具 | 无 Device Bridge；调度仍按"指定 provider"，没有按历史成功率/成本/负载打分 |
| 4 记忆与知识 | Working/Short/Long/Episodic/Semantic/Collective/Project/Decision | **部分**：`experience.py`（集体经验）、`persona.py`（使用者模型）、`memory.py`（项目记忆，散文）、事件账本（事件记忆） | **没有语义知识与决策记忆**：知识是 JSONL + 关键词匹配，没有图谱、没有向量检索；决策依据只存在事件里，没有可查询的 Decision 对象 |
| 5 能力扩展 | Skill Registry / Discovery / Builder / Sandbox / Evaluation / Version | **强**：`skills_defs.py`（三层加载）、`skill_market.py`（GitHub 市场安装）、`skill_proposals.py`（提案→采用）、`skillsmith.py`（从标准沉淀）、skills 表带 version/history/rollback | **没有能力发现与评估**：遇到不会做的事，不会自己判断"缺能力"并提案；新技能没有前后效果对比 |
| 6 进化与可靠性 | Evolution / Verification / Performance / Resource / Health / Recovery / Rollback | **部分**：`verifier.py`、`cost.py`（金额闸）、`stall.py`、技能 rollback | **没有进化评价**：改了什么、变好还是变坏，没有前后对照；"没有可验证改善就不算进化成功"这条**目前做不到** |
| 7 基础设施 | 数据库/存储/检索/队列/事件/日志/调度/API/监控 | **够用**：SQLite + `agent_events`（全量可回放）、FastAPI、前端 13 页、`monitor` 工具 | 无向量库、无消息队列（当前量级不需要） |

**一句话**：第 3、5 层已经强于目标文档的最低要求；第 1 层最空，第 4 层的"语义"和
第 6 层的"进化评价"是两个真缺口。

## 二、图中 10 个 Skill：判定

Star 数在截图里被头像遮挡，只作热度参考，不引具体数字。

| # | Skill | 它做什么 | 我们的对应物 | 判定 |
|---|---|---|---|---|
| 01 | karpathy-guidelines | 澄清需求，让改动有边界 | `repo-onboarding`（先摸清再动手）+ 计划硬闸 + `request_user_input` | **已有机制，不装**。缺的不是文档，是"没澄清就开干"的拦——那是机制问题 |
| 02 | ponyatil | 复用现有代码，更少改动 | `safe-refactor`（小步、每步可回滚）+ `_review_scope` 统计 +/- 行 | **等价，不装** |
| 03 | ui-ux-pro-max | 配色/字体/布局整理成统一方案 | `ui-audit` 只做**体检**，不做**生成** | **半个缺口**：我们能挑出问题，不能产出设计系统 |
| 04 | graphify | 代码与文档连成可查询知识图谱 | 无。现在只有 JSONL + 关键词 | **真缺口**，但成本最高（要 AST + 索引 + 查询），不是装个 skill 能解决 |
| 05 | archify | 想法/代码转成可交互的图 | 无 | 缺口，属交付形态；我们前端能做，但价值排后 |
| 06 | impeccable | 逐项打磨界面 | `ui-audit`（宽度/对齐/字号档位/空态/重复入口/状态色） | **等价，不装** |
| 07 | i-have-adhd | 先说结论与行动 | "简洁"做事标准（个性化）+ `report_is_short`/`no_padding` 核对 | **有核对，没有默认基线**：只有你要求过才生效 |
| 08 | humanizer | 删套话与重复 | 同 07 | 同 07 |
| 09 | diagram-design | 流程与关系画成统一风格图表 | 无 | 缺口，与 05 同源 |
| 10 | book-to-skill | 提炼书籍/文档生成技能 | `skill-authoring` + 我们刚做的 `skillsmith.py`（料是使用者标准） | **同源，料的来源不同**：可以把"从文档提炼"补进 skillsmith |

### 结论：10 个里 4 个不该装

`02 / 06 / 10` 我们已经有等价物，`01` 的问题是机制不是文档。**为了"集齐 10 个"去装
重复技能，正是第十章禁止的那种重复建设。**

真正值得做的是三件，按性价比排序：

1. **07+08 合成一个默认基线**（结论先行、删套话）——成本最低，立刻能被感知；
2. **03 的生成侧**（设计系统）——`ui-audit` 已经有了，缺的是产出；
3. **04/05/09 同源**：代码/文档 → 图/图谱，这是"理解环境"的底座，也是第 4 层语义记忆的入口，但要单独一轮。

## 三、目标文档里最该先做的一件事

不是这 10 个 skill，是第三章：**自主运行循环**。

现在的系统只在被叫时工作（`tasks` 由 API 触发）。目标文档要求 Event-driven /
Heartbeat / Dreaming 三种唤醒——我们只有第一种的一半（事件驱动的是内部状态流转，
不是"系统自己醒来看看"）。

没有这一层，第 2 层的 Perception / Opportunity 就永远没有输入，第 6 层的进化评价也
没有样本。它比再装十个 skill 重要。

但顺序要诚实：**自主循环必须建立在"验证与回滚"之上**，否则它会自己跑偏而没人拦。
所以前置条件是第 6 层的进化评价——而它现在是空的。

## 四、我建议的落地顺序

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 进化评价 ✅ 已做（`harness/evolution.py` + `scripts/evolution.py`） | 改前存快照、改后对照；样本 <20 明确不下结论 |
| 2 | 默认输出基线（07+08）✅ 已做 | 内置 skill `report-style`：结论先行、证据跟随、禁套话、四条自检 |
| 3 | Heartbeat ✅ 已做（`heartbeat.py` + `scripts/heartbeat.py`） | 同类合并、只报告不动手；还差接定时执行 |
| 4 | 目标层级：Mission→Strategic→Project→Operational | 任务能挂到目标上，目标能汇总进度 |
| 5 | 语义层（图谱或向量） | 能回答"这个模块谁依赖谁" |

前三步都不需要新基础设施，能在一两周内落地。

## 五、两条已经能用的命令

    python scripts/evolution.py --snapshot --label "改了什么"
    python scripts/evolution.py --compare data/evolution/<基线>.json
    python scripts/heartbeat.py

基线（2026-11-19，98 条已结束任务）：成功率 0.918 / 每任务 69k token /
每任务 2.38 步 / 驳回率 0.372 / 需要解释的驳回 0.062。

心跳首次巡检发现四件：2 个任务跑了 9 小时没动静、同一原因（上下文超限 400）失败 3 次、
9 个审批停着最久 26 小时、金额闸触发 2 次。**都是真事，且都不该由系统自己动手处理。**
