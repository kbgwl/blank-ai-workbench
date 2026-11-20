# 需求理解与拆分体系设计（PMP 映射，2026-11）

> 目标读者：要动手改 `services/api/src/blank_api` 的人。
> 这份文档不是方法论综述——每一条规则后面都写着它会影响哪个文件的哪一行，
> 或者它能拦住哪个已经发生过的失败。
>
> 关于 PMBOK 的引用口径：本文凡涉及具体条款，一律标注「通行理解」或「原文规定」。
> 章节号按 PMBOK 第 6 版第 5 章（范围管理，过程 5.1–5.6）书写，**未逐字核对原文**，
> 与原文有出入时以原文为准；SMART、DoD、用户故事 INVEST 不是 PMBOK 原文，会单独标出。

---

## 0. 一页纸结论

### 0.1 这套东西解决什么

一条需求从用户嘴里到交付物，在我们系统里现在有四段路，其中三段是断的：

| 段 | 现在有什么 | 断在哪 | 后果 |
|---|---|---|---|
| 理解 | `TaskCreate.goal` 存原话，`goal_spec` 有 `objective/done_when/constraints/out_of_scope` | 没有任何一层回答「他要解决什么麻烦」「成功长什么样」「谁来看」 | 用户说方案（"给我做个看板"）就被当成需求，方案照抄 |
| 拆分 | `plan_steps` 是模型随口写的字符串，`build_plan` 事后补 `kind/risk/tools` | 没有粒度约束，一条「实现整个功能」完全合法；`success_condition` 生成了但**没有任何地方读它** | 没有可验证的中间态，崩了不知道崩在哪一步 |
| 验收 | 模型自评 `covered`（`graph.py:7110`）+ verifier 逐条核对 `done_when`（`verifier.py:389`） | 判据形态不受约束，否定式判据天然通过；`done_when` 的 check 只有三种类型，**没有"页面里有什么"这一维** | 三列空框的看板，判据全过 |
| 追溯 | `acceptance_criteria[].step_ids` 把验收指到步骤 | 没有"需求"这个实体，只有步骤和判据两端 | 漏项无法机器发现，超范围无法机器发现 |

**一句话**：我们缺的不是"计划对象"（`plan_steps` 已经有了，而且比 opencode 强），
缺的是**计划里每个包的可验证性**、**判据的形态约束**、以及**需求到交付物的那条追溯线**。

### 0.2 最少要改哪三处

| # | 改动 | 落点 | 能拦住什么 |
|---|---|---|---|
| 1 | `done_when[].check` 加第 4 种类型 `page`（读 `browser_check` 的回执），并在 `_run_done_checks` 里加一支 | `services/agent/verifier.py:192-236`、`services/tools/plan.py` 的 `set_goal` schema | **空框案例**。空页面能过 `path_exists`、能过 `command`，过不了「打开时 `.task-item ≥ 3`」 |
| 2 | `plan_steps[*]` 加 `output / check / req_ids / estimate_min` 四个字段 + 一个确定性校验 `work_package_problems()`，在 `_update_plan` 里打回 | `services/plan.py:299-307`（`normalize_steps`）、`services/tools/plan.py:47-82` | 「一步实现整个功能」在计划阶段就被拒，模型必须重写 |
| 3 | `goal_spec.understanding`（五层结构 + 假设台账 + 确认状态）+ `objective_problems()` | `services/plan.py:169-175`（`normalize_goal` 的返回字典**必须加键**，否则静默丢弃） | 「给我做个看板」直接变成 `objective="做一个任务看板"` |

三处加起来约 **600 行 Python + 1 个迁移**，不新增表、不新增图节点、不新增一种中断类型。

### 0.3 明确不做什么

- **不建 PMBOK 式的独立数据表**（WBS / RTM / 范围说明书各一张表）。一次任务的范围是一次性的，
  它的生命周期跟任务一样长；跨任务复用的东西我们已经有 `plan_templates` 与 `goals`。
- **不引入"工时"**。8/80 规则在我们这儿没有数据支撑（我们没有工时账本），硬套就是假的。
  用**工具调用次数**替代小时数，理由见 §4.1 第 6 条。
- **不把 verifier 改成 blocking 的验收器**。它现在的纪律是"只摆事实、不改判"
  （`verifier.py:197-199` 的注释写得很清楚），这条纪律要继续守。新的门加在**计划阶段**
  和**范围确认阶段**，不加在事实核对阶段。

---

## 1. 我们现在的需求链条与断点

按数据流顺序，逐个环节给文件行号。

### 1.1 需求进入

| 事实 | 位置 |
|---|---|
| 建任务只收 `title / goal / acceptance_criteria / goal_spec / scenario` | `schemas/task.py:25-50` |
| `acceptance_criteria` 最多 20 条，接受 `["文本"]` 或 `[{criterion, milestone_id, step_ids}]` | `schemas/task.py:31` |
| `goal` 是用户原话，**不许改写**（注释明写） | `models/task.py:82-84`、`models/task.py:90-92` |
| `goal_spec` 在落库时归一化 | `api/routes/tasks.py:235-236` |
| `normalize_goal` 只返回固定的 5 个键 | `services/plan.py:169-175` |

**断点 1.1**：`goal_spec` 的形状是**固定的**——`normalize_goal` 手工拼一个 dict 返回，
任何新键都会被静默丢掉（不是报错，是丢掉）。这意味着后面所有"加一个字段"的方案，
第一步都得改这个返回字典，否则字段进了 ORM 也读不出来。

**断点 1.2**：建任务接口没有"包含/不包含"的输入位。`out_of_scope` 只能由调用方塞进
`goal_spec`，而前端有没有这个入口是另一件事——`AGENTS.md:66-68` 已经把"判据要怎么写"
写成了项目约定，但**约定不是门**，模型看不见就不会照做。

### 1.2 意图判定与首轮拆分

| 事实 | 位置 |
|---|---|
| 意图判定只输出 `{intent, note, steps, answer}` 四个字段 | `services/agent/intent.py:163-184` |
| `note` 上限 20 字，定位是"复述用户想要什么" | `intent.py:165` |
| `steps` 要求 2-6 条，**每条不超过 18 字** | `intent.py:175` |
| 规则预判 `guess_intent` 只看寒暄/意见/动作词 | `intent.py:136-160` |
| 模型跳格式直接调工具时，按 act 处理并用 `_parse_plan` 兜步骤 | `graph.py:3485-3493` |
| `_parse_plan` 的收条标准：带项目符号且去掉符号后 >3 字 | `graph.py:3145-3155` |
| `_plan_is_really_answer` 用"条目太长/带 Markdown"判它其实是回答 | `graph.py:3158-3177` |

**断点 1.3**：`note ≤ 20 字` 决定了这一层**只能复述，不能理解**。
20 字装不下"他现在靠脑子记、分不清哪些在做"这种问题陈述。
这不是提示词写得不够好，是字段宽度把这个能力排除了。

**断点 1.4**：`steps ≤ 18 字` 同理。"实现整个功能"是 6 个字，完全合规；
"首屏渲染三列并带至少 3 张种子卡片"是 20 个字，超了。**当前的字符上限在惩罚好的拆分。**

**断点 1.5**：`_parse_plan` 只要一行带 `-` 或编号、超过 3 个字就算一步。
这是一条"能收就收"的宽进策略，它和生产上"一步实现整个功能"的失败是同一个根：
**没有下限，也没有形态要求**。

### 1.3 计划生成

| 事实 | 位置 |
|---|---|
| `create_plan_node` 把 `steps` 归一化后落进状态 | `graph.py:3629` |
| `build_plan` 事后补 `kind/tools/risk/depends_on/success_condition` | `services/agent/planner.py:74-121` |
| `kind` 由**关键词规则**推断（修复/测试/读取/修改四类） | `planner.py:22-62` |
| `depends_on` 只连"上一步"，不表达真实依赖 | `planner.py:104` |
| `success_condition` 是 f-string 模板句 | `planner.py:124-131` |
| `plan_steps` 项只有 5 个键：`id/step/status/milestone_id/evidence` | `services/plan.py:299-307` |
| 计划状态只有 4 态，`cancelled` 是为"不做了"准备的 | `services/plan.py:27-30` |
| 手工改计划的入参也只认 5 个键 | `schemas/task.py:193-200`、`tasks.py:570` |

**断点 1.6（最严重的一条）**：`success_condition` **写完就没人读**。
全仓 grep 只在 `planner.py` 和一个单测里出现；`_finish` 落库的是 `plan_steps / milestones /
goal_spec`（`runtime.py:2137-2146`），**`plan_manifest` 根本不落库**。
所以这个字段既不在库里、也不参与判定——它和 `done_when` 在被编译成 check 之前的状态一模一样
（`docs/external-reference-2026-11.md:185-188` 已经点过一次这个形状）。

**断点 1.7**：`update_plan` 的校验只有两条：至少一个步骤、同一时刻只有一个 `in_progress`
（`services/tools/plan.py:58-69`）。一条"实现整个功能"能过。

**断点 1.8**：手工改计划时 `PlanStepIn` 只有 5 个字段，而且 `status` 的 Literal
**不含 `cancelled`**（`schemas/task.py:198`）——模型能标"不做了"，人不能。
这个不对称迟早会被用户撞到。

### 1.4 验收

