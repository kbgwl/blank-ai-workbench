# v163 Super Agent 完整闭环验收报告

任务书：`docs/workbuddy-v163-super-agent-completion.md`
目标版本：`v0.9.1 Super Agent Completion`
工作目录：`E:\projects\kbgai\kbgai`

## 结论先说（一句话）

后端能力（Router/Worker/Trigger/Watchdog、暂停恢复重试、低打扰、四行交付、结构化状态、
零改动伪成功拦截、coding-035 口径）已全部落地并有单测 + 集成测试 + 真实 Benchmark 证据；
真实 Electron GUI 18 项**已实跑 17 项通过、1 项未通过**（§八），验收过程中发现 4 个真缺陷，
其中 2 个当场修掉（审批 TTL 卡死、代理导致 ProxyError）。按 §9，2.7 未通过即不满足发布条件，
故**版本号已升到 `0.9.1` 但未打 tag、未重打包安装**。

## 一、版本与提交

| 项 | 值 |
| --- | --- |
| 当前版本号 | `0.9.1`（用于 §2 的 GUI 验收，见下条） |
| 是否发布 | **否**：未打 tag、未重打包安装 |
| Tag | 无新增（既有 `v0.9.0` 保持指向 `c8276e7`） |
| 未发布原因 | 任务书 §9 要求 GUI 18 项全部有证据；本轮 17/18，2.7 未通过（§八） |
| Commit | `e7679d9`（能力实现）/ `12faf03`（55 项结果）/ `4b4cdc3`（口径标注）/ `c9f5814`（GUI 验收 + 两个缺陷修复） |
| 代码分支 | `master` |

> 版本号为什么先升：任务书 §2 明写「使用 v0.9.1 最新构建版或安装版，不得只使用旧版本窗口」，
> 不升版本就无法按要求取证。升号 ≠ 发布，tag 与安装包才是发布动作，这两步没做。

## 二、修改文件

24 个文件，`+2866 / −706`（含 55 项正式结果 JSON 与两份文档；删除行主要来自
`last-run.json` 整份替换）。

新增（5）：`services/api/src/blank_api/services/agent/{notify,delivery}.py`、
`services/api/tests/test_v163_{super_agent,integration}.py`、`docs/workbuddy-v163-super-agent-completion.md`

改动（19）：`README.md`、`evals/benchmarks/coding/{run.mjs,last-run.json}`、
`evals/benchmarks/coding/fixtures/coding-035/{profiles,test_profiles}.py`、
`evals/lib/judgement.mjs`、`evals/tests/judgement.test.mjs`、
`services/api/src/blank_api/api/routes/{tasks,settings}.py`、
`services/api/src/blank_api/schemas/settings.py`、
`services/api/src/blank_api/services/{settings.py,agent/{events,evidence,graph,queue,runtime,watchdog}.py}`、
`services/api/tests/test_watchdog_v162.py`、`docs/workbuddy-v163-acceptance-report.md`

## 三、coding-035：改前 / 改后

### 改前（`E:\tmp\blank-eval-8061\bench-55-nim.json`，v162 全量跑）

```
status        = succeeded        ← 任务自述成功
changedFiles  = []               ← 一个文件都没改
testExit      = 0
testTail      = ""               ← 测试函数从来没被调用过
passed        = False            ← 只靠 expectedFilesTouched 才被判失败
checks        = {statusMatch:true, testPassed:true, expectedFilesTouched:false, ...}
```

根因有两层，缺一不可：

1. **fixture 没有真 bug**：`rename()` 原本就是 `{k.upper(): v}`，注解 `dict[str, str]`
   本来就成立，"修实现"这个任务目标不存在。
2. **测试从未执行**：任务 `testCommand` 是 `python -X utf8 test_profiles.py`，
   而 `test_profiles.py` 里只有 `def test_bench()`，**没有 `__main__` 入口**。
   直接执行等于什么都没跑，退出码恒为 0 —— 一个永远"通过"的测试比没有测试更糟。

### 改后

fixture（`evals/benchmarks/coding/fixtures/coding-035/`）：

- `profiles.py`：`return {index: value for index, (key, value) in enumerate(mapping.items())}`
  —— 真的把 key 写成了 int，与 `dict[str, str]` 注解冲突，必须改实现（不许改注解）。
