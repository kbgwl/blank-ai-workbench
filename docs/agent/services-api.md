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
| 后台任务被 detach | run 记录留在任务事件流；`/tasks/{id}` 读终态 | 终态与事件流一致 |

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

**还没覆盖的**：取消/暂停路径（`graph.py` 里那两处 `task.cancelled`）仍只写
`reason` 不写 `terminal`；界面靠"/取消/"这段文字兜住，行为正确但不够干净。
要补就在那两处调同一个函数。

## 改完怎么验

最小集（按改动范围挑，别一上来跑全量 18 分钟）：

```bash
cd services/api && .venv/Scripts/python.exe -m pytest tests/<相关文件> -q
cd services/api && .venv/Scripts/python.exe -m pytest -q      # 全量，约 18 分钟
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test "src/**/*.test.ts"
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
