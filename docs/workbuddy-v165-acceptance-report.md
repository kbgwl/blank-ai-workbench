# v0.10.0 Autonomous Super Agent — 验收记录

> 任务书：[docs/workbuddy-v165-autonomous-super-agent.md](workbuddy-v165-autonomous-super-agent.md)
> 记录日期：2026-11-06　|　范围：Phase 1–5 代码落地 + 自动化验收 + 本机安装版同步
>
> **口径声明（先读）**：本报告只写「已经拿到的事实」。任务书 §19 明确「只有所有证据齐全，
> 才允许写『v0.10.0 Autonomous Super Agent 已完成』」。本次有两项真机证据未重跑
> （v0.10.0 的 GUI 18 项、55 项 Benchmark），见 §11。因此本报告**不宣称发布闸门通过**，
> `v0.10.0` Tag 是代码定版锚点，不是「已发布」。

---

## 1. 交付物与环境

| 项 | 事实 |
| --- | --- |
| 代码仓库 | `E:\projects\kbgai\kbgai`（分支 `main`） |
| 本机安装版 | `D:\Softwares\kbg\blank-ai-workbench-desktop`（`self-deploy.cjs` 同步） |
| 真机数据库 | `%APPDATA%\blank-ai-workbench-desktop\blank.db`，已迁到 `0033_chat_goals` |
| 冒烟数据库 | `E:/tmp/blank-diag/mig-0033-check.db`（真机库副本，接口冒烟专用，不污染真机） |
| 冒烟服务 | 安装版自带 Python 起的 uvicorn `127.0.0.1:8061`（`BLANK_INPROCESS_RUNNER=0`） |
| 版本真源 | `desktop/package.json` = `0.10.0`；根 `package.json` 与 README 由 `scripts/check_versions.mjs` 钉住 |

改动规模：`git diff --stat` 35 个已跟踪文件 / +1371 −111，另有 29 个新增文件
（其中 3 个是上一轮雾白皮肤与启动屏遗留、1 个是任务书本身）。

---

## 2. Chat Supervisor 架构与消息状态图（§2）

实现：`services/api/src/blank_api/services/agent/supervisor.py`，入口
`api/routes/threads.py::create_turn()`。**纯规则、不额外调模型、不改用户原话**——
用户那句话原样进 `observe()`，判定结果写进 `task.goal_spec.supervisor`，不拼任何
「执行规则 / 你处于开发场景」前缀（v0.9.1 删掉场景模式的原因）。

```text
用户一句话
   │ observe(text, has_active_goal, active_task_status, scenario, automation_mode…)
   ├─ 控制词（继续/暂停/取消/重试/同意）→ 作用于 last_task，不新建 turn
   │     且目标状态同步：continue→executing、pause→paused、cancel→cancelled
   ├─ 「换个任务…」等显式新目标 → 旧目标 archived，另起 goal
   ├─ 补充要求（这个问题还没解决 / 再改一下 / 按刚才的继续）→ continues_goal=True
   │     新建 turn，但挂回同一个 goal_id；执行侧用 execution_override() 纠偏成 act
   ├─ 寒暄 / 概念 / 判断 → answer、advise，不落盘
   └─ 明确动手目标 → execute，进 Dispatcher 七条件下发
```

`as_dict()` 固定字段：`intent / needs_execution / needs_approval / needs_clarification /
priority / confidence / reason / scenario / automation_mode / goal_id / related_task_id /
control_action / continues_goal`。风险判定不在这里放行——`needs_approval` 恒为 false，
审批由 `PermissionPolicy` 与 §13 矩阵负责。

---

## 3. Goal Tracker 数据结构与状态转换（§3）

实现：`models/goal.py`（ORM）、`services/agent/goals.py`（`GoalTracker`）、
迁移 `alembic/versions/0033_chat_goals.py`、`schemas/goal.py`（`GoalOut`）。

表 `goals`：一条会话同一时刻只有一个活跃目标。`conversation_id` 实际是 `threads.id`
（沿用 v0.9.1 的别名口径，不新造会话层）。目标存 `task_ids[] / run_ids[]`，
`current_task_id / status / steps_done / steps_pending / steps_failed /
blocked_reason / next_action / budget_tokens / tokens_used`；索引覆盖
`status / conversation_id / project_id`。

状态机：

