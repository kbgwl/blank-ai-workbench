# 内核（services/api）工作边界

给要改 agent 内核的人或 agent 看的。**这份文件不会被注入 system prompt**——
只有工作区根的 `AGENTS.md` 与 `.blank/AGENTS.md` 会（见
`services/api/src/blank_api/services/instructions.py:46`）。所以别在 `services/api/`
下再放一份 `AGENTS.md`，那不会被读到，只会多一个"看起来有约定"的假入口。

> 已知分歧：better-harness 评测会给 `services/api`（290 个源文件）建议一份嵌套
> `AGENTS.md`，因为 Claude/Codex 那类宿主按目录层级读指令。我们的加载器不读嵌套文件，
> 照建议加一份只会让静态评测好看、运行时什么都没变。要对齐就得先改加载器（引入按需
> 或分层的嵌套指令 + 预算），那是独立改动，别顺手做。

本文按需读：动内核之前读一遍对应小节，不要凭文件名猜。

## 模块地图（真实路径）

| 模块 | 职责 | 改动风险 |
|---|---|---|
| `services/agent/graph.py` | agent loop：模型调用、工具分派、截断恢复、交付文本 | 高：一个分支错了所有任务都受影响 |
| `services/agent/runtime.py` | 运行生命周期：建 scope、分层指令、钩子、终态写回 | 高：终态写错会污染任务历史 |
| `services/permissions.py` | 审批矩阵：四种模式、前缀记账、不可逆闸门 | 最高：放宽它等于放开副作用 |
| `services/sandbox/tiers.py` | 沙箱档位 off / workspace / workspace-net | 高：档位语义变了会静默放宽隔离 |
| `services/sandbox/snapshot.py` | 覆盖或删除前的可恢复副本（`data/snapshots`） | 中：只影响恢复能力 |
| `services/sandbox/task_isolation.py` | 任务级独立可写根（默认关，开则整任务拒绝而非降级） | 中：改变"任务在哪干活" |
| `services/tools/shell.py` | 命令执行、超时、转后台 | 高 |
| `services/tools/gateway.py` | 外部写入/联网的出口 | 高 |
| `services/instructions.py` | 分层指令装配与预算（8000/文件、12000/总） | 中：改预算会截断别人的约定 |

## 审批边界

四种模式（`permissions.py:31-41`）：`default` 逐个确认 / `accept_edits` 文件写入放行、
命令与联网仍确认 / `plan` 只读取证 / `bypass` 全放行但**不越过不可逆闸门**。

三条不能动的性质：

1. **不可逆动作永远不放行**——`IRREVERSIBLE` 正则（`permissions.py:155`）在放行链的
   任何一环之前生效，`bypass` 也一样。放开它就不是"减少打扰"，是"允许删库"。
2. **只有装依赖会按动作类型记账**（`_REMEMBERABLE_ACTION_KINDS = {"install_dependency"}`，
   `permissions.py:79`）。批准一次后同任务内不再重复问；删除、`git push`、部署、
   迁移、越界一律逐条确认。
3. **带管道的安装命令不记账**：前缀规则对 `... | tail` 这类命令故意不记（后半段不可控），
   所以卡片上只有"仅本次允许"。这是安全设计，不是 bug。

改这里的验收口径：**必须同时补测试或写明风险**（见 `CONTRIBUTING.md`），并跑
`cd services/api && .venv/Scripts/python.exe -m pytest tests/test_permissions*.py tests/test_sandbox_refusal_v99.py -q`。

### 人工闸门可以「打回到指定步骤」（2026-11-20 起）

驳回不再只有"否掉这次调用"一种。审批请求带 `send_back_step_id` 时，计划会从该步
起重开成 `pending`（`plan.rewind_to_step`），并给模型一句可据以行动的指令
（`plan.rewound` + hook_context），同时把 `plan_manifest.revision` 加一。

- 只在执行节点里改计划，**不在审批门里改**：审批门要重放安全，中断恢复会整节重放。
- 被打回步骤的 `evidence` 会清空——那条证据对应的是旧产物，留着会让下一步误以为输入端已就绪。
- 不带 `send_back_step_id` 时行为完全不变（老客户端不受影响）。

