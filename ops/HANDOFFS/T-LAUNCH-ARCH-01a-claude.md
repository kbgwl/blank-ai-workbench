# T-LAUNCH-ARCH-01a 开发交接单（tasks.py 第一批领域动作外移）

## 任务与范围

按 `docs/MASTER-LAUNCH-PLAN.md` Phase 2 `ARCH-01` 做第一刀：先把任务创建与
派发快照从 HTTP 路由层移到独立的编排层，行为不变。

- 已实现：
  - 新增 `services/orchestration/runner.py`，容纳 `persist_task()` 与
    `_record_dispatch()`；两者只依赖模型、设置、上传、计划与派发服务，
    不反向导入 `api/routes/tasks.py`。
  - `api/routes/tasks.py` 删除这两个函数，改为从新模块导入并保留同名符号；
    `create_task`、`worker._task_for_trigger`、`channels/inbound.py` 与测试的
    现有导入路径都不需要改。
- 明确未实现：
  - `tasks.py` 仍包含 `_resolve_provider`、`_run_request`、`launch_task`、
    `_execute_claimed_run`、`recover_pending_runs`、`recover_orphan_approvals`
    等大块编排逻辑；本卡只是第一批，尚未达到计划里的 `<400 行`。
  - `ARCH-02` graph.py 拆分、`ARCH-04` AgentRun 1:N、`ARCH-03` 统一入口、
    `ARCH-07` 团队任务入库。

## 修改内容

- 新增 `services/api/src/blank_api/services/orchestration/__init__.py`。
- 新增 `services/api/src/blank_api/services/orchestration/runner.py`：
  `persist_task()`、`_record_dispatch()` 原样迁移；日志器改为
  `blank_api.services.orchestration.runner`。
- `services/api/src/blank_api/api/routes/tasks.py`：
  删除两个函数定义，新增
  `from blank_api.services.orchestration.runner import _record_dispatch, persist_task`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `import blank_api.api.routes.tasks` / `orchestration.runner` | **imports_ok** | 无循环导入 |
| `test_worker_v90 + test_projects_v02 + test_threads` | **45 passed** | 建任务、线程、worker |
| `test_agent_v04 + test_approval_card_v101 + test_triggers_v162 + test_channels_v103 + test_plan_mode_v113` | **102 passed** | 创建/派发/触发/IM/计划模式 |
| 全量（排除 3 项 browser 环境红） | **5164 passed / 24 skipped / 3 deselected** | 18:18，`logs/pytest-arch01a-2026-11-27.log` |
| `git diff --check` | **0** | 无空白/格式错误 |

## 风险与需要审查的点

- **日志器名字变化**：`persist_task`/`_record_dispatch` 的日志从
  `blank_api.api.routes.tasks` 变为 `blank_api.services.orchestration.runner`；
  如果运维按旧 logger 名过滤，需要同步。
- **符号再导出**：`tasks.py` 仍以同名导入暴露这两个函数，避免一次改所有调用方；
  后续可以逐步把调用方改为直接导入新模块。
- **尚未拆薄**：这是 ARCH-01 的第一批，tasks.py 仍然很大；不要据此宣称
  “ARCH-01 已完成”。
- **循环导入**：新模块不导入路由层；如果后续移动 `_run_request` 等函数，
  需要继续守住这条边界。

## 建议人工验收

1. 真机创建独立任务与会话内一轮任务，确认 `task.created` 与
   `dispatch.decided` 事件仍按原顺序落库。
2. 检查使用计划模板创建任务、附件上传、命令展开三条路径。
3. 全量通过后，再继续把 `launch_task`/`_execute_claimed_run` 移入编排层。
