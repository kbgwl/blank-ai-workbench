# C2 开发交接单

## 任务与范围

- 已实现：
  - `done_when[].check` 新增 `file_min_chars`，按 Unicode 字符数核对。
  - `set_goal` 工具 schema 可声明该判据；执行节点写回 `goal_spec` 时保留
    `done_checks`（此前会被静默丢掉）。
  - 验证节点在机器判据没过时：把 `validation.status` 改成 `failed`、增加
    `repair_rounds`、写 `goal.check_failed` 与 `repair.started` 事件，并把
    “用 `run_shell` 实测字数”的修复指令注入下一次模型调用；路由回
    `execute_action`。修复预算用尽后交审查/收尾，最终按失败状态收尾。
  - `file_min_chars` 的“读不到”（文件不存在、非 UTF-8、根目录拿不到）分别按
    failed / unknown 处理，不冒充通过。
- 明确未实现：
  - 未做真实模型“先写短、再自动补齐”的端到端；该路径由 8 条单测覆盖。
  - 未改 Verifier 的 blocking 策略；验证节点已经把判据结果写进
    `validation.status`，`resolve_status` 会据此判失败。
  - 未跑全量后端测试。

## 修改内容

- `services/api/src/blank_api/services/plan.py`：`CHECK_TYPES` 与
  `_clean_check` 支持 `file_min_chars`。
- `services/api/src/blank_api/services/tools/plan.py`：`set_goal` schema 增加
  该判据与 `min` 字段，描述要求字数门槛必须写成机器判据。
- `services/api/src/blank_api/services/agent/verifier.py`：`_run_done_checks`
  改名 `run_done_checks` 并增加 `file_min_chars` 分支。
- `services/api/src/blank_api/services/agent/graph.py`：
  - 验证节点拆成 `_run_validation_body` + `run_validation_node` 包装；
  - 新增 `_apply_goal_checks`，未过时改 `validation`、加修复轮次与提示；
  - `set_goal` 的 `goal_update` 保留 `done_checks`。
- `services/api/src/blank_api/services/agent/planner.py`：新增
  `goal_check_failed` 修复提示。
- `services/api/tests/test_goal_checks_v490.py`：新增 8 例。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 8 个验证/计划/修复测试文件 | `103 passed`，退出码 0 | 含 `test_goal_checks_v490.py` 8 例 |
| API 判据持久化 | `DONE_CHECKS=[{"type":"file_min_chars","path":"large-check.md","min":9000}]` | 独立 Python 请求验证，临时项目已删除 |
| 受控任务 `596a4dda` | 9498 字符，succeeded | 自然语言要求 9000，模型实测后写入超过门槛 |

## 风险与需要审查的点

- `file_min_chars` 只读文本文件；二进制/编码异常记 unknown，不判失败。
- 修复回路最多 `MAX_REPAIR_ROUNDS=3` 轮；用尽后不再空转，按失败收尾。
- 只有 `goal_spec.done_checks` 里真的有机器判据时才走新逻辑；纯问答/只读任务
  仍直接放行。
- 受控任务 596a4dda 没有触发 `goal.check_failed`（请求构造问题），修复分支
  依赖单测；建议人工用真实模型跑一次“先写短再补齐”。

## 建议人工验收

1. 创建一个 `done_when` 带 `file_min_chars` 的任务，先让模型写一篇短文档，
   确认事件里出现 `goal.check_failed` / `repair.started`，且任务回到执行节点。
2. 确认模型收到“用 `wc -m` 实测”的修复提示，补足后任务成功。
3. 确认修复预算用尽时任务判 failed，交付里不出现“已完成”。
4. 确认纯问答任务不会因为 `file_min_chars` 被逼去写文件。
