# 使用者模型：让 agent 学“他怎么干活”

> 这份报告只回答一个问题：agent 怎么从**用户自己的行为**里学到他的工序、粒度、验收标准、表达偏好和决策边界，让下一次做出来的东西越来越像他，而不是越学越偏。
> 它不回答“这个仓库有什么坑”——那是 `experience.py` 的事。
>
> **证据标记**：【事实】= 官方文档 / 代码 / 论文原文可核对；【二手】= 第三方转述或论坛回复；【推断】= 我的判断；【查不到】= 没有公开依据。
>
> **代码位置**：经验库实际在 `services/api/src/blank_api/services/agent/experience.py`（设计文档里的 `services/agent/experience.py` 是旧路径）。下文 `schemas/task.py`、`graph.py`、`runtime.py`、`persona.py` 分别指 `services/api/src/blank_api/schemas/task.py`、`services/api/src/blank_api/services/agent/graph.py`、`services/api/src/blank_api/services/agent/runtime.py`、`services/api/src/blank_api/services/agent/persona.py`。
>
> 截至 **2026-11-19**，工作区里有一版**未提交**的 `persona.py`。本报告把它当作“当前实现”来对照；只读代码，没有运行，也没有改任何代码。

---

## 0. 现状：五条信号，基线里零条流进“人”

### 0.1 逐条对照

下表是 `persona.py` 出现**之前**的基线；前五条是用户信号，最后一条是经验库（放进来是为了说明为什么不能直接复用它）。0.3 说当前实现补了什么。

| 已有信号 | 代码位置 | 现在流向哪里 | 持久化吗 | 能学“人”吗 | 缺什么 |
|---|---|---|---|---|---|
| **批准**，作用域 `once` / `prefix` / `tool` | `schemas/task.py:136-151`；`graph.py:4550-4570` | `once` 只对本次调用生效；`tool` 只在本会话；`prefix` 且工具是 `run_shell` 时写进 `permissions.rules`，跨任务/重启仍有效 | 只有 prefix 规则落盘 | 只回答“**允许什么**”，不回答“**为什么允许、什么时候该问**” | 没有任务类型 / 路径作用域；没有否定样本；只覆盖 `run_shell` |
| **驳回** + 一句话（`feedback`，≤600 字） | `schemas/task.py:151`；`graph.py:4581-4616`；拼回执在 `graph.py:4651-4668` | 拼进当轮工具回执交给模型；同时写进 `approval.denied` 事件的 `feedback` 字段 | 事件库里有，但基线里**没有任何学习者读它**（`persona.py` 现在读，见 0.3） | **这是最纯的“用户逻辑”信号**：他亲口说了为什么不许这么做 | 没有抽取、没有去重、没有作用域、没有晋级/退役 |
| **任务描述** `title` / `goal` | `schemas/task.py:26-27` | `experience.paths_from_goal()` 从 goal 里认路径，用于检索经验；不进入任何“人”的模型 | 任务表里有 | 只被当成“这次要做什么”，没被当成“他一贯怎么做” | 没有区分“这次的任务”与“他的要求”；没有跨任务聚合 |
| **同 thread 追问**（用户补一句 / 纠正） | `api/routes/threads.py` | 作为下一轮 user 消息进上下文 | 消息表里有 | **完全没被抽取**：用户说“不对，我是说先写测试”，这句话用完就没了 | 没有纠正检测、没有签名、没有与已有偏好合并 |
| **显式记忆** `memory_write`（类型含 `user` / `feedback`） | `services/tools/memory_tools.py:50-63`；类型定义在 `services/memory.py:37-43`；开关在 `scenarios.py:152-166` | 模型自己决定写不写；只在「日常场景 + 用户选了记住」时开放；开发场景一律禁用 | 写进项目 markdown，散文 | 形状是对的（`user` / `feedback` 两类），但**写入靠模型自觉**，不是从行为里学 | 没有证据、没有置信度、没有作用域、没有生命周期；开发任务学不到 |
| **经验库** | `experience.py:162`（准入）、`:265`（检索）、`:376`（抽取）、`:565`（回写）、`:600`（退役） | 任务收尾抽 0–2 条；任务开始最多注入 1 条 | 每工作区一份 JSONL | 学的是**世界**：某个命令会超时、某片路径要这样验证 | 没有 `subject`（人 / 仓库）与 `scope`；准入要求“有改动 + 有验证”，所以**“先给方案再改代码”这类工序偏好永远进不来**；检索要求路径/报错硬命中，工序偏好没有路径可命中 |

### 0.2 三个结论

1. **驳回理由是唯一“带理由的否定”，基线里只用一次。** 它进了当轮回执和事件账本，但没有变成任何可复用的东西（`graph.py:4581-4616`）。这相当于每次都在重新学同一个人的同一条边界。
2. **基线里唯一已经持久化的“人”的信号是 prefix 权限规则**，但它只回答“这条命令以后别再问我”，不回答“他为什么同意 / 他在什么情况下会不同意”。它是权限，不是工序。
3. **`experience.py` 复用不了。** 它的准入要求“有改动 + 有验证”（`experience.py:182-199`），而“先写测试再实现”“一次只改一个文件”发生在改动之前，没有 `changed_paths`；它的检索要求路径或报错签名硬命中（`experience.py:294-317`），而工序偏好没有路径。把工序塞进经验库，要么被过滤掉，要么被路径误命中。

### 0.3 当前实现（`persona.py`，未提交）已经补了什么

工作区里已经有一版 `persona.py`，方向和本报告一致，先把它说清楚，后面的对照才有意义：

- **只从用户原话学**：`Preference.quotes` 是准入门槛，没有原话不写入（`persona.py:101-123`、`:509-531`）。这直接堵住了“agent 自己偷懒成功 → 归纳成用户喜欢快”的回路。
- **两条写入路径**：驳回反馈当场记（`graph.py:4584-4600`）；任务描述只能通过 `scripts/persona.py --harvest` 显式扫，且只出候选、不自动生效（`scripts/persona.py:195-254`）。
- **两次才算一贯**：`MIN_OCCURRENCES = 2`（`persona.py:52`），单次纠正只当临时约束。
- **同维度冲突两条都停用**：`merge()` 把方向相反且说的是同一件事的两条压成 `conflict`，都不注入（`persona.py:292-350`）。
- **注入在 system**：`runtime.py:1540-1546` 读取，`graph.py:2797-2800` 拼进第一段 system；理由是压缩时保留“第一条 system + 当前目标”，放 user 消息会在压缩后消失。
- **白盒入口**：`scripts/persona.py` 能看、能 `--promote`、能 `--retire`、能 `--lint`、能 `--report`。

它仍然只覆盖了“驳回”这一条信号；同 thread 追问、批准模式、改写幅度都还没进来。下面逐节说明外部证据怎么评价这套形状，以及还差什么。

一处**文案与实现不一致**：`scripts/persona.py:64-66` 的 `show()` 说“它会从你说过的话里学：任务描述里的明确要求、驳回时写的理由”，但运行时只在**驳回**时写；任务描述必须显式 `--harvest`，且只出候选。这不影响安全，但会让用户以为它自动读了 goal。

---

## 1. 一句话结论

【推断】以我们现在的形状，最小可行的“学用户”闭环**不是建一个用户画像**，而是把“**带理由的驳回**”和“**同一形状的重复纠正**”变成一条**带作用域、带一对反差样本、带用户原话的偏好条目**：默认只作用于这次涉及的路径 / 命令与任务类型，两次**独立任务**出现才允许生效，作用域按阶梯升级（路径 → 仓库 → 跨项目，后两级要人确认），注入时最多 1–2 条且只在硬作用域命中时出现，用“一次通过率 + 纠正复发率 + 决策预测一致率”验收，并用“重复踩坑率 + 验证通过率”做质量护栏。

```
驳回（带话） ─┐
同 thread 追问 ─┼─► 写入闸门 ─► 作用域判定 ─► 偏好条目 ─► 硬命中注入 ─► 回写
重复同形状批准 ─┘  (有原话/≥2 任务)  (路径/命令/任务类)  (claim+scope+反差)   (1–2 条)     (确认/否定/退役)
```

五步各自的“最小可行”定义：

