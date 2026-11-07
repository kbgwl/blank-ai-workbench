# v0.10.1 验收报告：自主执行、权限闭环与全局下拉框

> 任务单：`docs/workbuddy-v101-p0-autonomous-and-ui.md`
> 基线：v0.10.0（tag `v0.10.0`，验收报告 `docs/workbuddy-v165-acceptance-report.md`）
> 验收时间：2026-11-07
> 提交：`9ca4751`（feat(v101)）
> 验收载体：本机 web 前端（Next dev server，同源 `/api` 反代）+ 独立后端实例（8060，独立库 `E:/2026-10-15-17-39-54/.gui/data/blank.db`）+ 脚本化 OpenAI 兼容 mock 模型（8090）。
> **桌面安装版未重装**，GUI 验收跑的是同一套前端源码（详见「已知限制」）。

---

## 1. 修改文件清单

### 后端（9 文件）

| 文件 | 改动 |
| --- | --- |
| `services/api/src/blank_api/schemas/thread.py` | `ThreadTurnIn` 新增 `auto_start: bool \| None`、`permission_mode`、`memory_opt_in` |
| `services/api/src/blank_api/schemas/task.py` | `TaskCreate` 新增 `auto_start`、`permission_mode`、`memory_opt_in`；`TaskApprovalIn` 新增 `approval_id` / `tool_call_id` / `arguments_hash` |
| `services/api/src/blank_api/api/routes/threads.py` | `create_turn` 尾部：`auto_start` 终值合成 + **后端就地入队/启动**（不再依赖前端 `/run`）；启动失败记 `task.dispatch_failed` |
| `services/api/src/blank_api/api/routes/tasks.py` | `create_task` 就地启动块；`persist_task` 把权限档位钉进 `goal_spec.dispatch`；`launch_task` 补 `run.created`/`run.started`；`decide_approval` 重写为身份精确匹配（per-task 串行、409 mismatch、重复点击幂等） |
| `services/api/src/blank_api/services/agent/graph.py` | 新增 `approval_resource_path()` / `approval_identity()`（`approval_id` 确定性派生）；审批中断载荷与 granted/denied 事件带身份；**校验通道不再复用「改文件之前」那条 `run_tests` 结果**（`_mutated_after()`，见 §6.5） |
| `services/api/src/blank_api/services/agent/evidence.py` | `pending_approvals` 两个分支挂 `approval_id` / `arguments_hash` / `resource_path` |
| `services/api/src/blank_api/services/agent/events.py` | seq 锁从「实例级」改为「事件循环 + task_id 共享」，消除创建轮次的记录器与后台 runner 记录器的撞号 |
| `services/api/src/blank_api/services/agent/manager.py` | `RunManager.active_run_ids`（测试与收尾排空用） |
| `services/api/src/blank_api/worker.py` | run 认领成功后记 `run.started` |

### 后端测试（5 改 + 2 新增）

| 文件 | 改动 |
| --- | --- |
| `services/api/tests/test_backend_autostart_v101.py` | 新增 10 例：后端就地启动、`run.created < run.started`、显式 True 越过 manual、False 保持 draft、闲聊不启动、派发失败记事件仍 201、权限快照固定（turn 路径 + 独立任务路径） |
| `services/api/tests/test_approval_identity_v101.py` | 新增 7 例：卡片带完整身份、身份跨重放稳定、错 id/错 hash 409、匹配则执行且 granted 带身份、重复点击 `already_decided`、decided 事件带身份 |
| `services/api/tests/test_validation_gate.py` | 新增 4 例（§6.5 修掉的真 bug）：改文件之前的 `run_tests` 结果不得复用、改文件之后的必须复用、未成功的补丁不使旧结果过期、旧的「通过」在后续改动后不作数 |
| `services/api/tests/conftest.py` | `_drain_active_runs()`（等 runner 收尾再 drop_all）；`_restore_shared_settings_file`（用例结束恢复共享 settings.json） |
| `services/api/tests/test_agent_v04.py` | env 夹具 likewise 排空在途 run |
| `services/api/tests/test_model_switch_v153.py` | `_add_turn` 显式 `auto_start=False`（这些用例验证的正是手动 `/run` 契约） |
| `services/api/tests/test_v164_gui_closure.py` | 改为等后端启动（`_wait_terminal`），删除前端补枪式的显式 `/run` |

