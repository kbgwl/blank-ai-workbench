# v163 Super Agent 完整闭环验收报告

任务书：`docs/workbuddy-v163-super-agent-completion.md`
目标版本：`v0.9.1 Super Agent Completion`
工作目录：`E:\projects\kbgai\kbgai`

## 结论先说（一句话）

后端能力（Router/Worker/Trigger/Watchdog、暂停恢复重试、低打扰、四行交付、结构化状态、
零改动伪成功拦截、coding-035 口径）已全部落地并有单测 + 集成测试 + 真实 Benchmark 证据；
**真实 Electron GUI 18 项未取证**，因此按 §9 保留当前版本 `0.9.0`，不升 `0.9.1`、不打 tag。

## 一、版本与提交

| 项 | 值 |
| --- | --- |
| 当前发布版本 | `0.9.0`（未升版，见 §9） |
| 目标版本 | `v0.9.1`（**未发布**） |
| Tag | 无（仅保留既有 `v0.9.0`） |
| Commit | 见文末「提交记录」 |
| 代码分支 | `master` |

## 二、修改文件

（见文末「提交记录」的 diffstat；新增 4 个文件：`notify.py`、`delivery.py`、
`test_v163_super_agent.py`、`test_v163_integration.py`）

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

## 八、Electron GUI 18 项（§2）—— **未完成**

本轮在 headless 环境执行，无法启动真实 Electron 窗口、无法点击审批卡片、无法做模型/场景切换的
界面取证。按 §9 明确记录：**GUI 验证未完成**，因此不升版本、不打 tag、不发布。

未取证清单（与任务书 §2 一一对应）：

| 项 | 状态 |
| --- | --- |
| 2.1 启动/health/首页/版本号/重启 | 未取证（后端 health 已单独验证） |
| 2.2 开发任务闭环 8 步（读文件→改文件→Diff→测试→四行交付） | 未取证（后端同类链路由集成测试覆盖） |
| 2.3 双 provider 模型切换（事件 + usage 真变） | 未取证 |
| 2.4 五场景切换（Router 快照/工具白名单/记忆/审批） | 未取证（快照行为有单测） |
| 2.5 审批卡片真实点击 | 未取证（禁止用 API 放行代替，故不冒充） |
| 2.6 日常记忆「记住/不记住」 | 未取证（后端 `test_scenarios_v153.py` 已覆盖开关语义） |
| 2.7 关窗后继续 + 终止 Worker 后接管 | 未取证（租约接管有单测） |

## 九、自动化回归（本轮实跑）

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `.venv/Scripts/python.exe -m pytest -q` | **2118 passed / 23 skipped / 0 failed**（18m22s） |
| 前端单测 | `node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"` | **262 / 262** |
| 前端 lint | `eslint src` | exit 0 |
| 前端类型 | `next typegen` + `tsc --noEmit` | exit 0 |
| 桌面单测 | `node --test desktop/tests/*.test.cjs`（5 个文件） | **57 / 57** |
| 评测单测 | `node --test evals/tests/judgement.test.mjs` | **21 / 21**（新增 4 例零改动判定） |
| CLI | `node --test` + `node --check src/index.mjs` | **6 / 6** + build 通过 |
| 版本口径 | `node scripts/check_versions.mjs` | 一致（发布版本 0.9.0） |
| 空白错误 | `git diff --check` | clean |

本轮新增测试 30 例：`test_v163_super_agent.py`（24）、`test_v163_integration.py`（3）、
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

1. GUI 18 项全部未取证（§八）。
2. `/tasks/{id}/status` 尚未接进 GUI 任务面板（后端已就绪，前端未验证）。
3. `blank-ai start` 未能端到端起服务（端口占用），只验证到"正确报错并退出"。

## 十二、最终工作区状态

见文末。
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