| 步 | 最小可行 | 不做 |
|---|---|---|
| **采** | 只采两种：① 驳回时写了理由的原话；② 同一 thread 里用户对上一轮结果的纠正。批准只在“同一形状重复 ≥2 次”时作为弱证据 | 不从单次 accept 学；不从 agent 自己的成功反推；不从 diff 幅度直接反推偏好 |
| **判** | 作用域从**这次被驳回的调用**里取：工具 + 路径前缀 + 命令前缀 + 任务类型；默认最窄 | 不默认“全仓库”；不默认“跨项目”；不把一句“这次先别动”学成习惯 |
| **存** | 一条 = `claim`（用户原话归一化）+ `scope`（谓词）+ `contrast`（被拒版本 / 他改后的版本，各一个指针）+ `evidence`（原话、任务、时间）+ 生命周期字段 | 不存“他喜欢简洁”这种形容词；不存整段画像；不存没有原话的推断 |
| **用** | 只在硬作用域命中时注入；最多 1–2 条；措辞是“他一贯这样，可能不适用，本次明确要求优先” | 不用语义相似度单独触发；不在每次任务都全量注入；不要求模型引用/复述 |
| **量** | 一次通过率、纠正复发率、决策预测一致率；与 `repeat_mistake_rate`、验证通过率成对看 | 不把“像他”压成一个分数；不在样本 <20 时做前后对比结论 |

**落点（只是指位置，不是本次改动）**：驳回写入在 `graph.py:4581-4616` 已经是现成的钩子；追问写入要在 thread 的 user 消息落库处加一次“纠正检测”；偏好存储可以沿用 `PersonaStore` 的 JSONL + 生命周期；度量入口在 `scripts/persona.py --report` 与 `scripts/repeat_mistakes.py`。

---

## 2. 对照表

### 2.1 产品：它们到底学什么

| 产品 / 方法 | 学什么信号 | 存成什么形状 | 何时注入 | 怎么防过度泛化 | 我们能抄什么（它在 X 用 Y 做 Z；我们的差异是 W） |
|---|---|---|---|---|---|
| **GitHub Copilot Memory** | 【事实】两类：仓库级事实（约定、架构决定、构建/测试命令）+ 用户级偏好（“clearly stated”的、非敏感的表达方式、审批要求、跨项目工作流）。用户偏好以**用户原话**作 citation；仓库事实以支撑代码作 citation（`docs.github.com/en/copilot/concepts/agents/copilot-memory`） | 文本记忆，**两类所有权分开存**：仓库事实归仓库，用户偏好归个人；单条不可在设置里新建/编辑，只能让 Copilot 记（`docs.github.com/en/copilot/how-tos/use-copilot-agents/copilot-memory/create-memories`） | 判断相关时检索；仓库事实用 citation 对当前分支回查，**只有校验通过的才用**；用户偏好靠“最佳判断”确认仍适用 | 只收“明确说过”的偏好；secret / model-control token 拒收；据本仓库前一轮核对，90 天过期、只有 upvote 重置（`docs/self-learning-devtools.md:110-127`）；downvote 过阈值停用；隐藏 ≠ 删除 | 它在 **citation** 上用“用户原话/支撑代码”做可核对性；我们的 `persona.py` 有 `quotes`，但 `render()` 只输出 rule，不把原话给模型看（`persona.py:388-400`）。差异：我们的 citation 只给人看，没给模型看。 |
| **Cursor Rules / 已移除的 Memories** | 【事实】当前只有人写的 Rules：Project / User / Team / AGENTS.md；官方明说“Large language models don't retain memory between completions”（`cursor.com/docs/context/rules`）。【二手】Memories 在 1.0 引入（per-project、per-user），1.2 GA 并加“后台生成需批准”，2.1.x（一说 2.1.17）起移除；Cursor 员工在论坛说“intentionally removed”，建议导出成 `.mdc` 并入 Rules（`gethrbr.com/blog/cursor-memories`；`cachly.dev/docs/cursor-memories-removed`） | `.mdc` + frontmatter：`description` / `globs` / `alwaysApply`；四种激活模式：Always / Apply Intelligently / Specific Files / Manual；优先级 Team > Project > User | 按 frontmatter 决定：每轮、按文件、模型判断、或 `@` 手动 | **靠人写、靠作用域、靠优先级**；不自动推断。官方建议“Agent 重复犯同一个错时再加 rule”，即**人来做频次判断** | 它在 **作用域** 上用 `globs` + 四种激活模式，比我们的 `scope: project/global` 细；我们的差异是：`persona.py` 的 `scope="path"` 只在注释里出现，`injectable()` 只处理 `global` 和 `project`（`persona.py:357-371`）。【推断】它用“移除自动记忆、回到人手写”解决信任问题；我们走的是“自动但只从驳回学”的中间路线，必须补上它的作用域与优先级。 |
| **Windsurf / Devin Desktop 的 Memories** | 【事实】Cascade 在对话中判断“值得记”的上下文，也支持“create a memory of …”；自动记忆**绑定 workspace、只存本机 `~/.codeium/windsurf/memories/`、不进仓库、不跨 workspace**；**Memories 只适用于 legacy Cascade；默认的 Devin Local agent 不持久化记忆**（`docs.windsurf.com/windsurf/cascade/memories`） | 本地记忆文件 + `global_rules.md`（全局，6,000 字符）+ `.devin/rules/*.md`（workspace，12,000 字符/文件，`trigger: always_on / glob / model_decision / manual`） | 模型认为相关时检索；Rules 按 trigger 注入 | 官方明确劝退：**要可靠复用就写 Rule 或 AGENTS.md，别依赖自动 Memories**；记忆可编辑、可删；作用域只有 workspace | 它在 **“记忆 vs 规则”** 上做了明确分工：一次性的、本机的进 Memories；要可靠、要共享的进 Rules。我们的差异是：`persona.py` 把两者混在一个 JSONL 里，且没有“提升为 AGENTS.md / 项目规则”的出口。它的默认 agent **不再持久化记忆**，【推断】这是对自动记忆的一次公开退让；我们如果继续自动学，必须把“停止生成”和“停止注入”分开。 |
| **Devin Knowledge** | 【事实】从 chat 里的**纠正**自动建议 Knowledge（“Devin will automatically suggest Knowledge to remember based on your feedback in chat”），人可编辑或 dismiss；也会建议更新已有条目；Knowledge 有 trigger description，按 repo / organization / enterprise 作用域；Knowledge 已弃用，迁移到 Skills（`docs.devin.ai/product-guides/knowledge`） | Knowledge item = trigger description + content + 可选 macro；现在迁到 Skill（可带脚本/模板） | trigger 匹配时检索，不是全量注入 | **人审**：建议 → 人改/删 → 才保存；作用域 repo/org/enterprise；trigger 必填；可单条 enable/disable | 它在 **“纠正 → 建议 → 人批”** 上是最接近我们驳回信号的：用户纠正一句，它生成候选，人点头才生效。我们的差异是：`persona.py` 在**第二次** explicit 驳回时自动 `active`（`persona.py:312-318`），没有“第二次让人确认”的一步；两次都可能是同一个任务的两次重试。 |
| **Claude Code auto memory** | 【事实】auto memory 记录“你的纠正和偏好”（notes Claude writes itself based on your corrections and preferences）；**按 repository 存**，`~/.claude/projects/<...>/memory/`；`MEMORY.md` 是索引，启动只读前 200 行 / 25KB，细节在 topic files 按需读；用户级偏好放在人手写的 `~/.claude/CLAUDE.md`，项目级放 `./CLAUDE.md` / `./.claude/CLAUDE.md` / `./CLAUDE.local.md`；`/memory` 可浏览、可开关（`code.claude.com/docs/en/memory`） | `MEMORY.md` 索引 + topic files；CLAUDE.md 分层：managed policy → user → project → local | 每个 session 启动加载（前 200 行 / 25KB）；topic files 按需 | auto memory **按 repo 隔离**；跨项目的“我是谁”必须人手写进 user CLAUDE.md；文档明说“Claude treats them as context, not enforced configuration”“no guarantee of strict compliance”；`MEMORY.md` 超限会要求压缩、合并/删除 stale | 它在 **“自动学的按仓库，跨项目的靠人写”** 上划了一条非常清楚的线。我们的差异是：`persona.py` 的存储是**每工作区一份文件**（`persona.py:407-418`），所以 `scope="global"` 实际上跨不了仓库——这条线我们目前是“全都按仓库”，但代码里又声明了 global，语义不一致。 |
| **Lovable Memory** | 【事实】记录你在构建时**说过的**偏好、决定、事实和纠正；分 **Project memory**（属于一个项目，所有能编辑的人共享）与 **Personal memory**（属于你，每个 workspace 一份，只在**该 workspace 的项目与 Chats** 中生效，不跨 workspace）；**project memory 与 personal memory 冲突时 project 胜**；技术/架构决定不写 memory，写成 `AGENTS.md` 里的短规则；付费计划有 **Dreaming** 后台清理：对照代码和聊天，改写变了的、删掉不再适用/重复的（`docs.lovable.dev/features/memory`） | Project memory + Personal memory + `AGENTS.md` 规则；memory 设置里有自然语言摘要，可选中改、可在 chat 里说“Change” | 后续对话自动应用；Dreaming 后台维护 | project > personal；Dreaming 去重/失效/改写；secret 值会被移除；明确“memory 是你说过的，不是从代码里读的” | 它在 **冲突裁决** 上用“更具体的作用域胜”（project > personal），和 Cursor 的 Team > Project > User 同构。我们的差异是：`persona.py` 只在**同维度、同 scope、同 scope_ref** 下判冲突（`persona.py:320-347`），没有“项目规则 > 用户偏好”的跨层裁决；`render()` 也只说“本次任务优先”，没说“项目 AGENTS.md 优先”。 |
| **Replit Memories** | 【事实】opt-in；**User memory** 记“耐久的 workspace 偏好与工作风格”，**Project memory** 记约定/坑/修复，**Custom memory** 自定义可见性；私有 by default，协作共享默认关；显式请求与 Custom Instructions **优先于 memory**；memory 文件可看可改（`docs.replit.com/chat/memories`） | Memory file（user / project / custom 三类），可在 Settings → Memory 查看/更新 | 相关时检索；显式请求 / Custom Instructions 覆盖 | opt-in；user memory 只属于一个 builder + 一个 workspace；不保留敏感信息；显式指令优先 | 它在 **“显式指令 > memory”** 上写得很硬。我们的差异是：`persona.py` 的 `render()` 写了“与本次任务冲突以本次为准”，但没有“用户的显式请求 / 项目规则优先”的分层；而且 memory 是 opt-in，我们的驳回学习是默认开。 |
| **v0** | 【二手·系统提示词镜像】`v0_memories/user/`（个人偏好，跨所有 chat）与 `v0_memories/team/`（团队约定）；`MEMORY.md` 常驻上下文，超过约 200 行截断，细节放 topic files 按需读；默认写 `user` scope，除非用户明确要 team；什么时候写：用户说“记住”、发现用户偏好/约定/模式、项目架构决定、反复出现的调试模式（`systempromptindex.ai/gallery/vercel`） | `MEMORY.md` 索引 + topic files + `skills/` 分开 | `MEMORY.md` 常驻；topic files 按需 | 默认 user scope；写前先读/Glob 查重；不存 secret / 一次性信息；错了要更新或删除 | 它在 **“索引 + topic files”** 上把常驻 token 压到很小；我们的差异是：`persona.py` 一次注入最多 4 条完整规则（`persona.py:58`、`:357-385`），没有“索引 + 按需展开”的层次。 |
| **Cursor Tab 的 accept/reject** | 【二手】accept/reject 作为 reward 做在线 RL，目标是接受率；它更新的是**全局排序策略**，不是“你这个人”的偏好（`cursor.com/blog/tab-rl`，由 `docs/self-learning-devtools.md:17` 转述） | 服务端模型 checkpoint | 下一次补全 | 不针对个人；用全局接受率门槛 | 它在 **accept/reject** 上证明了补全信号可以学“何时该建议”，但学的是人群。我们的差异是：我们把 accept/reject 当“这个人的偏好”来学，风险更高；所以只能把 accept 当弱证据，不能单次就写。 |

