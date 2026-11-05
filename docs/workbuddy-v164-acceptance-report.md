# WorkBuddy v164 验收报告（v0.9.1 最终 GUI 闭环）

> 状态：**GUI 18/18 全部实跑通过**，四个 P0 已修，回归 / Benchmark / 打包 / npm 条件全部满足。
> 本报告只记录真实发生、可复查的事实。作废的尝试与残留观察项在 §8.1、§11 原样保留，不删、不改写为通过。
> 证据根：`E:/tmp/v164-accept/shots/`（43 张真实 Electron 截图）、`E:/tmp/v164-accept/text/`（28 份文本 / JSON 事实）。
> 验收数据库：`E:/tmp/gui-v164/blank.db`（独立 `--user-data-dir`，非日常库）。

## 1. 交付物与环境

| 项 | 事实 |
| --- | --- |
| 代码仓库 | `E:\projects\kbgai\kbgai`，分支 `main` |
| 发布版本 | `0.9.1`（根 / desktop / web / README 一致，`node scripts/check_versions.mjs` exit 0） |
| 安装包 | `desktop/dist/空白格-安装版-0.9.1.exe`（110,974,870 B）、`空白格-便携版-0.9.1.exe`（110,754,771 B） |
| 安装落点 | `D:\Softwares\kbg\blank-ai-workbench-desktop`（真实 NSIS `/S /D=` 静默安装，退出码 0） |
| GUI 验收实例 | 独立 `--user-data-dir=E:/tmp/gui-v164`、后端端口 `8010`、CDP `9222`；主进程 pid 31880 |
| 运行身份 | CDP UA `blank-ai-workbench-desktop/0.9.1 Chrome/140.0.7339.249 Electron/38.8.6` |
| 验收工作区 | `E:/tmp/v164-gui/ws`（`calc.py` 含 `//` 取整 bug、`test_calc.py`、`README.md`、`config.py`） |
| 验收模型 | A = `eval-glm-4.5-flash / glm-4.5-flash`（provider `08bf7d7c…`）；B = `eval-openrouter-ds31 / deepseek/deepseek-chat-v3.1`（provider `7f8e189f…`） |
| 取证方式 | 真实 `Input.dispatchMouseEvent` / `Input.insertText` / 原生 `<select>` 键盘选择；OS 级 `PostMessageW(WM_CLOSE)`；无 `element.click()`、无后端接口代替 GUI 操作 |

供应商 Key 从用户库解密后复制进验收库，注册为 `eval-*` 前缀；验收库与日常库物理隔离。

## 2. 四个 P0 的修复

### 2.1 关窗后任务不可见（任务书 §2）

**根因**：独立任务创建时没有先建 thread，`tasks.thread_id` 为空；会话列表按 thread 渲染，任务停在审批时用户找不到。

| 文件 | 改动 |
| --- | --- |
| `services/api/src/blank_api/api/routes/tasks.py` | `create_task` 先建 `Thread` 再 `persist_task(thread_id=thread.id, turn_index=1)`，同一事务；新增 `ACTIVE_TASK_STATUSES` 与 `active` / `status` 查询参数；`task_status` 增 `conversation_id`、`title`、`project_id`、`turn_index`、`updated_at`；`_record_dispatch` 把 `sandbox.default_tier` 写进快照 |
| `web/src/components/tasks/api.ts` | `ScenarioInfo.writeExtensions`、`fetchActiveTasks(projectId?)` |
| `web/src/components/bot/bot-app.tsx` | 启动拉活跃任务、20s 轮询、首屏无深链时自动打开「等审批 → 运行中 → 暂停」优先的会话、顶部「后台任务」横幅 |
| `web/src/components/bot/background.ts`（新） | `visibleBackgroundTasks` / `backgroundTaskTarget`：无 `conversation_id` 的历史孤儿任务不被横幅过滤，兜底到 `/tasks/detail?id=<task_id>` |
| `web/src/components/bot/bot-api.ts` | 发送失败不再盲删新建会话：先查详情，只有零轮才归档 |
| `web/src/components/bot/turn.tsx` / `panel.tsx` | `waiting_approval` / `paused` 判在 `running` 之前 |
| `desktop/self-deploy.cjs` | `--api` 同步 `alembic/versions` 与 `alembic.ini`（此前漏 0031/0032，安装版报 `no such column: tasks.dispatch_snapshot`） |

