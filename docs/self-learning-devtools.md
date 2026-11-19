# 开发工具里的"自学"：真实机制盘点

> 日期：2026-11-19
> 范围：IDE / 编辑器 / coding agent / 代码托管平台里**真实存在**的"从用户行为中学习"，只看官方文档、官方博客、可核对的产品行为。
> 口径：`【事实】`= 有来源；`【推测】`= 我的推断，已标注。厂商没公开的一律写"查不到"，不替它们编实现。
> 对照基线：Kbgai 的 `experience.py`（statement + trigger_paths/task_terms/error_signature + evidence + confidence 0.3–0.9；规则式抽取；最多注入 1 条；路径前缀/报错签名硬命中；按工作区 JSONL）。本快照未包含该文件，实际字段以 `services/api/data/experience/kbgai-f1a6db42.jsonl` 为准（含 `status`、`hits`、`misses`、`expires_after_days`）。

---

## 0. 一句话结论

**最成熟的"自学"是补全的接受/拒绝信号驱动排序，但它学的是"人群"，不是"你"。真正落到个人/项目、能持久、能撤销的，是写进可读文件的文本记忆——而不是隐式画像。**

你的猜测对了一半：

- 【事实】JetBrains 的 ML completion 是"按**其他用户**在类似情境下的选择"排序（`jetbrains.com/help/phpstorm/2025.3/auto-completing-code.html`），模型随 IDE 发布，不按人训练；用户侧只有一个按语言开关。
- 【事实】Cursor Tab 用 accept/reject 做**在线强化学习**，一天多次推 checkpoint，但它更新的是**全局策略**（`cursor.com/blog/tab-rl`）。
- 【事实】真正"记住你这个人"的编辑器机制小得可怜：VS Code 的 `editor.suggestSelection: "recentlyUsedByPrefix"`——记住"你输入 `co` 时上次选了 `console`"（`code.visualstudio.com/docs/editor/intellisense`）。这是本地、可关、可解释的启发式，不是 ML。
- 【事实】2025–2026 年新增的"记忆"产品（Copilot Memory / Cursor Memories / Windsurf Memories）全部选择**落成文本**，并且都提供查看/删除入口；没有一家把"用户偏好"藏进一个不可见的向量里。

**结论的修正**：不是"补全排序的个人化"，而是"**全局排序模型 + 项目级文本记忆**"两条腿。前者成熟但不可审计，后者不成熟但可审计。可审计的那条，才是产品信任的来源。

---

## 1. 对照表

| 产品 | 学什么 | 存储 | 注入时机 | 防学歪 | 用户可控性 | 我们能抄什么 |
|---|---|---|---|---|---|---|
| **VS Code（IntelliSense）** | 只学"前缀→上次选择"这一条；不学代码风格 | 编辑器本地状态（非仓库） | 下次同前缀弹出时预选 | 无置信度、无衰减；就是最近一次 | 设置可切 `first` / `recentlyUsed` / `recentlyUsedByPrefix` | "极小记忆 + 明确开关"比大而全的画像更可信 |
| **JetBrains（ML completion）** | 全体用户在某上下文下的选择（接受/取消、选中位置、延迟） | 训练侧在 JetBrains；推理在本地模型 | 每次补全弹窗排序（只重排 top 5，保留分组） | 全局模型 + 按语言开关；异常用户会话从训练集剔除 | 设置里按语言关闭；有"相关性上移/下移"图标 | 排序要"可解释"：用图标告诉用户为什么它动了 |
| **GitHub Copilot** | ① 补全 accept/reject（训练全局模型/过滤）② Memory：仓库事实 + 用户偏好 ③ 明确纠正/“记住这个” | ① 服务端 ② 仓库设置/个人设置，文本 + citations ③ `.github/copilot-instructions.md`、`~/.copilot-instructions.md` | Memory 按相关性检索进上下文；instructions 每轮注入；NES 用"最近编辑" | 密钥检测、prompt-injection token 拒收；引用回查当前分支；内部 up/down vote；90 天过期（可配 7–180） | 四层开关（个人 scope→组织策略→仓库→客户端）；可看/可删，**不可编辑单条** | "引用 + 过期 + 投票"三件套；以及"隐藏≠删除"的诚实说明 |
| **Cursor** | ① Tab：accept/reject → 在线 RL ② Memories：对话中推断的项目事实 ③ Rules：人手写 | Tab 在服务端；Memories 按项目（repo scope）；Rules 在 `.cursor/rules/*.mdc` | Memories 由 Agent 判断相关性拉取；Rules 按 always/glob/description/manual | Memories 据称"后台生成需批准"；可查看/删除；Rules 有 Team>Project>User 优先级 | Settings → Rules 查看/删除；Tab 可状态栏关闭 | 把"自动记忆"降级成"候选记忆，等人批" |
| **Windsurf** | Cascade 对话中"值得记"的上下文；用户显式"create a memory" | `~/.codeium/windsurf/memories/`（workspace 级、仅本机、不入库） | 模型自判"相关"时自动检索；无 glob/激活模式 | 可编辑标题/内容/标签、可搜索、可删；有 Auto-Generate 总开关；官方明确建议"长期知识写 Rule/AGENTS.md" | 面板可见、可改、可删、可关 | 存储路径和"仅本机"要写清楚；把"记忆 vs 规则"的分工写进产品文案 |
| **Cline** | 不学。Memory Bank 是**人写/模型按指令维护的文档** | 仓库内 `memory-bank/*.md`（6 个文件） | 任务开始时"读全部"（靠自定义指令），不是系统注入 | 无自动抽取、无置信度；靠人 review + git diff | 完全可见、可编辑、可提交/回滚 | 最便宜的可信方案：把记忆做成仓库文件，让 PR 审它 |
| **Roo Code** | 同 Cline，且是**社区模板**（`memory-bank/` 五文件 + `.roo/rules*`） | 仓库内 markdown | 新会话读文件；"update memory bank" 手动触发 | 靠规则约束与人工更新；无自动去重/衰减 | 完全可见、可编辑 | 同上；但别把"社区模板"当产品能力宣传 |
| **Aider** | 不学。Repo map 是确定性索引；lint/test 失败只在**本轮**回灌 | Repo map 每次重算；chat 历史不持久 | 每轮请求都带 repo map（默认 1k tokens，动态伸缩） | 无学习，因此无"学歪"；map 可 `/map` 打印人工审计 | 完全可控：`--map-tokens`、`.aiderignore`、`/read` | "可打印的上下文"本身就是信任：用户能看见模型看到了什么 |
| **Cody / Continue** | 不从 accept/reject 学排序；Cody 用关键词/embedding/code graph，Continue 用 LSP/imports/最近文件 | 索引在服务端或本地；无"偏好库" | 每次请求检索后拼 prompt | 无学习闭环；Cody 用 context filters 做准入控制 | Cody 可排除仓库；Continue 可配 provider/`useRecentlyEdited` | 上下文选择要可配置、可排除；别把"检索"包装成"学习" |

