# T-LAUNCH-ARCH-01c 开发交接单（执行函数外移）

## 任务与范围

继续 `ARCH-01`：把 `_execute_claimed_run()` 与租约心跳常量移到
`services/orchestration/runner.py`。

- 已实现：
  - `_execute_claimed_run()` 原样迁移；它继续调用 runner 里的
    `_record_run_failure` / `_resume_decision_delivered` / `_graph_decision` /
    `_record_notify_decision`。
  - `LEASE_HEARTBEAT_SECONDS` 从 `tasks.py` 移到 runner；`tasks.py` 不再定义。
  - `tasks.py` 从 runner 导入 `_execute_claimed_run`，worker 的
    `task_routes._execute_claimed_run` 与测试的 `tasks._execute_claimed_run`
    路径保持不变。
  - `test_run_lease_v102` 的 monkeypatch 目标从 `tasks.AgentRuntime` /
    `tasks.LEASE_HEARTBEAT_SECONDS` 改为 `orchestration_runner.*`。
- 明确未实现：
  - `_run_request`、`launch_task`、`_enqueue_and_maybe_run`、`_defer_*`、
    `recover_pending_runs`、`_launch_resume`、`recover_orphan_approvals`
    仍在 tasks.py。
  - 本轮未跑全量；放在 ARCH-01 系列收口时统一跑（上一刀 ARCH-01b 全量
    **5164 passed / 24 skipped / 3 browser deselected**）。

## 修改内容

- `services/api/src/blank_api/services/orchestration/runner.py`：
  新增 `asyncio`、`AgentRuntime`、`RunRequest`、`finish_run`、`renew_run`
  导入与 `LEASE_HEARTBEAT_SECONDS`；追加 `_execute_claimed_run()`。
- `services/api/src/blank_api/api/routes/tasks.py`：
  删除 `_execute_claimed_run()` 与 `LEASE_HEARTBEAT_SECONDS`；从 runner 导入
  `_execute_claimed_run`。
- `services/api/tests/test_run_lease_v102.py`：
  monkeypatch 目标改为 `orchestration_runner`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `import blank_api.api.routes.tasks` / `orchestration.runner` | **imports_ok** | 无循环导入 |
| 租约/worker/通知/触发器 6 文件 | **61 passed** | 执行、心跳、失败、通知 |
| 全量 | 未跑（ARCH-01 收口时统一跑） | 上一刀 ARCH-01b 全量已通过 |

## 风险与需要审查的点

- **测试补丁点**：`test_run_lease` 现在 patch `orchestration_runner`；其他测试仍
  patch `tasks._execute_claimed_run`（tasks 的同名导入），worker 调用的是这个
  别名，行为不变。
- **日志器名字**：执行函数日志从 `blank_api.api.routes.tasks` 变为
  `blank_api.services.orchestration.runner`。
- **全量未跑**：本轮只跑了覆盖执行路径的 61 项；ARCH-01 收口时必须跑全量。
- **仍未拆薄**：`_run_request` 与 launch/recover 还在 tasks.py；不要据此宣称
  ARCH-01 完成。

## 建议人工验收

1. 真机跑一条普通任务与一条审批恢复任务，确认租约心跳、终态与通知不变。
2. 检查 `LEASE_HEARTBEAT_SECONDS` 在 runner 中可被测试 patch，worker 仍能续租。
3. ARCH-01 收口时跑全量，再进入 ARCH-04 或 ARCH-02。