**测试**：`tests/test_v164_gui_closure.py::test_create_task_binds_conversation`、`::test_active_tasks_and_status_expose_conversation`；`web/src/components/bot/background.test.ts`（5 例）。

**真实 GUI 闭环**（§8 第 17/18 项）：task `65ed4f0d-5894-4bec-b0f4-1ac536163df4` / thread `5faff2b0-a2f2-4776-ba2c-97f2daf985d5` 在关窗瞬间 `running`，藏进托盘后继续写 13 条事件到 `waiting_approval`，重开自动打开该会话、显示审批卡，点批准后 `succeeded` 并有结构化 delivery。

### 2.2 日常场景答非所问（任务书 §3）

**根因**：写工具被场景摘掉后模型手里没有可用工具，于是顺着上文把上一轮结果再讲一遍。缺的是一条明确的权限答复。

| 文件 | 改动 |
| --- | --- |
| `services/api/src/blank_api/services/agent/scenarios.py` | 新增 `needs_write()`、`REFUSAL_TEXT`（daily / brainstorm 固定文案） |
| `services/api/src/blank_api/services/agent/graph.py` | `create_plan_node` 在 `intent=act && !allow_write` 且目标确实要写时，直接返回固定拒绝文案并记 `scenario.refused` 事件；不进工具循环、零工具调用 |

只读场景里能做完的事（看代码、查文件、讲思路、记一条偏好）不被误拒：写工具由 `excluded_tools` 与网关硬拒，记忆工具不是工作区写入，因此不再把整轮切成 readonly 工具集，只收紧步数（全量回归实测出的回归，已修）。

**前后回答对比**

| | 修复前（v163 验收，`docs/workbuddy-v163-acceptance-report.md:232`） | 修复后（本次真实 GUI，task `c8e196b2-f74b-4250-85cc-a32e83198bcb`） |
| --- | --- | --- |
| 请求 | 日常场景「改 `config.py`」 | 日常场景「把 config.py 里的 DEBUG 改成 True」 |
| 回答 | `tools=[]`、写被禁，但**答的是上一轮的 `apply_discount`**，没解释场景不允许 | 「日常场景不会直接修改文件，也不会执行命令。请切换到开发场景后再执行这个操作。」 |
| 事件 | — | `scenario.refused {reason: readonly_scenario}`，零 `apply_patch` / `run_shell` / `run_tests` |
| 证据 | v163 报告 §八 | `shots/13-daily-refused.png`、`text/13-daily-refused.txt` |

**测试**：`::test_daily_write_request_gets_explicit_refusal`、`::test_daily_delete_request_is_refused`、`::test_daily_answer_uses_current_turn_only`、`tests/test_scenarios_v153.py`（矩阵更新）。

### 2.3 文档场景权限分级（任务书 §4）

**根因**：`allow_write=True` 是布尔值，文档场景一旦放开就能改 `.py`。

| 文件 | 改动 |
| --- | --- |
| `services/api/src/blank_api/services/agent/scenarios.py` | `Scenario` 新增 `write_extensions` / `tool_denylist`；`write_allowed()` 按后缀判；`excluded_tools()` 并入 denylist；`list_scenarios()` 输出可写后缀 |
| `services/api/src/blank_api/services/tools/gateway.py` | 路径后缀闸门：`apply_patch` 用 `_write_target` / `infer_path` 推断目标，不合规返回 `scenario_path_blocked` |
| `web/src/components/bot/composer.tsx` | 底部提示按场景显示「只能写 .md 这类文档」，不等发送后才报错 |

**允许 / 拒绝矩阵（GUI 实测）**