**表里能读出的四件事：**

1. **自动学人的产品都在收窄。** Cursor 直接移除自动 Memories；Windsurf 的默认 agent 不再持久化记忆；Copilot 把用户偏好限定为“clearly stated”；Claude Code 把跨项目偏好交回人手写；Lovable 说“memory 是你说过的”。【查不到】没有一家在公开文档里承诺“从你的改写 diff 里自动学会你的工序”。
2. **作用域是主轴，不是附加项。** 所有产品都在回答“这条属于谁”：repo / user / workspace / team / org。【事实】Copilot 用两类所有权，Lovable 用 project > personal，Cursor 用 Team > Project > User。
3. **写入要么人写、要么人批，没有“全自动且全局”。** Devin 是建议 + 人批；Copilot 是明确说过才收；Cursor 是人写；Claude Code 自动学的按 repo 隔离。我们的 `persona.py` 走“两次 explicit 驳回自动生效”，是这条光谱上偏自动的一端，必须用“独立任务”和“作用域最窄”来补偿。
4. **没有一家用“用户改写幅度”作为偏好信号。** 【查不到】Lovable / Replit / v0 的公开文档都只描述“你说过的偏好/决定/事实”，没有说会读你对 agent 输出的 diff。GitHub 在 2022 年测过补全的“持久性 / Levenshtein 小改动”，但那是**生产力指标**，不是个人化（`Ziegler et al., MAPS'22, arXiv:2205.06537`）。

**关于“用户反复说同一件事”的聚合：** 【事实】Copilot 文档说匹配到已有记忆时会 reinforce 而不是重复创建；Claude Code 说 `MEMORY.md` 超限时会合并/删除 stale 条目；Devin 说会建议更新已有 Knowledge。**但没有一家公开说明用“重复次数”作为晋级阈值**（【查不到】）。`persona.py` 的 `occurrences >= 2` 是我们自己的准入规则，不是从产品抄来的；这也意味着它的阈值没有外部校准，应该用我们自己的“纠正复发率”去验证。

### 2.2 学术：用户模型怎么表示、怎么学、哪里会坏