- `test_profiles.py`：补 `if __name__ == "__main__": test_bench()`，并断言 key 全为 `str`。
  改前 `python test_profiles.py` 退出 1、正解退出 0、错误修法（`{str(i): v}`）仍退出 1。

判定器（`evals/lib/judgement.mjs` + `evals/benchmarks/coding/run.mjs`）：

- 新增 `detectZeroChangeSuccess({status, expectedFiles, changedFiles})`：
  `succeeded` + `expectedFiles` 非空 + `changedFiles` 为空 → 判伪成功。
- `agentTouched` 先剔除 `testSideEffectFiles`（跑测试留下的 `.db` 不能洗白"零改动"）。
- summary 新增 `zeroChangeSuccessCount`，进质量统计。

### 改后实证（真实模型，单任务复跑两次）

`E:\tmp\blank-eval-8061\probe-035-v163.json` / `probe-035-v163b.json`：

```
status        = succeeded
changedFiles  = ['profiles.py']
testExit      = 0
testTail      = "ok"             ← 测试真的执行并打印了 ok
passed        = True
checks        = 全部 true（含 noZeroChangeFalseSuccess）
```

交付文本（真实运行，`task.result_summary`）：

```
完成：rename 返回的 key 改为 str（原 key 大写），与 dict[str, str] 注解一致
改动：1 个文件、1 处 Diff
验证：…\.venv\Scripts\python.exe -m pytest -q（退出码 0）
```

## 四、55 项 Benchmark（§8）

- 运行方式：独立评测后端 8061（`BLANK_DATABASE_URL` 指向 `E:\tmp\blank-eval-8061\data\blank_eval.db`），
  provider `b33715d7-5e88-49f1-b2d5-9506190b9788`（NVIDIA NIM `mistralai/mistral-large-4-1t-a52b`）。
- 运行时段：2026-11-04 18:25 → 20:27（+0800），55 项全部跑完，`--pause 5`。
- 正式结果 JSON：`evals/benchmarks/coding/last-run.json`（同一份副本：`E:\tmp\blank-eval-8061\bench-55-v163.json`，
  原始日志 `E:\tmp\blank-eval-8061\bench-55-v163.log`）。逐项结果见文末附表。

### 门槛逐条核对

| 门槛 | 要求 | 实测 | 结论 |
| --- | --- | --- | --- |
| completionRate | ≥ 90% | **91%**（50/55；另 5 项为 cancel_resume，期望就是 cancelled） | 通过 |
| passRate | ≥ 90% | **100%**（55/55） | 通过 |
| testPassRate | = 100% | **100%** | 通过 |
| misEditedFiles | = 0 | **0** | 通过 |
| falseDoneCount | = 0 | **0** | 通过 |
| falseClaimCount | = 0 | **0** | 通过 |
| unauthorizedHighRisk | = 0 | **0** | 通过 |
| totalTokens | > 0 | **3,030,581** | 通过 |
| coding-035 | passed | **passed**（`profiles.py`、退出码 0、`testTail="ok"`） | 通过 |
| zeroChangeSuccessCount（本轮新增） | = 0 | **0** | 通过 |

其它指标：`providerFailed=0`、`unknownUsageTasks=0`、`totalSteps=270`、`avgElapsedMs=113,203`、
`avgTokens=55,101`、`p95Tokens=139,231`。55 项的实际状态与期望状态**全部一致**
（50 succeeded + 5 cancelled）。

关于 `completionRate=91%` 与 `passRate=100%` 的差别：前者只把 `succeeded` 算作"完成"，
后 5 项（`cancel_resume` 类）的期望状态就是 `cancelled`，判分器按期望状态判定为 PASS。
这不是"5 项失败"，而是口径不同；`judgedCompletionRate` 同样为 91%。

## 五、后台运行能力（§4）