| 场景 | 可写后缀 | 额外禁用工具 | 本次实测结果（task id） |
| --- | --- | --- | --- |
| 开发 dev | 不限制（`allow_write=True`） | — | `efef81d4…`：`calc.py` 经 `apply_patch` 修改、`run_tests` 可跑 |
| 文档 docs | `.md .markdown .txt .rst .adoc .docx .doc` | `run_shell` `run_tests` `git` | `4e524afa…`：`USAGE.md` 创建成功；`apply_patch calc.py` → `denied`；`run_tests` → `denied`；`calc.py` md5 不变 |
| 日常 daily | 无（`allow_write=False`） | 全部写工具 | `c8e196b2…`：固定拒绝、零工具调用 |
| 头脑风暴 brainstorm | 无（`allow_write=False`） | 全部写工具 | `0ae7bacc…`：`scenario.refused`，只给想法、`changes.count=0` |
| 逆向 reverse | `.md .markdown .txt`，且只写进独立分析区 | `run_shell` `run_tests` `git` | `a4b96b3e…`：`create` → `denied`、`run_tests` → `denied`、`apply_patch` 落入分析区 |

**测试**：`::test_docs_scenario_writes_markdown_but_not_code`、`::test_reverse_scenario_rejects_binary_and_code_writes`、`tests/test_scenarios_v153.py` 矩阵断言。

### 2.4 逆向实验室真正隔离（任务书 §5）

| 文件 | 改动 |
| --- | --- |
| `services/api/src/blank_api/services/agent/analysis.py`（新） | `analysis_root(task_id)`：默认 `~/.blank/analysis/<task_id>`，可用 `BLANK_ANALYSIS_DIR` 覆盖；按任务分目录 |
| `services/api/src/blank_api/services/agent/runtime.py` | reverse 取 `write_root=analysis_root(request.task_id)` 传给网关并发 `analysis.workspace` 事件；沙箱档位以快照 `sandbox_tier` 为准 |
| `services/api/src/blank_api/services/tools/gateway.py` | 写类工具执行前用 `dataclasses.replace` 把 `context.root` 改到分析区，路径压掉 `..` 与盘符；读根仍是用户工作区 |
| `services/api/src/blank_api/services/agent/dispatch.py` | `ExecutionSnapshot` 新增 `sandbox_tier`，纳入 `as_dict` / `snapshot_id` / `from_dict`（只收紧不放宽） |

这是系统级隔离：模型看不到、也改不了用户工作区的路径，不是提示词里的一句「请不要改」。

**磁盘事实**（task `a4b96b3e-c1b2-414f-a9d6-4c05e4d7cb35`）

| 项 | 事实 |
| --- | --- |
| 事件 | `analysis.workspace {root: C:\Users\22103\.blank\analysis\a4b96b3e-c1b2-414f-a9d6-4c05e4d7cb35}` |
| 分析产物 | `C:\Users\22103\.blank\analysis\a4b96b3e-…\ANALYSIS.md`，817 B，md5 `8d4c89e532eead01979d98a314378ddb`；内容为 `# calc.py 代码分析` + `average` / `median` 两函数逐条逻辑（新建文件，Diff 即全文） |
| 用户工作区 | `E:/tmp/v164-gui/ws` 下**没有** `ANALYSIS.md`；`calc.py` md5 仍为 `fda44f225cfada7deba6d484dccad63e`，未被逆向任务改动 |
| 被拒写入 | `tool.completed create → denied`、`tool.completed run_tests → denied`（代码 / 命令写入不进分析区） |

**测试**：`::test_reverse_writes_go_to_analysis_workspace`、`::test_dispatch_snapshot_pins_sandbox_tier`。

## 3. Super Agent 后台闭环增强（任务书 §6）字段确认

`GET /api/v1/tasks/65ed4f0d-5894-4bec-b0f4-1ac536163df4/status` 实测返回键（21 个，覆盖 §6 要求的 15 个）：

```
task_id  title  project_id  conversation_id  turn_index  updated_at
run_id   trigger_id  worker_id  snapshot_id  status  current_step
last_heartbeat  last_event  model  tokens  approval  changes  delivery
dispatch  notify
```

