# REA 第二波：协议与成本（Responses API + 提示词缓存）

> 承接 `REA-CODEX-CLAUDE.md`。第一波补的是**机制**（计划可更新、hooks、审批分档）；
> 这一波补的是**协议与钱** —— 同一份提示词，发错协议或用错前缀，输入单价能差一个数量级。

## 0. 这一波为什么是这两件事

第一波落地后，我们的能力面和两家基本对齐了（工具、审批、子代理、交接、记忆都有）。
继续对标"功能清单"收益已经不大，真正还差的是两类**结构性**的东西：

1. **协议形态**：我们只会说 `/chat/completions` 一种方言。新模型（推理系列）在 Responses 上
   才有完整的 `reasoning` / `store` / 缓存语义；用错协议不是"少个功能"，是"贵且看不见"。
2. **前缀稳定性**：我们的上下文压缩、交接笔记、AGENTS.md 分层都在动**前缀**。
   而两家的计价都按"最长相同前缀"算 —— 前缀一动，整段历史重新计费。

证据来自本机实物拆解（`E:\projects\codex-reverse\raw\` 与 `claude-reverse\raw\`）：

- Codex 走 Responses 时实际下发：`instructions` + `input[]` + 扁平 `tools` + `store:false`
  + `prompt_cache_key` + `reasoning.effort` + `include=["reasoning.encrypted_content"]`；
- Codex 把系统提示从 `messages` 里**拿出来**放进 `instructions`，工具调用与工具结果都是
  `input[]` 的一等条目（`function_call` / `function_call_output`），靠 `call_id` 配对；
- Anthropic 侧是"前缀缓存 + 断点"：`cache_control: {type:"ephemeral"}` 打在**稳定前缀的末尾**，
  命中部分按约 0.1 倍计价，写入按 1.25 倍。

## 1. 本期落地（7 项，全部带测试）

| # | 改动 | 文件 | 为什么 |
|---|---|---|---|
| 1 | **新增 Responses 适配器** | `services/models/openai_responses.py`（新） | 换协议才能用上推理档位与缓存键；`store:false` 保证客户任务内容不停在供应商存储 |
| 2 | **注册新供应商类型** `openai_responses` | `models/model_provider.py`、`services/models/registry.py`、`web/.../settings/bits.tsx` | 配置页的 kind 列表由 presets 推导，加预设即出现在下拉里 |
| 3 | **Anthropic 缓存断点** | `services/models/anthropic.py` | system 末尾、工具表末尾、对话尾部各打一个断点（上限 4 个，用 3 个），命中率随轮次上升 |
| 4 | **交接笔记改挂尾部** | `services/agent/graph.py::_insert_handoff` | 原来插在系统提示之后，一插就把它后面的全部历史作废；改挂"最后一条 user 指令之前"，前半段原样不动 |
| 5 | **缓存/推理 token 计量** | `services/models/base.py::Usage`、`models/model_provider.py::ModelUsage`、迁移 `0011` | 不拆细分就只能看到"便宜了"，看不到"为什么便宜" |
| 6 | **成本按缓存打折** | `services/models/pricing.py` | 缓存命中部分按倍率计价（Anthropic/DeepSeek 0.1、OpenAI 0.5），未收录倍率的模型不打折 |
| 7 | **输出档位落到供应商行** | `models/model_provider.py`、`schemas/model.py`、迁移 `0012` | `reasoning_effort` / `verbosity` 写死在代码里等于所有客户共用一个档位 |

### 关键实现细节

**Responses 的消息模型转换**（`openai_responses.py`）

```
system 第一条        → instructions
system 其余（交接/摘要）→ input[] 里的 developer 条目
user / assistant     → message + input_text / output_text
role=tool            → function_call_output(call_id)
assistant.tool_calls → function_call(call_id, name, arguments)
tools                → {"type":"function","name","description","parameters"}  // 不套 function
```

两个容易踩的点，已经写进代码注释：

- **推理模型不接受 `temperature`**：带了直接 400，所以只在 `reasoning_effort` 为空时才下发；
- **`prompt_cache_key` 只跟系统提示有关**：按 instructions 的 sha256 取前 32 位，
  **与任务内容无关** —— 同一套系统提示的不同任务命中同一份服务端缓存。这是"跨任务复用"的关键。

**缓存断点为什么是三个**

Anthropic 允许最多 4 个断点，且按前缀顺序生效：`system → tools → messages`。
我们打三个：system 末尾（长期稳定）、工具表末尾（每轮相同）、对话尾部（下一次调用的前缀）。
第三个是"移动断点"：每次调用都把断点推到最新位置，于是下一轮能命中上一轮的全部内容。

## 2. 怎么用

新建供应商时选 `OpenAI Responses`，或直接走接口：

```json
POST /api/v1/providers
{
  "name": "openai-responses",
  "kind": "openai_responses",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "model": "gpt-4.1",
  "supports_tools": true,
  "context_window": 400000,
  "reasoning_effort": "medium",
  "verbosity": "low"
}
```

`reasoning_effort` 取 `minimal|low|medium|high`（留空则不下发，等价于旧行为）；
`verbosity` 取 `low|medium|high`（留空不下发）。

**看省了多少钱**：

```json
GET /api/v1/providers/usage/summary
→ providers[].cache_hit_rate   // 命中率
→ providers[].cached_tokens    // 命中量
→ providers[].cost.amount      // 已按缓存倍率折算
```

## 3. 没有抄的部分（以及为什么不抄）

| 机制 | 为什么不抄 |
|---|---|
| **三代工具协议按模型档位自动切** | Codex 那样做是因为它要给不同世代的模型发不同形态。我们的工具表统一是扁平 function，切协议只在少数模型上有收益，复杂度远大于收益 |
| **140 个特性开关** | 我们的开关是"设置项 + 环境变量 + 请求参数"三处，已经够用。Codex 那套开关是它自身持续迭代的产物，抄形态不如抄"每个开关要有生命周期"的意识 |
| **exec：模型写代码编排工具** | 收益是减少往返，但需要给模型一个 JS/Python 运行时并在其中暴露工具。我们的工具都是异步 Python 函数，落一个等价物等于再做一个沙箱，风险高，留到有明确性能诉求时再做 |
| **服务端远程压缩** | 我们的两层压缩（确定性裁剪 + 五要素摘要）已经在本地闭环，且能解释"为什么省了"，不依赖供应商能力 |
| **freeform apply_patch（grammar 约束）** | 我们的 `apply_patch` 收 unified diff，本身已经是结构化输入；grammar 的收益主要在"模型自由生成补丁文本"，与我们的工具形态不匹配 |

## 4. 第二波收尾（三项补齐）

上一版留了三处缺口，这一版补齐，并顺手把两个 P1 做掉。

| # | 改动 | 文件 | 为什么 |
|---|---|---|---|
| 8 | **配置页补两个档位输入框** | `web/.../provider-create-form.tsx`、`provider-edit-panel.tsx`、`api.ts` | 后端能存、接口能收，但没人能填 —— 等于没做。选 `OpenAI Responses` 预设时默认给 `medium`，其余保持空（等价旧行为） |
| 9 | **特性开关注册表 + 自省接口** | `services/features.py`（新）、`api/routes/settings.py`、前端 `feature-flags-panel.tsx`（新） | 见下 |
| 10 | **只读工具并行执行** | `services/agent/graph.py::execute_action_node`、`services/tools/registry.py`、`services/tools/gateway.py` | 见下 |

### 特性注册表：为什么不是"再加一个设置项"

设置项散落在各处时有三个后果：没人知道总共开了什么、开关改了没生效查不出原因、
删掉一个能力后开关还在那儿骗人。注册表把这三件事一次性解决：

- **单一真源**：`is_enabled("prompt_cache")` 是全仓唯一入口，拼错名字直接抛
  `UnknownFeature`，不给"静默不生效"的机会；
- **生命周期**：每条标 `stable / beta / deprecated`，beta 默认必须关 —— 升级换包
  不能悄悄改变 Agent 的行为；
- **来源可解释**：`describe()` 报 `enabled + source`。四层优先级
  `环境变量 > 设置文件 > 老字段 > 注册表默认`，用户说"我明明开了"，一查就知道
  是被 `BLANK_FEATURE_TOOL_SEARCH` 压住了，还是写在了没人认的旧字段里；
- **老字段兼容**：`tool_search` 声明 `legacy=("tools","search")`，升级前配过
  `tools.search=false` 的用户行为不变，但设置页改的是新字段。

```json
GET  /api/v1/settings/features
→ {"features":[{"name":"tool_search","stage":"stable","default":true,
                "enabled":false,"source":"legacy","summary":"…","legacy":["tools","search"]}],
   "unknown":[], "env_prefix":"BLANK_FEATURE_"}