### 前端（19 改 + 3 新增）

**统一下拉组件（新增）**

- `web/src/components/ui/select-option.ts` — 纯逻辑：`moveCursor` / `placementFor` / `menuStyle`（向上弹用 `bottom` 贴锚点上沿、右对齐触发器等，见 §6.1）
- `web/src/components/ui/select-popover.tsx` — portal 渲染、fixed 定位、键盘 ↑↓/Home/End/Enter/Esc、aria haspopup/listbox/option、compact/regular 两档
- `web/src/components/ui/select-option.test.ts` — 8 例（光标循环、方向判定、上下两侧空间收紧）

**原生 `<select>` 全量替换（23 处，业务界面残留 0）**

| 文件 | 处数 |
| --- | --- |
| `web/src/components/bot/composer.tsx` | 4（场景 / 模型 / 权限 / 删除原生选择器） |
| `web/src/app/team/page.tsx` 组件 `team/team-view.tsx` | 5 |
| `web/src/components/tasks/task-list-pane.tsx` | 3 |
| `web/src/components/settings/provider-edit-panel.tsx` | 3 |
| `web/src/app/brainstorm/page.tsx` | 2 |
| `web/src/components/settings/agent-runtime-panel.tsx` | 2 |
| `web/src/components/settings/provider-create-form.tsx` | 2 |
| `web/src/components/model.tsx` | 1 |
| `web/src/app/reverse-lab/page.tsx` | 1 |
| `web/src/components/settings/role-model-settings.tsx` | 1 |
| `web/src/components/settings/stream-demo.tsx` | 1 |
| `web/src/components/tasks/bits.tsx` | 删除自制 `Select` 组件 |

**链路与状态**

- `web/src/components/tasks/api.ts` — `TaskDraft` 加 `permissionMode` / `memoryOptIn` / `autoStart`；`createTask` / `createTurn` 发送 `permission_mode` / `memory_opt_in` / `auto_start`；`runTask` 带 `permissionMode`；`decideApproval` 带身份三件套
- `web/src/components/tasks/timeline.ts` — approval 块带 `approvalId` / `argumentsHash`
- `web/src/components/bot/bot-api.ts` — 发送时带权限档；兜底 `/run` 只在后端没启动（status=draft）时触发
- `web/src/components/bot/bot-app.tsx` — 权限档持久化（`blank.bot.permission`，白名单四档）；审批回调传身份
- `web/src/components/chat/chat-home.tsx`、`workbench/workbench.tsx`、`tasks/tasks-view.tsx` — 三个入口从「创建后前端补 `/run`」改为 `autoStart: true` 交给后端；手动「运行」按钮保留
- `web/src/components/tasks/timeline.ts` / `workbench` / `tasks-view` — 审批卡提交具体 `approval_id` / `tool_call_id` / `arguments_hash`

---

## 2. 数据库迁移说明

**无新增迁移。** 本轮不建新表、不加列：权限快照存在既有 `tasks.goal_spec`（JSON）与 `tasks.dispatch_snapshot`（JSON）里，审批身份由事件流派生（`approval_id = "apr-" + sha256(task_id|call_key|tool|args_hash)[:16]`），可随时重算，不需要落表。

GUI 验收用的独立库从零跑过 `alembic upgrade head`（`0033_chat_goals` 为 head），启动正常。

## 3. API schema 变化

| 端点 | 变化 |
| --- | --- |
| `POST /threads/{id}/turns` | 请求体新增 `auto_start: bool \| null`、`permission_mode`、`memory_opt_in`；响应行为变化：`auto_start` 时任务**就地启动**，返回状态可能是 `queued`/`running` |
| `POST /projects/{id}/tasks` | 同上（`auto_start` 默认 `false`，老客户端行为不变） |
| `POST /tasks/{id}/approval` | 请求体新增 `approval_id` / `tool_call_id` / `arguments_hash`；不匹配返回 **409 `approval_mismatch`**（「审批对象已变化，请重新生成审批请求」）；重复点击返回 200 `already_decided` |
| `GET /tasks/{id}/evidence` | `pending_approvals[]` 新增 `approval_id` / `arguments_hash` / `resource_path` / `permission_mode` / `permission_rule` |
| 事件 | `approval.required` / `approval.decided` / `approval.granted` / `approval.denied` 带 `approval_id` + `tool_call_id` + `arguments_hash` + `resource_path`；新增 `task.auto_started`（`source: backend`）、`task.dispatch_failed`、`run.created`、`run.started` |