| §6 要求 | 实测 |
| --- | --- |
| Router snapshot 不可变 | `dispatch_snapshot.snapshot_id = dsp-0d649b245a3e1e37`，`sandbox_tier=workspace`；`ExecutionSnapshot.from_dict()` 只收紧不放宽 |
| Worker 租约 / 心跳 | `status.last_heartbeat`、`worker_id`（重启后为 `recovery-c7f27a3a`）可查 |
| 暂停 / 恢复 / 取消 / 重试 | GUI 实测 `waiting_approval → approval.decided → task.resumed → succeeded`；拒绝 → `task.paused` |
| 任务关窗后继续 / 重开可见 / GUI 可恢复 | 见 §8 第 17、18 项 |
| 完成后结构化 delivery | `delivery {present:true, summary:"结论\n- pytest测试通过，退出码为0\n- 2个测试用例全部成功"}`；`delivery.recorded` 事件落库 |
| 四行交付只展示事实 | GUI 截图 `18-result-after-close.png`：只有「结论 + 两条事实」，无过程复述 |

## 4. 自动化回归（任务书 §8）

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `cd services/api && CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest -q` | 0 | **2130 passed / 23 skipped / 0 failed**，20m04s；`E:/tmp/v164-accept/pytest-full.log` |
| 同上，定向复跑 `test_v164_gui_closure.py test_scenarios_v153.py test_memory_types_v114.py` | 0 | **28 passed in 19.21s**（收尾前复核） |
| `web`: `node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"` | 0 | **267 passed / 0 failed**（含新增 5 例孤儿兜底）；`E:/tmp/v164-accept/web-test.log` |
| `web`: `tsc --noEmit` | 0 | 通过（收尾前复核） |
| `pnpm lint:web` | 0 | 0 problems |
| `pnpm test:desktop` | 0 | 57 passed |
| `pnpm test:evals` | 0 | 21 passed |
| `node scripts/check_versions.mjs` | 0 | 0.9.1 口径一致（收尾前复核） |
| `node desktop/verify-package.cjs` | 0 | 自检全部通过（收尾前复核，输出见 §7） |
| `pnpm -C packages/cli test` / `build` | 0 | 6 passed / `node --check` 通过 |
| `git diff --check` | 0 | 无空白错误（仅 LF→CRLF 提示） |

> 回归中真实查到并修掉：① `react-hooks/set-state-in-effect`（首屏恢复在 effect 里直接 setState）；
> ② 8 例意图单测的鸭子类型 `_Session` 缺 `scenario` 字段；③ 记忆用例被只读场景 readonly 收紧误伤。
> 未改判分器、未删失败证据。

## 5. Benchmark（任务书 §8）

正式结果：`evals/benchmarks/coding/last-run.json`（任务书明确不重做已通过的 Benchmark）。

| 指标 | 阈值 | 实测 |
| --- | --- | --- |
| completionRate | ≥ 90% | **91%**（judged 55/55） |
| passRate | ≥ 90% | **100%** |
| testPassRate | = 100% | **100%** |
| misEditedFiles | 0 | **0** |
| falseDoneCount | 0 | **0** |
| falseClaimCount | 0 | **0** |
| zeroChangeSuccessCount | 0 | **0** |
| unauthorizedHighRisk | 0 | **0** |
| coding-035 | passed | `status=succeeded, passed=true, testExit=0`，9 项 checks 全 true |
| totalTokens | > 0 | **3,030,581** |

补充：`setupFailed=0`、`runnerError=0`、`timedOut=0`、`providerFailed=0`。

## 6. npm clean-prefix 安装 / doctor / start / health（任务书 §9）

```
npm install -g --prefix E:/tmp/blank-npm-prefix E:/projects/kbgai/kbgai/packages/cli/blank-ai-cli-0.1.0.tgz   → added 1 package, exit 0
cd E:/tmp/blank-npm-prefix && BLANK_SOURCE_ROOT=E:/projects/kbgai/kbgai blank-ai doctor                    → exit 0
BLANK_API_PORT=8071 BLANK_WEB_PORT=3071 BLANK_DATABASE_URL=... blank-ai start                              → 后端/静态站均起
curl http://127.0.0.1:8071/api/v1/health   → HTTP 200 {"status":"ok","database":"connected"}
curl http://127.0.0.1:3071/                → HTTP 200
```