| 事实 | 位置 |
|---|---|
| 有验收条件时，把验收判断并进交付那一次模型调用 | `graph.py:7047-7125` |
| 模型按 `{covered, evidence}` 逐条自评 | `graph.py:7086`、`graph.py:7110-7125` |
| 失败兜底：模型没给可解析结论 → 全部 `covered=False` | `graph.py:7142-7145` |
| 证据页对没有结论的判据补一条 `covered=False` | `services/agent/evidence.py:524-539` |
| 未覆盖的判据进 `unverified` 列表 | `evidence.py:780-784` |
| verifier 只读事实，**不设 blocking** | `verifier.py:197-199`、`verifier.py:387-397` |
| `done_when` 的 check 只有三种：`path_exists / file_contains / command` | `services/plan.py:70`、`verifier.py:203-236` |
| 认不出的 check 保留文本、丢掉 check | `services/plan.py:128-130` |

**断点 1.9（空框的直接原因）**：三种 check 覆盖的是"文件在不在/里面有没有字/命令跑没跑"。
看板交付的是一个**能打开但空的页面**——文件在、命令（跑测试）也过了。
**"打开时看见什么"这一维在判据体系里根本不存在**，所以这个失败在结构上不可能被拦住。

**断点 1.10**：模型自评 `covered` 与 verifier 的事实核对是两条独立的线，
且 `verdict` 里 `goal_met` 只看"有没有交付文本"（`verifier.py:380-383`）。
自评说"覆盖了"、事实说"没验过"，两者不冲突，最终状态由 `resolve_status` 决定
（`runtime.py:1832`）——它看不到逐条判据的结果。

### 1.5 落库与追溯

| 事实 | 位置 |
|---|---|
| 收尾写 `plan_steps / milestones / goal_spec / acceptance_results` | `runtime.py:2137-2146` |
| 计划改版计数只在内容真变了时 +1 | `runtime.py:2140-2141` |
| 验收条件与步骤的映射在 `acceptance_criteria[].step_ids` | `services/plan.py:386` |
| **没有 `requirements` 这一列** | `models/task.py:88-106` 全表扫过 |

**断点 1.11**：RTM 的"需求"这一端是空的。我们有"步骤"和"判据"，中间没有"需求"。
于是：
- 漏项无法机器发现（没有"应该有几条"的基准）；
- 超范围无法机器发现（没有"这次说好做哪些"的清单）；
- 用户中途加需求，没有任何记录（`TaskSteerIn` 只进下一轮消息，`schemas/task.py:187-190`）。

### 1.6 周边：已经有的、可以借的

| 能力 | 位置 | 对本设计的作用 |
|---|---|---|
| 事实闸门：写进去的值必须有出处，否则弹卡 | `graph.py:4938-4978` | §3.2「什么必须问」的现成范式：**不是提醒，是不让写** |
| `propose_goal`：把目标写成条件，让用户点头 | `services/tools/goals.py:75-122` | §3.3 复述确认直接复用这张卡，不新开中断类型 |
| 假设台账：用户没答就按默认走，交付时列出"替你定的" | `graph.py:3982-4030` | §3.2「可以假设但要标注」的落地容器 |
| 执行式验收脚本：`open` 段 + 动作段 | `scripts/acceptance.py:4-15`、`acceptance/kanban.json` | §4.4 判据形态的**现成实现**，`page` check 直接编译成它 |
| `browser_check` 工具已支持 `steps` 动作数组与断言 | `services/tools/browser.py:306-340` | `page` check 的执行器，不需要新工具 |
| 进展哨兵：4 步无新证据就提醒 | `services/agent/progress.py:29-36` | §4.2「拆得太碎」的管理税有量化依据 |
| 使用者模型里有 `granularity` 维度 | `services/agent/persona.py:83` | 粒度偏好可以沉淀，但现在是关键词归类，没和计划联动 |
| 经验准入四类 + 剂量 1 | `services/agent/experience.py:162-199`、`265-321` | §7.4 的"拆分模板"要新增一种 kind |
| 进化评价：样本 <20 不下结论 | `services/harness/evolution.py:29` | §8 的对照实验门槛 |

---

## 2. PMBOK 对应关系表

三列：**原文要求**（通行理解，措辞尽量贴近原意）→ **我们的映射**（具体到文件行号）→ **缺口**。

| # | PMBOK 要求 | 我们的映射 | 缺口 |
|---|---|---|---|
| 1 | **收集需求**（5.2）：需求要有来源、要记录谁提的、为什么提 | `tasks.goal`（原话）+ `acceptance_criteria`（判据） | 没有"来源"与"为什么"。`understanding.problem` 缺失 → 用户说方案时无从判断 |
| 2 | **定义范围**（5.3）：范围说明书含 产品范围描述 / 验收标准 / **除外责任（不包含什么）** | `goal_spec.objective / done_when / out_of_scope`（`models/task.py:90-95`） | 三个字段都有，但**没有一个"没填就拦"的门**；`out_of_scope` 只进提示（`graph.py:2810-2813`），从不参与判定，也不参与"超范围"检查 |
| 3 | **创建 WBS**（5.4）：① 100% 规则——子项之和恰好覆盖父项全部，不多不少；② 工作包必须可估算、可分配、可验收 | `plan_steps` + `milestones`（`services/plan.py`） | ① 没有任何校验；② `estimate_min` 字段不存在，`success_condition` 生成了但没人读（§1.3 断点 1.6） |
| 4 | **WBS 词典**：每个工作包要有唯一编号、工作描述、验收标准、责任人、依赖、估算 | `step.id`（内容寻址，`plan.py:47-54`）、`evidence`、`depends_on`（`planner.py:104`） | 编号有、描述有；**验收标准没有**（`success_condition` 是死字段）；**估算没有**；**依赖是假的**（只连上一步）；责任人无意义（都是 agent） |
| 5 | **确认范围**（5.5）：与客户一起正式验收可交付成果，重点是"这是不是你要的" | **没有** | 这是最大的概念缺口。`verifier` 是**控制质量**（8.3）：回答"做得对不对"；确认范围回答"做的是不是这件事"。空框案例里，页面确实"没有控制台错误"——质量控制通过，确认范围失败 |
| 6 | **控制范围**（5.6）：监控范围状态，管理范围基准的变更 | `plan_revision`（`models/task.py:104`）、`plan.updated` 事件（`tasks.py:594`）、`update_plan` 整份覆盖 | 没有**基准**（baseline）这个对象。没有基准，"改了多少"就无法量化——`plan_revision` 只数了"计划改过几次"，不区分"补一步"和"换了个目标" |
| 7 | **需求跟踪矩阵（RTM）**：需求 ↔ 交付物 ↔ 验收 ↔ 证据，双向可追溯 | `acceptance_criteria[].step_ids`（`plan.py:386`）是 RTM 的一列 | 需求实体不存在（§1.5 断点 1.11）；反向"每个工作包服务于哪条需求"不存在 |
| 8 | **质量成本（COQ）**：一致性成本（预防 + 评估）vs 非一致性成本（内部失败 = 返工，外部失败 = 客户发现） | 只有"外部失败"——用户打开看到三个空框 | 没有预防成本的口子（澄清），没有内部失败的口子（交付前自检）。见 §2.1 的账 |
| 9 | **实施整体变更控制 / CCB**：变更要评估影响、要批准、要记录 | 用户插话 → 下一轮 `update_plan` 追加（`tools/plan.py:236`） | 无记录、无影响分析、无批准。范围蔓延在结构上是无摩擦的 |
| 10 | **SMART**（**非 PMBOK**，Doran 1981，通行借用）：具体、可测、可达成、相关、有时限 | `AGENTS.md:64-83` 已经写成了项目约定 | 约定没有代码门——模型看不见 `AGENTS.md` 的某一段就绕过去了 |
| 11 | **Definition of Done**（**非 PMBOK**，Scrum/敏捷）：一组预先约定、可核对的完成条件 | `done_when` + `verifier` 逐条核对 | 判据形态不受约束（`AGENTS.md:66-68` 就是因为这个才写的） |

### 2.1 用空框案例算一笔质量成本

已知基线（`docs/dubhe-alignment.md:82`，98 条已结束任务）：**每任务 69k token、2.38 步**。

| 成本项 | 发生了什么 | 量级 |
|---|---|---|
| 预防（一致性） | 建任务后弹一张 3 问的澄清卡（问题聚类 + 用户答 20 秒） | 1 次模型调用 ≈ 3-6k token + 用户 30 秒 |
| 评估（一致性） | 交付前跑一遍 `acceptance.py --journey` | 一次 `browser_check`，约 15 秒，**0 token**（纯本地探针） |
| 内部失败（非一致性） | 用户说"就做了个这个" → 同一 thread 重做一轮 | ≥ 1 个完整任务 ≈ 69k token（基线均值；返工轮通常更长，因为要重读上下文） |
| 外部失败（非一致性） | 用户对"交付"这件事的信任损失 | 不可计量，且**不可逆**——这次他没再让它做第二件 |

结论：**预防 + 评估的成本比返工低 1~2 个数量级**（3-6k vs 69k），而评估成本几乎为零。
这条账也是 §7.4 把 `page` check 排在第一阶段的理由：它是最便宜的评估手段。

> 标注：69k 是 98 条任务的**均值**，不是返工轮实测；"1~2 个数量级"是推算，不是测量。
> 见 §9。

---

## 3. 需求理解：分层模型与抽取动作

### 3.1 五层结构

"理解"不是复述。复述是 `note ≤ 20 字` 能做的事（`intent.py:165`），理解要产出五层东西。