所有新字段都有默认值，老客户端不受影响。

---

## 4. 前端组件迁移清单

| 原实现 | 新实现 |
| --- | --- |
| 27 处原生 `<select>` | `<SelectPopover>`（统一组件，portal 渲染） |
| 各处手写下拉样式 | `select-option.ts` 纯逻辑 + `select-popover.tsx` 视图 |
| 发送栏无权限入口 | `[场景] [模型] [权限]`，权限三档带说明与边界提示，选择持久化 |
| 创建后前端调 `runTask` | `autoStart: true` 交给后端；手动「运行」仍保留 |

业务界面 `<select>` 数量：**0**（`/`、`/settings`、`/reverse-lab`、`/team`、`/tasks`、`/brainstorm`、`/projects` 逐页实测）。

---

## 5. 测试命令与退出码

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `services/api/.venv/Scripts/python.exe -m pytest -q` | 0 | **2216 passed, 23 skipped**（含 §6.5 修复后新增的 4 例，752s） |
| `pnpm -C web test` | 0 | **281 passed / 0 failed** |
| `pnpm -C web lint` | 0 | 0 problems |
| `pnpm typecheck:web` | 0 | 无类型错误 |
| `pnpm -C web build`（`BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8060`） | 0 | 静态导出 13 条路由全部生成 |

> 前端测试必须用 `pnpm test`（脚本内含 `--import ./test-bootstrap.mjs`，负责解析 `@/` 别名）。直接用裸 `node --test` 会因别名解析失败而假红 12 个文件——已在基线上验证过同样失败。

## 6. 真实 GUI 验收

截图存于 `docs/acceptance/v101/`（13 张），全部由真实 Chromium 渲染、真实后端、真实任务执行产生。

### 6.1 下拉框（§10 第一组）

| 页面 | 结果 | 截图 |
| --- | --- | --- |
| 首页 · 场景 | 5 个场景带说明与「只读」警示，选中项打勾 | `02-home-menu-0.png` |
| 首页 · 权限 | 询问 / 完全访问 / 计划，各带范围说明与边界警示 | `02-home-menu-2.png` |
| 设置页 · 模型 | 30 个供应商选项完整、可滚动 | `04-settings-menu-0.png` |
| 逆向实验室 | 模型 + 项目两个下拉 | `06-reverse-menu-0.png` |
| 深色主题 | 浮层底色 `rgba(255,255,255,0.035)`、文字 `rgb(233,238,248)`，非白色系统菜单 | `31-dark-scenario-menu.png` |
| 键盘 | `↓↓Enter` → 场景变「日常」；`End Enter` → 权限变「计划」；`Esc` 关闭 | `33-dark-permission-plan.png` |
| 原生 select 计数 | 7 个页面均为 0 | — |

**本轮修掉的真实缺陷**（GUI 第一次跑就暴露）：向上弹的菜单原先用 `top = anchorTop` 定位，既盖住触发器又从视口底部溢出（截图见修复前的 `02-home-menu-0`），且左对齐展开会横着压住发送按钮。改为 `bottom` 贴锚点上沿 + 右对齐触发器 + 按可用空间收 `maxHeight`，并补了 3 个单测（`select-option.test.ts`）。

### 6.2 完全访问（§10 第二组）

1. 开发场景 + 输入栏选「完全访问」，触发器显示「完全访问」、底部提示边界；
2. 只说一句「创建 test.txt 并写入 hello」，**只点发送**；
3. 前端**零** `/run` 调用，后端就地启动：

```
task.created → message.observed → goal.created → goal.linked → task.dispatched
→ thread.turn → run.created → run.started → task.auto_started → task.started
→ plan.created → tool.requested → tool.completed → file.changed
→ verification.started → validation.completed → delivery.recorded
→ verification.verdict → task.completed
```