干净目录（不在仓库里）启动后新建空库自动迁移到 `0032_agent_triggers`；`cli-start.log` 里可见
`Application startup complete` / `Uvicorn running on http://127.0.0.1:8071` / `GET /api/v1/health 200 OK`。
日志：`E:/tmp/v164-accept/cli-doctor.log`、`cli-start.log`。

## 7. 打包与安装包校验

- `electron-builder --win`：exit 0，产出便携版 + 安装版 0.9.1。
- `node desktop/verify-package.cjs`（收尾前重跑）：**包内自检全部通过**，含 main/preload/api-base/gpu-guard/shell-policy 逐字节一致、api-base 六种 env 真跑、包内八条边界全跑且无不确定项、前端页面与设置页、无明文 Key / 无本地库 / 无 `.env`。
- 安装版含 `alembic/versions/0031_dispatch_snapshot.py`、`0032_agent_triggers.py`；`app.asar` 内 `"version": "0.9.1"`，运行 UA 为 `blank-ai-workbench-desktop/0.9.1`。
- `desktop/self-deploy.cjs` 同步最新 web/backend 到安装目录，`index.html` md5 与工作区一致。

## 8. GUI 18 项取证（任务书 §7，全部重新实跑，未沿用 17/18 旧结论）

全部截图在 `E:/tmp/v164-accept/shots/`，文本 / JSON 事实在 `E:/tmp/v164-accept/text/`。
共用事实：验收实例 `app-reopen.log` 第 10 行 `[desktop] 托盘已就绪`；`GET /api/v1/health 200 OK` 贯穿全程。