移植来源与取舍见 `docs/external-loopforge-2026-11.md`。

## 沙箱档位

`off` / `workspace`（默认）/ `workspace-net`（`tiers.py:21-24`）。
`run_shell(sandbox="host")` 是**单次**请求，不改变任务档位。

判据是稳定的 `reason` 码（`tier_off` / `sandbox_backend_unavailable` / ...），
界面与测试读码不读文案。后端不可用时**不许降级成"看起来隔离了"**，
要么整任务拒绝，要么如实标注只强制了文件边界（`measured_file_boundary_only`）。

## 失败与恢复

| 场景 | 恢复入口 | 判据 |
|---|---|---|
| 覆盖/删除前担心丢内容 | `sandbox/snapshot.py` 的快照（`data/snapshots`） | 快照存在且能从它还原 |
| 需要隔离目录做实验性改动 | 工具 `enter_worktree` / `exit_worktree`（`registry.py:89`） | 退出后主工作区 `git status` 干净 |
| 并行任务互相写文件 | `task_isolation.py`（默认关） | 打开后越界读取必须 `PermissionError` |
| 两个任务进同一棵 worktree | `worktree_session.claim_owner()`（写者唯一） | 第二个任务必须被拒；持有者释放后才能进 |
| 这一轮改错了要退回去 | 工具 `restore_worktree`（`action=plan|restore`） | 回滚后 `git status` 干净，`HEAD` 回到 `baseline` |
| 交付算不算被验收 | `POST /api/tasks/{id}/closure` + `.blank/closures/` | 台账里的 `revision` 与当前 `HEAD` 一致 |
| 后台任务被 detach | run 记录留在任务事件流；`/tasks/{id}` 读终态 | 终态与事件流一致 |

**写者唯一**（2026-11-21 起）：`enter_worktree(path=...)` 允许进入任何已注册的工作树，
所以两个任务可能同时写一棵树——它们各自的 diff 与验证结论都会建立在对方随时会改的
树上。认领文件落在**工作树自己的 git 目录**（`<repo>/.git/worktrees/<name>/blank-owner.json`），
不落在工作树里：写进工作树会让 `git status` 多一个未跟踪文件，我们自己的 remove 闸门
会把它算成"还有未提交改动"，用户就永远删不掉那棵树。

四种认领结果，`unavailable` 不能当成 `owned`：读不到 git 目录时流程照走，但事件里
`owner_status` 会如实写 `unavailable`——不许写成"看起来独占了"。持有者死在本进程里
（会话没了、文件还在）按 `reclaimed` 自愈，否则一次崩溃会永久锁住那棵树。

**租约与存活探测**（2026-11-21 补）：跨进程原先只认「pid 不同即他人所有」，于是别的
进程被杀之后那棵树会一直被记成"正被某个任务使用"，直到有人手去删 `blank-owner.json`。
现在两层判定（`_still_held`）：

1. **pid 死活**。Windows 上不能用 `os.kill(pid, 0)`（0 会被当成 CTRL_C_EVENT 真发出去），
   改走 `OpenProcess` + `GetExitCodeProcess`；只看 `OpenProcess` 成败不够——进程对象要等
   所有句柄关掉才消失，崩溃留下的 pid 仍能被打开。死 → 立刻可接手，不用等租约。
2. **租约**。`heartbeat_at` 由 `active_root()`（每次工具调用都走它）续，超过
   `LEASE_SECONDS`（30 分钟）未续即视为失效，下一个任务按 `reclaimed` 接手。
   取 30 分钟是因为单次工具调用本身可能跑很久；租约短于一次工具调用，正常任务会在
   自己干活时被人接手。判断不了死活且没有时间戳的老认领保守拒绝（放行 = 两个写者）。

**基准线与按任务回滚**（2026-11-21 补）：隔离只解决"别把主工作区改坏"，不回答
"改错了怎么退回去"。`enter()` 现在记两样东西：