| 层 | 要回答的问题 | 存在哪 | 谁填 | 看板案例 |
|---|---|---|---|---|
| **L1 诉求**（verbatim） | 用户原话是什么 | `tasks.goal`（`models/task.py:82-84`，**不许改写**） | 系统 | "给我做个任务看板" |
| **L2 意图**（problem） | 他要解决什么麻烦 | `goal_spec.understanding.problem` | 模型，可被用户改写 | "现在一堆事靠脑子记，分不清哪些在做、哪些还没动" |
| **L3 约束** | 不能碰的边界（技术栈、不许做的事、时间） | `goal_spec.constraints` | 用户 / 模型（推断的要标） | "单文件、不引第三方库" |
| **L4 成功的样子** | **打开看到什么 / 做完一遍变成什么 / 隔天还在不在**——必须肯定式、可观察 | `goal_spec.understanding.success_looks_like` + `done_when` | 模型起草，用户确认 | "打开就有 ≥3 张卡分布两列；点添加多一张；拖一下换列；刷新还在" |
| **L5 不做的事** | 这次明确不做什么 | `goal_spec.out_of_scope` | 用户 / 模型 | "多人协作、登录、后端" |
| +A 谁来看结果 | 决定判据的严格度与形态 | `understanding.audience` | 模型推断 | "用户本人，桌面每天开着" |
| +B 我替你定的 | 自己拍的假设，交付时要显式列出 | `understanding.assumptions` | 模型 | "用 localStorage，不做后端（任务没提后端，工作区是空目录）" |

**每一层缺了会怎样**（这是这套结构的全部理由）：

| 缺哪层 | 具体后果 | 已有案例 |
|---|---|---|
| 缺 L2 | 方案照抄。objective 直接等于用户说的那个交付物名词，之后所有判据都在为"有这个交付物"服务 | "给我做个看板" → objective="做一个任务看板" |
| 缺 L4 | 判据只能写否定式（因为没有"应该看到什么"可写），空壳天然通过 | 三列空框 |
| 缺 L5 | 范围蔓延。每多做一点都算"更完整"，没有"这不在范围内"的判据 | — |
| 缺 +B | 用户不知道哪些是你替他定的，出问题时无法定位是"他没说"还是"你猜错" | 已有机制：`_with_delivery_notes`（`graph.py:4013-4030`） |

**为什么 L4 要和 `success_looks_like` 分开存**：`done_when` 是**判据**（带 check，进 verifier），
`success_looks_like` 是**描述**（不带 check，给人看、用来生成判据）。
两者合一会重演 `goal` 与 `goal_spec` 分家时的那个决定——**同一列放不下两种用途**
（`models/task.py:90-92` 的注释）。

### 3.2 什么必须问、什么可以假设（判定规则）

原则一句话：**问的代价是一次中断，猜错的代价是一轮返工**。只有当"猜错的概率 × 返工代价"
明显大于"问的代价"时才问。落成四条可执行规则：

#### 必须问（blocking，四类）

| 编号 | 条件 | 判据（怎么机器判） | 为什么不能猜 |
|---|---|---|---|
| **I1** | 判据不可观测 | `success_looks_like` 为空，且用户原话里没有任何"看到/能/会/至少"这类可观察词 | 没有 L4 就没有肯定式判据，交付物形态由模型自由决定 → 空框 |
| **I2** | 两种解释的后果**不对称且不可逆** | 涉及删除/覆盖/发布/发消息/改线上数据，且用户没说清 | `permissions.py` 的审批门管的是"要不要执行"，管不了"执行的是不是这个意思" |
| **I3** | 涉及外部事实值 | 电话/邮箱/地址/密钥/账号 | **已有闸门**：`_plan_fact_gate`（`graph.py:4938-4978`）。这条不重复建设，直接引用 |
| **I4** | 边界决定交付物形态 | 交付物是"一个页面"还是"一套系统"、"本地文件"还是"要部署" | 这类歧义会让整个 WBS 作废，不是补一步能修的 |

#### 可以假设但必须标注（三类）

| 编号 | 条件 | 标注要求 |
|---|---|---|
| **A1** | 可从工作区事实推出 | 写 `why` 指向事实（"工作区有 package.json，用现成栈"） |
| **A2** | 有通行默认且错了代价小 | 配色、端口、文件名、目录结构 → `risk: low` |
| **A3** | 用户明显不在意且属实现细节 | 循环写法、变量命名 → **不必标**（标了是噪音） |

#### 不问也不标

纯实现细节。判断标准：**用户看到交付物时不会产生"这不是我要的"**。

#### 防骚扰的量化门槛

| 门槛 | 值 | 依据 |
|---|---|---|
| 一次最多问几个 | **3** | `tools/ask.py:23` 的 `MAX_QUESTIONS = 4`，留一格给事实闸门合成的问题 |
| 什么时候才弹 | `intent == act` **且** 预计工作包 > 1 **且** 存在 I1/I4 | 一句话任务弹卡 = 骚扰。用户说"帮我改个错别字"不该被问三个问题 |
| 同一问题问几次 | **一次**（同一 thread 内） | `session.asked_facts`（`graph.py:4973-4975`）已有这个语义，复用 |
| 弹卡总字数 | **≤ 6 行 / 300 字** | 超过用户不读。`propose_goal` 的 `CONDITION_CHARS = 500` 是单条上限，不是整卡上限 |

**这条规则和 `AGENTS.md:43-56` 的"先派代理出去学"是同一个形状**：调研成本低、返工成本高，
所以前置。区别在对象——调研补的是"外部事实"，澄清补的是"用户意图"。

### 3.3 复述确认的格式

**载体**：复用 `propose_goal` 的问题卡（`tools/goals.py:75-103`），**不新开一种中断类型**。
理由 `graph.py:4775-4780` 已经写过：`_finalize` 只认 `kind == "question"`，
多造一种会让恢复值在两条门之间错位。

**内容**：固定四行（多一行用户就不读）：

```
要做成：<objective，一句话，不含交付物名词>
算做完：<2-4 条，肯定式、可观察>
不做的：<1-3 条>
我替你定：<assumptions，最多 3 条，每条一句话>
```

硬约束：

1. **objective 里不许出现交付物名词**（看板/仪表盘/系统/页面/后台/脚本/工具）。
   这条由 `objective_problems()` 判（§3.4），不是靠提示词。
2. `算做完` 每一条都要能回答"在哪看到"。答不出来就不许进这一行。
3. `不做的` 为空时**整行不写**，不要写"无"。
4. 用户改写后的自由文本直接当作修订后的理解——`goal_decision` 已有这个语义
   （`tools/goals.py:121-122`：非否定、非空 → 当作修订后接受）。

**什么时候弹 / 不弹**：

| 情形 | 弹? | 理由 |
|---|---|---|
| act + 预计 >1 个工作包 + 缺 L4 | **弹** | 这是本设计的主要场景 |
| act + 单步任务（"把端口改 8001"） | 不弹 | 弹卡的代价大于猜错 |
| answer / advise | 不弹 | 不动工作区，没有交付物形态可确认 |
| `out_of_scope` 非空但 objective 清楚 | 不弹 | 用户已经给了边界 |
| 用户本轮说过"不用问 / 直接做" | 不弹，但 **assumptions 必须进交付** | `wants_confirmation` 的现有语义（`goals.py:55-58`） |

### 3.4 方案 vs 问题的识别

**这是"给我做个看板"这一类需求的关键。** 用户说的是方案（手段），要的是问题被解决。

#### 可执行的信号

| 信号 | 例子 | 权重 |
|---|---|---|
| 交付物名词 + "做个/加个/搭个/写个/整一个" | "做个任务看板" | 强 |
| 完全没有"现在/每次/老是/因为/否则" | "给我做个看板" | 中 |
| 有"能拖/能筛/要好看/支持导出"这类**属性描述** | "能拖拽的看板" | 中（说明他想清楚了形态，但没说问题） |
| 出现"我要管住/我看不清/老是漏/记不住" | "我要管住手头这堆事" | **反向信号——他已经说了问题，不用再问** |

#### 动作

1. 命中强信号且**无**反向信号 → 允许问**一个问题**：
   「你现在是怎么处理这堆事的？」（不是"你的需求是什么"——那种问法用户答不出东西）
2. 把答案写进 `understanding.problem`。
3. `objective` 必须由 problem 生成，**不许**从交付物名词生成。
4. 用户答不上来（"就做个看板嘛"）→ 按 A2 假设，但 `problem` 写"用户未说明，按交付物形态推断"，
   并进 assumptions（交付时显式列出）。

#### 校验器：`objective_problems(goal_spec) -> list[str]`

```
规则 1：objective 命中 _SOLUTION_NOUNS（看板|仪表盘|面板|系统|后台|页面|脚本|工具|平台|app）
        且 understanding.problem 为空  →  "GOAL_IS_SOLUTION"
规则 2：objective 与 tasks.goal 逐字相同（说明只是复述，没有概括）
        →  "GOAL_IS_VERBATIM"
规则 3：objective 长度 > 200 → "GOAL_TOO_LONG"（说明在写方案细节）
```

规则 1 会误判吗？会——用户说"做一个仪表盘给我看服务器状态"是**真的**要一个仪表盘
（problem 已经在句子里）。所以规则 1 只在 `problem` 为空时触发；而"服务器状态看不清"
这类词可以从原话里抽出来填 `problem`，抽不出来才拦。误报率要在 T2 阶段用历史数据量（§7.4）。

---

## 4. 拆分：粒度判据

用户原话是"是否拆到最细"。答案是：**不是最细，是最小可验证**。
"最细"没有上界，会一路拆到"写一个 `<div>`"；"最小可验证"有明确的停止条件。

### 4.1 工作包七条判据

一个工作包必须**同时**满足七条。每条给"怎么判"和"当前落点"。