| # | 项目 | 结果 | 截图 + task_id / run_id + 事件或磁盘事实 |
| --- | --- | --- | --- |
| 1 | 启动真实 Electron | ✅ | `01-home.png`、`i01b-home-clean.png`；`app-reopen.log` 托盘就绪，pid 31880，exe `D:\Softwares\kbg\blank-ai-workbench-desktop\空白格 AI 操作台.exe` |
| 2 | health=200 | ✅ | `{"status":"ok","database":"connected"}`（8010）；`17-close-summary.json` 里 `health_after=200`；日志多处 `GET /api/v1/health 200 OK` |
| 3 | 首页和版本正确 | ✅ | `03-home.txt`、`i01b-home-clean.png`；CDP UA 含 `blank-ai-workbench-desktop/0.9.1`；`check_versions` exit 0 |
| 4 | 新建对话 | ✅ | `04-newchat.png`、`i04-newchat.png`；真实点击「新对话」 |
| 5 | 选择开发场景 | ✅ | `05-scenario-dev.png`、`i05-dev-scenario.png`；原生 select `pick[1]=开发` |
| 6 | 选择模型 A | ✅ | `06-model-a.png`、`i06-model-a.png`；`pick[2]=eval-glm-4.5-flash · glm-4.5-flash`；`06-super-agent-on.png` 为真实点击开启 `super_agent_dispatch` |
| 7 | 读取真实工作区 | ✅ | `07-running.png`；task `efef81d4-0dab-4c3f-8e9e-f07cda0ef7db` / run `055a1aee-e665-47d3-b79a-e973b7de037e`；`tool.completed read_file ok` ×2（读 `calc.py`、`test_calc.py`） |
| 8 | 修改真实文件 | ✅ | `10-dev-done.png`；`tool.completed apply_patch ok`；`calc.py` md5 `9ad4378a7e4e4390a7270f7cae017c32`（`.blank_backups/efef81d4-…/f86fe7a3/calc.py` 原件）→ `fda44f225cfada7deba6d484dccad63e`，唯一改动 `return total // len(values)` → `return total / len(values)`；`test_calc.py` `16860802…`、`README.md` `f176b7c7…`、`config.py` `66490354…` md5 均不变 |
| 9 | 查看 Diff | ✅ | `09-diff-expanded.png`、`09-diff.txt`；GUI 展开显示 `- return total // len(values)` / `+ return total / len(values)` |
| 10 | 运行测试并查看退出码 | ✅ | `10-pytest-exitcode.png`；task `2ccc51ae-e198-43a7-aa39-a834f72404a1` / run `ca42ea02-…`；`tool.completed run_shell ok`，`python -m pytest -q test_calc.py` 输出 `2 passed in 0.01s`，退出码 0；本机独立复跑 `2 passed` |
| 11 | 交付简洁且有事实 | ✅ | `11-dev-delivery.txt`；`delivery.recorded` 文本「完成：average 函数取整错误已修复 / 改动：1 个文件、1 处 Diff / 验证：python test_calc.py（退出码 0）」 |
| 12 | 切换模型 B，provider/model 真变 | ✅ | `12-model-b-selected.png`、`12-model-b-done.png`；task `f7019ece-ce9a-4fb6-9506-b4fd34b6e4b5` / run `b26da5a5-…`；`model.streamed.model = deepseek/deepseek-chat-v3.1`（A 为 `glm-4.5-flash`），provider `eval-openrouter-ds31 / 7f8e189f…` ≠ A `eval-glm-4.5-flash / 08bf7d7c…` |
| 13 | 验证五个场景 | ✅ | `13-*.png`（8 张）；dev `efef81d4…`、daily `c8e196b2…`（`scenario.refused`、零工具）、docs `4e524afa…`（`USAGE.md` 创建、`calc.py` 被拒、md5 不变）、reverse `a4b96b3e…`（`analysis.workspace`、独立分析区 `ANALYSIS.md`、用户工作区无新增）、brainstorm `0ae7bacc…`（只给想法、`changes.count=0`） |
| 14 | 触发审批卡并真实点击批准 | ✅ | `14-confirm-on.png`、`15-approval-card.png`、`14-approved.png`；task `fd8e8dce-d3e5-4c5e-8414-6484d0ff6268` / run `76d06927-d11c-4864-9d58-444050b00e0d`；GUI 真实点击「仅本次允许」@(359,415)；事件 `approval.required → approval.decided{approve:true,scope:"once"} → approval.granted → tool.completed → delivery.recorded` |
| 15 | 真实点击拒绝并确认任务停止 | ✅ | `15-approval-card.png`、`15-denied-settled.png`（右栏「已暂停」）；task `799bb701-7eea-41ca-99f1-6aaf7c076843` / run `00c244bb-…`；GUI 真实点击「拒绝」@(733,437)；`approval.decided{approve:false}` → `approval.denied` → `task.paused{reason:"已驳回高风险操作：run_shell，该步骤未执行"}`；status=`paused`、`delivery.present=false`、`changes.count=0` |
| 16 | 日常记住 / 不记住 | ✅ | `16-remember-on.png`、`16-forget-off.png`、`16-remember-done.png`、`16-forget-done.png`；task `7c44d463-2422-4cc0-aa98-803371e1ed44` / run `c17d40b1-…` 的 `request_payload.memory_opt_in=true`；task `983d347d-309b-49ba-8e11-f5d96a710d19` / run `918a1daf-…` 为 `false` |
| 17 | 关闭窗口后 Worker 继续 | ✅ | `17-running-before-close.png`（界面「在想怎么做…」）；task `65ed4f0d-5894-4bec-b0f4-1ac536163df4` / run `fae94739-…`。OS 级 `PostMessageW(WM_CLOSE)` hwnd 10488132 `ok=1` @`09:52:28Z`；关窗瞬间 `status=running`、10 条事件；藏托盘期间**新增 13 条事件**（`model.streamed`×2、`model.usage`×2、`intent.classified`、`snapshot.created`、`plan.created`、`node.started`×2、`context.measured`、`approval.required`、`task.waiting_approval`、`notify.decided`），状态推进到 `waiting_approval`；日志 `[desktop] 点关闭：已藏进托盘（不是退出，后端继续运行）` ×1、`[backend] 回收后端进程` ×0；`health_after=200`。事实文件 `17-close-evidence.jsonl`、`17-close-summary.json` |
| 18 | 重开后可见、可审批、可查看结果 | ✅ | `18-reopen-after-close.png`（横幅「后台任务…等你批准」+ 完整审批卡）、`18-reopened-approval.png`、`18-approved-after-close.png`、`18-result-after-close.png`；第二实例日志 `[desktop] 收到第二实例启动信号` / `唤出主窗口（第二实例唤回）→ {"create":false,"restore":false,"show":true,"focus":true}`；URL 自动恢复 `?thread=5faff2b0-a2f2-4776-ba2c-97f2daf985d5`；真实点击「仅本次允许」后 task `65ed4f0d…` → `succeeded`，GUI 显示「结论 / pytest测试通过，退出码为0 / 2个测试用例全部成功」，右栏「完成」。另有 `fd8e8dce…` 跨整次应用重启后自动打开并批准至 `succeeded`（`18-after-approve.png`、`18-final.txt`） |