三条表外共性（都是事实）：

1. **没有任何产品把"学到的偏好"存成不可读的二进制然后让用户签收。** 能持久的，最后都落成 markdown/JSON/设置项。
2. **没有任何产品承诺"学了就一定照做"。** Copilot 文档明说 memory 是判断而非保证，重要规则请写 custom instructions。
3. **"关闭"普遍是多层收敛，不是单个 switch。** 关掉一层经常不等于全关（Copilot 的 user/repo 两类记忆就是独立开关）。

---

## 2. 逐个体检（五个问题）

### 2.1 VS Code / JetBrains

**① 学什么**

- VS Code IntelliSense：【事实】不学代码风格。唯一与"历史"有关的是 `editor.suggestSelection` 的三个值：`first`（默认）、`recentlyUsed`、`recentlyUsedByPrefix`。后者"记住某个前缀上次选了哪一项"，例如 `co → console`、`con → const`（`code.visualstudio.com/docs/editor/intellisense`）。
- JetBrains：【事实】ML ranking 的输入是补全调用时的**可枚举/数值特征**：是否自动弹出、缩进层级、前缀长度、光标处语法树父类型、是否在行中、是否在点后、候选项长度、匹配位置、是否标准库 token、用户多久后行动、选中了第几项。**不采集源码**（`blog.jetbrains.com/?p=159185/`）。
- 【事实】JetBrains 的排序是"其他用户在相似情境下的选择"，ML 只重排，不新增候选；只对 heuristic 分数高的 top 5 重排，其余保持原有分组（同前 blog）。

**② 怎么学**

- VS Code：即时、本地、无模型。选一次就记住，下次预选。
- JetBrains：离线批量训练 + 随版本发布的全局模型；不是"你这台机器在线学习"。官方 FAQ 的说法是"数据不在任何地方暴露，在本地收集"（`jetbrains.com/help/phpstorm/2025.3/auto-completing-code.html`），与 2019–2021 年博客描述的"匿名上报"存在代际差异；【推测】现在指推理在本地、特征不上传，但官方没有一份逐条对齐的说明。

**③ 存哪 + 谁读 + 何时注入**