| 能力 | 落地位置 | 证据类型 |
| --- | --- | --- |
| Router 不可变快照（intent/scenario/priority/model_policy/tool_policy/notify_policy/memory_policy/steps/budget/approval_required/snapshot_id） | `services/agent/dispatch.py`（v162 已实现） | 单测 `test_dispatch_v162.py`、集成 `test_dispatch_runtime_v162.py` |
| 快照不可事后放宽（`from_dict` 收紧、`guard()` 不变量、`approval_required` 恒 True） | 同上 | `test_dispatch_v162.py` + 集成断言 `snapshot["approval_required"] is True` |
| Worker 领取/租约/心跳/检查点/幂等/崩溃恢复 | `services/agent/queue.py`、`worker.py`、`runtime.py` | `test_run_lease_v102.py`(8) / `test_worker_recovery_v152.py`(7) / `test_crash_replay_v90.py`(4) |
| 暂停 / 恢复 / 重试 | `api/routes/tasks.py`（新增 3 端点）+ `graph.py`（`current_status` 区分 paused/cancelled）+ `events.py`（`has_terminal(paused_counts=)`） | 单测 3 例 + 集成 |
| Trigger 五类事件 + 去重 + 过期不补跑 + 空 goal 不建任务 | `services/agent/triggers.py`（v162） | `test_triggers_v162.py`(15) |
| Watchdog 六类监控 + 动作分级（只记/通知/建议，不改状态） | `services/agent/watchdog.py`（本轮补耗时预算） | `test_watchdog_v162.py`(15，含新增 2 例) |
| 后台安全上限（时长/步数/token/恢复次数，全部夹硬上限） | `services/settings.py`（`background` 段）+ `api/routes/settings.py` | 单测 4 例 |

## 六、低打扰与四行交付（§5）

- 通知门：`services/agent/notify.py`。四策略（silent/milestone/approval_only/always）+ 免打扰时段，
  **审批永远放行**（silent 下也放行，否则任务停到天亮）。判定与投递分离，判定写成
  `notify.decided` 事件，`/tasks/{id}/status` 暴露 `notify.policy` 与最近一次判定。
- 交付格式：`services/agent/delivery.py`（纯函数）+ `graph.py` 收尾接线。
  四行全部由**事实**拼：结论取模型段落首句、改动数取工具结果、退出码取校验记录、
  需要你取「下一步」首条。`验证` 行只在真的跑过且退出码已知时出现；模型那句「已验证」在这里不起作用。
  没有改动也没跑校验的交付（只读分析）**不进四行格式**——压成一句等于把答案截掉。
- 设置项：`background`（enabled / notify_policy / quiet_hours / max_seconds / max_steps /
  token_budget / auto_recoveries / default_model_policy），全部经 `_clamp` 夹硬上限，
  跨零点免打扰时段合法。

真实交付样例见 §三「改后实证」。

## 七、可观测性（§6）

`GET /api/v1/tasks/{task_id}/status` 返回结构化事实：

```
task_id / run_id / trigger_id / worker_id / snapshot_id / status / current_step /
last_heartbeat / last_event{type,node,at} / model / tokens{total,calls} /
approval{tool,waiting_since,risk_level} / changes{count,paths,diff_fingerprint} /
delivery{present,summary} / dispatch{intent,scenario,notify_policy,memory_policy} /
notify{policy,last}
```

本轮修掉两个观测缺陷：

1. 审批状态原来只查 `ToolCall.status == awaiting_approval`，而图在工具网关之前 `interrupt()`
   时**不会落 ToolCall 行**，导致"正在等审批"的任务显示成"没有审批"。改为回退到最近一次
   `approval.required` 事件，与 `build_evidence` 同口径。
2. `finish_run` 原来在写终态时把 `worker_id` 清空，事后无法回答"这条任务是谁跑完的"。
   改为只释放租约（`lease_expires_at = NULL`），保留 `worker_id` 作为审计痕迹。

前端仍读 `live` 结构化状态与事件流，不靠"最后一条文本"猜状态；把 `/status` 接进任务面板属于
GUI 侧工作，本轮未验证（见 §八）。

## 八、Electron GUI 18 项（§2）—— **实跑，17 项通过 / 1 项未通过**

### 方法与边界

先前把这一节判成「headless 不可代劳」是错的：本机有真实桌面会话，Electron 能起窗口，也能用
Chrome DevTools Protocol 驱动它。本轮的做法是：

- `electron .` 以 `--remote-debugging-port=9222 --user-data-dir=E:\tmp\blank-gui-profile` 启动，
  连**评测后端 8061**（独立库），不碰日常 8000；
- 所有点击走 `Input.dispatchMouseEvent`（真实坐标）、输入走 `Input.insertText`、原生 `<select>`
  走 `Home/ArrowDown/Enter` 键盘序列——不用 `el.value = x` 那种绕开用户路径的写法；