1. `baseline`——新建的树取建树基准，`path` 进入的树取进树那一刻的 `HEAD`；
   写进认领记录（`blank-owner.json`），同时另存 `blank-baseline.json`。
   **`release()` 只松认领，不删基准**：任务收尾之后用户才发现改错了，那正是最需要它的时候。
2. `untracked`——进树时已存在的未跟踪文件。回滚只删"基准之后新增的"，
   别人的草稿不是本任务改的，不碰。

回滚入口是工具 `restore_worktree`（`action=plan|restore`）：`plan` 只读，列出相对基准
的改动路径；`restore` 才真退，整棵退会把已提交的改动一并退掉（`git reset --hard`），
退完用 `git status` 实测是否干净，`clean` 是实测值不是推断。三种情况拒绝而不是猜：
被另一个**还活着的任务**持有、没有基准线记录、路径越出工作树根。`plan` 的风险是
`low`、`restore` 是 `high`——批准一次 `restore` 不能记成"以后都免问"（见 `ARGUMENT_SCOPED_TOOLS`）。

**闭合记录与验收**（2026-11-21 补）：任务收尾（`deliver`）写
`.blank/closures/<task_id>.json`：`revision`（当前 `HEAD`）+ `changed_paths` +
校验结论 + `decision=pending`。三样绑在同一个 revision 上，这是"改完并且被验收"
能作为一条事实被查到的前提。

- 验收不是模型自评：本地测试与"已完成"都归改动验证（`models/agent-work-loop.md`）。
  所以记录默认 `pending`，等人或项目策略签。
- `GET /api/tasks/{id}/closure` 读记录；`POST /api/tasks/{id}/closure`
  （`{decision: accepted|rejected, decided_by, note}`）签字，追加
  `acceptance/ledger.jsonl` 一行。**revision 变了返回 409**：验收针对的是一份具体结果，
  把旧决定挂到新提交上，台账会显示"这一版被验收过"而实际看的是上一版。

交付事实里的 `changedPaths` 与校验的 `covered_fingerprint` 同源，并补上
`git status` 的删除项（`shell rm` 不会出现在工具结果里）；任务页的改动卡读它，
不再由总结文本决定"改了哪些文件"。

**已知不一致**：任务停在 `waiting_approval` 时若服务中断，`agent_runs` 可能停在
`waiting_approval` 而 `tasks` 已是 `failed`。界面看 task，所以不影响用户，但不要
据此推断 run 状态。TTL 巡检只管 `task.status == waiting_approval` 的任务。

## 这一轮为什么结束：终态码（2026-11-21 起）

`task.failed` / `task.completed` / `task.cancelled` 等终态事件的载荷里多了一个
`terminal` 对象：`{code, detail?, reason}`。`code` 是封闭枚举
（`services/agent/terminal.py` 的 `TERMINAL_CODES`），`detail` 是族下面的种——
`verification_blocked` 的 detail 是 verifier 的硬违规码（`zero_change` 等），
`model_error` 的 detail 是供应商码（`model_timeout` 等）。

三条性质，改的时候别破坏：

1. **终态码只由一个纯函数产出**（`terminal_detail()`），两个写入点都调它：
   `runtime._finish` 与 worker 的审批过期收敛。第三个写入点出现时也要调它，
   否则界面得认第二种形状。
2. **它不能被 `extra` 覆盖**。`terminal_payload()` 先铺 `extra` 再写 `terminal`；
   反过来任何一个多传一个键的调用点都能把失败伪装成 `completed`。
3. **不许读中文推码**。`failure_reason()` 以前靠 `"零改动" in finding` 判断，
   同一件事换个说法就认不出。现在按 `verdict["codes"]` 取句；子串那一段只是
   给升级前的旧 verdict 留的退路。

前端按「越具体越优先」取码：`terminal.detail` → `payload.code` → `terminal.code`
（`web/src/components/bot/turns.ts` 的 `turnReply`），码表文案在
`web/src/components/tasks/error-copy.ts`。

**取消是三条独立的写入点**（不是一条）：`runtime.run` 在开跑前发现已取消、
`graph` 在执行节点发现已取消、`graph` 在执行中工具被中断。三处都自己写
`task.cancelled`，而且 `_finish` 看到已有终态会跳过自己那条——所以终态码必须在
这三处各自带上，漏一处，用户看到的恰好就是没码的那条。