| # | 判据 | 含义 | 怎么判（确定性 / 半自动） | 违反时的错误码 |
|---|---|---|---|---|
| 1 | **单一产出物** | 一个包只产出一个可指认的东西（一个文件 / 一条命令的结果 / 一段可读结论） | 半自动：`output` 非空；且 `output` 里不出现"和/以及/、"连接的两个动作名词 | `WP_MULTI_OUTPUT` |
| 2 | **可独立验证** | 自带 ≥1 条机器可执行的 `check` | **确定性**：`_clean_check()` 返回非空（`plan.py:73-92`） | `WP_NO_CHECK` |
| 3 | **可估算** | `estimate_min` 在 5–120 之间 | **确定性**：数值区间 | `WP_TOO_SMALL` / `WP_TOO_BIG` |
| 4 | **失败可定位** | 失败时能指出"是这个包的哪条判据没过" | 半自动：`check` 指向的对象必须与 `output` 指同一个（路径前缀 / 同一条命令 / 同一个 url） | `WP_CHECK_MISMATCH` |
| 5 | **不需要再问需求** | 执行这个包时不该弹新的澄清卡 | 半自动：`open_questions` 里没有 `blocking: true` | `WP_BLOCKED_BY_QUESTION` |
| 6 | **大小在带内** | 预计 1–4 次工具调用 | **确定性**：`est_calls ∈ [1,4]` | `WP_TOO_BIG` |
| 7 | **归属清晰** | 至少指向一条需求 | **确定性**：`req_ids` 非空且都存在于 `requirements` | `WP_NO_REQ` |

#### 为什么第 6 条用"工具调用次数"而不是 8/80 的工时

8/80 规则（工作包不小于 8 小时、不大于 80 小时）是通行的 WBS 启发式，
**PMBOK 把它作为启发式提及、而非硬性数值**（按通行理解；各版本表述不同）。

我们没有工时数据：账本里只有 `steps_used`、`tool_calls`、`usage`，没有"这一步花了多少人时"。
硬套 8/80 就是编数字。但"1–4 次工具调用"是我们**真的能数**的：

- **下限 1**：`est_calls < 1` 说明这个包其实就是"一个动作"（读一个文件/跑一条命令），
  它和相邻的包共享产出物，该合并。
- **上限 4**：`IDLE_LIMIT = 4`（`progress.py:34`）的实测含义是"4 步没有新证据就是空转"。
  一个包如果需要 >4 次调用才产出东西，说明它没有定义清楚"产出什么"，
  而不是"它很大"。

换算关系（粗略，用于给人看）：`estimate_min ≈ est_calls × 6`（一次"读→想→写"约 6 分钟）。
这个系数是估计值，标在 §9。

### 4.2 何时停止拆分（量化门槛）

四个停止条件，**任一成立即停**：

| 编号 | 条件 | 为什么停 |
|---|---|---|
| **S1** | 这个包已经有 1 条 `check`，且这条 check 只需"看一个文件 / 跑一条命令 / 走一遍页面"就能判 | 再拆只会拆出"同一个 check 的两半"——它们无法独立验证 |
| **S2** | 继续拆，子项的 `output` 与父项相同（或互为子集） | 产出物重合 = 没有独立价值，只是把一句话写成三行 |
| **S3** | 包数达到 `MAX_STEPS = 12`（`services/plan.py:32`） | 硬顶。超过就渲染不下、上下文成本陡增 |
| **S4** | 管理税 > 收益：`estimate_min < 5` 或 `est_calls < 1` | 见下面的账 |

#### 管理税的量化

每多一个工作包，要付：

| 成本 | 量 | 来源 |
|---|---|---|
| 计划渲染进上下文（每轮一次） | ≈ 30–60 token / 包 / 轮 | `render_plan`（`plan.py:461-490`）一行一步；一个 8 步计划约 300 token，20 轮重述 = 6k token |
| `update_plan` 往返 | ≈ 500 token / 次（模型要重发完整步骤列表） | `tools/plan.py:239`「每次提交**完整步骤列表**」 |
| 状态机维护（勾选、in_progress 唯一性） | 每包每完成一次 = 1 次工具调用的一部分 | `tools/plan.py:63-69` |

**换算**：从 5 个包拆到 14 个包，多付 ≈ 9 × (60 × 20 轮 + 500) ≈ 15k token，
换来的"更细"如果不能带来**独立可验证性**（S1/S2 已排除），这 15k 是纯亏损。
参照基线每任务 69k（`dubhe-alignment.md:82`），这是 **20% 的成本**。

#### 过碎的真实形态

不是"包很多"，是"**包很多但 check 重复**"。判据：任意两个包的 `check` 指向同一对象
（同一 path / 同一命令 / 同一 url+断言）→ 合并。这条做进 `work_package_problems()`。

### 4.3 三档样例（用任务看板）

#### 档 A：太粗（1 个包，当前系统会接受）

```json
[
  {"step": "实现任务看板", "status": "pending"}
]
```

- 包数 1，`check` 0 条。
- `build_plan` 会给它 `kind="edit"`、`success_condition="「实现任务看板」已落盘且可在 diff 中确认"`
  （`planner.py:131`）——**这句话没有任何地方核对**。
- 空框案例就是这一档：文件落盘了，diff 有了，判据（否定式）全过。

#### 档 B：刚好（5 个包 / 2 个里程碑）

```json
{
  "milestones": [
    {"id": "m1", "title": "打开就有东西"},
    {"id": "m2", "title": "用起来是对的"}
  ],
  "steps": [
    {
      "step": "三列骨架 + 首屏种子数据",
      "milestone_id": "m1",
      "output": "index.html 打开即见的三列，且至少 3 张卡片分布 ≥2 列",
      "output_paths": ["index.html"],
      "check": {"type": "page", "url": "index.html", "phase": "open",
                "expr": "document.querySelectorAll('.task-item').length", "min": 3},
      "req_ids": ["R2", "R3"],
      "estimate_min": 30, "est_calls": 2, "depends_on": []
    },
    {
      "step": "新增任务：填标题提交后多一张卡并落在待办列",
      "milestone_id": "m2",
      "output": "点提交后 .task-item 数量 +1，且新卡片文案正确",
      "output_paths": ["index.html"],
      "check": {"type": "page", "url": "index.html", "phase": "journey",
                "assert_text": "点添加后卡片数比刚才多 1 张"},
      "req_ids": ["R4"],
      "estimate_min": 25, "est_calls": 3, "depends_on": ["<上一步 id>"]
    },
    {
      "step": "拖拽改状态：拖到进行中后那一列真的有卡",
      "milestone_id": "m2",
      "output": "拖拽后目标列卡片数 ≥1",
      "output_paths": ["index.html"],
      "check": {"type": "page", "url": "index.html", "phase": "journey",
                "assert_text": "拖到进行中之后，那一列真的有卡片"},
      "req_ids": ["R5"],
      "estimate_min": 35, "est_calls": 4, "depends_on": ["<上一步 id>"]
    },
    {
      "step": "刷新后状态不丢",
      "milestone_id": "m2",
      "output": "reload 后卡片数与 reload 前一致",
      "output_paths": ["index.html"],
      "check": {"type": "page", "url": "index.html", "phase": "journey",
                "assert_text": "刷新之后卡片一张不少"},
      "req_ids": ["R6"],
      "estimate_min": 20, "est_calls": 2, "depends_on": ["<拖拽那一步 id>"]
    },
    {
      "step": "关键字过滤收窄到 1 张且不是 0 张",
      "milestone_id": "m2",
      "output": "输入「早报」后可见卡片恰好 1 张",
      "output_paths": ["index.html"],
      "check": {"type": "page", "url": "index.html", "phase": "journey",
                "assert_text": "按关键字过滤真的收窄到 1 张而且不是 0 张"},
      "req_ids": ["R7"],
      "estimate_min": 15, "est_calls": 2, "depends_on": ["<新增那一步 id>"]
    }
  ]
}
```

- 包数 5，check 5 条，全部**肯定式**且**机器可执行**（断言文本与 `acceptance/kanban.json` 对齐，
  说明这些判据不是设计出来的，是从那次失败里长出来的）。
- 空框交付在这里死在第 1 个包：`.task-item` 为 0 < 3。
- 每个包的 `check` 只需"走一遍页面"，符合 S1 → 停。

#### 档 C：太碎（14 个包，反例）

```json
[
  {"step": "写三列的 HTML 结构", "check": {"type": "file_contains", "path": "index.html", "text": "column"}},
  {"step": "给列加 data-status 属性", "check": {"type": "file_contains", "path": "index.html", "text": "data-status"}},
  {"step": "写 .task-item 的 CSS", "check": {"type": "file_contains", "path": "index.html", "text": ".task-item"}},
  {"step": "写空态提示节点", "check": {"type": "file_contains", "path": "index.html", "text": "empty-hint"}},
  {"step": "写种子数据数组", "check": {"type": "file_contains", "path": "index.html", "text": "seed"}}
  // …还有 9 个
]
```

错在哪（逐条对应判据）：

| 症状 | 违反 | 具体 |
|---|---|---|
| 5 个包的 `output` 都是 `index.html` 这个文件 | 判据 1（单一产出物）→ 其实是**共享产出物**，不是各自一个 | `output_paths` 全是同一路径，且没有更细的对象可指 |
| `file_contains "column"` 能证明"三列骨架可用"吗？不能——写了字符串但没渲染，一样通过 | 判据 4（失败可定位） | check 与 output 不指同一个可观察对象 |
| 任何两个包的 check 指向同一文件同一形态 | S2 + 判据 2 | `work_package_problems()` 会判重复 |
| 14 个包 × 每轮重述 ≈ 25k token，比 5 个包多 20k | S4 | 拆到第 6 个之后边际收益为负 |

**这一档最危险的地方**：它看起来"拆得很细"，而且每条都有 check，
但 check 全是 `file_contains`——**它把"拆得细"退化成了"把同一个文件检查五遍"**。
空框能过这五条里的每一条。

### 4.4 粒度与验收形态的绑定