PATCH /api/v1/settings/features   {"parallel_read_tools": true}
```

写盘是**整批校验、要么全写要么不写**：部分生效最难排查 —— 用户以为全开了，
界面上没有任何一个值是"看起来错的"。

### 只读工具并行：三段的拆分

原来 `execute_action` 是一个 `for` 循环做完全部事情，没法并发，因为并发的前提是
**决策已经定死**。改成三段：

```
① 串行决策   问题门 → 审批门 → pre_tool_use 钩子，产出 _PlannedCall[]
② 执行       按"连续只读段"并发，遇到写操作 / 拒绝项就断开
③ 串行收尾   post_tool_use → update_plan → tool.completed → file.changed → messages
```

三个刻意的取舍，都写在代码注释里：

- **不是整批并发**：`A 读文件 → B 改文件 → C 读文件` 有顺序依赖，全并发会让 C 可能
  读到 B 改之前的版本。只把相邻的无依赖只读聚成一段。
- **拒绝项是边界**：被钩子拦下、被审查否掉、被用户驳回的调用不执行，但它在逻辑顺序上
  夹在中间，保守起见当边界处理，代价只是几毫秒串行。
- **写库加锁**：并发时多条 `_record` 会同时落库。SQLite 连接池有 30s 锁等待兜底，
  但内存库用 `StaticPool` 只有一个连接，并发拿同一个连接会直接报错 —— 用一把
  `asyncio.Lock` 把写库串起来，只读工具省下的是 handler 的等待，不是这毫秒级的写库。

`PARALLEL_SAFE_TOOLS` 单独一张表，不复用 `permissions.READ_ONLY_TOOLS`：后者语义是
"计划模式允许用什么"，里面含 `update_plan`（改计划）、`tool_search`（改可见工具集）、
`request_user_input`（等用户）—— 这三个都不是只读。`git` 也刻意排除：同一张工具表里
还挂着 `commit` / `push`，按工具名放行等于把写操作混进并行批次。

**默认关（beta）**，打开：

```bash
BLANK_FEATURE_PARALLEL_READ_TOOLS=1        # 或设置页「特性开关」里打开
```

## 5. 下一波建议（按性价比）

| 优先级 | 事项 | 理由 | 预估 |
|---|---|---|---|
| P2 | **guardian 结果从二分类改成分数** | 现在只有 allow/deny，导致"可逆动作自动放行"只能靠 `mode=auto` 一把梭；有分数才能设阈值 | 低 |
| P2 | **事件双通道**（人读 / 机器读） | 现在 50+ 事件全量推给前端，CLI 与集成方需要一份稳定最小集 | 中 |
| P2 | **`parallel_read_tools` 观察后转 stable** | 先在真实任务里跑一段，看事件顺序与前端渲染有没有边角问题 | 低 |
| P3 | **工具表按模型档位裁剪** | 17 个工具对弱模型是干扰；Codex 的三代形态本质上就是这件事 | 中 |

## 6. 验收

- `tests/test_protocol_cache_v14.py`（12 例）：请求体形状、缓存断点、缓存键稳定性、
  输出条目解析、流式增量、前缀稳定性、缓存折扣定价、供应商档位透传。
- `tests/test_features_registry.py`（18 例）：注册表完整性、四层优先级、老字段回落、
  未知名报错、整批拒绝写入、`unknown` 拼错可见、接口往返。
- `tests/test_parallel_tools_v14.py`（5 例）：真并发（交错日志，不靠单点耗时）、开关关闭
  时行为不变、事件与消息保持请求顺序、写操作切断并发段、钩子拒绝的调用不执行。
- 全量回归：**363 passed / 14 skipped**（上一版 340，新增 23）。
- 迁移链：`0010_tool_artifacts → 0011_usage_cache_tokens → 0012_provider_output_knobs`。
- 前端：`tsc --noEmit` 与 `eslint` 对改动文件零报错。

**未做**：`parallel_read_tools` 仍为 beta 默认关，等真实任务观察一轮再转 stable。


## 7. 第三波：MoE 里能借的只有调度

起因是一篇把 MoE 讲成「GPT-4 = 8 个 220B 小模型拼起来」的资料。这个说法是错的：
MoE 里专家替换的只是 FFN，attention / embedding / layernorm 全层共享，专家不是小模型，
没法用 prompt 拼出来，必须预训练。所以「把 MoE 集成进产品」这件事，直接答案是不行。

能借的是它那套调度，而且这三件事和小模型无关：

1. **极便宜的分派器**：每个 token 用一次点积决定走哪几个专家，判完就走，不回头；
2. **负载均衡**：专家会被路由塌缩（全挤到少数几个上），所以要 aux loss 或 bias 调节；
3. **成本核算**：总参数决定显存，激活参数决定算力与单价，两个数不能混着报。

第 3 条先落地，因为它是前两条的前提——不知道每个模型的成本，路由就是在瞎猜。

### 7.1 A 档：把参数量拆成两个数

`model_providers` 加两列（迁移 `0013_provider_params`）：

| 列 | 含义 | 决定什么 |
|---|---|---|
| `total_params_b` | 总参数（十亿） | 权重占多少显存 —— 能不能塞进这台机器 |
| `active_params_b` | 每 token 激活参数 | 算力与单价 —— 跑多快、多贵 |

稠密模型两者相等；稀疏模型差一个数量级（671B / 37B）。只填一个数必然误判：
只看总参数会认为 MoE「贵得只能用在最难的地方」，正好和它便宜的事实相反。

`0` 表示未填写——**语义是「未知」，不是「零成本」**。路由遇到 0 会跳过这个维度，
前端把它显示成空输入框而不是 `0`。

### 7.2 B 档：门控路由 + 升级重试

`services/agent/router.py`，打分全在纯函数里：

```
assess(node, 上下文, 步数, 有无验收标准, 已失败次数) → Demand(复杂度 0..100, 档位)
pick(Demand, 候选池)                                → Decision(选中谁, 为什么, 淘汰原因)
```

复杂度 = 节点基线 + 上下文体量 + 步数 + 验收标准 + 失败升级。档位三档：
`<40 轻量 / <70 标准 / 其余重型`。

**三条约束，少一条就会亏：**

- **打分器必须是纯函数。** 路由自己再调一次模型，省下的钱不够付这次调用；判错时
  两个模型的错叠在一起，排查分不清是谁判错的。实测打分是微秒级。
- **够用优先，便宜其次。** 排序键是 `(档位, 成本, 名字)`：先筛掉不够档的，再在够用的
  里挑最便宜的。反过来先挑最便宜再祈祷它够用，就是「路由错了比不路由更糟」。
- **淘汰原因必须进事件。** `model.routed` 里带 `rejected: [{name, why}]`，否则路由就是
  黑盒——用户看到换了模型，却不知道为什么不是另一个。

**候选池只从「本次任务已经选定的供应商」里取**（主用 + 兜底 + 分角色，去重）。
「换个更便宜的模型」和「换个供应商跑」是两件事，后者必须由用户显式指定过。
没配 Key 的供应商排除在外：它只是占位配置，选中等于把任务交给一个必然 401 的端点，
而路由失败不像兜底那样静默降级，是直接报错。

**升级重试**：同一节点工具真跑失败 → `note_failure(node)` → 下一步需求档位 +30 分，
自然跨过一档阈值。三条边界：

- **粘性 + 封顶 2 档**。来回抖动（这步升级、下步降回）比一直用强模型还贵，而且事件流
  里看不出发生过什么。
- **只有真失败才算**。被用户驳回、被钩子拦下、被审查否掉都是策略判断，不是模型不够强；
  为它们换更强的模型，等于拿钱重试用户刚刚说「不」的事。
- **池子里更强的不够用时标 `shortfall`**。选中池子里最强的那个，并在事件里说清楚
  「不是路由判错，是池子里没有更强的」。

**默认关（beta）**，打开：`BLANK_FEATURE_MODEL_ROUTING=1` 或设置页「特性开关」。
关着时 `router=None`，`call_model` 走原路径，一行都不碰。

**可观测**：任务收尾的 `task.completed` 里带一块 `routing`：

```json
{"calls": 8, "switched": 3, "escalations": 1,
 "cost_units": 62, "baseline_units": 405, "saved_ratio": 0.847}