```text
captured ──下发 immediate──> executing ──终态 succeeded──> succeeded
   │                            │
   ├── wait_user ──> waiting_user    ├── waiting_approval（审批卡）
   ├── queue ─────> planned          ├── repairing（自动修复）
   └── blocked ───> paused           └── failed / cancelled / paused
旧目标在显式新目标出现时 -> archived
```

任务终态回写目标在 `services/agent/runtime.py::_finish()` 里集中做
（`_GOAL_STATUS_BY_TASK`），避免「任务失败、目标还写着执行中」。
索引：`ix_goals_status / ix_goals_conversation_id / ix_goals_project_id`。

---

## 4. Planner / Dispatcher / Verifier 与自动修复闭环（§4–§7）

| 角色 | 文件 | 落地口径 |
| --- | --- | --- |
| Autonomous Planner | `services/agent/planner.py` | `build_plan()` 把 `list[str]` 升级成 `plan_manifest`（`goal_id / plan_id / steps[] / max_steps / token_budget / approval_points / revision / rollback_strategy`）；`revise_plan()` 动态调整，`repair_hint()` 生成修复指令 |
| Task Dispatcher | `services/agent/dispatcher.py` | `decide_dispatch()` 逐条核对 §5.1 七条件，`dispatch_snapshot()` 让一次下发能靠 `task_id / goal_id / conversation_id` 找回 |
| 独立 Verifier | `services/agent/verifier.py` | `verify()` 不调模型、不查库、不看 Agent 自述，只读工具结果 / 测试结论 / 磁盘改动 / 交付文本；只有硬违规 `blocking=True` |
| 接线 | `services/agent/graph.py` | `plan.created`、`verification.started/failed`、`repair.started/completed/stopped`、`verification.verdict` |
| 终态 | `services/agent/runtime.py` | `resolve_status()` 被 `verdict.blocking` 改判 failed；`reason` 取 `failure_reason()` |

自动修复闭环（§7）：

```text
run_validation 失败/超时/error
  → verification.failed（带 kind + detail）
  → repair.started + repair_hint 只进 hook_context（不写回用户历史、不改用户原话）
  → revise_plan(plan, failure, repair_round)
  → route_after_validation → execute_action
  → 再验证 → verification.verdict / repair.completed
  → 相同失败重复出现 → no_new_evidence=True / exhausted=True → repair.stopped（硬上限 3 轮）
```

`revise_plan` 用 `last_failure_key = kind + detail` 判「有没有新证据」：标题里带轮次
也要判死，否则会一直追加「第 2 轮修复」空转。写文件工具白名单
`_WRITE_TOOLS = {apply_patch, write_file, delete_file, create_file}`——`run_shell`
不算写盘，`ls / echo / git status` 不会被误判成「零改动伪成功」。

事件链实测（冒烟库）：`message.observed / goal.created / goal.linked / task.dispatched`
均可查（`scripts/v165_smoke.py` 第 22–25 条断言）。

---

## 5. 后台持续运行、预算与三种自动化模式（§8/§14/§15）

**Phase 3 的真实缺口不是「没有后台」，是设置没被消费。** 关窗藏托盘、重开恢复、
Worker 租约/心跳、前端 20 秒轮询在 v0.9.1 已接线；本轮补的是：

| 缺口 | 修复 |
| --- | --- |
| `settings.background` 八个字段七个无业务消费点 | `background_policy.py`：`resolve_background / recovery_limit / budget_override / autonomous_enabled / enforcement` 五个纯函数统一口径 |
| `MAX_RECOVERY_ATTEMPTS=3` 硬编码 | `tasks.py::recovery_attempt_limit()` 与 `worker.py` 都读设置，**只能调小不能调大** |
| 派发快照预算不认设置 | `_record_dispatch()` 用 `budget_override()` 逐项取 min 折进 `dispatch_snapshot.budget`；`route()` 保持纯函数 |
| 触发器 / Watchdog / 过期审批只在独立 worker 里跑，桌面默认链路从不执行 | `app.py::background_supervisor()` 每 30s 调 `worker.supervise_once()`；`autonomous_enabled(policy, inprocess=...)` 二选一，杜绝双执行者；manual 模式不推进 |
| Watchdog 只写 `enforced: False` | 告警事件新增 `disposition`（`record/notify/requeue/abandon`），由 `enforcement(action)` 映射 |
| §15 缺「当前活跃目标列表」 | 新增 `GET /api/v1/observability/goals`：活跃目标、后台任务（含 goal/task/conversation 三索引）、`by_status`、队列长度、Worker 健康与过期租约、孤儿数 |

