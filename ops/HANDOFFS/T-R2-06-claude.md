# T-R2-06 开发交接单：v4 任务 1b/1c + T09 评分口径 + 测试清理（第一批）

## 任务与范围

- 已实现：
  - **1b 写工具并发**：新增 `parallel_write_tools`（stable/default true）。
    `_execute_all` 把相邻、不同文件、`permission_mode=bypass` 的 `write_file` /
    `edit_file` 用 `asyncio.gather` 并发落盘；同文件、默认/接受编辑档、需审批的
    调用仍串行。事件与结果顺序不变。
  - **1c 提示词**：系统提示明确「多个文件各写/改一次能同一轮说完，就不要一个
    文件一轮；拿到写回执后直接验证，不要为确认再读文件」。
  - **T09 评分口径（方案 B 已批准）**：`expect_files` 从 `tools/links.py` 改为
    `tools/`，把任务要求里的「tools 下提供可执行入口」纳入合理实现范围，修复
    `minimal=0.6`。
  - **测试清理第一批**：
    - 删除已注册且干净的测试 worktree `.blank-worktrees/x`；worktree 7 个测试
      文件 **93 passed**。
    - 审批接口改为 202 异步入队后，更新旧断言并 `_drain_active_runs()` 等恢复：
      `test_permission_modes_v11` **34 passed**、`test_tool_grant_scope_v52`
      **17 passed**、`test_sandbox_bypass_v115` **27 passed**、
      `test_validation_approval_v157` **2 passed**、`test_v163_integration`
      **3 passed**。
    - lsp：`test_lsp_tool_v42.py` 单独 **40 passed**；与上述审批测试组合
      **123 passed**，未复现全量里的 13 个失败，暂判为全量顺序/环境相关，
      待全量复跑确认。
- 明确未实现：
  - **任务 2 lsp 13 失败**：未在全量里复现，尚无生产代码修复；需要全量测试
    失败清单再定位。
  - **任务 3 质量底线**：`done_when` blocking、钩子 fail-closed、审查确定性
    范围均未改。
  - **任务 4 推测性工具执行**未做。
  - **任务 5 其余 202 断言**（handoff/unknown/plan_mode 等文件）与全量 37
    失败收口未做。

## 修改内容

- `services/api/src/blank_api/services/features.py`：新增
  `parallel_write_tools` 开关。
- `services/api/src/blank_api/services/agent/graph.py`：`_execute_all` 增加
  不同文件写/改的并发段；`_parallel_write_ok` / `_write_path`；系统提示 1c。
- `services/api/tests/test_parallel_writes_v4.py`（新）：不同文件并发、同文件
  串行、非 bypass 串行。
- `services/api/tests/test_claude_parity_v24.py`：开关与提示词判据。
- `evals/task-samples.json`：T09 `expect_files` 改为 `tools/`。
- `services/api/tests/test_permission_modes_v11.py`、`test_tool_grant_scope_v52.py`、
  `test_sandbox_bypass_v115.py`、`test_validation_approval_v157.py`、
  `test_v163_integration.py`：202 + drain 后读证据。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest test_parallel_writes_v4.py test_claude_parity_v24.py test_features_registry.py test_agent_v04.py test_claude_fork_v30.py -q` | **110 passed** | 1b/1c 与集成 |
| `pytest test_permission_modes_v11.py test_tool_grant_scope_v52.py test_sandbox_bypass_v115.py test_validation_approval_v157.py test_v163_integration.py test_lsp_tool_v42.py -q` | **123 passed** | 审批 202 更新 + lsp 组合 |
| `pytest test_worktree_*（7 文件） -q` | **93 passed** | 清理残留后 |
| `node --check evals/compare.mjs` | 通过 | 0 |

## 风险与需要审查的点

- 1b 只在 `permission_mode=bypass` 启用：默认/接受编辑档仍串行，避免审批卡被
  gather 打乱；同文件永远串行，避免备份与读后写状态冲突。
- 1c 是模型行为提示，能否把 22 轮压到 ≤12 仍需真机 T09 复跑；当前 token 已
  达成 ≥30% 目标（372K），但轮次未达。
- T09 `expect_files` 改为目录前缀后，`tools/` 下任何文件都算合理入口；任务
  prompt 原文就是「在 tools 下提供可执行入口」，口径一致。
- lsp 13 失败未复现；如果全量仍失败，需要完整失败日志（测试名 + 输出）再改，
  不要凭猜测动生产代码。
- 审批 202 更新只是测试修补，不改产品接口；其余 14 个含审批调用的测试文件
  仍需按同一模式清理。

## 建议人工验收

1. 重启后端，跑 T09：确认 token ≤340K/调用 ≤12/分数 100（方案 B 后 minimal=1）。
2. 在完全访问档让 agent 一次改两个不同文件：确认并发落盘、结果顺序正确；
   同文件两次写仍串行。
3. 全量 pytest 出失败清单；按 202/worktree/lsp 三类逐项收口。