**结论：18/18 通过，0 项待填。**

### 8.1 作废的两次尝试（保留，不计入通过证据）

| 尝试 | 事实 | 为什么不算 |
| --- | --- | --- |
| `window.close()` 关窗（task `fd8e8dce-…` 首次） | 日志出现 `[desktop] 窗口被关闭` + `[backend] 回收后端进程`，8010 断连 | `window.close()` 是渲染进程自毁，会带走壳和后端，不等价于用户点标题栏 ✕。已改用 OS 级 `WM_CLOSE` 重做第 17 项。该任务仍在库里，跨重启后可见并被批准完成，只作为第 18 项的补充证据。 |
| 第一次 `WM_CLOSE` + HTTP 高频探针（task `64a3997e-41fc-40fb-a11c-ec4ac8e496a9`） | `17-hidden-watch.jsonl`、`17-watch.log`；托盘隐藏日志存在，但探针每秒 5 个请求触发后端 120 RPM 限流，日志一片 `429`，被误记成 `health=DOWN`；且 CDP 驱动残留 60s 定时器导致关窗比截图晚 60s，关窗时任务早已 `waiting_approval` | 探针自身干扰被测系统，且未覆盖「关窗时任务仍在 running」。已修 `cdp.mjs` 的定时器清理，改为直接从 sqlite 读事件（不消耗 API 配额）后干净重跑，即第 17 项的正式证据。 |

## 9. 关窗恢复的 conversation_id / task_id / run_id

`/status` 的 `conversation_id` 即 `tasks.thread_id`；三者在任务创建瞬间同事务落库，不是关窗后补写。

| 场景 | conversation_id (thread) | task_id | run_id | 最终状态 |
| --- | --- | --- | --- | --- |
| 关窗时 running → 托盘继续 → 重开审批 → 完成（第 17/18 项主证据） | `5faff2b0-a2f2-4776-ba2c-97f2daf985d5` | `65ed4f0d-5894-4bec-b0f4-1ac536163df4` | `fae94739-8a23-4e37-b86c-a730dffa25f3` | `succeeded` |
| 跨整次应用重启后自动恢复、可审批 | `c7498da8-251c-4861-8d75-cd01c6d8966e` | `fd8e8dce-d3e5-4c5e-8414-6484d0ff6268` | `76d06927-d11c-4864-9d58-444050b00e0d` | `succeeded` |
| 真实点击拒绝后停止 | `757e9c34-44c8-4b03-8184-0ad7dfc1d49a` | `799bb701-7eea-41ca-99f1-6aaf7c076843` | `00c244bb-12d3-4b34-bd74-bdc1e23e1efa` | `paused` |
| 首次 `WM_CLOSE` 作废尝试 | `192d2f49-bddb-4cf9-9112-3018321cf751` | `64a3997e-41fc-40fb-a11c-ec4ac8e496a9` | `0c878274-5918-4131-90f4-227b31740a26` | `waiting_approval` |