**§8 三种模式与 §13 审批矩阵**（`services/agent/automation.py`）：

| | 权限档位 `permissions.default_mode` | 自动化模式 `background.automation_mode` |
| --- | --- | --- |
| 回答 | 「要不要问」 | 「要不要自己动」 |
| 取值 | `default / accept_edits / plan / bypass` | `manual / assist / autonomous` |

`approval_matrix()` 的硬闸排在 `MODE_BYPASS` 之前：不可逆命令、`HIGH_RISK_ACTIONS`
（删除/批量修改/装依赖/迁移/网络/push/部署）一律 `ask` 且 `bypassable=False`；
`DEFAULT_OFF_ACTIONS`（工作区外、批量删、外发、生产部署、提权、敏感上传）直接 `deny`。
接线在 `permissions.py::PermissionPolicy.decide()` 的两个「一次都不问」短路之前，
用户亲手点过的「总是允许 git push」前缀账仍然生效（那是一次真实审批，不该被顶掉）。
plan 档仍自动开跑只读分析，写动作由 `PermissionPolicy` 拒绝——用户选的是「只许读」，
不是「别动」，不该又变成「一直要我催」。

设置页回显：后端 `services/settings.py`（DEFAULTS + normalize）、
`schemas/settings.py`（`BackgroundSettings.automation_mode` / `BackgroundPatch` 用
`Literal` 让非法档位 422）、`api/routes/settings.py::_limits()` 都补齐；前端
`agent-runtime-panel.tsx` 的 `apply()` 同时校验权限档位与自动化模式回显，
不再出现「保存成功但没生效」。

---

## 6. GUI 状态交互（§9）

新增 `web/src/components/bot/goal-status.ts`（纯数据层，可单测）与
`goal-status-bar.tsx`（纯展示），挂在 `bot-app.tsx` 的 `<header>` 之下：

| 状态格 | 数据来源 |
| --- | --- |
| 场景 | `scenarioKey`（本地选择） |
| 模型 | `boot.providers` 里当前 `providerId` 的 `model` |
| 模式 | `GET /settings/agent` → `background.automation_mode`，20s 轮询 + focus 补拉 |
| 当前目标 | `GET /threads/{id}/goal` → `title + status`，8s 轮询 + 任务状态变化立即补拉 |
| 后台任务数 | `fetchActiveTasks()` 的 `activeTasks.length` |
| 服务状态 | 以设置接口这次真实往返为准（后端中途挂掉不再一直显示在线） |

切会话时目标按 `threadId` 键存取，不会把上一条会话的目标显示到这一条；
老后端没有 `/goal` 端点时静默降级，不把整页打红。

### 6.1 真机撞出来的 bug：任务一成功，目标格就消失

第一版 `GET /threads/{id}/goal` 只返回 `GoalTracker.active()`（非终态）。真机跑一条
33 秒的任务：状态条上「目标 … · 执行中」一路正常，任务成功的同一刻目标格**直接消失**——
恰好是最该被看见结论的时刻。

修法是把「展示口径」和「决策口径」拆开：

```python
# services/agent/goals.py
async def latest(self, conversation_id: str) -> Goal | None:
    """这条会话最近的一个目标，含已结束的。只给展示用，不参与决策。"""

# api/routes/threads.py
goal = await tracker.active(thread.id) or await tracker.latest(thread.id)
```

`active()`（不含终态）继续负责「新建/复用目标、控制词判定」，`latest()` 只喂状态条。
修复后真机复验：状态条稳定显示 `目标 … · 已完成`，切走再切回仍在。
回归用例 `test_completed_goal_is_still_readable_by_the_status_bar` 同时钉住两侧口径。


---

## 7. 自动化测试与回归

| 层 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest -q` | 见 §7.1 |
| 前端类型 | `node ./node_modules/typescript/bin/tsc --noEmit` | rc=0，零输出 |
| 前端单测 | `node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"` | **273 pass / 0 fail** |
| 前端 lint | `eslint`（改过的 6 个文件） | rc=0 |
| 桌面壳 | `node --test tests/*.test.cjs`（7 个文件） | **71 pass / 0 fail** |
| 版本口径 | `node scripts/check_versions.mjs` | 6 项 ok，`0.10.0` |
| 安装版接口冒烟 | `python scripts/v165_smoke.py --base …:8061 --db <副本> --project <id>` | **28/28 通过** |

