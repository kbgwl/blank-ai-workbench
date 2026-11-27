# T-LAUNCH-ARCH-01d 开发交接单（请求装配外移）

## 任务与范围

继续 `ARCH-01`：把运行请求装配从 `api/routes/tasks.py` 移到
`services/orchestration/runner.py`。

- 已实现：
  - 迁移 `_factory_from()`、`_resolve_provider()`、`dispatch_of()`、
    `_run_request()`、`_routable()` 到 runner。
  - `tasks.py` 从 runner 导入这些符号，保留
    `tasks._run_request` / `tasks.dispatch_of` 等旧路径；
    `run_task`、`launch_task`、`_launch_resume` 与测试无需改地址。
- 明确未实现：
  - `_enqueue_and_maybe_run`、`launch_task`、`_defer_intent`、`_when_of`、
    `_defer_if_needed`、`recover_pending_runs`、`_launch_resume`、
    `recover_orphan_approvals` 仍在 tasks.py。
  - 本轮未跑全量；放在 ARCH-01 收口时统一跑。

## 修改内容

- `services/api/src/blank_api/services/orchestration/runner.py`：
  新增 `anyio`、`Request`、`case`、`load_project`、`ModelProvider`、`Skill`、
  `TaskRunIn`、`ExecutionSnapshot`、`candidate_from_provider`、
  `supports_vision`、`build_adapter`、`FallbackAdapter`、`criteria_text`、
  `load_attachments_for` 导入；追加五个函数。
- `services/api/src/blank_api/api/routes/tasks.py`：
  删除五个函数定义，从 runner 导入同名符号。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `import blank_api.api.routes.tasks` / `orchestration.runner` | **imports_ok** | 无循环导入 |
| worker/租约/agent/threads/projects | **65 passed** | 创建、运行、线程、项目 |
| 审批恢复/计划模式/续跑/交付快照 | **88 passed** | `_run_request` 主要分支 |
| 全量 | 未跑（ARCH-01 收口时统一跑） | — |

## 风险与需要审查的点

- **分层倒置**：runner 现在从 `api.routes.projects` 导入 `load_project`；
  没有循环，但服务层依赖路由层。后续应把 `load_project` 下移到
  `services/projects.py`，再删掉这条导入。
- **符号再导出**：`tasks.py` 仍以同名导入暴露这些函数；测试与 worker 的旧路径
  不变，但新代码应逐步直接导入 runner。
- **`dispatch_of` 的剩余调用方**：launch/recover 还在 tasks.py，本轮通过导入
  继续使用；01e 迁移后要一并清理。
- **全量未跑**：本轮只跑了 153 项覆盖请求装配的测试；ARCH-01 收口时必须跑全量。

## 建议人工验收

1. 真机创建带附件、带 fallback/角色模型的运行请求，确认模型适配器与附件仍正确。
2. 真机走一次审批恢复与计划模式运行，确认 `_run_request` 的快照/只读边界不变。
3. 继续迁移 launch/recover，然后跑全量。