- VS Code 的选择记忆在编辑器本地状态里，不进仓库；只在补全弹窗出现时起作用。
- JetBrains 模型随 IDE；本地历史是另一套东西：二进制文件放在系统目录 `LocalHistory/` 下（Windows 为 `%LOCALAPPDATA%\JetBrains\<product><version>`），默认保留 **5 个工作日**，装新版本 IDE 会清空，保留期设 `0` 可完全关闭（`jetbrains.com/help/idea/local-history.html`）。
- VS Code Local History：保存时生成整文件快照，默认每文件 50 条、单文件 256 KB、10 秒合并窗口，可 `workbench.localHistory.enabled` 关闭；官方只说明"存在本地/远程 user data folder，Web 端进 IndexedDB"，**没有公布 `entries.json` 这类文件格式**（`code.visualstudio.com/updates/v1_66`、`code.visualstudio.com/docs/editing/userinterface`）。社区观测到的路径是 `%APPDATA%\Code\User\History\<hash>\`，属实现细节，非官方契约。
- 【事实】Local History **不是学习**：它是"自动版本控制"，没有抽取、没有泛化、不跨文件推理。把它当"记忆"是误读。

**④ 怎么防学歪**

- VS Code：无置信度、无衰减；只有"最近一次"覆盖。风险低是因为作用面极小（只影响预选，不改候选集）。
- JetBrains：全局模型 + 从训练集剔除"按字母排序"的用户会话（blog ep4）；没有用户级纠错入口，只能整体关。
- 两者都**没有**"这条经验错了，撤掉"的粒度——因为压根没有"条"。

**⑤ 用户可见性**

- VS Code：可见（设置项语义明确），可关（改 `suggestSelection`）。
- JetBrains：可关（按语言），并可用"上移/下移"图标解释排序变化；但看不到"模型从谁身上学的"。
- 【事实】关闭是否真关：JetBrains 文档只承诺开关控制"是否用 ML 排序"；没有独立文档说明关闭后是否仍上报特征。**这一点查不到。**

**对 Kbgai 的差异**：我们的 `statement` 是"一条可读的判断"，比 `recentlyUsedByPrefix` 表达力强，但比它多了"可能错且会持续生效"的风险。它用"作用面极小 + 即时覆盖"换可信；我们用"硬命中 + 最多 1 条"换可信，方向一致。

---

### 2.2 GitHub Copilot

**① 学什么**

- 【事实】补全侧：accept/reject 是训练/过滤信号。Cursor 的博客转述了 2022 年 Copilot 的 contextual filter：11 个特征（语言、上一条是否被接受、光标前字符等），分数低于 15% 就不显示（`cursor.com/blog/tab-rl`，属二手转述）。
- 【事实】Memory 侧：两类——
  - **仓库级事实**：编码约定、架构决策、构建/测试命令、项目规则；有 write 权限才可创建，仓库内共享。
  - **用户级偏好**：你明确说过的、非敏感的工作方式、审批要求、跨项目工作流。
  - 还有"NES 用你最近的编辑"——这是**会话内**上下文，不是持久学习（`code.visualstudio.com/docs/copilot/ai-powered-suggestions`）。
- 【事实】Memory 的归类不看"谁说的"，看"适用于谁"；意图不明时 Copilot 会追问 scope（`docs.github.com/en/copilot/concepts/agents/copilot-memory`）。

**② 怎么学**

- 隐式为主：`"Copilot creates memories itself, as a byproduct of working with you."`
- 用户在 Copilot app / CLI 交互会话里会被请求批准存储；cloud agent 与 code review **不请求批准**。
- 不能直接新建/编辑单条记忆；只能通过"明确陈述 / 明确要求记住 / CLI 批量导入"影响它。
- 明确不学：短命/任务型指令、敏感个人信息、机密；检测到 secret 直接拒收；检测到 model-control token 也拒收（防 prompt injection）。

**③ 存哪 + 谁读 + 何时注入**

- 仓库事实属于仓库，仓库设置里看/删；不跨仓库池化，fork 不复制。用户偏好在个人设置，`github.com/settings/copilot/memory`。
- 【事实】检索时"当 Copilot 发现某条仓库事实与当前工作相关，会用该事实的 citations 对照当前分支，判断是否仍然准确"。
- 【事实】不是所有记忆都进 prompt；被 upvote 的更可能被发送。
- 计费：进上下文的记忆计入 token / AI credits。

**④ 怎么防学歪**

这是全篇最完整的一套，值得逐条抄：

1. **入库过滤**：secret、model-control token、敏感信息、短命指令。
2. **引用**：仓库事实引用支撑它的代码；用户偏好引用你的原话。
3. **使用前校验**：citation 对当前分支回查，而不是默认信任。
4. **内部投票**：能验证就 upvote，发现错误/过时 downvote；downvote 过阈值后不再使用，**过一段时间删除**。
5. **过期**：默认 90 天；只有 upvote 会重置，**单纯检索不重置**；downvote 不延长。仓库事实可配 7–180 天，组织与企业冲突取小值。
6. **诚实边界**：文档明说"这是判断不是保证"，重要规则用 custom instructions；"从列表消失 ≠ 已删除"；已关闭未合并 PR 也可能产生事实。

**⑤ 用户可见性**

- 可看：仓库设置 / 个人 Memory 页；列表按"有用性"排序（投票效果可见，票数不可见）。
- 可删：有权限即可删；Business/Enterprise 管理员可导出/删除成员的用户级偏好。
- **不可编辑单条**——这是它和 Windsurf 最大的差别。
- 可关：个人四选一（user / repo / both / none）→ 组织策略 → 仓库设置 → 客户端本地开关（VS Code 默认**关**，CLI 交互默认开、程序化默认关）。关一类不等于关另一类。
- 【事实】2026-04-24 起，Free/Pro/Pro+/Max 的交互数据可能用于训练，可在个人设置关闭；Business/Enterprise 合同排除。这一条与"记忆"无关，但决定了"你的行为信号最终去哪"。

**对 Kbgai 的差异**：它在"存哪"上用**两类所有权**（仓库/个人）解决了我们"按工作区一个 JSONL"没回答的问题——同一条经验，是"这个仓库的坑"还是"这个人的偏好"？我们的 `trigger_paths` 天然偏仓库，但没有 `scope` 字段。它在"防学歪"上有使用前校验与过期重置规则；我们只有 `expires_after_days`，没有"检索本身不续命、只有再次确认才续命"的显式规则。它在"可控"上不可编辑；我们可以直接改 JSONL——**可编辑是我们的优势，但没有 UI 等于没有**。

---

### 2.3 Cursor / Windsurf

**Cursor**

① **学什么**：两层。Tab 学"何时该建议"——accept/reject 作为 reward，目标是接受率 ≥25% 才显示，新 checkpoint 一天多次（`cursor.com/blog/tab-rl`）；Memories 学"对话里的事实"，按项目、按个人存（`cursor.com/changelog/1-0`）。Rules 不是学来的，是人写的。

② **怎么学**：Tab 是全局在线 RL（不针对个人）；Memories 由对话自动生成，需在 Settings → Rules 启用（beta）。官方没有说明是否需要逐条批准——该说法只见第三方。

③ **存哪 + 谁读 + 何时注入**：Memories 按项目存、从 Settings 管理；Rules 在 `.cursor/rules/*.mdc`，可版本化，对话开始时进上下文；优先级 Team > Project > User，且**只作用于 Agent chat，不影响 Tab / Inline Edit / Bugbot**（`cursor.com/docs/rules`）。

④ **怎么防学歪**：官方可见的只有"按项目隔离 + 可从 Settings 删除"；没有公开的去重、置信度、衰减、冲突消解。官方文档反而强调 Rules 才是可靠层："Large language models don't retain memory between completions."（同 docs）

⑤ **用户可见性**：可见（Settings → Rules）、可删；是否可编辑单条内容未见官方说明；Memories 是 beta，隐私模式下的行为只见第三方说法。

**Windsurf**

① **学什么**：Cascade 在对话中判断"值得记"的上下文（纠错、发现的坑、你声明的偏好），也支持你直接说"create a Memory of …"（`docs.windsurf.com/fr/windsurf/cascade/memories`）。

② **怎么学**：隐式 + 显式并存；自动生成是模型判断，不是规则抽取。新增 **Auto-Generate Memories** 开关，关掉后只在用户明确要求时创建（`windsurf.com/changelog`）。

③ **存哪 + 谁读 + 何时注入**：workspace 级，存 `~/.codeium/windsurf/memories/`，**仅本机、不入仓库、不跨 workspace**；检索靠"模型认为相关"，没有 glob/激活模式；自动生成与使用不消耗 credits（同 docs）。Tab 会看命令历史、剪贴板、Cascade 动作，但那是**会话上下文**，不是持久学习（`windsurf.com` 首页文案）。

④ **怎么防学歪**：可编辑标题/内容/标签、可搜索、可删；官方明确劝退"要可靠复用就写 Rule 或 AGENTS.md，别依赖自动 Memories"；【查不到】容量上限、衰减、冲突消解、"相关"的判定逻辑。

⑤ **用户可见性**：面板可见、可改、可删、可关；"仅本机"写得很清楚。但 Auto-Generate 是**生成开关**，不是**使用开关**——关掉后已有记忆是否还注入，文档没说。


**对 Kbgai 的差异**：Windsurf 的存储位置与"仅本机"写得极清楚，用户能直接 `ls` 到文件；我们的 JSONL 也在本地，但产品里没有"记忆"面板。它的 Auto-Generate 开关是**生成开关**，不是**使用开关**——关掉后已有记忆是否还注入？文档没说。这正是"关闭是不是真的关掉"的灰区，我们如果做开关，必须把"停止生成"和"停止注入"分成两个开关，并在 UI 上写明。

---

### 2.4 Cline / Roo Code

**① 学什么**：不学。没有接受/拒绝信号、没有 diff 挖掘、没有自动抽取。

**② 怎么学**：靠**自定义指令**要求模型在特定时机维护文档。Cline 官方列出的更新时机是：发现新项目模式、实现重大变更、用户说 `update memory bank`、上下文需要澄清（`docs.cline.bot/prompting/cline-memory-bank`）。注意：这是**提示词里的时机**，不是系统事件钩子；是否真的写，取决于模型这一轮是否遵守。

**③ 存哪 + 谁读 + 何时注入**：
- `memory-bank/projectbrief.md`、`productContext.md`、`activeContext.md`、`systemPatterns.md`、`techContext.md`、`progress.md`；都是仓库内普通 markdown，"both you and Cline can access"。
- 注入方式是"每次任务开始读全部文件"，靠 `.clinerules/memory-bank.md` 里的硬指令（"I MUST read ALL memory bank files at the start of EVERY task"）。
- 【事实】Roo Code 的 Memory Bank 是**社区项目**（`github.com/GreatScottyMac/roo-code-memory-bank`），文件集是五件套 + `decisionLog.md`，靠 `.roo/rules*` 约束；不是 Roo 内置功能。Roo 本身提供的是 rules 加载与模式系统。
- 【未证实】有中文文章称 Roo Code 扩展于 2026-05-15 关闭并迁移到 Kilo Code；未在官方渠道核实，引用需谨慎。

**④ 怎么防学歪**：没有机制，只有流程——人 review、git diff、回滚。优点是**天然可审计**；缺点是**必然腐化**：`activeContext.md` 越写越长、旧结论不失效、token 成本每轮都付。

**⑤ 用户可见性**：100% 可见、可编辑、可提交。没有"关闭"概念——不想要就删文件/删指令。

**对 Kbgai 的差异**：Cline 把"记忆"放在**仓库**里，因此天然获得 code review、blame、revert；我们的 JSONL 在 `services/api/data/experience/`，在 git 之外，**用户看不见也审不了**。它在"更新时机"上是模型自觉，我们在"准入"上是规则判定——我们更硬，但也更容易漏掉"模型觉得重要、规则没覆盖"的经验。它每轮读全部，我们最多注入 1 条——我们的 token 成本低，但"想起"的召回率也低。

---

### 2.5 Aider

**① 学什么**：不学。没有跨会话经验库。

**② 怎么学**：
- Repo map：tree-sitter 抽符号（定义/引用），建文件依赖图，用 PageRank 类算法排序，按 token 预算裁剪；默认 `--map-tokens 1k`，chat 里没加文件时自动放大（`aider.chat/docs/repomap.html`）。
- 编辑失败/ lint 失败：`--auto-lint` 默认对改过的文件跑 linter；`--auto-test` / `--test-cmd` 在每次编辑后跑测试；非零退出就把输出回灌给模型让它修（`aider.chat/docs/usage/lint-test.html`）。**只在本轮**，不写任何"下次别这么改"的记录。
- `CONVENTIONS.md`：【事实】要用 `/read` 或 `read:` 配置**手动**加载，加载后是 read-only、可被 prompt cache 缓存（`aider.chat/docs/usage/conventions.html`）。没有自动发现。

**③ 存哪 + 谁读 + 何时注入**：repo map 每次重算，不落盘；conventions 在仓库，但只有被 `read` 时才进上下文；lint 结果在当轮对话里。

**④ 怎么防学歪**：没有学习，所以没有"学歪"。它的防御是**确定性**：同样的仓库状态生成同样的 map；`/map` 可打印，用户能逐行审计；`.aiderignore` / `--subtree-only` 控制范围。

**⑤ 用户可见性**：极高。map 可打印、conventions 是普通文件、lint 命令是用户配的。

**对 Kbgai 的差异**：Aider 用"**可打印的上下文**"替代"记忆"，把信任建立在"你能看见模型看到了什么"。我们的 `evidence` 有 pointer，但没有"这次为什么想起这条"的可视化；Aider 的 `/map` 是我们可以直接借的交互——一个 `kbgai experience why <task>` 输出"命中路径/签名 + 证据指针 + 置信度"，比在 JSONL 里翻强得多。

---

### 2.6 Sourcegraph Cody / Continue

**Cody**

① **学什么**：不学用户行为。它做的是**上下文检索**：关键词（BM25 类）、embeddings、Sourcegraph search、code graph；聊天侧把各 provider 的片段做全局排序后取前 N（`sourcegraph.com/blog/how-cody-understands-your-codebase`、`sourcegraph.com/docs/cody/core-concepts/context`）。

② **怎么学**：没有学习闭环。检索排序是静态算法 + 人工调参；【事实】没有公开的"accept/reject 反向影响排序"机制。自动补全为低延迟只用本地上下文：当前文件、其他打开的 tab、**最近关闭的 tab**，并用 tree-sitter 判意图（同 blog）。

③ **存哪 + 谁读 + 何时注入**：索引在 Sourcegraph 实例（Enterprise）或本地（Free/Pro 单仓）；每次请求现检索、现拼 prompt，不持久化"偏好"。

④ **怎么防学歪**：准入控制是**管理员级**的 `cody.contextFilters`（include/exclude 正则），决定哪些仓库能进上下文（`sourcegraph.com/docs/cody/capabilities/ignore-context`）；没有置信度/衰减，因为不学习。

⑤ **用户可见性**：上下文档位因客户端而异；管理员可审计过滤规则。是否在服务端用接受率做隐式评估，官方未披露。

**Continue**

① **学什么**：不学。自动补全的上下文是：文件前后缀、LSP 定义、匹配 import 的符号、**最近打开/编辑的文件片段**（`docs.continue.dev/ide-extensions/autocomplete/context-selection`）。

② **怎么学**：即时、会话内、无模型更新。"最近编辑"是工作区最近性，不是学到的偏好；`useRecentlyEdited` / `useRecentlyOpened` 可关。

③ **存哪 + 谁读 + 何时注入**：无偏好存储；每次补全/请求按 provider 现取。规则/提示文件是静态的，官方与社区都明确"rules 不会自我更新"；要跨会话记忆得接 MCP 或外部存储。

④ **怎么防学歪**：没有可学歪的东西；风险是"检索到不相关片段"，靠 provider 配置和手动 `@` 控制。

⑤ **用户可见性**：配置可见、可关；`@Codebase` 是检索不是学习。没有"它学了我什么"的页面，因为没有学。

**对 Kbgai 的差异**：Cody 的 context filters 是"**谁能进来**"的硬闸门，比我们的"路径前缀/报错签名硬命中"更像安全策略而非相关性策略。Continue 的"最近编辑"是**时间衰减**的天然实现——我们没有任何时间衰减，只有 `expires_after_days`。如果要抄一个低成本改动：检索排序里加"最近确认时间"作为同分时的 tie-breaker，而不是加语义检索。

---

## 3. 用户凭什么信任

先给结论：**目前只有"文本 + 面板 + 删除"这一层做到了；"解释为什么这次用了它"几乎没人做到。**

| 产品 | 可查看 | 可编辑 | 可删除 | 可解释"为什么这次用了它" |
|---|---|---|---|---|
| Copilot Memory | ✅ 仓库设置/个人页，按有用性排序 | ❌ 单条不可编辑 | ✅（含管理员代删） | ⚠️ 部分：citations 可查、References 列出用到哪个文件；不解释排序 |
| Cursor Memories | ✅ Settings → Rules | ⚠️ 未见官方说明可改内容 | ✅ | ❌ 未见 |
| Windsurf Memories | ✅ 面板，可搜索 | ✅ 标题/内容/标签 | ✅ | ❌ "模型认为相关"，无理由 |
| Cline / Roo | ✅ 就是仓库文件 | ✅ | ✅（删文件/git revert） | ⚠️ 只能靠人读指令链 |
| Aider | ✅ `/map` 可打印 | ✅ conventions 文件 | ✅ | ✅ 确定性：同状态同输出，可复现 |
| VS Code IntelliSense | ✅ 设置项 | ✅ | ✅ | ⚠️ 只有"最近用过"这一条规则，足以解释 |
| JetBrains ML | ⚠️ 只有上移/下移图标 | ❌ | ❌（只能整体关） | ⚠️ 图标表达"动了"，不表达"为什么" |
| Cody / Continue | ⚠️ 上下文片段可见性因客户端而异 | ✅ 配置 | ✅ 配置 | ❌ 无 |

三个真正做到的细节：

1. **Copilot 的"引用对分支回查"**：不是"我记住了"，而是"我记住的这句话，在当前分支还能找到支撑代码"。这是把"记忆"降级成"假设"，每次使用前验证。它的代价是：验证失败时仍然可能误用（文档自己承认）。
2. **Copilot 的"隐藏 ≠ 删除"**：被 downvote 的记忆会从列表隐藏但可能仍存储到过期。它没有假装"看不见就是没了"，而是明确告诉用户"要彻底不用，请显式删除"。这是诚实，也是产品责任的边界。
3. **Windsurf 的"仅本机 + 不入库"**：记忆是个人状态，不是团队配置。要共享就写 Rule/AGENTS.md。这个分工让"记忆"不需要经过 code review，也不会污染仓库；代价是换机器就没了。

三个只做到一半的：

- **Cursor**：有面板、有删除，但官方文档在 2026-11 已无法直接访问 Memories 细节；"需要批准"无法核实。产品能力存在，可验证性不足。
- **JetBrains**：可解释性只到"排序变了"，不到"因为你在类似上下文选过 X"。用户只能选择信或关。
- **Cline/Roo**：可见性满分，但"谁在什么时候写了什么"没有审计轨迹；`activeContext.md` 是覆盖写，历史只在 git 里。

**"关闭"是不是真的关掉？**

- Copilot：文档明确分类型、分 surface、分层；VS Code 默认关；CLI 的 `/memory off` 只影响该机交互会话，"不能覆盖个人 scope 或组织策略"。这是目前写得最清楚的。
- Windsurf：Auto-Generate 是"停止自动生成"，**不是"停止使用已有记忆"**；两者没有被文档区分。要确认只能看它是否还在注入（不可观测）。
- Cursor：Memories 是 beta、按项目；隐私模式与 Memories 的关系只见第三方说法。
- JetBrains：按语言关 ML 排序，语义清楚；但"关闭后是否仍上报特征"没有独立说明。
- **共性**：闭源产品只能通过开关文案和网络行为推断，"偷偷用"无法从文档证伪。**这是所有闭源产品的信任天花板，不是某一家的问题。**

**信任的层级**（我的判断，标【推测】）：

```
可打印的确定性上下文（Aider map）
  > 仓库里的文本 + git 历史（Cline/Roo/AGENTS.md）
  > 本地文本 + 面板 + 删除（Windsurf/Copilot Memory）
  > 隐式模型（Copilot 补全 / Cursor Tab / JetBrains ML）
```

越往下，越难解释、越难撤、越依赖厂商自律。产品若想让人信，应该尽量把"学到的东西"往上层挪，而不是把下层包装成"懂你"。

---

## 4. 查不到 / 没有公开实现

明确列出来，避免把营销当机制：

1. **Cursor Memories 的官方机制页**：`cursor.com/docs/context/memories` 重定向到 Rules；`cursor.com/help/customization/memories` 404。创建触发、批准流程、存储路径、检索算法均**无官方可引用来源**。第三方说的"后台生成需批准"未证实。
2. **Windsurf 的检索排序**：只说"模型判断相关"，没有阈值、没有 top-k、没有衰减、没有冲突消解。Auto-Generate 关闭后是否仍注入，未说明。
3. **Copilot Memory 的内部投票细节**：票数不显示、阈值不公开、downvote 后"过一段时间删除"的具体时长不公开；repo fact 的 citations 校验失败时是丢弃还是降权，未说明。
4. **JetBrains 当前 ML 模型的特征清单与训练数据范围**：2019–2021 博客给了特征样例（枚举/数值、无源码），但 2025–2026 文档只说"其他用户的选择""本地收集"，两代说法未对齐。模型是否按语言/IDE 版本分治，未公开。
5. **VS Code 是否有基于历史的选择排序**：只有 `recentlyUsed` / `recentlyUsedByPrefix` 这一条；没有证据表明 IntelliSense 会学习代码风格或跨会话偏好。Copilot 的排序在服务端，不可见。
6. **Cody / Continue 的反馈闭环**：没有任何官方文档说 accept/reject 会影响后续上下文排序。Cody 的排序是 BM25/embedding/code graph + 人工调参；Continue 是固定 provider 组合。
7. **Roo Code 的产品归属**：Memory Bank 是社区模板；扩展是否已停止维护/迁移 Kilo Code，未在官方渠道核实。
8. **"关闭后是否仍使用"**：所有闭源产品都没有可验证的承诺。只能靠设置文案、隐私政策、网络请求观测；本报告不做断言。

---

## 5. 和 Kbgai `experience.py` 的逐条差异

> 下表只写"它在 X 处用 Y 做了 Z，我们的差异是 W"，不写"值得借鉴"。

| 机制点 | 它怎么做 | 我们的现状 | 差异 / 风险 |
|---|---|---|---|
| **作用域** | Copilot 把记忆分"仓库事实 / 用户偏好"两类所有权，分别存、分别授权、分别删 | 一个 workspace 一个 JSONL；`trigger_paths` 偏仓库，但没有 `scope` 字段 | 同一条"这台机器没有 rm"到底是仓库坑还是个人环境偏好？现在只能靠 `statement` 文字表达，检索时无法按 scope 过滤 |
| **使用前校验** | Copilot 用 citations 对当前分支回查；判断"这条事实还成不成立" | `evidence` 有 task/signal/pointer，但检索时只看路径前缀/签名，不校验证据是否还在 | 代码改了、路径还在，旧经验仍会命中。这是"学歪"的主要入口 |
| **失效** | Copilot：downvote 过阈值→停用→过期删除；检索不续命，只有 upvote 续命 | `confidence` + `hits/misses` + `expires_after_days`；没有"检索不续命"的规则，也没有"被证伪即降级"的自动路径 | 一条经验只要被反复注入，就可能因 `hits` 增长而显得"有用"，但 `hits` 不等于"正确" |
| **冲突** | Copilot：新事实覆盖旧事实靠 vote + expiry；Windsurf：靠人编辑，冲突时行为不确定 | 同句去重涨置信度；同报错签名结论不同→旧条目退役 | 我们的冲突处理比 Windsurf 硬，但"同签名不同结论"可能误杀：两个不同根因可能产生同一句报错 |
| **注入** | Cline：每轮读全部 memory-bank；Aider：每轮带 repo map；Copilot：按相关性挑，不是全量 | 最多 1 条，路径前缀/签名硬命中 | 召回率低但精确；适合"坑"，不适合"项目常识"。Cline/Aider 的"全量/地图"解决的是另一类问题 |
| **可见性** | Windsurf/Copilot：产品内面板 + 删除；Cline：仓库文件 + git | JSONL 在 `services/api/data/experience/`，产品内无入口 | 用户无法在 UI 里看到"它学了我什么"，也无法一键删；可编辑性只对开发者成立 |
| **可解释** | JetBrains：排序变化用上移/下移图标；Aider：`/map` 打印 | `statement` 可读，但没有"这次为什么用它"的输出 | 注入 1 条时，用户不知道是哪条路径/签名命中的；排障只能翻日志 |
| **关闭** | Copilot：四层收敛、分类型；Windsurf：生成开关 | 无开关（按工作区删文件） | "关掉学习"和"关掉注入"没有区分；删 JSONL 是唯一手段，粒度太粗 |
| **写入者** | Copilot/Cursor/Windsurf：模型判断 + 用户批准（部分 surface 不批）；Cline：模型按指令写 | 规则式抽取，不调模型 | 我们不引入"模型反思污染"，但也漏掉"规则表达不了的经验"；这是有意的取舍，不是缺陷 |
| **存储形态** | 全部落文本（md/JSON/设置），没有一家用二进制黑盒存偏好 | JSONL，一行一条，可 diff | 形态是对的；缺的是"人可读的摘要视图"和"按条删除"的交互 |

三个具体可验证的差距（不涉及改代码，只是指出事实）：

1. 我们的 schema 已有 `hits` / `misses` / `expires_after_days`，但**没有字段记录"最后一次被证据校验的时间"**；Copilot 的 expiry 只在 upvote 时重置，我们无法区分"被检索"与"被确认"。
2. 我们的 `evidence.pointer` 是路径，不是**可回查的代码位置 + 当时的分支/commit**；Copilot 的 citation 能对"当前分支"判断，我们只能对"路径是否存在"判断。
3. 我们的注入是"最多 1 条"，但没有**"为什么是这条"的输出**；Aider 的 `/map` 和 JetBrains 的排序图标说明：可解释性不需要复杂，只需要把决策依据摆出来。

---

## 6. 如果只抄三件事（按可落地性排序）

1. **把"证据"变成"可回查的引用"，并在注入前校验一次。**
   Copilot 在 `docs.github.com/en/copilot/concepts/agents/copilot-memory` 里写得很直白：用到仓库事实时，拿 citations 对当前分支判断是否仍准确。我们的 `evidence.pointer` 已经是这个结构，差的是"用前校验 + 校验失败就降级/退役"。
2. **把"过期"从"天数"改成"事件"。**
   Copilot 的规则是：创建时起算，只有 upvote 重置，检索不重置。我们的 `expires_after_days=90` 是纯时间；如果一条经验被反复命中但从未被新证据确认，它应该**更快**过期，而不是因为 `hits` 高而显得可靠。
3. **给"这条经验"一个用户能看见的出口。**
   Windsurf 的做法最便宜：一个面板，列出 workspace 的记忆，能改标题/内容/标签，能删；Copilot 的做法更完整但不可编辑。我们已经有 JSONL，最小可用形态是"只读列表 + 删除 + 显示命中依据（路径/签名）+ 显示置信度与最后确认时间"。没有这个出口，"自学习"对用户就是黑盒。

---

## 附：来源清单

**官方文档 / 发布说明**

- VS Code IntelliSense（`editor.suggestSelection`、`localityBonus`）：https://code.visualstudio.com/docs/editor/intellisense
- VS Code 1.66 Local History 发布说明（存储位置、5 个设置项）：https://code.visualstudio.com/updates/v1_66
- VS Code 界面文档（Local History 设置项、Timeline）：https://code.visualstudio.com/docs/editing/userinterface
- VS Code Copilot 行内建议（ghost text、NES 用最近编辑）：https://code.visualstudio.com/docs/copilot/ai-powered-suggestions
- JetBrains Local History（二进制存储、5 个工作日、`localHistory.daysToKeep`、装新版清空）：https://www.jetbrains.com/help/idea/local-history.html
- JetBrains 代码补全 / ML 排序（按语言开关、"其他用户的选择"、"data is collected locally"）：https://www.jetbrains.com/help/phpstorm/2025.3/auto-completing-code.html
- JetBrains 博客：数据采集（只记枚举/数值、不记源码）：https://blog.jetbrains.com/?p=159185/
- JetBrains 博客：模型训练（top 5 重排、剔除字母排序会话）：https://blog.jetbrains.com/?p=174298/
- Copilot Memory（两类记忆、citations、投票、过期、权限矩阵、不可编辑）：https://docs.github.com/en/copilot/concepts/agents/copilot-memory
- Copilot 自定义指令（仓库/路径/组织/个人优先级）：https://docs.github.com/en/copilot/concepts/response-customization
- Visual Studio Copilot 上下文（Memories 检测偏好、保存到 `.editorconfig`/`CONTRIBUTING.md`/`README.md`、user/repo 级）：https://learn.microsoft.com/en-us/visualstudio/ide/copilot-chat-context
- Copilot 个人策略（2026-04-24 起 Free/Pro 数据可能用于训练、可 opt-out）：https://docs.github.com/en/copilot/how-tos/manage-your-account/manage-policies
- Cursor 1.0 发布说明（Memories beta、按项目、Settings → Rules）：https://cursor.com/changelog/1-0
- Cursor Rules（规则优先级、只作用于 Agent chat、"LLM 不在补全间保留记忆"）：https://cursor.com/docs/rules
- Cursor Tab 在线 RL（accept/reject → reward、全局策略）：https://cursor.com/blog/tab-rl
- Windsurf Memories（workspace 级、`~/.codeium/windsurf/memories/`、模型判断相关、建议写 Rule/AGENTS.md）：https://docs.windsurf.com/fr/windsurf/cascade/memories
- Windsurf 更新日志（Memories 标签页、可编辑标题/内容/标签、Auto-Generate 开关）：https://windsurf.com/changelog
- Cline Memory Bank（6 文件、手动/模型触发更新、普通 markdown）：https://docs.cline.bot/prompting/cline-memory-bank
- Aider Repo map（tree-sitter、PageRank、`--map-tokens` 默认 1k）：https://aider.chat/docs/repomap.html
- Aider CONVENTIONS（`/read` 只读加载、`read:` 配置）：https://aider.chat/docs/usage/conventions.html
- Aider lint/test（自动 lint、`--auto-test`、非零退出回灌修复）：https://aider.chat/docs/usage/lint-test.html
- Cody 上下文（关键词/embedding/SG search/code graph、全局排序）：https://sourcegraph.com/docs/cody/core-concepts/context
- Cody 上下文过滤（include/exclude 正则）：https://sourcegraph.com/docs/cody/capabilities/ignore-context
- Continue 自动补全上下文（前后缀、LSP、imports、最近打开/编辑）：https://docs.continue.dev/ide-extensions/autocomplete/context-selection

**社区/第三方（仅用于标注"未证实"或补充产品行为，不作为机制证据）**

- Roo Code Memory Bank（社区模板，五文件 + decisionLog）：https://github.com/GreatScottyMac/roo-code-memory-bank
- Cursor Memories 批准流程的第三方汇总：https://www.positioniseverything.net/cursor-rules-vs-memories-what-persists-and-when-to-use-each
- Windsurf Memories 生命周期指南（含"冲突记忆导致不确定行为"）：https://localskills.sh/blog/windsurf-memories-guide

**本仓库**

- 经验存储样例：`services/api/data/experience/kbgai-f1a6db42.jsonl`
- 现状体检：`docs/self-learning-diagnosis.md`
- 开源实现盘点：`docs/self-learning-reference.md`