## 这一轮为什么又跑一轮：跳转解释与它的界面（2026-11-21 起）

每次节点跳转记一条 `agent.transition`：`{from, to, rule, why, ...facts}`。`rule` 是机读码，
`why` 是只吃 state 字段（校验状态、修复轮次、待办条数）推出的那句话，不调模型
（`services/agent/transition.py`）。三条性质：

1. **判定顺序必须与 `graph.route_after_*` 逐条一致**。这是一份刻意的重复实现：路由
   返回字符串，解释返回一句话。判据不是 22 条手写样本，而是按字段取值做笛卡尔积、
   以真实路由为真值（`tests/test_transition_exhaustive_v485.py`）：路由怎么走，解释就
   必须怎么说，且不许退化成 `unexplained`。这一层抓出过一个真缺陷：`validation` 为空
   （没跑过校验）时路由照样进审查，旧解释却写"校验通过（通过）"。
2. **认不出就不编**。没登记的跳转标 `unexplained`、`why` 留空，`emit_transition` 见到
   `to` 对不上自己就丢弃整条——宁可少一行，也不要一行错的理由。
3. **界面是消费方，不是第二份账**。任务页「执行过程」区读 `agent.transition`，
   `rule → 短句` 的映射在 `web/src/components/tasks/transition-copy.ts`（与
   `error-copy.ts` 同目录同口径）；数字仍用后端那句 `why`，前端不重算。连着同一条
   折叠成「连续 N 次」，隔开出现的标「第 N 次」——后者就是"为什么又跑一轮"的答案。
   认不出的 rule 标黄并露出原始码与 `from→to`。后端加分支而前端没登记，判据会红
   （`transition-copy.test.ts` 直接读 `transition.py` 的 `_result` 字面量）。

对照表式的一致性是上一版（`test_transition_explain_v432.py`）的遗留，仍然保留：
它带着"期望的机读码"，与穷举那条互为二重口径（一个防漂移，一个防两边一起算错）。



最小集（按改动范围挑，别一上来跑全量 18 分钟）：

```bash
cd services/api && .venv/Scripts/python.exe -m pytest tests/<相关文件> -q
cd services/api && .venv/Scripts/python.exe -m pytest -q      # 全量，约 18 分钟
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/**/*.test.ts"
bash check.sh                                                  # 跨端门禁，约 1 分钟
```

**已知环境红（4 条）**：`fs_broker` 1 条 + `sandbox_restricted` 3 条，改动前后都一样。
不要为了"全绿"去改它们，也不要因为它们红就认为自己的改动没生效。

## 校验与改动的绑定（2026-11-20 起）

`run_validation_node` 会给校验记录盖章：`covered_mutations`（跑时已有几次成功写动作）、
`covered_files`（覆盖到的改动文件）、`covered_fingerprint`（这些文件的内容指纹）。
交付时 `_changes_after_validation` 三路判断：

1. 写动作计数超过戳 → 过期（用计数不用下标：收尾前的 scratch 隔离会删条目、让下标前移）；
2. 覆盖文件的内容指纹变了 → 过期（shell 的 `sed -i`、格式化器不经文件工具结果，只能靠内容比对）；
3. 只有旧 `covered_results` 下标的记录 → 按老口径兜底。

scratchpad 的写在两侧都不计。过期后交付走「未跑校验：校验之后又改了代码，需要重跑」，
接口 `/tasks/{id}/status` 的 `delivery.verify` 带 `observed` / `missing` / `stale` / `covers`。
`missing` 与 `stale` 正交：前者是根本没跑，后者是跑了但作废，(0,1) 与 (1,0) 是两种缺口。

**残留盲区**：指纹只覆盖「校验时已经改过的文件」。校验之后由 shell 新**创建**的文件
不在指纹里，只有它同时被文件工具写、或之后再跑校验才会被发现。改相关逻辑时别把
「没报未跑校验」直接读成「验过了」。