- 证据三件套：窗口截图（`E:\tmp\blank-eval-8061\gui-*.png`）+ 后端事件流（`/tasks/{id}/events`）
  + 磁盘真实变化（文件内容 / md5）。

限制说明：截图与点击由脚本驱动，不是人手点击。**审批卡片的点击是真的鼠标事件**（后端留下
`approval.decided` / `approval.granted` 事件），但「人眼看到的动画、hover 态」没有验证。

### 逐项结果

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 2.1 启动 / health / 首页 / 版本 / 重启 | **通过** | `gui-2.1-home.png`；`[startup] startup \| complete`；health `{"status":"ok"}`；`desktop/package.json` = `0.9.1`；关窗后重启 CDP 1s 就绪 |
| 2.2 开发任务闭环 8 步 | **通过 8/8** | 见下表 |
| 2.3 双 provider 模型切换 | **通过** | 任务 `0e56e0cd`：`model=mistralai/mistral-large-4-1t-a52b`、`provider=bench-nim-…`；任务 `15ad426a`：`model=glm-4.5-flash`、`provider=bench-bigmodel-glm45flash`；usage 7648+441=8089 tokens；上游 `integrate.api.nvidia.com` ↔ `open.bigmodel.cn/api/anthropic` |
| 2.4 五场景切换 | **通过（4 项符合，1 项与任务书有出入）** | dev/docs/daily/brainstorm/reverse 各真跑一轮，见下 |
| 2.5 审批卡片真实点击 | **通过** | 3 张卡真实点击，事件 `approval.decided{approve:true,scope:once}` → `approval.granted` → `tool.completed`；`gui-2.5-approval-card.png` |
| 2.6 日常记忆「记住 / 不记住」 | **通过** | 见下 |
| 2.7 关窗后继续 + 重开可见 | **未通过（半项）** | Worker 继续 ✅；重开窗口看不到该任务 ❌ |

#### 2.2 八步明细

| 步 | 要求 | 实测 |
| --- | --- | --- |
| 1 | 新建对话 + 选开发场景 | 点「新对话」、`select[2]` 键盘切到「开发」 |
| 2 | 选已配置模型 | 键盘选 `bench-nim-mistral-large-4-1t` |
| 3 | 输入「读取当前工作区并说明入口文件」 | 原话进输入框（`gui-2.2-typed.png`） |
| 4 | 真实调用文件读取工具 | 事件：`list_files` ×1 + `read_file` ×5，共 6 次 `tool.completed` |
| 5 | 输入代码修改任务 | 「把 config.py 里的 MAX_RETRY 从 9 改成 11…」 |
| 6 | 文件真实变化 + 查看 Diff | 磁盘 `MAX_RETRY = 11`；右侧面板 Diff `-MAX_RETRY = 9 / +MAX_RETRY = 11`（`gui-2.2-diff.png`） |
| 7 | 运行测试并查看退出码 | `run_shell python -m pytest -q` 真实执行，退出码 **0**；`run_tests` 卡另有一次 |
| 8 | 交付只含完成/改动/验证/需要你 | 交付原文三行（无待办则整行省略）：`完成：MAX_RETRY 已改为 11，测试 2 个全通过` / `改动：1 个文件、1 处 Diff` / `验证：…python.exe -m pytest -q（退出码 0）`；`gui-2.2-delivery.png` |

#### 2.4 五场景

| 场景 | 实测 | 与任务书比对 |
| --- | --- | --- |
| 开发 | 读写 + 审批 + 改文件，全链路通过 | ✅ |
| 文档 | `intent=answer`、`tools=[]`，直接成文 | ⚠️ 任务书要求「文档默认只读」，代码里 `DOCS.allow_write=True`（写 .md 需要它）。二者冲突，未擅自改 |
| 日常 | 让它改 `config.py` → `tools=[]`、文件仍是 11（写工具确实被禁） | ✅ 写被禁；⚠️ 但它答的是上一轮的 `apply_discount`，没解释「日常场景不改文件」 |
| 头脑风暴 | `tools=[read_file]`，只读发散，未改文件 | ✅ |
| 逆向实验室 | `tools=[read_file]`，只读分析 | ⚠️ 「隔离工作区」未单独体现：`sandbox.tier=workspace / isolation=host`，与开发场景同档 |