| 方法 / 论文 | 学什么信号 | 存成什么形状 | 何时注入 | 怎么防过度泛化 | 我们能抄什么 / 差异 |
|---|---|---|---|---|---|
| **LaMP**（`arXiv:2304.11406`） | 每个用户的历史条目（摘要、标题、评论等），按任务检索 | 用户 profile 条目集合，**检索增强**进 prompt | 每个 query 检索 top-k 个人条目 | 只取相关条目；没有显式 scope/冲突机制 | 它在 **“按 query 检索个人条目”** 上比“全量画像”稳。我们的差异是：`persona.py` 是启动时全量注入 active 条目，不按任务类型检索；`injectable()` 只有 scope 过滤，没有 task-kind 过滤（`persona.py:357-385`）。 |
| **PrefEval**（`arXiv:2502.09597`，ICLR 2025 oral） | 长对话里显式 + 隐式的用户偏好；3,000 对偏好/查询，20 个主题，10 个模型，最长 100k tokens | 偏好放在对话历史里，测“能不能主动遵守” | 对话中 | 提醒（reminder）、迭代反馈、RAG 都只能缓解；微调显著提升 | 它在 **剂量** 上给了最硬的数字：zero-shot 下，**仅仅 10 轮（约 3k tokens）后，多数模型的偏好遵守准确率就掉到 10% 以下**；隐式偏好更难。我们的差异是：我们把偏好放 system 第一段（压缩会保留），比放 user 消息稳；但长任务里仍可能漂移，需要在**决策点**（第一次编辑前、审批前）再提醒一次，而不是只在开头。 |
| **PersonaMem**（`arXiv:2504.14225`，COLM 2025） | 180+ 个模拟用户、每人最多 60 个 session、15 类任务；静态属性 + **动态演化**的偏好 | 用户 profile（静态 + 动态），给 in-situ query 选最合适的回应 | 每个 query 基于当前 profile 状态 | 要求跟踪“偏好随时间变化”；前沿模型（GPT-4.1 / o4-mini / GPT-4.5 / o1 / Gemini-2.0）整体只有约 50% | 它在 **“偏好会变”** 上是我们最缺的一课。我们的差异是：`persona.py` 只有在**方向相反**时才判冲突（`polar()` 不同且 `_same_subject()`，`persona.py:336-347`）；同方向的演化（“先写测试” → “先实现再补测试”）两条都是正向，不会冲突，注入时按 `occurrences` 取更老的那条（`persona.py:372-385`）。这是会越学越旧的。 |
| **MirageBench / The Personalization Mirage**（`arXiv:2608.04570`） | 只给 3 条事实，让 12 个模型写 6 类个性化内容；150 个人设、143,616 条声明 | 模型自己“推断”的用户属性 | — | 外部 judge 逐条判“有据 / 合理 / 刻板印象 / 编造”；**不能信模型自评** | 它在 **过度推断** 上给了基准：12 个模型全部过度推断 **35%–49%**（均值 41.6%）；任务越需要想象越糟（27%–59%）；多轮里推断属性**近似线性累积、很少修正**；模型自评与外部评分甚至负相关（rho = −0.60，探索性）。我们的差异是：我们没有“推断属性”，只存用户原话，天然避开了大部分 over-inference；但 `dimension_of()` / `polar()` 仍是规则推断，仍可能把一句临时话归错维度。 |
| **BenchPreS**（`arXiv:2603.16557`） | 持久记忆里的偏好 + 收件人/任务语境；测“该不该用这条偏好” | 偏好 + 语境 | 生成时 | 要求**选择性抑制**；但实测模型做不到 | 它在 **“偏好被当成常驻指令”** 上是最直接的反面证据：遵从率越高（AAR），误用率也越高（MR）；模型不是“选择性抑制”，而是**整体调强/调弱偏好应用**，把偏好当成 broadly applicable instructions。我们的差异是：`render()` 明确写了“不是这次任务的指令 / 以本次为准”，方向对；但把它放 system、一次最多 4 条，仍可能整体调强。 |
| **RPEval**（`arXiv:2601.16621`） | 个性化记忆 + 当前 query；测“该不该忽略记忆” | 记忆条目 | 相关时检索 | 用 pragmatic reasoning 判 query 是否真的暗示该偏好；RP-Reasoner 在商用助手上解决 80% bad cases | 它在 **“无关记忆被硬塞进上下文”** 上给了失败模式：用户要“放松的音乐”，系统因为记得“喜欢强节奏”而推荐了不放松的曲子。我们的差异是：`injectable()` 只按 scope 过滤，不看当前任务是否真的需要这条偏好；建议加一个“任务类型 / 当前目标”硬匹配，而不是只靠 workspace。 |
| **PCA-Bench**（`arXiv:2610.39550`） | 64 个改写任务 × 256 个 task–preference 对，偏好与请求有兼容/竞争/矛盾/超范围四种关系 | profile 偏好 + 当前请求 | 生成时 | 分开评估“满足要求”“达成任务目标”“遵守偏好” | 它在 **“遵守偏好 ≠ 做得更好”** 上给了最清楚的结论：当偏好与任务目标竞争时，同时满足要求和偏好的更高成功率，可能伴随**更大的质量损失**。我们的差异是：`persona.py` 的 `render()` 说“以本次为准”，但没有可计算的“本次目标 vs 偏好”仲裁；需要把“任务目标”作为 scope 的一部分。 |
| **PRISK**（`arXiv:2608.28833`） | 13 个模型，比较“无画像 / 有画像 / 有检索记忆” | 用户画像 + 检索记忆 | 相关时 | 测三类风险：无关个性化、偏好收窄、谄媚 | 它在 **副作用** 上给了系统证据：加入用户画像/检索记忆后，三类偏差都被放大（无关个性化、preference narrowing、sycophantic bias）。我们的差异是：我们只从驳回学，理论上不容易学出“谄媚”；但“只学否定”会学到一套**更窄的做事方式**，同样有 preference narrowing 风险。 |
| **STALE**（`arXiv:2605.06527`） | 400 个专家校验的冲突场景、1,200 个 query、100+ 主题、最长 150K；测“旧记忆是否失效” | 带状态的记忆条目 | query 时 | 三维：State Resolution / Premise Resistance / Implicit Policy Adaptation；最好模型只有 55.2% | 它在 **“失效”** 上给了最硬的数字：新证据**没有明确否定**旧记忆时，模型很难自己发现旧状态已过时；检索到新证据 ≠ 会据此行动。我们的差异是：`persona.py` 没有 TTL、没有 `last_confirmed`、没有“同方向新证据覆盖旧条目”；`last_seen` 只记录、不参与注入（`persona.py:122-123`、`:357-385`）。 |
| **DITTO / Show, Don't Tell**（`arXiv:2406.00888`） | 用户自己的**示范输出**（<10 条），把示范当作“优于模型输出”的偏好 | 示范 → 合成偏好对 → DPO 微调 | 训练后常驻 | 固定 reference policy、在线采样、replay，防止小数据过拟合 | 它在 **“示范比描述更有效”** 上给了方法：<10 条示范，平均比 few-shot / SFT / self-play 高 19 个百分点。我们的差异是：我们不能为每个用户微调；但可以存**一对反差样本**（被拒版本 / 他改后的版本）作为 in-context exemplar，成本远低于 DPO。 |
| **Teach Better or Show Smarter?**（`arXiv:2406.15708`） | 指令优化 vs 示例优化 | 指令文本 / 示例集 | prompt | 示例优化泛化更好；指令优化容易过拟合；所谓“优化过的指令”里常藏着 quasi-exemplars | 它在 **“示例 > 规则”** 上给了可迁移的结论：即使很详细的指令，也不如示例能约束行为；示例在验证集/测试集之间的泛化差距更小。我们的差异是：`persona.py` 只存 `rule` + `quotes`，没有存“他改后的样子”；建议每条偏好可选带一个 `accepted_example` 指针。 |
| **MPCoder**（`arXiv:2406.17255`） | 多用户的代码风格：显式语法风格（Checkstyle 25 项）+ 隐式语义风格；PCIDense 50 人/5,794 条、PCISparse 1,121 人/34,642 条 | 每用户 style adapter + 风格向量 | 生成时按 user id | 对比学习把不同用户分开；需要每个用户的历史代码 | 它在 **“风格可度量”** 上给了指标：Coding Style Score（24 维风格向量 + Jensen-Shannon divergence），PCIDense 上 64.50% vs 基座 48.73%。我们的差异是：我们没有 per-user adapter，但可以**借用它的风格向量**做离线度量，不必训练。 |
| **Code stylometry**（Caliskan-Islam et al., USENIX Security 2015） | 代码作者的风格特征（AST 派生） | 随机森林分类器 | 离线判定“这段像谁写的” | 闭集实验；1,600 作者 94%，250 作者 98%；难任务更容易归属，熟练程序员更容易归属 | 它在 **“像不像”可量化** 上给了最强证据：风格确实稳定到可以区分 1,600 个作者。我们的差异是：这是**闭集**、离线、C/C++ Google Code Jam；直接拿来当线上指标会高估。可以作为“风格距离”的上界参考，不能当产品承诺。 |
| **Algorithmic confounding**（`arXiv:1710.11214`） | 推荐系统从用户行为里学，再用学到的去影响用户 | 模型 | 反复训练 | 模拟显示：用被算法影响过的数据训练，会**放大同质化而不增加效用** | 它在 **反馈回路** 上给了机制：只从“用户已经接受的”学，会把分布越收越窄。我们的差异是：`persona.py` 只从“否定”学，短期不容易收窄；但如果以后加入“重复批准”作为正证据，就会踩同一个回路，必须保留“未尝试过的做法”作为探索预算。 |

**两点边界说明：**

- 学术里的 **user simulator** 多用于**评估**（用 LLM 模拟用户来压测助手），不是把用户模型注入产品；本报告只取它“用户表示”的部分（PersonaMem 的 static/dynamic profile、LaMP 的 profile items）。
- 代码风格迁移的后续工作 **Style2Code**（`arXiv:2505.19442`）用双模态对比学习支持未见风格，但同样需要训练；它和 MPCoder 的共同结论是**风格可以被表示和度量**，不是只能靠提示词描述。

### 2.3 一句话读表

**【推断】产品在收窄自动学习的作用域，学术在证明“文本偏好会被过度应用/过期/无关注入”。** 这两条合起来的意思是：**学人可以自动，但只能学“带原话、带作用域、可失效”的一小条，不能学“他是谁”的一大团。**

---

## 3. 偏好的数据结构

### 3.1 五种候选形状

| 形状 | 表达力 | 可审计 | 冷启动 | 抗过度泛化 | 我们能不能用 |
|---|---|---|---|---|---|
| **散文画像**（“他是一个注重测试、喜欢简洁的人”） | 高 | 低（没法逐条删/改） | 快 | 最差（MirageBench：41.6% 过度推断；STALE：过期不自知） | 不推荐。我们原来的 `MemoryStore` 就是散文，已经证明会膨胀、重复、矛盾（`experience.py:1-27` 的复盘） |
| **规则文本**（“改 schema 前先写迁移脚本”） | 中 | 高 | 快 | 中（PrefEval：10 轮后 <10%；BenchPreS：被当常驻指令） | 必用，但不能只有它 |
| **偏好对**（A 优于 B；他被拒的版本 vs 他改后的版本） | 中高 | 高（能指向具体 diff） | 慢（要有对照） | 较好（有具体语境，不容易泛化） | 可用，作为规则的“锚”；DITTO 证明 <10 条示范就够，我们只需要 1 对 |
| **示例片段**（他写过的一段代码 / 一次任务的做法） | 高 | 高 | 中 | 较好（示例自带语境；`2406.15708`：示例比规则泛化更好） | 可用，存指针不存全文；MPCoder 证明风格特征可量化 |
| **参数化 adapter / 微调** | 最高 | 低（黑盒） | 最慢 | 取决于数据 | 不做。单用户数据不够，且不可审计 |

