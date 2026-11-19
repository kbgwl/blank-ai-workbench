# 外网参考总报告（2026-11-19）

三份详报（`opencode-reference.md` / `deepseek-harness-reference.md` / `ui-reference.md`，共 1,268 行）
的合并版。这份只管一件事：**哪些能直接开工**。细节、源码行号、来源 URL 都在那三份里。

---

## 0. 一句话

**技术上抄三件：工具超时转后台、压缩结果当"历史根"、写类工具只回执。
界面上改一件：把"一步一张卡"改成"一次连续工具调用 = 一行"。**

界面不好看的真因不是配色，是**我们把 12 次工具调用渲染成 12 张卡**。两家都不这么干。

---

## 1. 先纠正两个错的前提（包括我自己的）

| 我之前说的 | 实际是 | 依据 |
|---|---|---|
| 「我们没有成本上限」 | **有 token 闸**（`token_budget` + 子代理合计 + 耗尽后只准总结），`graph.py:5549-5645` | 源码核对 |
| | 缺的是**金额口径**（$），而两家 agent 都没有金额闸——这块必须自建 | DSH 明确写 "No built-in turn budget"；opencode 同样只有 token |
| 「opencode 的 TUI 是 Go 写的」 | 现在 `packages/tui` 是 **TypeScript + Bun**，早期 0.x 才是 Go | `anomalyco/opencode` 源码 |
| 「DeepSeek 没有 coding 产品」 | **有。`deepseek-ai/deepseek-harness`（dsh）**，MIT，2026-08 首发、10-01 发 0.2 带 macOS/Windows 桌面端，`npx @deepseek-ai/dsh web` | GitHub 仓库核实 |

第二条特别值得看一眼：**DSH 的形态和我们几乎一样**（桌面 + Web、会话 + 轨迹双视图、插件化），
它的问题清单大概就是我们明天会遇到的清单。

---

## 2. 界面：为什么他们的好看

### 2.1 三个可对照的实拍

| 图 | 看什么 |
|---|---|
| `_shots/external/opencode-desktop-sessions.png` | 左侧会话列表是**纯文字行**、无卡片无边框；主区单栏；**`Used 1 Read` 是一条 24px 的淡灰字行**，不是卡片 |
| `_shots/external/deepseek-harness-chat.png` | 顶部 **Chat / Trajectory 双 tab**；Chat 里只有人话，`Thought for a while ›` 一行折叠；助手回答是**纯文本段落**，没有边框 |
| `_shots/external/deepseek-harness-trace.png` | 同一个任务的 Trajectory 视图：11px 等宽、24px 行高，全量事件平铺 |

### 2.2 两个可以直接照搬的结构

**A. 双密度（抄 DSH）**

同一个任务两种呈现，靠 tab 切换，不是靠排版取舍：

- **Chat**：13px / 行高 1.7，只讲人话。工具调用压成一行（`Used 1 Read`），思考压成一行（`Thought for a while ›`）。
- **Trajectory**：11px 等宽 / 24px 行高，全量事件平铺，给要看细节的人。
- **右栏 360px**：只放产物、diff、预览。会话列表不占横向空间（他们在左，但只用文字行，不做卡片）。

**B. 一步一张卡 → 一次连续调用一行（抄 opencode）**

现在的"每步一张带边框的卡"是视觉噪音的来源。改法是：**连续的工具调用合并成一个活动行组**，
展开才看到逐个调用。判定"连续"用现成的 `tool_call_id` 与相邻 step 即可，不需要新数据。

### 2.3 可以直接改的五条（都有具体数值）

1. 工具调用行高 24px、11–12px 淡色字，图标 + 动词 + 目标（`Used 1 Read` 格式）
2. 助手正文 13px / 行高 1.65，段落之间不留边框线
3. 用户消息右对齐气泡，助手消息不做气泡（现在两边都是气泡，层级没差）
4. 会话在 Chat / Trajectory 之间用 tab 切，不做"详情页"
5. 右栏固定 360px 只放产物与 diff，不再塞时间线

---

## 3. 技术：按"能立刻做 / 要设计 / 不做"分三档

### A. 能立刻做（都有现成落点）

**A1. 工具超时转后台**——⚠️ 我们今天已做（`558f761`），但两家都做得更彻底

- DSH：`timeoutMs` 到期转 background job，模型用 `job_output` / `job_list` / `job_kill` 继续收。
- opencode：超时**不杀**，转后台并通知，工具结果里明令"不要 poll"。
- 我们：`run_tests` 已转后台；**`run_shell` 还没**。补齐即可，机制完全一样。