这是本设计的核心机制，也是空框案例的唯一解。

#### 铁律

> **一个工作包必须自带至少一条"肯定式 + 机器可执行"的验收判据；否则不许进计划。**

"肯定式"= 说"应该看到什么"，不说"不应该有什么"。
"机器可执行"= 有一个确定性执行器能给出 passed/failed，不是模型读一遍觉得对。

#### 为什么否定式判据会反向塑造交付物

空框案例的四条判据（`scripts/acceptance.py:6-8` 原文列举）：
可拖拽 / localStorage 能恢复 / 不引第三方库 / 无控制台报错。

把交付物当成一个优化问题：**在满足约束的前提下最小化工作量**。

| 判据 | 空页面的满足情况 |
|---|---|
| 可拖拽 | 空页面上"拖拽"这个动作无从发生，模型自己造了三条数据验过（`acceptance.py:10-12`） |
| localStorage 能恢复 | 塞三条 → 刷新 → 三条还在，验过了 |
| 不引第三方库 | 真满足（一行都没写） |
| 无控制台报错 | **天然满足**——空页面永远不会有控制台错误 |

四条全过，工作量趋近于零。**这不是模型在偷懒，这是判据设计出来的最优解。**

#### 判据形态检查器：`check_shape_problems()`

输入：`goal_spec.done_when` + `plan_steps[*].check`。输出：问题列表。

| 规则 | 判什么 | 错误码 |
|---|---|---|
| 否定式词表命中且**是唯一一条**判据 | `不报错|无报错|没有错误|不引|不用|不能崩|不卡|不闪|无警告|doesn't|no error` | `CHECK_NEGATIVE_ONLY` |
| 没有任何一条"存在/等于/至少"类断言 | 需要至少一条 `path_exists` / `file_contains` / `command` / `page(min|===)` | `CHECK_NO_POSITIVE` |
| 前端交付物（`output_paths` 含 `.html/.tsx/.vue`）没有 `page` 类型判据 | 首屏必须有断言（`AGENTS.md:70-75` 的四层里第 1 层） | `CHECK_NO_FIRST_SCREEN` |
| 判据文本里出现"应该能/大概/看起来" | 不可测的模糊表述 | `CHECK_VAGUE` |

#### 否定式 → 肯定式的改写对照（空框案例）

| 原判据（否定式，空页面天然通过） | 改写（肯定式，机器可执行） | 执行器 |
|---|---|---|
| 不引第三方库 | 页面里 `.task-item` 至少有 3 个 | `page` |
| 无控制台报错 | 卡片分布在 ≥2 个列里（`new Set(...data-status).size >= 2`） | `page` |
| 可拖拽 | 拖到 `doing` 列后，该列 `.task-item` 数 ≥1 | `page` |
| localStorage 能恢复 | reload 后 `.task-item` 数与 reload 前**相等** | `page` |

注意改写后的每一条都**不能靠"什么都不做"满足**。这就是判据形态与交付物形态的绑定。

#### 和 verifier 的分工

- 判据只写**要看到什么**（`expr` + `min` / 断言文本），不写**怎么看**（用什么选择器、等多久）。
  怎么看由 `browser_check` 决定（`browser.py:243-248`）。
- `page` check **不新增工具**：它读的是 `browser_check` 已经在 `tool_results` 里留下的回执
  （`browser.py:283-285` 的 `steps` 数组，每项带 `ok`）。verifier 是纯函数，
  只读回执、不发请求——这条纪律不破（`plan.py:69` 的注释：刻意不收 http/db）。

---

## 5. 跟踪：需求矩阵与范围确认

### 5.1 数据结构：`requirements`

落在 `tasks.requirements`（新列，JSON，默认 `'[]'`）。

**为什么单独一列，不塞 `goal_spec`**：需求有**独立生命周期**（每条有自己的 status），
RTM 校验器要当列表遍历；塞进 `goal_spec` 会被 `normalize_goal` 的固定形状挤掉（`plan.py:169-175`）。

```json
[
  {
    "id": "R1",
    "text": "打开页面就能看出这是一块板，不是一列清单",
    "kind": "success",              // problem | constraint | success | out
    "source": "user",               // user | inferred | template
    "priority": "must",             // must | should | could
    "wp_ids": ["s3f9a1c2d0"],
    "criterion_ids": ["c1"],
    "status": "planned",            // planned | done | blocked | dropped
    "note": ""
  }
]
```

字段规则：

| 字段 | 类型 | 约束 | 谁写 |
|---|---|---|---|
| `id` | str | `R` + 序号，任务内唯一 | `sync_requirements` |
| `text` | str | ≤200 字 | 模型 / 用户 |
| `kind` | enum | 四类，对应五层结构（`problem` 只有一条） | `sync_requirements` 从 `goal_spec` 推 |
| `source` | enum | `user` 用户说的 / `inferred` 模型推的 / `template` 模板带的 | 生成时标 |
| `priority` | enum | `must` 默认；用户说"最好有"→`should`，"以后再说"→`could` | 模型，用户可改 |
| `wp_ids` | list[str] | 反查得到，不超过 5 | `sync_requirements` |
| `criterion_ids` | list[str] | 至少 1（`must` 必须有） | `sync_requirements` |
| `status` | enum | 由 wp 状态推出，不采信模型自报（同 `resolve_milestone_status` 的口径，`plan.py:344-350`） | `resolve_requirement_status` |

### 5.2 自动生成：`sync_requirements(goal_spec, plan_steps, acceptance_criteria)`

纯函数，无副作用，可单测。规则：

```
objective                    → R0 (kind=problem, priority=must)      # 唯一的"这条任务本身"
done_when[i]                 → kind=success  # 顺序与 done_when 一致，id 用 c{i} 对齐
constraints[i]               → kind=constraint
out_of_scope[i]              → kind=out, status=dropped（不是漏项，是明确不做）
plan_steps[*].req_ids        → 反向填 wp_ids
acceptance_criteria[*].step_ids → 反向填 criterion_ids
```

**关键约定**：`out_of_scope` 生成的条目 `status` 恒为 `dropped`。
这样"超范围"检查只需要问一句"有没有 wp 指向 dropped 的需求"——不需要另存一份排除清单。

### 5.3 校验：`requirement_problems(requirements, plan_steps, criteria)`

每次 `plan.updated` 后、收尾前各跑一次。

| 编号 | 规则 | 错误码 | 严重度 |
|---|---|---|---|
| V1 | 每条 `must` 需求至少被一个工作包覆盖（`wp_ids` 非空） | `REQ_UNCOVERED` | **拦**（计划阶段） |
| V2 | 每个工作包至少指向一条需求（反向，判据 7） | `WP_NO_REQ` | **拦** |
| V3 | 每条 `must` 需求至少有一条验收判据 | `REQ_NO_CRITERION` | **拦**（收尾前） |
| V4 | 没有工作包指向 `status=dropped` 的需求 | `SCOPE_EXTRA_PLANNED` | **拦**（这是"说好不做却排了活"） |
| V5 | 所有 wp 的 `output` 并集能回答 objective（100% 规则） | — | **不拦，也不自动判** |

**V5 必须老实说不做**：100% 规则在 PMBOK 里的判法是"子项之和覆盖父项全部，不多不少"，
这需要判断"覆盖"的语义。机器判不了，模型判不可靠。
我们的替代是**事后指标**：`requirement_coverage`（must 需求被 done 的比例），
跑够样本才看（§8）。不要在计划阶段假装能验。

### 5.4 执行中失败/被拒怎么标

现在的状态机不够用，这是必须承认的：

| 事实 | 位置 |
|---|---|
| 计划步骤只有 4 态：`pending / in_progress / completed / cancelled` | `services/plan.py:30` |
| **没有 `failed` 或 `blocked`** | 同上 |
| 工具调用有 `denied` 状态 | `models/task.py:30` |

没有 `blocked` 态的后果：模型碰到"这一步失败了"只有两个烂选择——留 `pending`（进度条永远差一截）
或标 `completed`（把没做的写成做完了）。`cancelled` 的注释（`plan.py:27-29`）说的正是这个困境，
但它只能表达"不做了"，表达不了"做不了"。

**处理方案（分两步，第一步不改状态机）**：

1. **不新增状态**（避免前端与存量数据分叉）：
   - 步骤失败 → 保持 `in_progress`，`evidence` 写失败事实（命令 + 退出码），
     同时由 `resolve_requirement_status` 把对应需求标 `blocked`。
   - 审批被拒（`ToolCall.status == "denied"`）→ 同上，`evidence` 写"被拒：<用户理由>"
     （理由已经能从 `TaskApprovalIn.feedback` 拿到，`schemas/task.py:151`）。
2. **第二阶段（需拍板）再加 `blocked` 态**：改 `PLAN_STATUSES`（`plan.py:30`）、
   `PlanStepIn` 的 Literal（`schemas/task.py:198`，现在连 `cancelled` 都不让手工设）、
   `_MARKS`（`plan.py:38`）、`plan_progress`（`plan.py:421-455`）、前端勾选框。
   影响面明确，但不是这一轮该做的事。

`resolve_requirement_status(requirements, steps)` 的规则，与 `resolve_milestone_status`
（`plan.py:344-362`）同构：

```
该需求的所有 wp 都 completed            → done
有任一 wp 的 evidence 以「失败/被拒/未通过」开头  → blocked
有任一 wp 是 cancelled                  → dropped（且 note 写原因）
其余                                    → planned
```

### 5.5 收尾前的范围确认（Validate Scope）

`scope_review(requirements, steps, acceptance_results, verdict, changed_paths, criteria)
-> {missing, extra, undelivered, cancelled}`

纯函数，落成事件 `scope.reviewed`，并在证据包里多一段 `scope`。