### 3.2 推荐的形状：`ScopedPreference`

这条形状和当前 `persona.py` 的 `Preference` 同源，但补了四类东西：**结构化 scope、独立任务计数、反差样本、失效/回写**。

```yaml
id: pref-7f3a                # 指纹：规则 + scope 归一化后的 hash
subject: user                # user | repo；决定“跟人走”还是“跟仓库走”

claim: "改 schema 时先写迁移脚本，再改模型"   # 用户原话归一化，不改写
dimension: order             # order | granularity | verify | style | boundary | delivery | tooling
polarity: +1                 # +1 要求 / -1 禁止；用于判冲突，不用于渲染

scope:                       # 谓词，不是一句话；空 scope = 不可注入
  workspace: "kbgai"         # "*" 表示跨项目（需要显式提升）
  paths: ["services/api/**/models/**"]   # 路径前缀；从被驳回的调用参数里取
  task_kind: ["schema_change"]           # 任务类型；从 goal/plan 里取
  commands: ["pytest"]                   # 可选：命令前缀
  session_only: false                    # “这次先……”必须为 true，且不落盘

contrast:                    # 一对就够，不存全文，存指针
  rejected: "task:ab12/diff#2"           # agent 被拒的版本
  accepted: "task:ab12/final#2"          # 他改后的版本（如果有）
  note: "他补了迁移文件，没动模型"

evidence:                    # 每条都能回到一句原话
  - {signal: "approval.denied", quote: "先把迁移补上", task: "ab12", at: "2026-11-18T10:12:00Z"}
  - {signal: "followup", quote: "我说的是先迁移", task: "cd34", at: "2026-11-19T09:03:00Z"}

occurrences: 2               # 出现的**独立任务**数，不是提及次数
confidence: 0.65             # 0.3–0.9；永不 1.0
status: candidate            # candidate | active | conflict | retired
first_seen: "2026-11-18T10:12:00Z"
last_confirmed: "2026-11-19T09:03:00Z"
expires_after_days: 90       # 到期停止注入；只有再次确认才续命
hits: 0                      # 注入后被证明有用
misses: 0                    # 注入后被用户否定/带偏
```

每个字段的理由：

| 字段 | 为什么必须是这个形状 | 出处 |
|---|---|---|
| `subject` | 同一条“先写测试”可能是**这个人的习惯**，也可能是**这个仓库的约定**；两者失效条件不同 | Copilot 把仓库事实与用户偏好分开存、分开关（`copilot-memory`）；Lovable project > personal |
| `claim` 用原话 | 改写会把“他说的”变成“我们理解的他说的”；MirageBench 的过度推断就是从“合理延伸”开始的 | `arXiv:2608.04570`；Devin “edit the suggested Knowledge” 也是让人改回自己的话 |
| `dimension` | 判“两条是不是在说同一件事”需要固定维度；自由文本聚类要么靠模型、要么靠词面 | `persona.py:70-99` 的维度表；BenchPreS 的“同一维度整体调强”失败 |
| `scope` 用谓词 | “全仓库”“跨项目”是最容易过度泛化的两个默认值；作用域必须是可判定的条件，不是一句描述 | Cursor `globs` / 四种激活；Copilot 两类所有权；Replit user memory 绑定 builder+workspace |
| `contrast` 一对 | 规则说“做什么”，反差样本说“不要做什么/他改成了什么”；示例比规则泛化更好 | DITTO `arXiv:2406.00888`；`arXiv:2406.15708` |
| `occurrences` 按独立任务 | 同一个任务里两次重试各驳回一次（以后接追问后，denial + 追问也会），会假性达到 2 次；必须是**不同任务** | 当前 `persona.py:301-305` 只加 `occurrences`，没有按 `task_id` 去重；这是实现缺口 |
| `last_confirmed` + TTL | 偏好会过期；过期后继续注入比不注入更糟 | STALE `arXiv:2605.06527`；Copilot 90 天 + 只有 upvote 续命 |
| `hits` / `misses` | 没有回写就不知道这条有没有用；经验库已经证明“只写不读”会停在 candidate | `experience.py:565-598`；OpenHands 的两级 MEMORY.md 没有这个回路（`experience.py:568-571` 的注释） |
| `expires_after_days` | 到期**停止注入**，但保留可追溯；不是自动删 | 我们自己的经验库规则（`experience.py:118-134`）；Copilot 的“隐藏 ≠ 删除” |

### 3.2.1 七个可观测维度（把“工作逻辑”拆开）

“他是怎么干活的”不能是一个形容词，必须落到可观测的维度上。下面这组维度和 `persona.py` 的 `DIMENSIONS` 一致（`persona.py:70-99`），每个维度都要能回答“下次在哪一步想起它”。

| 维度 | 他可能怎么说 | 从哪个信号里取 | 反例（不该学的） |
|---|---|---|---|
| **工序** `order` | “先写测试再实现”“先给方案再改代码” | 驳回理由里的顺序词；追问里的“我是说先…” | “这次先跑一下看看”——这是这次的任务步骤 |
| **粒度** `granularity` | “一次只改一个文件”“别整个仓库一起动” | 被驳回的 patch 范围；他要求拆分的追问 | “把这个文件改完”——这是本次范围 |
| **验收** `verify` | “改完必须跑 pytest”“没有判据不算完” | 他拒绝“完成”的理由；他要求补的验证命令 | “这次不用跑”——临时豁免，不是标准 |
| **表达** `style` | “注释只写为什么”“命名用 snake_case” | 他改你的注释/命名；他指出的风格问题 | “这段先随便写”——临时草稿 |
| **边界** `boundary` | “动 schema 前必须先问”“不要碰生成文件” | 驳回 + “先问/别动”；审批卡上的拒绝 | “这个文件这次先别动”——可能是临时约束 |
| **交付** `delivery` | “最后给截图”“变更说明写清” | 他对结果的追问；他补的交付要求 | “这次先给个草稿”——临时交付 |
| **工具** `tooling` | “用 pnpm 不用 npm”“直接调 list_files” | 驳回命令；他纠正的工具选择 | “这次用 curl 试一下”——临时手段 |

这张表也是**写入闸门**的检查表：一条候选如果落不进任何一个维度，或者落进去但带着“这次/先/暂时”的标记，就不该晋级。

### 3.3 作用域怎么表达

**四层优先级（从高到低）：**

1. **本次任务的明确指令**（user 当前消息 / 任务 goal 里写死的约束）
2. **项目规则**（`AGENTS.md` / `.cursor/rules` / 本项目 `CLAUDE.md` 中的硬约定）
3. **使用者偏好**（`subject=user`，跨任务但默认只在本仓库 / 本路径）
4. **模型默认**

冲突时：**高优先级胜，低优先级不静默生效**；如果高优先级与低优先级冲突且用户没明说，记一条 `preference.conflict` 事件，必要时问一次。依据：Lovable 的 project > personal；Cursor 的 Team > Project > User；Replit 的“显式请求 / Custom Instructions 优先于 memory”；Copilot 的仓库事实与用户偏好分开。

**作用域升级阶梯（【推断】防止一句话变全局）：**

```
一次任务里的一个调用
   │  第 1 次：scope = 这次调用的 path / command（candidate，不注入）
   ▼
同一路径 / 同一命令前缀，第 2 个独立任务
   │  第 2 次：scope = path 前缀（active，可注入）
   ▼
同一仓库，跨 ≥2 个路径，第 3+ 个独立任务
   │  第 3 次：scope = 整个仓库（需要人确认）
   ▼
跨仓库（scope.workspace = "*"）
      第 4 次：必须 `--promote` 人工确认；默认永不自动
```

这不是保守，是**代价不对称**：一条过窄的偏好最多是“这次没帮上”，一条过宽的偏好会让 agent 在不相干的场景里坚持错的做法（BenchPreS / PCA-Bench）。

### 3.4 为什么不是“画像”，也不是“纯规则”

- **不是画像**：MirageBench 的 41.6% 过度推断、PersonaMem 的动态漂移、STALE 的过期不自知，三个证据指向同一件事——**“他是谁”这种整体表示，一旦写错就很难被局部纠正**。而一条 `claim + scope` 写错，可以单独退役。
- **不是纯规则**：PrefEval 在 10 轮后 <10% 的遵守率；Claude Code 官方文档自己说“no guarantee of strict compliance”；BenchPreS 说模型会把偏好当成“broadly applicable instructions”。文本规则必须配一个**具体语境**（反差样本 / 示例指针）才能落地。
- **不是纯偏好对**：偏好对不带“为什么”，也不带作用域；DITTO 需要 DPO 微调，我们用不了；in-context 的 few-shot 又很脆（DITTO 论文原话：few-shot prompting is brittle）。所以是**规则 + 一对反差 + 谓词作用域**的混合。