**A2. 压缩结果 = 可回放的历史根**（opencode）

`messageEntries()` 先找最近一条 `status=completed` 的 compaction，**从它之后才开始读**；
旧消息根本不进请求。我们的 `context.compacted` 事件只是"记账"，读取侧（`threads.py:history_for`）
仍然靠"最近 N 轮"，压缩边界不参与读取。**改读取侧，不让它当摆设。**

**A3. 写类工具只回执**（opencode）

`Wrote file successfully: <path>` / `Edited <path> (N replacements)`。
我们的 `editing.py` 对新文件返回**全文 diff**——这正是"模型写过的文件在历史里反复重发"的直接来源。
回执只需要"哪个文件、变了多少"。

**A4. 摘要切点必须落在 user 消息上**（opencode）

现在压缩切点可能切开 assistant 的 tool call 与结果。加一条硬约束：`start` 必须是 user 消息索引，
落在别处就往前回退。

### B. 要设计（收益大，但要动结构）

**B1. 系统提示：壳 + 增量**（两家都做）

- opencode：模板 756 字符、全开约 1.47k；日期/环境/AGENTS.md/技能/MCP 都走"基线 + 哈希 diff + epoch"。
- DSH：系统提示是历史里的 `system/message` 节点，**默认原地替换**，声明 `systemPromptUpdate: 'in-history'` 时改为追加。
- 我们：10,133 字符，每轮全量组装。

两家的差别刚好是我们要选的取舍：**原地替换省 token 但废缓存，追加保缓存但费 token**。
DSH 把它做成显式配置，这个设计比"偷偷选一个"好——值得照抄的是"把它变成配置项"。

**B2. 金额闸（自建）**

两家都没有。我们已有 token 闸，补 $ 只需在成本口径上加一层：
`spent_tokens × 单价`，超限时走已有的"收尾区 / 耗尽"两段语义。
**按价目表配，不要按 token 估。** DSH 的 token meter 只做展示，明确不是计费输入。

**B3. 上下文三级治理：先确定性裁剪 → 再摘要 → 大结果落盘**（DSH）

阈值有公开默认值：`8192 / 4096 / 1024` 字符、`0.8 / 0.16 / 65536`。
顺序固定为"先便宜后昂贵"（prune 先于 summarize）。我们现在是"按字符上限截断"，没有分级。

**B4. 大输出双上限：行数 + 字节**（opencode）

2000 行 / 50 KiB 取更早的切点。我们只有字符上限——一行 10 万字符的 JSON 切得太晚，
2000 行正常日志切得太早。

### C. 不做 / 暂缓

- **后台预压缩**（opencode）：会产生"永远用不到的摘要"，在有金额闸之前是纯浪费。
- **模型判定式防打转**（opencode 的 goal 插件让 reviewer 出 done/continue/blocked）：
  我们的确定性断路器（同签名失败 2 次、同类 3 次）更可靠，不替换。
- **子代理编排 / swarm**：两家都在收着用，我们单会话的打转还没治完。
- **把 agent loop 做成插件**（DSH 的 "everything is a plugin"）：架构级重构，不是升级。

---

## 4. 建议的升级顺序

**第一批（今天就能做完，都是小改）**
1. `run_shell` 超时也转后台（对齐 `run_tests`）
2. 写类工具只回执（A3）——直接减少历史膨胀
3. 摘要切点落在 user 消息（A4）

**第二批（界面，1 天）**
4. 工具调用行化（一步一张卡 → 连续调用一行）+ 双密度 tab
5. 右栏收窄到 360px 只放产物

**第三批（结构性，需要对照实验）**
6. 压缩结果当历史根（A2）
7. 系统提示壳 + 增量（B1）——这条要和"删提示"一起做，不然省下的又被增量吃回去
8. 金额闸（B2）

---

## 5. 三份详报索引

| 文件 | 行数 | 里面有什么 |
|---|---|---|
| `docs/opencode-reference.md` | 336 | 7 类逐条对照表（loop / 上下文 / 工具 / 权限 / 子代理 / 会话 / provider），39 个源码文件引用，agent loop 文字时序图，明确列出"不该抄的部分" |
| `docs/deepseek-harness-reference.md` | 505 | DSH 架构、四种运行模式、turn/step 阶段、三级上下文治理的公开默认值、**明确标出"抄不到的部分"**（无预算闸、无 SWE-bench runner、部分数值未公开） |
| `docs/ui-reference.md` | 427 | 18 张实拍截图（含来源 URL）、三形态信息架构、双密度具体数值、可直接照搬的 5 条 |

三份都区分了【事实】/【推测】，查不到的明确写"未找到"，没有编。