### 7.1 后端全量回归

```text
2194 passed, 23 skipped, 62 warnings in 890.21s (0:14:50)
```

（本轮最后一次全量跑在权限闸门修正之后；随后又改了 `goals.py` / `threads.py` 的展示口径
并补了 1 例回归，这两处只影响 `/goal` 一个只读端点，`test_v165_supervisor.py` 11 例
单独复跑通过。）

### 7.2 本轮修掉的回归（都是真机/全量里撞出来的）

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 4 例权限测试红：`curl https://x` 在 bypass 下变成 ask | 把 `risk="high"` 也送进 §13 矩阵，等于偷偷改掉用户选的档位 | 闸门只认 `classify_action()` 命中的 §13 动作签名，普通风险标签仍走既有 `irreversible / risk:ask` |
| `test_approve_high_risk_reports_its_own_rule` 红 | `echo hi` 被 `approve_high_risk` 短路前误判 | 同上；低风险命令仍返回 `flag:approve_high_risk` |
| 两个 guardian 测试批量跑才红、单跑全绿 | `test_v165_p0_fixes.py` 收尾用 `PUT default`，写出一份含 `guardian` 段的完整设置文件，`Guardian.from_settings()` 因此不再回落 `BLANK_GUARDIAN` 环境变量 | 测试改为按**原始字节**复位共享设置文件 |
| Phase 2 首轮 6 例红 | 鸭子替身缺 `goal_spec`/`hook_context`；`run_shell` 被当成写文件工具 | `getattr` 兜底；`_WRITE_TOOLS` 收紧 |
| 旧 asar 快照测试红（6 → 9 文件） | 启动屏新增 3 个壳文件 | 测试改为从 `desktop/package.json` 的 `build.files` 动态读 |
| 设置污染导致后续审批用例被静默放行 | 同上 guardian 那条 | 同上 |
| 启动日志出现 `migration-failed \| 迁移超时（10000ms）`，但后端随后又起来了 | 换版后首次启动要为上百个新源码文件生成 `.pyc`，叠加杀软首扫，10 秒预算不够（本机 0030→0032 实测 5.9 秒已占六成） | `MIGRATION_TIMEOUT_MS` 默认改 60 秒，仍可用 `BLANK_MIGRATION_TIMEOUT_MS` 覆盖；真卡住照样被这个上限打断 |
| `approve_high_risk` 被 §13 硬闸误挡，`rm -rf` 用例红 | 把「本次运行的显式授权」当成了「模式放行」 | 硬闸只闸 `MODE_BYPASS`；`approve_high_risk` 是请求体里当场写下的授权，放行并保留 `flag:approve_high_risk` 归因 |

### 7.3 GUI 18 项真机取证（v0.10.0 实测）

**先说清上次为什么没做成**：之前判断「工具环境拉不起 Electron GUI」是错的。真实原因
是本机 shell 注入了 `ELECTRON_RUN_AS_NODE=1`，Electron 被当成纯 Node 跑，于是既没有
窗口、也报 `bad option: --remote-debugging-port`。用 `env -u ELECTRON_RUN_AS_NODE`
启动后，GUI 与 CDP 都正常。用户双击桌面图标不受这个变量影响。

取证环境：`desktop/dist/win-unpacked/Blank AI.exe`，`--remote-debugging-port=9222`，
数据库指向真机库**副本**（`E:/tmp/v165-gui/gui.db`），工作区指向副本目录
（`E:/tmp/v165-gui/ws`，从 `F:\空白格\测试` 复制），不动用户真实数据。
截图 18 张在 `E:/tmp/v165-gui/shots/`。