---

## 4. 怎么度量“像不像他”

### 4.1 先分开两个轴：像 ≠ 对

| | **做对了** | **做错了** |
|---|---|---|
| **像他** | 目标区：既像又对 | **盲区继承**：把他的坏习惯也学来了（最危险） |
| **不像他** | 过度保守 / 通用化：没帮上忙，但没坏事 | 既不像也没用 |

“像他”不能单独最大化。正确的目标是**在质量不下降的前提下，向右上角移动**；任何“像”指标上升而质量指标下降，都要当成回归。

### 4.2 指标表

| 指标 | 可计算定义 | 它代表“像”的哪一面 | 偏差来源 | 我们现在有没有 |
|---|---|---|---|---|
| **一次通过率** `first_pass_rate` | 没有 `approval.denied`、没有后续纠正、任务有结论的任务数 / 有结论的任务数 | 他不用返工就接受；最接近“像他”的结果指标 | 任务难度、审批门槛、用户是否在场；简单任务会抬高；用户懒得纠正会假性抬高 | 【部分】`scripts/persona.py:256-292` 的 docstring 承诺了，但 `report()` **没有算**，只打印了“有结论的任务”数 |
| **驳回率** `denial_rate` | `approval.denied` / (`approval.granted` + `approval.denied`) | 他拦你的频率 | 只有高风险动作会进审批；scope=prefix 的规则会减少后续审批；用户可能“懒得管”而放行 | 【有】`scripts/persona.py:280` |
| **需要解释的驳回占比** `explain_rate` | 带 `feedback` 的 denied / denied | “他得跟我讲道理”的频率；越高越不懂他 | 高风险/合规场景本来就会写理由；不能反向解释为“低=懂他” | 【有】`scripts/persona.py:281`，但方向解释要谨慎 |
| **纠正复发率** `correction_recurrence` | 跨 ≥2 个任务的同一纠正签名数 / 纠正签名总数 | 同一个“不像他”的点有没有反复出现；与 `repeat_mistake_rate` 同构，但对象是“人的纠正”不是“环境的报错” | 样本小；签名归一化（去数字/路径/引号）会误合并；单任务内多次纠正只算一次 | 【无】需要新脚本；可复用 `scripts/repeat_mistakes.py:52-70` 的签名函数 |
| **决策预测一致率** `decision_agreement` | 在历史审批点上，模型/规则预测的“批准/驳回”与用户实际决定的一致率；用 balanced accuracy / AUC，不用 raw accuracy | 最直接的“用户模型”指标：它到底能不能预判他的边界 | 批准/驳回极度不平衡；只覆盖走到审批的动作；用户决策有随机性 | 【无】需要离线评估；特征是工具、路径、命令前缀、任务类型 |
| **改写幅度** `edit_ratio` | agent 产出与用户最终接受版本之间的 changed lines / total lines，或归一化 Levenshtein | 生成后被改了多少；越高越不像 | 任务类型（新文件 vs 改旧文件）、格式化/lint、生成代码的样板比例；GitHub 发现**acceptance rate 比 persistence/Levenshtein 更能预测感知生产力**（`arXiv:2205.06537`） | 【无】我们只存 `apply_patch` 的入参，不存用户手改后的文件；要算这个指标，需要前端保存“接受/修改后的版本”或用 git diff 做真值 |
| **风格距离** `style_distance` | 用户写的代码 vs agent 写的代码，在风格特征上的距离：命名、注释密度、函数长度、错误处理、import 风格等；可用分类器 AUC 或 MPCoder 的 CSS（风格向量 + JSD） | 代码“像不像他写的” | 闭集准确率会高估（stylometry 1,600 作者 94% 是闭集）；文件归属标签噪声；agent 代码污染仓库 | 【无】`persona.py` 有 `style` 维度，但只存文本规则，没有度量 |
| **偏好命中 / 误用** | 注入后，用户下一步是确认还是再次纠正；确认=弱 hit，再次纠正=miss | 这条偏好到底有没有帮上 | 归因不清（任务成功可能是别的原因）；没有反馈不等于命中 | 【无】`persona.py` 没有 `hits/misses`，也没有注入记录 |

### 4.3 当前 `--report` 的三个口径问题

1. **承诺了三个数，只算了两个。** `scripts/persona.py:32` 的 docstring 写“一次通过率 = 没进过修复轮就成功的任务 / 有结论的任务”，但 `report()`（`:256-292`）只算了驳回率、解释率、任务总数，没有算一次通过率。这不是小数点问题：没有一次通过率，就无法判断“驳回率下降”是因为更懂他，还是因为他懒得管了。
2. **`explain_rate` 的方向不能单看。** 它把“带理由的驳回”当成“我不懂他”。但高风险动作（删文件、改 schema、发外部请求）本来就应该带理由；一个 security review 的驳回率天然高。正确用法是**按工具/风险分层**看，而不是全局一个数。
3. **分母混了“审批次数”和“任务数”。** `denial_rate` 的分母是审批次数，`first_pass_rate` 的分母是任务数；一个任务可能有很多次审批。两个数不能直接相除或相加。报告里应写清分母，并按任务聚合一次（同一任务多次驳回算一次“未一次通过”）。

### 4.4 评估协议（否则指标会骗人）

- **按时间切分，不随机切分。** 用前 80% 的审批/纠正记录做“学习”，后 20% 做“预测”。随机切分会让同一任务的多次审批泄漏到训练集。
- **样本 <20 个纠正签名时，不做前后对比结论。** 这是 `repeat_mistakes.py:52-54` 已经写死的规矩：分母小到任何新签名都能把比率翻倍。新指标沿用同一条。
- **报 balanced accuracy / AUC + 支持度，不报 raw accuracy。** 审批数据里“批准”远多于“驳回”；全猜“批准”也能有高准确率。
- **“像”指标与“对”指标必须成对出现。** 建议一个最小面板：
  - 主指标：`first_pass_rate`（结果）、`correction_recurrence`（学习）、`decision_agreement`（预判）
  - 护栏：`repeat_mistake_rate`（少犯错）、验证通过率、回滚/重开率
  - 诊断：`denial_rate` 分层、`edit_ratio`、`style_distance`
- **不做单一综合分。** 一旦把“像他”做成一个可优化的分数，它就会被 Goodhart：模型会学会迎合、少问、少验证，分数上升而质量下降。

### 4.5 参考基线（用来校准，不要当目标）

- **Copilot 补全**：接受率约 30%（【二手】多份材料引用 GitHub 2022 数据；原文 `Ziegler et al., MAPS'22, arXiv:2205.06537`）。它说明“用户接受”本身就是稀疏信号，不能把 accept 当强标签。
- **Claude Code PR**：567 个 PR / 157 个项目，83.8% 被合并；合并的里面 54.9% 未经修改，45.1% 被人改过；人类 PR 合并率 91.0%（同研究；91.0% 这个数见【二手】转述，`Watanabe et al., arXiv:2509.14745`）。换算下来，**约 46% 的提交 PR 原样落地**——这是目前最接近“一次通过率”的公开数字。
- **代码风格**：闭集 1,600 作者 94%、250 作者 98%（Caliskan-Islam et al., USENIX Security 2015）；MPCoder 的 CSS 64.50% vs 48.73%（`arXiv:2406.17255`）。这些是**上界**，不是线上可达值。

---

## 5. 过度泛化与作用域冲突

### 5.1 六种典型形态（都有实证）

| 形态 | 机制 | 证据 | 在我们这里会长成什么样 |
|---|---|---|---|
| **从少量行为推断人格** | 只给 3 条事实，模型补出 12 条属性 | MirageBench：12 个模型平均 41.6% 的声明是过度推断；任务越需要想象越糟（27%–59%）；多轮累积、很少修正（`arXiv:2608.04570`） | 从一次“这个文件先别动”推出“他不喜欢改生成文件”，再推出“他保守” |
| **把偏好当常驻指令** | 不做“该不该用”的判断，整体调强/调弱 | BenchPreS：遵从率越高，误用率也越高；模型不是选择性抑制，而是整体调节（`arXiv:2603.16557`） | `persona.py` 一次注入 4 条 active 规则，模型把它们当“必须遵守” |
| **无关记忆被硬塞** | 检索到就注入，不看当前意图 | RPEval：无关个性化普遍；RP-Reasoner 在商用助手上解决 80% bad cases（`arXiv:2601.16621`） | 用户在做前端页面，注入“后端迁移要先写脚本” |
| **偏好压过当前任务** | 遵守偏好与完成任务目标冲突 | PCA-Bench：同时满足要求和偏好的更高成功率，可能带来更大质量损失（`arXiv:2610.39550`） | 用户这次要快速原型，agent 坚持“先写测试”拖慢 |
| **过期偏好继续生效** | 新证据没有明确否定旧偏好 | STALE：最好模型只有 55.2%；“检索到新证据” ≠ “据此行动”（`arXiv:2605.06527`） | 用户已经换用 pnpm，偏好还停在“用 npm” |
| **反馈回路收窄** | 只用用户已接受的数据训练/学习 | Chaney et al.：算法 confounding 会放大同质化而不增加效用（`arXiv:1710.11214`）；PRISK：画像/记忆放大 preference narrowing（`arXiv:2608.28833`） | 只学“他批准过的做法”，再也提不出他没见过的方案 |