#### 2.6 日常记忆

| 开关 | 实测 |
| --- | --- |
| 记住习惯 | `memory_write` 被调用 3 次（前两次参数错：缺 `type`、`type=habit` 不在枚举内，第三次 `type=user` 成功）；磁盘 `services/api/data/memory/work-006857a0.md` 新增 `- [user] 用户习惯用 pytest 跑测试，不用 unittest` |
| 不记住 | 同一请求（含地址）→ `tool.requested` 为空、任务 succeeded、记忆文件 md5 `4b1e1160…` **前后完全一致** |

顺带发现：该记忆文件里除 `[user]` 条目外，还有各场景任务结果的自动固化条目（文档/头脑风暴/逆向的
结果都写了进去）。工具层面的「用户习惯」确实只开放给日常，但「任务结果固化」不分场景——这条边界
比任务书描述更松，如实记录。

### 2.7 为什么算未通过

**做到了的**：窗口跑任务途中被 `Stop-Process` 强制结束（不是隐藏），50 秒后后端事件从 seq 31 涨到 77、
工作区文件从 2 个增到 4 个——**Worker 完全不受窗口影响**，任务继续执行。

**没做到的**：重开窗口后，会话列表里找不到那条正在跑的任务（`title=在工作区新建 5 个文件…`、
`status=waiting_approval`），`GET /tasks/{id}` 里 `conversation_id=None`。也就是说窗口关闭那一刻，
这一轮没有落成一条可回到的会话记录，用户只能看到「它曾经跑过」，看不到「它现在卡在哪、要不要批」。
这条任务最后卡在审批上，直到审批 TTL 到期被 `expire_stale_approvals` 收敛成 failed。

根因：会话（conversation）与任务（task）是两条写入路径，关窗时 task 已落库、conversation 还没。
修法是把 conversation 的创建提前到发送瞬间（或让任务列表按 `project_id + status` 兜底展示孤儿任务），
这属于前端改动 + 需要重新静态导出，本轮未做。

### 验收过程中发现并修掉的缺陷

**① 审批卡 5 分钟过期，过期后任务永久卡死（已修）**

真实撞了三次：21:49 挂起、22:06 点 → 409「审批请求已过期」，而任务状态仍是 `waiting_approval`，
界面显示「进行中」，既批不了也没有重试入口。第三次是脚本点得快也没用——TTL 就是 5 分钟。

修法两处：

- `graph.approval_ttl_seconds()`：TTL 改为 `BLANK_APPROVAL_TTL_SECONDS` 可配，默认 **30 分钟**，
  夹在 60 秒 ~ 24 小时。理由写在函数 docstring 里：5 分钟对「先去确认一下再回来」不够。
- `worker.expire_stale_approvals()`：每轮 `run_once` 开头把「审批已过期超过 60 秒宽限」的
  `waiting_approval` 任务收敛成 `failed`（`failure_reason` 写明原因并提示「点重试」），
  并写 `approval.expired` + `task.failed` 事件。界面因此从「永远进行中」变成「失败 + 重试」。

配 3 个单测：TTL 默认/夹取/坏值回默认；只收敛过期的那一条（未过期的必须不动）；总开关关掉时不动。
修复后同一套 GUI 流程连跑 3 次审批，全部点击成功（`approval.decided` → `running`）。

**② 后端继承宿主代理，NVIDIA 端点间歇 `ProxyError`（已修）**

`start-8061.sh` 从 shell 继承了 `https_proxy=127.0.0.1:58288`，httpx 经它访问
`integrate.api.nvidia.com` 间歇失败，而 `curl` 直连和经代理都是 200。修法是启动脚本里
`unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy`，后端直连。
修复后同一 provider 连续跑通文档 / 逆向 / 头脑风暴三个场景。

### 验收中发现但**未修**的缺陷