```

单位是「激活参数当量」而不是钱——各家单价随时会变，但同一把尺子量出来的相对值
能回答唯一要紧的问题：**这个开关值不值得开**。省 3% 还要多一个可能判错的环节，
就不值得；省 80% 就值得观察一轮然后转 stable。

### 7.3 刻意不做

- **不让模型自己选模型**。多一次调用、多一层不可复现，而且模型对自己能力的判断并不准。
- **不做按 token 的专家路由**。那是 MoE 的推理时形态，需要权重层面的稀疏计算；在
  Agent 这一层能路由的粒度只有「节点」，硬要做到 token 级只能是自欺。
- **不做负载均衡（暂时）**。原理上要按近 N 分钟 `ModelUsage` 做加权轮询，但现在池子
  通常只有两三个模型，均衡的收益小于「多一个可能出错的开关」的成本。

### 7.4 验收

- `tests/test_model_router.py`（33 例）：打分确定性与可解释、档位边界、`0` 不当便宜、
  激活参数优先、够用优先、淘汰原因、`shortfall`、同价定序可复现、升级粘性与封顶、
  按节点隔离、历史有界、成本记账与 `saved_ratio`、老库字段缺失、真事件验证（切换 /
  无适配器退回 / 失败提档 / 被拦不提档 / 关掉无痕迹）。
- 全量回归：**396 passed / 14 skipped**（上一版 363，新增 33）。
- 迁移链：`0012_provider_output_knobs → 0013_provider_params`。
- 前端：`tsc --noEmit` 无报错；`eslint src/components/workbench src/components/settings`
  零报错（顺手修掉了四处既有报错：`safety-panel` / `agent-runtime-panel` 里 effect 内
  同步 setState，`workbench` 里同类问题与 `goalNote` 的 memo 无法保留）。
- 活动面板：`选择模型 → 云端 671/37（标准档）` 一行摘要，悬浮显示难度打分与
  「为什么不是另一个」；任务收尾行显示 `路由 8 次 · 换模型 3 次 · 省 85%`。

**未做**：`model_routing` 仍为 beta 默认关；负载均衡；工具表还没按档位裁剪。

### 7.5 C 档：工具表跟着档位走（`tiered_tools`）

路由算出档位之后，同一个决定还有另一半没被用上：**给这个模型看多少个工具**。
Codex 三代协议之间的差异本质上就是这件事——菜单越短，弱模型选错的概率越低；而省下的
schema token 是每一轮都在付的，不是只付一次。

`services/tools/registry.py` 新增两个档位视图，`set_profile()` 在两者之间切：

| 档位 | 下发 | 数量 |
|---|---|---|
| `full`（标准 / 重型 / 没路由） | 核心工具全集 | 11 |
| `lean`（轻量） | 每类能力只留一个代表 | 8 |

lean 里留下的是**一条完整的最小回路**，不是随便砍：

```
看    read_file / list_files / search_code
改    apply_patch / run_shell
规划  update_plan / request_user_input
出口  tool_search
```

被拿掉的全是「已保留工具的专门化」或「长会话便利」：`run_tests` / `git` 是 `run_shell`
的专门化（模型说"跑测试""提交"时，`tool_search` 的别名表直接把它们拉回来）；
`write_notes` / `read_notes` 服务跨窗口交接，撑不到交接那一步的模型写出来的笔记也没信息量。

**四条约束：**

- **裁剪是展示，不是能力边界。** 被藏起来的工具照样注册、照样做风险评估、照样走审批；
  按名字调过来仍然会执行。隐藏工具永远不能变成绕过策略的通道。
- **已激活的跨档保留。** lean 下用 `tool_search` 拉回 `git`，切回 full 再切回 lean，它还在。
  否则弱模型刚把能力拿回来、下一步又被夺走，表现就是最难查的那类「工具丢失」。
- **切回来必须完整。** `_base_core` 记住裁剪前的基准，lean → full 恢复原集合且**顺序不变**
  （顺序进 prompt，变了就是缓存前缀失效）。
- **子代理不受影响。** 它拿到的本来就是一小撮白名单，再裁一层只会造出两份互相矛盾的
  「当前能用什么」，所以 `activate_all` 的 registry 对 `set_profile` 直接返回 False。

**开关**：`tiered_tools`（beta，默认关，`BLANK_FEATURE_TIERED_TOOLS=1` 开）。关着时
`_scope_tools` 直接 return，连 `set_profile` 都不调——"绝对不动"比"动了但结果一样"更容易证明。
开着但没有 router（池子里只有一个候选）时同样不动：没有档位就没有裁剪依据。

**可观测**：只在可见集合**真的变了**时发一条 `tools.scoped`，每步都发等于噪音。

```json
{"profile": "lean", "tier": "light", "tier_label": "轻量",
 "visible": ["tool_search", "update_plan", "request_user_input", "list_files",
             "read_file", "search_code", "apply_patch", "run_shell"],
 "hidden": ["spawn_agent", "spawn_agents", "write_notes", "read_notes",
            "memory_search", "git", "run_tests", "web_search", "web_fetch"]}
```

活动面板显示 `轻量档 下发 8 个，收起 9 个`，悬浮列出具体收起了哪几个——只说数字，
用户没法判断是不是收错了。

**验收**：`tests/test_tiered_tools.py`（16 例）：档位映射（只有轻量收窄）、lean 是 full 的
真子集、切回来顺序一致、`set_profile` 返回值语义、未知档位抛错、检索拉回、跨档保留、
隐藏工具仍可执行、子代理免疫、`as_dict` 带 profile、真事件验证（轻量下发 8 个 /
标准恢复全量 / 开关关闭恒全量且零事件 / 无 router 不动 / 提档后菜单同步放宽）。

**顺手修的回归**：`#46`（纯问答不拍快照）落地时只跑了 `-k` 子集，`test_intent_and_reply.py`
的 `_Session` 桩少了 `ensure_snapshot`，两个用例失败——补上桩并加了断言：动作类任务
`snapshot_calls == ["create_plan"]`，问答类 `== []`。这是"改完只跑相关子集"的典型代价。