### 5.2 处理范式：写入 / 读取 / 冲突 / 失效

**① 写入闸门（最重要：写错一条，后面所有读取都在放大它）**

- 只收**用户原话**；没有原话不写（当前 `persona.py` 的核心不变量）。
- 只收**带理由的否定**或**同一独立任务形状重复 ≥2 次**；单次 accept / 单次 reject 都不写。
- 拒收**临时/指示词**：“这次”“先”“暂时”“就这个文件”“等会儿再”。当前 `persona.py` 的 `_IMPERATIVE` 里有“先”，会把“这次先别动”收进来；需要显式的 `session_only` 检测。
- 拒收**会拆掉质量底线**的偏好：“不用跑测试”“不用验证”“不用审批”“直接提交”。这类应该在写入侧 veto，而不是只在提示里劝。
- 拒收**敏感属性 / 人格判断**：年龄、性别、政治倾向、健康、能力评价。Copilot 的入库过滤（secret / model-control token / 敏感信息 / 短命指令）是同一层。

**② 作用域阶梯**：见 3.3。默认最窄；升级要独立任务 + 人工确认；`global` 必须是显式动作，不能靠“出现次数多”自动获得。

**③ 读取闸门**

- **硬命中才注入**：scope 的 `paths` / `commands` / `task_kind` 至少一个硬命中；不要用关键词相似度单独触发（经验库已经踩过：`deliverables` / `html` 这类词几乎每个任务都有，`experience.py:294-317`）。
- **最多 1–2 条**：`persona.py` 现在 `MAX_INJECTED = 4`（`:58`），这个数字没有出处；SWE-Exp 的经验剂量消融是 1 条最好（`docs/self-learning-design.md:50-52`），但那是“经验”不是“短规则”。建议先做 0/1/2/4 的消融，再定。
- **在决策点再提醒**：PrefEval 说明长上下文会漂移；只在开头注入不够。可以在第一次编辑前、第一次审批前各提醒一次（同一条，不重复增加剂量）。
- **措辞是“假设”不是“命令”**：当前 `render()` 的“他一贯的做法 / 不是这次任务的指令 / 以本次为准”方向是对的（`persona.py:388-400`）；再补一句“可能不适用，请自行判断”，并附一条原话作依据。

**④ 冲突裁决**

- **跨层**：本次指令 > 项目规则 > 用户偏好 > 模型默认。冲突时低层不生效，记 `preference.conflict`，必要时问一次。
- **同层**：方向相反且同一维度/同一 subject → 两条都停用，报给人（当前 `persona.py` 已经这么做，`merge()` 的 `conflict` 分支）。不要自动选一条：两条矛盾记忆同时被检索时，遵从率 63–72%，遵从后成功率 17–31%（`docs/self-learning-design.md:104-106` 引 2607.10608）。
- **同方向演化**：老偏好被新证据取代，不是“冲突”，而是 `superseded_by`。当前 `persona.py` 只判方向相反，同方向会两条并存、按 `occurrences` 选更老的那条（`:372-385`），这是 STALE/PersonaMem 说的典型失败。修法：同维度、同 scope、`_same_subject`、同 polarity，但 `last_seen` 更新的一条应覆盖旧条（保留旧条为 `superseded`，可追溯）。

**⑤ 失效与回写**

- TTL：默认 90 天；只有“再次确认”（用户又说了 / 注入后任务成功且未被纠正）才续命；单纯被检索不续命（Copilot 的规则）。
- 回写：注入时记 `applied`；任务结束按“成功 / 被纠正 / 被驳回”更新 `hits/misses`；两次 miss 退役（复用 `experience.py:565-598` 的不对称步长：成功 +0.05，失败 −0.12）。
- 退役分层：这条 / 这类（维度或路径）/ 全部。`persona.py` 有 `--retire`（指纹）与 `--retire-all`，没有“这一维度/这一类”；`experience.retire()` 已经有四档（`experience.py:600-649`），可以对齐。
- **“停止生成”与“停止注入”分开**：Windsurf 的 Auto-Generate 是生成开关，不是使用开关（`docs.windsurf.com/.../memories`）；Cursor 移除 Memories 后，2026 年 1 月还有用户报告旧记忆仍进上下文、只是没有 UI（`cachly.dev/docs/cursor-memories-removed`，【二手】）。这是我们必须避免的：删/停用之后要能证明“下一次真的不再注入”。

### 5.3 盲区继承：会不会把他的坏习惯学走

**会，而且有三条独立证据链：**

1. **谄媚**：RLHF 模型本来就会偏向“同意用户”；人类和偏好模型都会非零概率地偏好“写得漂亮的谄媚回答”而不是正确答案（`Sharma et al., arXiv:2310.13548`）。
2. **镜像**：个性化聊天里，LLM 的立场变化比人更大；个性化会放大双向偏移，形成“个性化回声室”（`arXiv:2510.20039`）。PRISK 进一步显示，用户画像和检索记忆会放大无关个性化、偏好收窄、谄媚三类偏差（`arXiv:2608.28833`）。
3. **回路**：只用被算法影响过的用户行为去学习，会放大同质化而不增加效用（`arXiv:1710.11214`）；模型在自己产出上递归训练会丢掉分布尾部（`arXiv:2305.17493`）。从“用户接受过的做法”里学，是同一个回路的轻量版。

**设计上的四条护栏：**

- **底线不可学**：验证、审批、安全、隐私这四类，不允许被“用户偏好”覆盖。写入侧直接拒收“跳过测试/跳过审批/不要验证”的偏好；渲染时再说一遍（当前 `render()` 已经有“不要为了迎合而变通安全规则或跳过验证”，但这是提示层，不是闸门）。
- **保留反例**：被拒的方案不要删，存成 `contrast.rejected` / `negative_example`；这样模型知道“他不喜欢这个”，而不是只知道“他喜欢那个”。
- **定期做“无偏好”对照**：在固定的一小组任务上，比较“带 persona”和“不带 persona”的质量；如果带 persona 后 `repeat_mistake_rate`、验证通过率、回滚率变差，说明学到了坏习惯，应该退役而不是继续加。
- **把“像”与“对”同时报给人看**：只报“驳回率下降”会掩盖“他懒得管了”；只报“风格距离下降”会掩盖“代码更难维护”。两个轴一起看，才能发现盲区继承。

---

## 6. 查不到 / 没有公开实现

以下是我在公开资料里**没有找到**的，不要当成“有但没写”：

1. **Copilot 是否从“用户对 agent diff 的改写”里学偏好。** 【查不到】官方文档把用户级偏好定义为“clearly stated”，用用户原话作 citation；没有说会读你对 agent PR 的修改。它测过补全的 persistence/Levenshtein，但那是生产力指标（`arXiv:2205.06537`）。
2. **Cursor 是否从 accept/edit/reject 三态里区分“偏好”和“这次不同”。** 【查不到】Tab 的 accept/reject 是全局排序 RL（二手转述）；Memories 已移除；没有公开文档说它做“偏好 vs 一次性”的分类。
3. **Cursor 移除 Memories 的内部原因。** 【查不到】只有员工论坛回复“intentionally removed”，没有公开复盘。
4. **Windsurf 的“相关”判定、容量上限、衰减、冲突消解。** 【查不到】官方文档只说“模型认为相关时检索”，没有说怎么判、能存多少、多久过期。
5. **Devin Knowledge suggestion 的模型、去重、冲突消解细节。** 【查不到】文档只说“会根据 chat feedback 建议”，人可编辑/ dismiss；没有说它怎么选、怎么合并。
6. **Lovable / Replit / v0 是否用“用户编辑幅度”作为偏好信号。** 【查不到】三家的公开文档都只描述“你说过的偏好/决定/事实”；没有一家说会读 diff 幅度。
7. **有没有产品公开度量“agent 像不像这个用户”。** 【查不到】没有。代码风格有学术度量（stylometry、MPCoder CSS），但不是产品指标。
8. **有没有论文专门研究“从审批/驳回决策里学开发者的工序偏好”。** 【查不到】最接近的是 PrefEval / PersonaMem（偏好遵守）、DITTO（示范即偏好）、MPCoder（代码风格）；没有一篇把“approval decision”作为开发者偏好的主信号。
9. **长期效果。** 【查不到】现有研究大多是短周期 benchmark；没有公开证据说明“coding agent 长期学用户偏好”会让人更满意还是更盲区。这也是我们必须自己建指标的原因。
10. **Claude Code 的 `#` 快捷方式现状。** 【查不到】旧文档里 `#` 用于快速写记忆；当前文档的主路径是“让 Claude 记住…”→ auto memory，或直接编辑 `CLAUDE.md`，`/memory` 用于浏览与开关。`#` 是否仍是当前入口，当前文档没有明确说明；不要把旧教程当成现状。

