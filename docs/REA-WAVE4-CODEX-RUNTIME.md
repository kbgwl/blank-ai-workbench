# REA 第四波：用运行态实物拆解 Codex 的实现过程

日期：2026-10-22 拆解对象：本机 `C:\Users\22103\.codex`（`cli_version = 0.162.0-alpha.17.2`，
`session_meta.originator = "Codex Desktop"`、`source = "vscode"`）
方法：只看实物。前三波读的是二进制里的字符串（能证明"有什么"，证明不了"怎么跑的"）。
这一波读的是 Codex 在自己机器上**跑出来的运行态产物**：会话 rollout、用量账本、压缩记录、
Windows 沙箱的落盘结构与它自己的日志。凡本文出现的数字与字段名，都能用文末命令复现；
复现不了的结论一律不写。

## 0. 实物清单

| 实物 | 数量/形状 | 用途 |
| --- | --- | --- |
| `sessions/**/rollout-*.jsonl` | 3 个文件 | 追加式事件账本，实现的骨架 |
| 顶层记录 | `session_meta` 3、`event_msg` 2159、`response_item` 1882、`world_state` 8、`turn_context` 93、`token_usage_record` 592、`compacted` 3 | 七类，不多不少 |
| 内层类型 Top | `item_completed` 1306、`token_count` 593、`reasoning` 581、`custom_tool_call` 496、`custom_tool_call_output` 496、`message` 298、`task_started` 90、`task_complete` 89、`thread_settings_applied` 80、`function_call` 5、`function_call_output` 5、`turn_aborted` 1、`compaction` 1 | 真实调用分布 |
| `.sandbox/setup_marker.json` | `{"version":5,"offline_username":"CodexSandboxOffline","online_username":"CodexSandboxOnline","created_at":…,"proxy_ports":[],"allow_local_binding":false,"read_roots":[],"write_roots":[]}` | 沙箱初始化状态 |
| `.sandbox-secrets/sandbox_users.json` | `version:5`，`offline.password` / `online.password` 是 `AQAA…` 开头的 base64 DPAPI blob | 独立账号凭据 |
| `cap_sid` | 4 个键：`workspace`、`readonly`、`workspace_by_cwd`、`writable_root_by_path`，值全是 `S-1-5-21-…` | 按工作区分配的自定义 SID→ACL 账本 |
| `.sandbox/deny_read_acl_state.json` | 本机是 `{"principals":{}}` | 拒绝读的 ACE 记账（这台机器上没启用条目） |
| `.sandbox-bin/` | `codex.exe` + `codex-command-runner-0.154.0-alpha.6.2.exe` / `-0.155.0-alpha.9.exe` | 命令执行助手按版本各存一份 |
| `.sandbox/sandbox.*.log` | 带模块名的时间戳日志 | 沙箱自己的失败也写日志 |
| `config.toml` | `model = "gpt-5.6-luna"`、`model_reasoning_effort = "medium"`、`notify = [codex-computer-use.exe, "turn-ended"]`、若干 `[plugins."…"] enabled` | 用户侧配置面 |

## 1. 记录层：一个只追加的账本，七类记录

`rollout-*.jsonl` 每行一条记录，固定 `{"timestamp","ordinal","type","payload"}`。
`ordinal` 是**单调序号**，不是时间：读侧不依赖时钟就能定序。七类记录里只有 `event_msg` 和
`response_item` 是"发生了什么"，其余五类各自承担一个别的系统职责：`session_meta` 存会话身份与
基础提示词、`turn_context` 存治理、`token_usage_record` 存账、`compacted` 存历史替换、
`world_state` 存环境全量快照。

对照我们：`agent_events` 一张表、一个 `seq`、`type` 是自由字符串——同一条通道什么都装。
好处是简单，代价是"账本"和"过程"混在一起，压缩后过程记录还在但历史已经被换掉了，
两者的对应关系只能靠约定。**可复现**：见 §7 第一条命令。

## 2. `turn_context`：每回合把治理原样钉一遍

93 条记录，实测字段（截自 `ordinal=5` 那条，节选：省略了 `workspace_roots` 之外的可视化目录条目
与 `permission_profile.entries` 里同类的两条可视化路径授权，字段名与取值未改）：

```json
{"turn_id":"01a13d72-…","root_turn_id":"01a13d72-…","disabled_plugin_ids":[],
 "cwd":"E:\\projects\\kbgai",
 "workspace_roots":["E:\\projects\\kbgai","C:\\Users\\22103\\.codex\\visualizations\\2026-10-15\\01a13d6e-…"],
 "current_date":"2026-10-15","timezone":"Asia/Shanghai",
 "approval_policy":"on-request","approvals_reviewer":"user",
 "sandbox_policy":{"type":"workspace-write",
   "writable_roots":["C:\\Users\\22103\\.codex\\visualizations\\2026-10-15\\01a13d6e-…"],
   "network_access":false,"exclude_tmpdir_env_var":false,"exclude_slash_tmp":false},
 "permission_profile":{"type":"managed",
   "file_system":{"type":"restricted","entries":[
     {"path":{"type":"special","value":{"kind":"root"}},"access":"read"},
     {"path":{"type":"path","path":"E:\\projects\\kbgai"},"access":"write"},
     {"path":{"type":"special","value":{"kind":"slash_tmp"}},"access":"write"},
     {"path":{"type":"special","value":{"kind":"tmpdir"}},"access":"write"},
     {"path":{"type":"path","path":"E:\\projects\\kbgai\\.git"},"access":"read","missing_path_behavior":"skip"}]}}}
```

三个设计点值得单独说：

1. **`root_turn_id` 与 `turn_id` 并存**：子回合能回溯到根回合，代理树不用另建一张表。
   我们靠 `AgentSession.depth` / `agent_path` 两个字段，等价但没进事件，审计时看不全。
2. **`permission_profile` 是"生效值"而不是"配置引用"**。它不写"用了哪个 profile"，
   而是把限制展开成逐条 ACE（`:root` 只读、仓库可写、`.git` 只读且 `missing_path_behavior:"skip"`）。
   也就是说：**看一条记录就知道当时能碰哪些路径**，不需要再去解析配置。
   `writable_roots` 里包含 `.codex/visualizations/<日期>/<会话>`——工具产物目录是被显式授予写权限的，
   不是"顺便可写"。
3. **日期与时区也在治理快照里**。`current_date:"2026-10-15"` + `timezone:"Asia/Shanghai"`：
   "今天"是回合属性。对照结果比预想更糟——我们**事件里没有，提示词里也没有**
   （`grep` 过 `services/agent/` 全部模块，无任何日期注入），所以模型完全不知道今天几号，
   跨天恢复的任务更无从对齐"当时的今天"。本轮已把 `current_date`/`timezone` 补进快照（审计）；
   注入提示词会改变每次调用行为，留给下一批单独决策。

## 3. `token_usage_record`：三级累计 + 缓存与推理分开记

592 条，一条实测：

```json
{"thread_id":…,"turn_id":…,"session_id":…,"root_turn_id":…,"response_id":"resp_0802…",
 "usage":{"input_tokens":25536,"cached_input_tokens":19200,"cache_write_input_tokens":0,
          "output_tokens":259,"reasoning_output_tokens":124,"total_tokens":25795},
 "turn_token_usage":{…同形状…},
 "thread_token_usage":{…同形状…}}
```

要点：单次调用、回合、线程**三个口径同时写**，所以"这一轮花了多少"和"这个任务累计花了多少"
是两条都能直接查出来的事实，不靠求和推算。`cached_input_tokens=19200`（占输入 75%）与
`reasoning_output_tokens=124` 单独成字段：缓存命中率与"想多久"是可审计的，不是模型自报。

对照我们：`model_usage` 表按调用存 prompt/completion tokens，证据包用
`sum(row.latency_ms)`、`prompt_tokens + completion_tokens` 现算；缺的是**三级口径同时写**
这件事。**这条是本轮之后最值得补的账**：没有缓存字段就无法证明提示词缓存真的生效，
只能说"我们打开了 `prompt_cache`"。

> 回查纠正（2026-10-22，写 §7.3 时实测）：上面这段里"`cache_*` 与 `reasoning_*` 没落库"
> 是我第一版写错了。查 `models/model_provider.py` 的 `ModelUsage` 与
> `services/models/openai_compat.py` 的 `_usage_from()`，三列早就在表里、也确实在写；
> 真实库上 703 行里 `sum(cached_tokens)=2,943,232` / `sum(prompt_tokens)=3,461,107`。
> 真缺的两处是：这些数**从没进过事件与证据包**（所以界面上看不见），以及没有三级口径。
> 记下来是因为这类"我以为没做"的判断本身就是要核对的东西。

## 4. `compacted` 与 `world_state`：替换历史与快照环境是两类记录

`compacted`（3 条）的形状是 `{"message":"", "replacement_history":[完整的新历史数组]}`——
压缩**不是摘要字符串，而是把整段历史替换掉**，并且替换后的内容原样落盘，可重放。
`thread_settings_applied`（80 条）与 `compaction`（1 条内层事件）负责说明"什么时候换了套设置"。

`world_state`（8 条，`{"full":true,"state":{…}}`）是环境侧的全量快照：`agents_md`、
`apps_instructions`、`collaboration_mode{mode, model, instructions:"<sha>"}`、
`environments{local:{cwd,status,shell:"powershell"}}`、`context_window_guidance`，
以及一段 XML 形态的 `filesystem`/`permission_profile` 序列化。注意指令是用 **哈希** 代表内容
（`instructions:"196577151ae691aee6e4add4dd0a75a5693c07da"`），既能在事件里指认版本，
又不把几万字抄进账本。

对照我们：两层压缩 + 换窗口时的 `_insert_handoff` 注入 notes，压缩后**不留"替换了什么"**的记录，
所以事后无法回答"这次压缩丢了哪些内容"。`world_state` 的 `full:true` + 内容哈希这两个手法，
是我们"压缩边界成为一等记录"（任务 #31）可以直接借的形状。

### 4.1 `compacted` 的完整形状（#42 拆到这一层，比 §4 第一版记的多出七项）

同一份 rollout 里三条 `compacted` 逐条量出来的载荷键与字节（`C:/Users/22103/.codex/sessions/**/rollout-2026-10-15T10-40-46-01a13d6e-*.jsonl`，
整条记录 307,553 字节——第一版只记了 `message` + `replacement_history`，漏了下面这些）：

| 键 | 实测 | 它解决的问题 |
| --- | --- | --- |
| `window_number` | 1 → 2 → 3 | 现在活的是第几个窗口，一眼可数 |
| `first_window_id` / `previous_window_id` / `window_id` | 三个 uuid7；`first` 始终 `01a13d6e-fc65…`，`previous` 等于上一条的 `window_id` | **窗口链**：新窗口指回被它替换掉的那个，整条替换史能从任意一条往回走 |
| `replacement_history` | list，18 → 36 → 39 项，每项 `{type,id,role,content[],internal_chat_message_metadata_passthrough}` | 替换后的**全量**历史，原样落盘、可重放（不是摘要字符串） |
| `replacement_history_metadata` | 与上一项**等长**的平行数组：`{retained_source{id{message_id,turn_id,role},revision,complete}, client_authored, user_input_order, mcp_attribution{status,sources[]}}` | 逐条**出处**：这条保留内容来自哪条消息、是否完整、是客户端写的还是工具产生的、原始用户输入排在第几位 |
| `guardian_history` | 96 / 151 / 133 项，228,102 字节；类型分布 `reasoning 27 · custom_tool_call 23 · custom_tool_call_output 23 · message 20 · function_call* 3` | 审阅人（guardian）看的**全量轨迹**：压缩丢的是活动历史，审阅侧一条不丢（我们这一侧的对应实现见 §7.40，任务 #79） |
| `retained_context` | `{verified_answers, incomplete, user_messages[{revision,order,turn_id,message_id,text,complete}]}` | 被**点名保留**的用户原话（带 order 与"是否完整"），不只是"塞回去了几条" |
| `compaction_response_id` | `resp_080281808ab14ccf016ad0968ec7d48199a80fc5be24a0d819` | **做这次压缩的那一笔供应商调用**的 id——压缩本身花的是真钱，得能追到那一笔 |
| `latest_token_usage_record` | 8 键：`thread_id/turn_id/session_id/root_turn_id/response_id/usage/turn_token_usage/thread_token_usage`（三级各自带 cached / cache_write / reasoning） | 压缩**那一刻**的三级用量定格；`root_turn_id` 在这里就是一等字段 |
| `resume_metadata` | `{multi_agent_version, last_started_turn_id, turn_attribution{turn_id,turn_trigger,parent_turn_id,initiating_agent_path,root_turn_id}, previous_turn_settings{model,…,comp_hash,realtime_active}}` | 重放时要恢复的身份与设置；`turn_trigger:"composer"`、`parent_turn_id:null`、`root_turn_id` 等于自己 |

三条实测出来的两件事，光看一条看不出来：
① `resp_0802…` 三处**完全相同**——三次压缩是同一次供应商调用的产物（换窗口不等于重新摘要一次），
所以 `compaction_response_id` 是"钱的来源"，不是"窗口的来源"，别拿它当窗口主键去 join。
② `replacement_history` 里的文本是**原样明文**，连 `E:\projects\kbgai\kbgai` 这种主机路径都在里面（`retained_context.user_messages` 里就有）。
它敢这么写，是因为这份文件是本机单用户的私有日志；我们的 `agent_events` 会进证据包、被同项目其他人看到、
还进全文索引。**所以借的是"逐条 + 出处 + 窗口链"这套结构，不借"把正文抄进账本"这一手**——
与 #41 对指令层用哈希指认是同一个口径。这一裁决的落地与其四层证据见 §7.7（任务 #42）。

## 5. Windows 沙箱实物：没有 Docker，也没有 Hyper-V

这台机器是 Windows 11 家庭版、无管理员权限（连可选功能查询都返回 `requires elevation`），
Docker / Hyper-V / Windows Sandbox 全部排除。Codex 在这台机器上实际是怎么做的，
`.sandbox*` 与它自己的日志写得清清楚楚：

- **两个本地专用账号**，按网络能力分档：`CodexSandboxOffline` / `CodexSandboxOnline`
  （`setup_marker.json` v5 的 `offline_username` / `online_username`）。
  联网与不联网不是运行时开关，而是**两个身份**。
- **凭据是 DPAPI 密文**，不是明文也不是环境变量：`.sandbox-secrets/sandbox_users.json` 里
  `AQAA…` 开头的 base64 blob（DPAPI 头），按当前用户解密。
- **按工作区分配自定义 SID**：`cap_sid` 是 `workspace` / `readonly` / `workspace_by_cwd` /
  `writable_root_by_path` 四本账，值全是 `S-1-5-21-…`。ACL 授予到"这个工作区专属的 SID"，
  因此不同工作区之间互不相通，改一个目录的授权不会波及其他目录。
- **拒绝读单独立账**：`.sandbox/deny_read_acl_state.json` 的 `principals` 用来记哪些主体
  被显式加了 deny-read ACE（这台机器上是空的，即机制在场、条目未启用）。
- **命令由独立助手进程执行**：`.sandbox-bin/codex.exe` + 每个 CLI 版本一份
  `codex-command-runner-<version>.exe`；日志里有 `helper copy: reused command-runner source=… destination=…`
  和 `helper launch resolution: using copied command-runner path …`。
  执行体与主程序分离，才能以另一个用户身份 `CreateProcessAsUser`。
- **网络限制靠 `proxy_ports` + `allow_local_binding`**（这台机器上 `proxy_ports:[]`、
  `allow_local_binding:false`，即默认不放行）。
- **best-effort 步骤失败也只写日志，不中止**：
  `hide users: failed to hide current user profile dir (C:\Users\Default): SetFileAttributesW failed … 5 (拒绝访问。)`。
  这与我们「快照拍不了也要继续跑，但必须在事件里说清楚为什么没拍」是同一条原则。

`config.toml` 侧则完全没有沙箱字段——沙箱档位是由 `turn_context.sandbox_policy` 在回合里决定的，
不是启动时全局钉死。这与任务里定的"三档 sandbox × 审批模式"方向一致。

## 6. 差距表（前四条已动，其余排队）

| Codex 的做法 | 我们之前 | 本轮处置 |
| --- | --- | --- |
| 每回合钉一条 `turn_context` 治理快照 | 只有 `task.started` 一次任务级快照，中途改模式/换模型/开关被 env 压住都不留痕 | **已实现**：`turn.context` 事件（`services/agent/governance.py` 单点口径），只在治理字段变化时落一条 |
| 每次命令调用都带 `sandbox_policy`（rollout 里每条 `custom_tool_call` 都能回溯到当时的边界） | `tool.completed` 事件与证据包里没有执行边界；被拒的调用不留痕；`AuditLog` 是一个没人调用的孤儿类 | **已实现**：边界进事件、进证据包、进时间线卡片，并落 JSONL 审计账（`sandbox_backend_event_field` + `AuditLog.record`） |
| 用量三级累计 + 缓存/推理字段 | 表里已经有单次与缓存/推理列，但累计只能靠求和推；事件流与证据包里看不见这三个口径 | **已实现**（任务 #32，见 §7.3）：每次调用落一条 `model.usage`，一次带出 单次/本回合/整个任务 三个口径 + 缓存与推理；证据包求和与事件任务级同源；面板新增「缓存命中 / 推理 / 三级口径」三行。**子代理已并入同一本账**（任务 #33，见 §7.4）：落账抽成唯一函数 `usage.account_call()`，线上真跑出一条派了子代理的任务，面板上 `本回合 22,275 ≠ 任务累计 26,505`。**供应商那一笔的 request id 也进了同一处收口**（任务 #40，见 §7.5）：`response_id` 从适配器一路到 `model_usage` 行、`model.usage` 事件与「证明」面板，DeepSeek 真链路 7/7、真任务 5/5、真浏览器 5/5 |
| 压缩写 `replacement_history`，环境写 `world_state(full, 内容哈希)` | 压缩只留一个"已压缩"标记，不留下"替换到哪里为止"；环境压根没记账，指令层以 dataclass repr 的形式把**正文和主机路径**抄进事件（单条 `turn.context` 实测 4145 字节） | **两半都落地**：边界那一半见任务 #31（§7.2）——`context.compacted` 带 `boundary`/`covered`/`folded`/水位，时间线里是一张卡；环境那一半见任务 #41（§7.6）——`world.state` 全量快照 + 指令用 sha256 前 40 位指认，正文与主机路径不再入账（4145 → 1770 字节）。**替换历史见任务 #42（§7.7）**：窗口链（窗号递增 + `window_id`/`previous_window_id`/`first_window_id`）与逐条引用（位置/角色/种类/字数/内容指纹）已入账并进界面；**整段 `replacement_history` 原文按裁决刻意不抄**——借结构不借抄正文 |
| `permission_profile` 展开成逐条 ACE 与 `missing_path_behavior` | 只记审批模式与前缀规则 | **已实现**（任务 #77，见 §7.38）：`turn_context.permission_profile` 逐条列出工作区/允许根/外部通配，每条带 `enforced_by`（`os`/`policy`/`none`）——本机 job_only 下如实写 `policy`，不冒充内核；`missing_path_behavior` 与 `SafetyPolicy._outside_paths` 同源（`skip`），关掉「仅限工作区」时写 `not_applicable`；证据包取末次快照，老任务报 `unrecorded` 不按档位反推 |
| 沙箱 = 独立账号 + DPAPI + 工作区专属 SID ACL | Docker 容器；无 Docker 时只能 `allow_local` 直连 | 立项设计已出：**`docs/SANDBOX-WINDOWS-DESIGN.md`**（§1 是今天真实生效/只是展示值的对照表，含一条实测的逃逸证据；实现未动） |
| 「另一台机器的差异都能归因」是 M4 的退出判据 | 八条探针只能各自自证，**没有任何东西把两份报告放在一起对质**；「归因」这个问题无从回答，本机又没有第二台机器（无 docker、无 wsl） | **已实现装置**（任务 #78，见 §7.39）：报告 schema 2 起带机器指纹（`host_id` 是主机名的 sha256 前 12 位，明文不落）与 `audit_task_id` 跑法标记；`blank doctor --compare A.json B.json` 按**只收实测关系的归因表**判五种结果，同机两份报告**主动拒证**（`same_host` rc=1），缺指纹/换 schema/跨平台/装置没自证过一律不比；换 Python 或系统版本只列进 `context`，**不解释任何探针**。17 条用例（HEAD 基线 17 红）、变异 10/10 按实测红集合命中、四份真报告五组对质逐条真 rc。**跨机实物仍缺**（`EXPLAINED`/`UNEXPLAINED` 只有离线夹具证明过，#76 保持未结） |
| 一整条不删减的 `guardian_history`（实测 96 / 151 / 133 项 vs `replacement_history` 18 / 36 / 39 项） | 审阅判定只活在 `AgentSession` 内存里，压缩记录不带、证据包不带；此前本格按「刻意不做」记账（§7.7 缺口 2、§7.9 缺口 4） | **已实现**（任务 #79，见 §7.40）：`context.compacted` 带 `guardian_history` 六键（审阅侧全量引用 + 判定清单 + 当时生效的窗口与模式），判定是 `guardian.reviewed` **事件的投影**、不是会话副本——真链路量出旧写法在「驳回→暂停→再跑」后一次都带不过来（4 条判定在前、6 次压缩在后，`reviews` 全为 0），改成压缩那一刻现查事件后实测跨 `attempt` 带上（seq 64/71 出生在 `attempt:1`、落进 `attempt:2` 的压缩 133）；`outcome` 进事件；证据包 `decisions`/`windows`/`windows_unrecorded` 与事件逐字段同源。16 条用例（HEAD 上整文件收集 ImportError）、17 个变异按**实测**红集合命中（`battery-run4` 报 8 条 MISMATCH 后写回）、真链路 13/13 绿。**正文仍按裁决不抄**，`client_authored`/`mcp_attribution`/`retained_source` 未做，前端未渲染（#80） |
| `root_turn_id` 进事件 | `depth`/`agent_path` 在内存里 | 已随快照带出（`depth`/`agent_path` 进 `turn.context` payload） |
| `token_usage_record.turn_id` + `resume_metadata.turn_attribution.{root_turn_id, parent_turn_id, initiating_agent_path}` | 用量行只有 `trace_id`，钱问不出「哪一轮花的、谁引起的」；子代理那几笔与主线程混在同一张表里靠时间猜 | **已实现**（任务 #43，见 §7.8）：`run_id` 成为 `model_usage` 的一等列（迁移 0019），行的归属与 `model.usage` 事件同源；`root_thread_id`/`parent_thread_id`/`agent_path` 四处同写；证据包补 `turns`/`branches`/`unattributed_calls` 三张账，老账只报条数、绝不并成「第 0 轮」 |
| `replacement_history_metadata` 逐条出处（`retained_source`/`revision`/`user_input_order`/`client_authored`）+ 一整条不删减的 `guardian_history` | #42 的引用只有位置/角色/种类/字数/指纹——知道"留了哪几条"，不知道"这条是谁写的、第几次问的、活过了几窗" | **已实现**（任务 #44，见 §7.9）：引用多五枚字段 `run`/`user_order`/`retained_id`/`retained_since`/`retained_windows`，出生轮只有一个真源（`EventRecorder.run_id`）且 `carried` 不盖章；跨窗身份按内容寻址 `ret:<sha12>`；证据包 `windows[].turns` + `turns_unattributed` 与事件同源。`guardian_history` 那 228KB 全量审阅轨迹的**清单**半边已由任务 #79 补上（正文仍按裁决不抄，见 §7.40），`retained_source.message_id`/`revision`/`complete` 列为未覆盖 |
| 一次调用的归属与它的用量在同一行里（`latest_token_usage_record` + `turn_attribution.initiating_agent_path`） | #43 把"哪一轮、谁引起的"算清了，token 预算却仍只盯主线程自己的 `usage`；`spawn_agents` 给主代理的负载里没有用量（§7.4 自己记下的缺口 1）；"那一次 fan_out 花了多少"只能从 `agent_path` 字符串里再解析一遍 | **已实现**（任务 #45，见 §7.10）：`parallel_group` 成为 `model_usage` 的一等列（迁移 0020），空串＝"不属于任何一批 / 当时没记"；委派单列成 `delegated_usage` 并进两道预算闸（耗尽原因分「token 用完」与「派给子代理的 token 用完」），#33 的三级口径一个字没动；批次合计进 `subagents.completed` 与工具负载、并进证据包 `parallel_groups`；孩子只继承父压缩器**真给过的**水位键；`latency_ms` 刻意不相加 |
| `current_date`/`timezone` 进治理快照 | 事件与提示词里都没有，模型不知道"今天" | 已补进快照（天粒度）；注入提示词未做，会改变每次调用行为，单独决策 |
| 一轮的钱问得出「这一轮里这几个人合起来花了多少」 | #45 只在单任务（一条线程）上验过批次与委派；团队群聊里每个成员各有一条 `AgentSession`，那条链上有没有把委派算进预算、批次行认不认得出出身，只有代码推理没有实物 | **已取证**（任务 #46，见 §7.11）：真 DeepSeek 跑了一轮三人发言的群，`Σ 成员气泡 = Σ usage_by_bot = round.tokens = 101,172`，成员任务里 33,267 = 主线程 30,279 + 派出去 2,988 = 批次 `153fb8` 两条线程之和——**委派确实在群预算之内**。同时量出两处还欠着：账本四列里带 `team-` 的行数为 **0**（群线程与成员的账没有列级连接），每条成员任务自己就是 root（"这三个人合起来花了多少"只能读编排器加的 `team_rounds.tokens`）。界面那一处缺陷（`team-…` 轮 id 砍前八位，同轮三条发言念成同一个 `team-016`）已修并过了 7/7 变异与 10/10 真浏览器点验 |
| 一整棵任务树挂在同一个**发起轮**上（`turn_attribution` 是跨任务的：一次 resume、一批子代理都算同一轮引起的） | #46 量出来账本上没有这一层：`run_id` 每人一个值，"这几个人这一轮合起来花了多少"只能读编排器自己累加的 `team_rounds.tokens`，而它只在发言**收尾**时加——中途失败的那次发言钱花了不计数。另外 `run_id` 声明 36 字符、实物最长 51（`task:{uuid}:attempt:{n}`），SQLite 不检查所以本地看不出来 | **已实现**（任务 #47，见 §7.12）：`model_usage.origin_run_id`（迁移 0021，同时把两处 `run_id` 放宽到 120），发起轮只有一个真源（`EventRecorder`，顶层运行"发起轮=自己"是定义不是兜底，子代理跟着父记录器白拿）；群轮的读写用同一个函数 `round_origin()`，放行续跑算同一轮；群读模型多一个按发起轮从账本筛出来的 `round.usage`；团队页页头改读账本并显示差额芯片，成员条每人念自己本轮那份。真 DeepSeek 一轮四人发言实测 **4 个任务 / 4 个 run / 12 笔 / 51,009** 全收在一个 origin 上 |
| 用量账不引用供应商配置表（`token_usage_record` 里没有指向配置表的外键——账是历史事实） | `model_usage.provider_id` 声明着 `ON DELETE CASCADE`：SQLite 连接层不强制所以本地永远看不出来，MySQL/PostgreSQL 照声明执行。真 MySQL 8.4 实测删一家供应商 = 名下 **3 笔 600 tokens 归零**；概览与成本两个读模型又是内连接，供应商一删那笔钱从合计里静默消失 | **已实现**（任务 #48，见 §7.13）：账本上把这条外键**彻底摘掉**（不是改 RESTRICT——那会让"删除供应商"这个正常动作在强制方言上失败），悬空成为正常状态，由 `PROVIDER_GONE = "供应商已删除"` 念出；两处读模型改左连接；迁移 0022 按方言分支（SQLite 反射出的内联 FK 无名 ⇒ 手写整表重建 + 两次 `COUNT` 守卫，不等就抛错回滚；MySQL/PG 用 `get_foreign_keys()` 取服务端真名字定点摘）。红基线 8/11 → 绿 11/11，变异门禁 10/10 精确命中，真 MySQL 8.4 复跑 **11/11**（含"补回 CASCADE 必须当场抹账"这条正对照） |

| 它的 schema 只有一种出生方式：会话与账本都在一个 SQLite 单文件里，没有第二方言，"同一份结构在两种数据库上都得成立"这件事在 Codex 的实现里根本不存在 | 我们把"能换 MySQL"写在产品口径上，但整条迁移历史在真 MySQL 8.4 上从 0002 就死（17 处 LOB 列写字面量默认值 ⇒ 1101），ORM 的 `create_all` 另有 4 处同样写法（差点被"AST 扫过 0 处"骗过去），两条建库路径还差着 12 个时间列的可空性 | **已实现**（任务 #49，见 §7.14）：17+4 处统一成 8.0.13+ 的表达式默认值（`DEFAULT ('')` / `('[]')` / `('{}')`，两种方言都吃、落值逐列验过没变），迁移 0023 把 12 列收成 NOT NULL（先数 NULL、有 NULL 当场回滚）。真 MySQL 8.4 上整条历史 `upgrade head` **rc=0**（0001→0023）、0023 往返、1101 探针 **5/5**、两条建库路径对账 **7/7**；新用例 6 条 + 变异电池 **9/9 精确命中**；SQLite 侧在**线上库的副本**上逐行比过（20 张表行数、7 张重建表逐行、NULL 反证 + 停在 0022 的正对照） |
| 同上，但下一层：应用**真跑**在第二种数据库上，而不只是 schema 能生出来 | 59 步真 HTTP 端到端跑在两引擎上对照，MySQL 第一版 **25 步红**（4 个根因 + 21 条级联 404）：①提交后直接序列化 `onupdate` 那一列 ⇒ `MissingGreenlet`/500，这一类**引擎无关**，SQLite 上也 500，而全仓 866 条离线用例里 `/skills` 一条 HTTP 用例都没有；②MySQL 8.4 的 INSERT 没有 RETURNING，任务/会话/Bot 创建后读服务端生成列同样 500；③`aiomysql 0.3.2 × PyMySQL 1.2.3` 转义 bytes 时调的是哨兵字符串（`TypeError: 'str' object is not callable`），检查点写 BLOB 必炸 ⇒ MySQL 上整条运行链路是死的；④两个 MySQL 驱动根本不在 `pyproject`/`uv.lock` 里（`asyncpg` 却在），按声明装出来的环境连不上 MySQL | **已实现**（任务 #50，见 §7.15）：7 处「提交后先读回再出参」（skills 三条 + `cancel_task` 是 UPDATE 形状，`persist_task`/`create_thread`/`create_bot` 是 INSERT 形状，取消语义没放宽、终态仍 409）；`db.ensure_mysql_driver_compat()` 在建引擎那刻把 aiomysql 的引用指回 PyMySQL 自己的 bytes 编码器（3307 实测：补前 `TypeError`、补后 `_binary X'…'` 落库并**逐字节回读相等**）；`pyproject` 加 `mysql` extra + `uv lock`（30 行变更，其余包没动）。**两引擎各 59/59**、库里的实物是 `agent_checkpoint_writes` 34 行/18,076 字节 msgpack、服务日志 Traceback=0；新用例 9 条（含用 `dialect.insert_returning=False` 在 CI 里替身复现 MySQL 的 INSERT 形状）、变异电池 **13/13 精确命中**、全量回归 **875 passed / 14 skipped** |
| 同上，再下一层：**这条判定得住在哪里**。Codex 只有一种出生方式，"跨方言"这件事压根不需要长住的地方 | #50 的装置、日志、桩、库全在 `E:/tmp/`：换台机器、换个人就跑不出来，CI 里 MySQL 那一层**一条真库判定都没有**，换库回归只能靠我手工再跑一次脚本 | **已实现**（任务 #51，见 §7.16）：装置进仓库 `evals/dialect_e2e.py` + CI 新增 `api-dialect` job（真 `mysql:8.4` service、`uv sync --extra mysql`、`--require-mysql`）；跳过与中止分两种退出码（没库 `[SKIP]` rc 0 / 给了却连不上 `[ABORT]` rc 2），"今天没法判"不再伪装成"换库没问题"。这一批还量出并修了**装置自己说谎的两处**：对照格红而被测格全绿时退出码竟是 0（判据要求每格都绿，抽成纯函数 `verdict()` 并被用例钉住）、桩轨迹被下一格删掉（改成每格一份）；新用例 7 条 + 变异电池 **11 靶**（M9–M11 专守 rc 判定） |
| 结论只住在同一份**可重放的账**里——重放不出的路径等于没测过。本机 3 份 rollout 实测 4,041 条带类型的内层事件（`item_completed` 1306 / `token_count` 593 / `reasoning` 581 / `custom_tool_call` 496 与 `custom_tool_call_output` 496 逐条配得上对 / `message` 298 …）与 4,740 条顶层记录（`turn_context` 93 / `token_usage_record` 592 / `world_state` 8 / `compacted` 3），里面**一条审批事件都没有、一条退路事件都没有**：它不靠"这两条在单测里过"来主张自己走得通，走得通就在那份账上 | §7.16 自己记下了「审批与答题这两条路由在这个桩上到不了」，可产品口径一直说这两条走得通；而「离线 882 条全绿」里，一张带着明文密钥的审批卡经由 `/evidence` 与 `/events` 回显出去长什么样，**一条 HTTP 用例都没有**。第一次真跑第 54–62 步，量出来的四处全是「离线绿、端到端不成立」 | **已实现**（任务 #52，见 §7.17）：驱动从 59 步长成 **68 步**（54–62 是审批卡内容可核对 / 批准后文件真写出 / 驳回留痕 / 提问挂起 / 答案回上下文 / 空流退整包，删除尾段顺延编号），桩按消息里的探针标记换工具、其余照常；修掉四处实物缺陷——① 桩的非流式支发裸 `{"name","arguments"}`，产品只认 `call["function"]["arguments"]` ⇒ 空参数 ⇒ 风险算成 medium ⇒ 按 `risk:auto` 静默放行，**一张卡都挂不出来**（两分支现共用 `_envelope`，用产品自己的解析函数验）；② 密钥只在 `FileChange.diff` 一处打码，事件载荷四类型裸奔 ⇒ `/evidence` 整包能捞出原件，现收在 `events.py` 写入边界 `mask_deep` + `mask()` 的形态兜底（执行仍用检查点里那份真参数）；③ 答题那一路跳过 `tool.completed`，时间线上没有可对核的实物；④ 诊断被自己的 `[:400]` 截断，`_why` 现以「终态原因」开头、事件通道断了退化成「取证失败」而不是假绿。**§7.16 未覆盖 1 结案**：桩抖的根因是 SSE 无 Content-Length、靠 `close()` 定界，Windows proactor 在接收缓冲有未读字节时发 RST 而非 FIN，把已 flush 的响应一起丢 ⇒ `ReadError`；现每段响应自带边界 + `finish()` 半关读空，桩自检从两数变三数（**连接/服务/截断**，连接>服务=有请求没被读到）。两格各 **68/68**、每格连接 19=服务 19/截断 0、第 19 步一次没重试；装置闸门 **17 条**（2.95s，原先 182.40s）、密钥边界 3 条、变异电池 **10/10 按实测红集合命中**、全量回归 **894 passed / 14 skipped** |
| 它没有"带令牌"这一形态：单机单用户，`~/.codex` 里的东西只有本人读得到，所以"开放面在鉴权之下长什么样"这个问题在它的实现里不存在；它的错误形状也只有一种来源（本地进程），不用把供应商的 4xx/超时翻译成同一种可判定的东西 | 装置 68 步全跑在**不设令牌**那一形态上，而产品口径一直是"设了令牌才交付"——设令牌后 `/openapi.json`、`/docs`、`/redoc` 三条 FastAPI 自带路由在 `Depends(require_token)` 之外，**匿名可读**（实测 83 个 operation / 65 条路径全暴露）；`BLANK_MODEL_TIMEOUT_SECONDS` 读进了 settings 却没接到 `AdapterConfig.timeout_seconds`，是**死配置**；退路不问能不能帮就重放（非幂等时请求翻倍、多等一整轮）；`graph.py` 的 `except httpx.HTTPError` 分支不可达（8 个适配器调用点全走 `_client()`），一条判定都守不到它 | **已实现**（任务 #53，见 §7.18）：驱动从 68 步长成 **74 步**（69–71 令牌三查：匿名每条接口 401 / 带令牌 200 / 文档面在守卫之下读不到；72–74 退路三查：400 退且退后正常流式、429 与 503 **不退**并留 `model.stream_fallback_skipped`、超时按点失败）；`app.py` 以 `guarded = bool(settings.api_token)` + `_mount_guarded_docs()` 把三条文档路由收进令牌之下（不设令牌时仍是本地开箱可读）；`base._client()` 作为唯一翻译点把 `httpx.TimeoutException`→`model_timeout`、`HTTPError`→`model_unreachable`，**状态码从真响应骑到 `ModelError.status`**（v36 用 3 条车道 × 3 种状态码参数化钉住，不再靠手工设字段）；`BLANK_MODEL_TIMEOUT_SECONDS` 接进 `timeout_seconds`（非正数 ⇒ 60.0，写坏 ⇒ pydantic 当场报错）。新用例 **29 条**（v34 令牌面 5 / v35 超时与死配置 4 / v36 状态码骑乘 20）、两格各 **74/74** rc=0「每一格全绿：对照判据成立」、每格连接 27=服务 27/截断 0、`agent_checkpoint_writes` 两格同为 234 行/101,101 字节；变异电池 **14/14 按实测红集合命中**（含 3 处「预期写宽 → 改成实测」与 3 个**只打装置**的自检靶 M-F1/F2/F3）、7 份文件 sha256 逐字节还原；全量回归 **926 passed / 14 skipped** |
| 审批记忆落成 `prefix_rule`，pattern 是**完整 argv**（`-Command` 正文整段都在规则里），按「这一条命令」记 | 前缀只到 `command_prefix`：批准 `python -c "print(1)"` 后账本里只记了 `python`，随后 `python evil.py` / `python -m pip install requests` / `python -m http.server` / 一段写 `id_rsa` 的内联脚本全部静默放行；卡片文案与账本规则各算各的；不可逆命令被前端硬传 `scope=prefix` 时也能记成 `git push` | **已实现**（任务 #54，见 §7.19）：`memo_rule` 把解释器规则带到正文 / `-m` 模块 / 脚本路径，卡片与账本同源，不可逆与含密钥形态不入账，留痕写实际学到的规则；装置 76 步两格全绿，12 靶按实测红集合命中 |
| 群聊里的审批不是「一张卡」而是**一条队列上的一条消息**：谁挂起、批完谁接着发言、拒了以后这一轮还能不能收尾，都归编排器管 | 单任务审批在装置里已走通（#52），团队路径只有离线用例；`POST /team/messages/{id}/approval` 是异步续跑（202 + 后台任务），轮状态、队列推进、驳回后的终态都没有跨方言实物。群聊里批错入口会得到哪一种 409 也没人钉过 | **已实现**（任务 #55，见 §7.20）：装置从 76 步长成 **80 步**，同一群里主理人先挂卡、批准后成员接着挂卡、驳回后消息 `paused` 且轮 `completed`；错入口两个 409 码分开；两格各 80/80 rc=0，4 靶按实测红集合命中。**产品代码零改动**——这一批补的是跨方言判定，不是修缺陷 |
| 429 限流只代表「现在别问」：主流客户端会退避后再问，而不是把这一轮判死 | #53 把 429 归进「与问法无关」、不退路是对的，但**一句都不再问**：供应商抖一下整轮就失败；装置第 77 步只钉了"不许静默成功"，没钉"过一会儿能好" | **已实现**（任务 #56，见 §7.21）：流式与整包两条路都只对 429 退避重试 **2 次**（0.5s/1s），已经出字后一次都不重试；每次重试落 `model.rate_limit_retry`（attempt/status/wait_ms/max_retries），两次仍 429 才走原来的 `stream_fallback_skipped`。装置从 80 步长成 **81 步**：桩只拒第一次，断言「恰好一条重试事件 + 终局成功 + 退路 0 次」；两格各 81/81 rc=0 |
| 429 上供应商给的 `Retry-After` 比客户端猜的退避更权威；压缩那一笔也是花真钱的调用 | #56 只做了固定 0.5s/1s 盲猜——供应商明说「1 秒后再来」也照样 0.5 秒就敲门，说 120 秒也没有上界；`summarize_history` 直接调 `adapter.invoke`（避免递归压缩），这条路的 429 仍是即刻失败 | **已实现**（任务 #57，见 §7.22）：等待取 `Retry-After`（整秒或 HTTP 日期）与指数退避的**较大者**、**30s 封顶**；事件新增 `retry_after_ms` 原值与含封顶的 `wait_ms`；四条适配器的 HTTP 抛错点全部把表头骑到 `ModelError`；压缩复用同一套闸门/事件（`role=summarizer`、`node=context_compact`）。装置从 81 步长成 **82 步**：桩带 `Retry-After: 1`，断言 waited=1000ms、终局成功、退路 0 次；两格各 82/82 rc=0 |
| 压缩只有离线反证/单机真模型探针，装置从没让它在跨方言的 HTTP 栈上真发生 | #42/#44 的 `window_chain_probe.py` 只跑单机 SQLite + 真供应商；#57 的压缩退避只有离线用例；装置 82 步里没有一步会跨过窗口水位，"压缩在两种方言上是否工作"没有实物 | **已实现**（任务 #58，见 §7.23）：装置 82 → **83 步**，窗口调成 1200 tokens（水位 984）+ 两轮各读 6KB 文件逼出真折叠；断言窗口链逐窗递增、边界严格前进、替换 `dropped/items` 非空、`compaction_response_id` 指认摘要响应；摘要那笔第一次 429+`Retry-After: 1`，事件 waited=1000ms。两格各 **83/83** rc=0，读数同为 **2 窗 / 末窗折叠 3 条 / dropped=5 / items=4 / 成功工具 2 个**；3 靶按实测红集合命中 |
| 压缩的「摘要」在真模型与两种数据库上是不是同一件事：真模型写的摘要、供应商响应 id、窗口链与替换记录必须一起出现 | #58 的摘要由桩写死五要素 JSON，真模型压缩只有 #42/#44 的单机 SQLite 证据；"真模型跨方言"从未同框（:8061 当时还跑着 0017 旧库旧代码，事件里连 `replacement`/`compaction_response_id` 都没有） | **已实现**（任务 #59，见 §7.24）：`compaction_probe.py` 可指向任意实例（`PROBE_BASE/PROBE_OUT_DIR`）并加 5 条硬判据（摘要非空、每窗响应 id、窗口链 1..N 且 boundary 严格前进、替换 dropped/items 非空、证据包与事件同一组非空 id）。真 DeepSeek × SQLite(:8061) 与 MySQL 8.4(:8063) 各 **11/11** rc=0，两格同为 **2 窗 / boundary 3→7 / 摘要 400+ 字符**；库内 `agent_events` 与 `model_usage.response_id` 逐项相同。组合变异靶使 5 条新判据全红、旧 6 条保持绿，`graph.py` sha256 逐字节还原 |
| 它的结论只住在一种出生方式（SQLite）里：「PostgreSQL」只是迁移分支里的一行 `else`，从没起过一台真库 | 0021/0022/0023 都写着 `if dialect.name == "sqlite" ... else`，但没有任何一条证据是 PG 上跑出来的——整条迁移历史、应用、83 步 HTTP 判定全部零实跑；CI 也只有 MySQL 一格 | **已实现**（任务 #60，见 §7.25）：便携 PG 17.6（`127.0.0.1:55432`）上整条 0001→0023 历史 rc=0、`downgrade base`→`upgrade head` rc=0；同一份 83 步驱动新增 `postgres` 格，SQLite 对照 + PG 各 **83/83**、合计 166 PASS、0 FAIL、rc=0；PG 库内 7 任务 / 187 事件 / 23 笔账 / 461 条检查点。装置新增 PG URL 解析、建库、检查点见证与归属点名，CI `api-dialect` 增 `postgres:17` service。负对照（PG 专属坏 `search_path`）装置 rc=1，`db.py` 逐字节还原 |
| 「两条建库路径给出同一个 schema」只在 SQLite 上对过账；PG 的 `create_all` 与迁移是否一致从没量过 | #49 的 A1 只跑 SQLite，而 SQLite 对可空性/默认值照收不误；#60 只证明迁移路径能跑，测试与本地快速起库用的 `create_all` 路径在 PG 上的形状没有对账 | **已实现**（任务 #61，见 §7.26）：真 PG 17.6 上开两个库分别跑迁移与 `create_all`，逐表/逐列/可空性/外键/服务端默认值对账。第一次真跑数出 **77 列**只有迁移侧有服务端默认值（ORM 只有 Python `default=`），已把 ORM 补齐（Text/JSON 用表达式默认值，不改 Python 默认值、不改迁移）；3 条用例绿，2 条变异靶按实测红集合命中并逐字节还原，全量 **976 passed / 17 skipped**；CI `api-dialect` 增该对账步骤 |
| 同一个 schema 还包含**列类型/长度、索引与唯一约束**：不是"列名一样"就等价。约束名是运维/迁移按名字引用的对象 | #61 的对账只到列名/可空性/外键/默认值，`String(36)` vs `String(120)` 这类漂移看不见；索引少一条、唯一约束名字不同，两条路建出来的库在 DBA 工具与后续迁移眼里并不等价 | **已实现**（任务 #62，见 §7.27）：真 PG 上补比 **213 列的反射类型与长度**（118 列带长度）、**44 个索引**、**6 个唯一约束**（名字一起比）。第一次跑就红在 `model_providers.name`：迁移 0003 显式命名 `uq_model_providers_name`，ORM 的 `unique=True` 被 PG 自动命名成 `model_providers_name_key`。ORM 改成显式 `UniqueConstraint` 后 5 条全绿；3 条变异靶按实测红集合精确命中、逐字节还原。CHECK 两边都是 0 条，不写空对空的对账，记了触发条件 |
| MySQL 侧两条建库路径自 #49 之后只剩「迁移能跑通」；`create_all` 没再逐列对过 | #61/#62 的对账只在 PG 上跑；MySQL 的列宽漂移、少一条索引、外键自动命名都看不见。而 MySQL 对同一个 `now()` 会存成两种同义写法，直接逐字比会天天假红 | **已实现**（任务 #63，见 §7.28）：真 MySQL 8.4 上两个库分别跑迁移与 `create_all`，比 **213 列 / 118 个长度 / 48 个索引 / 6 个唯一约束 / 21 个外键（名字 + ON DELETE）/ 125 个服务端默认值**。第一次量出 13 条：12 条是同义时间写法（0023 的 ALTER 重述成 `CURRENT_TIMESTAMP`，create_all 是 `(now())`），1 条是真分歧——`tasks.thread_id` 迁移显式命名 `fk_tasks_thread_id`，ORM 被服务端自动命名成 `tasks_ibfk_2`。ORM 补名字后 0 分歧；新增 4 条用例，PG 侧同时补比外键名与动作；4 靶变异按实测红集合命中，全量 **976 passed / 23 skipped**；CI `api-dialect` 增该步骤 |
| 主方言 SQLite 的两条建库路径只对过表/列/可空性/外键；类型、长度、索引、约束名、默认值没比 | #61–#63 把新维度补在了 PG/MySQL 上，主方言反而缺席。生产走 `alembic`、测试与本地起库走 `create_all`——两边差一个索引或一个约束名，测试全绿而生产缺对象 | **已实现**（任务 #64，见 §7.29）：SQLite 上两条真库逐项对账：**20 表 / 213 列（118 个长度）/ 38 索引 / 6 唯一约束 / 21 外键（名字 + 动作）/ 125 默认值 / 0 CHECK**。先量出 8 条布尔默认值分歧：迁移写 `'1'/'0'`，ORM 的 `text("true"/"false")` 落成 `'true'/'false'`；改用 `true()/false()` 后 0 分歧。v29 从 6 条长到 **9 条**，4 靶变异红集合精确相等，全量 **979 passed / 23 skipped** |
| 全群累计的 `usage_by_bot` 拿发言行当筛子：没有发言行的钱被 inner join 静默丢掉，也没有"未对应发言"档 | #47 把本轮的合计改成按发起轮筛账本，但全群累计仍是 `TeamMessage JOIN ModelUsage ON task_id`——一笔 origin 在本群、task 却没挂到任何消息上的钱，两个口径都说不清它去哪了；而且没有总数可以核对每档之和 | **已实现**（任务 #65，见 §7.30）：两个来源取并集（task_id 属于群内消息 / origin_run_id 属于群内轮次）、按 `usage.id` 去重；对不上发言的归"未对应发言"，`Σ by_bot == tokens` 成为构造出来的恒等式。`group_state` 新增 `usage`，`usage_by_bot` 保留为同源别名。2 条新用例，4 靶变异红集合精确相等，全量 **981 passed / 23 skipped** |
| 事件时间线只记 `run_id`（每人一个），发起轮进了账本却没进事件：证据包离开数据库就说不清一条事件属于哪一轮 | #47 把 `origin_run_id` 记进了 `model_usage`、记录器内存里也有，但 `agent_events` 没有这一列；`/tasks/{id}/events` 与证据包 `timeline` 都只端出 `run_id`，跨任务归属还得再 join 任务或账本 | **已实现**（任务 #66，见 §7.31）：`AgentEvent` 补 `origin_run_id`（VARCHAR(120)、非空、默认空串、带索引），`EventRecorder.add` 落库，`EventOut` 与证据包 `timeline` 同源端出；老行保持空串（当时没记，不许用 `run_id` 回填）。迁移 0024；新用例 6 条，自然红 6/6，6 靶变异红集合精确相等，全量 **987 passed / 23 skipped** |
| 证据包只有「按 run / 按 trace / 并行组」三张表，没有「按发起轮」的那一张：一条事件有了归属字段，却仍要人肉把一轮拆出来的成员任务加回去 | #66 端出了每条事件的 `origin_run_id`，`usage.turns` 里也按 `run_id` 有账；但团队一轮的发起轮（群轮）自己没有一行的汇总，跨任务核对只能逐条筛 | **已实现**（任务 #67，见 §7.32）：`evidence.origin_rollup()` 按 `origin_run_id` 汇总 `run_ids/threads/tasks/subagent_calls` 与用量；`usage.origins` 与 `usage.turns` 并列，空发起轮的账**不编成第 0 轮**，只由 `usage.origin_unattributed_calls` 报数。新用例 3 条，4 靶变异红集合精确相等（自然红是 HEAD 上的 collection ImportError，见 §7.32）；全量见 §7.33 |
| 批准记忆只活在会话与任务里：换一个任务、重启一次进程，用户刚说过的「总是允许这个前缀」就不认了 | #54 把规则粒度修到正文，但 `PermissionPolicy` 只在内存、任务级靠回放 `approval.granted`；同一台机器上换任务仍要重新挂卡。Codex 对应的是持久化的 `~/.codex/rules/default.rules` | **已实现**（任务 #68，见 §7.33）：前缀档落盘到 `approvals.json`（默认与设置同目录，`BLANK_APPROVALS_FILE` 可指认），只收 `scope=prefix` 的 `run_shell` 前缀，去重/最新在前/上限 200，坏文件 fail-closed；判定里记 `prefix:persistent:…` 与本次会话的批准分开归因。新用例 22 条，自然红在 HEAD 上为 collection ImportError，8 靶变异红集合精确相等；全量 **1012 passed / 23 skipped** |
| 内联解释器代码（`python -c "open(...).write(...)"`、`node -e`、`pwsh -Command`）算 medium，默认模式按 `risk:auto` **静默执行**，审批卡一次都不出现 | `assess_risk` 只认破坏性命令与联网；`risk_for` 把这类命令判成 medium，而 `PermissionPolicy.decide` 对非 high/critical 直接 `risk:auto`。这不是批准记忆放行，是根本没问 | **已实现**（任务 #69，见 §7.34）：`_has_inline_code()` 认解释器后的 `-c/-e/-Command/-EncodedCommand/cmd /c/bash -lc`（旗标必须在第一个非旗标参数之前，`-m 模块` 与脚本参数不算），命中即 high；复合命令任一段命中即 high。新用例 22 条，自然红 **14 failed / 8 passed**，4 靶变异红集合精确相等 |
| 它的规则只记「这一条命令 / 这个前缀」，没有「整个工具本会话全放行」这一档；放行边界窄，卡片不需要解释例外 | 工具档比它宽：记住 `run_shell` 后普通命令不再问——但 `is_irreversible()` 排在工具档**之前**，`git push --force` / `drop table` 仍逐条问。卡片却只写「该工具在本会话内不再询问」，把「有例外」写没了 | **已实现**（任务 #70，见 §7.35）：文案改成「该工具在本会话内不再询问；不可逆命令仍逐条确认」，并补一条**能失败**的断言——工具档记住后不可逆命令仍必须 `ASK/irreversible`（把工具分支挪到不可逆判定之前即红）。判定逻辑零改动，57 条审批用例全绿 |
| 它按 `turn_id` / `turn_attribution` 的字段名直接区分谁是谁，消费方不需要猜字符串形状 | `run_id` 一列混着四种实测形状（空串 / `team-…` / `task:<uuid>:attempt:N` / `agent_runs.id`），证据包 `turns` 只给原值；界面与外部消费者只能各自 `startswith` 或正则猜，猜错了也不报错 | **已实现**（任务 #71，见 §7.36）：`evidence.run_id_kind()` 在**读侧**定形状 `unrecorded/team/attempt/run`，`turns[].kind` 随每轮带出；存量值一个不改（空串仍是「当时没记」）。新用例 3 条 + v24 键集，自然红：v24 单跑 **1 failed / 12 passed**（`KeyError: 'kind'`）、v45 为 collection ImportError；4 靶变异红集合精确相等 |

## 7. 本轮落地与其验证证据

实现（对标 §2，只报我们自己真有依据的字段）：

- `services/api/src/blank_api/services/agent/governance.py`：`sandbox_boundary()`（现取，
  后端一律走 `get_sandbox`，不重新推导）、`feature_snapshot()`（生效值 **+ 来源**）、
  `turn_context()`（主机路径在这里就掩掉）、`digest()`（**只算治理字段**）。
- `graph.py`：`AgentSession.emit_governance()`，调用点紧跟 `supports_tools` 兜底之后、
  窗口/压缩之前——先落快照再动上下文。
- 快照里补了 `current_date` / `timezone`（§2 第 3 点）：只到天粒度，
  用例 `test_current_date_is_audited_without_breaking_dedupe` 连发三步仍只落一条，
  放进秒级时钟这条就会红。
- `evidence.py`：`governance` 块（`snapshots` / `first` / `changes` / `models` / `modes`）。
- 前端：`web/src/components/tasks/api.ts` 的 `normalizeGovernance()` + `sandboxBoundaryLabel()`
  + `sandboxBoundaryNeedsWarn()`（判定抽成纯函数才测得到）；`evidence-panel.tsx` 新增
  「执行边界与策略」区块。

证据（按"每条判定都能失败"的口径）：

- 后端 `tests/test_governance_snapshot_v16.py`：**12 passed**。含沙箱数值来自真实 policy
  （`BLANK_SANDBOX_CPUS=2.5` 等）、无 docker 且未开 `allow_local` 时 `backend=="none"` 且
  `denied` 含 `ALLOW_LOCAL`、主机路径不进事件（`":"` + `/` 断言）、快照先于压缩落下
  （把 `compactor.build` 打成爆炸也要有那条记录）、老任务不编造历史、以及
  `test_the_wire_contract_is_what_the_panel_reads`（把跨语言字段名钉死在前端读的键上）。
- **变异证明**：`digest()` 最初把 `step`/`node` 算进指纹，去重永不命中，用例期望 1 条实得 2 条——
  是这条用例先红才暴露的实现 bug，改完转绿。前端侧两次变异各红一条：
  去掉 `|| source === "env"` 红"特性标记"用例；去掉 `first 为空则 sandbox=null` 的守卫
  红"老任务不补一份"用例；两次都改回并复绿。
- 全量回归：后端 **707 passed / 14 skipped**，前端 **85 passed**，`tsc --noEmit` 无输出。
  `tsc` 还抓到一处真漏：`Governance.sandbox` 加了 `enabled` 字段但投影没读它——
  类型层先红，界面才不会显示「暂无」。
- 真实链路（不是 fixture）：`evals/governance_probe.py` 通过隔离实例 `:8061` 的真实 DeepSeek
  任务拿到 HTTP 证据包，`snapshots=1`、`writable_root:"<workspace:ws>"`（掩码生效）、
  `backend:"local", enabled:false`，再把**这份 JSON** 灌进 `normalizeEvidence()`，投影正确。
- 浏览器点验（Chromium，视口 **531×559**，第二个 dev 实例 `BLANK_WEB_DIST_DIR=.next-verify`
  指向 `:8061`，未触碰用户常驻的 `:3000` / `:8000`）：
  证明页签实测渲染
  `执行边界与策略 / 审批模式 default / 模型（当时配置）deepseek-chat / 执行边界 沙箱未启用 · 宿主直连（不承诺隔离）· 断网 · 根目录只读 / 资源上限 cpus 1 · memory 512m · pids 128 · tmpfs 128m · timeout_seconds 120 / tool_search·legacy、handoff_notes·default、prompt_cache·default、stream_output·default / 共 1 条快照…`。
  同屏暴露一条真实漂移：用量行 `模型 deepseek-flash`、快照行 `模型（当时配置）deepseek-chat`——
  供应商实际服务的模型与请求的模型名不一致，因此两行标签已改为「模型」与「模型（当时配置）」。
  新旧同跑：`snapshots=0` 的老任务（`098dc115-…`）证明页正常渲染但**不出现**这一节，
  也不会凭空给出边界。

顺带查实的一条既有事实（不是本轮引入）：这台机器上 `tool_search` 的来源是 `legacy`——
即它由旧字段驱动，而不是新特性开关。以前没地方看见，现在快照里看得见并标了警告色。

### 7.1 第二项落地：每次调用把执行边界说清楚（对标 §5 与 rollout 的逐调用形状）

实现（口径只有一处定义，别处搬运）：

- `services/tools/gateway.py`：`SANDBOX_DATA_KEY`（命令类工具才往 `data["sandbox"]` 写，只读工具不写——
  于是**键的有无本身**就是"这次调用起没起进程"的判据）、`_sandbox_backend()`、
  `sandbox_backend_event_field()`（事件字段的唯一定义处）、`_audit()`（记账 + 被拒调用也记）。
- `graph.py` 两处 `tool.completed`（主工具循环 + `run_tests` 兜底，后者补了 `tool_call_id`，
  否则测试那行和台账对不上）、`subagent.py` 一处 —— 子代理也在边界内，对应"全 Agent 进边界"那条路线。
- `agent/evidence.py`：**不给 `ToolCall` 表加列**。台账记"做了什么"，边界记"在哪做的"，各自有主；
  读出时用 `tool_call_id` 把边界贴回 `tools[]` 行，少一次迁移也少一处会写歪的副本。
- `sandbox/audit.py`：孤儿类 `AuditLog` 接进运行时，新增 `record()`；目录由 `BLANK_AUDIT_DIR` 定，
  默认 `data/audit`。
- 前端：`timeline.ts` 的 `eventBoundary()` 用 `hasOwnProperty` 区分"值为 null"与"键不存在"；
  `status.ts` 的 `boundaryLabel()` / `boundaryNeedsWarn()`（`host` → 「宿主执行（未隔离）」警示色）；
  `timeline-blocks.tsx` 在卡片右上角渲染 chip。

证据（按"每条判定都能失败"）：

- 后端新用例 `tests/test_sandbox_audit_v16.py`：**7 passed**。分别钉住
  宿主执行必须"说出来而不是省略"（`"sandbox_backend" in payload` 且为 `None`，审计 `host_execution is True`）、
  只读调用不带这个键、后端名照原样搬运（假工具 `sandbox="win-native"`，为立项后的真实取值预留契约）、
  被拒调用同样落账且 `trace()` 读得回、审计行里主机路径与两类密钥形态都不出现、
  证据包 `tools[]` 逐行带边界、以及**整条图自己**（mock 模型跑 `graph`）会发出这些字段并按 `task_id` 落账。
- 真实 HTTP（DeepSeek，非 fixture）：`evals/boundary_probe.py` 六项判定全 PASS（`rc=0`）——
  任务 `b24fb624-…` 里 `run_shell` 真跑了 `echo boundary-probe && ver`，回显进了证据包，
  `tools[]` 与 `tool.completed` 都带 `sandbox_backend: null`，`list_files` 那行不带这个键，
  审计 `audit_lines=2 chain=['list_files:ok','run_shell:ok']` 且 `workspace="<workspace:ws>"`。
- 装置自检（负对照，同一探针换任务号）：`81d243d3-…`（真实超时任务）→ `run_shell:timeout`、
  无只读行，2 条判定变红、`rc=1`；`fca81e26-…`（只走 `list_files` 的任务）→ 4 条变红、`rc=1`。
- 全量回归（本轮收尾复跑）：后端 **714 passed / 14 skipped**，前端 **89 passed**，`tsc --noEmit` 无输出。
- 浏览器点验：**真 Chrome（headless），视口 1440×900**，第二个前端实例
  `BLANK_WEB_DIST_DIR=.next-verify -p 3120` + `API_PROXY_TARGET=http://127.0.0.1:8061`，
  未触碰用户常驻的 `:3000` / `:8000`。7 项判定 PASS：命令卡片有 chip、chip 带 `border-warn/40 text-warn`
  配色、非命令卡片没有、整页 chip 数 == 命令卡片数、负对照任务（只读）零 chip、无控制台报错。
  两次前端变异各红三条且互补：`eventBoundary` 改成"忽略键的存在性"→ 红"非命令卡片不带 chip"
  ＋"整页数量"＋负对照；改成"一律不显示"→ 红"每张命令卡片都渲染出 chip"＋配色＋整页数量。
  同一变异下 `timeline.test.ts` 也各红对应的那条（9 条里红 1 条 / 红 3 条），改回后 9/9 复绿。
- 顺带查实并修掉的真 bug（本轮引入的实现里）：`redact(json.dumps(detail))` 在 Windows 上**永远匹配不上**
  `C:\\Users\\…`——JSON 序列化把反斜杠成对转义了，掩码自测一直是假绿。改成逐字段 `shield_value()`；
  并且 `extra_secrets` 必须显式传给 `record()`，否则非 `sk-` 形态的口令（用例里 `AuditTokenValue-42`）会漏网。
  这两条都由"读解析后的字段、不读原始文本"的用例钉住。
- 收尾时发现并补的第二个缺口：审计账默认落 `data/audit/`，而这个目录**当时没被 `.gitignore` 覆盖**
  ——`git check-ignore` 实测返回非零。现在 `data/audit/` 已入忽略表（`.gitignore:61`），
  测试侧另由 `tests/conftest.py` 把 `BLANK_AUDIT_DIR` 指到临时目录，跑测试不会往仓库里写账。

如实说明未覆盖的部分：这台机器上 `BLANK_SANDBOX_ENABLED=0`，从未真起过沙箱进程，所以
"后端名非 null"的 chip（`沙箱：win-native` 这类）目前只有单测与预留契约支撑，浏览器里只见过
「宿主执行（未隔离）」这一种取值；被拒调用在界面上的呈现同样只有单测覆盖。子代理那条 emit
是照同一 helper 搬运的，未单独做真模型点验。

### 7.2 第三项落地：压缩边界成为一条能定位的记录（对标 §4 的 `compacted`）

`context.compacted` 事件早就带齐了账（`boundary`/`covered`/`folded`/水位/`triggered_by`），
问题是**界面上看不见**：一次任务的上下文从哪里开始已经不是原文，用户与模型都不知道自己踩在哪一侧。
这一轮把它做成时间线里的一等卡片，并顺手在真实链路上撞出一个真缺陷。

实现：

- `web/src/components/tasks/timeline.ts`：新增 `boundary` Block 变体，把事件里的
  `folded`/`covered`/压缩前后 token/`triggered_by`/水位/窗口/`micro` 逐字段搬出来。
  只对 `context.compacted` 建卡——同族的 `context.measured` 每步都发（实测一个任务 4 条），
  全建就是一片噪声；`context.microcompacted` 也不建卡。
- `web/src/components/tasks/status.ts`：`compactionHeadline()` / `compactionWaterLabel()` /
  `compactionTriggerLabel()` 三个纯函数（判定抽成纯函数才测得到）。触发依据的真实取值是**带阶段后缀**的
  （`tokens:fold`、`tokens:micro`、`chars:fold`），因此按冒号前的基底查表，认不出的原样露出，不编中文标签。
- `web/src/components/tasks/timeline-blocks.tsx`：`BoundaryCard`，卡片正面是
  「历史前 N 条已被摘要替换（这次折入 M 条，省 K tokens）」＋边界/折入/压缩前后/触发依据/水位/同轮裁剪/节点。
  `covered` 与 `folded` 必须都报：只按 `folded` 判空会把"水位推进但这次没折新内容"读成"什么都没发生"。
- `services/api/src/blank_api/services/agent/context.py`：`_safe_cut()` —— 见下面那条真实缺陷。
- `evals/compaction_probe.py`（新）：真实 HTTP 链路上的压缩探针，六项判定。

真实链路上撞出来的缺陷（这是本轮最值钱的一条，探针不是来发奖状的）：

把窗口调到 2000 跑一条三步任务，压缩真的发生了（`folded=6, covered=6, water_ratio=1.534`），
但**整条任务失败**：`task.failed reason="模型返回 400：Messages with role 'tool' must be a
response to a preceding message with 'tool_calls'"`。根因是折叠切点按字符预算取整，
落在一轮 `tool_calls` 的配对中间——摘要折掉了发起调用的那条 assistant，却把它的工具结果留在尾部，
发给供应商就是一条孤儿 `role="tool"`。OpenAI 与 DeepSeek 都按这条校验，所以"压缩省了 token"
直接换来了"任务丢了"。

处置：`_safe_cut(history, cut)` 把切点**退回**到声明这批调用的那条 assistant（往回退只多留几条原文，
往下扔会把结果连同证据一起丢掉）；若退到段头仍然是一段无主的工具结果（最老的内容），
则改为**整段前推**，否则等于什么都不折、水位永远压不下去。`build()` 与确定性兜底
`compact_messages()` 两条路径都接上。

证据（按"每条判定都能失败"）：

- 后端新用例 `tests/test_context_pairing_v14.py`：**4 passed**。钉住"切点落在 c3 上必须退到那条
  assistant"、"段头无主时整段前推"、字符预算与 token 水位两条路径产出的历史里
  **不存在无锚点的 tool 段**（`_unanchored_tool_runs()` 就是按供应商口径实现的），
  以及标记里"省略 N 条"必须等于真的折掉的条数。
- 三次变异，各红各自那条：摘掉 `compact_messages` 的保护 → 红字符预算那条（`assert [3] == []`，
  正是孤儿结果的下标）；摘掉 `build()` 的保护 → 红水位那条；摘掉段头前推分支 →
  红新用例**并且**老套件里 5 条同时红（`test_over_budget_folds_…`、`test_summary_window_is_bounded`、
  `test_same_messages_are_not_folded_twice`、`test_summarizer_failure_…`、
  `test_character_mode_is_byte_for_byte_the_old_behaviour`）——这条分支是被既有测试一起守着的，
  少它就是把压缩器变成空转。前端两次变异：`context.compacted` 不建卡 → 单测红 4 条（91/95 通过）且
  浏览器 6 条 FAIL；水位行改成写死的字符串 → 单测红 1 条、浏览器只红「水位读数来自事件」这一条。
  全部改回后复绿：单测 95/95，浏览器 8 项 ALL PASS。
- 真实链路：`evals/compaction_probe.py` 在窗口 2000 下**六项全 PASS、`rc=0`**，
  任务 `042d5319-…` `status=succeeded`，两次压缩 `folded=3`→`covered=3`、`folded=4`→`covered=7`，
  `tokens_saved=498` 与 `857`，`water_ratio` 1.534 / 1.95（比值超过 1 是真的：估算校准后仍高于窗口）。
- 负对照两个，用同一装置只换任务号：没有压缩过的 `b24fb624-…` → 4 条 FAIL、`rc=1`；
  **修复前那条被供应商拒的任务** `cbd4ae9b-…` → 前五条照旧 PASS、只有新加的
  「压缩后的历史没有把工具调用配对切散」FAIL、`rc=1`。这一对是本轮最硬的证据：
  同一个判定，老数据红、修完的数据绿。
- 全量回归：后端 **718 passed / 14 skipped**，前端 **95 passed**（`timeline.test.ts` 15 条），
  `tsc --noEmit` 无输出。
- 浏览器点验：**真 Chrome（headless），视口 1440×900**，dev 实例 `:3121`（Turbopack，
  `/api/v1` 代理到 `:8061`），未触碰用户常驻 `:3000` / `:8000`。八项判定 PASS，其中卡片文本实测为
  `上下文已压缩 历史前 3 条已被摘要替换（这次折入 3 条，省 498 tokens）边界 3 条之前已不是原文 …
  触发依据 按 token 水位 · 折叠了历史 水位 水位 153% · 窗口 2000 节点 执行动作`，
  第二张卡 `历史前 7 条…（这次折入 4 条，省 857 tokens）… 水位 195%`；
  卡片数 == 事件里 `context.compacted` 条数（2==2），负对照任务 0 张卡但页面照常渲染 4 张其他卡。
  （上面这两处 `153%` / `195%` 是当时候的真实读数；任务 #42 的真链路跑法之后，
  比率大于 1 不再印百分比，改印 `超出窗口 1.5 倍 · 窗口 2000` 那种倍数读法，见 §7.7。）

如实说明未覆盖的部分：Codex 的 `compacted` 还带 `replacement_history`（替换掉的那段原文本身），
这一节（#31）只留了边界位置与摘要，**原文不留**。该缺口的后半已在任务 #42 补上——
窗口链与逐条引用（位置/角色/种类/字数/内容指纹）见 §7.7，**原文照抄仍是刻意不做**。
另外孤儿工具结果这条判定只在"窗口被人为调小"的探针路径上跑过，
日常 65536 窗口的任务不会触发折叠，因此线上默认配置下这条路径本来就是冷的。

### 7.3 第四项落地：一次调用同时写下三个口径（对标 §3 的 `token_usage_record`）

补这一条不是为了多写一张表，而是因为在此之前有两个问题是**回答不了**的：
「这一步到底花了多少」只能从单次事件读，「这个任务累计花了多少」只能在读出时对着
`model_usage` 求和推，而「缓存真的命中了吗」根本没有出口——三列在库里躺着，
事件、证据包、界面三层都看不见它。Codex 的解法是一次调用把三个口径同时写下来，
本轮照这个形状做。

实现：

- `services/api/src/blank_api/services/agent/graph.py`：
  `AgentSession.turn_usage`（本回合累计，随会话走）+ `_task_usage_totals()`（按 `task_id`
  现查一次 SQL 聚合，跨会话把子代理/摘要/审查员都算进来）+ `_emit_usage()`。
  **更正（写 §7.4 时回查库发现的）**：那句"把子代理算进来"当时只是聚合口径成立——子代理压根没有落库的行，
  所以线上任何一条派了子代理的任务，任务级都少算了派出去的那部分。补齐见 §7.4。
  落点只有一处：`_record_usage()` 在 `session.commit()` 之后紧接着 merge 回合级并发
  `model.usage` 事件。放在这里而不是各个调用点，是为了让**摘要、审查员、失败的调用**
  也都有这条记录——第一版我把 `_emit_usage` 同时写在 `call_model` 的两个分支里，
  结果是一次调用双发事件、回合数翻倍，靠 `len(events) == len(rows)` 那条判定才抓住。
- payload 形状：`role` / `trace_id` / `depth` / `agent_path` / `call` / `turn` / `task`，
  其中 `call` 额外带 `model` / `provider` / `latency_ms` / `status`。
  `accumulate_usage()` 补了 `total_tokens`，三级于是同形、可以逐项比单调。
- `evidence.py`：usage 块新增 `cached_tokens` / `cache_write_tokens` / `reasoning_tokens` /
  `cache_hit_ratio` / `records`（=`model.usage` 事件条数）/ `levels`（最后一条事件的三级原样带出，
  老任务是 `null` 而不是补一个像是算过的数）。
- 顺带修了一个真 bug：证据包求和用的是 `select(ModelUsage, ModelProvider.name).join(...)`
  **内连接**——供应商行被删或 `provider_id` 为空时，那笔用量在求和里静默消失，
  证据包就会说"这个任务没花 token"，而同期事件里的任务级明明记着。改成 `outerjoin`
  并让 `providers` 过滤空名。这条由老任务用例（`provider_id="provider-1"` 在表里不存在）钉住。
- 前端：`api.ts` 的 `UsageInfo` 增 5 个字段与 `levels`（缺项一律 `null`，不当成 0）；
  `status.ts` 新增纯函数 `usageCacheLabel()`（没有分母就不给百分比）与
  `usageTrailLabel()`（缺项写 `—`，不写 0）；`evidence-panel.tsx` 的「Token 用量」区块
  新增「缓存命中 / 推理 / 三级口径」三行。`model.usage` 不进对话流（一步一条，
  摊出来会把执行过程淹没），由 `timeline.test.ts` 的同族用例与新的 `usage.test.ts` 一起钉住。

证据（按"每条判定都能失败"）：

- 后端新用例 `tests/test_usage_levels_v17.py`：**10 passed**。钉住三级齐全与单调、
  **事件条数 == 表行数**、任务级跨会话而回合级不跨（子代理那一步的 700 不能算进根代理的回合）、
  失败调用同样进账、摘要与审查员路径各有一条、`call ⊆ turn ⊆ task` 逐项单调、
  证据包求和与事件任务级同源、老任务 `levels is None`、字段名即跨语言契约。
- 六次变异，各红各自那条：回合级不 merge（3 红）／任务级不查库只退回回合（1 红）／
  证据包退回内连接（2 红，含老任务那条）／`accumulate_usage` 去掉 `total_tokens`（3 红）／
  失败调用直接 return（1 红）／任务级聚合只算 `status="ok"` 的行（1 红——这条最阴，
  它会让"发了 5 次请求、1 次失败"读成"花了 4 次的钱"）。全部改回后复绿。
- 前端新用例 `web/src/components/tasks/usage.test.ts`：**7 passed**（全量 102 passed）。
  五次变异各红各条：去掉 `reasoning_tokens` 映射（1 红）／去掉 `cache_write_tokens` 映射（1 红）／
  `levels` 缺失时编一个 `{call:null,turn:null,task:null}` 空壳（2 红）／缓存标签去掉分母守卫（1 红）／
  三级口径把缺失项显示成 0（1 红）。
- 真实链路：新建 `evals/usage_probe.py`，在 `:8061` 上用 DeepSeek 真跑一条三步任务，
  **七项全 PASS、`rc=0`**，任务 `0aae1a61-…` `status=succeeded`。实测数字：
  4 次调用、`prompt 14,458 / cached 10,496`（命中率 72.6%，最后一次单次 4,275 里 3,968 走缓存 = 93%）、
  `total 14,753`、`latency 4,235ms`。这一条把 §3 那句"只能说我们打开了 prompt_cache"
  变成了可核对的事实。
- 负对照：同一装置只换任务号，打上线前的老任务 `042d5319-…` → **6 条 FAIL、`rc=1`**
  （它 6 行用量、`cached 11,392 / prompt 14,821`，求和与命中率照旧对得上，
  所以红的全是"这批事件当时不存在"那几条）。
- 浏览器点验：**真 Chrome（headless），视口 1440×900**，dev 实例 `:3121`，
  右侧「证明」面板用 DOM click 切换（这台机器指针点击不可用），**11 项 ALL PASS**。
  实测行文本：
  `缓存命中 73% 走缓存（10,496 / 14,458）`、
  `三级口径 单次 4,333 · 本回合 14,753 · 任务累计 14,753`、`推理 0`、`调用次数 4`；
  负对照任务同一段显示 `缓存命中 77% 走缓存（11,392 / 14,821）` 而 `三级口径 暂无`。
  两个数都从 `/api/v1/tasks/{id}/evidence` 现取再与 DOM 对比。
  装置自检两次变异：把 `cachedTokens` 映射到 `prompt_tokens` → 只红「缓存命中行的数来自证据包」
  和负对照那条；把三级口径三档都填任务累计 → 只红「三级口径」那条。改回后 11/11 复绿。
- 全量回归：后端 **728 passed / 14 skipped**（+10），前端 **102 passed**（+7），
  `tsc --noEmit` 无输出。

如实说明未覆盖与不整齐的部分：

1. 这条任务只有一个 `AgentSession`，所以实测的 `turn` 与 `task` **数值相等**（14,753 = 14,753）。
   两者分叉的形状只在后端用例里证过（子代理那一步），线上还没跑出一条真分叉的任务；
   团队/子代理链路的三级对照留到下轮。→ **下轮已完成，见 §7.4**：线上一条真出子代理的任务
   跑出了 `本回合 22,275 ≠ 任务累计 26,505`，缺口关闭。
2. `cache_write_tokens` 恒为 0、`reasoning_tokens` 恒为 0：前者是 DeepSeek 的
   `prompt_tokens_details` 里没有写缓存写这一项，后者是这次用的 `deepseek-chat` 不是推理模型。
   字段留在那里是为了"换模型就能看见"，现在还不能声称它们在工作。
3. 没抄 Codex 的 `response_id`（供应商返回的响应 id）与 `root_turn_id`：`trace_id` 已经能定位到
   任务与尝试次数，但**拿不到供应商侧的 request id**，所以"这次请求在 DeepSeek 那边是哪一笔"仍然回答不了。
   → **`response_id` 已闭环，见 §7.5**；`root_turn_id` 仍缺（我们的 `trace_id` 里带 attempt 序号，
   重试链能还原到"同一任务的第几次尝试"，但"哪一次派生了哪一次"的父子根还没有一等字段）。
4. 孤儿用量的两种数据库行为不一致：本项目没有任何地方执行 `PRAGMA foreign_keys=ON`，
   所以 SQLite 里删供应商会把用量行留成孤儿（本轮的 `outerjoin` 正是为它兜底）；
   而 `ModelUsage.provider_id` 的 FK 声明是 `ondelete="CASCADE"`，在 MySQL 上删供应商会**连用量一起删**。
   同一份账在两种引擎上结局不同，这是个独立的决定（改 FK 语义 or 禁止删供应商），本轮只记录不动。

### 7.4 第五项落地：派出去的子代理进同一本账（补 §7.3 缺口 1，对标 §3 的三口径）

§7.3 的三级累计只覆盖主代理：`_record_usage()` 是 `AgentSession` 的方法，而子代理走的是
`services/subagent.py` 里另一条循环——它只在内存里累计 `Usage()` 并把结果交回主代理的 state，
**既不写 `model_usage` 行，也不发 `model.usage` 事件**。
这条不是推测，三处可查：
① `git show HEAD:services/api/src/blank_api/services/subagent.py | grep -c "ModelUsage\|model.usage"` → **0**
（HEAD 即 #33 之前那份代码），且它内存里的 `usage` 只有 `prompt_tokens/completion_tokens/calls` 三个键，
连缓存与推理都不累计；
② 安装包里的旧代码（`desktop/dist/win-unpacked/resources/backend/.../services/subagent.py`，打包于 2026-10-18）
`ModelUsage` 同样出现 0 次；
③ 改完之后回查 `:8061` 的库，全库 `trace_id` 不是 `task:` 开头的用量行只有 3 条，
全部来自本轮那条派了子代理的任务（`bc6c26f6`），此前一条都没有。
所以 §7.3 里"任务级现查 SQL 聚合，跨会话把子代理算进来"只是**聚合口径成立**，
线上根本没有子代理的行可聚合——事件流、证据包、面板三处报的任务总账一直少算了派出去的那部分。
Codex 的 rollout 里 `token_usage_record` 是线程级还是会话级一眼可辨（带 `agent_path`），本轮把这个形状补齐。

实现：

- 抽出唯一落账处 `services/api/src/blank_api/services/agent/usage.py`：
  `accumulate_usage()` / `merge_usage()` / `task_usage_totals()` / `account_call()`。
  `account_call()` 里**落库与发事件是同一个动作**（先 commit 行，再按 `task_id` 现查任务级、
  发 `model.usage`），这样"表里有行但事件里没有"和"事件里有但表里没行"两种漏账都只可能同时发生。
  顺序是有意的：任务级必须在自己的这一行落库**之后**聚合，否则事件里的任务级永远少算自己这一笔（M5 变异）。
  `session_factory` 为空时事件照发、任务级退回本回合——那是离线评测 harness 的形状，
  不能因为拿不到库就整条省略。
- `graph.py`：`_record_usage()` 退化成一次 `account_call()` 调用，
  原来那份内联的落库+发事件与 `_task_usage_totals()`/`_emit_usage()` 一起删掉；`accumulate_usage`/`merge_usage` 从 `graph` 导出不变，
  `test_protocol_cache_v14.py` 的既有 import 不受影响。
- `subagent.py`：子代理循环里加 `account()` 闭包，成功与 `ModelError` 两条路都走它
  （失败也进账，理由同 §7.3）；`run_subagent()` 新增 `provider_id`/`provider_name` 参数，
  由 `_session_provider(session)` 从父会话取出（子代理角色可能挂着另一个供应商）。
  并行 `fan_out` 在层内计算一次供应商，不在每个子任务里重复读父会话。

顺带抓出两个真 bug（都不是为了过测试而改的）：

1. **并行子代理会静默丢事件**。`EventRecorder.add()` 是 `SELECT max(seq) → INSERT` 的读-改-写，
   `fan_out` 的多个子代理共用同一个 recorder，两个子任务同时算出同一个 `seq` →
   `IntegrityError: UNIQUE constraint failed: agent_events.task_id, agent_events.seq`。
   时间线是唯一证据来源，丢一条就是那段执行过程在账本上根本不存在。
   修法：`events.py` 里对 `IntegrityError` 做有界重试（`MAX_SEQ_ATTEMPTS = 5`，每次 `await asyncio.sleep(0)` 让出）。
   新用例 `test_parallel_children_do_not_lose_events_to_the_seq_race` 钉住，M7（把重试次数改成 1）证明不修就会被抓到。
2. **不透传供应商时，子代理的钱挂不上账号**。首轮实现漏了 `provider_id`，
   `ModelUsage.provider_id` 是 `nullable=False` ——要么写空串成孤儿行，要么整条落库失败。
   M8 变异（`_session_provider` 返回空）证实这条判定能失败。

证据（按"每条判定都能失败"）：

- 后端新用例 `tests/test_subagent_usage_v18.py`：**9 passed**。钉住子代理发事件且行数==事件数、
  三级齐全单调、`depth`/`agent_path`/独立 `trace_id` 的身份、**父回合与任务级真分叉**
  （`turn.prompt_tokens == 1100` 而 `task.prompt_tokens == 1600`、`task.calls == 4`）、
  失败调用进账、`session_factory=None` 时事件照发、payload 字段名即跨语言契约。
  与用量相关的同族用例同跑（v18 + v17 + `test_worktree_parallel_v13` + `test_subagent_v12` +
  `test_context_layers_v12`）：**48 passed in 11.07s**，日志 `E:\tmp\realmodel\pytest-v18-5.log`。
- `test_worktree_parallel_v13.py` 的离线 harness 补了 5 行断言（3 条 `model.usage`、三级齐全、
  `depth==1` 且 `agent_path` 形如 `root/par-*`、`task == turn`）。不加的话，
  那条用例对"子代理一条账都不发"这件事是完全无感的——它只验结果文本。
- 八次变异，**8/8 CAUGHT**：M1 不落账／M2 行不挂 `task_id`／M3 借用父 `trace_id`／
  M4 失败调用不进账／M5 先算任务级再落库／M6 事件不带真实身份／M7 seq 竞态不重试／M8 不透传供应商。
  变异脚本每次只破坏一处、跑完立即还原原文，baseline 与还原后各跑一次 `test_subagent_usage_v18.py`
  （9 条），两个 `rc=0` 才认这八条。
  第一轮 M6 是 **MISSED** 的——我原本变异 `account_call(agent_path=...)`，但子代理的 `_Recorder.add()`
  会用 `{**payload, agent_path, depth, parent_thread_id}` 把它覆盖掉，那是一条**行为中性**的变异，
  不能当证据；改成变异 `_Recorder` 构造本身（`agent_path="root", depth=0`）才真的抓到。
- 真实链路：新建 `evals/subagent_usage_probe.py`（只用 HTTP，不 import 被测代码），
  在 `:8061` 上用 DeepSeek 真跑一条"必须派子代理去读文件"的任务，
  任务 `bc6c26f6-a363-…` `status=succeeded`，**八项全 PASS、`rc=0`**。
  实测：44 条事件 / 8 条 `model.usage`，其中 **3 条来自 `depth=1` 的 `root/sub-1`**；
  `prompt 26,026 / completion 479 / total 26,505 / cached 17,024`（命中率 65.4%）、`latency 7,501ms`、
  证据包 `records == 事件条数 == 8`、求和 == 最后一条事件的任务级；
  两个 `trace_id`（父 `task:bc6c26f6…:attempt:1` 与子线程 `ff16b5a6…`）并成一本账。
  **线上第一次跑出真分叉**：最后一条事件 `turn.total_tokens 22,275` ≠ `task.total_tokens 26,505`。
- 负对照：同一装置只换任务号，打 #33 之前的老任务 `0aae1a61-…` → **5 条 FAIL、`rc=1`**
  （4 条用量事件、`depth>=1` 为 0，所以"派了子代理/进了账/带身份/真分叉"四条必然红；
  三级齐全、行数==事件数、求和同源这三条照旧绿，说明红的是缺事件而不是账本坏了）。
  如实说明：那次运行里"找一个没有子代理的历史任务作对照"这条也红了，原因是它**没找到可用对照任务**
  （`control_task=` 为空——列表里除本次任务外，有用量事件的候选都被 `subagent.*` 过滤掉了），
  属于装置自身的可辨识度不足，不是断言被推翻；面板点验里改用显式 `CONTROL_TASK_ID` 避开了这个歧义。
- 浏览器点验：**真 Chrome（headless），视口 1440×900**，dev 实例 `:3121`（代理到 `:8061`），
  `E:\tmp\realmodel\subagent-panel-verify.mjs`，**11 项 ALL PASS、`rc=0`**。
  面板实测行：`三级口径 单次 5,704 · 本回合 22,275 · 任务累计 26,505`、`调用次数 8`、
  `缓存命中 65% 走缓存（17,024 / 26,026）`、`合计 26,505`；
  负对照同一段是 `单次 4,333 · 本回合 14,753 · 任务累计 14,753`、`调用次数 4`。
  关键三条：本回合 ≠ 任务累计（分叉看得见）、任务累计 == 面板合计（派出去的钱并进了同一本账）、
  调用次数 == 接口事件数且 `depth>=1` 的有 3 条（子代理真的被算进来）。
  期望值全部从 `/api/v1/tasks/{id}/{evidence,events}` 现取，脚本没有一个写死的数字；
  取证通道断了（没有 `levels` 或没有 `depth>=1` 事件）时脚本自己先红再退出。
  第一版有一条断言指向面板上并不存在的「供应商」行 → 它红了，改成
  "8 条事件的 `call.provider`/`call.model` 与证据包同源 + 面板「模型」行一致 + 父子各有 `trace_id`"。
- 全量回归：后端 **737 passed / 14 skipped**（较 §7.3 的 728 增 9 条，正是 v18 新增的 9 条用例；
  v13 那条并行用例是原地加断言，不增条数）。
  本批未改前端代码（三级显示在 #32 已完成），故未重跑 `npm test`/`tsc`；改的是"同一行现在真的把子代理算进来了"，
  用上面的真浏览器判定来证。

如实说明未覆盖与不整齐的部分：

1. **并行 `fan_out` 的工具结果里根本没有用量**。这次补的是"账本侧"（`model_usage` 行 + `model.usage` 事件）。
   `SubagentResult.usage` 只有单个子任务自己的回合累计，而 `services/tools/delegate.py:_spawn_agents`
   拼给主代理的 payload 只有 `agent_path/ok/steps/summary/error/merge/diff`，**不带 `usage`**。
   也就是说：钱记进了同一本账，但主代理下一步做上下文预算时看不见兄弟分支刚花掉的那部分。
   （**任务 #45 已补这一条**：`spawn_agents` 的负载里现在有 `usage` 与 `parallel_group`，
   预算按 `自己 + 派出` 判，四层证据见 §7.10。）
2. 团队链路（多成员各有 `AgentSession`）没在这轮真跑：`account_call()` 用的是 `task_id` 聚合，
   形状上天然支持，但"三个成员各花多少、任务级是否等于三者之和"目前只有离线用例级别的依据。
3. `cache_write_tokens` 与 `reasoning_tokens` 在这条任务里仍恒为 0（原因同 §7.3 第 2 条），
   子代理路径同样没有新的观测能力。
4. 事件 seq 的修法是**有界重试**，不是唯一序号生成器。5 次之内抢不到仍会抛；
   真正的解法是把 `seq` 交给 DB 自增或事务内 `SELECT ... FOR UPDATE`，那属于事件层的独立改造。



| Codex 有 | 没抄的原因 |
| --- | --- |
| `approvals_reviewer`、`disabled_plugin_ids` | 我们没有插件市场与多审阅人身份，编字段等于造没人写的空列 |
| `permission_profile` 逐条 ACE + `missing_path_behavior` | **已补（#77，见 §7.38）**，但不是抄：逐条 ACE 与 `missing_path_behavior` 都有，每条额外带 `enforced_by`——本机没有 ACL 层时如实写 `policy`，不冒充内核 ACL |
| `sandbox_policy.type` 的 `workspace-write` 命名 | 我们的 `SandboxPolicy` 已有语义，再取一个别名只会让两套名字漂移 |
| DPAPI 存账号口令 | 立项阶段先做设计；抄之前要回答"我们是否要引入本地账号"这个更大的决定 |
| `custom_tool_call` 与 `function_call` 双轨（496 : 5） | 那是它插件/自定义工具生态的产物，我们只有一种工具调用，没有对应需求 |
| 每回合都写一条 `turn_context`（93 条 / 3 会话） | 我们按 `digest` 去重，只在策略真的变化时写。审计要的是"什么时候变了"，重复行只增加噪声与体积 |

### 7.5 第六项落地：供应商那一笔请求的 id（补 §7.3 缺口 3，对标 §3 的 `response_id`）

Codex 的 `token_usage_record` 里除三级口径还有一枚 `"response_id":"resp_0802…"`。它的用处不在计费，
在**出事之后能去供应商那边把这一笔调出来**：限流、内容审核、计费对不上时，供应商客服只认 request id。
我们原来只有 `trace_id`（本地形状 `task:<uuid>:attempt:<n>`），它说得清"我们这边的第几次尝试"，
说不出"那是 DeepSeek 那边的哪一笔"。

落地的形状与 §3 同一处收口：适配器取 id → `ModelResponse.response_id` → `account_call()` 一次写两处
（`model_usage.response_id` 列 + `model.usage` 事件的 `call.response_id`）。证据包不用改代码——
它本来就把最后一条三级记录原样带出，所以这一枚自动进了「证明」面板。

**实测纠正一处想当然**：id 的形状不是通用的。OpenAI 给 `chatcmpl-<hex>`，而 DeepSeek 真实返回的是
**裸 UUID**（`a4ec27d7-cdd7-4f33-9696-bd813dd75025`），且 `x-request-id` 响应头为空。
所以：① 判定只问"像不像一枚 id"（UUID 或 `chatcmpl-` 两种形状都收），不把某家前缀写成通用断言，
否则换一家就误红；② 响应头那路 fallback 对 DeepSeek 是走不通的，它只对"帧里不带 id、只在头里给"
的自建网关有意义（离线用例覆盖这一路）。

四家的取法各不相同，且**取不到就写空串**——空串是"这家没给"这个事实，不许拿我们的 uuid 顶上
（一个像是 id 的字符串会让人去供应商后台搜一个根本不存在的请求，比空着坏得多）：

| 适配器 | id 来源 | 取不到时 |
| --- | --- | --- |
| `openai_compat` 非流式 | 响应体 `id` | 空串 |
| `openai_compat` 流式 | 任一帧的 `id`，退回 `x-request-id`/`x-dashscope-request-id` 头；网关不认 stream 而整包返回时走 `_parse_completion` | 空串 |
| `openai_responses` | 非流式 `id`；流式取 `response.completed` 帧的 `id`（`finish_reason="incomplete"` 那一路没有 completed 帧，就是空串） | 空串 |
| `anthropic` | 非流式 `id`；流式取 `message_start.message.id` | 空串 |
| `ollama` | 本地进程没有这一枚 | 空串（源码里写死这条理由） |

验证分三层，每层都能失败：

1. **离线 12 条**（`tests/test_response_id_v21.py`）：三家方言各自的 id、流式退响应头、整包回退、
   没有 id 时是空串、账本行与事件同源、失败调用照进账且 id 为空、摘要那一路（不走 `call_model`）也带 id、
   子代理带自己那一笔的 id。变异跑过三种：把 `_parse_completion` 的 id 抹成空串 → 3 条红；
   只抹 `graph.py`/`subagent.py` 的转发 → 3 条红；把"没有就造一个"塞进流式分支 → 恰好 1 条红
   （就是那条不许造的用例）。
   这轮还**撞出我自己的用例一个隐性抖动**：用量行按 uuid 主键排序去比顺序，同一份代码两次跑出
   一绿一红。改成按 `node` 归位后才是可依赖的断言——这条记下来，因为"绿"本身不证明断言可靠。
2. **真线上 7 条**（`E:/tmp/m1b/response_id_check.py`，DeepSeek）：7/7。关键两条是
   「同一次原始 httpx 请求的响应体 `id` 与我们解析出的 id 逐字符相等」与
   「两次请求给出两个不同 id」——后者专防缓存/常量/自造这三种假通过。
3. **真任务链路 + 真浏览器**：`response_id_task.py` 5/5（5 条用量行全部非空、5 个互不相同、
   行与事件按节点完全一致、不与任何本地标识重合）；`response_id_ui_verify.mjs` 5/5
   （新任务的「证明」页那一行印的是 `7d7a4739-c8da-4098-bd8f-2c31e00bdc7c`，
   而字段上线前跑的老任务同一行印「暂无」）。变异：把那一行改成印模型名 → U3/U4 同时红，
   界面上"把相邻字段当 id"这种错是能被抓住的，不是只能靠人眼看。

全量回归：后端 **772 passed / 14 skipped**（较 §7.4 的 737 增 35 条 = 本批 12 条 + 沙箱那批 23 条），
前端 **114 pass / 0 fail**，`tsc --noEmit` rc=0。迁移 `0018_usage_response_id` 在自己的隔离库上
`alembic upgrade head` 跑通，加列 + `ix_model_usage_response_id`；**存量 727 行 id 全为空串**，
这是"当时没记"，不是"当时没花"，回填补值一律禁止。

如实说明未覆盖：

1. `root_turn_id` 仍没有：重试的父子关系靠 `trace_id` 里的 attempt 序号推，不是一等字段。
2. 供应商后台能不能拿这枚 id 查到内容，取决于对方的留存策略——本轮只证明"我们记的是他们给的那一枚"，
   不声称"拿它一定能调出那笔请求"。
3. 治理审阅人（guardian）那一路用量仍写空串：它的 `Verdict` 结构不带 id，要接得先改 guardian；
   空串在这里同样是事实（那一笔的 id 我们确实没拿到）。
4. 老任务（字段上线前）在界面上是「暂无」，无法回填。

复现（三条都在这台机器上跑过）：

```bash
# 1) 离线
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest tests/test_response_id_v21.py -q
# 2) 真供应商：同一次请求的字节级保真 + 两次请求两个 id
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/response_id_check.py
# 3) 真任务链路 + 真浏览器（先 instance.py start off，再起 BLANK_WEB_DIST_DIR=.next-8062 next dev -p 3123）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/response_id_task.py
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/response_id_ui_verify.mjs
# 变异：把 evidence-panel.tsx 那一行的 responseId 换成 models[0] → U3/U4 必须同时红
```

### 7.6 第七项落地：环境快照成为一条全量记录，指令用指纹指认（补 §6 的 `world_state` 那一行，对标 §4）

拆 §4 时最反直觉的一点：Codex 的 `world_state` 里**没有指令正文**，`collaboration_mode.instructions`
放的是一个 40 位十六进制哈希。它把"当时模型读到的是哪一版 AGENTS.md"变成了一次数值比较——
几万字的上下文要判"变没变"，不需要抄原文。我们照这个手法做，并用实测证明它值这个改动。

改之前先量（`E:/tmp/m1b/world_probe.py`，隔离实例 :8062 的库，19 条历史事件）：
一条 `turn.context` **4145 字节**，里面是 `InstructionLayer(source='project', path='E:\\tmp\\…\\AGENTS.md', …)`
这种 dataclass repr——正文、主机绝对路径、用户名三样一起进了事件流、证据包和全文索引。
这不是"多存了点字"：证据包会被复制、时间线会被搜索，路径与正文就在这条链上往外走。

实现（口径仍然只有一处）：

- `services/instructions.py`：`content_sha()`（sha256 前 40 位，**算的是注入文本**，不是磁盘原文件——
  文件被截断时账本要说"模型当时看到了什么"）、`audit_path()`（绝对路径一律掩码，`task` 这类逻辑名原样保留）、
  `InstructionLayer.as_dict()` 改成语义化的 `{source, path(掩码), chars, sha, truncated}`。
- `services/agent/governance.py`：`layer_ref()`（turn.context / world.state / instructions.loaded **共用**这一个引用口径）、
  `instructions_digest()`（顺序算在内：同样两层换顺序提示词就不同）、`environment_snapshot()`
  （cwd 掩码、shell 只留 basename、PATH 只留**条数**、env 只留**变量名**）、
  `world_state()` 与 `world_state_digest()`（秒级时钟一律不算进指纹）。
- `graph.py`：`emit_world_state()`，去重后落 `world.state` 事件；
  调用点刻意放在**窗口对齐之后**——放在对齐之前，首条快照会说 `context_window: 0`，
  而那是"还没同步"而不是"没有窗口"。这条顺序是被变异 M8 逼出来的（见下）。
- 与 Codex 有意不同的一处：`operating_mode.model` 记会话默认模型，不记逐路由模型。
  路由事实属于 `turn.context`；写进 `world.state` 会让每次换角色都产生一条"环境变了"的假快照。
- `evidence.py`：`world` 块（`snapshots` / `current`=终态那条 / `changes`）。
- `shell_env.py`：`ShellSnapshot.as_dict()` 补掩码（真链路 W6 抓出来的既有泄漏，见下）。
- 前端：`api.ts` `normalizeWorld()` + `world.test.ts`；`evidence-panel.tsx` 新增「当时的环境」区块
  （工作目录 / 系统·shell / 解释器·PATH N 条 / 上下文窗口·N 触发压缩 / 指令版本=指纹前 12 位 /
  逐层 `source · path · chars 字 · sha8` 折叠明细）。

证据（每条判定都能失败）：

- 后端 `tests/test_world_state_v22.py`：**13 passed**。指纹对得上独立复算的 sha256、改一个字节指纹就动、
  同一份内容换个路径指纹不变、`instructions_digest` 对顺序敏感、无 shell 时 `status=="unknown"`、
  秒级 `created_at` 不算新环境而 PATH 条数算、形状钉死为 `full:true` + 五个 state 键、
  事件必须先于真调用落下（把 `compactor.build` 打爆炸也要有那条）、证据包取的是终态那条。
- 指纹对得上真文件这一条差点自己骗自己：Windows 上 `Path.write_text` 会把 `\n` 译成 `\r\n`，
  读回来再算就多了行尾，独立复算与实现值差一位——改成 `write_bytes` 才是同一份字节。
  **这是三个自己给自己挖的坑之一**，另两个：① `str(dataclass)` 会把反斜杠翻倍，用未转义的路径
  去子串判断会漏报（探针 W3/L3 第一版就假阴了）；② 只测构造函数不测接线，M6 变异跑了个绿。
- 变异门禁 **10/10 全被指定测试抓住**，每轮还原后字节一致（`E:/tmp/m1b/world_mutate.py` →
  `world-mutate-4.log`）：M1 快照退回抄正文 / M2 指纹写死常量 / M3 环境快照带完整 PATH /
  M4 把 created_at 算进指纹 / M5 换指令不发第二条 / M6 摘掉 graph 里的接线 /
  M7 证据包把最早一条当现状 / M8 快照挪回窗口对齐之前 / M9 内联指令层把正文塞进 `path` /
  M10 shell 快照放弃掩码。**M6、M8、M9 三条是它抓出我自己的缺陷**，不是设计时想到的。
- 前端变异 **5/5**（`world_mutate_web.py`）：数字键缺失冒充 0 / 投影整个摘掉 / 指纹读错键 /
  `current` 取第一条 / 老任务 null 变空对象。`world.test.ts` 4 passed，`tsc --noEmit` rc=0。
- 真实模型链路（一次 DeepSeek 真任务，`E:/tmp/m1b/world_state_verify.py` → `world-verify-2.log`）**12/12 PASS**：
  W3 事件里的 sha == 我独立算的 `0e394ba3…`（同一份 54 字 AGENTS.md）；W4 全事件流再无 dataclass repr；
  W5 快照里无正文；W6 快照类事件里**无主机路径**；W7 去重生效（4 次调用 1 条快照）；
  W8 `turn.context` 单条从 4145 降到 **1770** 字节；W9 证据包终态
  `cwd:"<workspace:ws-world>"`、`shell:"cmd.exe"`、`os:"Windows 11"`、`path_entries:26`；
  W10 `instructions.loaded` 字段恰为 `{chars, path, sha, source, truncated}`；W11 库里行同样干净。
  W6 第一次是**红的**：它抓到 `shell.snapshot` 这条既有事件一直原样带着真实 cwd、解释器路径和 26 条
  Windows PATH（`C:\Users\<用户名>\…`）。查清消费者只有 `runtime.py` 的发事件处和一处测试，
  才动 `as_dict()`，并同步把 `test_handoff_v12.py::test_snapshot_records_shape_not_secrets`
  从"钉死未掩码形状"改成"钉死掩码后的形状"——那条断言原本就是在给泄漏背书。
- 浏览器点验（Chrome headless，视口 **1440×900**，第二个 dev 实例 `:3123`
  （`BLANK_WEB_DIST_DIR=.next-8062`、`API_PROXY_TARGET=http://127.0.0.1:8062`），
  未触碰用户常驻的 `:3000`/`:8000`/`:8061`）：`world_ui_verify.mjs` **6/6 PASS**。
  那一节实测渲染 `工作目录 <workspace:ws-world> / 系统 · shell Windows 11 · cmd.exe / 解释器 python.exe · PATH 26 条 /
  上下文窗口 65536 · 49152 触发压缩 / 指令版本 dba20b1f95b7 / project · <workspace:AGENTS.md> · 54 字 · 0e394ba3 /
  共 1 条环境快照…`；V4 用字段上线前跑的老任务做 A-B：`snapshots=0` 时**整节不出现**
  （不能把"当时没记"渲染成"当时环境为空"）；V5 断言节内无 `E:\` / `C:\Users` / `D:\` 形状。
- 全量回归：后端 **785 passed / 14 skipped**，前端 **118 pass / 0 fail**，`tsc` rc=0。

如实说明未覆盖：

1. `world.state` 只在时间线的证据包里出现，**没有单独的时间线卡片**——与 `turn.context` 的先例一致，
   要做就得同时定义"第几步换了环境"怎么在人话里说。
2. 老任务（本批上线前）库里没有 `world.state`，界面上整节缺席，无法回填。
3. `full:true` 目前是"每条都是全量"，没有 Codex 那种 `replacement_history` 拼接语义——
   替换历史的**结构与逐条指纹**已在任务 #42 落地（§7.7），原文照抄仍是刻意不做；
   `root_turn_id` 仍未做。
4. 环境快照取的是**规划时**的环境（`shell` 层来自会话级快照），不是每条命令 fork 前的实时探测；
   中途 `os`/`machine` 变了（远程执行器那种）这一版看不出来。
5. `expose_workspace_paths=1` 时全部字段还原成真路径：那是操作者本机的显式选择，本轮未测其证据包外流路径。

复现：

```bash
# 1) 离线
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest tests/test_world_state_v22.py tests/test_handoff_v12.py -q
# 2) 后端变异门禁（10 条）/ 前端变异门禁（5 条）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m1b/world_mutate.py
cd ../web && PYTHONIOENCODING=utf-8 ../services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/world_mutate_web.py
# 3) 真链路 + 真浏览器（先 E:/tmp/m1b/instance.py start，再起 API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 next dev -p 3123）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/world_state_verify.py
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/world_ui_verify.mjs
```

### 7.7 第八项落地：压缩窗口链与替换历史引用（对标 §4.1 的 `compacted`）

任务 #42。§4.1 拆到的实物是：Codex 每次压缩写一条 `compacted`，里面有
`window_number`（1→2→3 递增）、`window_id`/`first_window_id`/`previous_window_id`（uuid7，
出生窗 `previous == first == birth`）、`compaction_response_id`（同一次压缩的多条记录指向同一笔调用）、
`replacement_history`（**整段替换后的历史原文**，实测单条 307,553 字节）、`retained_context`、
`guardian_history` 与 `latest_token_usage_record.root_turn_id`。

我们的裁决是**借结构，不借抄正文**：这些事件会进证据包、对项目成员可见、还被全文索引，
把整段历史原文再抄一遍等于把同一份内容存 N 份并把泄漏面扩大一个量级。所以做的是
"窗口链 + 逐条引用（位置/角色/种类/字数/内容指纹）"，正文一条都不搬。

实现（`services/agent/context.py`、`graph.py`、`services/evidence.py`）：

- `message_ref()` → `{index, role, kind, chars, sha}`；`kind` 把 `summary`（摘要）、
  `handoff`（交接笔记）与 `tool_call`/`tool_result`/`role` 分开——之前这两类只能靠 role 猜。
- `replacement_record(before, after, ...)` → `{before_items, after_items, retained, added, dropped, items}`。
  **差异按内容 sha 判定，不按消息身份**：我们没有持久化的消息行，位置相同不代表内容相同。
- `ContextCompactor` 带上 `window_number`/`window_id`/`first_window_id` 与 `open_window()`，
  窗口号**只在真的发生折叠时推进**（不是每次装配都开新窗）。
- 事件发射条件收紧成 `if report.folded or self.compactor.covered > covered_before:`，
  载荷是 `{**report.as_dict(), "folded": …, **window, "replacement": …, "compaction_response_id": …}`；
  `compaction_response_id` 在调用摘要模型前重置、调用后从 `response.response_id` 取，
  与 #40 那套供应商指针同源——**存指针，不复制账单**。
- 证据包 `context` 块新增 `windows[]`（`seq`/窗号/窗 id/上一窗/压缩调用/retained/dropped/items）
  与累计 `dropped`：**只累计条数，绝不把 items 再抄一份**。
- 前端（`status.ts` / `timeline.ts` / `timeline-blocks.tsx`）：缺失的数值键投影成 `null` 而不是 `0`
  ——「没说」不能渲染成「第 0 窗」或「一条都没保留」；引用数组只认那五个键；
  压缩卡片折叠区逐条列出丢弃/新进/替换后的历史，一行一条 `#序号 · kind · 字数 · 指纹前 8 位`。

验证证据（四层，每条判定都能失败）：

- 离线：后端 `tests/test_compaction_window_v23.py` 10 条；前端 `web/src/components/tasks/window.test.ts` 10 条。
- 后端变异门禁 `E:/tmp/m1b/window_mutate.py` **10/10**（日志 `window-mutate-1.log`），
  每条都指定"该被哪条测试抓"，还原后字节一致且两组复绿。
- 前端变异门禁 `E:/tmp/m1b/window_mutate_web.py` **7/7**（日志 `window-mutate-web-2.log`），
  含 F1/F2 那对"缺失读成 0"、F4"丢弃条数取自新进数组"、F7"删掉倍数分支就印出 579%"。
- 真实链路 `E:/tmp/m1b/window_chain_probe.py` **C1–C9 九项全 PASS**（`rc=0`，
  日志 `window-chain-1.log`，实物 `window-chain-55578136.json`）：DeepSeek，窗口临时 65536→1600、
  跑完恢复，工作区绑到 `E:/tmp/m1b/ws-window` 并在 `finally` 里还原激活位。
  任务 `55578136-1965-4f86-8d12-95ad39b6de67` `status=succeeded`，56 条事件、4 次压缩、4 条压缩用量、
  48 条引用；窗链 1→2→3→4 首尾相接且出生窗恒为 `49da04bb…`（第 1 窗的 `previous == first`、
  `window_id` 是它自己 `060bbb97…`，正是 §4.1 的出生窗规则）；逐账
  `8→4 (保留 3 · 新进 1 · 丢弃 4)`、`11→4 (3/1/6)`、`15→7 (6/1/4)`、`17→6 (5/1/9)`，
  `retained + added == after_items == len(items)` 且丢弃的指纹不在留下的指纹里；
  证据包 `windows` 与事件逐字段相等（`compactions=4`、`boundary=12`、`tokens_saved=87466`）；
  四个 `compaction_response_id`（`deacdf75…`/`50650a44…`/`fb107712…`/`4f691658…`）
  全部落在 `node=context_compact` 那几行用量的 id 集合里，且没有一个等于 `window_id`；
  `kinds` 实测出 `summary` 与 `tool_result` 分得开；blob 里没有 `content` 字段、没有主机路径。
- 真浏览器点验 `E:/tmp/m1b/window_ui_verify.mjs` **7/7 PASS**（**Chrome headless，视口 1440×900**，
  dev 实例 `:3123` 代理 `:8062`，未触碰用户 `:3000`/`:8000`/`:8061`；日志 `window-ui-2.log`）：
  卡片数 == 压缩事件数（4==4，页面总卡片 10，控制台报错 0），逐卡印出
  「第 N 窗 · 接续 49da04bb…」「保留 x 条 · 丢弃 y 条」，引用行 48 条且形状
  `/^#\d+ · \S+ · \d+ 字 · [0-9a-f]{8}$/`，每卡恰好一个 `<details>`；
  A-B 负对照用字段上线前跑的老任务 `042d5319-…`（2 条压缩事件、0 条带链）：
  卡照样渲染，但 `第 \d+ 窗`、`保留 \d+ 条` 零命中——没记过的东西不能当场编出第 0 窗。

真实链路跑出来的第一个红是**界面缺陷，不是判定写错**：强制 1600 窗口下压缩前实测 9264 tokens，
`water_ratio = 5.79`，卡片原样印成「水位 579% · 窗口 1600」——一个看着像量表算错的数。
比率大于 1 在这里是事实（压缩前真的塞不下），所以 `compactionWaterLabel` 改成
`超出窗口 5.8 倍 · 窗口 1600`，先补一条会红的单测，再用变异 F7（删掉该分支必红）钉住，
浏览器侧新增 V7 从接口现算期望读法。实测四张卡读法 `5.8 / 10.7 / 15.3 / 11.8 倍 · 窗口 1600`。

顺带清掉一处**死代码**：`timeline.ts` 的 `describeSystem` 里有一条
`context.compacted` / `context.microcompacted` 的水位分支，读代码才确认它永远走不到——
该函数对任何 `type.startsWith("context.")` 先返回 `null`（timeline.ts:270-282），
而 `context.compacted` 在 `buildBlocks` 更早处已成卡。本来想"统一两处水位口径"，
真口径只有一处；与其留下第二个读法或再加一个走不到的分支，直接删。

两个门禁最初什么都没抓到，教训记在这里：

1. W3（让 `first_window_id` 随窗口滑走）第一次是**绿的**。它瞄的用例只做一次压缩，
   出生窗 == 当前窗平凡成立，换成任何错误实现都不会红。改瞄 `::test_second_window_points_at_the_first_one`
   （断言第二窗的 `first_window_id` 等于第一窗的）才红。
2. W9（证据包 `dropped` 从 `folded` 倒推）第一次也是**绿的**：那条测试只断言 `dropped >= 1`。
   改成与事件里各条 `dropped` 长度之和**精确相等**，并加装置自检
   `assert dropped_from_events != folded_from_events`（"测试装置失效：丢弃条数与折入条数相同，
   换成任何倒推实现都抓不住"）。自检第一次就报了 `22 == 22`，于是把 fixture 改成
   复制一条尾部工具输出（微压缩会让 `dropped = folded + 1`），装置才真的能失败，W9 随即变红。
   **弱断言等于没测，门禁自己要先能失败。**

如实说明未覆盖：

1. `replacement_history` **原文照抄刻意不做**（见上面的裁决）。要回放"被折掉的到底是什么"，
   现在能定位到"第几条、什么角色、什么种类、多少字、内容指纹是什么"，取不回正文。
2. Codex 的 `guardian_history`、`retained_context` 的来源语义（`retained_source`）没有对应实现；
   我们的 `retained` 只是"替换后仍在的条数"。（`guardian_history` 的**清单**由任务 #79 补上，
   正文仍按裁决不抄、`retained_source.message_id` 依旧写不出来：见 §7.40）
3. `compaction_response_id` 与用量行只做**集合归属**判定（指针 ∈ 该节点用量 id 集合），
   没有逐条一一配对——`model.usage` 与 `context.compacted` 之间没有共享的关联键可依赖。
4. "摘要调用失败前把 `compaction_response_id` 重置成空"这一步**没有可观测的事件路径**，
   因此没有任何测试能抓住"删掉这次重置"。这一条不声称测过。
5. §4.1 的 `latest_token_usage_record.root_turn_id`（以及 `parent_turn_id`）当时仍未成为一等字段
   ——**已由任务 #43 落地**，见 §7.8；那里的窗口链与本节的轮仍未共享键。

复现：

```bash
# 1) 离线
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest tests/test_compaction_window_v23.py tests/test_context_compactor.py tests/test_context_pairing_v14.py -q
cd ../web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/window.test.ts"
# 2) 后端变异门禁（10 条）/ 前端变异门禁（7 条）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_mutate.py
cd ../web && PYTHONIOENCODING=utf-8 ../services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_mutate_web.py
# 3) 真链路 + 真浏览器（先 E:/tmp/m1b/instance.py start，再起 API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 next dev -p 3123）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_chain_probe.py   # C1–C9 PASS, rc=0
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/window_ui_verify.mjs                              # 7/7 PASS
```

### 7.8 第九项落地：钱要答得出「哪一轮、谁引起的」（对标 §4.1 的 `turn_attribution`）

任务 #43。§4.1 与 §3 里 Codex 的实物是：`token_usage_record` 带 `turn_id`，
`resume_metadata.turn_attribution` 带 `root_turn_id`、`parent_turn_id`、`initiating_agent_path`
——每一笔钱既能归到"用户的一次回合"，又能顺着因果链指回"是谁把它派下去的"。

我们的对位口径先说清楚：**我们的"轮"= 一次 run**（`/start` 台账里那条 `AgentRun.id`，
`AgentEvent.run_id` 同源），不是 Codex 那样"用户一次输入"。线程身份用 `trace_id`，
父 = `parent_thread_id`，根 = 因果链顶端那条线程（主会话的根按定义就是它自己）。

实现（`models/model_provider.py`、`services/agent/{usage,graph,subagent,evidence,runtime}.py`、
`alembic/versions/0019_usage_turn_attribution.py`、`web/src/components/tasks/{api,status,evidence-panel}.tsx|ts`）：

- `model_usage` 加一等列 `run_id`（迁移 0019，只加列不动老数据）；一行同时写
  `run_id` / `root_trace_id` / `parent_trace_id` / `agent_path`。
- **轮 id 只有一个真源**：`AgentSession.run_id` 直接读 `EventRecorder.run_id`，不在别处再抄一份
  （变异 B5 试着用 `task_id` 冒充，立刻红）。根同理：`root_thread_id` 按定义现算（B6 留空即红）。
- 子代理那条线程的归属由 `subagent._Recorder` 注入（B12 把根清空即红），于是主线程与子线程
  在同一张表里第一次能分开认领：子的 `parent == root == 主线程 trace_id`，子的 `trace_id ≠ 主的`。
- `model.usage` 事件带同名字段，行与事件**逐字段必须相等**（连接键 `response_id`，#40 那套）。
- 证据包 `usage` 补三张账：`turns[]`（按轮：`threads`/`subagent_calls`/四类 token/`calls`）、
  `branches[]`（按线程）、`unattributed_calls`。归不上轮的老账**只进 `unattributed_calls` 的条数**，
  绝不并进一个空轮（B9 删掉那道 `if not run_id: continue` 就红），`_sole` 在值飘的时候返回空
  而不是挑一个冒充出身（B11）。
- 前端：缺键 → `null`（面板「暂无：后端没给 turns 这个键」），空数组 → `[]`（「共 0 轮 · N 笔归属未记」）
  ——「后端没说」与「后端说了没有」长得不一样（F3/F5）；单项缺的数印破折号不印 0（F1/F2/F8）；
  「主代理本身」只在**有轮 id 却没有父线程**时才说，老账一律「归属未记」（F7）。

验证证据（四层，每条判定都能失败）：

- 离线：后端 `tests/test_turn_attribution_v24.py` **15 条**、前端 `web/src/components/tasks/attribution.test.ts` **9 条**。
- 后端变异门禁 `E:/tmp/m1b/turn_mutate.py` **13/13**（B1–B12 四层：行 / 事件 / 归属来源 / 汇总，B13 见下面的缺陷一），
  前端变异门禁 `E:/tmp/m1b/turn_mutate_web.py` **11/11**（F1–F8 投影与文案，F9–F11 见缺陷二），全部还原逐字节一致、还原后复绿。
- 真实链路 `E:/tmp/m1b/turn_chain_probe.py` **22/22 PASS（rc=0）**，三桩 + 一对照：
  `single`（同步 `/run`，任务 `fd2d57e2-…`）、`parallel`（`spawn_agents`，`282b312a-…`）、
  `started`（界面那条 `/start` + 轮询，`e7ca3371-…`）、`legacy`（字段上线前的 `bd13bac9-…`）。
  C3 逐字段同源连上 7/9/7 对，不一致 0；C4 子线程 2/4/2 行归属全对；C5 按轮求和等于账本
  （25,357 / 29,124 / 24,879，笔数 7/9/7 全等）；C6 新任务 `unattributed_calls=0`；
  C8 这次真派出两条子线程，根与父各同一枚、`threads=3 ==` 账本线程数；
  C9 走 `/start` 时 `model_usage.run_id` 与 `agent_runs.id` **同一枚**（`545f3993-…`）——轮不是编的，是台账那一行；
  C10 走同步 `/run` 时台账里根本没有 run（`not_started`），行仍带轮 id `task:<任务>:attempt:1`，
  证明兜底那一支真在承重；C7 老任务 20 行轮 id 全空、`turns=[]`、`branches` 4 条且 `run_id` 全空。
- 真浏览器点验 `E:/tmp/m1b/turn_ui_verify.mjs` **8/8 PASS**（**Chrome headless，视口 1440×900**，
  dev `:3123` 代理 `:8062`，未触碰用户 `:3000`/`:8000`/`:8061`）：期望串全部从 `/evidence` 现取再拼，
  「轮次账本」行、按轮一行、按分支两行逐字等于接口值；老任务那一桩必须写
  `共 0 轮 · 20 笔归属未记`、四条分支全是 `无路径 · … · 归属未记` 且**不出现「主代理本身」**；
  新任务不出现「归属未记」；控制台报错 0。

真跑抓出来的两个红，都不是判定写错：

1. **同步入口没有轮 id**（缺陷一）。`POST /tasks/{id}/run` 从不设 `run_request.run_id`，
   第一次点验 C2 直接报「7 行里轮 id 为空的 7 条」、C5「轮 0 个：calls 合计 0 vs 行 7」——
   钱进了账本却按哪一轮都筛不到。`runtime._prepare` 改成 `run_id=request.run_id or thread_id`
   （入口给了就用台账那一行，没给就退回这条线程的 attempt 键），补离线第 15 条
   `test_a_run_without_a_run_ledger_still_gets_a_turn_id`，并用变异 B13（把 `or thread_id` 摘掉）钉住。
2. **attempt 键砍前八位等于没指认**（缺陷二）。修完 C 层后浏览器第一轮 U4/U5 变红：
   子分支印成「派自 `task:e7c`」、同步那轮的轮名印成「`task:fd2` · 合计 …」——
   同一任务每次运行的父引用会长成一个样。`status.ts` 新增 `shortTurnId()`：
   `task:<uuid>:attempt:<N>` 念成「主线程#N」，uuid / 随机 trace id 仍取前八位；
   补离线第 9 条（两次运行的父引用必须不同串）与变异 F9/F10/F11（退回砍位、丢次数、抓不到还硬说）。

如实说明未覆盖：

1. 「轮 = run」是对位口径，不等于 Codex 的"用户一次输入"：一条任务多次同步运行会是多"轮"，
   与 Codex 的回合语义不是一一对应。
2. 根只在**一个任务内**串因果链；团队交接、跨任务派生还没有把 `root` 传出去。
3. 770 条迁移前的老行留在原样（`run_id=""`），只以条数出现；没有回填——回填要用 trace/时间反推，
   那正是这批刻意不做的事。
4. 按轮/按分支的合计只覆盖 `model_usage` 的四类 token，没有成本换算（单一费率仍不存在，见 §7.3）。
5. 轮与 §7.7 的压缩窗口链**当时还没有共享键**：一个 run 内每次压缩落在哪一轮，那时只能靠时间线推断。
   （这一条已由任务 #44 补上——见 §7.9：逐条引用带 `run`，证据包 `windows[].turns` 直接按轮合账。）
6. 面板每张表只展开 8 条，超出只报「另有 N 条未展开」，没有分页取回。

复现：

```bash
# 1) 离线
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest tests/test_turn_attribution_v24.py tests/test_subagent_usage_v18.py -q
cd ../web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/attribution.test.ts"
# 2) 变异门禁（后端 13 条 / 前端 11 条）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m1b/turn_mutate.py
cd ../web && PYTHONIOENCODING=utf-8 ../services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/turn_mutate_web.py
# 3) 真链路 + 真浏览器（先 python E:/tmp/m1b/instance.py start，再起 API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 node ./node_modules/next/dist/bin/next dev -p 3123）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/turn_chain_probe.py   # 22/22 PASS, rc=0
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/turn_ui_verify.mjs                              # 8/8 PASS
```

### 7.9 第十项落地：这条历史是谁写的、第几次问的、活过了几个窗（对标 §4.1 的 `replacement_history_metadata` 与 `guardian_history`）

任务 #44。§4.1 量出来的实物是：`replacement_history` 旁边平行挂着
`replacement_history_metadata`——逐条给 `retained_source{message_id,turn_id,role}`、`revision`、`complete`、
`client_authored`、`user_input_order`；另外还有一整条 `guardian_history`（96/151/133 项、228,102 字节）
给审阅人看不删减的全量轨迹。

对位口径先讲清楚，免得后面对不上：**#44 借的是"逐条带出处"这套结构，不抄正文、也不建第二份全量轨迹**。
落在 #42 那套引用（`index/role/kind/chars/sha`）上，每条多钉五枚字段：

- `run`：这条历史是**哪一轮写的**（轮 = run，见 §7.8 的口径）。真源只有一个——`EventRecorder.run_id`，
  经 `runtime._prepare` → `initial_messages(run_id=)` / `ask()` / `_append_tool_response` /
  `_insert_handoff(run_id=)` / `drain_steer` 逐处盖章；`authored()` **已有 `run_id` 就不覆盖**，
  因为出生那一轮才是事实（变异 M1 改成"总是覆盖"，红；M2 干脆不读出身，也红）。
  `carried`（从上一窗带进来的）条目**刻意不盖章**：它们属于别的轮次，冒充成"本轮写的"就是假账（M12 一盖就红）。
- `user_order`：第几次用户输入，对标 `user_input_order`。只对 `role == "user"` 编号，
  非 user 一律 `null`——写 0 会说"第 1 次提问"（M4 把助手与工具结果也算成一次输入，红；M5 让序号从 1 起，也红）。
- `retained_id`：跨窗**身份**。Codex 用 uuid `revision`，我们只有内容，所以按内容寻址：`"ret:" + sha[:12]`。
  同一条内容在第 2、第 3 窗还是同一枚（离线第 7 条钉住；M6 改成每窗另发一枚 id 即红）。
- `retained_since` / `retained_windows`：从第几窗开始留下、到这次已经跨了几窗。由压缩时的那本
  留存台账 `ContextCompactor.retention`（`sha → {first_window, windows_held}`）算出，**同一条内容
  在同一窗里出现两次只算一跳**（离线第 8 条；M8 让它累加即红，M7 让已认得的内容重建台账、于是首窗被最新窗盖掉），
  没给台账或没给 `window_number` 就**整段不写**、不是写 0（离线第 9 条；M11 忘记把窗号交给台账即红）。
  `dropped` 条目如果台账查得到也照样带出窗数——丢掉的东西"活过几窗"是审计要问的（M9 把这段摘掉即红）。
- 证据包 `context.windows[]` 每条加 `turns[]`（按出生轮合起来的 `items`/`first_window`/`max_windows_held`）
  与 `turns_unattributed`；`run` 为空的条目**只进 `turns_unattributed` 的条数**，不并成一个空轮
  （`evidence.window_turn_rollup()`；M13 把它们并成一轮、M14 把同轮多条压成 1 条、M15 把未归属硬写成 0，三条都红）。
- 前端 `web/src/components/tasks/{status,timeline}.ts`：`HistoryRef` 多四字段，缺键读成 `null`/空串；
  引用行只在**真说了**的时候加尾巴——`出自 主线程#1`、`第 N 次提问`、`自第 1 窗留下 · 已跨 2 窗`
  （`retained_windows > 1` 才说，跨 1 窗等于没说）；内部台账 id 不上页面。

实现文件：`services/agent/context.py`、`services/agent/graph.py`、`services/agent/runtime.py`、
`services/agent/evidence.py`、`web/src/components/tasks/{status,timeline}.ts`（`timeline-blocks.tsx` 的
引用清单不用动，它渲染的就是 `historyRefLabel` 的输出）。

验证证据（四层，每条判定都能失败）：

- 离线：后端 `tests/test_retention_v25.py` **13 条**（与 `test_compaction_window_v23.py` 一起跑 = **23 passed**）；
  前端 `web/src/components/tasks/window.test.ts` **11 条**。逐条点名能失败的地方：出生轮不被覆盖、
  `carried` 不盖章、`user_order` 只在 user 上、`retained_id` 跨窗稳定、同窗重复只算一跳、
  没台账就没数字、rollup 不造第 0 轮、证据包与事件同源。
- 变异门禁：后端 `E:/tmp/m1b/retention_mutate.py` **15/15 红得其所**（M1–M15：出生轮被当前轮覆盖、引用不读出身、
  非 user 被读成第 0 次输入、助手/工具结果也算一次输入、序号从 1 起、留存身份另发 uuid、已认得的内容重建台账、
  同窗重复也累加、丢掉的条目不再带跨窗史、摘要不记出身、忘交窗号、`carried` 冒充本轮、未归属并成一轮/压成 1 条/硬写 0），
  前端 `E:/tmp/m1b/retention_mutate_web.py` **9/9**（W1 出身轮不读、W2 缺键兜底成 0、W3 没记跨窗读成第 1 窗、
  W4 没出身硬印一句、W5 把真的第 0 次提问当没说、W6 印成「第 0 次」、W7 只跨 1 窗也印「已跨 1 窗」、
  W8 attempt 键砍前八位、W9 投影多搬一个键＝正文混进引用行）。两个脚本都自带装置自检（每处替换必须**恰好命中一次**），
  还原逐字节一致，还原后 `test_retention_v25.py` + `test_compaction_window_v23.py` + `window.test.ts` 全复绿。
- 真实链路 `E:/tmp/m1b/window_chain_probe.py`（真 DeepSeek，供应商窗口临时压到 1600 再还原）：
  任务 `5016f7e4-1a07-4372-8f21-2bde734a00cf`，2 次压缩、26 条引用、3 条真跨窗样本、5 条 user 提问序号，
  **C1–C14 全 PASS（rc=0）**。新增的五条：C10 出身轮形状必须等于运行台账那一枚 `task:<uuid>:attempt:<N>`；
  C11 同内容跨窗 `retained_id` 不变、`retained_since` 恒等于首窗、窗数逐窗 +1（且**必须真有跨窗样本**，
  没有就判装置失效）；C12 每窗的 user 序号必须重排成 `0..n-1`、非 user 必须为 `null`；
  C13 证据包 `windows[].turns` 与事件引用**逐窗重算相等**、`turns_unattributed` 精确；
  C14 负对照用 #42 那批的 `55578136-…`：48 条引用的键**恰好还是** `index/role/kind/chars/sha` 五枚，
  它的证据窗里 `turns` 为空、`turns_unattributed == items`。
- 真浏览器点验 `E:/tmp/m1b/window_ui_verify.mjs` **9/9 PASS**（**Chrome headless，视口 1440×900**，
  dev `:3123` 代理 `:8062`，未触碰用户 `:3000`/`:8000`/`:8061`）。V8 把 26 行引用**逐行**按接口值重算再比字，
  不合行 0；V9 对 `55578136-…` 的 48 行要求「出自 / 第 N 次提问 / 自第 N 窗留下」一个字都不出现，
  同时组标题「替换后的历史（N 条）」必须在——先证明这段真的渲染了，再谈"没印出来"。
  V6 复算：页面不含工作区文件正文、也不含主机路径；V8 另加一条：`attempt:` / `task:` / `ret:`
  这些内部 id 一个都不许上页面（`ret:` 是留存台账的内部身份，只在事件里承重）。
  实物两行（从页面抓的，不是拼的）：

  ```text
  #1 · user · 181 字 · 06d23d37 · 出自 主线程#1 · 第 1 次提问 · 自第 1 窗留下 · 已跨 2 窗
  #3 · user · 31 字 · e06cc5e5 · 出自 主线程#1 · 第 2 次提问 · 自第 1 窗留下 · 已跨 2 窗
  ```

真跑抓出来的两个红，都不是判定写错：

1. **C3 一开始 FAIL：把"引用键必须一模一样"当成了契约**。后端对 `dropped` 条目只在留存台账真记过它时才给窗数，
   而出生窗被丢掉的那几条从没进过台账——真实数据是 22 条带满十键、4 条带 `retained_id` 但没有窗数。
   这不是漏写，是「没记过就不说」。判定改成能失败、且口径对的三条：留下（`items`/`added`）必须带满十键、
   丢掉的两枚窗数键**不许只出现一半**、且"说了"与"没说"两种分支必须都跑到（否则记 `shape_装置失效`）。
2. 加字段后 `test_compaction_window_v23.py` 的两条契约守卫变红——它们把 #42 那批的键集合钉死了。
   这里是**有意识地加宽键集合清单**（引用清单与证据窗的字段名列表），没有动 `safety.py`、
   `permissions.py`、`sandbox/policy.py` 里的任何限制。

如实说明未覆盖：

1. `retained_source.{message_id,turn_id}`：我们没有消息行，写不出可核对的出处 id。引用上的 `run`
   只能回答"哪一轮写的"，不等于 Codex 的 message id；宁可空着也不编一枚 uuid。
2. `revision`：Codex 是 uuid 版本号，我们只有内容寻址的 `ret:<sha12>`。同内容必然同 id，
   但**改一个字就是另一枚**，表达不了"同一逻辑条目的第 N 版"。
3. `complete` / `incomplete`（这条原话是否完整）：我们的裁剪发生在整条层面，没有逐条完整度字段。
4. `guardian_history`（228KB 全量审阅轨迹）按裁决**没有做**：压缩丢的是活动历史，而我们的审阅侧
   并没有独立副本，只有 `context.compacted` 里那份引用清单（位置/字数/指纹）。要回看被折掉的正文，
   现在只能靠工作区文件与命令输出事件，历史账本给不出来——这是审计口径上的真实缺口。
   （这一格的**清单**半边由任务 #79 补上：审阅侧全量引用 + 判定副本 + 当时生效的模式；
   **正文**半边仍按同一裁决不抄，见 §7.40。）
5. `client_authored` / `mcp_attribution`：`kind` 只到 `summary/handoff/user/assistant/tool_call/tool_result`，
   分不出"客户端写的 / 工具产生的 / MCP 来的"。
6. 跨任务与团队交接的轮归属还没接：`run` 只在一个任务内成立（同 §7.8 未覆盖 2）。
7. 老任务（#44 之前）不会突然多出这些字段：C14/V9 两桩就是钉这一点的，缺键读成"没说"。

复现：

```bash
# 1) 离线
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest tests/test_retention_v25.py tests/test_compaction_window_v23.py -q   # 23 passed
cd ../web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/window.test.ts"                        # 11 passed
# 2) 变异门禁（后端 15 / 前端 9）
PYTHONIOENCODING=utf-8 E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/retention_mutate.py
cd web && PYTHONIOENCODING=utf-8 ../services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/retention_mutate_web.py
# 3) 真链路 + 真浏览器（先 python E:/tmp/m1b/instance.py start，再起 API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 node ./node_modules/next/dist/bin/next dev -p 3123）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_chain_probe.py            # C1–C14 全 PASS, rc=0
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/window_ui_verify.mjs                                       # 9/9 PASS
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/ref_line_sample.mjs 5016f7e4-1a07-4372-8f21-2bde734a00cf    # 抓页面上真渲染的引用行
```

### 7.10 第十一项落地：派出去的钱算在这一轮头上，批次成为可指认的一等事实（对标 §4.1 的归属与 §7.4 自己留下的缺口 1）

任务 #45。这次的起点不是 Codex 有而我们没有，而是 **§7.4 交付时自己写下的那条缺口**：
`SubagentResult.usage` 只有单个子任务自己的回合累计，`services/tools/delegate.py::_spawn_agents`
拼给主代理的 payload 里压根没有 `usage` 这一项——钱确实进了同一本 `model_usage` 账，
但主代理下一步做上下文预算时看不见兄弟分支刚花掉的那部分。Codex 侧的形状（§4.1）是归属和钱不分家：
`latest_token_usage_record` 一条里同时带 `thread_id/turn_id/root_turn_id/usage/turn_token_usage/thread_token_usage`，
`resume_metadata.turn_attribution` 同时带 `parent_turn_id/initiating_agent_path`。
#43 把"哪一轮、谁引起的"算清了，预算却仍然只盯主线程自己的 `usage`——**归属有了，钱没跟着归属走**。

对位口径先讲清楚，免得后面被读成"#33 又改了一遍"：**#45 把委派并进预算与批次，但没有并进 #33 的三级口径**。

- 预算看见委派：`graph.py` 新增独立状态键 `delegated_usage`（`AgentState.delegated_usage`，
  `execute_action_node` 里由 `_delegated_usage(tool, data)` 逐次 `merge_usage` 累计，只有
  `DELEGATE_TOOLS = {spawn_agent, spawn_agents}` 的返回体进得来）。两道闸都按
  `spent = spent_self + spent_delegated` 判（graph.py:1737–1742），耗尽原因三分：
  「步数用完」/「token 用完」/**「派给子代理的 token 用完」**（`spent_self < 限额 <= spent` 时只能是这一条）。
  收尾提示词也补一句「合计 X 里有 Y 是子代理花掉的，再派一次同样算在这轮预算上」——
  不写这句，模型会把"余量变少"归因到自己多走一步，继续以为派子是免费的。
  `usage`（#33 的三级口径）一个字没动：**面板上「本回合」小于预算数不是缺陷，是这两个口径本就不是一回事**（变异 B1–B3 拆的就是这条边界）。
- `parallel_group` 成为账本一等列：迁移 `alembic/versions/0020_usage_parallel_group.py`
  （down_revision `0019_usage_turn_attribution`），`usage.account_call(parallel_group=)` 落库，
  `SubAgentResult.parallel_group` 由 `fan_out` 一次分配（`uuid4().hex[:6]`，同批孩子同一枚），
  并写进 `subagent.started` / `subagent.completed` / `subagents.completed` 三处事件与 `spawn_agents` 的工具负载。
  **空串是"不属于任何一批 / 当时没记"，不是第 0 批**（同 #43 那条规矩）：主线程、摘要、单发子代理、
  批次号上线前落下的老行一律为空；`evidence.parallel_group_rollup()`（evidence.py:95）按列筛，空值直接不进表。
- 水位继承：`subagent._inherited_watermark()` 只把父压缩器**真给过的**键传给孩子
  （`context_window`/窗口号/`trigger_ratio`），没给就不写——写 0 会让水位线落在 0、每一步都触发压缩（变异 B5/B6 钉这两条）。
- 合计有个能指认的落点：`subagents.completed.usage` = `batch_usage(results)`（subagent.py:241），
  `spawn_agents` 的 `data.usage` 同一枚数，证据包 `usage.parallel_groups[]` 由账本行**当场按列重算**。
- `latency_ms` 刻意不相加：并行段重叠，加起来比真实墙钟还大，是假账。这道闸有**两道**保险
  （`batch_usage` 剔除 + `merge_usage` 白名单不带它），单拆任何一道测试仍然绿——
  所以门禁里把它记成复合变异 **B7+**（两处同时破坏才红），而不是假装一条就能抓住。
- 前端：`api.ts` 的 `normalizeUsageParallelGroup()` 与批次九键投影（多搬一个键＝正文混进账，W9）、
  `status.ts` 的 `usageParallelGroupLabel()`（「批次 a67815 · 合计 10,188 · 5 笔 · 2 条线程 · 出自 …」，
  缺键读成「—」而不是 0）与 `delegatedSpendLabel()`（拒绝从总量反算，W6/W7）、
  `evidence-panel.tsx` 新增「按并行批次记账」小节 + 「派给子代理」行、
  `timeline.ts` 的「并行批次收口」卡与预算行的「含派出的子代理 / 其中派出 N」。

实现文件：`services/api/src/blank_api/services/agent/{usage,graph,evidence}.py`、
`services/api/src/blank_api/services/{subagent.py,tools/delegate.py}`、
`services/api/src/blank_api/models/model_provider.py`（`ModelUsage.parallel_group` 列）
+ `alembic/versions/0020_usage_parallel_group.py`、
`web/src/components/tasks/{api,status,timeline}.ts` + `evidence-panel.tsx`。

验证证据（四层，每条判定都能失败）：

- 离线：后端 `tests/test_parallel_budget_v26.py` **16 条**（与 #33/#32 同族 83 条一起跑全绿）；
  前端 `web/src/components/tasks/parallel.test.ts` **7 条**。逐条点名能失败的地方：
  三级口径不被污染、委派的钱单独成键、批次号只盖在派下去的行上、空批次不进表、
  合计键集合 ⊆ `merge_usage` 白名单、事件与账本行同一枚批次、老账读成"当时没记"。
  预算那条把限额写死成 `limit = 50_000` 并断言 `tokens_self < limit <= tokens` 且
  `tokens_delegated == 121_000`——主线程自己远没用完，耗尽的必须是被派出去的那笔。
- 变异门禁：后端 `E:/tmp/m1b/parallel_mutate.py` **21/21 红得其所**（B1–B21，其中 B7 是同时改
  `usage.py` 与 `subagent.py` 的复合变异 B7+：`usage.py`/`subagent.py`/`tools/delegate.py`/`agent/graph.py`/`agent/evidence.py` 五处），
  前端 `E:/tmp/m1b/parallel_mutate_web.py` **9/9**（W1–W9：投影漏键、缺键兜底成 0、反算委派花费、
  「批次」标签丢了线程数还硬说出自、时间线把"没用过预算"说成"派出去用的"）。
  两个脚本都带装置自检（每处替换**恰好命中一次**、按 sha 校验），还原逐字节一致，还原后
  `test_parallel_budget_v26.py` + `test_subagent_usage_v18.py` = **25 passed**、`parallel.test.ts` + `attribution.test.ts` 双双 rc=0。
- 全量回归：后端 **839 passed / 14 skipped**（见本节末复现命令，跑于 #45 代码定型之后）、
  前端 **145 passed**、`tsc --noEmit` rc=0。
- 真实链路 `E:/tmp/m1b/parallel_chain_probe.py`（真 DeepSeek，:8062 配 `BLANK_TOKEN_BUDGET=20000`）：
  任务 `037b4252-88b6-4ff1-ba13-00c7dfc423dd`，**P1–P8 全 PASS（rc=0）**。实物数字：
  11 行账本 / 其中 5 行带批次 `a67815`、主线程 6 行全为空；行↔事件 11 对逐字段一致；
  批次合计 `{prompt 9442, completion 746, cached 7296, total 10188, calls 5}` **逐项等于**两个孩子
  `subagent.completed.usage` 的求和，且**没有** `latency_ms`；
  水位三处同源 `context_window=65536`（从 `model_providers` 现查，不是硬编码）；
  预算事件 1 条：`tokens 21589 = tokens_self 11401 + tokens_delegated 10188`、`token_budget 20000`、
  `reason=「派给子代理的 token 用完」`；证据包 `batched=5 unbatched=6 delegated=10188/5 笔 总=42301（派出 10188 + 自己 32113）`。
  P8 是 A-B 负对照：#45 之前那趟真并行任务 `282b312a-…`，9 行**全部**空批次，
  `parallel_groups=[]`、`batched_calls=0`、`unbatched_calls=9`——0 要说成 0，不许编出第 0 批。
- 真浏览器点验 `E:/tmp/m1b/parallel_ui_verify.mjs` **7/7 PASS**（**Chrome headless，视口 1440×900**，
  dev `:3123` 代理 `:8062`，未触碰用户 `:3000`/`:8000`/`:8061`）。U2 是装置自检（页签真点得开、
  小标题「按并行批次记账（1 批）」与列表条数都等于接口值）；U3 逐字段比批次行原文，
  U4 逐字比「派给子代理 10,188 · 5 笔」这句**整句**，U5 让时间线的「并行批次收口 / 含派出的子代理 / 其中派出 N」
  全部由 `budget.*` 与 `subagents.completed` 事件驱动；U6 对老任务要求"0 批 + 空表文案"，
  同时不许长出一行 `批次 <hex>`，但「派给子代理」那行仍须等于接口值（委派账本早于批次号存在，不该跟着一起消失）；U7 控制台 0 报错。

真跑与点验抓出来的四个红，都不是产品代码错：

1. **P6 连着两轮 FAIL，是我的假设错**：先配 60000 跑（任务 `416df87e-…`，总 39551 = 自 24619 + 派 14932），
   再配 30000 跑（任务 `11e413c8-…`），两条都是**一条预算事件都没有**。把 11 行按时间摊开才看清形状：
   `spawn` 那一步结束时 `自 11443 + 派 12910 = 24353`，而 24353 既没过 30000 也没进 0.85×30000=25500 的收尾区，
   下一步模型直接写总结、不再进 `execute_action` 的预算闸。**"委派占大头"在这条任务上不成立**（主线程每一步都重发整段上下文，
   自己烧得比孩子多）。改配 20000 才让这道闸恰好在委派之后响——限额不是拍的，是从这张表反推的（探针顶部注释记了这三档）。
2. **P8 与 U6 先误报老账"不对"**：判据里 `int(usage.get("batched_calls") or -1)` 与 `Number(x || -1)`
   把真值 0 读成了"没说"。这套「没说 vs 说了没有」的规矩是给产品立的，判定装置自己先破了。
   现在两处各有专门的取数函数（探针 `num_of()`、点验 `numOf()`），键不在返回 `null`、值真是 0 就返回 0。
3. **U4 期望值写错**：面板那一行是整句「派给子代理 10,188 · 5 笔」，不是光一个数。
   期望串改由接口两枚数现拼（`delegated_tokens` + `delegated_calls`），这样反算的、丢笔数的都还会红。
4. **批次行的「出自」在真跑时是 uuid 前 8 位**（`出自 91630dc3`）：真链路里 `run_id` 是一个 run 的 uuid，
   `task:<uuid>:attempt:<N>` 那枚形状在 `root_trace_id`/`trace_id` 上；只有走同步入口（没有 run 台账）的
   老任务才把 attempt 键当 `run_id` 写。`shortTurnId()` 对两种形状都有定义，界面没有编东西，
   但同一个 run 在界面上有两种读法——这条记在下面"未覆盖"里，不在 #45 里顺手改。

如实说明未覆盖：

1. 批次号是 `uuid4().hex[:6]`（6 位十六进制），只在**单个任务的账本内**保证可指认；跨任务理论上可撞。
   要全局唯一就得换 uuid7，那会让面板上的短码变长，先不换。
2. 委派并进预算，但没有并进 #33 的三级口径，也没进 `context.measured` 的水位读数：
   面板上「本回合」与预算事件里的 `tokens` 不相等是设计使然（口径不同），不是账对不上。
3. `latency_ms` 在批次合计里刻意不相加（重叠段相加是假账），所以"这一批花了多少**时间**"仍然答不出；
   要答得先把并行段的墙钟区间并进事件层，属另一件事。
4. 团队链路（多成员各有 `AgentSession`）的批次与委派还没真跑：同 §7.4 未覆盖 2、§7.8 未覆盖 2、§7.9 未覆盖 6。
   （**已由任务 #46 真跑取证**，见 §7.11：群账三条相等、委派在预算之内；同时量出两处新欠账——
   群线程与成员的账没有列级连接、每条成员任务自己就是 root。）
5. 预算限额是**进程级** env（`BLANK_TOKEN_BUDGET`，`runtime._token_budget()`），没有按任务列——
   原注释的理由保留：它是安全网阈值，为一件事加一次迁移不值。代价是面板只能从 `turn.context.token_budget`
   读当时生效的那枚，读不出"这条任务本来配了多少"。
6. 单发 `spawn_agent` 的钱进 `delegated_usage`（所以进预算），但**不进批次表**（它没有批次号）；
   界面用 `batched_calls`/`unbatched_calls` 两个数分开说这类有多少条，P7/U1 把这两条口径钉住。
7. 老任务不会突然多出批次号：P8 与 U6 两桩就是钉这一点的。
8. `run_id` 有两种真形状（见上面红 4）：排队入口写的是 run 的 uuid，同步入口（没有 run 台账）写的是
   `task:<uuid>:attempt:<N>`。批次行与轮行的「出自」都直接取接口原值，界面没编，但同一件事在界面上
   有两种读法。要对齐得先定 #43 那一层的口径（改 `run_id` 的语义会牵动轮账与分支账两张表），不在 #45 顺手改。
   （#46 团队链真跑之后，这一列上实测是**四种**形状：同一张 `model_usage` 的 873 行 / 11 个 distinct 值 =
   空串 786、run 的 uuid 42、`team-<轮 id>-<消息 id>` 23、attempt 键 22——见 §7.11。）

复现：

```bash
# 1) 离线
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_parallel_budget_v26.py tests/test_subagent_usage_v18.py   # 25 passed
cd ../web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/parallel.test.ts"                          # 7 passed
# 2) 变异门禁（后端 21 / 前端 9）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/parallel_mutate.py
cd web && PYTHONIOENCODING=utf-8 ../services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/parallel_mutate_web.py
# 3) 真链路（:8062 必须带 BLANK_TOKEN_BUDGET=20000 起，P6 才有事件可判）+ 真浏览器
BLANK_TOKEN_BUDGET=20000 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py start on
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/parallel_chain_probe.py            # P1–P8 全 PASS, rc=0
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/parallel_ui_verify.mjs                                       # 7/7 PASS（TASK_NEW=…）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/parallel_dom_peek.mjs <taskId>                               # 需要改判据时先看 DOM 真形状，别照猜写期望
```

### 7.11 第十二项：团队链三级的实物——批次与委派追到群聊这一层（任务 #46，补 §7.10 未覆盖 4）

§7.10 的未覆盖 4 说「团队链路（多成员各有 `AgentSession`）的批次与委派还没真跑」。
这一批就是去把那句话变成实物：真 DeepSeek、真群聊、真 fan_out，钱从群轮追到成员任务、
再追到一批并行子代理。改动只有一处前端读法（`shortTurnId` 认团队链的轮 id），其余全是取证。

**实测出来的链路形状**（`E:/tmp/m1b/run.db`，:8062，2026-10-22）：

- 群：`87646534b01e4ee7beb462439af86d86`「团队链三级 teamchain-042044」，`token_budget=200000`。
- 轮：`0162c5af-68f1-4eb8-9710-7612e9df00c3`，`spoken` 三次 = 小白管家 → 工程 → 小白管家，
  `tokens=101172`，`status=completed`，零审批（`merge:false` 的 fan_out 风险不是 high，默认档直接放行）。
- 三个成员任务与各自的账：`137df88d`(33,267) / `b3c9a5ff`(29,461) / `92e0e031`(38,444)。
- 群侧的账对得上三条，一条不差：Σ 气泡 = Σ `usage_by_bot`(71,711 + 29,461) = `round.tokens` = **101,172**。
  主理人一轮里发言两次，所以按 Bot 汇总是两条任务相加——这正是"委派在预算之内"的证据：
  子代理写的是**成员自己的** `task_id`，钱从没离开这一轮。
- 三级链的实物挂在第一次发言那条任务上（`137df88d`）：7 笔 / 33,267，其中
  主线程自己 30,279 + 派出去 2,988；批次 `153fb8` 两条线程 `root/par-153fb8-1`(1,510)
  与 `root/par-153fb8-2`(1,478)，相加正好 2,988；`unattributed_calls=0`。
- 团队链的轮 id 形状是 `team-<轮 id>-<消息 id>`（放行续跑再多一个 `-resume` 后缀，
  来自 `services/team.py:1013`）；`agent_runs` 表里**没有**这些 id 的行（T2 数出来 0 条）。

**一处界面缺陷：团队链的轮 id 被砍成同一个前缀**

修之前界面上是 `出自 team-016`——`team-<uuid>-<消息号>` 砍前 8 位，同一轮里三条发言
（38/39/40）会念成**同一个** `team-016`，等于没指认。`web/src/components/tasks/status.ts`
加了 `TEAM_RUN_ID` 分支：认成「群聊第 N 条发言」，带 `-resume` 的后缀念成「（放行后续跑）」；
非 team 形状的回落前 8 位不变。

判定装置（每条都能失败，装置自己先自检）：

- 用例 `attribution.test.ts`：拿真跑出来的三枚 id（`team-0162c5af-…-38/-39/-40`）断三个读法
  **互不相同**、断言里直接 `doesNotMatch(/team-016/)`、断 `-resume` 与普通发言不同、
  断 `team-abc` 这种非形状仍走前 8 位。`attribution.test.ts` 10/10，前端全套 **146/146**，`tsc --noEmit` 干净。
- 变异门禁 `E:/tmp/m1b/team_label_mutate_web.py`：**7/7**，每条的红集合与期望**精确相等**，
  还原逐字节一致（`status.ts` sha256 前后同一枚 `49c45870…`）。M6（撤掉 attempt 分支）一次红了**两条**
  ——团队链用例自己拿 attempt 键做对照，批次行的「出自 主线程#1」也是它念的（`parallel.test.ts:131`），
  那是真覆盖不是靶没隔离开，期望如实写成两条。
- 真浏览器点验 `E:/tmp/m1b/team_chain_ui_verify.mjs`（Chrome 无头 1440×900，:3123 dev 代理 :8062）：
  **10/10**。期望串不在脚本里硬抄，全由 `/team/groups/{id}/state` 与 `/tasks/{id}/evidence` 的数值
  现算（预算条 `101k / 200k`、宽度 51%、三条气泡 `33k/29k/38k tokens`、轮行「群聊第 38 条发言 · 合计 33,267 · 7 笔 · 含子代理 2 笔」、
  批次行「批次 153fb8 · 合计 2,988 · 2 笔 · 2 条线程 · 出自 群聊第 38 条发言」、「派给子代理 2,988 · 2 笔」）。
- 反证 `E:/tmp/m1b/team_chain_ui_negative.py`：在**真源码**里把那半句退回砍前八位，让热更新把改动带进浏览器，
  指定 U6/U7/U9 必须红、其余七条必须照旧绿——实测正是这三条红、其余全绿，还原后 10/10 且 sha256 一致。
  没有这一步，上面那十个绿只证明"页面和接口一致"，不证明"这几条判定能失败"。

**这一批抓出来的红，全是判定装置的错，不是产品的错**：

1. **探针 T6 误报群账不对**：我按 bot 名建字典，主理人两条任务（33,267 与 38,444）同名被后一条覆盖，
   把一次正确的合账判成失败。改成先按任务列、再按 bot 聚合；没重烧真模型的钱——用只读的
   `team_chain_recheck.py` 在已有实物上重算，R1/R2 通过。
2. **跨行锚点在 CRLF 文件里命中 0 次**：`status.ts` 整份是 CRLF（402 行 402 个 CRLF），
   两行各自命中一次、拼起来一次不中，被误报成"源码变了"。锚点按文件真实换行拼。
3. **`✖ failing tests:` 汇总行被当成一条红**：node 的汇总行也带 ✖，红名单永远对不上期望。
   现在先从两份用例里抓出声明过的 17 个用例名当字典，红集合与它取交，字典里查不到的期望直接判装置失效退出。
4. **DOM 宽度少比了一个分号**：`getAttribute("style")` 读回来是 `width: 51%;`，
   拿 `width: 51%` 去 `includes` 就把一条正确的渲染判成红（U3）。先跑勘察脚本看真形状，再写期望。

**团队链上还没覆盖的三件事（这次是量出来的，不是猜的）**：

1. **群线程与成员的账之间没有任何列级连接**：账本里 `trace_id`/`root_trace_id`/`parent_trace_id`/`run_id`
   四列中，带 `team-` 前缀的行数为 **0**。也就是说"这条子代理是群里哪一轮引起的"目前只能靠
   `run_id` 上的 team 字符串反解，没有外键、没有索引，改 id 形状就会静默失配。
2. **每条成员任务自己就是 root**（`root_thread_id = task:<自己的 id>:attempt:1`），
   链不跨任务：「这三个人这一轮**合起来**花了多少」在账本上答不出，只有 `team_rounds.tokens`
   那个累加值能答，而它是编排器加的、不是从账本求和来的。§7.8 未覆盖 2 记的是同一件事，这次有了实物。
3. 顺带一条口径债：同一张 `model_usage.run_id` 列上现在有**四种**形状，实测 873 行 / 11 个 distinct 值 =
   空串 786、run 的 uuid 42、`team-…` 23、`task:<uuid>:attempt:<N>` 22。前端对四种都有定义所以界面没编，
   但"轮"这个概念在库里没有单一形状。对齐它要先裁 #43 那层的语义（牵动轮账与分支账两张表），不在 #46 顺手改。
   ——其中第 2 条已在任务 #47 收掉（见 §7.12）：账本多了一列**发起轮** `origin_run_id`，整轮的钱有了一个
   可按的键；第 1 条随之变成"事件那一侧还没有这一列"，第 3 条照旧排队。

复现：

```bash
# 1) 离线：用例 + 类型 + 变异门禁（7/7，红集合精确相等，还原逐字节一致）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/**/*.test.ts"   # 146 passed
E:/env/node/node-v24.20.0-win-x64/node.exe node_modules/typescript/bin/tsc --noEmit
PYTHONIOENCODING=utf-8 ../services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_label_mutate_web.py
# 2) 真链路（:8062 起真实模型实例）——会在 DeepSeek 上真花钱
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py start on
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_chain_probe.py        # T0–T9
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_chain_recheck.py      # 只读重算，不花钱
# 3) 真浏览器（先勘察形状，再断言，最后反证）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/team_chain_dom_peek.mjs
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/team_chain_ui_verify.mjs                             # 10/10
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_chain_ui_negative.py   # 指定 U6/U7/U9 红，其余绿
```

### 7.12 第十三项落地：一轮群聊的钱问得出来了——发起轮成为账本上的一列（任务 #47，补 §7.11 未覆盖 2）

§7.11 量出来的第二件事是这句话在账本上问不出：「这几个人这一轮**合起来**花了多少」。
原因是 `run_id` 是"每人一个"的键——一轮里每位发言成员各有一个 Task、各有一条
`team-{轮}-{发言}` 的 run，而"轮"只有一个值。当时唯一的合计数是 `team_rounds.tokens`，
它是编排器在 `_finalize_turn` 里**加**出来的，只在发言收尾时累加：中途失败或没走完的那次发言，
钱花了、数不计。这正对 Codex 的 `turn_attribution`——一整棵任务树（含 resume、含子代理）
挂在同一个发起轮上，而不是各自是一条链的起点。

**实现（钱只走一条路，键也只有一个真源）**

- `models/model_provider.py:79` 新增 `origin_run_id`（`String(120)`、`NOT NULL`、`server_default=""`、带索引），
  `models/model_provider.py:73` 与 `models/task.py` 的 `run_id` 从 `String(36)` 放宽到 120；
  空串一律读作"这一行上线前就存在，当时没记"，不许读成"没有发起轮"。
- `services/agent/events.py:37` `EventRecorder.origin_run_id = origin_run_id or run_id`——
  顶层运行的发起轮就是它自己，这是**定义**不是兜底。放在记录器上而不是会话字段上的理由：
  子代理共用父记录器，`services/subagent.py:178` 的 `_Recorder` 照抄一枚（和它抄 `run_id` 同一个形状），
  孙代理因此天然知道自己是被哪一轮引起的，不必再传一次、也不必自己猜。
- `services/agent/graph.py:285` 只留一个读记录器的 `origin_run_id` 属性，`:746` 交账时与 `run_id` 并列带给
  `services/agent/usage.py`——行、`model.usage` 事件的 `attribution`（`:153` / `:184`）念的是同一个值。
  值和 `run_id` 一样**由调用方给出，落账函数不推导、不回填**。
- `services/team.py:669` `round_origin(round_id)` 是"写进账本"和"按它查账"共用的**同一个函数**，
  不留两处各自拼一遍的机会；发言处（`:810`）与放行续跑处（`:1029`）都填它——续跑的 `run_id` 多一个
  `-resume`，发起轮一个字不改，放行后花的钱属于被放行那一轮。`RunRequest.origin_run_id`
  （`services/agent/runtime.py:126`）把值带到记录器（`:241`）。
- 迁移 `alembic/versions/0021_usage_origin_run_id.py`：加列 + 建索引 + 放宽两处 `run_id`。
  这是本仓**第一处** `alter_column`，SQLite 走 `batch_alter_table`（本项目主开发库是 SQLite，必须真能跑），
  其余方言走 `op.alter_column`。
- 读模型：`services/team.py:1201` 按发起轮把整轮的账筛出来，`round.usage` 给出
  `origin_run_id / tokens / calls / tasks / runs / subagent_calls / by_bot[]`；对不上发言的行归进
  `bot_id=""`、`bot_name="未对应发言"` 那一档，所以 `Σ by_bot == tokens` 是构造出来的恒等式，不是巧合。
  `usage_by_bot` 保持它原来的**全群累计**口径，两个数各说各的，不许互相冒充。
- 前端：`web/src/components/team/format.ts:85` `roundSpend()`（账本优先，读不到才退回轮计数，且
  `fromLedger` 如实报告自己用的是哪个口径）、`:106` `roundGapLabel()`、`:112` `botRoundTokens()`；
  页头读数与进度条按账本（`team-view.tsx:743`、`:748`），有差额时上一枚芯片与"两个口径各写一遍"的
  悬停说明（`:752`），成员条每人念自己本轮那份（`:957`、`:978`）。

**列宽为什么必须一起改（实测，不是预防性的）**

`model_usage.run_id` 与 `agent_events.run_id` 建库时都写成 `String(36)`（0019 / 0009），
而 #46 的真实库里实物最长 **51** 字符（`task:{uuid}:attempt:{n}`）。本地看不出来——SQLite 根本不按
varchar 长度检查；换 MySQL/PostgreSQL 就直接拒。本轮真跑那一轮写进去的实物是
`run_id` 44 字符、`origin_run_id` **47** 字符（`team-round-` + uuid），仍超旧的 36，所以这一列
如果没跟着放宽，按发起轮筛账本身就是"换数据库才炸"的那一类。

**判定装置（每条都能失败，装置自己先自检）**

- 用例 `tests/test_origin_run_v27.py` **10 条**：发起轮默认值/同源/"当时没记"/子代理继承；
  声明列宽容得下 51 字符实物（并自检样本长度本身）；**迁移 0021 的子进程往返**（建到 0020 → 灌一行
  51 字符的老账 → `upgrade head` → 断列宽、索引、行原样、`origin` 不被回填 → `downgrade` 回去）——
  这是全套里唯一能失败在"改了模型忘了写迁移"上的判定；一轮跨任务合计；放行续跑算同一轮；
  账本比轮计数宽（一笔没有对应发言的 440 让账本涨、轮计数不动）；全群字段不是轮字段。
  `tests/test_turn_attribution_v24.py` 的 `ATTRIBUTION_KEYS` 加了这一枚键，字段名契约由那条集合相等断言守着。
- 前端 `web/src/components/team/format.test.ts` 加 **3 条**（账本优先与退回口径、差额只在有差额时说、
  每人一档加起来等于整轮）。前端全套 **149/149**（原 146 + 3），`tsc --noEmit` 干净；
  后端全量回归 **849 passed / 14 skipped**（原基线 839，正好多出这 10 条）。
- 变异门禁：后端 `E:/tmp/m1b/origin_mutate_backend47.py` **13/13**、前端 `origin_mutate_web47.py` **7/7**，
  每条红集合与期望**精确相等**、还原逐字节 sha256 一致。后端靶按"钱从哪一站流到下一站"排：
  交账（graph）、记录器定义（events）、子代理继承（subagent，窄坏 = 值为空 / 宽坏 = 属性整个没有，
  两种形状各一条）、团队发言处、放行续跑处、运行时传值、归属字段名、声明列宽、索引、
  迁移的方言分支/建索引/加列。M3b 红了**五条**不是靶没隔离开——属性整行抽掉会让子代理那笔账直接写不出来，
  凡断"子代理有账行"的都跟着红，期望按五条写全。
- 迁移另在**真库**上验过两遍：一份带数据的拷贝（`E:/tmp/m1b/mig_test.db`，873 行用量 + 5,420 行事件）
  与点验实例自己的 `E:/tmp/m1b/run.db`。升级后两处列宽 120、`origin_run_id VARCHAR(120) DEFAULT ('') NOT NULL`、
  `ix_model_usage_origin_run_id` 建成，原有 11 个索引名、`ON DELETE CASCADE` 外键与 `uq_agent_events_task_seq`
  全部保留，行数一字不差，老行 `origin` 全为空串（没被回填）。MySQL/PostgreSQL 那条分支本机没有实例，
  **没有实跑过**，只有代码路径与列宽声明断言。
- 真 DeepSeek 一轮（:8062，2026-10-22）：组 `014c286a0d3d4a6a96ca127f4ca8b71c`、轮
  `819b12e5-0ac9-4673-8303-f3703e470ea7`，`origin_run_id=team-round-819b…` 一个键收住
  **4 个成员任务 / 4 条 run / 12 笔 / 51,009 tokens**，`by_bot` = 小白管家 27,914（6 笔）+
  评审 11,636（3 笔）+ 工程 11,459（3 笔）。探针 `E:/tmp/m1b/origin_round_probe47.py` 九条判定
  （R0–R8）**9/9**：读模型键齐、账本按发起轮重算与读模型四个数逐一相等、跨任务真的发生、
  每人一档加起来等于整轮、差额必须等于"没有对应发言的行"之和、上线前的 873 行空 origin 不被顶名、
  每个成员任务的 `model.usage` 事件里 `attribution.origin_run_id` 与账本同一个值、实物长度 44/47 超旧 36。
  **这条判定当场红过一次**：第一次跑发的那条消息没 @ 成员，编排器只让主理人发了一次言，
  `tasks=1 runs=1`，R3 直接判红——不是产品坏了，是那轮确实只有一个人花钱。补成点名两位成员再跑才是 R3 绿。
- 真浏览器点验 `E:/tmp/m1b/origin_ui_verify47.mjs`（Chrome 无头 1440×900，:3123 dev 代理 :8062）**22/22**，
  三段同一组、只动一个变量：A 段（干净）页头 `51k / 200k`、宽度 26%、**没有**差额芯片；
  B 段往真库补一笔 `100,000` 的"没有对应发言"的账（`E:/tmp/m1b/ghost47.py`），页头必须换成 `151k / 200k`、
  宽度 76%，芯片 `+100,000 未计入`，悬停写清两个口径（`账本口径：13 笔 · 5 个成员任务 · 5 次发言（含放行续跑）`
  `编排器轮计数 51009，只累加收尾完成的发言`），并且负方向也断：页头**不许**出现 `51k / 200k` 那种读法、
  成员那一档的 title 与 A 段一字不差（那笔钱不能挂到任何成员头上）；C 段撤掉后必须回到 A 段读数。
  期望全部由 `/team/groups/{id}/state` 的数值现算，`shortTokens`/`usagePercent`/`roundGapLabel`
  在脚本里独立实现一遍再逐字比对。

**这一批抓出来的红，四条里三条是判定装置的错**

1. **`ghost47.py` 在干净状态要求"撤除必须少 100,000"**：装置自己骗自己（第一次跑就炸在这里）。
   改成"删掉几行必须等于本来有几行"，只有真删过才要求差额。
2. **芯片名从 `textContent` 剥尾数的顺序**：先剥数字再摘「主理人」时，`小白管家28k主理人` 剥不到尾数，
   取数装置认不出这个成员，把一条正确的渲染判成红。改成先摘标签再剥数字，并加一条
   「芯片名都认得出对应成员」的装置自检（A/B/C 三段各一次）。
3. **变异锚点贪多**：M1 本想只撤掉 `origin_run_id=` 那一行，锚点却把上一行 `run_id=self.run_id,`
   一起吞了，红了 12 条——那一次红的是"两轮都没交账"，不是我想测的那一站。锚点收窄后 5 条精确命中。
4. **测试自己的顺序假设**：`test_origin_lands_on_the_row_and_the_event_from_one_source` 起初按
   `_rows()[1]` 取"第二笔"，而行是按 uuid 主键排的、不是插入序；改成按这笔自己的金额认行，
   并先断金额集合 `[100, 500]` 自检。

**这一层还没覆盖的（照旧是量出来的，不是猜的）**

1. **事件那一侧还没有这一列**：`agent_events` 只有 `run_id`，跨任务问"这一轮的事件"仍要从
   `run_id` 字符串反解，或先查账本再回表。`round.usage` 走的是后者（先按 origin 拿账行，再用
   `TeamMessage.task_id` 映射回成员）。
2. **证据包里没有"按发起轮"那张账**：`evidence.py` 的 `turn_rollup` 仍按 `run_id` 聚合——单个任务内
   两者退化成一回事，目前没有消费方，就没有为了对称而先加一层。
3. `run_id` 的四种形状仍排队（#47 只让发起轮有了自己的列，没有统一 run 的形状）。
   `origin_run_id` 现在自己是两种形状：空串 873、`team-round-<uuid>` 13（隔离库实测）。
4. **MySQL/PostgreSQL 分支没有本机实跑条件**，0021 的往返只在 SQLite 上验过；
   列宽声明由那条"51 字符实物"的用例替它们把关。
   （这一条已被 #48 部分收回：MySQL 上"迁移按服务端名字找约束、定点摘掉"这一步的形状，
   连同 CASCADE 在真服务端的破坏力，都在 3307 的 MySQL 8.4 上实测过了，见 §7.13 的 F1–F7；
   仍没实跑的是 0021 那次整条 alembic 历史往返——本仓今天从 0002 起就在 MySQL 上撞 1101。）

复现：

```bash
# 1) 离线：用例 + 类型 + 变异门禁（后端 13/13、前端 7/7，红集合精确相等，还原逐字节一致）
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q          # 849 passed / 14 skipped
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_origin_run_v27.py tests/test_turn_attribution_v24.py   # 25 passed
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m1b/origin_mutate_backend47.py --check   # 13 条锚点各命中 1 次
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m1b/origin_mutate_backend47.py           # 跑电池时不要并行跑回归
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/**/*.test.ts"   # 149 passed
E:/env/node/node-v24.20.0-win-x64/node.exe node_modules/typescript/bin/tsc --noEmit
E:/env/python-e2e/Scripts/python.exe -X utf8 E:/tmp/m1b/origin_mutate_web47.py
# 2) 迁移：先看一眼实物列宽，再在带数据的库上往返（测试里已含同样判定）
BLANK_DATABASE_URL=sqlite+aiosqlite:///E:/tmp/m1b/run.db .venv/Scripts/python.exe -X utf8 -m alembic upgrade head
# 3) 真链路（:8062 起真实模型实例）——会在 DeepSeek 上真花钱
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py start off
cd web && API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 \
  E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/next/dist/bin/next dev -p 3123
MEMBERS="工程,评审" PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/origin_round_probe47.py   # R0–R8
# 4) 真浏览器（A 基线 → B 补一笔无对应发言的账 → C 撤掉，只动一个变量）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/origin_ui_verify47.mjs        # 22/22
# 装置自检：把前端 roundSpend 改成读轮计数，B-H 与 B-G 必须红（这条由电池 M1/M4 覆盖）
```

### 7.13 第十四项落地：账本不许跟着供应商一起消失（任务 #48，对标 §1 的 `token_usage_record`）

§1 记 Codex 那本账的时候只写了"只追加、七类记录"，这一批回去把它的多表关系又读了一遍：
`token_usage_record` 里没有指向供应商配置表的外键。账行是历史事实，配置行是可删的设置，
两者一起删等于把"钱真花过"这件事从系统里抹掉。我们这边恰好相反——
`model_usage.provider_id` 声明的是 `ForeignKey("model_providers.id", ondelete="CASCADE")`。

这条声明在本地永远看不见：连接层从不执行 `PRAGMA foreign_keys`（本项目实测 `foreign_keys=0`），
SQLite 就把它当装饰。换到 MySQL/PostgreSQL，服务端照声明执行。于是这是"换库才炸"那一类缺陷的
标准形状，而且炸掉的是钱。

**先取证，再动手。** 上一批在本机自起的 MySQL 8.4（3307，自己的 datadir，不碰 3306）和 SQLite 上
量出四条（`E:/tmp/m48/fk48_probe.log`）：

| 编号 | 条件 | 实测 |
| --- | --- | --- |
| F1 | MySQL + CASCADE + 强制外键 | `delete_rule=(('model_usage_ibfk_1','CASCADE'),)`；删一家供应商前 **3 行 / 600 tokens**，删后 **0 行 / 0 tokens** |
| F2 | MySQL + 只把规则换成 RESTRICT | 服务端返回 `1451 Cannot delete or update a parent row ... ON DELETE RESTRICT`，删除被拒，账本 **3 行 / 600** 一行不少 |
| F3 | SQLite，本项目真实连接层（`foreign_keys=0`） | 删除成功，留下 **3 行指向已不存在的 provider_id** |
| F4 | SQLite 打开 `foreign_keys` 而 CASCADE 仍在 | 同一条删除当场清空账本：**0 行 / 0 tokens** |

F2 说明"改成 RESTRICT"不是处置：那会让"删除供应商"这个正常配置动作在强制外键方言上直接失败。
所以按 Codex 的形状来——**账本上彻底摘掉这条外键**，悬空成为一条正常状态，由读模型负责念出来。

**实现**：

- `models/model_provider.py`：`provider_id` 去掉 `ForeignKey(...)`，新增模块级
  `PROVIDER_GONE = "供应商已删除"`（两个读模型共用同一个说法，界面上才不会出现 `None`）。
- `api/routes/observability.py`（`/observability/summary`）：内连接 → 左连接，孤儿桶键
  `f"{provider or PROVIDER_GONE}/{model}"`——保住 `split("/", 1)[1]` 那套成本口径不被打乱。
- `api/routes/providers.py`（`/usage/summary`）：同样改左连接（`select_from(ModelUsage)`），
  `provider = name or PROVIDER_GONE` 同时用于桶键和 `provider` 字段。这一处是**界面上真会少算**的：
  `web/src/components/overview/live-panels.tsx:41` 把各桶 token 相加当总量，
  `web/src/components/settings/usage-panel.tsx:76` 直接把 `row.provider` 渲染出来。
  而 `services/agent/evidence.py:197` 早就是 `outerjoin` 并在 `:383` 用 `if name` 滤掉空名——
  所以证据包一直说"钱还在"，概览页却在供应商被删后少一笔，两处口径本来就不一致。
- `alembic/versions/0022_ledger_no_cascade.py`（`down_revision="0021_usage_origin_run_id"`）管已有库；
  0003 那条老迁移里的 FK 声明**不改写**——它是 0022 的负对照靶子，也是"迁移后==ORM"那条用例的另一半。
  SQLite 分支不能靠 `batch_alter_table`：反射出来的内联外键在 SQLite 里没有名字，
  `op.drop_constraint` 直接抛 `ValueError`。所以手写整表重建：`autoload_with` 反射活表 →
  同一 MetaData 里建 `model_usage__0022_staging`（列、类型、NOT NULL、`server_default` 的 DDL 文本、
  主键照抄）→ 记下索引规格（名 / 列序 / unique）→ `COUNT` → 建表 → `INSERT..SELECT` **显式列清单** →
  再 `COUNT`，两次不等就 `raise RuntimeError`（整条迁移回滚，绝不静默交出一张空表）→ `DROP` 老表 →
  `RENAME` → 用 `op.create_index(name, LEDGER, cols, unique)` 按**原名原列序**补回索引。
  MySQL/PostgreSQL 分支：`sa.inspect(bind).get_foreign_keys()` 取服务端存的**真约束名** →
  按名 `op.drop_constraint(..., type_="foreignkey")`；反射到一条叫不出名字的外键就抛错请人工处理，
  不去猜名字（反射到 0 条＝这张表本来就没外键，无事可做，所以这条迁移可重复执行——F5.b 判的就是这个）。
  downgrade 用固定名 `fk_model_usage_provider` 把 CASCADE 装回去。

**验证（修复前后同跑，`tests/test_ledger_dialect_v28.py`，11 条）**：

- 红基线：把这个文件对着改前的代码跑 → **8 failed / 2 passed**。数字实例：
  概览页 `{"before": 2, "after": 0}`、成本页 `{"models": {}}`（供应商一删，桶整个没了）、
  强制外键那条 `{"删供应商前": [2,390], "删供应商后": [0,0], "foreign_key_list": [["model_providers","CASCADE"]]}`、
  方言编译那条在 MySQL 的 DDL 里抓到字面 `FOREIGN KEY(provider_id) REFERENCES model_providers (id) ON DELETE CASCADE`。
  两条"能失败"的正对照：同库另建一张 `probe_cascade` 表带 CASCADE，`PRAGMA foreign_keys=ON` 的连接上
  它必须被抹干净（否则说明这次没真测到强制外键）；0021 停在旧版的库必须**确实**声明着 CASCADE。
- 绿：摘掉声明 + 0022 + 两处读模型改完 → **11 passed**（含后补的成本页那条，它先单独红过一次）。
- 迁移往返：0021 库 → `upgrade head` 前后**逐列行 dump 相等**、删除规则集合变成空、索引字典逐条相等、
  列默认值（`'chat'` / `'0'` / `''` / `CURRENT_TIMESTAMP`）仍在；`downgrade` 回去外键回来了且行不丢；
  另一条用例把迁移产物与 ORM 声明逐列比类型与 notnull（"migrated == ORM"钉住两条建库路径）。
- 变异门禁 10 条（`E:/tmp/mut-v28.py`，含 `m_noop` 装置自检）：全部 RED-OK，
  每条的实测红集合与跑前写死的期望**精确相等**、`额外=[]`、还原逐字节一致，`[restore] 11 passed`。
  逐条：内连接退回（3 红）、只退标签（1 红）、成本页内连接（1 红）、成本页只改连接不改标签（1 红）、
  0022 建表仍带 CASCADE（2 红）、搬行筛掉所有行（4 红，验的就是那道 `COUNT` 守卫）、
  重建后不补索引（2 红）、丢掉列默认值（1 红）、ORM 把外键加回来（3 红）。
- 全量后端回归（电池结束后单独跑一次）：**860 passed / 14 skipped**，`E:/tmp/m48/regress-v28-final.log`。
  前端未动，故本轮不跑前端与 `tsc`（改动全在后端与迁移；`/usage/summary` 的形状与字段名一个字没改）。
- 真 MySQL 8.4 复跑（`E:/tmp/m48/f5_mysql_confirm.py`，**11/11**）：
  F5 按新声明编译出的 MySQL 表 `show create table` 里不含 `FOREIGN KEY`，
  迁移的 MySQL 分支在这张表上反射到 `[]`（可重复执行），删供应商后 **3 行 / 600 tokens** 原样留着；
  F6 补上 CASCADE 外键后，`_declared_fks()` 反射到服务端真名 `[('fk_probe_provider','CASCADE')]`，
  按这个名 `DROP FOREIGN KEY` 之后反射变 `[]`，同一条删除不再抹账（**3 行 / 600**，3 行悬空）；
  F7 正对照：清场（补外键前 MySQL 会校验存量行）→ 补 CASCADE → 重种 3 笔 → 删供应商 → **当场 0 行 / 0 tokens**。
  这条正对照是必须的：没有它，"摘掉外键后钱还在"可能只是因为这台库根本没在强制外键。

**这一批抓出来的装置错，一条值得单记**：`m_fk_readded_in_orm` 第一次跑报的是 `GREEN-BAD rc=4 转红=[]`，
看着像"变异没被测到"。实际是注入的代码本身不合法——`mapped_column(ForeignKey(...), String(36), ...)`
把外键放在类型槽位，这个 SQLAlchemy 版本上直接 `ArgumentError: 'SchemaItem' object ... expected, got String(length=36)`，
`conftest` 在收集期就 ImportError，11 条一条都没跑。装置只认 `FAILED|ERROR <文件>::<用例>`，抓不到文件级
`ERROR <文件> - <异常>`，于是把"根本没跑"读成"全绿"。两处都改了：注入换成合法的类型优先写法
（`mapped_column(String(36), ForeignKey(...), ...)`，这也正是仓库里原本的写法），电池加了
`crashed()` 判定——零红且日志里有收集期异常就叫"无效轮"，不计入证据。

**这一层还没覆盖的（量出来的，不是猜的）**

1. **整条 alembic 历史在 MySQL 上仍跑不通**：14 处 `sa.Text(..., server_default=...)` 会撞 1101
   （MySQL 不允许 TEXT 列带默认值）。这是本仓今天能不能换 MySQL 的**前置门槛**，比 #48 这一列大得多，
   需要单独裁决；#48 只把自己那一步（找约束名 → 定点摘）在真 MySQL 上验过。
2. **PostgreSQL 分支没有实跑条件**（本机没有可用的 PG 实例），它这一侧只有编译期 DDL 判定
   （`test_no_dialect_compiles_a_rule_that_deletes_the_ledger` 里三方言各一行）。
3. **`PRAGMA foreign_keys` 仍不打开**。账本这一侧的雷拆了，但库里还有别的 CASCADE 声明
   （project→workspace、tasks→* 那一族）；真要在 SQLite 上开强制外键，得先把那些一起处理，
   否则打开的瞬间是另一批静默清账。
4. **账本行没有快照供应商名字**。摘掉外键后，供应商被删只能念成「供应商已删除」，
   念不出"当时那家叫什么"。要留住名字得在账行上加一列（或在写账时冗余 `provider_name`），是另一笔设计。
5. `/observability/summary` 这一侧**没有浏览器点验**：`web/src/components/settings/api.ts:199` 定义了
   `fetchObservability`，全仓检索无调用方——接口只由真 ASGI HTTP 用例覆盖。这一点如实写着，不谎称点过界面。

复现：

```bash
# 1) 离线：11 条用例 + 变异门禁（10/10 RED-OK，红集合精确相等，还原逐字节一致，restore 回绿）
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q   # 860 passed / 14 skipped（`E:/tmp/m48/regress-v28-final.log`）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ledger_dialect_v28.py    # 11 passed
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ledger_dialect_v28.py    # 对着改前的代码：8 failed / 2 passed
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/mut-v28.py                                # 跑电池时不并行跑回归
# 2) MySQL 8.4 取证（自起实例，自己的 datadir；跑之前核对 3307 的命令行含 m48/my.ini）
E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m48/mysql48.py start
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/fk48_probe.py          # F1–F4
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/f5_mysql_confirm.py    # F5–F7，11/11
```

### 7.14 第十五项落地：整条迁移历史与两条建库路径在真 MySQL 8.4 上成立（任务 #49，补 §7.13 未覆盖 1）

这一批的题目不是"再对表 Codex"，而是 §7.13 自己记下的第 1 条未覆盖项。Codex 的实现里没有这件事——
它的会话与账本就是一个 SQLite 单文件，schema 只有一种出生方式；而"能换 MySQL/PostgreSQL"写在我们的
产品口径上，那换库就必须是**真跑得起来**，不是"理论上应该能跑"。

**规则实物**（真 8.4.0；自起实例、自己的 datadir、端口 3307，用户自己的 3306 全程没碰）：
MySQL 拒绝给 BLOB/TEXT/GEOMETRY/JSON 列写字面量默认值——
`1101, "BLOB, TEXT, GEOMETRY or JSON column 'description' can't have a default value"`；
8.0.13 起的**表达式默认值** `DEFAULT ('')` 是唯一一种两种方言都吃的写法。
探针 `E:/tmp/m48/mysql1101_probe2.py` **5/5**：M1 TEXT 被拒 **12/12**、M2 JSON 被拒 **5/5**、
M3 换成表达式默认值后 **17/17** 建得出、M4 MySQL 里表达式默认值落的就是字面量本身（17 列逐列）、
M5 SQLite 也接受 `DEFAULT ('…')` 且落值与改前逐列一致。

**站点分三层，一层比一层难找**

1. **迁移历史 17 处**（0002:1、0004:5、0008:1、0015:1、0016:8、0017:2）＝ 12 个 TEXT + 5 个 JSON，
   逐条点名见 `tests/test_schema_agreement_v29.py` 里的 `LITERAL_SITES`。
2. **ORM 声明层 4 处**：`tasks.attachments`、`team_rounds.queue`、`team_rounds.spoken`、`team_messages.meta`。
   这一层差点漏掉：先写的 AST 扫描只认 `ast.Call` 形式的类型节点，报"0 处"，而代码里写的是裸
   `mapped_column(JSON, ...)`——类型是个 `ast.Name`。真把它抓出来的是**把 `CreateTable(table)` 用 mysql
   方言编译一遍看 DDL**。缺陷实物：MySQL 上 `Base.metadata.create_all()` 当场死在 `tasks.attachments`
   的 1101 上。这条盲区写进了 `test_migration_files_have_no_literal_default_on_lob_columns` 的 docstring：
   扫描器必须同时认两种类型形状，"扫出来是 0"不等于"没有"。
3. **两条建库路径的可空性漂移 12 列**（S3b 在真 MySQL 上逐列比出来的）：`threads.created_at/updated_at`、
   `team_bots.created_at/updated_at`、`team_groups.created_at/updated_at`、`team_members.created_at`、
   `team_messages.created_at`、`team_rounds.created_at/updated_at`、`search_docs.created_at/updated_at`。
   成因是 0014/0016/0017 建表时只写了 `server_default=sa.func.now()`、漏了 `nullable=False`，而模型侧一直是
   `Mapped[datetime]`。SQLite 不拦、ORM 插入从不显式写 NULL，所以本地一辈子看不出来；但"库比代码宽松"
   在换库那天会变成一句谎言——任何绕过 ORM 的写入都能造出 `created_at IS NULL` 的行，应用读出来是 `None`，
   然后炸在任何 `.isoformat()` 上。→ 迁移 **0023**。

**处置**

- 17 + 4 处统一成表达式默认值（迁移侧 `sa.text("('')")` / `sa.text("('[]')")` / `sa.text("('{}')")`，
  模型侧 `server_default=text("('[]')")`）。**字面量一个都没丢**：M4、M5 与
  `test_all_literal_default_sites_kept_their_value` 三条都在盯"值没变"这件事。
- 0023 先数 NULL 再改：任一列还有 NULL 就当场抛错让整条迁移回滚，而不是让 MySQL 用一条 1138 警告
  把存量数据静默变成 `0000-00-00`。数 NULL 用反射出的 `sa.Table` + `select(count()).where(col.is_(None))`，
  不手写引号——第一版手写了 `FROM "threads"`，在真 8.4 上直接 **1064**（MySQL 里双引号是字符串定界符，
  除非开 `ANSI_QUOTES`；标识符引法必须交给方言）。SQLite 没有 `ALTER COLUMN`，走 `batch_alter_table`
  整表重建（0021 已确立的写法）；全库没有 AUTOINCREMENT，所以重建不会改 id 语义。

**验证（按"每条判定都能失败"的口径）**

- 整条历史在真 MySQL 上：清库 → `upgrade head` **rc=0**，`Running upgrade` 逐条 0001→0022
  （22 步，`E:/tmp/m48/alembic-mysql-full3.log`）再接 0023（`alembic-mysql-23.log`）；
  0023 `downgrade` → `upgrade` 往返各一次成功（`alembic-mysql-23-down.log` / `alembic-mysql-23-up2.log`）。
- **两条建库路径对账**（`E:/tmp/m48/mysql_schema_agreement.py`，真 MySQL，**7/7**）：
  S1 ORM 的 `create_all` 在 8.4 上建得出全部表（无异常）；S2 表集合 **20 vs 20**、两侧都不多；
  S3 列名集合不符 0 张；S3b 交集列可空性不符 **0 列**；S3c 外键集合不符 0 张；
  S4 `model_usage.provider_id` 上没有外键（#48 的果实；全库仍有 21 条别的外键，不是"整库没外键"）；
  S5 17 处在 `information_schema.COLUMNS` 里都带着字面量默认值——MySQL 把它们存成 `_utf8mb4\'\'`，
  比对前必须归一化，不归一化会得到 17 条假"值不符"（这条也是装置错，量出来的）。
- 新用例 `tests/test_schema_agreement_v29.py` **6 passed**：整条历史 subprocess 真跑到 head（rc=0 且库文件
  必须存在——"这条历史从没被真跑过"本身就是一个断言）、两侧逐列对账、ORM 的 MySQL DDL 里没有 LOB 裸字面量、
  迁移文件 AST 里没有、表达式默认值仍落空串、17 处值逐列没变、以及 `test_timestamp_migration_refuses_existing_nulls`
  （升到 0022 → 显式插入 `created_at=NULL` → `upgrade head` 必须失败且报错里带"还有 1 行是 NULL"，
  同时 `alembic_version` 仍停在 `0022_ledger_no_cascade`、那行数据原样还在）。
- **红基线是变异电池给的，不是"改前跑一遍"**：改前的工作树已经不存在了，`E:/tmp/m48/mutate49.py`
  把这 9 件修复逐条撤回，每条的实测红集合与跑前写死的期望**精确相等**、`额外=[]`、还原逐字节一致，
  `汇总：靶数 9，未按预期 0`，收尾复跑 rc=0 6 passed（`E:/tmp/m48/mutate49.log`）。逐条：ORM 三处退回裸字面量
  （各红 `test_orm_mysql_ddl_has_no_literal_default_on_lob_columns` 一条，其中 M2 特意换了源码形状：折行→单行）、
  迁移两处（各红 `test_migration_files_have_no_literal_default_on_lob_columns`；0016 的 `queue`/`spoken` 是同串，
  所以锚点必须带列名才命中恰好 1 次）、NULL 闸门换成 `if False:`（红 1）、TIMESTAMPS 少收一张表（红 2：对账 +
  NULL 用例——实测，不是把期望写宽）、`_alter` 传下去的取值写死成 `True`（红 1）、默认值写法没退但值被改成 `[ ]`
  （红 `test_all_literal_default_sites_kept_their_value`）。
- **电池自己错了两处，都记在脚本里**：① `orm_default()` 生成时漏了 `text(` 的收尾括号，M1 注进源码的是
  不可编译代码、M2/M3 的多行锚因此命中 0 次，第一版把这三条记成了"未捕获"——实际是装置坏了，不是修复无效；
  ② M8 的期望我写了两条，实测只有对账那条红，因为 NULL 用例测的是 `_null_count` 那道闸门、与 DDL 传下去的
  `nullable` 取值无关，**它保持绿是对的**，期望是我猜的。把期望写宽不叫放宽，写错就是写错。
- **SQLite 侧安全**（用户的库全程只读）：把线上 `run.db` 复制一份跑 0023 → 20 张表行数逐张对照 **0 张不符**；
  被 `batch_alter_table` 整表重建的 7 张（threads 13 / team_bots 3 / team_groups 5 / team_members 13 /
  team_messages 36 / team_rounds 10 / search_docs 107 行）**逐行等值**、值对照 **0 张不符**、12 列里 NULL 行数 **0**；
  反证：同一个副本上显式写 `created_at=NULL` 现在被 `NOT NULL constraint failed: threads.created_at` 拒掉，
  而停在 0022 的库放行同一条插入——判定确实来自 0023。全程没有对 `run.db` 写过一行（`mode=ro` 打开，
  反证插在副本与 `pre-0023.db` 上做）。
- 全量后端回归（电池结束后单独跑，SQLite 路径必须仍全绿）：**866 passed / 14 skipped**，`E:/tmp/m48/regress49.log`。
  这批改动全在后端与迁移，前端一个字没动，故不跑 `npm test` 与 `tsc`，也没有浏览器点验。

**这一层还没覆盖的（量出来的，不是猜的）**

1. **PostgreSQL 仍没有实跑条件**（本机没有可用 PG 实例）。它这一侧只有编译期 DDL 判定和迁移里的方言分支，
   和 §7.13 未覆盖 2 是同一条账。
2. **应用从没在 MySQL 上端到端跑过**：这批只到 schema 层（建库、整条历史、两条路径对账）。运行时 SQL 的
   方言差异（分页与 `LIMIT ALL`、函数名、JSON 列读写、`onupdate` 语义）一次都没被真实 HTTP 请求穿过。
   "MySQL 能建库" ≠ "MySQL 能跑产品"。
3. **`PRAGMA foreign_keys` 仍未打开**，库里其余 CASCADE 声明（project→workspace、tasks→* 那一族）没处理，
   沿用 §7.13 未覆盖 3。
4. 0023 的 NULL 闸门在**真 MySQL 的存量库**上没验过：3307 上跑的库是清库重建的，12 列全空，
   "有 NULL 时 MySQL 分支确实拒绝"这条只有 SQLite 侧的用例证据。
5. MySQL 侧的对账目前靠探针脚本（`E:/tmp/m48/`），没进 CI——pytest 里那 6 条跑的是 SQLite，
   MySQL 那两个"能失败"的判定（S1/S5）要换库回归得手工再跑一次脚本。

复现：

```bash
# 1) 离线：6 条用例 + 变异电池（9/9 红集合精确相等、还原逐字节一致、收尾复跑回绿）
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q   # 866 passed / 14 skipped（E:/tmp/m48/regress49.log）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_schema_agreement_v29.py  # 6 passed
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/mutate49.py             # 跑电池时不并行跑回归
# 2) 真 MySQL 8.4（自起实例、自己的 datadir；跑之前核对监听 3307 的命令行含 m48/my.ini，3306 是用户自己的）
E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m48/mysql48.py start
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/reset_mysql48.py        # 清 kbgai_full
BLANK_DATABASE_URL=mysql+aiomysql://root@127.0.0.1:3307/kbgai_full alembic upgrade head    # 0001→0023，rc=0
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/mysql1101_probe2.py     # M1–M5，5/5
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/mysql_schema_agreement.py  # S1–S5，7/7
# 3) SQLite 安全：线上库的副本上跑 0023，逐行对照 + NULL 反证（原库只读打开）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m48/live_copy_integrity.py
```

### 7.15 第十六项落地：应用真跑在 MySQL 8.4 上，量出并补平**运行时**方言分歧（任务 #50，补 §7.14 未覆盖 2）

#49 证明的是「这份 schema 在两种数据库上都能生出来」，不是「应用真跑在第二种数据库上」。
这一批问下一层：**应用自己发出的那些 SQL 与那些 ORM 对象，在 MySQL 上过不过**。
装置 `E:/tmp/m50/mysql_e2e.py`：59 步真 HTTP 端到端（项目→工作区→供应商→任务→运行→事件/证据/快照/diff→
会话→团队群→记忆→技能→设置→删供应商→删除族），同一段脚本、同样断言，**先在 SQLite 上跑一遍（对照），
再在 MySQL 上跑一遍（被测）**。判据只认线上接口，不看内部函数。裁决规则写进装置里：
**两边一起红 = 装置错；只有 MySQL 红 = 真分歧**。模型是本机 OpenAI 兼容桩（不打供应商、不花钱），
任务只用只读工具 + `default` 审批模式，不产生写文件与越界行为。

**第一版量到的东西**：SQLite 59/59，MySQL **25 步红**。25 条里只有 4 个根因，其余 21 条是级联 404
（创建就红了，后面每一步都在读一个没建出来的对象）。四个根因分三类：

| 编号 | 形状 | 实测红证 | 为什么以前看不见 |
| --- | --- | --- | --- |
| F1 | **UPDATE 后直接序列化**（引擎无关）：`updated_at` 声明是 `onupdate=func.now()`，提交后那一列在会话里处于「待读回」，`SkillOut.model_validate(skill)` 在同步上下文里触发惰性加载 → `MissingGreenlet` → HTTP 500。命中 `POST /skills/{id}/enable`、`/disable`、`/rollback`、`POST /tasks/{id}/cancel` | 驱动日志 `[FAIL] 50 技能停用 AssertionError: HTTP 500`；api.log 里 `ValidationError … updated_at … MissingGreenlet`，调用点 `routes/skills.py:118` | `expire_on_commit=False` **不覆盖**这个状态；且全仓 866 条离线用例里**一条 `/skills` 的 HTTP 用例都没有**（`grep -rn "/skills" tests/` 为空）——这是「测试全绿 ≠ 接口能用」的实物 |
| F2 | **INSERT 后直接序列化**（只在 MySQL 红）：`created_at`/`updated_at` 是服务端生成的，SQLite ≥3.35 走 `INSERT … RETURNING` 顺手带回，**MySQL 8.4 的 INSERT 没有 RETURNING** ⇒ 提交后第一次读是同步懒加载 | MySQL 独有红：`[FAIL] 16 任务创建`、`[FAIL] 29 会话创建`、`[FAIL] 35 Bot 创建` 全是 `HTTP 500`，同一份 payload 在 SQLite 上全绿 | 只在第二种方言上才成立，单机 SQLite 永远看不见；辅助函数（`persist_task`、`bot_payload`）还让「按函数扫 AST」这种静态比对漏计 |
| F3 | **驱动分歧**：`TypeError: 'str' object is not callable`（`aiomysql/cursors.py:197 _escape_args` → `aiomysql/connection.py:418 escape` → `escape_bytes_prefixed`）。LangGraph 检查点把 msgpack 字节写进 `LONGBLOB`（`services/agent/checkpoints.py` 的 `aput`/`aput_writes`），**任何一次真跑都要写它** ⇒ MySQL 上整条运行链路是死的 | 服务日志尾帧 `api/routes/tasks.py:392 run_task → services/agent/runtime.py:499 run → services/agent/checkpoints.py:207 aput`；根因核对：PyMySQL 1.2.3 的 `converters.py` 里就是 `escape_dict = escape_bytes_prefixed = "DO NOT IMPORT THIS!!!"`（上游 PyMySQL/PyMySQL#1274，1.3.0 计划删别名），aiomysql 0.3.2（PyPI 最新）还在 import 并调用它 | 这台开发机上的 pymysql 是手装的，从来没人用 `uv sync` 装出一个「能连 MySQL」的环境 |
| F4 | **依赖事实**：`pymysql`/`aiomysql` 既不在 `pyproject.toml` 也不在 `uv.lock`，而 `asyncpg`（Postgres 驱动）躺在默认依赖里 | 读 `services/api/pyproject.toml` 与 `uv.lock` 的 `blank-api` 依赖段：12 项里没有 MySQL 驱动 | 「能换 MySQL」写在产品口径上，但按这份声明装出来的应用**连不上** MySQL |
| F5 | **装置自己的抖动**（不是产品缺陷）：驱动步 19「任务运行到终态」一度**两边一起红**，SQLite 报 `HTTP 502 模型服务不可达（ReadError）` | 按规则判定为装置错：本机桩是 HTTP/1.1 keep-alive，httpx 连接池复用了一条服务端已关的连接 | 修法：桩的每个响应都带 `Connection: close`、SSE 收尾 `flush()`、`request_queue_size` 5→128，并把「已服务/被截断」两个计数打进日志（装置自检，被截断>0 就不许把红算到产品头上）。修后两边各 10 次服务、0 次截断 |

**实现**

1. F1/F2：7 处「提交后先把行读回来再出参」——`routes/skills.py` 的 enable/disable/rollback、
   `routes/tasks.py` 的 `cancel_task`（UPDATE 形状）与 `persist_task`、`routes/threads.py` 的 `create_thread`、
   `routes/team.py` 的 `create_bot`（INSERT 形状）。`persist_task` 的 docstring 直接把「SQLite 有 RETURNING、
   MySQL 8.4 没有」写进去，两种方言走同一条路而不是给 MySQL 打补丁。
   **取消语义没有顺手放宽**：终态任务仍然 409 `task_not_cancellable`（M5 那条变异专门守它）。
2. F3：`blank_api/db.py` 新增 `ensure_mysql_driver_compat(database_url)`，在**建引擎那一刻**把 aiomysql
   引用的那个名字指回 PyMySQL 自己给 bytes 用的编码器 `converters.escape_bytes`。它只在 URL 真是
   `mysql+aiomysql` 且那个名字当前不可调用时动手，所以 SQLite/asyncpg 一条路都不碰、且等上游修好后自动变成空操作。
   补完对不对不在 CI 里靠推理：`E:/tmp/m50/f_bytes.py` 在 3307 的真库上做了红/绿——
   补之前 `TypeError: 'str' object is not callable`，补之后 `_binary X'…'` 落库、
   回读 `b'\x89PNG\r\n\x1a\n\x00\xff\x00 msgpack …'` **逐字节相等**（判据是字节相同，不是「没报错」）。
3. F4：`services/api/pyproject.toml` 加 `[project.optional-dependencies] mysql = ["aiomysql>=0.3.2,<0.4", "pymysql>=1.2,<1.3"]`
   并 `uv lock`——锁文件只有 30 行变更（加两个包 + 一条 extra），其余 70 个包的版本号一位没动。
   上界 `<1.3` 是**被测出来的**：1.3.0 删掉别名后 aiomysql 会在 import 阶段就炸，那不是兼容函数能救的。

**这一层用实物回答的问题**

- 两引擎端到端各 59 步：**SQLite 59/59、MySQL 59/59**，`只在 MySQL 红 = 无`、`两边一起红 = 无`
  （`E:/tmp/m50/drive-both3.log`；改前两份 `drive-both.log` = MySQL 25 红、`drive-both2.log` = MySQL 2 红）。
- MySQL 侧不是「接口没报错」就算数，跑了库里的实物：服务端 `8.4.0:3307`，
  `agent_checkpoint_writes` **34 行 / 18,076 字节** msgpack（单行最长 7,005 字节，`hex()` 头部是
  `9283A4726F6C65…`＝msgpack 数组里嵌 `role`），`model_usage` 有账，服务日志里 `Traceback` **0** 条。
  删项目族之后 `tasks/threads/projects` 归零、而 `model_usage` 与账本仍在——§7.13 那条果实在 MySQL 上也成立
  （驱动步 54：`tokens 3912 -> 3912`）。
- 新用例 **9 条**：`tests/test_post_commit_refresh_v30.py` 6 条（改前实测 **4 failed / 1 passed** 的红基线）+
  `tests/test_mysql_driver_compat_v31.py` 3 条。其中 `test_mutating_routes_survive_a_dialect_without_insert_returning`
  用 `dialect.insert_returning = False` 把 MySQL 的 INSERT 形状在 SQLite 上**替身复现**——
  SQLAlchemy 2.1.4 不接受 `create_async_engine(insert_returning=False)`，但运行时翻这个标志可以，
  于是没有 MySQL 的 CI 也能守住这一整类缺陷。
- 变异电池 **13/13 红集合精确相等**，五份源码（skills/tasks/threads/team/db）还原后**逐字节一致**，收尾复跑回绿。
  其中 M10–M13 专门守 F3 的四条：接线（摘掉建引擎时那一次调用）、不动手、补错函数（`escape_string` 顶替）、
  对别的驱动也动手。M1 实测多带一条 ENABLE 红——那条用例第 69 行拿「先停用」当夹具，
  归因写进电池的注释，**没有把期望写宽**。
- 全量后端回归：**875 passed / 14 skipped**（`E:/tmp/m50/regression-50.log`；改前 866/14，净增就是这 9 条）。
  这批改动全在后端与依赖声明，前端一个字没动，故不跑 `npm test`/`tsc`，也没有浏览器点验。

**这一层还没覆盖的（量出来的，不是猜的）**

1. **PostgreSQL 仍没有实跑条件**（本机无可用实例）。`asyncpg` 在默认依赖里躺着，但 F2/F3 这两类在 PG 上
   成不成立没有任何实物——PG 的 INSERT 有没有 RETURNING、驱动转义 bytes 的形状，都没被真请求穿过。
2. **团队审批与续跑那几条路由在这个桩上到不了**：`waiting_approval` 需要写工具或 `ask_user` 才会出现，
   端到端只跑到 `default` 审批模式下的只读工具那一层。
3. **MySQL 侧的判定没进 CI**：`E:/tmp/m50/` 全是本机脚手架；CI 里只有 `insert_returning=False` 那个替身
   与「补的是不是同一个编码器」的断言。换库回归仍需手工再跑一次脚本。
4. **F3 是我们在替第三方补裂缝，不是上游修好了**。aiomysql 一旦发布修好的版本，
   `ensure_mysql_driver_compat()` 自动什么都不做；一旦 PyMySQL 1.3 删掉别名，上界 `<1.3` 就是硬门槛，
   届时只能换驱动（asyncmy 一类，Windows 轮子另是一笔账）或等 aiomysql 发版——需要有人裁决，不是代码能决定。
5. §7.14 未覆盖 3（`PRAGMA foreign_keys` 仍未开、库里其余 CASCADE 声明没处理）与 4
   （0023 的 NULL 闸门只在清库重建的 MySQL 上验过，存量库没验）沿用。

复现：

```bash
# 0) 先起自己的 MySQL 8.4（3307；3306 是用户自己的实例，一个字节都不碰）
E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m48/mysql48.py start
# 1) 端到端：同一段 59 步驱动，SQLite 对照 + MySQL 被测（日志里给出两份红清单与两引擎对照结论）
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m50/mysql_e2e.py --only sqlite,mysql
#    过程日志：E:/tmp/m50/drive-both.log（改前 MySQL 25 红）→ drive-both2.log（MySQL 2 红）→ drive-both3.log（两边全绿）
# 2) F3 的红/绿与逐字节回读（真库上的判定，判据是字节相同而不是"没报错"）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m50/f_bytes.py
# 3) 库里的实物：检查点 BLOB 行数与字节数、服务日志 Traceback 数（跑完驱动后立刻查）
# 4) 离线：9 条新用例 + 13 靶电池（跑电池期间不并行跑任何东西，电池独占源码）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_post_commit_refresh_v30.py tests/test_mysql_driver_compat_v31.py  # 9 passed
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m50/mutate50.py                                                                   # 13/13 精确命中
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q                                                                             # 875 passed / 14 skipped
```

### 7.16 第十七项落地：两方言判定从"我机器的 tmp 目录"搬进仓库与 CI（任务 #51，补 §7.15 未覆盖 3）

§7.15 那条结论当时只活在 `E:/tmp/m50/` 里：脚本、日志、桩、库全在本机，换个机器或换个人就跑不出来，
CI 里 MySQL 那一层**一条真库判定都没有**（只有 `insert_returning=False` 的替身与"补的是不是同一个编码器"）。
这一批把判定搬进仓库，并把它**该在什么场合闭嘴**写清楚——一个只会报绿的装置比没有装置更坏。

**装置进仓库**：`evals/dialect_e2e.py`（59 步真 HTTP，对照格 SQLite + 被测格 MySQL，判据仍是
**两边一起红 = 装置错；只有 MySQL 红 = 真方言分歧**；#52 起长成 68 步，步数由 `EXPECTED_STEPS` 自查，见 §7.17.5）。
从一次性脚手架改成能长期住的四件事：

1. **跳过与中止是两种退出码**：没给 `BLANK_E2E_MYSQL_URL` → 打印 `[SKIP]`、rc **0**；给了但连不上/库名不合法
   → 打印 `[ABORT]`、rc **2**；`--require-mysql`（CI 用）把前者也变成 rc 2。这样"这台机器今天没法判"
   不会伪装成"换库没问题"，也不会把 PR 冤枉成红。
2. **实物见证**：跑完驱动直接查 `agent_checkpoint_writes` 的行数与字节和——缺陷 C（§7.15 F3）复发时
   HTTP 那侧只表现为"任务红"，读不出"字节没写进去"，这一行读得出。
3. **桩逐次轨迹**（`stub-<engine>.log`，一行一次模型调用）：#50 把一次红归成"装置在抖"，靠的是桩只报了个总数；
   现在红了能查"这一格到底收到过那个请求没有"。
4. **抖动守卫**：第 19 步（任务运行到终态）认 `502/503 + ReadError/ConnectError/ConnectTimeout/PoolTimeout/RemoteProtocolError`
   这一类传输形状，换一个**全新任务**重来（≤2 次），重试次数打进那一步的详情里——不藏。
   真缺陷（500、`MissingGreenlet`、1101、`TypeError: 'str' object is not callable`）不许被这条洗绿。

**这一批自己量到的两处装置缺陷**（都是"装置说谎"，不是产品缺陷）：

| 编号 | 现场 | 修法 |
| --- | --- | --- |
| V1 | **对照格红、被测格全绿，退出码却是 0**（`E:/tmp/m54/drive.log`：SQLite 第 19 步 `HTTP 502 …（ReadError）`、MySQL **59/59** 全绿）。老代码写的是"只有被测格红才置非 0"，这种跑法 CI 会绿，而这次连**不做换库的对照**都没过不去，任何"换库成立"的结论都不该从它念出来 | 抽出纯函数 `verdict(totals)`：**判据要求每一格都绿**，任何一格红都交回 rc 1，并把归属分开念——「只有 MySQL 红（方言分歧候选）」「两格一起红（装置错）」「只有对照格红（判据不成立）」；只跑一格时明确写"对照判据不成立" |
| V2 | 桩轨迹每格开头 `unlink`，于是**第一格的轨迹被第二格删掉**——V1 那次红了才去读日志，读到的 10 行全是 MySQL 格的 | `STUB_LOG` 按格子命名（`stub-sqlite.log` / `stub-mysql.log`），两格各留一份 |
| V3 | 重试分支与"真缺陷不重试"那两条**当时没有可失败的门**：电池只守判据 `_is_transport_flake()`，分支整条封死也照样绿 | 新增流程图用例（打桩的假传输层按 `VERB 路径` 查表，断言调用序列与新任务 id 被后续步骤用上）；并把"真 500"那条从"抛了个 AssertionError"收紧成**钉住失败原因** `HTTP 500`——否则撤掉 500 闸门后它会走到别处再抛一个 AssertionError，测试照样通过（M8 实测出来的） |

**CI**：`.github/workflows/ci.yml` 新增 `api-dialect` job——`mysql:8.4` service（带 `mysqladmin ping` 健康门）、
`uv sync --extra mysql`、`uv run --extra mysql python ../evals/dialect_e2e.py --require-mysql`
（两步都带 `working-directory: services/api`——仓库根**没有** `pyproject.toml`，uv 的项目上下文只在 `services/api`，
从根目录 `uv run` 会找不到项目；装置自己按 `__file__` 找回仓库，`yaml.safe_load` 解出
jobs = `['web', 'api', 'api-dialect', 'team-e2e']`）。
**这台机器跑不了 GitHub 的 runner**，所以这一格的实跑证据只到"本机把 CI 会用的那条命令真跑通"：
`uv sync --extra mysql` 装得动、`cryptography 50.0.2` 在（`caching_sha2_password` 要走 RSA）、
并专门建了个用 `caching_sha2_password` + 口令写在 URL 里的账号 `m51` 来走 CI 那条认证形状。

**证据**

- 仓库装置两格同驱动（`E:/tmp/m55/drive.log`，rc=0）：**SQLite 59/59、MySQL 59/59**，
  判定行念的是「每一格全绿：对照判据成立」；第 19 步详情是 `succeeded`（**一次都没重试**，
  守卫只是兜底不是遮羞布）；检查点实物两格各 **34 行 / 18,118 字节**；服务日志里 SQL 相关行 **0**；
  删供应商那一步 SQLite 格 `tokens 4564 -> 4564`、MySQL 格 `5216 -> 5216`（§7.13 那条果实在第二种方言上仍在）；
  两份轨迹 `stub-sqlite.log`(8 行) / `stub-mysql.log`(11 行) 各自留下——V2 修好了才谈得上归因。
- 装置闸门用例 **7 条**：`services/api/tests/test_dialect_device_v32.py`（抖动识别 / 真缺陷不豁免 /
  重试真的换新任务并重放 / 真失败不重试 / 59 步一步不少 / 连接串解析与库名闸门 / **V1 那条 rc 判定**），1.22s 全绿。
- 变异电池 **11 靶全按实测红集合命中**（M1–M8 守判据与流程，M9–M11 守 `verdict`：红了也报 0、
  不念"只有对照格红"、把对照格认成被测格），装置源码 `evals/dialect_e2e.py` 还原后**逐字节一致**、
  收尾复跑回绿（`D:/Softwares/Qoder CN/tmp/m51/battery51d.log`）。
  其中两条是**跑出来的**：M5（清空标记表）实测比原期望多红一条流程图用例，M8（撤 500 闸门）
  在旧断言下**根本抓不住**——把"抛了 AssertionError"收紧成"必须钉住 `HTTP 500`"才成立；
  期望按实测改，没写宽。
- 全量后端回归：**882 passed / 14 skipped**（`E:/tmp/m54/regression-51.log`，rc=0；
  §7.15 的 875 + 本轮 7 条装置闸门）。前端一个字没动，故不跑 `npm test`/`tsc`，也没有浏览器点验。

**这一层还没覆盖的（量出来的，不是猜的）**

1. **本机桩抖的根因没定**。事实是：单格各跑四次全绿（`E:/tmp/m53/`），两格同进程时第 19 步偶发
   `model_unreachable（ReadError）`，而桩侧「服务 N 次 / 被截断 0 次」、api 日志里 `Traceback` 0——
   失败发生在请求还没被处理程序看见的地方（backlog / 半关连接 / Windows 的 RST 时序，本批没定出来）。
   守卫只是把"分不清"变成"看得见且重试一次"，**不是修根因**；CI 的 Linux runner 是不是也抖，要等那一次真跑。
   → **已结案（任务 #52，见 §7.17.3）**：根因是桩的 SSE 靠连接关闭定界，而 Windows proactor 在接收缓冲
   有未读字节时 `close()` 发 RST 而非 FIN，把已 flush 的响应一起丢；现在每段响应自带 Content-Length、
   `finish()` 半关读空，桩自检加到**连接/服务/截断**三个数，两格各跑 68 步实测「连接 19 = 服务 19 / 截断 0」
   且第 19 步一次都没重试。Linux 上是否同形仍等 CI 真跑（未覆盖见 §7.17 第 4 条）。
2. **PostgreSQL 仍零实跑**（§7.15 未覆盖 1 沿用）。
3. **审批与答题那两条路由在这个桩上仍到不了**（`waiting_approval`/`waiting_answer` 需要写工具或 `ask_user`），
   下一批补"高危命令被拒 + 答题续跑"那一格。
   → **已补（任务 #52，见 §7.17.1）**：驱动长出第 54–62 步，桩按消息里的探针标记换工具；
   第一次真跑就量出四处「离线绿、端到端不成立」，都修了并各有一条变异靶守着。
4. **带令牌那一路没进这一格**：装置起的实例没有 `BLANK_API_TOKEN`，`require_token` 直接放行为 `local-owner`
   ——桌面/本地形态；CI 这一格同样不覆盖带令牌的形状。
5. **MySQL 这一格的判定住进了 CI，但仓库里 #29–#51 这批改动一律还没 commit**，HEAD 仍是 `974f684`。

复现：

```bash
# 0) 起自己的 MySQL 8.4（3307；3306 是用户自己的实例，一个字节都不碰）
#    一次性脚手架 mysql48.py 已不在仓库里，用的是本机既有实例；端口与库名由环境变量给
# 1) 仓库里的装置：两格同驱动（CI 用 --require-mysql；没库时 [SKIP] rc=0）
cd E:/projects/kbgai/kbgai
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
BLANK_E2E_SCRATCH=E:/tmp/m55 services/api/.venv/Scripts/python.exe -X utf8 evals/dialect_e2e.py --require-mysql
#    过程日志：E:/tmp/m54/drive.log（V1 现场：对照格红、被测格全绿、老 rc 报 0）
#              E:/tmp/m54/stub-sqlite.log、stub-mysql.log（V2 修好后的两份轨迹）
# 2) 装置自己的闸门 + 变异电池（跑电池期间不并行跑任何东西，电池独占 evals/dialect_e2e.py）
cd services/api
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_dialect_device_v32.py     # 当时 7 passed（#52 起 17 passed，见 §7.17.5）
"E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" -X utf8 "D:/Softwares/Qoder CN/tmp/m51/mutate51.py"
# 3) 全量回归
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q
```


## 7.17 任务 #52：把「审批 / 答题 / 退路」三条到不了的路跑成实物，顺手给"本机桩抖"结了案

§7.16 留下两条自认的缺口：**未覆盖 3**（审批与答题这两条路由在这个桩上到不了）和
**未覆盖 1**（本机桩抖的根因没定，守卫只是"看得见且重试一次"）。这两条其实是同一件事的两面：
**判据只能测它到得了的路**，而"到不了"这件事本身，只有真去跑一次才会显形。
所以这一批先补装置，再让装置去量产品。

**这一批要立住的三句主张**（每句都能失败，判据读的都是接口或库里的实物）：

1. 「高风险写入 → 挂起成审批卡 → 批准 → 文件真写出」和「提问挂起 → 答题 → 答案回到上下文」不是只在
   离线单测里成立的路由；它们挂出来的卡、留下的痕、写出的文件，在证据包里逐条可对核。
2. 明文密钥不许出现在**任何**从事件流读出去的地方。产品口径一直是"密钥只留在用户批准的工作区文件里"，
   而过去只有 `FileChange.diff` 一处兑现。
3. 空流退整包那条退路是**被点名跑的一步**，不是等供应商抖了才被偶然踩到、踩到了也没人知道的一行代码。

### 7.17.1 驱动从 59 步长成 68 步

新增的是中间那九步，删除尾段整体顺延编号（原 `59 删除供应商` → 现 `63`）：

| 步 | 判据读的是哪里 | 实测原话（两格同一份） |
| --- | --- | --- |
| 54 高风险写入挂起成审批卡 | 接口返回的任务状态 + 卡片列表 | `status=waiting_approval，卡片工具=apply_patch 风险=high` |
| 55 审批卡内容可核对 | 卡片本身（风险、状态、命中的规则、剩余 TTL） | `风险=high 状态=待审批 规则=risk:ask 剩余 TTL=5.0 分钟` |
| 56 审批卡走答题口报 409 | 两个错误码各归各位，不互相冒充 | `no_pending_question / task_not_waiting_approval` |
| 57 批准后跑到终态且文件真写出 | 工作区的文件 + 审批轨迹三条事件 | `文件已写出，审批轨迹 ['approval.decided', 'approval.granted', 'approval.required']` |
| 58 明文密钥不落进库里的 diff | 库里那份 diff 与**工作区原件**两头对 | `库里 1 处打码，工作区原件保留密钥（107 字 diff）` |
| 59 驳回后不执行且留痕 | 终态、变更计数、`approval.denied` + `task.paused` | `status=paused，变更 0 处` |
| 60 提问卡挂起并可答 | 卡片正文 + TTL | `status=waiting_approval，问题=「这份资料要放在工作区的哪个目录下？」TTL 剩 30.0 分钟` |
| 61 答题后答案进了上下文 | 时间线里的问题四事件 **加一条 `tool.completed`** | `轨迹 ['question.answered', 'question.asked', 'question.decided', 'question.required']` |
| 62 流式空帧要退回整包调用 | `model.stream_fallback` 的次数与 `code`，加退路之后的 `model.streamed` | `status=succeeded｜退路 1 次 code=['model_stream_empty']｜之后正常流式 2 次` |

桩怎么"只在该换的时候换"：三条**探针标记**写在任务目标文本里（`PROBE-APPROVAL-42` /
`PROBE-QUESTION-42` / `PROBE-STREAMFLAKE-42`），桩只在这串出现在消息 JSON 里时才换成对应工具或
换成空流，其余调用照常。这条边界由 `test_the_stub_only_switches_tools_on_the_probe_marker` 钉住，
X4 变异（把 `in` 换成 `==`）实测就是它红。

### 7.17.2 量到的四处「离线绿、端到端不成立」

**① 信封缺陷——这一条里装置自己说得最响的谎。** 桩的非流式那一支发的是裸的
`{"name", "arguments"}`，而产品侧 `tool_call_args` 只认 `call["function"]["arguments"]`（或 `args`）。
读出来是空参数 ⇒ `apply_patch` 退化成"缺少必填参数：patch"的 error 调用 ⇒ 风险算成 medium ⇒
按 `risk:auto` 直接放行 ⇒ **一张审批卡都挂不出来**。而这条路只有撞上"流式退非流式"的退路才会走到，
所以单跑一格全绿、两格同跑才红——红的还全是装置自己。现在两支共用同一个 `_envelope(call)`，
并由 `test_both_stub_branches_use_the_openai_tool_call_envelope` 拿**产品自己的**
`tool_call_name` / `tool_call_args` 去解这两条分支的响应，而不是照桩的脚本念一遍。

**② 密钥只在一条写入路径上打码。** 第 58 步第一次真跑就抓到：diff 是干净的，
`approval.required` / `tool.requested` / `approval.granted` / `task.waiting_approval` 这四类**事件载荷**
走的是另一条写入路径，一个都没打码 ⇒ `/tasks/{id}/evidence` 的整包 JSON 里能捞出原件，
而前端是逐条渲染时间线的——等于把密钥回显给任何能打开这个任务的人。
收口收在唯一那条写入边界 `EventRecorder.add()`（`services/agent/events.py:47`）：`mask_deep` 递归过载荷。
顺带把 `log_safety.mask()` 的兜底补上——`redact` 认的是"键=值"那一类，`password = "…"` 这类形态
它**一个字都不改**（`contains_secret` 却认得出），边界上留了空子就等于没设边界。
两条不放宽的东西：打码只动库里这份证据，不动检查点里模型真正拿去执行的那份参数，
也不动用户批准之后落到工作区的文件（用例第 109 行就是在数工作区那份里密钥**恰好出现 1 次**）。

**③ 答题那一路跳过 `tool.completed`。** 过去它 `continue` 得太早，`tools` 台账里有那一行，
但**时间线**——前端逐条渲染、审计逐条回放的那一份——上没有；第 61 步实测：答完题的时间线里
一个 `tool.completed` 都没有，于是"答案回到了模型上下文"这句话在证据里拿不出可对核的实物。
它和普通工具是同一种"调用已完成"，就按同一种记（`graph.py` 问题分支里补上那一次 `recorder.add`）。

**④ 诊断被自己的截断遮住。** `Drive.step` 只存 `[:400]`，`_why` 又把 1500 字预算先给时间线，
结果每一步红都念不到真正的原因。现在 FAIL 全文存、`_why` 第一句就是「终态原因」
（从 `task.failed` / `model.stream_fallback` / `node.failed` / `validation.completed` 这些事件的**载荷**里取），
许可模式与高风险自动放行值跟在后面，时间线降为最后一句；取证通道自己断了就明写「取证失败」，
而不是拿一句"没读到所以通过"糊过去。`test_the_diagnostics_lead_with_the_failure_reason` 钉这三件事。

### 7.17.3 「本机桩抖」结案（§7.16 未覆盖 1）

**事实链**（都是量出来的，不是猜的）：单格各跑四次全绿、两格同进程时第 19 步偶发
`model_unreachable（ReadError）`；桩侧「服务 N 次 / 被截断 0 次」对得上，api 日志里 `Traceback` 0
⇒ 失败发生在**请求还没被处理程序看见**的地方。

**根因**：桩的 SSE 响应既没有 `Content-Length` 也不是分块传输，靠连接关闭定界；
而 Windows 的 proactor 在 `close()` 时若接收缓冲还有未读字节，发的是 **RST 而不是 FIN**——
已经 flush 出去的那段响应字节跟着一起丢。客户端读到的就是 `ReadError`，
服务端却以为自己做完了。这正好解释"服务次数对得上、却没被读到"。

**处置**（两层，都不放宽产品限制）：

- 桩发出的**每一段**响应自带边界：SSE 先把帧拼完整、按 `Content-Length` 发，再 `flush` +
  `close_connection = True`；`finish()` 改成 `flush` → `shutdown(SHUT_WR)`（先把 FIN 送出去）→
  把接收缓冲读空 → 才 `close()`。
- 桩自检从两个数变成**三个数**：本轮**连接 / 服务 / 截断**。判据写在装置输出的同一行里——
  `连接 > 服务` = 有请求没被读到；任一非零 = 装置在抖，**红了不算产品缺陷**。
  另外每格跑完各留一份逐次轨迹（`stub-sqlite.log` / `stub-mysql.log`），"红了分不清是谁"这件事
  从此有一份可以读的账。

**复核**：修完之后 `E:/tmp/m66`、`E:/tmp/m68` 两格同驱动，每格 **连接 19 = 服务 19 / 截断 0**，
且第 19 步 `succeeded`、**一次都没重试**（重试守卫退成兜底，不再是遮羞布）。

### 7.17.4 退路从「偶发」变成「点名的一步」

产品侧的形状一直没变过：`_call_adapter` 里流式抛 `ModelError` / `httpx.HTTPError` ⇒ 先落一条
`model.stream_fallback {reason, code, role, trace_id}` ⇒ 再重试非流式 `bind_tools`。
变的是它**第一次有了确定性的入口**：第 62 步要求桩在收到带 `PROBE-STREAMFLAKE-42` 的流式请求时，
确定地回一段**只有注释、没有任何 `data:` 帧**的 SSE ⇒ 产品解析不出任何东西 ⇒
`model_stream_empty` ⇒ 必须退整包。三条判据都读接口实物：退路**恰好 1 次**；
退路之后**仍有正常 `model.streamed`**（若那一次失败把流式开关整个关掉，这条就红——
这是"退了一步"和"被打死了"之间唯一的区别）；`code` 原样念出来。两格实测同为
`退路 1 次 code=['model_stream_empty']｜之后正常流式 2 次`。

### 7.17.5 步数不再需要"改一步改三处判据"

`EXPECTED_STEPS = 68` 由 `Drive.finish` 自己数着对账（少一步、多一步、跑两步都当场红，X5 变异实测）；
CI 那一步的名字也不再写死数字。

**证据**

- 两格同驱动（`E:/tmp/m68/both.log`，rc=0）：**SQLite 68 步失败 0、MySQL 68 步失败 0**，共 136 行 PASS，
  判定行念的是「每一格全绿：对照判据成立」；检查点实物两格分别是
  `agent_checkpoint_writes` **172 行 / 79,700 字节**（sqlite）与 **172 行 / 79,699 字节**（mysql）；
  服务日志里 SQL 相关行 **0**；每格桩自检 连接 19 / 服务 19 / 截断 0。
- 只跑对照格（`E:/tmp/m67/sqlite.log`）：68/68 全绿，装置仍然明确念
  「只有一格——两方言对照的判据不成立，这一份不能当成『换库没问题』的证据」。
- 装置闸门用例 **17 条**（`tests/test_dialect_device_v32.py`，**2.95s**；这一批之前是 182.40s——
  `resume` 守卫那条一直在等满 180 秒死线。现在 `wait_seconds`/`poll_seconds` 可注入，
  且 `paused` 被显式留在 `RESUME_TERMINAL` 之外：没放行绝不能算走到了终态）。
- 密钥边界用例 **3 条**（`tests/test_event_secret_boundary_v33.py`）：先证明空子存在
  （`redact` 对 `password = "…"` 一个字都不改，这条断言反过来还能发现"兜底被撤走"），
  再证明堵住；证据包那条**两头都判**——库里/接口 0 处密钥、工作区文件恰好 1 处。
  答题那格所在的 `tests/test_question_v12.py` 9 条一起绿（三个文件 29 条 / 6.63s）。
- 变异电池 **10 靶全按实测红集合命中**（`E:/tmp/m69/battery2.log`）：X1–X7 守装置
  （信封、Content-Length、探针帧、探针标记、步骤数、`paused` 不算终态、诊断先念原因），
  P1–P3 守产品的三处收口（形态兜底、事件边界打码、答题落 `tool.completed`）。
  四条是**跑出来的、不是想出来的**：X4 / P1 / P2 的期望起初写宽了——`mask_deep` 自己的用例是直接打函数、
  不经事件写入路径，撤掉调用点它照绿；把这种绿算成"没抓住"就是写宽。P3 起初命中 0 次，原因是
  `graph.py` 存 **CRLF** 而锚点按 LF 构造，多行锚点在它里面永远匹配不上——那会伪装成"靶不成立"，
  实际上是靶根本没被注入；`replace()` 现按两种行尾各试一次。四个文件还原后**逐字节一致**，收尾复跑回绿。
- 全量后端回归：**894 passed / 14 skipped**（`E:/tmp/m68/regress.log`，rc=0，536.02s；§7.16 是 882）。
  这一批前端一个字没动，故不跑 `npm test`/`tsc`，也没有浏览器点验——新增的三条主张全在后端证据层。

**这一层还没覆盖的（量出来的，不是猜的）**

1. **PostgreSQL 仍零实跑**（§7.15 未覆盖 1 沿用）。
2. **带令牌那一路不在这格**：装置起的实例没有 `BLANK_API_TOKEN`，`require_token` 直接放行为 `local-owner`。
3. **审批卡走团队群聊路径**（成员挂卡 → 群里谁来批）仍未端到端；第 54–59 步测的是单任务路由。
4. **Linux runner 上是否同形**：根因量在 Windows 的 proactor 上，CI 那一格要等真跑；
   区别是现在有了三数自检，真抖了能当场分清是装置还是产品。
5. **退路只测了一种形状**：`model.stream_fallback` 这一格走的是"空流"，
   网关 4xx / 连接超时那几类退路仍只有代码依据，没有实物。
6. **`#52` 里产品的四处修改没有一处放宽限制**：`safety.py` / `permissions.py` / `sandbox/policy.py`
   一个字没动；`risk:ask` 命中的高风险写入仍然是"人不批就不执行"。
7. **#29–#52 这批改动一律还没 commit**，HEAD 仍是 `974f684`（未 push）。

复现：

```bash
# 1) 两格同驱动（68 步；步数由装置的 EXPECTED_STEPS 自查，CI 步骤名里不写死）
cd E:/projects/kbgai/kbgai
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
BLANK_E2E_SCRATCH=E:/tmp/m68 services/api/.venv/Scripts/python.exe -X utf8 evals/dialect_e2e.py --require-mysql
#    只跑对照格（68/68，但判定行会自己说"对照判据不成立"）：--only sqlite
# 2) 装置闸门 + 密钥边界 + 答题那格（29 passed / 6.63s）
cd services/api
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q \
  tests/test_dialect_device_v32.py tests/test_event_secret_boundary_v33.py tests/test_question_v12.py
# 3) 变异电池（跑电池期间不并行跑任何东西：它独占 evals/dialect_e2e.py 与三个产品源文件）
.venv/Scripts/python.exe -X utf8 E:/tmp/m69/mutate52.py          # 10 靶，汇总「未按预期 0」
# 4) 全量回归
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q   # 894 passed / 14 skipped
# 5) Codex 本机 rollout 的事件类型清点（§6 那一行里 4,041 / 4,740 两个数的出处）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m70/codex_types.py
```

## 7.18 任务 #53：带令牌那一路真穿一遍，退路的另两种形状量出「不该退」

§7.17 自己记了两条欠账：**未覆盖 2**（装置起的实例没有 `BLANK_API_TOKEN`，`require_token` 一路放行成
`local-owner`，带令牌那一路一次都没被真 HTTP 穿过）和 **未覆盖 5**（退路只测了"空流"一种形状，
网关 4xx 与超时仍只有代码依据）。这一批把两条各跑成一格，判据仍然只读接口、库和事件流里的实物。

**这一批要立住的三句主张**：

1. 设了令牌之后，**每一条**业务接口匿名打都被拒，接口面（`/openapi.json`、`/docs`、`/redoc`）不许匿名可读；
   而带正确的令牌必须一条都不被挡——两个方向都要有实物，不能只靠"路由挂了 `Depends`"推理。
2. 退路只在**换个说法有可能变成功**时走：400 拒 stream ⇒ 退回整包并把活干完；429 / 5xx / 超时 ⇒ 立刻报原因，
   一次都不多打。两种形状都得是被点名跑的一步。
3. 供应商给的状态码要**骑在 `ModelError` 上**到达闸门；`BLANK_MODEL_TIMEOUT_SECONDS` 要真的接到适配器。

### 7.18.1 驱动从 68 步长成 74 步

装置起服务时多给两个环境变量：`BLANK_API_TOKEN`（把服务摆在"带令牌"那一形态）与
`BLANK_MODEL_TIMEOUT_SECONDS=6`（让"模型太慢"这一格在一轮里跑得完）。桩加三条支路：
带 429 标记的请求一律限流、带 400 标记**且** `stream=true` 才拒（整包照旧好）、带慢标记的睡 12 秒。

| 步 | 判据读的是哪里 | 实测原话（两格同一份） |
| --- | --- | --- |
| 69 匿名打每一条接口都被拒 | 真 HTTP：把 `/openapi.json` 枚举出的每条操作都不带头打一遍 | `匿名全量 82 条都 401，公共面只有 /api/v1/health` |
| 70 接口面不匿名可读 | 三个接口面的匿名 / 带令牌状态码对照 | `匿名 {'/openapi.json': 401, '/docs': 401, '/redoc': 401} / 带令牌 {…: 200}` |
| 71 带令牌不应当被鉴权挡住 | 全量带令牌再扫一遍 + 一条错令牌 | `带令牌 0 条被鉴权挡住；错令牌 401` |
| 72 网关拒 stream 时退回整包并把活干完 | `model.stream_fallback` 事件（次数与 `status`）+ 终态 + 工具成功 | `status=succeeded｜退路 3 次都带 400｜跳过 0 次｜工具成功 1 个｜正常流式 0 次` |
| 73 两种形状都 429 时不许静默成功 | 接口的 502 与 `error.code`、**不退路**、`model.stream_fallback_skipped` | `status=failed｜HTTP 502 code=model_http_error｜不退路 0 次｜跳过记录 1 次｜原因=模型返回 429…` |
| 74 模型太慢要按超时失败 | 墙钟耗时上界 + 原因文本 + 跳过记录 | `status=failed｜耗时 6.1s（上界 9.0s）｜原因=模型请求超时：stub-slow｜跳过记录 1 次` |

第 72 步的"退路 3 次"不是笔误：一次运行里三个模型节点各撞一次 400，各退一次。判据因此钉的是
"每一次退都带着 400"与"活干完了"，而不是某个魔法次数——写死 1 次会把这条判据变成对实现细节的过敏。

### 7.18.2 量出来的四处实物缺陷（离线全绿、真跑不成立）

1. **接口面匿名可读**。`create_app()` 里 `docs_url` / `redoc_url` / `openapi_url` 从来没参与 `guarded` 判定：
   只要设了令牌，`/openapi.json` 照样把 **83 条操作 / 65 条路径**整份交给不带头的请求。它自己不返回业务数据，
   但它是这张 API 的目录，攻击面从这里开始。修法：设了令牌时关掉三处内建路由，另挂三条同路径、
   带 `Depends(require_token)` 的路由（`app.py:_mount_guarded_docs`）。不设令牌时三处照旧匿名可用——
   桌面版单机是那个形态，不能为了护栏把自家界面拦死。
2. **`BLANK_MODEL_TIMEOUT_SECONDS` 是死配置**。它只住在 `Settings` 里，`build_adapter()` 从没把它写进
   `AdapterConfig.timeout_seconds`，dataclass 的 60 秒一直是实际生效值：调到 300 秒仍 60 秒截断，
   调到 5 秒也不会更快失败。现接进适配器（非正数退回 60.0，写成 `60s` 这种脏值由 pydantic 当场报错，不静默取默认）。
3. **退路是无条件重放**。`_call_adapter` 里只要一行文字都没收到，就把同一番话换成整包再打一遍。
   桩把 429 与"睡 12 秒"演出来之后实测（第一版装置的读数，不是推演）：限流那次**请求翻倍**、
   超时那次用户多等**整整一个超时窗口**，答案一个字都不会变——429/5xx 拒的是"现在能不能问"，超时是"还没答"，
   都与问法无关。现加准入函数 `graph._fallback_can_help()`：只认 `model_stream_empty`、`model_unreachable`
   和**读得出 400 的** `model_http_error`，其余一律立刻失败并落一条 `model.stream_fallback_skipped`
   （有意不退也要留痕：证据包里"没有退路事件"与"这里本来不需要退"是两回事）。
4. **`stream_transport` 是一条死分支**。`_call_adapter` 里 `except httpx.HTTPError` 那一支今天**不可达**：
   四家适配器 8 个调用点全走 `base._client()`，它在出口就把 `httpx.TimeoutException` / `HTTPError`
   翻译成了 `ModelError("model_timeout" / "model_unreachable")`。分支留着是纵深防御（哪天有人绕过
   `_client()` 自己 new 客户端仍兜得住），但**不能当实测过的路径写进结论**——桩能演"连不上"，演不出"绕过翻译层"。

为了让第 3 条站得住，`ModelError` 多了一枚 `status`（keyword-only，缺省 0 = "没走到有状态码那一步"，
与 400 是两回事），四家适配器 + `base._request_json` 共 8 处抛出点带上 `resp.status_code`。
唯一没带的那一处是 `openai_responses` 里"HTTP 200 但响应体自带 `error`"——那一刻确实没有状态码可带，
按规则不退，行为保守但不会多花钱。

### 7.18.3 证据

- 离线新用例 **29 条**：`test_auth_surface_v34.py` 5（枚举走的是**真解析 `/openapi.json`**，不是 `app.routes`——
  FastAPI 0.143 的 `app.routes` 只给文档路由加一个 `path=None` 的 `_IncludedRouter`，拿它做集合相减是**空集装置**）、
  `test_model_timeout_v35.py` 4、`test_stream_fallback_gate_v36.py` 20（准入表 10 + 状态码缺省 1 +
  **三条 lane × 三种状态码 9**：从真 HTTP 响应读出 400/429/503，`stream()` / `bind_tools_stream()` / `invoke()`
  各自都要把状态码骑到 `ModelError` 上再交给闸门——闸门只测"给定 status 怎么判"，这一组测"status 从哪来"）。
- `test_stream_output.py` 把旧的"流式一失败就退整包"**拆成两条互补判据**（旧那条没有失败能力：怎么改都绿）：
  形状问题 ⇒ 退并把活干完；形状无关 ⇒ 不退、立刻抛、且 `(code, status)` 逐条对得上跳过记录。
- 装置两格各 **74/74**（`E:/tmp/m72/drive-both.log`，rc=0，判定行「每一格全绿：对照判据成立」——
  §7.17 那份只有 sqlite 一格，读数是「两方言对照的判据不成立」，这批补齐了第二格）；
  每格桩自检**连接 27 = 服务 27 / 截断 0**；两格 `agent_checkpoint_writes` **行数 234 / 字节 101,101 同形**；
  服务日志里与 SQL 有关的行数 = 0。
- 变异电池 **14 靶全按实测红集合命中**（`E:/tmp/m73/mutate53.py`，7 个文件还原后**逐字节一致**）：
  A1–A3 守鉴权面（撤 401、写死 `guarded=False`、把 `openapi_url` 放回内建路由——第三条专守
  "护栏是不是真挡在前面"，因为先注册的那条匿名路由会赢）、B1 守超时接线、C1–C3 守三条 lane 的状态码
  （三处抛错文本一模一样，必须各有靶，否则另一处替它挡着）、D1–D3 守闸门（放宽成"凡 4xx 都重放" /
  只删留痕 / 整个关掉，三条红集合互不重叠才分得清坏在哪一处）、E1 步骤清单与驱动对不上、
  **F1–F3 守装置自己**（不带令牌起服务 ⇒ 69/70/71 当场红；不缩短超时 ⇒ 74 红；桩不演 400 ⇒ 72 红——
  这三靶产品代码一个字没动，红的是格子；判据抓不住就是"绿得没有依据"）。
  三条期望起初写宽了、按实测改窄：A2/A3 里 `test_every_operation_rejects_anonymous` **不红**
  （路由依赖没被拆，401 照样出，那条测的是另一件事）、B1 里 `test_the_timeout_is_actually_applied_to_the_client`
  **不红**（它读的就是默认 60 秒，正是拆掉接线后的值）；D2 起初与 D3 混成一条（我删的是整个闸门分支而不是留痕），
  按实测拆成两条互补靶。
- CI：`uv run pytest -q` 覆盖上述离线用例，`api-dialect` job 以 `--require-mysql` 跑这 74 步 ⇒
  带令牌那一路与退路三种形状在 CI 里是真判定，不是本地手工一次。
- 全量后端回归：**926 passed / 14 skipped**（rc=0，239.90s；§7.17 是 894）。
  这一批前端一个字没动，故不跑 `npm test` / `tsc`，也没有浏览器点验——新增主张全在后端证据层。

**这一层还没覆盖的（量出来的，不是猜的）**

1. **PostgreSQL 仍零实跑**（§7.15 未覆盖 1 沿用）。
2. **`stream_transport` 死分支没删**：它是绕过 `base._client()` 时的纵深防御，今天没有任何一条路走得到它，
   也就没有任何一条判据在守它。
3. **审批卡走团队群聊路径**（成员挂卡 → 群里谁来批）仍未端到端；69–74 与 54–61 测的都是单任务路由。
4. **超时那一格只验了"会按时失败"**，没验"多久算慢"：默认 60 秒现在真的生效了，慢推理模型要不要更大窗口是运维决策，
   而闸门对 `model_timeout` 的裁决是**不退**——反例是"只会 hang SSE 的代理"，那种情况要靠 `stream_output` 开关绕开，
   判断依据读 `model.streamed` 那些数字（本格读数：正常流式 0 次，因为桩被点名不流）。
5. **限流退避没做**：429 现在是"立刻报原因"，`retryable` 标了但没人据它退避重试——那是另一批的事。
6. **#29–#53 这批改动一律还没 commit**，HEAD 仍是 `974f684`（未 push）。

复现：

```bash
# 1) 两格同驱动（74 步；步数由装置的 EXPECTED_STEPS 自查）
cd E:/projects/kbgai/kbgai
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
services/api/.venv/Scripts/python.exe -X utf8 evals/dialect_e2e.py --require-mysql
#    只跑对照格（74/74，但判定行会自己说"对照判据不成立"）：--only sqlite
# 2) 这批的四组离线用例（41 passed）+ 装置闸门（17 passed）
cd services/api
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q \
  tests/test_auth_surface_v34.py tests/test_model_timeout_v35.py tests/test_stream_fallback_gate_v36.py \
  tests/test_stream_output.py tests/test_dialect_device_v32.py
# 3) 变异电池（跑电池期间不并行跑任何东西：它独占 dialect_e2e.py 与六个产品源文件，
#    而电池里的 F1–F3 每一靶都要真跑一遍装置）
.venv/Scripts/python.exe -X utf8 E:/tmp/m73/mutate53.py          # 14 靶，汇总「未按预期 0」
#    先实测红集合再钉期望：加 --probe（只注入只记录，不判定）
# 4) 全量回归
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q   # 926 passed / 14 skipped
```

## 7.19 任务 #54：批准记忆收窄到「这一条命令」，卡片文案与账本规则同源

Codex 的审批记忆落在 `~/.codex/rules/default.rules`：一条 `prefix_rule` 的 pattern 是**完整 argv**，
`-Command` 的正文整段都在规则里，所以它记住的是「这一条命令」。我们之前记的是
`command_prefix(command)`——解释器命令只到 `python` 这一段。这个差别不是措辞问题，是逃逸面：

- 探针（`E:/tmp/m74/approval_granularity_probe.py`，读数 `E:/tmp/m74/probe-out.log`）批准
  `python -c "print(1)"` 并选择「本会话总是允许 python」之后，以下四条**一张卡都没再挂**就被放行：
  `python evil.py`、`python -m pip install requests`、`python -m http.server 8765`、
  一段把 `~/.ssh/id_rsa` 读出来写进工作区的内联脚本。
- 卡片与账本还是两套算法：卡片上写「本会话总是允许 python」，账本里记的也是 `python`，
  只是这个「X」恰好是错的宽度；不可逆命令被客户端硬传 `scope=prefix` 时，`git push --force`
  会被记成 `git push`，连正常的 `git push` 一起放行。

**这一批要立住的三句主张**：

1. 卡片上的「本会话总是允许 X」与账本真正记下的规则必须是**同一串**；解释器类必须带到载荷
   （`-c` 正文 / `-m` 模块名 / 脚本路径），裸解释器不记；首段走 basename，换一种路径写法仍能命中。
2. 不可逆命令与含密钥形态的规则**不给前缀档、也不入账**——即使客户端硬传 `scope=prefix`。
   Codex 的 `default.rules` 是明文一行一条 `prefix_rule(...)`（本机实物：
   `prefix_rule(pattern=["C:\\…\\pwsh.exe", "-Command", "$env:CI='true'; pnpm install --frozen-lockfile"], decision="allow")`），
   argv 里带密钥就会原样落盘；这一点我们不照抄。
3. 批准 / 驳回留痕写的是**实际学到的规则**；`approve_high_risk` 的归因写
   `flag:approve_high_risk`，不再冒充 `mode:bypass`。

**实现**

- `permissions.py`：`memo_rule()` 是唯一口径——解释器跳过 `-u`/`-I` 这类旗标，停在 `-c`/`-e`/`-m`
  的载荷或脚本路径上；裸解释器返回空元组；含密钥形态（`contains_secret`）返回空元组；
  `render_rule()` 给带空格的 token 加引号，`python -c 'print(1 + 2)'` 写进账本再经 `prefix_rule`
  读回来逐字相等（不加引号会被切碎）。`Decision.memo` 携带这一串，`decide()` 只算一次；
  `approval_options(tool, memo)` 只在 memo 非空时追加前缀档；`remember()` / `forget()`
  对不可逆命令再自守一层，前端误传 scope 也记不出宽账。
- `graph.py`：中断值的 `suggested_prefix` 与 `approval_options` 都用 `decision.memo`；
  `approval.granted` / `approval.denied` 的 `prefix` 字段写 `learned = decision.memo`
  （`scope=prefix` 时），与账本同源。
- `gateway.py`：待审批项同样取 `decision.memo`，图上与网关口径不再分叉。

**装置从 74 步长成 76 步**

第 75 步用全新项目与任务跑第一条命令 `python -c "print(1)" https://a.example`；URL 是有意加的——
`run_shell.assess_risk` 只把破坏性命令与联网命令判成 high，不带 URL 的内联代码是 medium，
默认模式会直接放行（见「还没覆盖的」第 1 条）。第 76 步批准 `scope=prefix` 后，模型换第二段正文
`python -c "print(2)" https://b.example`，判据要求重新挂卡、且留痕写的是第一段的规则。

第一版两格都红在 `capability:shell_disabled`：装置默认 `shell_enabled=false`，能力边界在挂卡
**之前**就把工具拒了，测的根本不是批准记忆。修法是给这个**临时实例**（设置文件在 scratch 下）
`PUT /settings/agent` 打开 `shell_enabled` 与 `network_enabled`；产品默认值一个字没动——
「装好就能跑命令」仍然是不允许的。

两格读数（`E:/tmp/m75/drive-both-r2.log`，同一份）：

| 步 | 实测原话 |
| --- | --- |
| 75 | `status=waiting_approval｜前缀=python｜建议=python -c print(1)｜本会话总是允许 python -c print(1)` |
| 76 | `第二段重新挂卡｜留痕 prefix=python -c print(1)｜规则=['action:approved', 'action:approved']` |

两格各 **76/76**、rc=0、判定行「每一格全绿：对照判据成立」；每格 82 条匿名全 401、
带令牌 0 条被挡、桩自检**连接 31 = 服务 31 / 截断 0**、`agent_checkpoint_writes` **283 行 /
122,188 字节**、服务日志与 SQL 相关的行数 **0**。

**离线用例**

`test_approval_memo_v37.py` **25 条**：规则粒度 15 组参数（含裸解释器、绝对路径、`-I -u -c`、
`-m`、脚本参数）、路径归一、正文往返、一段正文不覆盖另一段、不可逆不入账、
含密钥不展示也不记、事件边界打码、`approve_high_risk` 归因、网关建议前缀、以及一条
走真网关的中断—批准—第二段重挂卡的端到端。`test_permission_modes_v11.py` 补了
「裸解释器不给前缀档」「硬传 prefix 也记不出危险前缀」两组；两文件合计 **57 条**。

**变异电池（12 靶）**

`E:/tmp/m75/mutate54.py`。先 `--probe` 实测每靶红集合
（`E:/tmp/m75/battery54-probe3.log`）再逐字钉进期望，正式跑（`E:/tmp/m75/battery54.log`）
**12/12 RED-OK**，额外与漏均为空，`permissions.py` / `graph.py` / `gateway.py` /
`dialect_e2e.py` 四份文件 sha256 **逐字节还原**。

靶面：M1 撤解释器分支（顺带量出裸 `python`/`node` 也红——`MEMOIZABLE_EXES` 里本来就有它们，
旧口径正是从这条缝漏的）、M2 规则串不加引号、M3 无条件给前缀档、M4 不可逆带 memo、
M5 账本自守拆掉、M6 密钥守卫关掉、M7 图上建议前缀退回首段、M8 留痕退回首段、
M9 归因冒充 bypass、M10 网关丢 memo、M11 规则截到旗标、M12 步数自检。
实测纠正了两处「靠推理会写错」的地方：按直觉 M1 会漏掉 `bare-python` / `bare-node`
（`MEMOIZABLE_EXES` 里本来就有它们，撤掉解释器分支后这两个参数确实该红），
M3 会漏掉 v11 的两条旧用例——写宽和写窄都算未按预期，最终按实测逐字钉死。

**回归**

全量后端 **951 passed / 14 skipped**（rc=0，250.99s，`E:/tmp/m75/regression54-r2.log`）。
第一次跑是 950 passed + 1 条计时用例红：`test_read_only_calls_overlap` 实测 **0.4534s**
（上界 0.45s），但交错日志证据成立，单独复跑 **3/3** 通过——判为本机负载下的计时抖动，
判据没有放宽。这一批前端一个字没动，故不跑 `npm test` / `tsc`，也没有浏览器点验。

**还没覆盖的（量出来的，不是猜的）**

1. **中风险内联代码根本不挂卡**。`assess_risk` 只认破坏性命令与联网命令；
   `python -c "open('x','w').write('…')"` 在默认模式下是 `risk:auto` 直接执行，
   与批准记忆无关。本批修的是「高危命令被记住后不得放宽」，要不要把一切内联解释器代码
   抬成 high 是产品裁决（抬了会显著增加挂卡频率）。
2. **`scope=tool` 仍是一档宽放行**：点一次「本会话总是允许 run_shell」后，后续 `run_shell`
   调用不再挂卡（不可逆命令除外）。这是既有产品语义，本批没有改；是否也按风险收窄，待裁决。
3. **批准记忆只活在进程内**：没有像 `default.rules` 那样落盘，重启即失忆；持久化规则的
   读写、冲突与迁移都没做。
4. **规则串的引号语义只覆盖 POSIX 空白**：`tokenize` 是 `shlex(posix=False)` 再手剥一层引号，
   PowerShell / cmd 的转义与续行没有等价验证。
5. **团队群聊路径的审批卡仍未端到端**（§7.18 未覆盖 3 沿用）。**已由任务 #55 结案，见 §7.20**。
6. **#29–#54 这批改动仍未 commit**，HEAD 仍是 `974f684`（未 push）。

复现：

```bash
# 1) 这批的离线用例（57 passed）
cd services/api
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q \
  tests/test_approval_memo_v37.py tests/test_permission_modes_v11.py
# 2) 两格装置（76 步；步数由 EXPECTED_STEPS 自查）
cd ../..
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py --require-mysql
# 3) 变异电池（跑电池期间不并行跑任何东西）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m75/mutate54.py   # 12 靶，汇总「未按预期 0」
#    先实测红集合再钉期望：加 --probe（只注入只记录，不判定）
```

## 7.20 任务 #55：群聊里的审批卡——主理人批、成员拒，两条路都走成实物

群聊的审批不是单任务审批的复制品：`POST /team/messages/{id}/approval` 只把决定写回**那条消息的任务**，
续跑在后台（202），编排器随后还要接着推队列；消息状态、轮状态、下一个发言者三处都得对上。
离线用例 `test_team.py::test_approval_waits_then_resumes_the_same_turn` 与
`test_origin_run_v27.py::test_approved_resume_is_the_same_round_not_a_new_one` 已经走过这条链，
但跨方言装置里一步都没有——§7.18 未覆盖 3 因此一直挂着。

**装置从 76 步长成 80 步**

用第 38 步那个群（主理人「小白管家」+ 成员「小查」），把 `max_speakers` 收到 2：
这一轮只需要「主理人先答 → 被 @ 的成员接着说」，不会多出收尾回合把卡的数量搅浑。
发一条同时带探针密钥标记与 `@小查` 的群消息，两条路依次走：

| 步 | 判据 | 实测原话 |
| --- | --- | --- |
| 63 | 首张卡由主理人挂出；消息与轮**同时**标成 `waiting_approval`；卡片工具与风险可核对 | `消息 4（小白管家）挂起，工具=apply_patch 风险=high` |
| 64 | 审批卡走答题口、非等待消息求批准，两个 409 码必须分得开 | `两码各归各位（no_pending_question / team_message_not_waiting）` |
| 65 | 主理人批准后文件**真写出**（含密钥标记、进变更账、轨迹有 `approval.required`/`approval.granted`），队列继续到成员并挂**新的一张卡** | `主理人批准后文件写出；队列继续到成员「小查」并挂卡，工具=apply_patch` |
| 66 | 成员驳回后消息 `paused`、轮 `completed`、工作区文件逐字节未变、轨迹有 `approval.denied`、变更账为 0 | `成员消息 paused、round completed、文件未变（106 字节）、驳回留痕` |

第 66 步盯的是最容易漏的一处：驳回在单任务里是 `paused`，群聊里若只看消息状态，
很容易漏掉 `round.status` 还卡在 running——界面就会永远转圈。这里把「消息停住」与「整轮收尾」
钉在同一条断言里。第 65 步先删掉上一格同名文件，再断言群聊批准真的写出了，避免拿旧文件冒充。

两格读数（`E:/tmp/m80/drive-both-r1.log`，同一份）：各 **80/80**、rc=0、判定行
「每一格全绿：对照判据成立」；匿名全量 **82 条**都 401；桩自检**连接 35 = 服务 35 / 截断 0**；
`agent_checkpoint_writes` 两格同为 **350 行 / 161,795 字节**。

**产品代码零改动**

这一批只读不写 `services/team.py` 与 `routes/team.py`：编排器本来就把轮与消息在挂卡时写成
`waiting_approval`、驳回时把消息写成 `paused` 并让轮收尾。装置补的是**跨方言判定**，
不是修缺陷——这一点与 #52 量出四处实物缺陷不同，不夸大。

**变异电池（4 靶）**

`E:/tmp/m80/mutate55.py`。先 `--probe` 实测红集合，再逐字钉进期望；正式跑
（`E:/tmp/m80/battery55.log`）**4/4 RED-OK**，额外与漏均为空，三份产品/装置文件 sha256
**逐字节还原**：

| 靶 | 注入 | 实测红集合 |
| --- | --- | --- |
| P1 | 挂卡收尾时轮状态不写 `waiting_approval`（写成 `running`） | `{63,64,65,66,69}` |
| P2 | 驳回后消息状态说成 `succeeded` | `{66}` |
| P3 | 非等待消息的错误码改名 | `{64}` |
| D1 | 装置自己把驳回后的期望改错（对照靶） | `{66}` |

P1 多红了第 69 步（群删除）：轮没有如实标成等待后，后面的驳回路径收不了尾，
删除群时轮还挂着。这是**实测集合**，不是按「应该红四步」推出来的；D1 专守装置自己——
把期望改错而装置仍全绿，就说明这一步根本没在判。

**离线用例**

`tests/test_dialect_device_v32.py` **17 passed**：装置自检跟着新编号一起更新，
`must_have` 增补 63 / 66、并把删除尾段改为 67 / 73–78；两个断言仍分别守住
「步骤数 = `EXPECTED_STEPS`」与「步骤不重名」。

**回归**

全量后端 **951 passed / 14 skipped**（rc=0，245.84s，`E:/tmp/m80/regression55.log`）。

**还没覆盖的（量出来的，不是猜的）**

1. **群聊审批卡的 TTL 过期路径没进装置**：单任务那一路已有读写成对的过期判定，
   群聊消息路由的「过期后再批」只有离线用例，没有跨方言实物。
2. **同一轮多个成员同时挂卡没覆盖**：本批用 `max_speakers=2` 走的是顺序队列，
   两个成员并发挂起、审批顺序与队列推进没有测。
3. **驳回后若队列里还有后续发言者**，轮是继续推进还是就地收尾，本批因队列用尽没有覆盖。
4. **群聊页面的审批卡没有浏览器点验**：本批前端零改动，装置走 HTTP；
   按钮位置、TTL 倒计时与驳回后的转圈状态都没有真浏览器读数。
5. #54 的未决项原样保留：中风险内联代码不挂卡、`scope=tool` 宽放行、批准记忆不持久化、
   PowerShell/cmd 引号语义未验、#29–#55 仍未 commit（HEAD 仍是 `974f684`，未 push）。

复现：

```bash
# 1) 装置自检（17 passed）
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_dialect_device_v32.py
# 2) 两格装置（80 步；步数由 EXPECTED_STEPS 自查）
cd ../..
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py --require-mysql
# 3) 变异电池（跑电池期间不并行跑任何东西）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m80/mutate55.py   # 4 靶，汇总「未按预期 0」
#    先实测红集合再钉期望：加 --probe（只注入只记录，不判定）
```

## 7.21 任务 #56：429 不是「这一轮废了」——两次退避，出过字就不再问

#53 把 429 归进「供应商说现在不行」，因此**不许换整包形状重放**——这条判断是对的：
429 拒的是时机，不是 `stream` 这个参数。但当时的实现把「不换形状」做成了「一句都不再问」，
供应商限流抖一下，用户这一轮就白扔。主流客户端在 429 上的做法是退避后再问；
本节把这半条补上，同时守住 #53 已经量过的边界。

**判定**

- 只对 HTTP **429** 重试，最多 **2 次**，等待 **0.5s → 1s**；5xx、超时、400 的口径一律不动。
- **已经出过字就不重试**：流式 `relay.chunks > 0` 时直接失败，否则用户会看到两遍开头。
- 两次退避后仍是 429，才走原来的 `model.stream_fallback_skipped`——「有意不退」只记一次。
- 每次退避落一条 `model.rate_limit_retry`：`attempt` / `status` / `wait_ms` / `max_retries` / `role`。
- 流式与整包两条路共用同一个闸门与同一个事件；整包路（`stream_output=false` 或退路）同样退避。

实现收在 `services/agent/graph.py`：`_call_adapter` 的流式循环、`_call_non_stream` 的整包循环，
判定抽成纯函数 `_rate_limit_may_retry` / `_rate_limit_delay`，离线用例直接钉住秒数与预算。

**装置从 80 步长成 81 步**

第 81 步用新探针 `PROBE-RATELIMITRETRY-42`：桩对**第一次**命中回 429，之后正常应答。
这一步不能只断言"重试事件存在"——终局必须是 `succeeded`、工具真执行、退路与跳过都为 0，
否则"重试了但仍然失败"或"拿整包重放"都会伪装成绿：

| 判据 | 实测原话 |
| --- | --- |
| 桩只拒一次，终局成功；恰好一条重试事件，`attempt=1/status=429/wait_ms=500/max_retries=2`；退路 0 次、有意不退 0 次；工具成功 ≥1 | `status=succeeded｜重试 1 次（waited=500ms）｜工具成功 1 个｜退路 0 次/跳过 0 次` |

桩的 `probe_hits` / `probe_rejects` 分开记：任务后续的每次模型调用都还带着探针串（goal 在历史里），
把"命中"当"拒绝"读会把一条正常收尾的链读成"拒了 4 次"——第一次跑就是这么红的，改的是装置自己。

两格读数（`E:/tmp/m81/drive-both.log`，同一份）：各 **81/81**、rc=0；匿名全量 **82 条**都 401；
桩自检**连接 41 = 服务 41 / 截断 0**；`agent_checkpoint_writes` 两格同为 **384 行 / 176,888 字节**。

**变异电池（4 靶）**

`E:/tmp/m81/mutate56.py`。先 `--probe` 实测红集合，再逐字钉进期望；正式跑
（`E:/tmp/m81/battery56.log`）**4/4 RED-OK**，额外与漏均为空，`graph.py` 与 `dialect_e2e.py`
两份文件 sha256 **逐字节还原**：

| 靶 | 注入 | 实测红集合 |
| --- | --- | --- |
| P1 | 429 一律不重试（闸门恒 `False`） | `{81}` |
| P2 | 重试事件把等待记成 0ms | `{81}` |
| P3 | 任何状态都可以重试（超时也等两轮） | `{78}` |
| D1 | 装置自己把期望等待改错（对照靶） | `{81}` |

P1/P2/D1 都只红第 81 步，但红在不同的断言上：P1 是任务直接失败（重试记录 0 条），
P2 是 `wait_ms` 对不上，D1 是装置自己的期望被改错——同一个集合不代表同一条判据在响。
P3 红在第 78 步，原话是 `超时 6s 却等了 19.8s`：允许任何状态重试后，一次超时被放大成三倍，
证明那道「耗时上界」确实在数等待，不是摆设。

**离线用例**

`tests/test_rate_limit_retry_v38.py` **6 条**（一次 429 退避后成功 / 持续 429 只退两次且只留一次
「有意不退」/ 出字后不重试 / 整包路同样退避 / 503 不退 / 秒数与预算的纯函数），
与 `test_stream_output.py`、`test_stream_fallback_gate_v36.py` 同跑 **46 passed**。

**回归**

后端全量 **957 passed / 14 skipped**（rc=0，243.02s，`E:/tmp/m81/regression56.log`；#55 的 951 + 本批 6 条）。

**还没覆盖的（量出来的，不是猜的）**

1. **`Retry-After` 响应头没有读**：等待时间是固定的 0.5s/1s，供应商若给 `Retry-After: 30`，
   当前实现会按自己的节奏在 1.5s 后放弃。主流客户端会取两者较大值；本批没做。
2. **压缩路径不重试**：`summarize_history` 直接调用 `adapter.invoke`（避免递归压缩），
   这条路的 429 仍是即刻失败。它只在上下文将满时发生，但确实不在本次闸门内。
3. **5xx 不退避**：503/502 仍按 #53 的"现在别问"处理（不换形状、不重试）。Codex 一类客户端
   通常对 5xx 也退避；本批刻意不扩，避免把"供应商过载"的等待时间也翻倍。
4. **真实供应商标度没有复测**：本批的证据是桩控制的 429 + 离线适配器用例，没有真 DeepSeek 限流样本。
5. #55 的未决项原样保留；#29–#56 仍未 commit（HEAD 仍是 `974f684`，未 push）。

复现：

```bash
# 1) 离线用例（46 passed）
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q \
  tests/test_rate_limit_retry_v38.py tests/test_stream_output.py tests/test_stream_fallback_gate_v36.py
# 2) 两格装置（81 步；步数由 EXPECTED_STEPS 自查）
cd ../..
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py --require-mysql
# 3) 变异电池（跑电池期间不并行跑任何东西）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m81/mutate56.py   # 4 靶，先加 --probe 实测红集合
```

## 7.22 任务 #57：429 说等一秒就等一秒——Retry-After 与压缩那条路

#56 补上了「退避后再问」，等待却是客户端自己猜的 0.5s→1s。供应商在 429 上常给
`Retry-After`：明说 30 秒，客户端 1.5 秒后就放弃，等于没听。另一边，`summarize_history`
直接调 `adapter.invoke`（避免递归压缩），这条路一次 429 就把整轮判死。本节各补一半，
口径与正文调用保持一致。

**判定**

- `Retry-After` 支持 RFC 9110 的两种形状：整秒（`120`）与 HTTP 日期
  （`Fri, 23 Oct 2026 08:00:04 GMT`）；读不懂、空值、过去的时间都按 0 算，
  退回指数退避，绝不产生负等待。
- 等待取「指数退避 0.5s/1s」与 `Retry-After` 的**较大者**，再封顶 **30s**：
  服务器说了多久就不该比它更早；界面也不能为此无限期挂着。
- 重试事件新增 `retry_after_ms`（供应商原值）与 `wait_ms`（实际等待，含封顶）。
  两个数分开留，事后才分得清「它只要 3 秒」和「它要 120 秒、被我们封到 30 秒」。
- 表头从响应骑到 `ModelError.retry_after`：`openai_compat` / `anthropic` / `ollama` /
  `openai_responses` 的 HTTP 抛错点全部接上；离线用例把普通流 / 带工具流 / 整包三条路逐条钉过。
- 压缩走 `_invoke_with_retry`：同一个「只对 429、最多 2 次」闸门、同一条
  `model.rate_limit_retry` 事件（`role=summarizer`、`node=context_compact`），
  成功后 `compaction_response_id` 指认重试后成功的那一笔，不指认失败的那笔。

**装置从 81 步长成 82 步**

第 82 步用新探针 `PROBE-RATELIMITAFTER-42`：桩对第一次命中回 429，且带
`Retry-After: 1`；之后正常应答。判据不能只看终局成功——盲猜的 0.5s 也能成功——
必须看事件里的等待是否真的变成 1000ms：

| 判据 | 实测原话（两格同一句） |
| --- | --- |
| 恰好一条重试事件，`attempt=1/status=429/wait_ms=1000/retry_after_ms=1000/max_retries=2`；终局 `succeeded`；退路 0 次、有意不退 0 次；工具成功 ≥1 | `status=succeeded｜重试 1 次（waited=1000ms，retry_after=1000ms）｜工具成功 1 个｜退路 0 次/跳过 0 次` |

两格读数（`E:/tmp/m82/drive-both.log`，同一份）：各 **82/82**、rc=0；匿名全量 **82 条**都 401；
桩自检**连接 45 = 服务 45 / 截断 0**；`agent_checkpoint_writes` 两格同为
**418 行 / 191,981 字节**。

**变异电池（4 靶 + 压缩 1 靶）**

`E:/tmp/m82/mutate57.py` 先 `--probe` 实测红集合再钉期望；正式跑
（`E:/tmp/m82/battery57.log`）**4/4 RED-OK**，额外与漏均为空，`graph.py`、
`openai_compat.py`、`dialect_e2e.py` 三份文件 sha256 **逐字节还原**：

| 靶 | 注入 | 实测红集合 |
| --- | --- | --- |
| P1 | 等待只按指数退避，不听 `Retry-After` | `{82}` |
| P2 | 事件把 `retry_after_ms` 记成退避值 | `{82}` |
| P3 | 带工具流那条路把表头丢成 0 | `{82}` |
| D1 | 装置自己把期望等待改错（对照靶） | `{82}` |

压缩那一路装置打不到（压缩由窗口水位触发），单独做了离线反证
（`E:/tmp/m82/mutate_compact.py`）：撤掉 `_invoke_with_retry` 的循环后，
`test_compaction_429_goes_through_the_same_backoff` **rc=1**，失败原因命中
「压缩那一笔 429 直接判死了，没走退避」，`graph.py` sha256 还原一致。
这条用例最初只断言调用次数，撤掉重试时红在裸 `ModelError` 上、读不出是哪条判据；
已改成捕获后 `pytest.fail(固定原因)`，红必须指名。

**离线用例**

`tests/test_retry_after_v39.py` **13 条**：表头七种输入、HTTP 日期对固定时钟、
过去的时间按 0、取较大者不缩短退避、30s 封顶、流式等待 3s 与事件原值、
120s 封顶但原值仍留在事件里、压缩 429 退避后成功且 `compaction_response_id` 指向第二笔。
与 `tests/test_rate_limit_retry_v38.py`、`test_stream_output.py`、
`test_stream_fallback_gate_v36.py` 同跑 **63 passed**；v36 新增的三条路表头断言
让「适配器读了表头」不再只靠 `base.py` 一处实现撑着。

**回归**

后端全量 **974 passed / 14 skipped**（rc=0，249.02s，`E:/tmp/m82/regression57.log`；#56 的 957 + 本批 17 条：
v39 13 条 + v36 新增 4 条）。

**还没覆盖的（量出来的，不是猜的）**

1. **5xx 仍不退避**：503/502 仍按 #53 的"现在别问"处理（不换形状、不重试）。
2. **真实供应商 429 样本没有复测**：本批仍是桩控制的 429 + 离线适配器用例。
3. **压缩只有离线反证**：装置没有压缩步骤（要伪造超窗长历史），这条路径的跨方言行为没有实物。
4. **只读 HTTP 响应头**：SSE 事件体里若带 `retry_after` 字段（部分网关如此），本批不读。
5. **另外三家适配器只有接线、没有逐家用例**：`anthropic` / `ollama` / `openai_responses`
   的 HTTP 抛错点都接了表头，但真响应用例只在 `openai_compat` 上跑了三条车道。
6. #29–#57 仍未 commit（HEAD 仍是 `974f684`，未 push）。

复现：

```bash
# 1) 离线用例（63 passed）
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q \
  tests/test_retry_after_v39.py tests/test_rate_limit_retry_v38.py \
  tests/test_stream_output.py tests/test_stream_fallback_gate_v36.py
# 2) 两格装置（82 步；步数由 EXPECTED_STEPS 自查）
cd ../..
BLANK_E2E_MYSQL_URL="mysql+aiomysql://<user>:<pw>@127.0.0.1:3307/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py --require-mysql
# 3) 变异电池（跑电池期间不并行跑任何东西）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m82/mutate57.py          # 4 靶，先加 --probe
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m82/mutate_compact.py    # 压缩 1 靶
```

## 7.23 任务 #58：压缩那一笔在两种方言上真发生——把 #57 未覆盖 3 换成实物

#57 的「还没覆盖」第 3 条写着：压缩只有离线反证，装置里没有一步会让压缩发生。
#42/#44 的 `window_chain_probe.py` 虽然是真 DeepSeek，但只跑单机 SQLite；#57 的
压缩退避也只有 `test_retry_after_v39.py` 一条离线用例。本节把这两件事一起搬到
**跨方言的 HTTP 栈**上：让装置自己把小窗口顶爆、逼出一次真折叠，并让摘要那一笔
第一次撞 429+`Retry-After: 1`。

**怎么让它真发生（算出来的，不是碰上的）**

- 新探针 `PROBE-COMPACT-42` 自带一个 `context_window=1200` 的供应商，水位
  `min(1200*0.82, 1200-16384)` 命中"预留大于窗口"分支 ⇒ **984 tokens**。
- 桩对主线连抛两次 `read_file`（`compact-a.txt` / `compact-b.txt`，各约 6KB、
  内容不同），第三轮收尾。只读一轮时 `_safe_cut` 会把切点退回段头，一条都不折；
  两份文件若内容相同，microcompact 会按指纹把第二份折叠掉，也压不下去。
- 摘要请求与主线共用 `/v1/chat/completions`，桩靠系统提示里的
  「你在压缩一段 Agent 的执行历史」认出它；第一次回 429+`Retry-After: 1`，
  第二次回五要素 JSON（`goal/constraints/done/failures/open`）。

**实测读数（两格同一句，`E:/tmp/m83/both.log`）**

| 判据 | 实测原话 |
| --- | --- |
| 终局成功；窗口链从 1 起逐窗递增；每次折叠的 `boundary` 严格前进；`replacement.dropped` 与 `items` 非空；`compaction_response_id` 非空；摘要那笔恰好退避一次且 `wait_ms=retry_after_ms=1000`；成功工具恰好 2 个 | `status=succeeded｜压缩 2 窗，末窗折叠 3 条｜替换 dropped=5 items=4｜摘要 resp-stub-summary-40813｜摘要退避 waited=1000ms｜成功工具 2 个` |

- SQLite 格：**83/83**、失败 0；`agent_checkpoint_writes` **461 行 / 286,410 字节**；
  桩自检**连接 52 = 服务 52 / 截断 0**；服务日志里 SQL 相关行 **0**。
- MySQL 8.4 格（本机 3307，`root` 无口令，`kbgai_e2e` 由装置自建）：同样 **83/83**、
  失败 0，唯一的差别是桩生成的摘要响应 id（`resp-stub-summary-74331`）；检查点与
  连接数逐项相同。两次折叠都发生在 `execute_action` 节点，边界 **3 → 6**。

独立于装置打印的库内复核（MySQL 直接读 `agent_events`，任务 `026b71f4-…`）：

```text
model.rate_limit_retry  context_compact  role=summarizer  wait_ms=1000
context.compacted       execute_action   folded=3 window=1 boundary=3  resp-stub-summary-74168
context.compacted       execute_action   folded=3 window=2 boundary=6  resp-stub-summary-74331
```

装置打印的是**末窗**的 id；第一窗那一笔（`74168`）只存在于库里，正好证明它不是把同一条
记录读了两遍。`window` 从 1 递增、`boundary` 3→6，与两格打印逐项相等。

**装置自己先错了一次，判据也太松**

第一次跑出来的是「压缩 200 窗、成功工具 200 个」：桩按"历史里看得见几条工具结果"
计数，压缩把第一条折进摘要后计数退回 1，于是它又抛了一次读取，任务空转到步数上限；
而当时的判据只写了 `folded >= 1`、`窗口链递增`，**这种空转照样绿**。修了两处：

1. 桩改按**主线调用次数**（`compact_calls`）计数，第三次必须收尾；
2. 判据把「成功工具 ≥ 2」收紧成 **恰好 2 个**，并加「每次折叠的 `boundary` 严格大于
   上一次」——原地重折同一条历史会在这里红。次数本身不钉死：一次任务可能在多个
   节点各折一次（这次就是 2 窗），钉次数等于把图的结构写进判据。

**变异电池（3 靶，红集合实测）**

| 靶 | 注入 | rc | 实测失败原因 |
| --- | --- | --- | --- |
| D1（只打装置） | 摘要请求永不 429（`if False and ...`） | 1 | 「摘要那笔应当恰好退避一次，实际 0 条」 |
| P1（产品） | `summarize_history` 绕开 `_invoke_with_retry` | 1 | 同上一句（**不是**事先猜的"没有压缩记录"：后面另一个节点的摘要请求命中了桩的第 2 次应答，照样折了一窗） |
| P2（产品） | `context.compacted` 的 `compaction_response_id` 写成空串 | 1 | 「压缩记录没指认那一笔摘要响应：''」 |

三靶都 `restored=True`：`evals/dialect_e2e.py` 与 `graph.py` 的 sha256 **逐字节还原**
（脚本用 `read_bytes`/`write_bytes`，不经过换行归一化，否则 CRLF 文件"还原"后会整份变样）。
P1 的期望写宽了一次——实测红原因与推理不同；按实测改记录，不按推理改判据。

**离线用例**

`tests/test_dialect_device_v32.py` **17 passed**（3.37s），步数自检从声明 82 改为 83，
`must_have` 收进第 83 步的完整步骤名；步数不符时 `Drive.finish` 会把"全绿"改判成红。

全量后端回归：**974 passed / 14 skipped**（rc=0，322.47s，`E:/tmp/m83/regression58.log`；
与 §7.22 的 974 持平——本批只改装置断言与步数，没有新增产品代码和用例）。

**还没覆盖的（量出来的，不是猜的）**

1. 装置里的摘要是**桩写的**五要素 JSON，不是真模型；真模型压缩证据仍是 #42/#44 的
   单机 `window_chain_probe.py`。本批补的是"跨方言 + 真 HTTP 栈"，不是"真模型跨方言"。
2. `context_window=1200` 是为了在装置规模内逼出折叠的人工配置；真实 8k/128k 窗口下
   的触发点没有被这一步覆盖。
3. 两次折叠都在 `execute_action` 节点，plan/validation 节点的压缩没有被单独点名。
4. MySQL 格跑的是本机 3307 的 `root` 无口令实例；CI 的 `api-dialect` job 仍未在
   GitHub runner 上真跑过。
5. #29–#58 仍未 commit（HEAD 仍是 `974f684`，未 push）。

复现：

```bash
# 两格装置（83 步；无库时有 [SKIP]/[ABORT] 语义）
cd E:/projects/kbgai/kbgai
BLANK_E2E_MYSQL_URL="mysql+aiomysql://root@127.0.0.1:3307/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py --only sqlite,mysql --require-mysql
# 变异电池（跑电池期间不并行跑任何东西）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m83/mutate58.py
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m83/mutate58b.py
```

## 7.24 任务 #59：真 DeepSeek 的摘要，在 SQLite 与 MySQL 上是同一组 id

#58 的「还没覆盖」第 1 条写着：装置里的摘要是桩写的 JSON，真模型压缩只有 #42/#44 的单机
SQLite。本节把真模型拉进跨方言：同一个 provider（`deepseek-chat`），一次跑 :8061（SQLite）、
一次跑 :8063（MySQL 8.4），窗口临时调到 2000，两份读数逐项对照。

**先修掉的两个取证环境问题**

- :8061 的进程跑的是旧代码：第一次真跑事件里没有 `window_number`/`replacement`/
  `compaction_response_id`，六条旧判据照样全绿——**判据没盯的字段，旧进程就能一直混过去**。
  按既定规则重启实例后才看到新字段。
- 重启后又撞 `no such column: model_usage.response_id`（`POST /run` 500）：隔离库停在
  alembic 0017，代码已是 0023。补 `alembic upgrade head` 后恢复。这两件事都说明
  「实例在跑」不等于「代码和库是这一版」。

**判据加硬（11 条）**

除原有六条（有压缩事件、`covered`、触发原因、水位、证据聚合、工具配对不散）外，新增五条，
都盯着真模型才会有的东西：

1. `summary_chars > 0`（每次折叠的摘要非空——空摘要这条以前没人钉）；
2. 每窗 `compaction_response_id` 非空（对标 §4 的 `compaction_response_id`，DeepSeek 每次都回 id）；
3. `window_number` 从 1 起、`previous_window_id` 逐窗接上一窗的 `window_id`、`boundary` 严格前进；
4. `replacement.dropped` 与 `replacement.items` 都非空（替换历史真的记了丢掉什么/换进什么）；
5. 证据包 `context.windows[*].compaction_response_id` 与 `context.compacted` 事件是**同一组非空 id**。

**两格实测**

日志：`E:/tmp/m59/final-sqlite.log` / `final-mysql.log`。

| 判据 | SQLite（:8061，任务 `df01ebe3`） | MySQL（:8063，任务 `9f9311c3`） |
| --- | --- | --- |
| 结果 | **11/11 PASS，rc=0** | **11/11 PASS，rc=0** |
| 事件数 / 压缩次数 | 39 / 2 | 39 / 2 |
| 窗口号 / 边界 | `[1, 2]` / `[3, 7]` | `[1, 2]` / `[3, 7]` |
| `summary_chars` | `[435, 725]` | `[450, 758]` |
| 摘要响应 id | `56a446bb…` / `8856ceb5…` | `d10c37e6…` / `e3d5f6ff…` |
| 替换 retained/dropped/items | 7/2/8、5/6/6 | 7/2/8、5/6/6 |
| 窗口归属 | 每窗 1 条 turn，`turns_unattributed=0` | 同左 |

- 两格 provider 的 `context_window` 都由 65536 临时改为 2000，finally 恢复 65536
  （日志里 `restore_window=200 -> 65536`）。
- 真模型摘要长度每次不同（这两格是 435/725 与 450/758），判据只钉「非空」不钉字数——
  钉字数等于把模型采样写死。

**不经过装置的库内复核**（`E:/tmp/m59/crosscheck.py`）：

```text
== sqlite == windows=[1,2] boundaries=[3,7]
event_ids=['56a446bb…','8856ceb5…']  usage_ids=[同左]  tokens=[[618,228],[976,345]]
same_order=True chain_linked=True
== mysql == windows=[1,2] boundaries=[3,7]
event_ids=['d10c37e6…','e3d5f6ff…']  usage_ids=[同左]  tokens=[[621,230],[1200,375]]
same_order=True chain_linked=True
```

`model_usage.node='context_compact'` 的两笔摘要调用，`response_id` 与 `agent_events` 里
`context.compacted` 的指针逐项相同、顺序一致；`window_id`/`previous_window_id` 首尾相接。
这是「真模型真被叫了两次、事件里指的是同两次」的独立证据。

**变异电池（2 靶，红集合实测）**

| 靶 | 注入 | rc | 实测红集合 |
| --- | --- | --- | --- |
| M1（产品） | `compaction_response_id` 恒为空串 | 1 | 仅「每次压缩都指认了供应商响应 id」红；「证据包与事件同一组 id」当时**没红**——空==空也成立。据此给该判据补 `all(evidence_ids)` |
| M2（产品） | 摘要长度置 0 + 窗口号恒为 1 + `replacement={}` + 响应 id 空 | 1 | 5 条新判据**全红**，旧 6 条全绿 |
| 还原 | `graph.py` sha256 | — | 注入前 `02e9f685…`，还原后同值，`git diff` 为空 |

M1 暴露的是判据自身的空值假绿，不是产品缺陷；M2 才是「证据字段全丢」这一形态。
两靶都按实测记录，没有按推理写红集合。

**离线用例**

`test_compaction_window_v23.py` + `test_response_id_v21.py` + `test_turn_attribution_v24.py`
+ `test_dialect_device_v32.py`：**54 passed / 11.15s**。

全量后端回归：**974 passed / 14 skipped**（rc=0，249.82s，`E:/tmp/m59/regression59.log`；
与 #58 持平——本批只改探针，未动产品代码与用例）。第一次从仓库根跑错了收集范围，
把 `services/api/data/snapshots/` 的运行产物当测试收进来（759 个 collection error）；
正确口径与 §7.23 相同：在 `services/api` 目录下跑。

**还没覆盖的（量出来的，不是猜的）**

1. **真 429 / `Retry-After` 仍未在真供应商上发生**：DeepSeek 不给可控的 429，这一形态
   只有 #58 的桩证据。真模型这一批证明的是「摘要真发生、id 真骑回来」，不是「真限流也听得懂」。
2. 2000 窗口是人工配置；真实 65536 窗口下同一任务一次都不会折——触发点是造出来的。
3. 两次折叠都在 `execute_action` 节点；plan/validation 节点的真模型压缩没有被单独点名。
4. MySQL 格仍是本机 3307 `root` 无口令的 `kbgai_m59` 库；CI runner 上的真库 job 仍未跑过。
5. 探针需要活实例 + 真 Key，不能进 CI（会花钱、要密钥）；它是取证工具，不是回归用例。
6. 真模型摘要内容没有做语义正确性判定——只证明非空与可追溯；「摘要是否忠实」仍要靠
   人工/模型评审，不在本判定里。

复现：

```bash
# SQLite 格（:8061 必须是当前代码，库先 alembic upgrade head）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 -u evals/compaction_probe.py

# MySQL 格（:8063 = MySQL 8.4 + 真供应商；启动脚本与库在 E:/tmp/m59/mysql-cell/）
BLANK_DATABASE_URL="mysql+aiomysql://root@127.0.0.1:3307/kbgai_m59" \
BLANK_SECRET_KEY_FILE="E:/tmp/realmodel/secret.key" \
BLANK_SETTINGS_FILE="E:/tmp/m59/mysql-cell/settings.json" \
BLANK_MEMORY_DIR="E:/tmp/m59/mysql-cell/memory" \
BLANK_NOTES_DIR="E:/tmp/m59/mysql-cell/notes" \
BLANK_WORKTREE_ROOT="E:/tmp/m59/mysql-cell/worktrees" \
BLANK_AUDIT_DIR="E:/tmp/m59/mysql-cell/audit" \
BLANK_SANDBOX_ENABLED=0 PYTHONPATH=src \
  services/api/.venv/Scripts/python.exe -m uvicorn blank_api.app:app --host 127.0.0.1 --port 8063

PROBE_BASE=http://127.0.0.1:8063 PROBE_PROVIDER_ID=6370024d-9590-4c08-bea3-ac1a8e8f41c6 \
PROBE_OUT_DIR="E:/tmp/m59/mysql-cell" PYTHONIOENCODING=utf-8 \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/compaction_probe.py

# 库内复核与变异靶
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m59/crosscheck.py
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m59/mutate59b.py inject   # 跑完 restore
```

## 7.25 任务 #60：第三格真的来了——PostgreSQL 17.6 上的迁移、应用与 83 步同一套判据

#60 的题目写在 §7.22、§7.24 的「还没覆盖」里：PostgreSQL 一直只是迁移代码里的方言分支
（0021/0022/0023 都写着 `if dialect.name == "sqlite" ... else`），从没起过一台真库。
这一批把它跑成第三格：**真 PostgreSQL 17.6 + 同一条 alembic 历史 + 同一份 83 步 HTTP 驱动**。

**装置与实例**

- EnterpriseDB 便携包 `postgresql-17.6-1-windows-x64-binaries.zip` 解到 `E:/tmp/pg60/pgsql`；
  `initdb -U postgres --auth=trust --locale=C`，只监听 `127.0.0.1:55432`。
  `kbgai_pg60` 用于迁移往返，`kbgai_e2e` 是装置格。
- 这是**隔离的本机实例**（trust、无口令、无 TLS），不是部署形态；只用来量"应用在 PG 上跑不跑"。

**接线（只动装置与 CI，产品代码零改动）**

- `_split_postgres_url`：SQLAlchemy URL 解析、库名只认 `[A-Za-z0-9_]{1,60}`（要拼进 DDL 的标识符）、口令不回显；
- `postgres_admin`：asyncpg 连 maintenance 库，`drop database ... with (force)` + `create database`；
- `prepare(engine, mysql_url, postgres_url)` 在 `postgres` 格建库后跑同一条 `alembic upgrade head`；
- `checkpoint_witness` 多一条 asyncpg 分支，直查 `agent_checkpoint_writes`；
- `verdict()` 从写死 MySQL 改成按格点名（`MySQL` / `PostgreSQL`），对照格红仍让整次判定作废；
  **三格同跑时两个被测格一起红，不再各念一句「只有 X 红」**，改念「非对照格与对照不同的步骤」并列出共有红步；
- CLI 新增 `postgres` 格与 `--require-postgres`；默认仍是 `sqlite,mysql`，旧调用行为不变；
- 离线闸门 19 条全绿（新增 2 条：PG URL 解析/拒绝、PG 归属点名）；
- CI `api-dialect` 增加 `postgres:17` service，一次跑
  `--only sqlite,mysql,postgres --require-mysql --require-postgres`（**未在 GitHub runner 上跑过**，见未覆盖 2）。

**实测**

1. 迁移：`alembic upgrade head` 0001→0023 **rc=0**（`E:/tmp/pg60/alembic.log`）；
   `downgrade base` **rc=0** 后再 `upgrade head` **rc=0**，末态 `0023_timestamps_not_null`、public **21 张表**
   （`downgrade.log` / `reupgrade.log`）。整条历史没有为 PG 改一行。
2. 装置：`--only sqlite,postgres` 两格各 **83/83 PASS、0 FAIL**，合计 **166 条 PASS**，
   rc=0「每一格全绿：对照判据成立」；每格服务日志里 SQL 相关行 **0**，桩连接/服务/截断 **52/52/0**。
3. 检查点：两格都 **461 行**，末次字节数同为 **286,410**（首次绿跑是 286,411 / 286,410）。
   字节和会随两次运行各自的 id / 时间等运行值变动，所以只把行数当跨格一致判据，不主张逐字节相等。
4. 绿跑后直查 PG：`alembic_version=0023`，`tasks=7`（5 succeeded / 2 failed——后两条正是第 77/78 步
   故意造的 429 与超时）、`agent_events=187`、`model_usage=23`、`agent_checkpoint_writes=461`。
5. 负对照（装置能不能红）：只在 `db.py::engine_options` 给 PG 加 `search_path=mutation_absent`
   （alembic 自建引擎，不受影响）⇒ uvicorn 启动时 `relation "agent_runs" does not exist`，装置 **rc=1**；
   `db.py` 逐字节还原，`git hash-object` = `7adbaef8…` = HEAD。
   这一靶证明 PG 格不是"没跑也报绿"；它红在启动而非步骤归属，步骤级归属由离线 `verdict()` 用例钉住。
6. 回归：离线 `test_dialect_device_v32` **19 passed**；后端全量 **976 passed / 14 skipped**
   （rc=0，244.43s，`E:/tmp/pg60/regression60.log`；#59 的 974 + 本批 2 条）。
   三格归属分支修正（两个被测格同时红）后，装置用例 **19 passed**、全量复跑仍 **976 passed / 14 skipped**
   （rc=0，241.03s，`E:/tmp/pg60/regression60b.log`）。

**还没覆盖的（量出来的，不是猜的）**

1. PG 是本机 trust 实例，没验口令 / TLS / 远程连接；也不是"别人运维的 PG"。
2. CI 的 PG service 只写进了 yml，**GitHub runner 上一次没跑过**（与 MySQL 那一格同一条账）。
3. 只在 Windows + PG 17.6 上跑过；PG 16 / Linux 形态没有实物。
4. 负对照在启动阶段就红了，没有产出「只有 PostgreSQL 红」的步骤级红集合；那条归属只由离线用例守。
5. 检查点只对账行数与字节和，未做逐行 / 逐字节的跨方言相等判定；两次绿跑的字节和本身就能差 1。
6. `test_schema_agreement_v29.py` 仍是 SQLite↔MySQL 两方言，PG 的逐列对账没有加。
7. #29–#60 中 #29–#58 已在 `c6581e3`；**#59 / #60 仍未 commit**（HEAD=`c6581e3`，未 push）。

复现：

```bash
# 1) 起隔离实例（便携包在 E:/tmp/pg60）
E:/tmp/pg60/pgsql/bin/initdb.exe -D E:/tmp/pg60/data -U postgres --auth=trust --encoding=UTF8 --locale=C
E:/tmp/pg60/pgsql/bin/pg_ctl.exe -D E:/tmp/pg60/data -l E:/tmp/pg60/pg.log \
  -o "-p 55432 -c listen_addresses=127.0.0.1" start

# 2) 整条迁移历史与往返（kbgai_pg60 先建库）
cd services/api
BLANK_DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/kbgai_pg60" \
  .venv/Scripts/python.exe -X utf8 -m alembic upgrade head
BLANK_DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/kbgai_pg60" \
  .venv/Scripts/python.exe -X utf8 -m alembic downgrade base
BLANK_DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/kbgai_pg60" \
  .venv/Scripts/python.exe -X utf8 -m alembic upgrade head

# 3) 离线闸门（19 passed）
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_dialect_device_v32.py

# 4) SQLite 对照 + PostgreSQL 被测（83 步；从仓库根）
cd ../..
BLANK_E2E_POSTGRES_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py \
  --only sqlite,postgres --require-postgres

# 5) PG 专属负对照：注入坏 search_path，装置必须 rc=1，然后逐字节还原
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/pg60/mutate_pgcell.py inject
BLANK_E2E_POSTGRES_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/kbgai_e2e" \
  services/api/.venv/Scripts/python.exe -X utf8 -u evals/dialect_e2e.py --only sqlite,postgres --require-postgres
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/pg60/mutate_pgcell.py restore
```

## 7.26 任务 #61：两条建库路径在 PostgreSQL 上对账——77 列的默认值只有迁移侧有

#60 证明的是「迁移能建出库、应用能跑」；没有证明「另一条建库路径（`Base.metadata.create_all`）
在 PG 上给出同一个 schema」。这不是同一件事：#49 的 A1 只在 SQLite 上对过账，
而 SQLite 对列宽、可空性、默认值照收不误——只有真方言才会把这些分歧变成事故
（#47 的 `String(36)→String(120)`、#48 的 `ON DELETE CASCADE` 都是这么被真库抓出来的）。

**装置**（`services/api/tests/test_schema_agreement_pg_v33.py`，3 条判定）

- 在真 PG 上开两个**独立库**：一个跑 `alembic upgrade head`（0001→0023），
  一个跑 `create_all()`；库名带进程号，跑完 `drop database ... with (force)`。
- 对账五个维度：表集合、列集合、可空性、外键集合、**服务端默认值**。
  默认值只比两个库反射出来的结果，不比 ORM 里的 Python 字面量——PG 会把等价写法
  规范化成同一个 `column_default`（`DEFAULT ''` 与 `DEFAULT ('')` 都是 `''::character varying`，
  先用探针 `probe_defaults_a/b` 验过，不是推断）。
- 防空绿：`counted >= 17`、`tasks.attachments` 必须含 `[]`、`agent_runs.request_payload` 必须含 `{}`；
  另有一条值探针：不带 `description/created_at/updated_at` 插入 `projects`，必须真落空串与非空时间。
- 没给 `BLANK_TEST_POSTGRES_URL` 时三条都跳过（默认测试入口与 CI `api` job 没有 PG）；
  给了却连不上是**失败**（`AssertionError`），不是跳过。

**第一次真跑就红了：77 列只有迁移侧有服务端默认值**

- `test_both_provisioning_paths_agree_on_postgres` 与值探针都绿；默认值一条红，
  实测 **77 个列**（20 张表）`迁移=<默认值>` 而 `create_all=None`：
  19 张表里 `''` 的 varchar、`''`/`'{}'`/`'[]'` 的 Text/JSON、`0/1/5/8/2048/60000/128000` 的整数、
  `true/false` 的布尔、`0.2` 的浮点；ORM 侧这些列只有 Python `default=`（插入时由 SQLAlchemy 填），
  所以 `create_all` 的 DDL 里一个默认值都没有。
- 顺带量到一处口径分歧：`tasks.max_steps` 迁移侧服务端默认值是 **8**，
  而 Python 默认值（`DEFAULT_MAX_STEPS`）已是 **200**——应用插入走 200，裸 SQL 走 8。
  这不是本批引入的，模型里加了一行注释如实写明，没有擅自改任何一侧。

**处置：只补 ORM 的 `server_default`，迁移与 Python 默认值都不动**

- 8 个模型文件、77 个列补上与迁移侧一致的服务端默认值；Text/JSON 一律用
  `text("('')")` / `text("('{}')")` / `text("('[]')")` 表达式默认值（MySQL 8.4 对 LOB 裸字面量
  默认值会 1101，这正是 #49 的约束）；varchar/int/bool/float 用普通字符串或 `text("true")`。
- Python `default=` 全部保留：应用插入的取值路径一个字没变，变的是 `create_all` 建出来的 DDL。
- CI `api-dialect` 在三方言驱动之后加一步 `pytest -q tests/test_schema_agreement_pg_v33.py`，
  用同一个 `postgres:17` service（连 maintenance 库 `postgres`，自己建/删两个临时库）。

**验证**

1. 修前红基线：`1 failed, 2 passed`，红集合恰好 77 列（`E:/tmp/pg60/pg-agree-red1.log`）。
2. 修后：3 passed（PG 17.6，`127.0.0.1:55432`）；`test_schema_agreement_v29.py` 仍 **6 passed**。
3. 变异电池两条，红集合按实测：
   - M1 摘掉 `tasks.status` 的 `server_default` ⇒ 只有 `test_server_defaults_agree_on_postgres` 红，
     红行 `tasks.status: 迁移="'draft'::character varying" create_all=None`；
   - M2 把 `tasks.attachments` 改 `nullable=True` ⇒ 只有 `test_both_provisioning_paths_agree_on_postgres` 红，
     红行 `tasks.attachments: 迁移 nullable=False 但 ORM nullable=True`；
   - 两次都逐字节还原，`task.py` sha256 = `07b819d0…`，还原后复跑 3 passed（`E:/tmp/pg61/m1.log` / `m2.log`）。
4. 全量：**976 passed / 17 skipped**（rc=0，246.22s，`E:/tmp/pg61/regression61.log`）；
   新增 3 条在没有 `BLANK_TEST_POSTGRES_URL` 时按设计跳过，跳过数从 14 变成 17。

**还没覆盖的（量出来的，不是猜的）**

1. 只对账表/列/可空性/外键/默认值：**列类型与长度、索引、唯一约束、CHECK** 没有逐项比——
   `String(36)` 与 `String(120)` 这类漂移在这条判定下仍然看不见。
2. 变异只打在 ORM 侧；迁移侧只被"第一次自然红"覆盖过，没有单独的迁移变异靶。
3. CI 的这条新步骤与 `postgres:17` service 一样，**GitHub runner 上没跑过**。
4. 只在 Windows + PG 17.6 上跑；PG 16 / Linux 形态没有实物。
5. `test_schema_agreement_v29.py` 的 A5（0023 NULL 闸门）仍只在 SQLite 上有拒绝证据。
6. 补的 77 个 `server_default` 只经过 SQLite 全量回归与 MySQL DDL 编译检查（A2），
   **MySQL 上真跑 `create_all` 路径没有重跑**（产品迁移路径不经过它）。

复现：

```bash
# 需要一台真 PG；没给 URL 时三条按设计跳过
cd services/api
BLANK_TEST_POSTGRES_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/postgres" \
  .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_schema_agreement_pg_v33.py

# 变异（M1 默认值 / M2 可空性），跑完必须 restore
.venv/Scripts/python.exe -X utf8 E:/tmp/pg61/mutate_orm.py inject-default
.venv/Scripts/python.exe -X utf8 E:/tmp/pg61/mutate_orm.py restore
.venv/Scripts/python.exe -X utf8 E:/tmp/pg61/mutate_orm.py inject-nullable
.venv/Scripts/python.exe -X utf8 E:/tmp/pg61/mutate_orm.py restore
```

## 7.27 任务 #62：schema 不止列名——类型/长度、索引、唯一约束，以及一处真实的约束名分歧

#61 把两条建库路径的「列名 / 可空性 / 外键 / 服务端默认值」对上了，但那不是 schema 的全部：
`String(36)` 与 `String(120)` 在列名上完全一样，少一条索引、唯一约束换个名字，也同样看不出来。
这一批先在真 PG 上**量实物**（`E:/tmp/pg62/probe_shapes.py`，只打印不判定）：

- 20 张表 / **213 列**，类型分布 `VARCHAR(36)` 49、`TIMESTAMP` 31、`INTEGER` 26、`VARCHAR(120)` 15、
  `TEXT` 14、`JSON` 11、…；其中 **118 列带长度**。
- **44 个索引**、**6 个唯一约束**；CHECK 约束**两边都是 0 条**。
- 类型/长度、索引的「列集合 + unique 标志」全部一致——唯一分歧在**名字**上。

**第一次真跑就红：唯一约束的名字**

```
model_providers 索引分歧：迁移独有=[('uq_model_providers_name', ('name',), True)]
                                  ORM 独有=[('model_providers_name_key', ('name',), True)]
model_providers 唯一约束分歧：迁移独有=[('uq_model_providers_name', ('name',))]
                                        ORM 独有=[('model_providers_name_key', ('name',))]
```

机理：迁移 0003 写的是 `sa.UniqueConstraint("name", name="uq_model_providers_name")`；
ORM 只写 `unique=True`，PG 按 `<table>_<column>_key` 自动命名。两条路建出来的库语义相同，
但**按名字 DROP 的脚本会一个中一个空**，以后的迁移也认不出它们是同一个对象。
处置：ORM 改成显式 `UniqueConstraint("name", name="uq_model_providers_name")`（与迁移同源），
列上的 `unique=True` 去掉；Python 侧行为与迁移文件都没动。这个修法在 SQLite/MySQL 的 `create_all` 上
同样生效，不只 PG。

**装置**（`test_schema_agreement_pg_v33.py` 从 3 条长成 5 条）

- `test_column_types_and_lengths_agree_on_postgres`：反射两库的 `str(type)` 与 `length` 逐列比；
  防空绿 `compared >= 200`、`with_length >= 100`，并钉三个具体锚点——`model_usage.run_id` /
  `agent_events.run_id` / `model_usage.origin_run_id` 必须是 `VARCHAR(120)`（#47 放宽的那三列）。
- `test_indexes_and_uniques_agree_on_postgres`：索引按 `(name, columns, unique)`、唯一约束按
  `(name, columns)` 比；防空绿 `index_count >= 40`、`unique_count >= 6`，并钉
  `("uq_model_providers_name", ("name",))` 必须在迁移库里。
- **CHECK 不做对账**：当前 20 张表两条路径都是 0 条，空对空的相等是一条永不失败的装饰
  （#49 的「只认 `ast.Call` 扫出 0 处」就是这类假绿的近亲）。触发条件写在文件头：一旦任何模型/迁移
  加 `CheckConstraint`，先让两边各反射出一条非空 CHECK，再纳入对账。
- 模块里显式 `import blank_api.models`：`create_all` 的 oracle 来自已注册的表，别依赖 conftest 的导入顺序
  （勘察脚本第一版就因此得到 `orm={}`，20 张表被比成 0 张——防空绿断言会先拦下这种形状）。

**验证**

1. 修前红基线：`1 failed, 4 passed`，红的只有索引/唯一约束那一条，红因与勘察逐字相同（`E:/tmp/pg62/red1.log`）。
2. 修后：`5 passed`（PG 17.6，`E:/tmp/pg62/green1.log`）；`test_schema_agreement_v29.py` 仍 **6 passed**。
3. 变异电池 **3/3 红集合精确相等**（`E:/tmp/pg62/mutate_orm.py`，日志 `mutations.log`）：
   - M1 `model_providers.name` 的 `String(100)` → `String(120)` ⇒ 只有类型/长度一条红；
   - M2 摘掉 `parallel_group` 的 `index=True` ⇒ 只有索引/唯一一条红；
   - M3 把约束名改成 `uq_model_providers_name_mut` ⇒ 只有索引/唯一一条红；
   - 基线 sha256 `55f5a97d…`，每条还原后哈希一致、基线复绿。
4. 全量：第一次 **975 passed / 1 failed / 19 skipped**（244.20s，`regression62.log`），唯一红是既有的墙钟用例
   `test_read_only_calls_overlap`（实测 0.47s vs 0.45s），而它同一条用例里的**交错证据 `trace.overlap` 是通过的**
   ——并行确实发生了，只是计时超线。按纪律不放宽阈值：单跑 **3/3 绿**（`wall1/2/3.log`），空载后复跑
   **976 passed / 19 skipped**（rc=0，249.73s，`regression62b.log`）；在最终字节上再跑一次同样
   **976 passed / 19 skipped**（rc=0，250.80s，`regression62c.log`）。新增的两条在无 `BLANK_TEST_POSTGRES_URL`
   时按设计跳过，跳过数从 17 变成 19。

**还没覆盖的（量出来的，不是猜的）**

1. **CHECK 约束**：今天两条路径都是 0 条，没有可对实物；触发条件已写在用例头部。
2. 索引只比「名字 + 列 + unique」；`USING`、部分索引的 `WHERE`、排序方向、包含列（INCLUDE）没有比。
3. 类型用反射的字符串形式比（`VARCHAR(120)` / `TIMESTAMP` / `JSON`）；collation、enum 标签、数组维度没有单独比。
4. 变异只在 ORM 侧；迁移侧仍只由「第一次自然红」覆盖。
5. 这一批只在 PG 17.6 + Windows 上跑；MySQL 的 `create_all` 路径**没有重跑**（同一处修复对它是方言中立的，
   但这是推理，不是读数）。CI 的 `api-dialect` 复用同一条 pytest 步骤，**GitHub runner 仍没跑过**。

复现：

```bash
cd services/api
BLANK_TEST_POSTGRES_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/postgres" \
  .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_schema_agreement_pg_v33.py

# 勘察：只打印两库的形状差异
.venv/Scripts/python.exe -X utf8 E:/tmp/pg62/probe_shapes.py

# 变异电池：3 靶，跑完自动还原并以 sha256 自证
.venv/Scripts/python.exe -X utf8 E:/tmp/pg62/mutate_orm.py
```

## 7.28 任务 #63：同一套 schema 对账搬到真 MySQL 8.4——13 条读数量出一条真分歧

#62 把 PG 的两条建库路径对到了类型/长度/索引/唯一约束，但它在「还没覆盖的」里自己记着：
**MySQL 的 `create_all` 路径没有重跑**。MySQL 侧自 #49 之后只有「整条迁移能跑通」与 83 步 HTTP，
而 `create_all` 是测试与本地快速起库用的另一条路——两边形状不一致，测试库就与生产库不是同一个东西。

这一批在真 MySQL 8.4.0（`127.0.0.1:3307`，自管实例）上开两个库：一个 `alembic upgrade head`，
一个 `Base.metadata.create_all()`，然后逐表反射对账（`E:/tmp/mysql63/probe_shapes.py`）。读数：

- 20 张表 / **213 列**，其中 **118 列带长度**；**48 个索引**（MySQL 反射把唯一约束背后的索引
  也列进 `get_indexes`，与 `get_unique_constraints` 重复，两边同源即可）、**6 个唯一约束**、
  **21 个外键**、**125 个非空服务端默认值**；CHECK 两边都是 **0 条**。
- 类型/长度、可空性、索引/唯一约束的集合、主键全部一致。

**第一次真跑：13 条分歧，分成两类**

```
search_docs.created_at: 默认值 迁移='CURRENT_TIMESTAMP' create_all='(now())'
...（共 12 个 created_at/updated_at）
tasks: 外键 仅迁移=[('fk_tasks_thread_id', ('thread_id',), 'threads', ('id',))]
             仅 create_all=[('tasks_ibfk_2', ('thread_id',), 'threads', ('id',))]
```

1. **12 条时间默认值的两种写法不是分歧。** 迁移 0014/0016/0017 建表时两边都写 `func.now()`，
   但 0023 在 MySQL 上做 `SET NOT NULL` 必须 `MODIFY` 整列定义；Alembic 会把
   `existing_server_default=sa.text("CURRENT_TIMESTAMP")` 重述进 DDL，于是迁移库把默认值存成
   `CURRENT_TIMESTAMP`，而 `create_all` 存的是表达式默认值 `(now())`。MySQL 文档里
   `NOW()` 与 `CURRENT_TIMESTAMP` 是同义词，两者都在插入时求值。**没有改迁移**（改历史迁移
   不会改变已经迁移过的库，而且这次差异是语义相同的）；用例对**这一族**写法归一，
   再用「不带时间列插一行必须真落当前时间」证明归一没掩盖「默认值丢了」。
2. **外键名是真分歧。** 迁移 0014 的 `batch.create_foreign_key("fk_tasks_thread_id", ...)` 有名字，
   ORM 只写 `ForeignKey("threads.id", ondelete="SET NULL")`：MySQL 自动起 `tasks_ibfk_2`、
   PG 自动起 `tasks_thread_id_fkey`。**按名字 DROP 的脚本会一个中一个空**——与 #62 的唯一约束名
   是同一类缺陷，只是这次在另一条方言上、另一个对象上。修法：ORM 补同名
   `ForeignKey("threads.id", name="fk_tasks_thread_id", ...)`，Python 行为与迁移文件都没动。

同时把 PG 侧 v33 的 `fks` 身份从「从哪列指向哪列」扩成
`(name, constrained_columns, referred_table, referred_columns, ondelete, onupdate)`：
之前那一版即使 ORM 外键名与迁移不同，PG 对账也会全绿——这是 #62 留下的一个盲区，这一批补上。

**装置**（新增 `test_schema_agreement_mysql_v40.py`，4 条；文件名避开已被鉴权面占用的 v34）

- `test_both_provisioning_paths_agree_on_mysql`：逐列比 nullable/类型/长度/默认值（时间族归一），
  逐表比外键（名字 + ON DELETE/UPDATE）/索引/唯一约束/CHECK/主键。防空绿：表 ≥20、列 ≥200、
  带长度列 ≥100、索引 ≥40、唯一 ≥6、外键 ≥20、非空默认值 ≥120。
- `test_timestamp_default_spellings_are_equivalent_on_mysql`：钉住「写法分歧恰好是这 12 列、
  归一后都等于 `CURRENT_TIMESTAMP`」，再在迁移库与 `create_all` 库各插一行不带时间列的记录，
  断言 `created_at/updated_at` 非空。别的默认值真变了归对账用例管，这条只负责时间族——
  第一版把「任何默认值分歧」都算成自己的红，变异 M4 量出来后已收窄（见下）。
- `test_foreign_key_names_and_actions_agree_on_mysql`：两库的 `tasks` 外键名集合相等，
  且 `fk_tasks_thread_id` 存在、`ON DELETE` 为 `SET NULL`。
- `test_expression_defaults_land_values_on_mysql`：#49 的表达式默认值（`('[]')`/`('')`）在两种建库
  路径上都真落值；裸 SQL 不走 SQLAlchemy 的 JSON 反序列化，MySQL 把 JSON 列念成字符串，用例自己解析。

**验证**

1. 修前勘察：`diffs=13`（`E:/tmp/mysql63/shapes.json` 第一次落盘）；修 ORM 外键名后同一勘察
   `diffs=0`（12 条时间写法按同义归一）。
2. 修后：MySQL 4 条 + PG 5 条 = **9 passed**，两种方言都真跑迁移与 `create_all`。
3. 变异电池 **4/4 红集合精确相等**（`E:/tmp/mysql63/mutate63.py`，日志 `mutate.log`）：
   - M1 ORM 外键名改成 `fk_tasks_thread_id_mut` ⇒ MySQL 两条（对账 + 外键锚点）、PG 对账一条；
   - M2 `model_providers.name` 的 `String(100)` → `String(120)` ⇒ MySQL 对账、PG 类型/长度；
   - M3 摘掉 `parallel_group` 的 `index=True` ⇒ MySQL 对账、PG 索引/唯一；
   - M4 `tasks.status` 的默认值 `"draft"` → `"done"` ⇒ MySQL 对账、PG 服务端默认值。
   - **第一次 M4 多红了一条**（时间默认值用例）：它当时把「任何默认值分歧」都算成时间族的失守，
     红集合实测 3 条而期望写了 2 条。按纪律不放宽期望，收窄用例职责后重跑 4/4；基线 sha256
     `cbd35ae0…`（task.py）/ `55f5a97d…`（model_provider.py），每条还原后哈希一致、基线复绿。
4. 全量：无 MySQL/PG 环境变量时新增 4 条按设计跳过——**976 passed / 23 skipped**，rc=0，243.96s
   （`E:/tmp/mysql63/regression63.log`）；跳过数从 19 变成 23。
5. CI `api-dialect` 增 `MySQL 两条建库路径对账` 步骤（`BLANK_TEST_MYSQL_URL` 指向 service 的
   `mysql` 系统库），与 PG 对账同 job。

**还没覆盖的（量出来的，不是猜的）**

1. **CHECK 约束**：两条路径都是 0 条，没有可对实物；触发条件同 #62——任一侧出现非空 CHECK 再纳入。
2. 索引只比「名字 + 列 + unique」；`USING`、前缀长度、排序方向、隐藏列没有比。
3. 类型用反射字符串比；collation、字符集、enum 标签没有单独比。
4. 变异只打 ORM 侧；迁移侧仍由「第一次自然红」与迁移文件本身覆盖。
5. 只在 MySQL 8.4.0 Windows 自管实例 + PG 17.6 上跑；CI runner 仍没跑过。
6. 时间默认值的归一只放行 `CURRENT_TIMESTAMP` / `NOW()` 这一族；换成 `UTC_TIMESTAMP()` 这类
   真不同表达式时，归一不相等，由对账用例红——但这一点是推理，没有单独造一次变异。

复现：

```bash
cd services/api
BLANK_TEST_MYSQL_URL="mysql+aiomysql://root:@127.0.0.1:3307/mysql" \
BLANK_TEST_POSTGRES_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/postgres" \
  .venv/Scripts/python.exe -X utf8 -m pytest -q \
    tests/test_schema_agreement_mysql_v40.py tests/test_schema_agreement_pg_v33.py

# 勘察：两库形状差异 + SHOW CREATE TABLE 原文
.venv/Scripts/python.exe -X utf8 E:/tmp/mysql63/probe_shapes.py

# 变异电池：4 靶，跑完自动还原并以 sha256 自证
.venv/Scripts/python.exe -X utf8 E:/tmp/mysql63/mutate63.py
```

## 7.29 任务 #64：把 PG/MySQL 的对账维度搬回主方言 SQLite——8 条布尔默认值分歧

#61–#63 一路把「两条建库路径同形状」从列名扩到类型/长度/索引/唯一约束/外键名/默认值，
但补的全是第二、第三种方言；**主方言 SQLite 的 v29 还停在 #49 的四样**（表/列/可空性/外键）。
生产库走 `alembic upgrade head`，测试与本地快速起库走 `create_all`——主方言上少一个索引、
约束名不同，测试依旧全绿，生产却缺对象。这一批把 v29 从「只建一个迁移库、拿 ORM 字面量当 oracle」
改成「两条真库互相对账」，并把新维度逐个搬回来。

**读数**（`E:/tmp/sqlite64/probe_shapes.py`，两库各自真跑）：

- 20 表 / **213 列**，其中 **118 列带长度**；**38 个索引**（SQLite 不像 MySQL 那样把唯一约束
  重复列进 `get_indexes`）、**6 个唯一约束**、**21 个外键**、**125 个非空服务端默认值**、
  **0 个 CHECK**。
- 类型/长度、索引/唯一/主键、外键（名字 + ON DELETE/UPDATE）、默认值，第一次修完布尔后全一致。

**唯一的真分歧：布尔默认值的拼法**

```
model_providers.supports_tools: 默认值 迁移='0' ORM='false'
model_providers.enabled:        默认值 迁移='1' ORM='true'
...（共 8 列：model_providers 3、team_bots 2、team_members 1、team_rounds 1、workspaces 1）
```

迁移侧（0020/0022）写的是字符串 `'1'/'0'`；ORM 侧此前写 `server_default=text("true"/"false")`，
在 SQLite 上原样落成 `'true'/'false'`。SQLite 会把它们各自读回 1/0，所以**行为测试全绿**——
这正说明「只测行为」看不见声明漂移。修法：ORM 改用 SQLAlchemy 的 `true()/false()`，在 SQLite 上
编译成 `1/0`、在 PG 上编译成 `true/false`（PG 服务端归一成布尔）、在 MySQL 上落 `1/0`，
与三条迁移的写法语义一致；Python `default=` 未动。改动只在 `model_provider.py` / `team.py` /
`project.py` 的 `server_default`。

**装置**（`test_schema_agreement_v29.py`，6 → 9 条）

- `provisioned_pair`：同一个临时目录里建两个库，迁移那条走 subprocess + `alembic upgrade head`
  （env.py 用应用异步引擎，同进程跑会搅事件循环），另一条 `Base.metadata.create_all()`；
  模块显式 `import blank_api.models`，否则 `create_all` 只会建出 0 张表（#62 踩过一次）。
- `_reflected` 从「列/可空性/外键二元组」扩到类型、长度、默认值原文、外键六元组
  （名字 + 列 + 目标表 + 目标列 + ON DELETE + ON UPDATE）、索引、唯一约束、CHECK、主键。
- A1 仍是表/列/可空性/外键的总闸门；新增
  `test_column_types_and_lengths_agree`（防空绿：比到 ≥200 列、≥100 个带长度列；
  锚点 `model_usage.run_id`/`agent_events.run_id`/`model_usage.origin_run_id` = `VARCHAR(120)`、
  `model_providers.name` = `VARCHAR(100)`）、
  `test_indexes_and_uniques_agree`（索引 ≥30、唯一 ≥6；锚点 `uq_model_providers_name`、
  `ix_model_usage_parallel_group`）、
  `test_server_defaults_agree`（默认值 ≥120；锚点 8 个布尔列与
  `tasks.attachments="'[]'"`、`tasks.goal="''"`、`agent_runs.request_payload="'{}'"`）。
  CHECK 两边都是 0 条，不写空对空判定，触发条件记在文件头。
- A2–A5（LOB 字面量默认值、迁移 AST 扫描、默认值落值、0023 NULL 闸门）原样保留。

**验证**

1. 修前勘察：`diffs=8`（8 条布尔拼法，`E:/tmp/sqlite64/shapes.json` 第一次落盘）；改 ORM 后
   同一勘察 `diffs=0`。诚实缺口：这 8 条没有留 pytest 自然红，改由变异 M1（把同一列写回
   `text("false")`）证明该判定会红。
2. 修后 `pytest -q tests/test_schema_agreement_v29.py`：**9 passed**（5.13s，含两次真建库）。
3. 变异电池 **4/4 红集合精确相等**（`E:/tmp/sqlite64/mutate64.py`）：
   - M1 `supports_tools` 默认值写回 `text("false")` ⇒ 只红 `test_server_defaults_agree`；
   - M2 `model_providers.name` `String(100)` → `String(120)` ⇒ 只红类型/长度；
   - M3 摘掉 `parallel_group` 的 `index=True` ⇒ 只红索引/唯一；
   - M4 摘掉 `tasks` 外键名 ⇒ 只红总闸门。
   基线 sha256 `deaa816f…`（model_provider.py）/ `cbd35ae0…`（task.py），每条还原后哈希一致、
   基线复绿。变异靶点跨 CRLF 时必须先归一换行再匹配（第一版按 `\n` 找靶点，报「0 次」）。
4. 全量：**979 passed / 23 skipped**，245.79s（`E:/tmp/sqlite64/regression64.log`），
   979 = #63 的 976 + 新增 3 条；跳过数不变（本文件不需要外部库，默认就跑）。

**还没覆盖的（量出来的，不是猜的）**

1. **CHECK 约束**：两条路径都是 0 条，没有可对实物；触发条件同 #62/#63——任一侧出现非空 CHECK。
2. 索引只比「名字 + 列 + unique」；partial index 的 `WHERE`、表达式索引、排序方向没有比。
   当前 SQLite 38 个索引全是普通列索引。
3. 类型用反射字符串比；SQLite 的 affinity 会吞掉一些写法差异（如 `DATETIME` vs `TIMESTAMP`），
   这类差异只有第二方言能看见。
4. 变异只打 ORM 侧；迁移侧由第一次自然红与迁移文件本身覆盖。
5. 只在 SQLite（Windows 自带）上跑；`alembic` 两条路径的 5 秒成本适合进默认套件，不需要 CI 新步骤。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_schema_agreement_v29.py

# 勘察：两库形状差异
.venv/Scripts/python.exe -X utf8 E:/tmp/sqlite64/probe_shapes.py

# 变异电池：4 靶，跑完自动还原并以 sha256 自证
.venv/Scripts/python.exe -X utf8 E:/tmp/sqlite64/mutate64.py
```

## 7.30 任务 #65：全群累计不能拿发言行当筛子——两个来源取并集

#47 把「这一轮合起来花了多少」改成按 `origin_run_id` 查账本，`round.usage` 对不上发言的钱
还有「未对应发言」一档。但同一个响应里的全群累计 `usage_by_bot` 仍是 #45 的写法：

```sql
TeamMessage JOIN ModelUsage ON ModelUsage.task_id = TeamMessage.task_id
WHERE TeamMessage.group_id = :g GROUP BY bot
```

inner join 只认「有发言行」的钱。一笔发起轮属于本群、task_id 却没挂到任何消息上的账
（中途失败后没写成消息的那次调用、编排器的额外调用），`round.usage` 算得出来，
全群累计却把它静默吃掉；返回里既没有总数、也没有差额档，看的人无法知道少没少。

**实现（`services/team.py` 新增 `_group_ledger_usage`）**

- 两个来源取并集：① `task_id` 属于本群任一条消息（连上线前没有 `origin_run_id` 的老账也算）；
  ② `origin_run_id` 属于本群任一 `TeamRound`（连没有发言行的钱也算）。两个来源按 `usage.id`
  去重，正常发言账两个条件都命中，不去重会凭空翻倍。
- 对不上发言的行归 `bot_id=""`、`bot_name="未对应发言"`，与 `round.usage` 同一档名字；
  于是 `Σ by_bot == tokens`、`Σ calls == calls`、`Σ subagent_calls == subagent_calls`
  三条都是构造出来的恒等式。
- `group_state` 新增 `usage` 字段（`tokens / calls / tasks / runs / subagent_calls / by_bot`），
  与 `round.usage` 同形状但**没有** `origin_run_id`；`usage_by_bot` 保留为 `usage["by_bot"]`
  的同源别名，不另算一遍。前端 `web/src/components/team/api.ts` 只补类型（`GroupLedgerUsage`），
  页面尚未消费这个字段——不假装已经展示。
- 大群的 task_id / origin 列表按 500 一段做 `IN`，避开旧版 SQLite 的变量上限。

**验证**

1. 自然红：两条新用例在修前都红（`KeyError: 'usage'`；旧响应没有这个字段），修前
   `2 failed, 10 deselected`。
2. 修后 `tests/test_origin_run_v27.py + tests/test_team.py`：**20 passed**。
3. 变异电池 **4/4 红集合精确相等**（`E:/tmp/team65/mutate65.py`，基线 sha256 `399f488f…`）：
   - M1 摘掉 origin 来源 ⇒ 只红「没有发言行的钱」；
   - M2 摘掉按 id 去重 ⇒ 红「两个键都命中只算一次」+「全群字段不是轮字段」；
   - M3 不数子代理 ⇒ 红「没有发言行的钱」（第一版只断言了顶层 `subagent_calls`，
     每档之和没对，M3 因此全绿——补上 `Σ by_bot.subagent_calls == usage.subagent_calls`
     后重跑 4/4，没有放宽期望）；
   - M4 摘掉 task 来源 ⇒ 只红「全群字段不是轮字段」（没有 origin 的老账被丢）。
   每条变异后都先还原、再复绿，sha256 逐字节核对。
4. 全量：第一次 **980 passed / 1 failed / 23 skipped**，唯一红是既有的墙钟用例
   `test_read_only_calls_overlap`（0.65s vs 0.45s；同一用例的交错证据 `trace.overlap`
   已通过，是并行确实发生、只是负载下超时）；单跑 **3/3 绿**（0.60 / 0.59 / 0.77s），
   不放宽阈值。空载复跑 **981 passed / 23 skipped**，rc=0，291.92s
   （`E:/tmp/team65/regression65b.log`）。

**还没覆盖的（量出来的，不是猜的）**

1. 全群累计现在把「本群所有用量的行」读进进程再分档；大群（数万笔）下这是每次轮询的
   扫描成本。当前没有实测大群，先记录不优化——要用 SQL 侧 `GROUP BY task_id, node`
   聚合时再补性能判定。
2. `tasks` / `runs` 两档只用于展示，没有单独的行为判定；恒等式只钉了 tokens/calls/subagent_calls。
3. 前端没有渲染 `usage`，只更新了类型；要做「全群累计」面板时仍需浏览器点验。
4. 变异只打在 `_group_ledger_usage` 内部；路由层是否把这个字段原样透出，由 20 条 HTTP 用例覆盖。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_origin_run_v27.py tests/test_team.py
.venv/Scripts/python.exe -X utf8 E:/tmp/team65/mutate65.py
```

## 7.31 任务 #66：发起轮进事件时间线——每个事件自己说得清归属

#47 把发起轮记进了账本，`EventRecorder` 上也一直有 `origin_run_id`，但 `agent_events`
这一层没有它：`/tasks/{id}/events` 与证据包的 `timeline` 都只端出 `run_id`。后者是
"每人一个"的键——团队一轮里每个发言成员各有自己的 Task 和自己的 run，所以拿事件回答
"这条事件属于哪一轮群聊"必须再去 join 任务或账本。证据包一旦离开数据库，归属就丢了。

**实现（一列、一个写入点、两个出口）**

- `models/task.py:102` `AgentEvent` 新增 `origin_run_id`（`String(120)`、`NOT NULL`、
  `server_default=""`、带索引），与账本 `model_usage.origin_run_id` 同源同宽。
- `services/agent/events.py:69` `EventRecorder.add` 把记录器上的那个值写进每一行；
  顶层运行的"发起轮就是它自己"仍在构造器里定义一次（`:38`），子代理的 `_Recorder`
  （`services/subagent.py:178`）只是照抄，所以孙辈事件天然知道被哪一轮引起的。
- 读侧两个出口同源：`schemas/task.py:73` `EventOut` 端出 `origin_run_id`（`/events` 与
  SSE 共用），`services/agent/evidence.py:512` 的 `timeline` 同时给 `run_id` 与
  `origin_run_id`。
- 迁移 `alembic/versions/0024_events_origin_run_id.py`：加列 + 建索引。存量行保持空串——
  它们上线前没记过发起轮，**不许拿 `run_id` 回填**（那会把"没记"伪装成一个答案）。

**判定装置（每条都能失败）**

- 用例 `tests/test_event_origin_v41.py` **6 条**：记录器写入与改值同源；顶层运行以自己
  为发起轮、两个都没给时留空串；子代理每条事件继承父辈发起轮；`/tasks/{id}/events`
  原样端出（老事件是空串）；证据包 `timeline` 两个键都在；迁移 0024 的子进程往返
  （0023 上没有列 → 加列/索引 → 老行空串且行原样 → 降级回去）。
- **自然红**：把同一份用例放到 HEAD（`edfddc2`）的临时 worktree 上跑，**6/6 全红**，
  原因各自成立：3 条 `'AgentEvent' object has no attribute 'origin_run_id'`、
  2 条 `KeyError: 'origin_run_id'`（接口 / 证据包）、1 条迁移 DDL 为空。
- 变异 **6/6 红集合精确相等**：M1 写库丢掉发起轮 ⇒ 5 条；M2 顶层不再以自己为发起轮 ⇒
  1 条；M3 证据包不端出 ⇒ 1 条；M4 接口不端出 ⇒ 1 条；M5 迁移不建索引 ⇒ 1 条；
  M6 迁移用 `run_id` 回填 ⇒ 1 条。基线 4 份文件 sha256 逐字节还原
  （`E:/tmp/event66/mutate66.py`）。
- 全量 **987 passed / 23 skipped**，rc=0，259.82s（`E:/tmp/event66/regression66.log`）；
  987 = #65 的 981 + 新增 6 条。

**还没覆盖的（量出来的，不是猜的）**

1. 老行（上线前的 `agent_events`）没有回填：真实旧库里这些事件的发起轮仍是空串，
   要归属只能退回 `run_id` / 任务线索。这是有意的诚实缺口。
2. 事件上的发起轮不参与任何聚合；证据包还没有「按发起轮汇总事件」的表，只有逐条字段。
3. 只加了 SQLite 子进程迁移往返；MySQL/PG 上这条迁移的实跑未单独做（schema 对账 v29
   会覆盖列/索引形状，但不会替 MySQL 执行 ALTER）。
4. 变异只打写入/读出口与迁移；`EventOut` 的 SSE 分支由同一定义覆盖，未单独变异。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_event_origin_v41.py
.venv/Scripts/python.exe -X utf8 E:/tmp/event66/mutate66.py
```

## 7.32 任务 #67：证据包按发起轮汇总——不把「没记」并成第 0 轮

#66 让每条事件自己说得清发起轮，但证据包只有 `turns`（按 `run_id`）、`branches`
（按 `trace_id`）与 `parallel_groups`。团队一轮里每个发言成员各有自己的 run，要回答
「这一轮一共花了多少、动过几个任务」仍得把成员行筛出来手加；而群轮本身在那三张表里
没有一行。这一批补的就是这张表。

**实现（一个函数、两个字段）**

- `services/agent/evidence.py:95` `origin_rollup(rows)`：按 `origin_run_id` 分组，
  产出 `origin_run_id` / `run_ids`（去重排序）/ `threads`（trace 数）/ `tasks`（task 数）/
  `subagent_calls`（`node == "subagent"` 的行数）与 `_usage_totals()` 的用量字段。
- `evidence.py:487` `usage["origins"]` 与 `usage["turns"]` 并列；`:488`
  `usage["origin_unattributed_calls"]` 报出没有发起轮的账本行数。
- **空串不参与分组**：空发起轮只读作"上线前没记"，把它并成"第 0 轮"等于替历史编一个
  归属。要归属只能退回 `run_id` / 任务线索——这是有意的缺口，不是补全。

**判定装置**

- 用例 `tests/test_origin_evidence_v42.py` **3 条**：① `origin_rollup` 的分组、空串排除、
  子代理计数；② 端到端证据包的 `usage["origins"]` 与 `usage["turns"]` 并列，且各档
  `total_tokens` 由库内账本行**现场求和核对**（不写死数字）；③ 同一次查询下
  `origin_rollup(rows)` 与证据包 `usage["origins"]` 自洽。
- **自然红**：HEAD（`edfddc2`）的 worktree 上整个文件在 collection 阶段
  `ImportError: cannot import name 'origin_rollup'`——不是逐条失败。逐条可失败性由下面的
  变异靶提供，不拿"红灯数量"冒充覆盖。
- 变异 **4/4 红集合精确相等**：M1 按 `run_id` 分组 ⇒ 2 条；M2 把空串并成第 0 轮 ⇒ 2 条；
  M3 不数子代理 ⇒ 2 条（实测集合，不是预期写宽）；M4 证据包不端出 `origins` ⇒ 2 条。
  `evidence.py` sha256 逐字节还原（`E:/tmp/event66/mutate67.py`）。

**还没覆盖的**

1. 只汇总量，不汇事件条数/失败数；要核对"这一轮挂过几次卡"仍得读 `timeline`。
2. 空发起轮的账只有计数，没有明细出口；老库要归因仍得 join 任务。
3. 没有在真实 MySQL/PG 上重跑这张汇总表；SQL 只是 `select` 账本行，方言风险低但未单测。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_origin_evidence_v42.py
.venv/Scripts/python.exe -X utf8 E:/tmp/event66/mutate67.py
```

## 7.33 任务 #68：批准过的前缀落盘——换任务、重启进程仍认得

#54 把"总是允许"的粒度修到真正执行的那一段，但记忆只有两层：会话内
（`PermissionPolicy`）与任务内（回放 `approval.granted`）。换一个任务，同一串命令
还要再挂一次卡。Codex 把这类决定写成 `~/.codex/rules/default.rules`，进程重启不失效。

**实现（一层存储、一个加载点、一个落盘点）**

- `services/approval_rules.py`：`path()` 默认取与设置文件同目录的 `approvals.json`
  （桌面壳把设置指到用户数据目录，规则自然跟着走；`BLANK_APPROVALS_FILE` 可指认）；
  `add_rule` 原子写（临时文件 + `os.replace`），重复规则移到最前、上限 200；
  `_read` 对**文件缺失/坏 JSON/版本不认识/非 UTF-8** 一律返回空表（fail-closed）。
  只收 `run_shell` 的非空前缀——`scope=tool` 是"本会话该工具"的宽口径，写盘等于整机永久放行。
- `services/settings.py:106` 新增公开的 `settings_path()`，规则文件与设置同源。
- `services/permissions.py:411` `PermissionPolicy.persistent_prefixes` 与本次会话的
  `approved_prefixes` 分开存；`:522` 只有整条命令逐段命中才放行，归因写
  `prefix:persistent:…`（本会话的批准仍念 `prefix:…`，驳回先于它生效）。
- `services/agent/graph.py:1540` 在 `approval.granted` 之后、且仅当
  `scope == prefix and name == run_shell` 且算得出规则时落盘，并补一条
  `approval.rule_persisted`（含 `persisted` 真假）；写盘失败不让已获批的调用失败。
- `services/agent/runtime.py:275` 每次构建策略时加载规则；读不出来就是空集——多问一次，
  而不是少问一次。

**判定装置**

- 用例 `tests/test_approval_persist_v43.py` **22 条**，三块：
  ① 存储：往返、去重（**直接看文件本身**，不只读侧）、上限、只收 `run_shell`；
  ② fail-closed：坏 JSON / 版本不认识 / `rules` 非表 / 坏字节 / 坏条目逐条跳过；
  ③ 判定与端到端：落盘前缀单独归因、不覆盖另一段内联正文与复合命令第二段、
  手改进去的不可逆规则仍然问、本会话驳回优先、端到端"批准 → 落盘 → 新任务不再挂卡
  （`prefix:persistent:…`）"、`scope=tool` 一条都不写盘。
- **自然红**：HEAD 的 worktree 上 `ImportError: cannot import name 'approval_rules'`，
  在 collection 阶段整文件失败。逐条可失败性由变异靶提供。
- 变异 **8/8 红集合精确相等**：M1 运行时什么都不加载 ⇒ 1 条；M2 判定忽略落盘规则 ⇒ 2 条；
  M3 任意作用域都落盘 ⇒ 1 条；M4 接受不认识的版本 ⇒ 1 条；M5 写盘不去重 ⇒ 1 条；
  M6 任意工具都收 ⇒ 2 条；M7 写盘不封顶 ⇒ 1 条；M8 坏字节直接抛 ⇒ 1 条。
  4 份文件 sha256 逐字节还原（`E:/tmp/event68/mutate68.py`）。
- 全量 **1012 passed / 23 skipped**，rc=0，274.23s；1012 = #66 的 987 + #67 的 3 + 本任务的 22。

**过程中量到的一件事（已修）**

用例集第一次交叉跑就红了 `test_permission_modes_v11.py::test_once_scope_asks_again_for_the_next_command`：
前一个用例批准了 `git push` 前缀，规则写进了**会话级**共享文件，下一个用例的新任务因此
被静默放行。这不是实现坏了，恰恰是落盘层按设计生效；修法是把 `BLANK_APPROVALS_FILE`
在 conftest 里做成**每个用例一份空文件**（`_isolate_approvals_file`），跨任务持久化只在
单个用例内验证。

**还没覆盖 / 有意的取舍**

1. 只落 `run_shell` 的 `prefix` 档；`scope=tool` 与 `scope=once` 都不写盘（产品取舍）。
2. 文件是给人看懂、可手工编辑的，没有签名/完整性校验；能改文件的人本来就能改设置。
3. 并发写是最后写入者赢（临时文件 + 原子替换只保证不出现半截文件，不做合并）。
4. 前端没有"查看/撤销已落盘规则"的入口；目前删除文件即全部忘记（CLI/文件层操作）。
5. 默认路径跟设置文件走；桌面安装包下的实际落点未在打包产物上单独点验。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_approval_persist_v43.py
.venv/Scripts/python.exe -X utf8 E:/tmp/event68/mutate68.py
```

## 7.34 任务 #69：内联解释器代码抬成高风险——「没问过」不是「记住后放行」

`assess_risk` 原先只认两类：破坏性命令（`rm`/`del`/`git push`/`drop table`…）与联网
（`curl`/`wget`/`http(s)://`）。其余一律 medium，而 `PermissionPolicy.decide` 对
非 high/critical 直接返回 `risk:auto`。于是
`python -c "open('secrets.txt','w').write('x')"` 这类**路径与命令正则都看不见正文**的
调用，一次审批卡都不挂就执行了。这与 #54 的批准记忆无关，是审批闸根本没被触达。

**实现（一个判定放进 `assess_risk`）**

- `services/tools/shell.py` 新增 `_has_inline_code()`：按 `&& || ; & |` 切段，每段若以
  解释器开头（`python*`/`py`/`node`/`perl`/`ruby`/`php`/`pwsh`/`powershell`/`bash`/`sh`/
  `zsh`/`cmd`，允许绝对路径与 `.exe`），且**在第一个非旗标参数之前**出现内联旗标
  （`-c`/`-e`/`-p`/`-r`/`-Command`/`-EncodedCommand`/`/c`，或 `-lc`/`-pe` 这类短旗标并写），
  即判 high。
- 两个刻意的边界：`python script.py -c foo` 的 `-c` 是脚本参数；`python -m pytest`
  是模块入口——两者都不抬高。普通脚本调用仍是 medium/`risk:auto`，收紧只针对内联正文。
- 复合命令任一段命中即 high：`pytest -q && python -c '…'` 不能靠第一段降风险。

**判定装置**

- `tests/test_inline_code_risk_v44.py` **22 条**：12 组内联形态（含绝对路径、旗标前置、
  PowerShell `-EncodedCommand`、`cmd /c`、`bash -lc`）为 high；7 组非内联形态仍 medium
  （`-m 模块`、脚本参数、`py -V`、`pytest`、`git status`、`npm run build`）；复合命令两正一反；
  用**生产定义** `shell_tools()` 判定 `python -c …write()` 得到 `ASK/high`（改动前是
  `ALLOW/risk:auto`），并保留普通脚本 `risk:auto` 作为对照。
- **自然红**：同一份用例放到 HEAD 的 worktree 上跑，**14 failed / 8 passed**——红的正是
  12 组内联参数 + 复合命令 + 生产工具判定，绿的正是"不该收紧"的 7 组与普通脚本对照。
- 变异 **4/4 红集合精确相等**：M1 `assess_risk` 不认内联 ⇒ 14 条；M2 去掉"第一个非旗标
  参数即停" ⇒ 脚本参数一条；M3 解释器只留 python/node 系 ⇒ 4 条；M4 把 `-m` 当内联 ⇒
  模块入口一条。`shell.py` sha256 逐字节还原（`E:/tmp/event69/mutate69.py`）。

**代价与还没覆盖的**

1. 审批频率上升：`bash -lc 'ls'` 这类包装也会挂卡；这是有意的取舍——包装里的正文同样
   看不见，宁可多问。
2. 只按**首段可执行名**识别，`env python -c …`、`cmd /c` 里再套一层、编码后的
   PowerShell（`-EncodedCommand` 已被判 high，但内容不解析）未逐形态覆盖。
3. 风险只影响"要不要问"；内联正文里又藏路径/密钥的检测仍靠 `safety.py` 与执行边界，
   本批没有扩大它。
4. 真模型/浏览器未重跑；本批是风险判定层，界面只会在待审批列表里多出这类卡片。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_inline_code_risk_v44.py
.venv/Scripts/python.exe -X utf8 E:/tmp/event69/mutate69.py
```

## 7.35 任务 #70：工具档的例外写回卡片——「不再询问」不能删掉不可逆

**量出的偏差**：`PermissionPolicy.decide()` 的判定顺序是 计划模式 → `is_irreversible()`
（L495–499）→ bypass / 高风险开关 → 动作 / 前缀 / 持久前缀 → `approved_tools`（L531）→
接受编辑 → `risk:auto`。也就是说工具档**确实不会**放行不可逆命令：记住 `run_shell`
以后，`git push --force`、`drop table`、`mkfs` 仍然 `ASK/irreversible`。

但卡片 `approval_options()` 的 warning 只写「该工具在本会话内不再询问」。前半句对普通
命令成立，后半句却成了一律无条件的承诺——用户批的是一张写着「以后都不问」的卡，实际
仍会被逐条拦。这不是权限缺陷，是**文案与判定不同源**：判定里那条例外没有任何界面出口。

**改动**

- `services/permissions.py`：warning 改成「该工具在本会话内不再询问；不可逆命令仍逐条确认」。
- `tests/test_permission_modes_v11.py`：工具档记住后，`curl` 得到 `ALLOW`，而
  `mkfs.ext4 /dev/sda1` 仍必须 `ASK` 且 `rule == "irreversible"`。这条断言**能失败**——
  把 `approved_tools` 分支挪到不可逆判定之前，它立刻变 `ALLOW`。
- `tests/test_approval_memo_v37.py`：卡片 warning 必须含「不可逆命令仍逐条确认」，
  文案回退即红。

**结果与边界**：两个文件 **57 条全绿**；判定逻辑零改动，本批没有新增分支，因此没有
值得做的变异靶（改文案不会红，正是因为它不是逻辑）。同批的 #69 已把内联代码抬成
high，卡片因此会更频繁地出现在这类命令上，文案必须先把「工具档仍拦不可逆」说清。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_permission_modes_v11.py tests/test_approval_memo_v37.py
```

## 7.36 任务 #71：`run_id` 的四种形状在读侧命名——「没记」与「记了但认不出」分开

账本里 `run_id` 实测四种形状共存：

| 形状 | 出处 | 读侧名字 |
| --- | --- | --- |
| 空串 | 回合归属上线前的存量行 | `unrecorded` |
| `team-<轮>-<消息>` | 群聊发言轮 | `team` |
| `task:<uuid>:attempt:N` | 同步入口的兜底键 | `attempt` |
| `agent_runs.id` | 排队入口的真实运行 id | `run` |

证据包 `usage.turns[].run_id` 之前只端原值，界面与外部消费者只能各自 `startswith` /
正则去猜；猜错了不报错，只是悄悄把群轮当普通运行、或把空串当第 0 轮。而「空串」与
「认不出的新形状」在账上含义完全不同：前者是**当时没记**，后者是**记了但读侧不认识**，
把两者混成一个 `run` 就把诚实缺口抹掉了。

**实现（只读侧，不改存量）**

- `services/agent/evidence.py` 新增 `run_id_kind()`：空串 → `unrecorded`；`team-` 前缀 →
  `team`；`^task:[^:\s]+:attempt:\d+$` → `attempt`；其余 → `run`。
- `turn_rollup()` 的每一项多一个 `"kind"`，与 `run_id`、用量同源；空串行仍然整行丢弃
  （不编号成第 0 轮），由 `unattributed_calls` 报数。
- `run_id_kind()` 的 docstring 写死这条口径：这是**命名**，不是回填；`run` 是兜底桶，
  不校验 uuid 形状，未来第五种形状出现时应显式加分支而不是默认冒充 `run`。

**判定装置**

- `tests/test_run_id_kind_v45.py` **3 条**：四种形状逐一命名；attempt 后缀必须完整
  （`task:…:attempt` 少了数字不算 `attempt`）；`turn_rollup` 带出 `kind` 且空串行仍被丢弃。
- `tests/test_turn_attribution_v24.py` 的精确键集加入 `"kind"`，并钉住普通运行读到
  `kind == "run"`——键集少一项、或形状判错都会红。
- **自然红**：同一份用例放回 HEAD 的 worktree，`v24` 单跑 **1 failed / 12 passed**
  （`KeyError: 'kind'`）；`v45` 是 **collection ImportError**（`run_id_kind` 尚不存在）。
- **变异 4/4 红集合精确相等**：M1 删掉 `team-` 分支 ⇒ 形状与轮表各一条；M2 空串行不丢弃 ⇒
  轮表、v24 老账用例与「丢弃空串」用例三条；M3 轮表不带 `kind` ⇒ 轮表与 v24 键集两条；
  M4 放宽 attempt 正则 ⇒ 后缀用例一条。`evidence.py` sha256 逐字节还原
  （`E:/tmp/event71/mutate71.py`）。

**代价与还没覆盖的**

1. `run` 同时容纳「普通运行」与「未来未知形状」。这是刻意的保守兜底，但也意味着新形状
   上线而读侧不改时不会报警——目前只在文档里写明「新形状要显式加分支」。
2. 形状判定不含 uuid 语义校验（`task:anything:attempt:1` 也算 attempt）；账本写入侧的
   构造点仍由 `EventRecorder` 单点负责，读侧不做二次验证。
3. 历史行按值即时分类，不做迁移回填；空串永远读作 `unrecorded`，不会被补成 `run`。
4. 界面未改：`kind` 已进证据包，前端是否展示另立任务；本批只保证数据面同源可核。

复现：

```bash
cd services/api
.venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_run_id_kind_v45.py tests/test_turn_attribution_v24.py
.venv/Scripts/python.exe -X utf8 E:/tmp/event71/mutate71.py
```

## 7.37 任务 #72：桌面壳的后端地址运行时可配——烘焙值退回兜底

**量出的偏差**：静态导出把 `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000` 编进了
`web/out` 的 chunk；壳却按 `BLANK_BACKEND_PORT` 拉起后端（`main.cjs` 的 `PORT`）。
两者只在不改端口时碰巧一致：用户或部署脚本一旦把端口改成 8123，后端在 8123，
界面仍去敲 8000，表现为「后端明明起着，界面一直说不可达」。旧的自检
`probeBackendFromRenderer` 也读 `NEXT_PUBLIC_API_BASE`，于是自检跟着烘焙值一起错。

**实现（运行时注入优先于构建值）**

- `desktop/api-base.cjs`：唯一算法——`BLANK_API_BASE` 优先（去尾部斜杠），否则
  `http://127.0.0.1:${BLANK_BACKEND_PORT || 8000}`。`main.cjs` 的 `HEALTH_URL`、
  sidecar 参数与自检都读它。
- `main.cjs` 注册同步 IPC `blank:api-base`；`preload.cjs` 在暴露
  `window.blankDesktop` 时取一次并带上 `apiBase`（取不到留空串）。
- `web/src/lib/http.ts`：`resolveApiBase(injected, baked)`——注入值非空则用它，否则退回
  `NEXT_PUBLIC_API_BASE`。模块加载时求值，正好赶在首屏请求之前。
- 打包清单同步：`electron-builder.files` 与 `self-deploy.cjs` 的壳入口列表都加上
  `api-base.cjs`（少一处，解包自部署时主进程会 `MODULE_NOT_FOUND`）。

**判定装置**

- `desktop/tests/api-base.test.cjs` **5 条**：默认 8000、端口生效、显式地址优先、
  尾部斜杠/空白归一、空白端口回默认。桌面全套 **15 passed**（含原有 GPU 10 条）。
- `web/src/lib/http.test.ts` **4 条**：注入值压过烘焙值、无注入退回烘焙值、都没有走相对
  路径、非字符串按未注入处理。前端全套 **153 passed**、`tsc --noEmit` rc=0。
- **真 Electron 点验**（不是只看单测）：用
  `BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000` 构建静态导出
  （`out/_next/static/chunks/…js` 里确实还有 `127.0.0.1:8000`），再以
  `BLANK_BACKEND_PORT=8123` 启动壳、8123 上放一个只认 `/api/v1/health` 的桩。
  8123 的访问日志出现 `GET /api/v1/health origin=app://bundle`、
  `GET /api/v1/projects origin=app://bundle`、`GET /api/v1/providers origin=app://bundle`
  ——**渲染进程的应用代码确实改打了 8123**；主进程日志同时是
  `渲染进程直连后端 http://127.0.0.1:8123 → ok 200`。截图落在
  `E:/tmp/desktop8000/proof.png`，窗口按 `main.cjs` 的 1280×820 创建。

**边界与还没覆盖的**

1. 只重跑了静态导出，**没有重打 electron-builder 安装包**；打包后的 `app.asar` 是否带
   `api-base.cjs` 只由清单与自部署拷贝列表保证，未按产物点验。
2. 运行态只支持 HTTP 基址；若将来前端走 WebSocket/SSE 的绝对地址，需同样从
   `window.blankDesktop.apiBase` 派生，不能另写一份 `8000`。
3. 反证没有做成「拆掉 preload 注入再跑」：那样渲染进程会去打用户常驻的 8000，
   属于禁止触碰的实例。注入是否承重由「烘焙值 8000 + 运行值 8123 + 8123 收到
   `origin=app://bundle`」这条实物证明。
4. `BLANK_BACKEND_HEALTH` 仍可整体覆盖健康地址（调试用），此时 `API_BASE` 与健康探测
   可以指向不同主机——这是刻意的逃生口，不是日常配置。

复现：

```bash
cd desktop && node --test tests/gpu-guard.test.cjs tests/api-base.test.cjs
cd web && node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"
# 真点验：web 静态导出 + 8123 桩 + 壳（BLANK_BACKEND_PORT=8123 BLANK_PROBE=1 BLANK_SHOT=…）
```

## 7.38 任务 #77：权限展开成逐条 ACE——「谁能碰哪些路径」不再靠人回去读配置

**量出的偏差**：Codex 的 `turn_context.permission_profile` 是**生效值**，不是配置引用——
`:root` 只读、仓库可写、`.git` 只读且 `missing_path_behavior:"skip"`，逐条摆在那里
（§2 的实测样本）。我们此前只写审批模式与前缀规则：读一条快照回答不了「这一步能碰哪些
路径」，得回设置页猜当时生效没有。差距表里这一行一直挂着「属沙箱立项范围」；M2–M4 把
档位、边界种类与自证装置做出来之后，这一条才具备写成事实的前提。

**实现（一条真源，两层不许混）**

- `governance.permission_profile()`：把限制展开成 `file_system.entries`——工作区一条
  `access:"write"`、每个允许根各一条、外部一条通配；每条都带 `enforced_by`：
  `os`（容器 / AppContainer 真拒）、`policy`（`SafetyPolicy` 的路径闸门，执行前判定）、
  `none`（关档且关闭「仅限工作区」）。容器里的外部路径落 `access:"none"` 而不是 `"deny"`：
  宿主路径根本不在挂载表里，说「拒」比说「没有」更含糊。
- `missing_path_behavior` 取 `safety.MISSING_PATH_BEHAVIOR`，与 `_outside_paths` 的
  实现同源：闸门只判**存在**的越界绝对路径，不存在的新目标直接跳过。关掉「仅限工作区」
  时写 `not_applicable`，不继续写 `skip`，更不写 `fail`——那会凭空造出一条我们没有的行为。
- `network` 拆成 `{requested, enforced_by, command_gate}`。本机 job_only 下这三项会分开：
  档位要的是不通网，真正拦着的是命令闸门的正则，不是内核。
- `turn_context` 顶层新增 `permission_profile`（与 Codex 同位置）；`graph.emit_governance`
  把工具层正在用的那份 `SafetyPolicy` 传进去，**不在治理层再 `load()` 一次**——两份策略
  各自读设置，快照与执行就会各说各话。
- 证据包 `boundaries.permission_profile` 取**末次** `turn.context` 的那一份：中途改过
  「仅限工作区」或换过档位，末次就是收尾时生效的那一份，逐次变化仍在事件流里。老任务
  报 `{"type":"unrecorded"}`，绝不按档位反推一份假授权。

**判据与计数**：`tests/test_permission_profile_v48.py` **10 passed**（job_only 归到
`policy`、内核已强制翻成 `os`、容器外部路径是 `none`、关档仍报路径闸门、只有真没边界才
说 `unrestricted`、`missing_path_behavior` 与闸门行为对质、允许根不漏宿主路径、
`turn_context` 带快照且指纹稳定、证据包老任务 `unrecorded`、证据包取末次）。
相关八套（v48 / `test_governance_snapshot_v16` / `test_boundary_facts_v20` /
`test_world_state_v22` / `test_doctor_v47` / `test_safety_policy_v050` /
`test_sandbox_matrix_v21` / `test_sandbox_v07`）**114 passed / 14 skipped**。

形状收敛：`permission_profile` 最后只留 `type / enforced_by / missing_path_behavior /
file_system / network` 五个键。第一版还带过 `"shell": {"enabled": …}`，没有任何一条判定
读它，也没有任何一层边界由它保证——**没有断言覆盖的字段不进治理快照**，删掉。
删掉后在**当前代码**上复跑全量：**1092 passed / 23 skipped**，rc=0，387.71s
（`E:/tmp/m77/regression-77-final.log`）。计数与删字段前那一次（1092/23）一样，
这本身就是那个字段没人读的旁证。上一次 1082（#75 的基线）+ 本批 10 条 = 1092。

基线不是 HEAD（HEAD 早于 M3，连 `boundary_report` 都没有），而是**本批动手前的工作区**：
分别摘掉两处接线后自然红——摘 `turn_context` 的键 → **1 failed / 9 passed**（`KeyError`），
再摘证据包那块 → **3 failed / 7 passed**，还原后 10/10 绿、`cmp` 逐字节一致。

变异电池 **8/8 按实测红集合精确命中**（M6 第一次期望写少了一条：允许根的掩码在
job_only 用例里也被断言，实测红了 2 条，按实测改期望而不是收窄断言）：

| # | 变异 | 实测红 |
|---|---|---|
| M1 | 内核已强制也报 `policy` | 1 条（os 用例） |
| M2 | 永远说 `managed` | 1 条（unrestricted 用例） |
| M3 | 把 `skip` 写成 `fail` | 2 条（闸门对质 + not_applicable） |
| M4 | 外部通配放行 | 2 条（job_only + off 档） |
| M5 | 网络拦截归到 `none` | 1 条（job_only 网络口径） |
| M6 | 允许根写真实路径 | 2 条（掩码 + 泄漏） |
| M7 | 证据包整块消失 | 2 条（unrecorded + 末次） |
| M8 | 证据包取第一条快照 | 1 条（末次） |

**真链路**（隔离实例 `:8061`，真 DeepSeek，任务 `20e6fbe6-…`）**8/8**：P1 真任务的
`turn.context` 带上了授权；P2 逐条 ACE 是 `<workspace:ws>` 写 + `*` 拒；P3 关档时
`type=managed, enforced_by=policy`；P4 `missing_path_behavior=skip`；P5 授权里没有
`E:\projects` / 用户目录；P6 证据包与末次事件**逐字段相等**；P7 快照去重仍是 1 条；
N1 负对照用 #77 之前的老任务 `16663dff-…`，证据包报 `unrecorded`。

**诚实的缺口**

1. 这**不是内核 ACL**：`enforced_by:"policy"` 就是在说「执行前判定」，只有 `os_enforced`
   或容器才写 `os`。把本机 job_only 的授权读成「系统会拒」仍然是错的。
2. `missing_path_behavior:"skip"` 是**如实记录**，不是顺手收紧：不存在的目标不参与越界
   判定，因此往一个不存在的外部路径写，闸门看不见；路径藏在解释器字符串里时更是看不见
   （设计稿 §1 的实测）。要不要收紧是产品裁决，本批不擅自改闸门。
3. 界面没有新增区块：前端本来就没有渲染 `boundaries`，这份授权在证据包 JSON 与
   `/tasks/{id}/evidence` 里可读。要做卡片得单独判一轮浏览器点验。
4. 证据包只保留末次授权；任务中途从 `workspace` 换到 `off` 的**历史**只存在于事件流，
   证据包不列时间线。

### 7.39 任务 #78：跨机对账装置——「另一台机器也是这么红的」要能拿两份报告对质

**量出的偏差**：M4 的退出判据是「第二台机器上八条探针的差异，都能归因到已知的环境层」
（设计稿 `docs/SANDBOX-WINDOWS-DESIGN.md` §6）。本机没有第二台机器：无 docker、无 wsl.exe，
M0 的实测表就是这一台非管理员 Win11 build 26300。此前 `blank doctor --sandbox` 只产出**一份**
自证报告，两台机器各跑一次之后**没有任何东西把它们放在一起比**——「差异能不能归因」这个问题
根本无从回答。这一批交付的是**对账装置本身**，跨机的实物仍缺（见缺口 1）。

**实现（`services/api/src/blank_api/doctor.py`，`SCHEMA_VERSION` 1 → 2）**

- **机器指纹**：`host_identity()` 落 `host_id`（`sha256(node|platform|machine|os_version)` 前 12 位，
  **主机名不进明文**）+ `platform / machine / os_release / os_version / elevated`。`elevated` 用
  `IsUserAnAdmin`（非 win32 退到 `geteuid`，拿不到写 `None` 而不是 `False`——「不知道」不是「没提权」）。
- **跑法标记**：报告新增顶层 `audit_task_id`。P-AUDIT 的结论取决于**这次有没有带 `--task`**，
  不带就是 `not_tested`；没有这个标记，两次不同跑法的报告会被当成机器差异。
- **归因表 `_ATTRIBUTION` 只收实测过的关系**（这是整条装置的承重墙）：每条是
  「差异字段 → 能解释它的探针集合」。身份层（`boundary_strength`/`appcontainer`/
  `restricted_token`/`elevated`）只许解释文件与网络四条（P-READ/P-WRITE/P-ENV/P-ESCAPE）；
  Job 层只许解释 P-PROC/P-INHERIT；`backend`/`tier_available` 只许解释「整档没跑起来」的批量
  `not_tested`。**Python 版本、系统小版本、架构一律只列进 `context`，不解释任何探针**——
  否则「机器不一样」变成万能理由。
- **三道拒绝先于比较**：缺 `host`/`audit_task_id`（schema 1 的老报告）→ `incompatible_reports`，
  note 里写明「不按档位补一份假指纹」；`schema` 不同；`platform` 不同（八条探针是 Windows 语义）。
  任一侧 `capability.apparatus_ok is not True` → `apparatus_untrusted`，**那一边没有结论资格**，
  点名是左还是右。
- **反冒充**：`host_id` 相同 → `same_host`，rc=1。同一台机器跑两份报告不构成跨机证据。
- **防「没比过报平安」**：`compare_reports()` 的 `base` 里先写 `compared: False`，只有过了三道拒绝
  才置 True；`render_compare()` 因此分两支——没比过就念「八条探针**没有逐条比较过**（原因见上面的
  说明），这里不报平安」。这一条是被真跑逼出来的：第一版在 `incompatible_reports` 的提前返回里
  照样打了「逐条相同」。
- 结果五值：`consistent` / `differences_explained`（rc 0）与 `unexplained_difference` / `same_host` /
  `incompatible_reports` / `apparatus_untrusted`（rc 1）。CLI 入口 `blank doctor --compare A.json B.json`，
  `packages/cli/src/index.mjs` 的分发条件原来只认 `--sandbox`，**`--compare` 根本到不了 Python**，一并修掉。

**判据与计数**：`tests/test_doctor_compare_v49.py` **17 passed**。取证基线用 `git show HEAD:doctor.py`
（#66–#77 已提交于 `dd96bad`，HEAD 可用）：把 HEAD 版 doctor.py 换回去、用**最终这 17 条**重跑 →
**17 failed**（全是 `AttributeError`/`KeyError`：没有 `host_identity`、没有 `compare_reports`、没有 `--compare`），
换回来 **17 passed**，`cmp` 逐字节一致。

变异电池 **10/10 按实测红集合精确命中**（`E:/tmp/m78/battery-rerun1.txt`，还原后 `cmp` 通过）：

| # | 变异 | 实测红 |
|---|---|---|
| M1 | 身份层差异什么探针都解释（`_IDENTITY_PROBES`→`_SANDBOXED_PROBES`） | 1 条（P-PROC 承重墙） |
| M2 | 同机两份报告也当跨机证据（`if base["same_host"]`→`if False`） | 2 条（夹具 + 真跑自比） |
| M3 | 缺机器指纹时补一份 | 1 条 |
| M4 | 报告版本不同也照比 | 1 条 |
| M5 | 跨平台也照比 | 1 条 |
| M6 | 没测过装置（`is not True`→`is False`）当成可信 | 1 条 |
| M7 | P-AUDIT 的差异算成机器差异（去掉 `run_flags`） | 1 条 |
| M8 | 换版本也算理由 | 1 条 |
| M9 | 主机名写进报告明文 | 1 条 |
| M10 | 没比过也报八条一致（`elif result.get("compared")`→`elif True`） | 1 条 |

M10 第一版把整行改成 `else:` 产生 `SyntaxError`，pytest 报 **ERROR** 而不是 FAILED，红集合为空——
电池当场报 `MISMATCH`（装置没被蒙过去）；改成 `elif True:` 才是合法的靶。**变异必须是"能跑的错"**，
语法错测的是解析器不是判定。

**真跑**（本机 `job_only`，`E:/tmp/m78/`）：`report-a.json` = 默认可信跑法八条逐条；
`report-b.json` = 带 `--task 20e6fbe6-…` → P-AUDIT `verified`；`report-c.json` = 不给
`BLANK_SANDBOX_ALLOW_LOCAL` → 七条 `not_tested`；`report-old.json` = 把 schema 改回 1 并删掉指纹。
`--compare` 实测（`E:/tmp/m78/compare-rerun2.txt`，逐条真 rc）：

- `a` vs `a` → `same_host` rc=1，正文点出「这是同一台机器跑了两次，M4 的『第二台机器』没有满足」；
- `a` vs `b` → `same_host` rc=1，同时列出 `P-AUDIT not_tested → verified 归因 run_flags`；
- `a` vs `c` → `same_host` rc=1，七条 `not_tested` 全部归因 `backend、tier_available`；
- `b` vs `c` → 同上，第八条 `P-AUDIT verified → not_tested 归因 run_flags`；
- `a` vs `old` → `incompatible_reports` rc=1，右侧 `host_id` 显示为 `?`，且**没有**「逐条相同」那句话。

**读数与一次我自己的失效**：收尾跑了**两遍串行**全量，均 rc=0：
`1109 passed / 23 skipped`（327.95s，`regression-78.log`）与 `1109 passed / 23 skipped`（337.85s，
`regression-78-final.log`）。在此之前，一次全量被会话中断留在半截态（末行是 44% 的进度条、没有汇总行），
我把「grep 不到 failed」当成了「没有失败」；随后同一台机器上两个全量重叠跑，其一留下
`3 failed, 1106 passed, 23 skipped in 608.51s`（608s ≈ 正常两倍的墙钟），**它的断言原文又被后一遍
`>` 覆盖了**——这 3 条红因此**未结案**，只能记为「重叠全量下的读数，且我销毁了自己的出处」。
`test_sandbox_matrix_v21.py` 单独复跑 27 passed，它与 v49/v47 两两同进程复跑 44/34 passed，
都不红。教训两条已写进验证纪律：**完成通知只证明进程结束，不证明结果可用**；**日志路径必须唯一、不许覆盖**。

**诚实的缺口**

1. **跨机的实物还没有**：`EXPLAINED` 与 `UNEXPLAINED` 两条分支只有离线夹具证明过能红/能绿；
   本机四份真实报告两两 `same_host`，装置**主动拒绝**把它们当成跨机证据——这是设计不是失败，
   但 M4 的退出判据（#76）仍未满足。
2. 归因表只收**实测过**的关系，因此偏保守：换 CPU 架构、换文件系统（ReFS/NTFS 大小写敏感性）、
   杀软拦截这类差异现在会落进 `unexplained_difference`，**这是有意的**（宁可判红也不给万能理由），
   但第一次在真第二台机器上跑时，很可能要为此扩表。
3. `elevated` 只回答「这个进程是不是管理员」，不回答「这台机器能不能建成 AppContainer」——
   后者要 M0 探针真跑，报告里靠 `capability.appcontainer` 承载，`--compare` 只比现成的值。
4. 装置没进 CI：`api-dialect` / `team-e2e` 之外没有 doctor 的格子，跨机对账本质上需要两台机器，
   runner 给不了（runner 之间同镜像，比出来只会是 `consistent`，没有信息量）。

## 7.40 任务 #79：审阅轨迹成为一等记录——「审查过什么」不再只活在会话内存里

**Codex 的实物**（§4.1 同一批 rollout）：`guardian_history` 分别 **96 / 151 / 133 项**、
228,102 / 373,181 / 532,767 字节，而同一份 rollout 的 `replacement_history` 只有 **18 / 36 / 39 项**
——审阅侧是模型侧的 5–8 倍。`guardian_metadata` 的键频次是
`client_authored 202 · mcp_attribution 85 · retained_source 27 · user_input_order 118 · fallback_token_limit_override 84`，
reasoning 条目还带 `encrypted_content`。这一格 §7.7 与 §7.9 都写的是「刻意不做」，本轮按用户点名补上。

**做的口径：借结构，不借抄正文。** 三处落点：

1. `services/agent/context.py: guardian_trail(before, after, *, reviews, in_effect)`——六个键
   `items` / `items_reviewed` / `items_in_model_context` / `reviewed_chars` / `reviews` / `in_effect`。
   `items` 是 `history_refs(before)` 的全量引用（不是模型侧剩下的那几条）再补
   `retained_id = ret:<sha12>`；`in_effect` 对标 `fallback_token_limit_override` 那一族「当时生效的档位」。
   **正文一个字节不进轨迹**——`agent_events` 会进证据包、被同项目其他人读到、还进全文索引，
   §7.6 那条 4145→1770 的教训就是同一个理由；能核对的是位置/角色/种类/字数/内容指纹。
2. `services/agent/graph.py`：`context.compacted` 载荷带上 `guardian_history`；
   `guardian_review_of()` + `guardian_reviews_for()` 把判定清单读成 **`guardian.reviewed` 事件的投影**；
   `_approval_gate` 把 `outcome`（`asked`/`blocked`/`auto_approved`）写进事件本身；
   **删掉 `AgentSession.guardian_reviews`**——判定的真源只有一个，副本长在内存上就是第二个口径。
3. `services/agent/evidence.py`：证据包 `guardian_history` = `{decisions, auto_approvals, windows, windows_unrecorded}`。
   `decisions` 逐字段从事件读，**不按档位反推**；老压缩没记轨迹就只数 `windows_unrecorded`、不补空的；
   审批卡 `_approval_hints` 念出 `guardian`——`None` 是「记过、当时关着」，缺键是「老任务没记过」，两种状态不并成一种。

**真链路量出来的缺陷（本任务真正的收获，离线全绿时看不见）**

装置 `E:/tmp/m79/trail_probe.py` 打在隔离实例 :8061 的真 DeepSeek 上：把供应商 `context_window`
临时改 3000 逼一次真压缩（`finally` 里还原，实测 rc=200 回到 65536），驱动是「**驳回 → 再 /run**」循环，
全程从不批准——这台实例没开沙箱，真模型的一条命令都不该被执行。

- 第 5 次跑（task `299c729f`，**130 条事件 / 6 次压缩 / 4 次审查**）：判定 seq **19/26/49/56**
  全在压缩 **81/88/95/102/109/124** 之前，证据包 `decisions` 四条齐全，**而每一次压缩的 `reviews` 都是 0**。
  根因：`guardian_reviews` 是会话内存上的副本，驳回把任务挂成 `paused`、再跑时新建会话，上一轮的审查一条都带不过来
  （和 `drain_steer` 是同一个理由）。
- 修法即上面的第 2 点。第 6 次跑（task `41c82847`，**139 条事件 / 7 次压缩 / 2 次审查**）：
  判定 64/71 出生在 `attempt:1`，压缩 seq=133 发生在 `attempt:2`，`reviews` 两条都带上来了，
  每条的 `run` 念的是**出生轮**不是当前轮，`outcome=asked`、`verdict=allow`、`reasons` 是真
  DeepSeek-flash 的原话（`latency_ms` 1043 / 1148），命令原文只以 `approval_key` 指认。
- **探针自己也被修过一次**：第一版的 R6c 在「判定在前、压缩在后、却一次都没带上」这种**该红**的形状上
  报了 NOT-TESTED——判据写宽了。加强后拿两份**已落盘的实物**复读（`E:/tmp/m79/r6c-replay-1.log`，rc=0）：
  `299c729f` → **FAIL**、`41c82847` → **PASS**，同一格判据一红一绿。
- probe-6 其余 12 格（`E:/tmp/m79/trail-probe-6.log`，13 项判定 FAIL 0 / NOT-TESTED 0，rc=0）：
  六键齐全；**审阅侧 22 vs 模型侧 9**；每条引用恰好 8 个键、不含正文；禁词（`E:\projects`、`.ssh`、`sk-` 等）
  **0 命中**，整块 5,912 字节；`in_effect={window_tokens: 3000, approval_mode: default, guardian_mode: veto}`
  与末次 `turn.context` 同源；证据包与事件那条轨迹**逐字段相等**，且 `items_reviewed=22` 等于清单长度、
  `reviewed_chars=10808` 等于逐条 `chars` 求和（独立复算，不是自己抄自己）；两条判定同指纹
  （同一个 `dir /b` 被审了两次，如实记两条）。N1 负对照：#79 之前的老任务 `cbd4ae9b` 有 1 次压缩 →
  `windows=0`、`windows_unrecorded=1`，不补空的。

**离线装置**

`tests/test_guardian_trail_v51.py` 16 条。HEAD 基线（`E:/tmp/m79/red-baseline.txt`）是**整文件收集 ImportError**
（`cannot import name 'guardian_trail'`）——16 条全是新增判定，没有一条是复述既有行为。
其中两条专治本轮缺陷：`test_a_review_from_an_earlier_run_is_still_in_the_trail`（跨 `attempt` 的判定必须还在轨迹里，
就是真链路那个形状的钉桩）与 `test_a_review_recorded_before_the_outcome_was_kept_has_no_invented_outcome`
（老事件没有 `outcome` 就不许凭空写一个空串）。

变异电池 `E:/tmp/m79/battery.py`：17 靶，`[ALL-OK]` rc=0，三个源文件 `cmp` 逐字节还原。
**实测轮 `battery-run4.txt` 报了 8 条 MISMATCH**——U3/U4/U5/U14–U17 的期望是我推理写的，与实测不符，
U1 也少一条（新加的跨运行用例一起红了）；全部按实测写回后才跑到 `battery-run5.txt` 全绿。红集合是量的，不是想的：

| 靶 | 改的是什么 | 实测红（条数） |
| --- | --- | --- |
| U1 | 压缩记录里不带审阅轨迹（删整块 `guardian_history`） | 4 |
| U2 | 轨迹只列模型侧剩下的（`history_refs(before)`→`(after)`） | 5 |
| U3 | 轨迹里的判定清单写成空（`reviews=await guardian_reviews_for(self)`→`()`) | 3 |
| U4 | 事件里不写审查的去向（删 `"outcome": outcome`） | 3 |
| U5 | 事件与指纹不对齐（删 `"approval_key": key`） | 2 |
| U6 | 审批卡不念审查意见（`hints["guardian"] = …`→`pass`） | 2 |
| U7 | 证据包整块消失（删 `"guardian_history": _guardian_history(events)`） | 1 |
| U8 | 引用没有留存身份（`retained_id`→`review_id`） | 2 |
| U9 | 当时生效的模式不记（`in_effect`→`{}`） | 2 |
| U10 | 没记过的轨迹不数（去掉 `unrecorded += 1`） | 1 |
| U11 | 审阅侧字节数写死 0 | 3 |
| U12 | 证据包把判定从档位反推（`guardian.reviewed`→`turn.context`） | 3 |
| U13 | 每次都当成压缩落轨迹（`if report.folded or …`→`if True:`） | 2 |
| U14 | 投影把命令原文抄进轨迹（多带一个 `arguments`） | 2 |
| U15 | 判定条目念当前轮而不是出生轮（`run`→`""`） | 4 |
| U16 | 老事件的去向被补成空串（无键也写 `""`） | 1 |
| U17 | 证据包丢掉审查的去向（`if "outcome" in payload:`→`if False:`） | 2 |

两处装置自身的失守，都当场报出来了：U8 第一版把整段循环改成空壳，是**语法错**——pytest rc=4、红集合为空，
电池报 MISMATCH 而不是假绿（和 §7.39 的 M10 同一课）；`test_no_compaction_means_no_review_trail` 一开始
在 13 个变体下都红不了，`[NEVER-RED]` 把它点出来，补 U13 才杀掉。

**读数**：v51 与 v12 同进程 25 passed；全量回归 `E:/tmp/m79/regression-79b.log` =
**1125 passed / 23 skipped in 372.74s，rc=0**（上一批基线 1109 + 本批 16 条，计数自洽）。
取证结束后 :8061 的配置已按 `E:/tmp/m79/settings.backup.json` 逐字节还原（`guardian=off`、`shell_enabled=false`）
并重启回原状，供应商窗口实测 65536。

**诚实的缺口**

1. **不抄正文**（裁决不变）：要回放「被折掉的到底是什么」，现在能定位到第几条、什么角色、什么种类、
   多少字、内容指纹是什么，**取不回正文**。
2. `client_authored` / `mcp_attribution` / `retained_source` / `revision` 仍无对应实现：
   我们没有「这条是谁写的、第几次改的」的一等来源，只有 `role` + `run` + `user_order`。
3. 轨迹**不跨任务**：`guardian_reviews_for()` 按 `task_id` 查事件。团队链路里成员各记各的，
   全群审阅轨迹要靠 #67 的按发起轮汇总去拼，这一格没做。
4. 判定条目只覆盖 `guardian.reviewed` 的**产出**，不带「审查当时看到了什么」：`reasons` 是模型原话，
   但它是在哪条指令、哪套档位下说出这句话的，仍要靠同 seq 附近的 `turn.context` 由人读——
   Codex 把整份审阅输入原样入账，我们没有（这也是缺口 1 的另一面）。
5. 加强后的 R6c 判据只在**两份既有实物**上复读过（一红一绿），没在第三次真跑上重验；
   真链路也只在 SQLite 那台 :8061 上量过，MySQL / PG 上「跨 attempt 的判定随压缩落下」这个形状没有实物。
6. **前端没渲染**：`guardian_history` 现在只活在证据包 JSON 里，界面上看不见（#80 的活）。
7. 轨迹是压缩那一刻的快照投影，之后的驳回/批准不会改写它：想问「这条判定最后怎么样了」要读
   `guardian.reviewed` 的 `outcome` 与后续审批事件，两者现在靠 `approval_key` 对齐，没有外键。

## 7.41 任务 #80：边界、权限快照与审阅轨迹进界面——后端说过的话，卡片得念出来

**这一批的起点不是「缺功能」，是「数据早就到了、页面没念」。**
#77 的 `permission_profile`、M4 的 `boundary_report`、#79 的 `guardian_history` 三块都已经写进
`/api/v1/tasks/{id}/evidence`，审批载荷里也早就有 `guardian`（`services/api/src/blank_api/services/team.py:1427`
的群聊 `pending[].approvals` 直接复用 `build_evidence(...)["pending_approvals"]`，等于后端一直在递，
前端一直不接）。§7.40 的缺口 6 就是这一格。

**三态是这批唯一的主张**：`undefined`（载荷里根本没这个键，这张卡/这台后端早于那一版）、
`null`（记过了、当时关着）、对象（说了话）。三态在 JS 里用真值判断必然塌成两种，所以：
- `web/src/components/tasks/api.ts`：`guardianFromPayload()` 用 `"guardian" in obj` 读，
  `boundaries` / `permission_profile` 同理归一化成 `recorded` / `type === ""` 这种「没记」的显式形状；
  读侧绝不把「没记」补成 `0` 或空数组（与 §7.40 的 `windows_unrecorded` 同一口径）。
- `web/src/components/tasks/status.ts`：15 个纯函数把归一化后的形状拼成人话
  （`sandboxTierLabel` / `enforcedFlagLabel` / `permissionEntryLabel` / `guardianReviewLabel` /
  `guardianDecisionLabel` / `guardianWindowLabel` / `toolCallBoundaryLabel` …），
  每个都单独可测、也是判定变异的靶。
- 渲染四处：`tasks/evidence-panel.tsx` 新增「边界与授权」「审阅轨迹」两个区块
  （只有 `boundaries.recorded` / `guardian_history.recorded` 为真才出现，没记就整块不说）；
  `tasks/timeline-blocks.tsx` 与 `bot/turn.tsx` 的任务审批卡念审查意见；
  `team/team-view.tsx` 群聊审批卡念审查意见（这批抓出来的真缺陷）。
- 每行都带「谁在保证」：`文件边界 = 系统强制` 与 `策略闸门（执行前判定，不是系统拒）` 分列，
  档位被部署策略降级时念「要求 workspace · 实际 off · 已降级」——面板上那句「沙箱档位 workspace」
  是最容易被读成「系统会拦」的一句话，这是这块区块存在的理由。

**离线**：`web/src/components/tasks/boundaries.test.ts` 新增 30 条（三态、降级、归账未归属、
代批不冒充实判、缺键不编话）；web 全量 **183 passed / 0 fail，rc=0**（`E:/tmp/m80/green5.txt`）；
`tsc --noEmit` **无输出 rc=0**（`E:/tmp/m80/tsc6.txt`）。

**判定变异电池**（`E:/tmp/m80/battery.mjs`）：22 条靶只改归一化与标签函数，
**22/22 按实测红集合命中，rc=0**（`E:/tmp/m80/battery-run2.txt`），起跑前有一条
「未变异必须 rc=0」的自检。三条值得单独念：
`enforced-null-collapses` / `payload-drop-tristate` 专治「用真值判断折叠三态」，
`tier-drop-degraded` 专治「只念档位、不念降级」——这三个正是这批最可能自己写出来的错。

**渲染变异电池**（`E:/tmp/m80/ui-mutate.mjs`，4 个 tsx 文件 × 8 条靶，两条点验 suite）：
两条基线先各绿一次（task 44/44、team 16/16），不绿就整批 `ABORT`；每条靶跑完 `copyFileSync`
逐字节还原并 `equals` 校验；**8/8 被打红，rc=0**（`E:/tmp/m80/ui-mutate-2.log`）。
其中 `team-drop-review`（`{reviewNote && (` → `{false && (`）红 3 条、
`team-collapse-missing`（把缺键并成 null）只红 TB3a 一条——这两条就是「群聊卡片接了线」和
「三态没塌」的可失败证明。

**真 Chrome 点验（playwright-core + 本机 Chrome，视口 1440×900，断言读 `textContent`）**

1. 任务侧 **44/44，rc=0**（`E:/tmp/m80/ui-verify-4.log`，同一份 44/44 也在电池基线里复现过）：
   打的是替身 :8063（经 dev :3123 代理），面板两个新区块的每一行、逐条授权列表、
   轨迹汇总行、判定条数与压缩窗条数都从 `/evidence` 现取再与 DOM 比；
   V7/V8 是「剥掉键之后区块必须不出现」的负对照。
2. 群聊侧 **16/16，rc=0**（`E:/tmp/m80/ui-verify-team-7.log`）：三个群的期望全部由
   `/team/groups/{id}/state` 的载荷经 `guardianFromPayload` + `guardianReviewLabel` 现算，
   TB0d 先证明「对象/null/缺键」算出**三种不同的话**，再拿页面逐条对；
   TB1b/TB2b/TB3a 是页面作用域的负断言（不许出现 `timed_out`、`latency_ms` 这种原始键，
   不许关着的时候凭空出现「审查员：」，缺键的时候两种话都不许出现）；
   TB1c/TB2c/TB3b 反过来钉「加了判定行没把卡片其余部分挤掉」。
3. **反证那一路不靠替身**：`REAL_ONLY=1` 关掉两条替身专属负对照后直通真模型实例 :8061，
   **38/38，rc=0**（`E:/tmp/m80/ui-verify-real-1.log`）——真后端没被替身改动带偏，
   替身只影响它自己造的那三种 guardian 形状。

**替身装置（`E:/tmp/m80/stub2.mjs`）的纪律**：只动两处——审批载荷的 `guardian`（set/keep/drop 三态）
与左栏群名，其余（工具名、参数、时间线、证据包）**逐字节照抄真物**；自检 8/8（`stub-selfcheck-2.log`）
里 T0a–T0c 分别验三种形状真被造出来、T0g 验另外两个群名没被改。装置自己的存活靠 SSE 逐块写 +
`res.on("error")` + `AbortController`，被截断的次数要在日志里数出来。

**这批在装置上栽的四次，全部如实记**（红的全是我的测量，不是产品）：
1. **群名有两个来源**：左栏来自 `/team/bootstrap`，卡片来自 `/state`。替身只改了 `/state`，
   点验报 `locator.click` 找不到群名——看起来像页面坏了，实际是装置改漏了一处。
2. **判据写宽造出假阳性**：TB3 第一版断「卡片里没有批准按钮」，而三张真卡的 `expired` 本来就是 `true`，
   界面本就不给按钮；`card.includes("驳回")` 是靠过期提示里那句「批准/驳回都不会再生效」撞绿的。
   修法：期望一律由载荷的 `expired` 推导，chip 只认按钮区独有的「已过期」，缺失类判定升到页面作用域。
3. **等待条件与容器都圈错过**：等「群名出现」＝左栏瞬间就有、数据还没渲染；
   按「高风险操作」圈容器只圈到 19 个字的标题行，判定行不在里面。
   用 `peek-team-dom.mjs` 打出祖先链才确认卡片根是 `div.mt-3 rounded-lg border …`，
   最终选择器锚在「需要你确认 + 该卡工具名」。
4. **并发跑第二条点验 = 测在飞的变异**：电池正在改写 4 个 tsx 时我并行起了 `ui-verify.mjs`，
   当场叫停，那份 12 行、没有汇总日志的产物改名成
   `ui-verify-5.ABANDONED-contaminated-by-mutate.log` 留档不作数。电池独占源码。

**诚实的缺口**

1. `bot/turn.tsx` 那张审批卡只在 `isLive && running` 时挂载（`bot-app.tsx:653`，
   `activeTaskId` 只能由**本会话发起的运行**产生），存量实物渲染不出来——
   这半句目前只有离线用例与 `timeline-blocks.tsx` 的同源读法在守，**需要一次真模型跑才能浏览器点验**。
   群聊那一路已经用存量实物点验过，所以「卡片念审查意见」这件事有两个来源、验了一个半。
2. 审查的 `timed_out` / `error` 两种形状**没有实物**：真链路（#79）落下来的判定全是
   `verdict=allow`、`timed_out=false`，替身也只造了 allow。标签函数写了这两种文案、
   离线用例钉住了，浏览器上没见过。
3. 这批纯前端，没有跨方言的新证据；点验只在 Windows + Chrome 上取过。
4. 界面仍然念不出「审查当时看到了什么」：`reasons` 是模型原话，被审的输入正文不在轨迹里
   （沿 §7.40 缺口 1、4，裁决不变）。
5. 逐条授权列表按 `RollupList` 折叠展示，超过一屏时靠展开看；没有做「按路径搜索」——
   真机上 ACE 条数多时读起来仍要人滚。

## 7.42 任务 #81：`pymysql<1.3` 这条买来的时间量了一遍——到期那天坏在哪一行

**这条上界是 §7.18/#50 那批写下的**（`services/api/pyproject.toml` 的 `mysql` extra：
`aiomysql>=0.3.2,<0.4`、`pymysql>=1.2,<1.3`）。当时的说法是「PyMySQL 1.3 计划删除
`escape_bytes_prefixed`，届时会直接 ImportError」，**这句是从上游 issue 读来的，不是量出来的**。
这一批把它量成实物，并回答「到期到底坏在哪」。

**先量上界到没到期**（PyPI JSON，2026-10-25 实测）：`PyMySQL` 最新发布 **1.2.3**（本机装的正是它，
`importlib.metadata.version` 与 `VERSION_STRING` 一致）、`aiomysql` 最新发布 **0.3.2**——
上游既没删别名，也没有修好引用的新版本。所以这条上界**仍在有效期内**，不需要裁决换驱动；
但它买来的是多久的时间，以前没人能答。

**到期那天坏在哪一行**：`aiomysql/connection.py:20` 是**模块顶层**的
`from pymysql.converters import (escape_item, encoders, decoders, escape_string, escape_bytes_prefixed, through)`。
装置是干净子进程里 `del pymysql.converters.escape_bytes_prefixed` 之后，再走一遍
`ensure_mysql_driver_compat("mysql+aiomysql://…")`（实测前先跑同一脚本的**对照格**：不删别名时必须打印 `OK`）。
量出来的形状：

- 坏在 **import 那一刻**，异常是 `ImportError: cannot import name 'escape_bytes_prefixed' …`；
  **猴子补丁来不及跑**——`ensure_mysql_driver_compat()` 那身补丁要在 aiomysql 已经导入成功之后才有机会执行。
  这一句才是「只能换驱动或等上游修引用」的实测依据；以前它只是我的推理。
- 因此 `db.py` 里把那圈裸 import 包起来，重新抛出**点名的** ImportError：带当前 PyMySQL 发行号、
  带在册上界 `1.3`、带出路（换驱动或等 aiomysql 修引用），并保留 `__cause__`。
  裸抛只会留一句 `cannot import name`，没人知道该动哪里。

**顺带挖出一条读数坑（这批最值钱的部分）**：PyMySQL 1.2.3 把 `pymysql.__version__` **硬写成 `"2.2.8"`**
（`pymysql/__init__.py:58`，`VERSION_STRING = "1.2.3"` 才是真号），目的是通过 Django 的
mysqlclient 版本检查。按 `__version__` 判 `<1.3`，会把**没到期的环境读成已过期**（2.2.8 ≥ 1.3）。
交叉印证：SQLAlchemy 的 pymysql 方言撞过同一件事，`retrieve_dbapi_version()` 只读 `VERSION_STRING`，
注释里原样写着「`__version__` … e.g. "2.2.8" for a pymysql that is itself version 1.2.0」。
新增 `db.pymysql_release_version()` 与上游同一口径：只读发行元数据，退一步读 `VERSION_STRING`，
两个都拿不到就返回 `None`——**不拿别的号冒充**。

**落点**：`services/api/src/blank_api/db.py`（上界常量 + 版本读法 + 点名 ImportError）、
`tests/test_mysql_driver_expiry_v81.py` 4 条（E4 上界在册且与常量同源、E1 发行号来自元数据且未到期、
E2 假号分歧哨兵、E3 到期形状真造出来）、`pyproject.toml` 注释改成实测口径。
E2 是一条**有退役条件的哨兵**：它断言的不是字面 `"2.2.8"`，而是「假号会越过上界、发行号不会」；
哪天上游撤了伪装，它会红并告诉你「退役这条、E1 的元数据口径不要动」。

**证据**：新用例 4/4；与旧兼容文件同跑 **7 passed rc=0**（`E:/tmp/m81/compat-2.log`）；
后端全量 **1129 passed / 23 skipped**，第一次 354.86s rc=0（`regression-81.log`）、
最终字节复跑 349.66s rc=0（`regression-81b.log`）= 上一批基线 1125 + 本批 4，计数自洽。
变异电池 `E:/tmp/m81/battery.py` 7 条靶：先 `--probe` 取实测红集合（`battery-probe-2.log`），
再按实测逐字钉进 `EXPECTED` 跑判定轮（`battery-run1.log`）**7/7 `[ALL-OK]` rc=0**；
基线自检看的是「`4 passed` 且 rc=0」而不是「红集合为空」——用例没被收集时也是 rc=0、红集合也是空的。
改完注释与 `@needs_pymysql` 装饰器后，在**最终字节**上复跑一轮判定轮（`battery-run3.log`）仍是 7/7 `[ALL-OK]` rc=0。
M6（放宽对照格断言）**实测红集合为空**，按机制登记为**等价变异**：对照格只在「对照本身会坏」的环境里承重，
本机驱动装得上、探针两次都真跑，所以它没有可观察后果；这条靶留在表里是为了换机器时它才有猎物。
E1/E2/E3 带 `@needs_pymysql`：没装 `mysql` extra 的环境（CI 只跑 SQLite）会**点名跳过**并写清缺谁，
不会报成错误，也不许静默恒通过。

**诚实的缺口**

1. **没有真的 PyMySQL 1.3 可装**（PyPI 上不存在），到期形状是「干净解释器里真删那个名字」造出来的模拟。
   忠实之处：坏的那一行就是 aiomysql 模块顶层那句 import，删除动作打在同一个模块对象上；
   不忠实之处：真 1.3.0 也许还会顺带删别的（我们只验了这一个名字），pip/uv 的解析行为也没重新走一遍。
   真装上那天要重量的清单写进 `pyproject.toml` 注释了。
2. **没在真 MySQL 上复跑到期形状**：这条判定是纯 import 期的，不需要数据库；
   但「换驱动（asyncmy）之后 bytes 转义对不对」是另一件事，本批没测、也没装 asyncmy。
3. `PYMYSQL_UPPER_BOUND` 只与 `pyproject` 的声明对账（E4），**不与 `uv.lock` 对账**——
   锁文件里的实际解析结果本批没读（`uv` 不在本机 venv 里，见工具链记忆）。
4. 上界到期这件事本身**不进 CI 的告警面**：CI 只跑 SQLite（`mysql` extra 没装），
   E1/E3 在有驱动的开发机上才是活的，CI 上会走 skip/不适用；哪天 CI 补上 MySQL job，这三条判定才真跑。

## 9. 复现命令

```bash
# 顶层/内层计数、turn_context 与 token_usage_record 形状
python - <<'PY'
import json, glob, collections, os
top=collections.Counter(); inner=collections.Counter(); seen={}
for f in glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True):
    for line in open(f, encoding="utf-8"):
        line=line.strip()
        if not line: continue
        rec=json.loads(line); top[rec.get("type","?")]+=1
        p=rec.get("payload") or rec
        if isinstance(p,dict) and p.get("type"): inner[p["type"]]+=1
        if rec.get("type") in ("turn_context","token_usage_record","compacted","world_state","session_meta"):
            seen.setdefault(rec["type"], rec)
print(dict(top)); print(dict(inner.most_common(14)))
for k,v in seen.items(): print(k, json.dumps(v, ensure_ascii=False)[:600])
PY

# 沙箱实物
ls ~/.codex/.sandbox ~/.codex/.sandbox-bin ~/.codex/.sandbox-secrets
cat ~/.codex/.sandbox/setup_marker.json
python -c "import json;d=json.load(open('C:/Users/22103/.codex/cap_sid',encoding='utf-8'));print(list(d), d['workspace'])"
grep -n "helper copy\|hide users" ~/.codex/.sandbox/sandbox.2026-09-18.log
```

我们的回归：

```bash
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_governance_snapshot_v16.py   # 12 passed
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_sandbox_audit_v16.py         # 7 passed
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_context_pairing_v14.py       # 4 passed
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_usage_levels_v17.py          # 10 passed
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_subagent_usage_v18.py        # 9 passed
services/api/.venv/Scripts/python.exe -m pytest -q services/api/tests/test_subagent_usage_v18.py services/api/tests/test_usage_levels_v17.py services/api/tests/test_worktree_parallel_v13.py services/api/tests/test_subagent_v12.py services/api/tests/test_context_layers_v12.py  # 48 passed
services/api/.venv/Scripts/python.exe -m pytest -q          # 全量 823 passed / 14 skipped
cd web && npm test                                          # 138 passed
cd web && node node_modules/typescript/bin/tsc --noEmit     # 无输出
python evals/governance_probe.py                            # 真实 HTTP 证据包 → E:\tmp\realmodel\

# 宿主执行边界的真实链路（:8061 上真跑一条命令；跑完自动把 shell 开关恢复原值）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe evals/boundary_probe.py            # 六项 PASS, rc=0
# 负对照：换任务号复用同一判定装置，必须变红
PROBE_TASK_ID=81d243d3-1bed-4e81-83db-5b02df70c5fd services/api/.venv/Scripts/python.exe evals/boundary_probe.py  # rc=1

# 压缩边界：把供应商窗口临时调到 2000 跑一条三步任务，跑完自动恢复原值
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe evals/compaction_probe.py           # 六项 PASS, rc=0
# 负对照一：没有发生过压缩的老任务 → 前四条变红
PROBE_TASK_ID=b24fb624-a36a-4c92-8a21-5375f2f38dfa services/api/.venv/Scripts/python.exe evals/compaction_probe.py  # rc=1
# 负对照二：修复前那条被供应商 400 拒掉的任务 → 只有"配对没被切散"这一条变红
PROBE_TASK_ID=cbd4ae9b-6709-460c-a85b-df530d9d50d8 services/api/.venv/Scripts/python.exe evals/compaction_probe.py   # rc=1

# 用量三级累计：新建一条三步任务真跑，判"每步一条事件 / 三级单调 / 事件数==表行数 / 缓存在命中"
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe evals/usage_probe.py                 # 七项 PASS, rc=0
# 负对照：三级记录上线前的老任务 → 六条 FAIL
PROBE_TASK_ID=042d5319-9668-4ad1-b449-ab02bd8c3359 services/api/.venv/Scripts/python.exe evals/usage_probe.py        # rc=1

# 改前基线（为什么"子代理不落账"不是推测）
git show HEAD:services/api/src/blank_api/services/subagent.py | grep -c "ModelUsage\|model.usage"    # 必须是 0

# 子代理进账：在 :8061 上真跑一条"必须派子代理去读文件"的任务
# 判"真的派了子代理 / 有 depth>=1 的 model.usage / 父子各有 trace_id / 线上出现 turn != task / 行数==事件数"
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe evals/subagent_usage_probe.py                             # 八项 PASS, rc=0
# 负对照：#33 之前跑出来的老任务（4 条用量事件、0 条子代理）→ 五条 FAIL
PROBE_TASK_ID=0aae1a61-12da-4a65-9b8c-11aa48f6d4f0 services/api/.venv/Scripts/python.exe evals/subagent_usage_probe.py # rc=1

# 环境快照这一批（#41）：先量改造前那条事件有多大，再验改完的
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/world_probe.py                       # 泄漏实物：单条 turn.context 4145 字节
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/world_state_verify.py                 # 真 DeepSeek 一条任务，W0–W11 全 PASS
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/world_ui_verify.mjs                                             # 真 Chrome 6/6 PASS（视口 1440×900）

# 压缩窗口链这一批（#42）：离线 21 条 → 变异门禁 → 真链路 → 真浏览器
services/api/.venv/Scripts/python.exe -X utf8 -m pytest -q services/api/tests/test_compaction_window_v23.py          # 10 passed
E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/window.test.ts"  # 10 passed（在 web/ 下）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_mutate.py                     # 后端变异门禁 10/10
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_mutate_web.py                  # 前端变异门禁 7/7
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_chain_probe.py                 # 真 DeepSeek；#42 时 C1–C9，#44 起同一脚本扩到 C1–C14
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/window_ui_verify.mjs                                            # 真 Chrome：#42 时 7/7，#44 起 9/9（视口 1440×900）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/find_compacted_tasks.mjs                                        # 挑 A-B 对照任务：哪些任务有压缩事件、是否带窗链

# 回合归属这一批（#43）：离线 24 条 → 变异门禁 → 真链路三桩 → 真浏览器
services/api/.venv/Scripts/python.exe -X utf8 -m pytest -q services/api/tests/test_turn_attribution_v24.py            # 15 passed
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/attribution.test.ts"   # 9 passed（在 web/ 下）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/turn_mutate.py                        # 后端变异门禁 13/13
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/turn_mutate_web.py                    # 前端变异门禁 11/11
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/turn_chain_probe.py                   # 真 DeepSeek C1–C10 + legacy 对照，22/22 PASS, rc=0
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/turn_ui_verify.mjs                                              # 真 Chrome 8/8 PASS（视口 1440×900；U4/U5 第一轮因 "task:e7c" 这种砍位读法变红）

# 逐条出处与跨窗身份这一批（#44）：离线 34 条 → 变异门禁 → 真链路 14 条 → 真浏览器 9 条
services/api/.venv/Scripts/python.exe -X utf8 -m pytest -q services/api/tests/test_retention_v25.py services/api/tests/test_compaction_window_v23.py   # 23 passed
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/window.test.ts"                        # 11 passed（在 web/ 下）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/retention_mutate.py                    # 后端变异门禁 15/15
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/retention_mutate_web.py                # 前端变异门禁 9/9
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/window_chain_probe.py                  # C1–C14 全 PASS, rc=0（任务 5016f7e4-…）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/window_ui_verify.mjs                                             # 真 Chrome 9/9 PASS（V8 逐行重算 26 行、V9 对 55578136-… 负对照）

# 并行批次与委派预算这一批（#45）：离线 23 条 → 变异门禁 → 真链路 8 条 → 真浏览器 7 条
services/api/.venv/Scripts/python.exe -X utf8 -m pytest -q services/api/tests/test_parallel_budget_v26.py services/api/tests/test_subagent_usage_v18.py   # 25 passed
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/components/tasks/parallel.test.ts"                          # 7 passed（在 web/ 下）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/parallel_mutate.py                        # 后端变异门禁 21/21（含复合变异 B7+：两道闸同时拆才红）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/parallel_mutate_web.py                    # 前端变异门禁 9/9
BLANK_TOKEN_BUDGET=20000 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py start on                     # 预算闸要响，限额必须低于一趟 fan_out 的实测合计
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/parallel_chain_probe.py                   # 真 DeepSeek P1–P8 全 PASS, rc=0（任务 037b4252-…；60000/30000 两档跑不出预算事件，见 §7.10 红 1）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/parallel_ui_verify.mjs                                              # 真 Chrome 7/7 PASS（视口 1440×900；TASK_NEW 传探针打印的新任务号）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/parallel_dom_peek.mjs <taskId>                                      # 判据改前先看 DOM 真形状（U4 那句整句就是这么量出来的）
services/api/.venv/Scripts/python.exe -X utf8 -m pytest -q                                                                 # 全量 839 passed / 14 skipped, rc=0
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/**/*.test.ts"                # 146 passed（#46 起，多出的一条是团队链轮 id）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit                            # 无输出

# 团队链三级真跑这一批（#46）：离线 → 变异门禁 → 真链路 → 真浏览器 → 反证（改动只有一处前端读法，后端未动）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs src/components/tasks/attribution.test.ts   # 10 passed（在 web/ 下）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_label_mutate_web.py                  # 前端变异门禁 7/7（红集合与期望精确相等；M6 撤 attempt 分支会红两条，见 §7.11）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py start on                                              # :8062；真花钱，一轮三人发言实测 101,172 token
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_chain_probe.py                       # 真 DeepSeek T0–T9（首轮 T6 因装置按 bot 名建字典而误报，见 §7.11 红 1）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_chain_recheck.py                     # 只读重算 R1–R4：不花钱复核群账，并量出"四列里带 team- 的行数 = 0"
API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 E:/env/node/node-v24.20.0-win-x64/node.exe web/node_modules/next/dist/bin/next dev -p 3123 &   # 前端 dev，代理到 :8062
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/team_chain_dom_peek.mjs                                              # 先量 DOM 形状（U3 那个分号就是这么栽的）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m1b/team_chain_ui_verify.mjs                                             # 真 Chrome 10/10 PASS（1440×900；期望全由接口现算）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/team_chain_ui_negative.py                 # 反证：真源码改坏 → 指定 U6/U7/U9 红、其余七条绿 → 还原 10/10 且 sha256 一致

# 审阅轨迹这一批（#79）：离线 16 条 → 变异电池 → 真链路 13 格 → 同一格判据一红一绿
cd services/api && .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_guardian_trail_v51.py tests/test_guardian_v12.py   # 25 passed
.venv/Scripts/python.exe -X utf8 E:/tmp/m79/battery.py                                                                     # 变异电池 17/17 按实测红集合命中、16 条全部可失败，[ALL-OK] rc=0（期望来自实测轮 battery-run4.txt）
bash E:/tmp/realmodel/restart-8061.sh &                                                                                    # 改过后端代码必须先重启隔离实例，再点验
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m79/trail_probe.py                                           # 真 DeepSeek R0–R6c + N1：13 格 FAIL 0 / NOT-TESTED 0, rc=0（供应商窗口临时 3000，finally 里还原并实测 65536）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m79/r6c_replay.py                                            # 不花钱复读两份实物：299c729f=FAIL（旧内存副本）、41c82847=PASS（事件投影），[ALL-OK] rc=0
```

# 边界/权限/审阅轨迹进界面这一批（#80）：离线 → 判定变异 → 替身自检 → 双路点验 → 渲染变异 → 反证
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs src/components/tasks/boundaries.test.ts   # 30 passed（三态/降级/归账未归属/缺键不编话）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/**/*.test.ts"                     # 183 passed / 0 fail（E:/tmp/m80/green5.txt）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit -p tsconfig.json                # 无输出 rc=0（E:/tmp/m80/tsc6.txt）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m80/battery.mjs                                                             # 判定变异电池 22/22（battery-run2.txt；起跑前一条「未变异 rc=0」自检）
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m80/stub2.mjs                                                               # 替身 :8063：只改 guardian 三态 + 两处群名来源（/team/bootstrap 与 /state），其余逐字节照抄真物
E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m80/stub-selfcheck.mjs                                                      # 替身自检 8/8（stub-selfcheck-2.log）
API_PROXY_TARGET=http://127.0.0.1:8063 BLANK_WEB_DIST_DIR=.next-m80 E:/env/node/node-v24.20.0-win-x64/node.exe web/node_modules/next/dist/bin/next dev -p 3123 &
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --import ./test-bootstrap.mjs E:/tmp/m80/ui-verify.mjs                     # 真 Chrome 44/44 rc=0，视口 1440×900（ui-verify-4.log）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --import ./test-bootstrap.mjs E:/tmp/m80/ui-verify-team.mjs                # 群聊 16/16 rc=0（ui-verify-team-7.log；期望全由 /state 载荷现算）
cd web && REAL_ONLY=1 E:/env/node/node-v24.20.0-win-x64/node.exe --import ./test-bootstrap.mjs E:/tmp/m80/ui-verify.mjs         # 反证：不经替身直通 :8061，38/38 rc=0（ui-verify-real-1.log）
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe E:/tmp/m80/ui-mutate.mjs                                                 # 渲染变异 8/8 打红（ui-mutate-2.log）；两条 suite 基线先各绿、逐字节还原
```

# 驱动上界到期这一批（#81）：先探实测红集合，再按实测钉期望
cd services/api && .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_mysql_driver_expiry_v81.py tests/test_mysql_driver_compat_v31.py   # 7 passed（E1–E4 + 旧兼容三条）
.venv/Scripts/python.exe -X utf8 E:/tmp/m81/battery.py --probe                                                                 # 探针轮：打出 7 条靶各自的实测红集合（battery-probe-2.log）
.venv/Scripts/python.exe -X utf8 E:/tmp/m81/battery.py                                                                         # 判定轮：7/7 按实测命中、M6 登记等价、[ALL-OK] rc=0（battery-run1.log）
.venv/Scripts/python.exe -X utf8 -m pytest -q                                                                                  # 全量 1129 passed / 23 skipped, rc=0（regression-81.log）
# 上界到没到期问 PyPI 本身（不需要装任何东西）：
.venv/Scripts/python.exe -X utf8 -c "import json,urllib.request as u; [print(p, json.load(u.urlopen('https://pypi.org/pypi/%s/json'%p))['info']['version']) for p in ('PyMySQL','aiomysql')]"
**电池期间源码归电池独占**：上面两条 `ui-verify*.mjs` 不许与 `ui-mutate.mjs` 并行——
本批实际并行过一次，那份没有汇总行的日志改名成 `ui-verify-5.ABANDONED-contaminated-by-mutate.log` 留档、不作数（见 §7.41）。

浏览器点验（chip）是一次性脚本，放在仓库外：`E:\tmp\realmodel\chip-verify.mjs`，
先起 `BLANK_WEB_DIST_DIR=.next-verify API_PROXY_TARGET=http://127.0.0.1:8061 next dev -p 3120`，
再 `node E:/tmp/realmodel/chip-verify.mjs`；任务号可用 `HOST_TASK_ID` / `READONLY_TASK_ID` 覆盖。

压缩卡片的点验同族：`E:\tmp\realmodel\boundary-card-verify.mjs`（默认打 `:3121` 那个 dev 实例，
卡片数、水位行、压缩前后 token 都从 `/api/v1/tasks/{id}/events` 现取再与 DOM 对比），
`TASK_ID` / `CONTROL_TASK_ID` / `WEB_BASE` 可覆盖；取证通道断了（任务上没有压缩事件）时脚本自己先红。

用量面板的点验同族：`E:\tmp\realmodel\usage-row-verify.mjs`（DOM click 切到右侧「证明」面板，
缓存命中/三级口径/推理三行的期望值都从 `/api/v1/tasks/{id}/evidence` 现取再与 DOM 对比，
`TASK_ID` / `CONTROL_TASK_ID` 可覆盖）。配套变异装置：
`E:\tmp\realmodel\mutate_usage.py`（后端六条）、`mutate_usage_fe.py`（前端五条）、
`mutate_usage_browser.py`（点验装置自检两条）——三个脚本每次只破坏一处、跑完立即还原原文。

子代理进账这一批（#33）同族装置：
`E:\tmp\realmodel\mutate_subagent_usage.py`（八条变异 M1–M8，跑之前与还原之后各跑一次
`test_subagent_usage_v18.py` 做 baseline / post-restore 自检，两个 `rc=0` 才认这八条），
`E:\tmp\realmodel\subagent-panel-verify.mjs`（真 Chrome headless、视口 1440×900、dev 实例 `:3121`，
默认 `TASK_ID=bc6c26f6-a363-…`、`CONTROL_TASK_ID=0aae1a61-…`；
"本回合 ≠ 任务累计""任务累计 == 面板合计""调用次数 == 接口事件数且其中 3 条 `depth>=1`"
三条是这一批的主张，期望值全部从 `evidence`/`events` 现取，取证通道断了脚本自己先红）。
截图：`E:\tmp\realmodel\subagent-panel-{main,control}.png`。

环境快照这一批（#41）同族装置：`E:\tmp\m1b\world_mutate.py`（后端十条 M1–M10，每轮只破坏一处、
还原后按 sha256 与备份逐字节比对，最后再跑一次全绿才算这十条有效）、
`E:\tmp\m1b\world_mutate_web.py`（前端五条 F1–F5，同样带 baseline / post-restore 自检）、
`E:\tmp\m1b\world_ui_verify.mjs`（真 Chrome headless、视口 1440×900、dev 实例 `:3123` 指向 `:8062`；
六条判据的期望值全部从 `/api/v1/tasks/{id}/evidence` 现取，V2 是装置自检——页签没点开、
卡片没渲染，就一律算 FAIL 而不是"没读到所以通过"）。日志：
`E:\tmp\m1b\world-mutate-4.log`、`world-mutate-web-2.log`、`world-verify-2.log`、`world-ui-1.log`。