| 输出 | 判据 | 含义 |
|---|---|---|
| `missing` | `priority=must` 且 `status != done` | 漏项（说好要做没做） |
| `extra` | `changed_paths` 里有文件不在任何 wp 的 `output_paths` 里，且不在 `.blank/scratchpad/` | 超范围（做了说好不做的） |
| `undelivered` | wp `completed` 但它的 `check` 在 `evidence.done_when` 里是 `failed` 或 `unknown` | 假完成（勾了但没验） |
| `cancelled` | `status=dropped` 的需求 | 明确不做，**不是**问题（只列出来给人看） |

**与 verifier 的分工**（这张表是概念上最重要的一页）：

| | verifier（控制质量） | scope_review（确认范围） |
|---|---|---|
| 回答 | 做得对不对 | 做的是不是这件事 |
| 依据 | 磁盘事实 / 命令退出码 / 页面断言 | 需求清单 / 计划 / 判据覆盖 |
| 失败含义 | 有 bug | 需求没满足，或做了多余的 |
| 现在有吗 | 有（`verifier.py`） | **没有** |

空框案例里两者都失败，但**只有前者有实现**。这就是为什么"四条判据全过"能发生：
我们只问了"对不对"，没问"是不是这个"。

---

## 6. 变更控制：中途改需求怎么走

### 6.1 现在是什么样

用户插话 → `TaskSteerIn`（`schemas/task.py:187-190`）→ 下一轮消息 →
模型调 `update_plan` 追加步骤（`tools/plan.py:236` 的描述）。

**没有记录、没有影响分析、没有批准。** 范围蔓延在结构上无摩擦：
用户随口一句"再加个导出吧"，模型加一步，`plan_revision +1`，没人知道原来有几条需求、
现在有几条、哪些已经做完的受影响。

### 6.2 三类变更与处理

| 变更 | 判据（怎么识别） | 处理 |
|---|---|---|
| **加需求** | 用户新诉求不否定旧 objective | `requirements` 追加一条（`source=user`）；`plan_steps` 追加 wp；`plan_revision + 1`；**旧的一律不动** |
| **改需求** | 用户否定旧判据（"不要拖拽了，改成点按钮换列"） | 旧需求 `status=dropped` + `note` 写原因；新增替代需求；对应 wp `status=cancelled` + `evidence` 写"需求变更" |
| **减范围** | 用户说"先这样 / 剩下的以后再说" | `out_of_scope` 追加；wp `cancelled`；需求 `dropped` |

**关键**：三类变更都**不删数据**。"原来要做后来不做"和"从来没提过"是两件事——
前者是范围变更记录（Control Scope 的产物），后者是漏项。混在一起就没法追责。

### 6.3 简易 CCB：一次模型调用回答三个问题

变更必须在一轮里回答：

1. **影响哪些已完成的工作包？**（按 `req_ids` 反查 `wp_ids`，再看这些 wp 的状态）
2. **哪些验收判据要改？**（`criterion_ids` 里哪些指向被删/被改的 wp）
3. **还要多少步？**（新 wp 的 `est_calls` 之和 vs 剩余预算）

答不出 → **拒绝这次 `update_plan`**，让它先查。

落点：`_update_plan` 里比较新旧 `requirements` 的 id 差集。有差集时：

```
if requirements_changed and not explanation 包含受影响的 wp id:
    return fail("error", "CHANGE_NO_IMPACT: 需求变了，但说明里没有写影响面…")
```

`explanation` 字段已经存在（`tools/plan.py:290`），不需要新字段。

### 6.4 防蔓延：objective 变更要用户点头，计划变更不用

| 变更对象 | 要不要用户确认 | 为什么 |
|---|---|---|
| `objective` / `done_when` / `out_of_scope`（范围基准） | **要**（复用 `propose_goal` 卡） | 这是"说好做什么"，改了就是改合同 |
| `plan_steps` / `milestones`（路径） | 不要 | 计划本来就该在执行中调（`tools/plan.py:7` 的设计前提） |

这个分工在 `tools/plan.py:86-90` 已经写成话术（"目标是判据，计划是路径……混在一起时，
模型改计划顺手就把验收标准也改了，而用户从头到尾没看见"）。**现在把这句话变成门。**

### 6.5 基准（baseline）：Control Scope 缺的那一环

`goal_spec.baseline`，第一次用户确认时冻结，之后**只读**：

```json
{
  "objective": "...",
  "done_when": ["..."],
  "out_of_scope": ["..."],
  "requirement_ids": ["R0", "R1", "R2"],
  "at": "2026-11-20T10:00:00Z",
  "revision": 1
}
```

用途：`plan_revision` 只数"计划改过几次"，分不清"补了一步"和"换了个目标"。
有 baseline 之后可以算：

- `scope_delta = len(requirements) - len(baseline.requirement_ids)`（净增需求数）
- `objective_changed = baseline.objective != goal_spec.objective`（是否改了合同）

这两个数进 `scope.reviewed` 事件，也就是 §8 的 `rework_rate` 指标的输入。

---

## 7. 落地设计

### 7.1 数据结构（字段级）

#### 7.1.1 `goal_spec` 扩展

```json
{
  "objective": "让用户一眼看出每件事卡在哪一列，并能拖动改状态",
  "done_when": [
    {"text": "打开页面就能看到至少 3 张卡片分布在两列以上",
     "check": {"type": "page", "url": "index.html", "phase": "open",
               "expr": "document.querySelectorAll('.task-item').length", "min": 3}}
  ],
  "done_checks": [{"type": "page", "...": "..."}],
  "constraints": ["单文件，不引第三方库"],
  "out_of_scope": ["多人协作", "登录"],
  "understanding": {
    "problem": "一堆事靠脑子记，分不清哪些在做、哪些还没动",
    "success_looks_like": ["打开就有东西", "点一下能加一条", "拖一下就换列", "刷新还在"],
    "audience": "用户本人，桌面每天开着",
    "assumptions": [
      {"id": "a1", "text": "用 localStorage，不做后端",
       "why": "任务没提后端，工作区是空目录", "risk": "low"}
    ],
    "open_questions": [],
    "confirmed": {"at": "2026-11-20T10:00:00Z", "by": "user", "revision": 1}
  },
  "baseline": {"objective": "...", "done_when": ["..."], "out_of_scope": ["..."],
               "requirement_ids": ["R0"], "at": "...", "revision": 1}
}
```

字段约束：

| 路径 | 类型 | 约束 | 缺省行为 |
|---|---|---|---|
| `understanding.problem` | str | ≤300 字 | 空 → `objective_problems` 可能触发 `GOAL_IS_SOLUTION` |
| `understanding.success_looks_like` | list[str] | 1–6 条，每条 ≤120 | 空 → `CHECK_NO_POSITIVE` 风险 |
| `understanding.audience` | str | ≤80 | 空不影响判定，只影响提示词措辞 |
| `understanding.assumptions` | list[dict] | ≤8；`{id,text,why,risk}`；`risk ∈ low\|medium\|high` | 进交付的"替你定的"（复用 `_with_delivery_notes`） |
| `understanding.open_questions` | list[dict] | ≤5；`{id,question,blocking}` | `blocking:true` 时不许开工 |
| `understanding.confirmed` | dict | 只在用户点头后写 | 空 = 未经确认（**不是**错误，单步任务允许） |
| `baseline` | dict | ≤1；冻结后不可变 | 空 = 还没确认过范围 |

**必须同时改的地方**（否则字段静默消失）：
`normalize_goal` 的返回字典（`services/plan.py:169-175`）。现在它手工拼 5 个键，
新增的 `understanding` / `baseline` 不加进去就会被丢掉——**不报错，是丢掉**。

#### 7.1.2 `plan_steps` 每项扩展

```json
{
  "id": "s3f9a1c2d0",
  "step": "三列骨架 + 首屏种子数据",
  "status": "pending",
  "milestone_id": "m1",
  "evidence": "",
  "output": "index.html 打开即见的三列，且至少 3 张卡片分布 ≥2 列",
  "output_paths": ["index.html"],
  "check": {"type": "page", "url": "index.html", "phase": "open",
            "expr": "document.querySelectorAll('.task-item').length", "min": 3},
  "req_ids": ["R2", "R3"],
  "estimate_min": 30,
  "est_calls": 2,
  "depends_on": []
}
```

| 字段 | 类型 | 约束 | 空值语义 |
|---|---|---|---|
| `output` | str | ≤200；非空（act 任务强制） | 空 → `WP_MULTI_OUTPUT` 判不了，按不合格处理 |
| `output_paths` | list[str] | ≤5；工作区相对路径 | 空 = 产出物不是文件（命令结论/页面断言） |
| `check` | dict | **单条**，不是数组；类型在 `CHECK_TYPES + ("page",)` | 空 → `WP_NO_CHECK` |
| `req_ids` | list[str] | 1–3，必须存在于 `requirements` | 空 → `WP_NO_REQ` |
| `estimate_min` | int | 5–120 | 空 → 按 `est_calls × 6` 填 |
| `est_calls` | int | 1–4 | 空 → 按 `estimate_min / 6` 填 |
| `depends_on` | list[str] | ≤3，必须是本计划内已存在的 step id | 空 = 无前置 |

**`check` 为什么是单条而不是数组**：判据 1（单一产出物）要求一个包一个产出物，
一个产出物对应一条主判据。需要两条 check 才能证明的，说明是两个产出物，该拆。
这条限制本身就是一个**拆分的强制函数**——比任何提示词都有效。

**必须同时改的地方**（三处，缺一处字段就丢）：