---

## 7. 附录：对当前 `persona.py` 的逐条校验（读代码，不是跑结果）

> 说明：`persona.py` 是工作区未提交版本，行号会随改动变化；以下是 2026-11-19 读到的状态。**这些是设计判断，不是运行结果。**

| # | 观察 | 代码位置 | 外部依据 | 风险 | 建议 |
|---|---|---|---|---|---|
| 1 | **只从用户原话学**，没有原话不写入 | `persona.py:101-123`、`:509-531` | Copilot 的“clearly stated”；MirageBench 的过度推断 | 这是最强的防跑偏不变量，应保留 | 不要为了“多学一点”放宽它 |
| 2 | **两次才算一贯**，但 `occurrences` 只加次数，不按 `task_id` 去重 | `persona.py:301-305` | 【推断】同一任务里两次重试各驳回一次，会假性达到 2 次 | 把临时约束学成习惯 | 改成 `distinct_tasks`：按 `quotes[].task` 去重，≥2 个不同 task 才晋级 |
| 3 | **同方向演化不判冲突**：`polar` 相同就不冲突 | `persona.py:336-347` | PersonaMem 的动态偏好；STALE 的隐式失效 | 老偏好按 `occurrences` 压过新偏好，越学越旧 | 加 `superseded_by`：同维度 + 同 scope + `_same_subject` + 同 polarity，`last_seen` 新者覆盖旧者（旧条保留为 `superseded`） |
| 4 | **`scope="global"` 与存储冲突**：`PersonaStore.path_for(workspace)` 每工作区一份文件 | `persona.py:407-418`、`:357-371` | Claude Code 的“auto memory 按 repo、跨项目靠人写”；Copilot 的两类所有权 | `global` 实际上跨不了仓库；代码声明与行为不一致 | 要么把 `global` 存到独立的 user 级文件，要么删掉 `global`，只保留 project/path，避免假承诺 |
| 5 | **`scope="path"` 没有实现**：`injectable()` 只处理 `global` / `project` | `persona.py:357-371`；注释 `:109` 提到 path | Cursor 的 `globs`；我们的经验库用路径前缀 | 一条针对某个目录的驳回会作用到整个仓库 | 从被驳回的调用参数里取路径，存成 `paths` 前缀；`injectable()` 加路径匹配 |
| 6 | **没有 TTL / 没有 `last_confirmed`**：只有 `last_seen`，且不参与注入 | `persona.py:122-123`、`:357-385` | Copilot 90 天 + upvote 续命；STALE 55.2% | 旧偏好永久生效 | 加 `expires_after_days` / `last_confirmed`；到期停止注入，只有再次确认才续命 |
| 7 | **没有 `hits/misses` / 注入回写** | 整个 `Preference` 没有这两个字段 | `experience.py:565-598` 的回写回路；OpenHands 的反例 | 不知道注入有没有用，条目永远停在 active | 注入时记 `applied`；任务结束按结果更新；两次 miss 退役 |
| 8 | **注入在 system 第一段，最多 4 条** | `runtime.py:1540-1546`、`graph.py:2797-2800`、`persona.py:58` | Claude Code 把 CLAUDE.md 作为 user 消息、明说“no guarantee”；BenchPreS 的“整体调强” | system 权威更高，4 条可能被当硬规则；与 `experience.py` 的“user 消息、1 条”不一致 | 保留 system 的压缩优势，但把措辞改成“背景假设，不是指令”，并做 0/1/2/4 消融；或改成“system 索引 + 决策点 user 提醒” |
| 9 | **`render()` 不展示原话** | `persona.py:388-400` | Copilot 的 citation；Devin 让人编辑建议 | 模型看到的是“我们归一化后的 rule”，不是“他说的”；无法判断语境 | 在每条后附一句 `依据：你说过"…"`；原话截断到 80–120 字 |
| 10 | **`origin="explicit"` 的默认值**：`learn_from_text()` 默认 explicit | `persona.py:456-479` | 当前调用点只有 denial 是 explicit；harvest 传 `origin="harvested"`（`scripts/persona.py:238-247`） | 如果以后有人在 goal 路径调用但忘了传 origin，会把任务描述当“他明说的”自动晋级 | 把 `origin` 设成必填参数，或在 `learn_from_text` 里按 `source` 推导，不依赖调用方 |
| 11 | **`dimension_of()` 只有中文关键词** | `persona.py:154-165` | 我们的任务有中文为主，但也有英文 goal/反馈 | 英文反馈会全部落到默认 `boundary`，维度判错 → 冲突判错 | 加英文关键词；或用 `dimension` 未知时置 `unknown`，不参与冲突判定 |
| 12 | **临时/指示词没有显式处理**：“这次先别动”会被收进来 | `persona.py:141-205` | Copilot 拒收短命指令；`persona.py` 的注释里已经踩过“冒烟脚本重复 26 次” | 临时约束被学成习惯 | 加 `session_only` 检测词表（这次/本次/暂时/先/就这个/等会儿），命中则不落盘或标 `session_only` |
| 13 | **`report()` 承诺一次通过率但没算** | `scripts/persona.py:32` vs `:256-292` | 指标缺口 | 无法判断“驳回率下降”是不是因为“懒得管” | 补 `first_pass_rate`，按任务聚合；分层看 `denial_rate` |
| 14 | **`explain_rate` 方向单看会误读** | `scripts/persona.py:281`、`:293-295` | 高风险审批天然带理由 | 把“合规解释”当成“不懂他” | 按工具/风险分层；同时报“无理由驳回率”和“带理由驳回率” |
| 15 | **没有“停止学习”开关** | 只有 `--retire` / `--retire-all` | Windsurf 的生成/使用开关分离；Cursor 的“移除 UI 但仍在注入” | 用户关了之后仍可能被学/被注入 | 分开 `learn_enabled` 与 `inject_enabled`；关闭时在事件里记状态，并在下一次任务证明未注入 |
| 16 | **异常被静默吞掉** | `runtime.py:1540-1546`、`graph.py:4596-4600` | 可用性优先是对的，但没有可观测性 | 学人失败没人知道，库悄悄不更新 | 记一条 `persona.error` 事件（低频），不要只 `pass` |
| 17 | **harvest 只从 `tasks.goal` 扫，不扫追问** | `scripts/persona.py:195-254` | 同 thread 追问是最自然的纠正信号 | 用户说“不对，我是说…”仍然学不到 | 把 thread 的后续 user 消息纳入纠正检测；先做规则式（否定词 + 指代 + 与上一轮的差异），不要一上来调模型 |
| 18 | **批准只用于权限，不用于学人** | `graph.py:4550-4570` | Copilot 的 upvote；DITTO 的“示范优于输出” | 学不到“他反复批准的做法” | 先不做；等 `distinct_tasks` 和回写稳定后，再把“同一形状 ≥2 次批准”作为弱正证据，且必须与否定证据分开计 |

**如果只改三处**（按收益/风险比排序）：

1. **`occurrences` 改成按独立任务计数**（#2）+ **同方向演化覆盖**（#3）。这两条直接决定“学的是不是他现在的做法”。
2. **补 TTL + 回写**（#6、#7）。没有失效和回写，偏好库会变成“越学越自信的旧观念”。
3. **补追问入口**（#17）。现在只学“他拒绝的那一次”，不学“他纠正的那一句”；后者才是“工序”的主要来源。

---

## 8. 一句话收尾

**学人不是把他写成一段画像，而是把他每一次“不行，因为……”变成一条有作用域、有原话、会过期的假设；下一次只在真的像那次的时候，轻轻提一句，然后接受他的再次裁决。**