4. `workspace/test.txt` 内容为 `hello`（真实落盘）；
5. 工具权限记录 `{"outcome": "allow", "rule": "mode:bypass"}`；
6. 交付文本四行：完成 / 修改 1 文件 1 Diff / 未做自动化校验（工作区无测试配置）/ 需要你：无。

截图：`13-done.png`。

### 6.3 关闭窗口（§10 第三组）

1. 发任务后等到 `queued`（后端已接管），**直接 `browser.close()`**（等价于用户关窗）；
2. 关窗期间不碰任何前端，只轮询后端：任务跑完 `succeeded`，事件链完整（含 `file.changed`）；
3. 重新开窗：任务在左侧「今天」列表可找回，状态 `succeeded`，`leftNoOneInCharge = false`（没有任何一次停在 `planned`/`draft`）。

截图：`21-reopened.png`。

### 6.4 截图授权（§10 第四组）

1. 上传 PNG 到项目，mock 让 Agent 写 `.env`（敏感路径 → `risk_for = high`）；
2. 审批卡出现，载荷完整：

```json
{
  "approval_id": "apr-295db933311c8028",
  "tool_call_id": "call-gui-1",
  "arguments_hash": "5ae913cceac4540e09410e6a9b38ccc67fee395052ca1306504e1efab65971a5",
  "resource_path": ".env",
  "risk_level": "high",
  "permission_rule": "risk:ask",
  "approval_options": [{"scope": "once", "label": "仅本次允许"}, {"scope": "tool", "label": "本会话总是允许 apply_patch"}]
}
```

3. 点「仅本次允许」→ 事件链 `approval.required` → `approval.decided` → `approval.granted` → `tool.completed(ok)`，**四条的 approval_id / tool_call_id / arguments_hash / resource_path 完全一致**；
4. `.env` 真实创建，内容 `GUI_TOKEN=1`；
5. 页面上没有「没有同意」字样，事件流里也没有。

截图：`40-approval-required.png`、`42-final.png`。

### 6.5 自动修复（§10 第五组）

工作区里造一个**真失败**的 pytest 项目：`calc.py` 的 `add` 写成减法，`tests/test_calc.py` 断言加法（`2 passed` 是唯一正确终态）。用户只发一句「跑一下项目测试，如果有失败就修好，再跑一遍确认通过」，**全程只点一次发送**。

**第一次跑就抓到一个真 bug，不是验收脚本的问题**：任务被判 `failed`，交付理由写着「交付文本声称测试通过，实际验证结果不支持」，两次 `validation.completed` 的输出**逐字节相同**（都是修之前的 `assert -1 == 5`），而磁盘上的 `calc.py` 明明已经改成 `a + b`。

根因在 `run_validation_node`：它从 `tool_results` 里直接取最后一条 `run_tests` 当证据。于是「**先跑测试看到失败 → 再改代码**」这个最自然的修复顺序，永远被判成「改了也没通过」，还会因为「相同失败重复出现」把修复回路提前停掉。真实代价是：代码已改对，任务判失败，用户看到的是「明明修好了却说我失败」。

修法：只有发生在**最后一次成功写操作之后**的 `run_tests` 才能当证据（`graph.py::_mutated_after()`）；否则校验通道自己再跑一次。补 4 个单测，其中两条是反向守卫——旧的「通过」在后续改动后同样不作数、失败的补丁不使前一次结果过期。

修完重跑同一条任务（`4e1f44cb…`）：

| 步骤 | 证据 |
| --- | --- |
| ① 首轮测试真失败 | `tool.requested`/`tool.completed`，`run_tests`，`status=failed`（真实退出码 1，2 failed） |
| ② 自动修 | `apply_patch` ok，`summary` = 「已修改文件：calc.py（2 行变更）」，紧跟 `file.changed` |
| ③ 校验通道**重跑** | 新的 `tool.completed(run_tests, ok)` → `validation.completed`，`passed=true`、`exit_code=0`、命令为工作区自带 venv 的 `python -m pytest -q` |
| ④ 结论与收尾 | `verification.verdict` = `passed`（`tests_executed` / `exit_code_zero` / `goal_met` 全 true）→ `task.completed` |