| # | 验收项 | 结论 | 真机依据 |
| --- | --- | --- | --- |
| 1 | 启动真实 Electron | ✅ | CDP `/json/version`：`Electron/38.8.6`，页面 `app://bundle/index.html` |
| 2 | health=200 | ✅ | 页面内 `fetch /api/v1/health` → `200` |
| 3 | 首页和版本正确 | ✅ | `document.title = "Blank AI"`；关于/安装包版本 `0.10.0` |
| 4 | 新建对话 | ✅ | 点「新对话」后 URL 由 `?thread=72a9…` 变为 `index.html` |
| 5 | 选择开发场景 | ✅ | 状态条 `场景 开发`；五个场景逐个切换见 #13 |
| 6 | 选择模型 A | ✅ | 下拉选 `DeepSeek · deepseek-flash`，状态条 `模型 deepseek-flash` |
| 7 | 读取真实工作区 | ✅ | 任务读 `README.md` 原文并生成计划（`plan.created`） |
| 8 | 修改真实文件 | ✅ | 磁盘事实：`README.md` 首行变为 `# 公司官网（静态单页）本仓库用于空白格日常演示` |
| 9 | 查看 Diff | ✅ | 点「README.md 修改」后 GUI 渲染 `@@ -1,4 +1,4 @@` 与 `-# / +#` 两行 |
| 10 | 运行测试并查看退出码 | ⚠️ 未重跑 | 该工作区是纯静态站点，没有测试；v0.9.1 已有 `10-pytest-exitcode.png` |
| 11 | 交付内容简洁且有事实 | ✅ | 面板 `改动文件 1 / 耗时 33s / TOKEN 28,046 / 改动的文件 README.md 修改` |
| 12 | 切换模型 B 并确认真实变化 | ✅ | 切到 `glm-4.5-flash`，状态条同步为 `模型 glm-4.5-flash` |
| 13 | 验证五个场景 | ✅ | 状态条依次 `开发 / 文档 / 日常 / 头脑风暴 / 逆向分析`，截图 5 张 |
| 14 | 触发审批并真实点击批准 | ✅ | 卡片三键「仅本次允许 / 本会话总是允许 git status / 拒绝」；点批准后事件链 `approval.required(risk=high) → approval.decided(approve=true, scope=once) → approval.granted → tool.completed` |
| 15 | 真实点击拒绝并确认任务停止 | ✅ | `approval.decided(approve=false) → approval.denied`，任务终态 `paused`，**没有**被记成成功 |
| 16 | 日常记住/不记住 | ⚠️ 未重跑 | v0.9.1 已有 `13-daily-refused.png`；本轮场景切换已覆盖 `日常` 只读口径 |
| 17 | 关闭窗口后 Worker 继续 | ✅ | `taskkill /PID <主进程>`（WM_CLOSE，等同点 ✕）后 6 秒：`health=200`、页面目标仍在、进程存活 |
| 18 | 重开后任务可见、可继续 | ✅ | 再启动触发单实例唤回，`visibilityState=visible`，同一会话同一任务仍在，状态条显示 `目标 … · 已完成` |

16/18 通过，2 项（#10 跑测试、#16 日常记忆）本轮没重跑，理由见上表，不冒充通过。

v165 本轮真正新增的 GUI 面（状态条、目标格、模式格、后台任务数、服务状态）全部在真机
读数里出现，例如完整一行：

```text
场景 开发  模型 glm-4.5-flash  模式 辅助  目标 把 README.md 的第一行标题后面加上一句… · 已完成  后台 13 个任务  服务在线
```

> 注：`window.close()`（渲染进程主动关）会走「窗口全关 → 退出」，与用户点 ✕ 的
> `win.on("close")` 托盘路径不同；本次用 WM_CLOSE 复现真实 ✕，结果符合设计。

---

## 8. 安装版同步与真机取证

`desktop/self-deploy.cjs` 全量同步（静态导出 29.0s，104 个前端文件 → `resources/web`；
后端源码 170 个文件、迁移 33 个文件 → `resources/backend`；壳入口已是最新，无需重打包）。
随后 `--api` 二次同步新增的 `/observability/goals`。

md5 比对（工作区 vs `resources/backend/api/src/blank_api`）13/13 一致：
`settings.py / schemas/settings.py / permissions.py / automation.py /
background_policy.py / verifier.py / planner.py / dispatcher.py /
api/routes/threads.py / api/routes/tasks.py / api/routes/observability.py /
app.py / worker.py`。前端静态产物中可检索到新状态条文案。

真机库迁移：

```text
alembic_version: 0033_chat_goals
goals indexes: ix_goals_status / ix_goals_conversation_id / ix_goals_project_id / sqlite_autoindex_goals_1
```

安装版后端冒烟（`scripts/v165_smoke.py`，28 条硬断言全通过）关键读数：

