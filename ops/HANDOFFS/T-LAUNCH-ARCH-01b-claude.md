# T-LAUNCH-ARCH-01b 开发交接单（运行失败与通知决策外移）

## 任务与范围

延续 `ARCH-01`：把运行失败记账与通知决策从 `api/routes/tasks.py` 移到
`services/orchestration/runner.py`，行为不变。

- 已实现：
  - 迁移 `_record_run_failure()`、`_RESUME_GRAPH_KEYS`、`_graph_decision()`、
    `_resume_decision_delivered()`、`_notify_kind()`、`_origin_from_trigger()`、
    `_record_notify_decision()` 到 `orchestration/runner.py`。
  - `_execute_claimed_run()` **暂时留在** `tasks.py`：它被
    `test_run_lease_v102` 直接调用，并 monkeypatch `tasks.LEASE_HEARTBEAT_SECONDS`；
    本轮不动它，避免把测试的补丁点从 tasks 挪走。
  - `tasks.py` 从 runner 导入这些符号，保持 `tasks._record_notify_decision`、
    `tasks._record_run_failure` 等旧路径可用；worker 的
    `task_routes._record_run_failure` 不变。
- 明确未实现：
  - `_execute_claimed_run` / `LEASE_HEARTBEAT_SECONDS` 仍在 tasks.py；
    下一步迁移时需要同步改 `test_run_lease_v102` 的 patch 目标。
  - `_run_request`、`launch_task`、`_enqueue_and_maybe_run`、`recover_*`
    仍在 tasks.py。
  - ARCH-02/03/04/07。

## 修改内容

- `services/api/src/blank_api/services/orchestration/runner.py`：
  新增 `traceback`、`select`、`db_retry`、`AgentEvent`、`AgentTrigger`、
  `notify`、`NOTIFY_APPROVAL_ONLY`、`quiet_hours_active` 导入；
  追加运行失败与通知决策函数。
- `services/api/src/blank_api/api/routes/tasks.py`：
  删除上述函数定义；从 runner 导入 `_graph_decision`、
  `_record_notify_decision`、`_record_run_failure`、
  `_resume_decision_delivered`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `import blank_api.api.routes.tasks` / `orchestration.runner` | **imports_ok** | 无循环导入 |
| `test_notify_policy_v111 + test_notifications_v111 + test_run_lease_v102 + test_worker_v90 + test_worker_recovery_v152 + test_triggers_v162` | **61 passed** | 通知、租约、worker、触发器 |
| 全量（排除 3 项 browser 环境红） | **5164 passed / 24 skipped / 3 deselected** | 21:06，`logs/pytest-arch01b-2026-11-27.log` |
| `git diff --check` | **0** | 无空白/格式错误 |

## 风险与需要审查的点

- **`inspect.getsource`**：`test_notify_policy_v111` 对
  `tasks._record_notify_decision` 做源码检查；函数已移到 runner，但导入后的
  函数对象仍指向 runner 源文件，定向 61 项已通过。
- **日志器名字变化**：通知决策日志从 `blank_api.api.routes.tasks` 变为
  `blank_api.services.orchestration.runner`。
- **`_execute_claimed_run` 边界**：它现在是“tasks 里的函数调用 runner 里的
  辅助函数”，还不是完整的执行层外移；不要据此宣称 ARCH-01 完成。
- **测试补丁点**：`test_run_lease_v102` 仍 patch `tasks.LEASE_HEARTBEAT_SECONDS`；
  下一轮迁移 `_execute` 时必须同步改，否则测试会假通过/假红。

## 建议人工验收

1. 真机跑一条失败任务，确认 `task.failed`、失败原因与通知判定仍按原样落库。
2. 真机跑一条审批通过/失败的任务，确认 `notify.decided` 与 IM 回话仍正常。
3. 全量通过后，再迁移 `_execute_claimed_run` 与 `LEASE_HEARTBEAT_SECONDS`。
