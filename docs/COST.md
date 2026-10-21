# 为什么「只烧 token，没做出东西」

这份文档记的是一次真实事故的实测数据与根因，不是设计愿景。数据来自本机
`%APPDATA%/blank-ai-workbench-desktop/blank.db`（0.8.1 装机后的真实用量）。

## 实测：6 个任务，900 万 prompt token

| 指标 | 数值 |
|---|---|
| 模型调用 | 258 次 |
| prompt token 合计 | 9,045,684 |
| 其中命中缓存 | 8,574,976（94.8%） |
| completion token | 88,048 |
| 单次最大 prompt | 114,098 |
| 工具调用 | 200 次 |

单个任务最多 116 次工具调用，产生 581 条事件，最后状态是 `failed`。

## 四个根因

### 1. 判意图的输入被界面自己的前缀污染

界面在用户原话前面拼了两行固定前缀：

```
执行规则：…只输出已执行动作、结果、阻塞和下一步。
你处于开发场景。优先读取现有代码，实际修改文件并运行相关测试；…
```

`guess_intent()` 拿整段文本做匹配，而这两行里全是「执行」「修改」「测试」。
于是**每一句话都命中动作词**：「你现在的工作目录是哪里」被判 `act`，进工具循环。
模型也不免疫——提示词里写着「系统预判：这句话需要动工作区，intent 必须是 act」。

修法分两步：

1. `unwrap_goal()` 先把前缀剥掉，只用用户原话判；同时把原话明写给模型，
   告诉它「前缀是界面附带的，不是这次的要求」。
2. **把场景模式整个删掉**。前缀不是配错了，是这个设计本身在逼用户在发送前
   替自己分类；而分类该由 Agent 看着这句话自己判。现在发出去的就是原话，
   一个字节都不加（`web/src/components/model.tsx`）。

### 2. 征求意见被当成派活

即使剥掉前缀，「你觉得界面有什么可以**优化**的」仍然命中 `_ACTION` 里的「优化」。
它于是真的去改代码：50 次 `apply_patch`、71 次 `run_shell`，最后交付审查给出
「意图仍未对齐」，整轮作废。

修法：`_OPINION` 排在 `_ACTION` 之前判。「在问我怎么看」和「让我去做」是两件事，
但句子里都可能出现动作词。带明确指派（帮我 / 然后 / 顺便 / 直接）的不降级。

判出来的结果是第三种意图 `advise`，不是 `answer`：`answer` 是一次调用直接答，
一行代码都没看，只能说「界面要简洁」这种放之四海皆准的话；`advise` 会先只读看几处
（`READONLY_TOOLS` 白名单），然后给有依据的判断——不写文件、不跑命令、不起快照。

真实 6 次请求的判定变化：

| 用户原话 | 修前 | 修后 |
|---|---|---|
| 你好 | act | answer |
| 你现在的工作目录是哪里 | act | 交给模型判 |
| 你觉得界面比 codex 有什么可以优化的（116 步那次） | act | **advise（只读看几处再答）** |
| 我们的应用你觉得就只有这些需要改么（12 步那次） | act | **advise** |
| 我给你开启执行命令了 你开始优化吧 | act | act |
| 你可以自己优化自己么 | act | act |

### 3. 等待审批被判成测试失败

`run_validation_node` 只认 `ok` / `denied`，`awaiting_approval` 落进默认分支：
`passed=False` → 修复轮次 +1 → 回到 `execute_action` → 模型重试同一个需要审批的
工具 → 再等 → 再判失败，直到步数烧完；最后 `resolve_status` 把整个任务记成
「测试未通过：run_tests」。

实测该任务有 3 次 `validation.completed` 全是 `awaiting_approval / passed=False`。
前面 116 步的全部产出（改过的文件、写过的测试）一条都没交付。

修法：等待审批是 `skipped` 且 `passed=True`，不进修复回路，如实写
「测试在等待你批准，本次未执行」。真失败仍然失败（有用例守着）。

### 4. 审查没给出结论，被当成「审查不通过」

审查节点要求模型回一段 JSON。模型偶尔回自然语言（或干脆跑偏），解析失败走异常分支，
返回 `approved=False`。而路由看到「未通过、修复预算还有剩」就把任务打回执行——
执行那边已经没有新东西可改，于是形成 `审查 → 执行 → 审查` 的空转：

```
T02 实测（加一个函数、6 次工具调用）
修前：31 次 execute_action + 27 次 review_result = 35 万 token，产出与 6 步时相同
修后：11 次调用、6.6 万 token，测试照样全过
```

修法两条，缺一不可：

1. 审查没结论 ≠ 审查不通过。解析失败时记 `review.unavailable`，放行并在交付里写明
   「独立审查未完成」，而不是打回重做。
2. 给审查本身加上限（`MAX_REVIEW_ROUNDS = 2`）。只靠修复轮次兜底是不够的——
   一次"没结论的审查"不消耗修复预算，回路就能一直转。

这件事的教训比数字更值钱：**"失败要重试"这条策略，必须配一个"重试能改变什么"的判断**。
执行已经无事可做时，重试只是把同一段上下文再发一遍。

## 附带修掉的探索成本

那次任务有 20 多次调用是在探工具链：`where node pnpm uv`、`dir node_modules\.bin`、
`tsc.CMD --version`、`pnpm exec node -v`……因为应用拉起的后端进程里 `PATH` 没有 pnpm，
而 `AGENTS.md` 只写了后端怎么跑。

现在 `AGENTS.md` 给出绝对路径的实测命令（前端单测、类型检查），并明确写
「别再用 where / dir / --version 逐个试」。

## 量化：把花销拆成三笔账