```text
PASS  limits 暴露三种自动化模式 — ['manual', 'assist', 'autonomous']
PASS  非法自动化模式返回 422 / autonomous 保存成功 / autonomous 读回一致
PASS  dispatch 快照带 goal_id/task_id/conversation_id
PASS  effective policy 落库 — mode=autonomous, auto_start=true, high_risk_always_asks=true
PASS  补充要求另起一轮但复用目标 / 目标没有被重建
PASS  「继续」不新建一轮 / 「暂停」作用于已有任务
PASS  后台任务三条索引齐全 / 孤儿任务为 0 / worker 健康字段齐全
PASS  事件链含 message.observed / goal.created / goal.linked / task.dispatched
```

**注意**：以上用的是安装版后端 + 真机库**副本**。真机用户数据库里
`goals=0 / v165_supervised_tasks=0`，说明用户尚未用新版客户端发过消息；
用户真机测试之后指标才会有真实分母（见 §10）。

---

## 9. §16 新增指标（`scripts/v165_metrics.py`，只读 sqlite）

冒烟库副本：

```json
{
  "conversationGoalLinkRate": 1.0,
  "orphanTaskRate": 0.0,
  "approvalStateMismatch": 0,
  "resumeAfterCloseRate": null,
  "automaticRepairEvidenceRate": null,
  "currentTurnAnswerLeak": 0,
  "_counts": {
    "tasks": 43, "threaded_tasks": 34, "v165_supervised_tasks": 4,
    "goals": 2, "linked": 4, "orphans": 0,
    "repair_started": 0, "repaired_with_evidence": 0,
    "resumed_runs": 0, "recovered_runs": 0, "succeeded_tasks": 25
  }
}
```

真机库（只读，用户尚未跑新版）：`conversationGoalLinkRate=null`、
`orphanTaskRate=null`、`approvalStateMismatch=0`、`currentTurnAnswerLeak=0`。

分母口径：只统计带 `goal_spec.supervisor` 的 v0.10.0 任务，否则 v0.9.1 历史任务
会把关联率拉到 6.25%。分母为 0 返回 `null` 而不是 100%——「没跑任何任务」不算满分。

`resumeAfterCloseRate` / `automaticRepairEvidenceRate` 为 `null`：本轮冒烟没有制造
关窗恢复与测试失败自动修复的真实事件。**不编数字**，这两项留待真机 18 项与
Benchmark 补证。

---

## 10. Benchmark（§16）

沿用已落库的 55 项 coding 基线（`evals/benchmarks/coding/last-run.json`）：

```text
total 55 / judged 55 / completionRate 91% / passRate 100% / testPassRate 100%
misEditedFiles 0 / totalTokens 3,030,581 / avgElapsedMs 113,203
```

该基线跑在 v160/v163 代码上。**v0.10.0 未重跑 55 项**：重跑需要可用的
provider 与数小时机时，本轮没有产生新的模型调用证据；不拿旧基线冒充新结论。
Phase 2 的 Verifier / 修复闭环改的是验收口径，理论上会影响 `falseDone /
zeroChangeSuccess`，因此这一项必须重跑才能判定达标。

---

## 11. 未完成项与风险（诚实清单）

| 项 | 状态 | 为什么没做 / 影响 |
| --- | --- | --- |
| GUI 18 项中的 #10（跑测试看退出码） | **未重跑** | 取证工作区是纯静态站点，没有测试可跑；v0.9.1 已有该截图。`run_tests` 与「无测试配置→skipped 不算失败」的 P0 修复由单测覆盖 |
| GUI 18 项中的 #16（日常记住/不记住） | **未重跑** | v0.9.1 已有 `13-daily-refused.png`；本轮只验证了 `日常` 场景在状态条与只读口径上的表现 |
| 55 项 Benchmark 重跑 | **未完成** | 见 §10；没有新的模型调用证据，不引用旧数当新数。本轮真机各跑了一轮真实任务（deepseek-flash / glm-4.5-flash），但那是功能取证，不是 55 项评测 |
| `resumeAfterCloseRate = 100%` / `automaticRepairEvidenceRate = 100%` | **分母仍不全** | #17/#18 已产生真实的「关窗继续 + 重开可见」事件；自动修复闭环仍只有单测与事件定义，没有一次真实的「测试失败→自动修复→重测通过」 |
| 真实自动修复闭环日志 | 只有单测与事件定义 | 需要一次真机测试失败任务；本次工作区没有测试可跑（与 #10 同因） |
| npm clean-prefix 全新安装 | 本轮未重跑 | v0.9.1 已有证据；本轮改的是 API、前端与桌面壳，CLI 包未动 |
| `desktop/dist` 新安装包 | 见 §12 | 打完需 `verify-package.cjs` 14 项校验。 |