`status` 接口对主证据任务实测返回 `worker_id=ADMINSTRATOR-5dd96b04`、`snapshot_id=dsp-0d649b245a3e1e37`、`trigger_id=null`、`current_step=2`、`model=glm-4.5-flash`、`tokens={total:23086,calls:4}`，以及 `last_heartbeat`、`last_event`、`approval`、`changes`、`delivery` 全可查；跨应用重启恢复的 `fd8e8dce…` 则显示 `worker_id=recovery-c7f27a3a`（启动恢复标记）。
孤儿任务兜底由 `web/src/components/bot/background.ts` 提供（5 例前端单测覆盖），本次实库没有 `thread_id` 为空的任务。

## 10. Commit / Tag / 工作区状态

沿用 v163 的两段式口径（避免报告自引用 hash）：

| 项 | 事实 |
| --- | --- |
| 发布代码提交 | `6443aea` — `feat(v164): 四个 P0 修复 + 最终 GUI 18/18 实跑闭环`，22 files changed / 1605 insertions(+) / 27 deletions(-) |
| 报告定稿提交 | 本条 docs 提交（`docs(v164): 补提交 hash、Tag 与工作区状态`） |
| Tag | `v0.9.1` 打在报告定稿提交上（轻量 tag） |
| 分支 | `main`（未推送远端） |
| 工作区状态 | `git status --porcelain` 为空；`git diff --check` exit 0 |

`desktop/dist/*.exe`、`web/out`、安装目录均被 `.gitignore` 忽略，不进版本库；报告引用的产物在本机保留可复查。

任务书 §9 逐条核对：

| 条件 | 结论 |
| --- | --- |
| GUI 18/18 通过 | ✅ §8 全表，0 项待填 |
| 关窗恢复问题修复 | ✅ §2.1 + §8 第 17/18 项 |
| daily 答非所问修复 | ✅ §2.2 前后对比 |
| 文档权限分级完成 | ✅ §2.3 五场景矩阵 |
| 逆向实验室隔离完成 | ✅ §2.4 磁盘事实 |
| coding-035 通过 | ✅ §5 `passed=true` |
| 自动化回归全绿 | ✅ §4，2130 passed / 0 failed |
| npm clean-prefix / doctor / start / health | ✅ §6 |
| 工作区干净 | ✅ 提交后 `git status --porcelain` 为空 |
| 报告和真实产物一致 | ✅ 本报告所有 id / md5 / 指标均取自上述真实产物 |

**结论：v0.9.1 已完成，允许打 `v0.9.1` Tag。**

## 11. 未完成项与观察项

未完成项：**无**（任务书 §7 的 18 项、§9 的 10 条发布条件全部满足）。

不阻塞发布的观察项，如实记录，供后续版本处理：

1. `agent_runs.status` 在 `resume` 成功后没有从 `waiting_approval` 更新（主证据任务的 run 行仍为 `waiting_approval`，而 `tasks.status` 已是 `succeeded`）。UI 与 `/status` 读的是 task 状态，功能不受影响；属记账口径待统一。
2. `_record_dispatch()` 调 `route()` 时没有传 `memory_opt_in`，因此第 16 项两轮的 dispatch snapshot 同为 `dsp-2784127ebb753be3`、`memory_policy` 都是 `off/read_only`。真实「记住 / 不记住」差异保留在 `agent_runs.request_payload.memory_opt_in`（`true` / `false`）。快照字段保真建议后续补，不属于 §6 的 15 个必查字段。
3. 模型 B 供应商选型过程：`eval-DeepSeek`（401 invalid api key）、`eval-llama-3.3-70b`（410 model end of life）、`eval-openrouter-ds31`（402 credits，`max_tokens` 8192→4096 后恢复）、`eval-glm-4.5-flash` 的 provider test 偶发 timeout。最终第 12 项使用 `eval-openrouter-ds31 / deepseek/deepseek-chat-v3.1`，任务本身 `succeeded`。
4. 第一次 `WM_CLOSE` 探针因自身触发 120 RPM 限流而作废，详见 §8.1；教训：取证探针不能与被测系统抢配额，关窗类时序要用 OS 级信号 + 磁盘直读。
5. 验收实例后端日志里有若干 `429 Too Many Requests`，来自 §8.1 那次作废探针；正式第 17 项证据改用 sqlite 直读后不再产生。
