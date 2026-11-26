# T-R2-01 开发交接单：写类工具只回执

## 任务与范围

- 已实现：
  - `edit_file` / `write_file` 的工具结果 `data` 不再携带完整 `diff`；回执保留
    `path`、`changed_lines`、`added`、`removed`、`hunks`、`replacements` 等计数。
  - 完整 diff 仍写入 `FileChange`，证据页 / 变更面板的 diff 不受影响。
  - 审查范围与交付事实改用回执计数；老记录仍回退解析 `data.diff`，旧检查点不失效。
- 明确未实现：
  - `apply_patch` / `notebook_edit` 的工具结果仍带 `diff`。模型上下文早已由
    `agent/payload.py` 的 `DROP_DIFF_TOOLS` 丢弃；本轮按路线图只改 `editing.py`。
  - 没有跑真机 benchmark / token 对照：主后端当前未启动（系统内存回收），
    需要用户确认后重启再跑。
  - 没有做「当前文件状态快照」「压调用次数」；这是路线图第 2/3 项。

## 修改内容

- `services/api/src/blank_api/services/tools/editing.py`：新增 `_diff_counts` /
  `_result_counts`；写盘结果增加 `added` / `removed` / `hunks`；
  `_edit_file` / `_write_file` 的 outcome 去掉 `diff`，改带计数。
- `services/api/src/blank_api/services/agent/graph.py`：`_review_scope` 与
  `_change_facts` 优先读计数，老记录回退 diff。
- `services/api/tests/test_claude_parity_v24.py`：断言 edit/write 回执不含 diff、
  计数正确。
- `services/api/tests/test_delivery_changed_paths_v488.py`：断言只靠回执计数也能
  算交付改动与 hunk 数、审查范围。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_claude_parity_v24.py tests/test_delivery_changed_paths_v488.py tests/test_edit_batch_v400.py tests/test_edit_tolerance_v424.py tests/test_append_write_v412.py tests/test_token_trimming.py tests/test_done_checks_v427.py tests/test_closure_v489.py tests/test_delivery_unverified_v479.py -q` | **147 passed** | 编辑/补丁/交付/审查/回执 |
| `pytest tests/test_agent_v04.py tests/test_notebook_edit_v37.py -q` | **41 passed** | 工具集成与 notebook |
| `pytest tests/test_review_scope_v427.py tests/test_delivery_incomplete_v489.py tests/test_delivery_scratchpad_v168.py tests/test_delivery_snapshots_v153.py tests/test_strict_review_gate_v414.py tests/test_ui_delivery_gate_v492.py tests/test_large_file_write_v176.py tests/test_patch_form_v426.py tests/test_codex_patch_format_v149.py -q` | **64 passed** | 审查范围与交付门槛 |
| `git diff --check` | 通过 | 0 |
| 真机 benchmark | 未跑 | 后端未启动；需要重启后跑 T09 对照 |

## 风险与需要审查的点

- **关键事实**：模型上下文里的写工具 diff 早已被 `agent/payload.py:237` 的
  `DROP_DIFF_TOOLS` 丢弃；本步不会直接降低模型 token，主要减少
  `tool.completed` 事件与检查点体积，并堵住 `repl` 等旁路把 diff 带进上下文。
- 路线图的 token 目标（上下文峰值 -30%）需要下一步「当前文件状态快照」或
  「压调用次数」。T09 基线是 **19 次调用 / 487,764 prompt tokens /
  active context 25,517 tokens**，调用次数是主因。
- `_review_scope` / `_change_facts` 对没有新增计数的老事件回退解析 diff，
  兼容旧检查点。
- `apply_patch` 仍会把完整 diff 放进 outcome data；如果确认要统一，另开小步。

## 建议人工验收

1. 在测试项目里让 agent 修改一个小文件，确认：
   - 交付四行的「改动」仍显示正确文件/行数；
   - 证据页 / 变更面板仍能打开完整 diff；
   - 事件流里 `tool.completed` 的 `data` 不再出现整段 diff。
2. 跑 T09 同题，记录调用数、prompt tokens、耗时；与基线
   19 / 487,764 / 74.7s 对照。