按 §18，以上任一项未补齐都**不允许宣称发布 v0.10.0**。本报告与 Tag 的定位是
「代码定版 + 自动化验收完成，等真机补证」。

---

## 12. 最终回归、打包、Commit / Tag / 工作区

| 项 | 事实 |
| --- | --- |
| 后端全量 pytest | `2195 passed, 23 skipped, 62 warnings in 657.65s (0:10:57)`，rc=0 |
| 前端类型 / 单测 / lint | `tsc --noEmit` rc=0；`node --test` **273 pass / 0 fail**；eslint 6 文件 rc=0 |
| 桌面壳单测 | `node --test tests/*.test.cjs` **71 pass / 0 fail** |
| 版本口径 | `node scripts/check_versions.mjs` 6 项 ok，`0.10.0` |
| 安装包 | `desktop/dist/Blank-AI-安装版-0.10.0.exe`，128,042,670 字节 |
| `verify-package.cjs` | 24 项全 ok（含「包内页面标题是 Blank AI」「包内源码无明文 Key」「包内无本地库」） |
| 本机安装 | `install-local.cjs` rc=0，5 分 32 秒装到 `D:\Softwares\kbg\blank-ai-workbench-desktop` |
| 装后启动 | `single-instance acquired → splash → tray ready → migration-start → spawn-start → backend started → startup complete`，全程 22 秒；`/api/v1/health` = 200 |
| 装后库版本 | `alembic_version = 0033_chat_goals` |
| 装后接口 | `/settings/agent` 返回 `automation_modes: [manual, assist, autonomous]` 与三档中文说明，`background.automation_mode = assist` |
| 用户库现状 | `goals = 0`（用户还没用新版发过消息，所以 §9 指标分母仍为空，不编数） |
| 装机数据保护 | 安装前备份 `resources/backend/api/data`（165 个文件）→ 装完回填，文件数一致 |

迁移超时那处修复在真机上得到验证：`migration-start → spawn-start` 用时 8.5 秒，
放在旧的 10 秒预算里就是这次假失败的边缘，新预算 60 秒不再报 `migration-failed`。

---

## 13. 任务书 §18 逐条核对

| 发布条件 | 结论 | 依据 |
| --- | --- | --- |
| Chat Supervisor 真实工作 | ✅ | §2 + 冒烟 28/28 + `test_v165_supervisor.py` |
| Goal Tracker 真实关联多轮 | ✅ | §3 + 冒烟「补充要求复用目标」 |
| 自然语言自动下发 | ✅ | §4 `dispatch` 快照与 `auto_start`；`shouldAutoRunTurn(status, autoStart)` |
| 自动修改和测试闭环 | ✅ | §4 接线与 Phase 2 回归；真机端到端沿用 v164 §8 第 7–11 项 |
| 测试失败自动修复或明确停止 | ✅ 代码 | `repair.started/completed/stopped` + 重复失败无新证据即停；**真实任务日志待补** |
| 关窗后继续且重开可见 | ✅ 代码 + v164 真机证据 | `app.py::background_supervisor`；v164 §8 第 17/18 项 |
| 高风险操作仍需审批 | ✅ | §5 矩阵 + `test_v165_phase4_modes.py`；v164 §8 第 14/15 项真机点击 |
| 三种自动化模式可用 | ✅ | 后端设置往返 422/200、前端选择器与状态条、冒烟 6 条断言 |
| Super Agent 新增质量门槛全部达标 | ⚠️ 部分 | 已测 4 项达标；2 项无分母（`null`），见 §9 |
| 真实 GUI 18 项通过 | ✅ 16/18 | §7.3；缺 #10（工作区无测试可跑）与 #16（日常记忆，v0.9.1 已有证据） |
| npm 安装和 Desktop 打包通过 | ⚠️ 见 §12 | 本轮重打安装包；npm 未重跑（CLI 包未改动） |
| 工作区干净 | 待 §12 | — |
| 报告、Commit、Tag 一致 | 待 §12 | — |

**结论：代码、自动化回归与真机 GUI（16/18）完成；仍差 55 项 Benchmark 重跑与真实
自动修复日志，因此不把 v0.10.0 写成「全部验收通过」，只写成「代码定版 + 可装机测试」。**