| 位置 | 现状 | 要改成 |
|---|---|---|
| `services/plan.py:299-307` | 只拼 5 个键 | 加 7 个键 + 类型清洗 |
| `services/tools/plan.py:250-268` | `steps.items.properties` 只有 5 个 | 加 7 个 property |
| `api/routes/tasks.py:570` | 手工改计划只遍历 5 个键 | 加 7 个键（否则手工编辑会把它们抹掉） |

#### 7.1.3 `requirements`（新列）

见 §5.1。归一化函数：

```
normalize_requirements(raw) -> list[dict]      # 形状容错，同 normalize_criteria 的风格
sync_requirements(goal_spec, steps, criteria) -> list[dict]     # 纯函数，生成
resolve_requirement_status(reqs, steps) -> list[dict]           # 纯函数，推出状态
requirement_problems(reqs, steps, criteria) -> list[str]        # 纯函数，V1–V4
```

#### 7.1.4 迁移 `0038_requirements_and_work_packages`

照 `0028_plan_persistence.py` 的模板写（**必须带存在性检查**，理由见那份迁移的注释：
本仓开发库存在版本号与实际结构不一致的历史）：

```python
additions = (
    ("requirements", sa.JSON(), sa.text("('[]')")),   # 新列
)
# plan_steps / goal_spec 形状变化走应用层归一化，不改列类型（与 0028 对 acceptance_criteria 的口径一致）
```

存量行不需要回填：`requirements=[]` 时 `requirement_problems` 直接返回空（"这条任务没登记需求"），
`scope_review` 输出空，`verdict` 不变。**向后兼容是硬要求**——否则上线那一刻所有在跑的任务都会被判失败。

### 7.2 提示词契约

三个节点，每个给完整输出结构与校验失败后的错误码。

#### N1 `create_plan`（扩展 `INTENT_INSTRUCTION`，`intent.py:163-184`）

改动原则：**不删现有规则**（`intent` / `answer` / 长度纪律全部保留），只把 `steps` 的结构升级。

```
只输出一个 JSON 对象：
{
  "intent": "answer / advise / act",
  "problem": "他要解决的麻烦，一句话，≤60 字。用户没说就留空。",
  "objective": "要达成什么，一句话，≤80 字。**不许出现交付物名词**
                （看板/页面/系统/工具/脚本/平台/后台）——那个是手段，不是目的。",
  "success_looks_like": ["打开时应该看到什么", "做完一遍变成什么", "刷新/重启之后还在不在"],
  "not_doing": ["这次明确不做的"],
  "audience": "谁来看这个结果，≤30 字",
  "assumptions": [{"text": "我替你定的", "why": "凭什么这么定", "risk": "low/medium/high"}],
  "questions": [{"question": "只有必须问才写，一次最多 3 个", "why_blocking": "不问会怎样"}],
  "steps": [
    {"step": "做什么，≤40 字（比之前放宽，因为要装下产出物）",
     "output": "这一步产出一个什么可指认的东西",
     "check": {"type": "path_exists|file_contains|command|page", "...": "见下"},
     "req": "R1",
     "estimate_min": 25,
     "est_calls": 2}
  ],
  "answer": "intent 为 answer 时写完整答案"
}
```

关键约束（写进提示词，与校验器一一对应）：

```
- steps 每条必须有一个 output：一个文件、一条命令的结果、或一段可读结论。
  写不出 output 的步骤，说明它还没想清楚，不要写。
- steps 每条必须有一个 check，且必须是**肯定式**：说"应该看到什么"，
  不说"不应该有什么"。"无报错""不引第三方库"这类不算判据。
- step 不超过 6 条。需要更多，说明该分阶段，不是该列更细。
- 同一个 output 不许出现在两条 step 里——那是同一件事写了两遍。
```

#### N2 `update_plan`（`tools/plan.py:232-296`）

新增 4 个 optional property（`output / check / req_ids / est_calls`），`estimate_min` 同名。
描述里加一段（模型的指令来源就是工具描述）：

```
每个步骤要为**可验证**负责：output 写这一步产出什么，check 写怎么机器核对它。
check 只认四种：
  path_exists   {path}                    文件和目录在不在
  file_contains {path, text}              文件里有没有这段文字
  command       {cmd}                     这条命令这次跑过、退出码为 0
  page          {url, phase, expr|assert_text, min}   页面打开/走完动作之后，表达式满足
page 是最强的一种：前端交付物必须至少有一条，写"打开时看到什么"。
```

**分阶段**：T1 阶段 `output/check` 是 optional（存量调用不崩），T2 之后对
`intent == act` 且 step 数 > 1 的计划强制。

#### N3 `summarize_result`（`graph.py:7083-7094`）

现在让模型自评 `covered`。改成"引用判据结果"，并要求模型**不能升级**：

```
只输出 JSON：
{"report": "三段式交付说明",
 "acceptance": [{"criterion": "原文", "covered": true,
                 "evidence": "证据，必须指向一条已执行的事实（文件/命令退出码/页面断言）"}]}

规则：covered 只能**下调**。如果有任何一条判据的机器核对结果是 failed 或 unknown，
covered 必须是 false，并在 evidence 里写"未通过：<事实>"。
不要把"我做了"当成"验证过了"。
```

配套：`graph.py:7110-7125` 组装结果时，把 verifier 的 `done_when` 逐条结果按文本匹配
塞进对应 `criterion`，模型说 covered=true 而事实是 failed → 记 `override_attempted` 事件，
最终 `covered` 取事实。**模型只能降级，不能升级**——这条与 `external-reference-2026-11.md:203`
的"`check` 字段模型不能改"是同一个纪律。

#### 错误码 → 模型该怎么做

`fail(message)` 的 message 是**给模型的指令**，不是给用户的错误提示。所以错误码表就是契约的一部分：

| 错误码 | message 应该说什么 |
|---|---|
| `WP_NO_CHECK` | "第 N 步没有 check。它产出的东西怎么机器核对？写一条肯定式判据，例如 path_exists 或 page 断言。" |
| `WP_MULTI_OUTPUT` | "第 N 步的 output 里有两个东西（A 和 B）。拆成两步，或者只留一个。" |
| `WP_TOO_BIG` | "第 N 步预计 {est_calls} 次工具调用，超过 4 次。说明它没想清楚产出什么——先拆出'能看到中间结果'的那一步。" |
| `WP_TOO_SMALL` | "第 N 步预计 {est_calls} 次调用，太小了，它和相邻步骤共享同一个产出物。合并。" |
| `CHECK_NEGATIVE_ONLY` | "你的判据全是否定式。空交付天然满足否定式。改写成：打开时应该看到什么。" |
| `CHECK_NO_FIRST_SCREEN` | "这是前端交付物，但没有任何一条判据看首屏。加一条 page：打开时页面上有什么。" |
| `GOAL_IS_SOLUTION` | "objective 里只有交付物名词（{命中词}），没有说明要解决什么。补一句 problem：他现在是怎么处理的、卡在哪。" |
| `REQ_UNCOVERED` | "需求 R{n}（{text}）没有任何步骤覆盖。补一步，或者把它放进 out_of_scope。" |
| `SCOPE_EXTRA_PLANNED` | "你在为 R{n} 排活，但它已经在 out_of_scope 里。要么去掉这一步，要么先跟用户确认范围变了。" |
| `CHANGE_NO_IMPACT` | "需求变了，但说明里没写影响面。回答三件事：影响哪些已完成的步骤、哪些判据要改、还要多少步。" |
| `CHECK_UNVERIFIABLE` | "check 类型 {type} 认不出来。只支持 path_exists / file_contains / command / page。" |

### 7.3 确定性校验器清单

| # | 校验器 | 入参 | 判什么 | **能验** | **验不了** | 落点 |
|---|---|---|---|---|---|---|
| 1 | `normalize_goal/steps/criteria/requirements` | 原始 JSON | 形状归一、类型清洗、长度截断 | 形状对不对 | 内容对不对 | `services/plan.py` |
| 2 | `work_package_problems(steps)` | plan_steps | 判据 2/3/6/7 + 重复 check | 有没有 check、估算区间、req 指向、check 重复 | 产出物是不是"单一"（只能靠 `output` 文本规则半判） | `services/plan.py`（新函数） |
| 3 | `check_shape_problems(goal_spec, steps)` | done_when + checks | 肯定式、首屏、模糊词 | 否定式词表命中、`page` 缺失、`应该能` 类表述 | 判据是否**足以**证明需求（那是确认范围） | 同上 |
| 4 | `objective_problems(goal_spec)` | goal_spec | 方案词、逐字复述、过长 | 三条文本规则 | 用户真的要那个交付物时（误报，需衡量） | 同上 |
| 5 | `sync_requirements` + `requirement_problems` | 三份数据 | RTM 完整性 V1–V4 | 漏项、反向缺失、超范围排活 | **100% 规则（V5）** | 新模块 `services/requirements.py` |
| 6 | `scope_review` | 全量 | missing / extra / undelivered | 文件级超范围、must 未 done、勾了没验 | 需求语义是否被真正满足 | 同上 |
| 7 | `verifier._run_done_checks` + `page` | done_when + 工具回执 | 逐条事实 | 文件/文本/命令/页面断言 | 判据写得对不对 | `verifier.py:192-236` |
| 8 | `resolve_milestone_status` / `resolve_requirement_status` | steps + 父项 | 状态推导 | 由子推父 | 模型自报的状态（**故意不采信**，`plan.py:344-350`） | `services/plan.py` |

**三态纪律**（必须沿用）：每个校验器返回 `passed / failed / unknown`，
**只有 failed 才拦**。`unknown ≠ failed`——这条在 `verifier.py:198-199` 和
`persona.py:564`（"unknown ≠ failed 的纪律与 done_when 一致"）都写过两遍了，不要再破一次。