看总额没有意义。9M token 里可能 7M 出自一个跑废的任务——那时候要改的是那一件事，
不是所有事。所以先分类，再优化：

```bash
python scripts/token_ledger.py                     # 读本机客户端库
python scripts/token_ledger.py --json ledger.json  # 留档，便于和以后比
python scripts/token_ledger.py --compare 2026-10-21T06:00,2026-10-21T12:00
```

台账按**钱的去处**分四类：`delivered`（有改动且成功）、`advised`（成功但没改文件，
产出是判断）、`wasted`（失败/取消/跑飞）、`running`（未结算）。

2026-10-21 15:45 的真实快照（9 个任务，含改造前的历史记录）：

| 去处 | token | 占比 | 任务数 |
|---|---|---|---|
| 有改动且成功 | 3.37M | 30.5% | 2 |
| 成功但无改动 | 373K | 3.4% | 5 |
| **失败/取消/跑飞** | **7.13M** | **64.5%** | **1** |
| 进行中 | 184K | 1.7% | 1 |

同一个快照里的四个效率口径：

| 口径 | 数值 | 怎么读 |
|---|---|---|
| 单次调用平均上下文 | 34K token | 成本地基：步数不变、上下文翻倍，账单就翻倍 |
| 缓存命中 | 94.6% | 便宜不是因为少调用，是因为命中了缓存 |
| 工具失败率 | 18.7% | 失败一次要重试一次，是"多跑几步"的源头 |
| 每个有效改动文件 | 169K token | 跨模型、跨任务可比的那一个数 |
| 每任务 token 中位 | 164K | 长尾很重：均值被一个 7.13M 的任务拉高 |

结论一句话：**当时 64.5% 的钱花在一个后来被判失败的任务上**。这比"平均每步省多少
token"重要得多，也是先修意图判定、再修工具失败、最后才谈裁剪顺序的原因。

## 减 token 的四个动作

前提：缓存命中 94.6%，所以**不能事后改写历史消息**——历史一改，前缀缓存整段失效，
省下的上下文会被未命中缓存的差价吃回去。四个动作全部发生在"结果刚产生时"。

| 动作 | 位置 | 省什么 |
|---|---|---|
| 工具输出分级预算 | `services/agent/payload.py` `TOOL_BUDGETS` | shell/测试保留头尾（失败摘要在最前、结论在最后），读文件保留开头 |
| 相同输出折叠 | 同上 `render()` 的 `seen` | 同一任务里第二次拿到完全相同的结果，只回一句指回第几步 |
| 补丁结果不回吐 diff | 同上 `DROP_DIFF_TOOLS` | 补丁是模型自己写的，回吐一遍等于让它读自己的作业 |
| shell 方言一次说清 | `tools/shell.py` | Windows 上模型写 `pwd/ls/cat` 必失败；在工具描述里写明 cmd，失败时给等价命令 |

前三项每次省多少都进事件流（`context.trimmed`，带 `total_saved_chars`），
`evidence.context.tool_chars_saved` 与之同源，前端和文档读的是同一个数——不是估的。

第四项直接降"失败率"：实测 83 次 `run_shell` 有 21 次失败，大头是 POSIX 命令
打在 cmd 上。失败一次 = 重试一步 ≈ 一次完整上下文重发。

## 现在有两道预算

| 预算 | 默认值 | 位置 | 管什么 |
|---|---|---|---|
| 步数 | 200 | `DEFAULT_MAX_STEPS` | 别跑太久 |
| token | 1,500,000 | `DEFAULT_TOKEN_BUDGET`（`BLANK_TOKEN_BUDGET` 可覆盖） | 别烧太多 |

只留步数是不够的：每一步都会把整段上下文重发一次，50 步可能花掉 4M token，
而步数一次都不会报警。token 预算在 85% 时进入收尾区（不再开新工作面），
到 100% 时丢弃本轮工具调用、要求模型直接交代「做了什么、没做完什么」。
事件里记 `budget.warning` / `budget.exceeded`，带 `tokens` 与 `token_budget`。

只读调研另有一条更紧的步数上限（`READONLY_MAX_STEPS = 8`）：看几眼就该有结论，
它的目的不是把活干完。

## 还没做的

- **上下文水位偏高**：窗口 128K、水位 0.82（104,960 token），实测单次已经到 114K
  才触发压缩。而 184 次 `context.measured` 里 `tokens_saved` 全是 0——不是压缩失效，
  是压根没到水位。压水位能省钱，但会频繁打断思考，暂时留着。
- **token 预算没进数据库**，是进程级阈值。按任务单独设置要加列 + 迁移，
  目前它还只是个安全网，不值得为它动表结构。
- **沙箱默认开着（`BLANK_SANDBOX_ENABLED` 缺省 true），而没装 docker 的机器上
  每个命令都会抛 `SandboxUnavailable`**。桌面壳自己传了 false，命令行/自建部署
  不会。现在至少不再把它判成"测试失败"（`_environment_blocked`），但更好的做法是
  启动时探测一次并明说，而不是等每个任务各自撞墙。

## 怎么复核

```bash
# 判定变化（含 advise）
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_intent_and_reply.py -q
# 只读拦截与 token 预算、环境失败不判失败
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_readonly_and_budget.py -q
# 审批不再判失败
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_validation_gate.py -q
# 工具输出瘦身、补丁可修复错误
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_token_trimming.py -q
# shell 方言
cd services/api && .venv/Scripts/python.exe -m pytest tests/test_shell_dialect.py -q
# 台账（读真实库，不写库）
python scripts/token_ledger.py
```

产出"比别家好"怎么验收，见 `docs/QUALITY.md`（`evals/compare.mjs`）。