- 任务 `status = succeeded`，交付文本「验证：…-m pytest -q（退出码 0）」；
- 磁盘核对：`calc.py` 由 `return a - b` 变为 `return a + b`；
- 顺带验证了「项目 venv 优先」这条：`run_tests` 自动识别出 `tests/` 并用工作区 `.venv` 的解释器（pytest 9.2.0），而不是后端进程那个没有 pytest 的解释器。

截图：`50-autofix-before.png`（发送前）、`51-autofix-running.png`（跑测试/改代码过程中）、`52-autofix-done.png`（终态 succeeded）。

## 7. 真实任务事件日志（摘要）

完全访问（`e750c96d…`）：41 条事件，关键路径见 §6.2。
关窗（`d9f83337…`）：43 条事件，关窗后 30 秒内由 `succeeded` 收尾。
授权（`67e77ad5…`）：审批四连事件身份一致，`tool.completed` 为 `ok`。
自动修复（`4e1f44cb…`）：50 条事件，`run_tests(failed) → apply_patch → file.changed → run_tests(ok) → validation.completed(passed) → verification.verdict(passed) → task.completed`。

原始 JSON 可通过 `GET /api/v1/tasks/{id}/events` 复现。

## 8. 构建日志

```
▲ Next.js 16.0.x (Turbopack)
✓ Compiled successfully
○  (Static)  prerendered as static content
13 条路由：/ /brainstorm /daily /evidence /features /projects /reverse-lab
          /settings /skills /tasks /tasks/detail /team
build rc=0
```

---

## 9. 未完成问题

1. **桌面安装版未重装**：本轮 GUI 验收用 web 前端 + 独立后端，`D:\Softwares\kbg\blank-ai-workbench-desktop` 里仍是 v0.10.0 的包。要交付桌面版需按 `MEMORY.md` 的打包流程重跑 electron-builder（含 winCodeSign 手工补包），本轮未做。**因此 `package.json` / `desktop/package.json` 的版本仍是 `0.10.0` 未动**——改版本号却不发包，只会让安装目录里实际跑的东西和声明的版本对不上，等真打包时一并改成 `0.10.1`。
2. **未跑 55 项 Benchmark**：模型侧行为（自主循环质量）没有用评测集复测，基线文件仍是 `evals/benchmarks/last-run-v163.json`。
3. **`run_tests` 审批**：GUI 验收里第二次 `approval.required`（`run_tests`）未被点击（那是 mock 脚本在第 3 步返回交付文本后仍触发的验证动作）——不影响结论，但说明「验证命令也要审批」这条路径只有单测覆盖（§6.5 那轮用的是完全访问，同样没点审批卡）。

## 10. 已知限制

1. **`_drain_active_runs` 是测试装置补丁**，不是产品能力：它保证用例之间不因 SQLite 单写者撞车，生产环境靠 `run_manager.recover()` 与 worker 认领。
2. **事件 seq 锁只在进程内生效**。跨进程（独立 worker 与 API 同时写一个任务）仍靠 `IntegrityError` 重试 5 次；撞满 5 次会如实抛错（丢事件比丢写入更糟，所以不静默吞）。
3. **`approval_id` 是派生的，不是持久化实体**。好处是重放幂等、无需迁移；代价是「历史上到底批过哪几次」只能从事件流回溯，没有独立审批表可查。
4. **越界路径是硬禁止**（`capability:path_escape`），不会生成审批卡。任务单里「读上传截图要点允许」在本产品语义下是「不许读」，不是「问一下」——实测记录在 §6.4 的排查过程中。
5. **`auto_start` 压不过 `blocked` / `wait_user`**：环境本身不成立（没工作区/没场景/没目标）时，用户显式要求立即执行也只会留在 draft。这是刻意的。
6. **五组 GUI 验收用的都是脚本化 mock 模型**（8090，OpenAI 兼容）。因此验收证明的是「产品侧链路真的会这样做」——后端接管、审批身份、校验重跑、下拉渲染；它不证明「真实模型会写出正确的修复补丁」。后者只能靠 55 项 Benchmark，本轮未跑（§9.2）。