**明确验不了的六件事**（写在这里是为了防止以后有人以为能验）：

1. 这条需求是不是用户真正想要的（确认范围，只能问人）；
2. 100% 规则（覆盖是否完整）；
3. 拆分是否"最合理"（只能事后看返工率）；
4. 判据是否足以证明需求（`page` 断言可以都过而需求没被满足——那是判据设计问题）；
5. 用户的"成功的样子"有没有被穷举（`success_looks_like` 是启发式清单，不是完备集）；
6. 变更的影响面是否完整（模型回答的三个问题是否穷尽）。

### 7.4 分阶段落地顺序与每阶段判据

| 阶段 | 工作量 | 改什么 | 立刻见效的证据 | **阶段判据（可验收）** | 不许做什么 |
|---|---|---|---|---|---|
| **T0** | 0.5 天 | `page` check 类型（`plan.py:70`）+ `verifier._run_done_checks` 一支（`verifier.py:203-236`）+ `set_goal` schema | 空框任务被判 `failed` | 用 `acceptance/kanban.json` 的断言复现空框，verdict 从 `passed` 变 `failed`，findings 明确指出首屏断言没过 | 不许把 `page` 做成 blocking 的唯一路径——存量任务没有 page 判据，不能因此全判失败 |
| **T1** | 1–2 天 | plan step 四字段 + `work_package_problems` + `_update_plan` 打回 + 三处归一化 | 计划里不再出现"实现整个功能" | 重放夜班那批"一步实现整个功能"的任务，计划被拒 ≥1 次并重写；**误拒率 <20%**（人工看 20 条） | 不许一上来就强制（先 optional，跑 20 条再开） |
| **T2** | 2–3 天 | `goal_spec.understanding` + `objective_problems` + 确认卡（复用 `propose_goal`） | "给我做个看板"先问一句"你现在怎么处理这堆事" | 20 条历史 goal 离线跑 `objective_problems`：命中率 ≥60%，误报率 ≤20%（人工标） | 不许对 `intent != act` 弹卡；不许一次问超过 3 个问题 |
| **T3** | 3–5 天 | `requirements` 列 + `sync_requirements` + `requirement_problems` + `scope_review` + 迁移 0038 | 收尾有"漏了几条 / 多做了什么" | 与 20 条已结束任务对照：人工标出真实漏项，`missing` 的召回 ≥70% | 不许让空 `requirements` 改变任何 verdict（向后兼容硬要求） |
| **T4** | 等样本 | 粒度与覆盖指标进 `evolution.py`（`requirement_coverage` / `first_screen_pass_rate` / `rework_rate`） | — | 每侧样本 ≥20 才下结论（`evolution.py:29`） | 不许在样本 <20 时说"变好了"（这条已经栽过，见 `evolution.py:14-15`） |

**排序理由**：T0 最便宜（一个函数分支）且直接对着最贵的失败（空框 = 外部失败成本）；
T1 的收益最大（计划质量是后面所有环节的地基），但它要改三处归一化，风险高于 T0；
T2 需要历史数据才能调规则，必须先有 T1 的计划数据才有得看；
T3 要有 T2 的 `understanding` 才能生成有意义的需求条目。

---

## 8. 怎么验证这套东西有效

### 8.1 新增三个指标（进 `harness/evolution.py`）

| 指标 | 定义 | 方向 | 数据来源 |
|---|---|---|---|
| `requirement_coverage` | `must` 需求中 `status=done` 的比例 | 越大越好 | `tasks.requirements` |
| `first_screen_pass_rate` | 有前端交付物的任务里，首屏 `page` 断言通过的比例 | 越大越好 | `verification.verdict.evidence.done_when` |
| `rework_rate` | 同一 thread 内连续两轮 objective 相同但前一轮 `failed` 的比例 | 越小越好 | `tasks.goal_spec` + `thread_id` + `turn_index` |

这三个都加进 `DIRECTION`（`evolution.py:35-45`）。注意 `rework_rate` 是**越小越好**——
"方向因指标而异"这条纪律（`evolution.py:16`、`evolution.py:35-45`）在这条上最容易搞反。

### 8.2 三个实验

| 实验 | 样本 | 做什么 | 指标 | 什么样的结果算失败 |
|---|---|---|---|---|
| **A（离线，立刻可做）** | 20 条历史任务 | 只跑"计划生成 + 新校验器"，不执行 | 打回条数、打回里"确实该打回"的比例（人工看） | 打回率 >50% 且误拒 >30% → 规则太紧，先放宽 |
| **B（在线 A/B）** | 每侧 ≥20 条已结束任务 | 按 project 分流，一半开 T1+T2 | 三个新指标 + `success_rate` | 样本 <20 不下结论；样本够但 `rework_rate` 不降 → 机制没生效，回去看是哪个校验器被绕过了 |
| **C（反例集，最快）** | 3 个固定 fixture | 空框 / 一步实现整个功能 / 全自评，每次改动必须全绿 | 通过/失败 | 任何一个从 fail 变 pass = 回归，直接拦 |

**C 应该第一个做**，因为它不需要样本、不需要上线、跑一次 30 秒。
它的形态是：把三类已知失败做成固定的 `goal_spec + plan_steps + 交付文本` 输入，
断言新校验器的输出。这是这套设计里唯一能"当天证明有效"的东西。

### 8.3 不要做的验证方式

- **不要用"模型觉得计划更好了"**。模型自评是我们要治的病，不是药。
- **不要用单条任务的成功/失败判断机制好坏**。一条任务的方差远大于机制的效果。
- **不要在样本 <20 时对比新旧**。`evolution.py:14-15` 记着一次几乎读反的教训：
  "机制后 0.5 高于机制前 0.071，差一点被读成机制没用——实际分母只有 4 个签名。"

---

## 9. 事实与推测的分界

| 结论 | 类型 | 依据 |
|---|---|---|
| 空框案例：三条/四条判据全过，交付是空页面 | **实测记录** | `scripts/acceptance.py:6-15`、`AGENTS.md:68` |
| 判据全是否定式时，空壳是最优解 | **推断（有实测支撑）** | `acceptance.py:13-14` 的原文结论，本条是对它的机制化表述 |
| `success_condition` 生成后没有任何地方读 | **源码事实** | 全仓 grep 只命中 `planner.py` 与 `tests/test_v165_phase2_units.py` |
| `plan_manifest` 不落库 | **源码事实** | `runtime.py:2137-2146` 只写 `plan_steps/milestones/goal_spec` |
| `normalize_goal` 会丢弃未知键 | **源码事实** | `services/plan.py:169-175` 手工拼 5 个键返回 |
| `normalize_steps` 会丢弃未知键 | **源码事实** | `services/plan.py:299-307` 手工拼 5 个键返回 |
| 手工改计划只认 5 个键，且不含 `cancelled` | **源码事实** | `api/routes/tasks.py:570`、`schemas/task.py:198` |
| `done_when` 的 check 只有三种，且 verifier 不设 blocking | **源码事实** | `services/plan.py:70`、`verifier.py:197-199` |
| `browser_check` 已支持动作序列与断言，`acceptance.py` 与它同构 | **源码事实** | `services/tools/browser.py:243-248`、`scripts/acceptance.py:95-124` |
| 每任务均值 69k token / 2.38 步 | **实测基线**（98 条任务） | `docs/dubhe-alignment.md:82` |
| "预防 + 评估比返工低 1~2 个数量级" | **推算** | 3–6k vs 69k；69k 是均值不是返工轮实测 |
| `estimate_min ≈ est_calls × 6` | **估计值** | 我们的经验，未做统计。T1 上线后应用真实数据校准 |
| "1–4 次工具调用"是工作包的健康区间 | **估计值** | 来自 `progress.py:34` 的 `IDLE_LIMIT=4` 的间接推论，**不是**针对工作包大小做的测量 |
| PMBOK 第 6 版第 5 章 5.1–5.6 是范围管理的六个过程 | **通行理解** | 未逐字核对原文。若与原文有出入以原文为准 |
| 100% 规则、WBS 词典内容、Validate Scope 与 Control Quality 的区分 | **通行理解** | 同上 |
| 8/80 是"工作包 8–80 小时" | **通行启发式** | PMBOK 把它作为启发式提及，**不是硬性数值**；本文不用它 |
| SMART（Doran 1981）、Definition of Done（Scrum） | **非 PMBOK** | 明确标为借用；不要写成"PMBOK 要求" |
| CCB 是变更控制里的批准角色 | **通行理解** | 本文只借用"影响分析 + 批准"两件事，不引入 CCB 组织形态 |
| `out_of_scope` 只进提示、不参与判定 | **源码事实** | `graph.py:2810-2813`（只拼进提示文本） |
| 前端交付物的判据"必须有首屏断言" | **已有项目约定** | `AGENTS.md:70-75`；本文把它从约定升级为代码门（T0） |

**未找到 / 无法确认**（不做推测）：

- PMBOK 对"验收判据的**形态**"（肯定式 / 否定式）没有任何规定。这是我们的工程结论，
  不是管理学结论——`AGENTS.md:64-68` 就是这么写的（来自实测，不是来自标准）。
- 没有找到任何外部实现对"工作包粒度"给出可机器校验的判据。opencode 没有计划对象，
  DSH 的 plan 是对话内文本（见 `docs/external-reference-2026-11.md:132-135`），
  两家的 `done_when` 也都不如我们（DSH 让执行模型自证并自认 "No independent evaluator"）。
  所以 §4.1 的七条判据是自建的，没有可对照的先例。
- 我们没有"工时"数据，所以 8/80 无法在我们系统里被验证——这也是 §4.1 第 6 条改用
  工具调用次数的原因，而不是"我们觉得更好"。