| # | 缺陷 | 影响 | 为什么没修 |
| --- | --- | --- | --- |
| 1 | 关窗后正在跑的任务在 GUI 里消失（`conversation_id=None`） | 用户回不到那条任务，卡在审批也没法批（§2.7 未通过的根因） | 前端会话落库时机 + 列表兜底，改动需重新静态导出并重跑 GUI 取证 |
| 2 | 日常场景收到「改文件」请求时答非所问（回了上一轮的 `apply_discount`） | 用户不知道是「场景不允许」还是「模型跑偏」 | 属提示词/意图层调优，改动会影响全部场景的判定，需单独评测 |
| 3 | 文档场景 `allow_write=True`，与任务书「文档默认只读」不一致 | 文档场景能改任意文件 | 任务书要求与「文档场景要产出 .md」直接冲突，需产品决策：要么加文件类型级权限，要么改任务书 |
| 4 | 逆向实验室没有独立工作区（`isolation=host`，与开发同档） | 「隔离工具和工作区」只体现在提示词，没有系统级隔离 | 沙箱档位是全局设置，按场景切档会与「同一条任务只能有一个沙箱结论」冲突 |


## 九、自动化回归（本轮实跑）

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `.venv/Scripts/python.exe -m pytest -q` | **2121 passed / 23 skipped / 0 failed**（13m08s，含审批 TTL 修复后的 3 例新增） |
| 前端单测 | `node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"` | **262 / 262** |
| 前端 lint | `eslint src` | exit 0 |
| 前端类型 | `next typegen` + `tsc --noEmit` | exit 0 |
| 桌面单测 | `node --test desktop/tests/*.test.cjs`（5 个文件） | **57 / 57** |
| 评测单测 | `node --test evals/tests/judgement.test.mjs` | **21 / 21**（新增 4 例零改动判定） |
| CLI | `node --test` + `node --check src/index.mjs` | **6 / 6** + build 通过 |
| 版本口径 | `node scripts/check_versions.mjs` | 一致（发布版本 0.9.1） |
| 空白错误 | `git diff --check` | clean |

本轮新增测试 33 例：`test_v163_super_agent.py`（27，含审批 TTL 3 例）、`test_v163_integration.py`（3）、
`test_watchdog_v162.py`（+2）、`evals/tests/judgement.test.mjs`（+4，含零改动判定）。

> 说明：跑分与全量回归并行时曾出现 1 次 `test_triggers_v162::test_worker_releases_due_trigger_once`
> 的 SQLite 争用 error；单跑该文件 15/15 通过，跑分结束后独占全量复跑 0 error。
> 该次异常计为环境争用，不计为回归，但如实记录在此。

## 十、npm 安装 / doctor / start / health

在干净前缀安装验证（`E:\tmp\blank-cli-clean`）：

```
npm pack            → blank-ai-cli-0.1.0.tgz（3 files）
npm i -g --prefix … → added 1 package in 1s
blank-ai --help     → 用法输出正常
blank-ai doctor     → Node.js v24.20.0 / 平台 win32 / 配置 C:\Users\22103\.blank
blank-ai start      → 找到 services/api/.venv 的 Python，绑定 127.0.0.1:8000 时
                      发现端口已被占用（Errno 10048），给出提示并干净退出（code 3）
```

`start` 未能真正起服务：本机 8000 端口已有另一个后端在跑。CLI 的失败处理是可读的，不是崩溃。

## 十一、未完成项

1. **§2.7 关窗后重开可见** 未通过：Worker 继续跑（已证），但重开的窗口里找不到那条任务（§八有根因）。
2. `/tasks/{id}/status` 尚未接进 GUI 任务面板（后端已就绪，前端未接）。
3. 验收中发现的 3 个未修缺陷：关窗任务不可见、日常场景答非所问、文档场景可写（§八表格）。
4. `blank-ai start` 未能端到端起服务（本机 8000 端口已被日常后端占用），只验证到「正确报错并退出」。
5. 逆向实验室未做系统级隔离（`isolation=host`）。
6. 未重打包安装版：按 §9，GUI 未全通过就不发布，`D:\Softwares\kbg\...` 仍是 0.9.0 构建。

## 十二、最终工作区状态

- `git status` 干净；HEAD 见文末「提交记录」。
- 版本口径：桌面/根/README 均为 `0.9.1`（`check_versions.mjs` 通过），但 **未打 tag、未发布**。
- 评测后端仍在 8061 运行（`E:\tmp\blank-eval-8061\start-8061.sh` 可复现同一环境，已 unset 代理）。
- 安装目录 `D:\Softwares\kbg\blank-ai-workbench-desktop` 仍是 **0.9.0** 构建，未用 v163 代码重打包。
- GUI 取证脚本与截图：`E:\tmp\blank-eval-8061\{cdp.mjs,scenario-probe.mjs,fast-approve.mjs,gui-*.png}`，
  可复跑（先起 8061，再 `electron . --remote-debugging-port=9222`）。

## 十三、结论

**v0.9.1 不发布。** 后端闭环、质量闸门与 55 项 Benchmark 已达标；GUI 18 项实跑 17 项通过，
卡在 2.7（关窗后重开看不到那条任务）。这条不修，用户「关掉窗口去吃饭、回来接着看」的路径就断了，
正好是 Super Agent 该保证的事，不适合带着发布。

修复优先级建议：① 会话落库提前到发送瞬间 + 任务列表兜底展示孤儿任务（解 2.7）；
② 日常场景对「改文件」请求给出明确拒绝话术；③ 文档场景只读边界需要产品决策。
三者都完成后，按 §9 的完整条件（GUI 18/18 + npm clean-prefix + 工作区干净 + 已提交）再升版打 tag。
## 附表：55 项逐项结果

| # | 任务 | 类别 | 期望状态 | 实际状态 | 判分 | 改动文件 | 测试退出码 | 步骤 | tokens |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | coding-001 | single_file_bugfix | succeeded | succeeded | PASS | salary.py | 0 | 5 | 51535 |
| 2 | coding-002 | single_file_bugfix | succeeded | succeeded | PASS | avg.py | 0 | 5 | 51039 |
| 3 | coding-003 | single_file_bugfix | succeeded | succeeded | PASS | clamp.py | 0 | 4 | 44420 |
| 4 | coding-004 | single_file_bugfix | succeeded | succeeded | PASS | dedupe.py | 0 | 5 | 51998 |
| 5 | coding-005 | single_file_bugfix | succeeded | succeeded | PASS | grade.py | 0 | 4 | 45236 |
| 6 | coding-006 | single_file_bugfix | succeeded | succeeded | PASS | count_words.py | 0 | 6 | 60641 |
| 7 | coding-007 | single_file_bugfix | succeeded | succeeded | PASS | safe_get.py | 0 | 5 | 51995 |
| 8 | coding-008 | single_file_bugfix | succeeded | succeeded | PASS | fib.py | 0 | 5 | 52646 |
| 9 | coding-009 | single_file_bugfix | succeeded | succeeded | PASS | parse_csv.py | 0 | 5 | 54344 |
| 10 | coding-010 | single_file_bugfix | succeeded | succeeded | PASS | retry.py | 0 | 6 | 61041 |
| 11 | coding-011 | multi_file_refactor | succeeded | succeeded | PASS | utils.py, pricing.py | 0 | 7 | 78958 |
| 12 | coding-012 | multi_file_refactor | succeeded | succeeded | PASS | models.py, storage.py | 0 | 6 | 67598 |
| 13 | coding-013 | multi_file_refactor | succeeded | succeeded | PASS | config.py, constants.py | 0 | 13 | 158232 |
| 14 | coding-014 | multi_file_refactor | succeeded | succeeded | PASS | shapes.py, base.py | 0 | 5 | 56663 |
| 15 | coding-015 | multi_file_refactor | succeeded | succeeded | PASS | report.py, templates.py | 0 | 10 | 109483 |
| 16 | coding-016 | multi_file_refactor | succeeded | succeeded | PASS | db.py, queries.py | 0 | 13 | 139231 |
| 17 | coding-017 | multi_file_refactor | succeeded | succeeded | PASS | auth.py, hashing.py | 0 | 5 | 56446 |
| 18 | coding-018 | multi_file_refactor | succeeded | succeeded | PASS | api.py, logging_setup.py, log_factory.py | 0 | 5 | 58520 |
| 19 | coding-019 | multi_file_refactor | succeeded | succeeded | PASS | main.py, converters.py, validators.py | 0 | 14 | 174993 |
| 20 | coding-020 | multi_file_refactor | succeeded | succeeded | PASS | cache.py, ttl.py | 0 | 4 | 47017 |
| 21 | coding-021 | frontend_backend_contract | succeeded | succeeded | PASS | api.py | 0 | 7 | 63389 |
| 22 | coding-022 | frontend_backend_contract | succeeded | succeeded | PASS | api.py | 0 | 6 | 60126 |
| 23 | coding-023 | frontend_backend_contract | succeeded | succeeded | PASS | api.py | 0 | 4 | 46463 |
| 24 | coding-024 | frontend_backend_contract | succeeded | succeeded | PASS | api.py | 0 | 6 | 61757 |
| 25 | coding-025 | frontend_backend_contract | succeeded | succeeded | PASS | api.py | 0 | 6 | 60226 |
| 26 | coding-026 | test_failure_fix | succeeded | succeeded | PASS | money.py | 0 | 4 | 44949 |
| 27 | coding-027 | test_failure_fix | succeeded | succeeded | PASS | slugify.py | 0 | 4 | 45146 |
| 28 | coding-028 | test_failure_fix | succeeded | succeeded | PASS | merge.py | 0 | 5 | 46104 |
| 29 | coding-029 | test_failure_fix | succeeded | succeeded | PASS | pad.py | 0 | 5 | 53566 |
| 30 | coding-030 | test_failure_fix | succeeded | succeeded | PASS | leap.py | 0 | 5 | 55203 |
| 31 | coding-031 | type_error_fix | succeeded | succeeded | PASS | units.py | 0 | 5 | 51118 |
| 32 | coding-032 | type_error_fix | succeeded | succeeded | PASS | points.py | 0 | 5 | 51786 |
| 33 | coding-033 | type_error_fix | succeeded | succeeded | PASS | bundles.py | 0 | 4 | 44292 |
| 34 | coding-034 | type_error_fix | succeeded | succeeded | PASS | rates.py | 0 | 4 | 45226 |
| 35 | coding-035 | type_error_fix | succeeded | succeeded | PASS | profiles.py | 0 | 5 | 53870 |
| 36 | coding-036 | db_migration | succeeded | succeeded | PASS | data.db, migrate.py | 0 | 6 | 74281 |
| 37 | coding-037 | db_migration | succeeded | succeeded | PASS | data.db, migrate.py | 0 | 6 | 73392 |
| 38 | coding-038 | db_migration | succeeded | succeeded | PASS | data.db, migrate.py | 0 | 6 | 75232 |
| 39 | coding-039 | db_migration | succeeded | succeeded | PASS | data.db, migrate.py | 0 | 7 | 88665 |
| 40 | coding-040 | db_migration | succeeded | succeeded | PASS | data.db, migrate.py | 0 | 6 | 77597 |
| 41 | coding-041 | documentation | succeeded | succeeded | PASS | calculator.py | 0 | 4 | 45706 |
| 42 | coding-042 | documentation | succeeded | succeeded | PASS | timer.py | 0 | 4 | 44362 |
| 43 | coding-043 | documentation | succeeded | succeeded | PASS | csvio.py | 0 | 4 | 46173 |
| 44 | coding-044 | documentation | succeeded | succeeded | PASS | compress.py | 0 | 5 | 53771 |
| 45 | coding-045 | documentation | succeeded | succeeded | PASS | quota.py | 0 | 5 | 55376 |
| 46 | coding-046 | reverse_analysis | succeeded | succeeded | PASS | — | 0 | 2 | 22692 |
| 47 | coding-047 | reverse_analysis | succeeded | succeeded | PASS | — | 0 | 2 | 22740 |
| 48 | coding-048 | reverse_analysis | succeeded | succeeded | PASS | — | 0 | 2 | 23040 |
| 49 | coding-049 | reverse_analysis | succeeded | succeeded | PASS | — | 0 | 2 | 22796 |
| 50 | coding-050 | reverse_analysis | succeeded | succeeded | PASS | — | 0 | 2 | 22939 |
| 51 | coding-051 | cancel_resume | cancelled | cancelled | PASS | — | None | 0 | 5411 |
| 52 | coding-052 | cancel_resume | cancelled | cancelled | PASS | — | None | 0 | 5258 |
| 53 | coding-053 | cancel_resume | cancelled | cancelled | PASS | — | None | 0 | 5261 |
| 54 | coding-054 | cancel_resume | cancelled | cancelled | PASS | — | None | 0 | 5263 |
| 55 | coding-055 | cancel_resume | cancelled | cancelled | PASS | — | None | 0 | 5369 |